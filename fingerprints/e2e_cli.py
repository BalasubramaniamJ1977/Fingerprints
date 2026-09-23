"""Command-line front end for bulk E2E flow generation.

The Studio's E2E Test tab is for reviewing one flow at a time (or a small
live-preview batch, up to `fingerprints.e2e.MAX_BATCH`). For volumes too
large to hold in a browser tab -- "50,000 end-to-end system messages for
testing and verification" -- use this CLI (or the matching
`/api/v1/e2e/export` endpoint; both call `fingerprints.e2e.export_flows`,
so behavior is identical). Output streams straight to disk, so memory use
stays flat regardless of count.

Examples
--------
    python -m fingerprints.e2e_cli export --scenario outward --count 50000
    python -m fingerprints.e2e_cli export --scenario inward --count 5000 \
        --seed 1 --account SG00OCBC0000004242 --out-dir data/outputs
    python -m fingerprints.e2e_cli export --scenario outward --count 100 \
        --chain ibnk-channel,fraud-check,payment-processing,corebanking,clearing-settlement \
        --system fraud-check:"Fraud Check"
    python -m fingerprints.e2e_cli export --scenario-def-file rtgs.json --count 20000
    python -m fingerprints.e2e_cli list-scenarios
"""

import argparse
import json
import sys

from fingerprints.e2e import SCENARIOS, SYSTEMS, export_flows
from fingerprints.messages import parse_mx


def _parse_chain(value):
    return [s.strip() for s in value.split(",") if s.strip()] if value else None


def _parse_systems(pairs):
    """--system id:Name repeated -> [{"id": id, "name": Name}, ...]"""
    systems = []
    for pair in pairs or []:
        sid, _, name = pair.partition(":")
        systems.append({"id": sid.strip(), "name": (name.strip() or sid.strip())})
    return systems


def _parse_files(pairs):
    """--file system_id:/path/to/message.xml repeated -> {system_id: [record, ...]}
    Reads local files directly (unlike the API, which reads from its own
    upload store) -- a file with several <Document> messages becomes that
    system's test dataset, cycled one message per flow."""
    datasets = {}
    for pair in pairs or []:
        sid, _, path = pair.partition(":")
        sid, path = sid.strip(), path.strip()
        if not sid or not path:
            raise ValueError(f"--file expects system_id:path, got '{pair}'")
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        records, _ = parse_mx(text)
        if not records:
            raise ValueError(f"no <Document> message found in '{path}'")
        datasets[sid] = records
    return datasets


def cmd_list_scenarios(args):
    out = {
        "systems": SYSTEMS,
        "scenarios": [{"key": k, "label": v["label"], "chain": v["chain"],
                       "origin_message_type": v["origin_message_type"]}
                      for k, v in SCENARIOS.items()],
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_export(args):
    scenario_def = None
    if args.scenario_def_file:
        with open(args.scenario_def_file, encoding="utf-8") as f:
            scenario_def = json.load(f)
    elif args.scenario_def:
        scenario_def = json.loads(args.scenario_def)

    db = {"dsn": args.database_dsn, "table": args.database_table} \
        if args.source == "database" else None

    try:
        datasets = _parse_files(args.file)
        manifest = export_flows(
            args.out_dir, count=args.count, datasets=datasets,
            seed_base=args.seed, run_id=args.run_id,
            scenario=args.scenario, source=args.source,
            account=args.account, amount=args.amount, ccy=args.ccy,
            db=db, verdict=args.verdict,
            chain=_parse_chain(args.chain), systems=_parse_systems(args.system),
            scenario_def=scenario_def,
        )
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"error: e2e export failed: {e}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps(manifest, indent=2))
    else:
        print(f"run {manifest['run_id']}: {manifest['count']} flows, "
              f"{manifest['total_hop_messages']} hop messages")
        print(f"  scenario: {manifest['scenario']}  chain: {manifest['chain']}")
        print(f"  message types: {manifest['message_type_counts']}")
        print(f"  verdicts:      {manifest['verdict_counts']}")
        for label, path in manifest["files"].items():
            print(f"  {label:16} {path}")
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m fingerprints.e2e_cli",
        description="Bulk-generate end-to-end payment flows to files, for volumes too "
                     "large to preview live in the Studio.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list-scenarios", help="print built-in systems and flow types")
    p_list.set_defaults(func=cmd_list_scenarios)

    p_export = sub.add_parser("export", help="generate N flows and write them to disk")
    p_export.add_argument("--scenario", default="outward",
                           help="outward | inward | outward_return | inward_return "
                                "(default: outward), or any key when --scenario-def is given")
    p_export.add_argument("--scenario-def",
                           help='JSON: {"label", "chain", "origin_message_type"} for a '
                                'custom flow type (e.g. FAST, RTGS, Book Transfer)')
    p_export.add_argument("--scenario-def-file", help="path to a JSON file with the same shape")
    p_export.add_argument("--count", type=int, required=True, help="number of flows to generate")
    p_export.add_argument("--seed", type=int, default=None,
                           help="base seed; flow i uses seed+i for uniqueness")
    p_export.add_argument("--account", default=None)
    p_export.add_argument("--amount", type=float, default=None)
    p_export.add_argument("--ccy", default=None)
    p_export.add_argument("--verdict", default="ACSC", choices=["ACSC", "RJCT"])
    p_export.add_argument("--chain", default=None,
                           help="comma-separated system ids, overriding the scenario default")
    p_export.add_argument("--system", action="append", default=[],
                           help='id:Name for a custom system used in --chain; repeatable')
    p_export.add_argument("--source", default="synthetic",
                           choices=["synthetic", "file", "database"])
    p_export.add_argument("--file", action="append", default=[],
                           help="system_id:/path/to/message.xml — that system's test "
                                "dataset (source=file); repeatable")
    p_export.add_argument("--database-dsn", default=None, help="source=database")
    p_export.add_argument("--database-table", default=None, help="source=database")
    p_export.add_argument("--out-dir", default="data/outputs")
    p_export.add_argument("--run-id", default=None, help="defaults to a timestamp + random suffix")
    p_export.add_argument("--format", choices=["text", "json"], default="text")
    p_export.set_defaults(func=cmd_export)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
