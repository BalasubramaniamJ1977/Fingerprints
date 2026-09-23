"""Command-line front end for the fingerprint registry.

Lets you inspect registered fingerprints and see what changed between any two
versions (or the two most recent versions) without going through the Studio
UI or the HTTP API. Designed to be dropped into a CI/CD pipeline (see
docs/documentation.html -> "Continuous integration" for an Azure DevOps
example) so a drift/regression check can gate a build.

Examples
--------
    python -m fingerprints.registry_cli list
    python -m fingerprints.registry_cli versions payments
    python -m fingerprints.registry_cli show payments --version 2
    python -m fingerprints.registry_cli changes payments
    python -m fingerprints.registry_cli changes payments --from 1 --to 3
    python -m fingerprints.registry_cli changes --all --format json
    python -m fingerprints.registry_cli changes payments --fail-on-drift
"""

import argparse
import json
import sys

from fingerprints.registry import Registry

DRIFT_KEYS = ("states_added", "states_removed", "transition_shifts", "field_drift")


def has_drift(diff):
    return any(diff.get(k) for k in DRIFT_KEYS)


def format_diff_text(name, v_from, v_to, diff):
    lines = [f"{name}: v{v_from} -> v{v_to}"]
    if not has_drift(diff):
        lines.append("  no changes detected")
        return "\n".join(lines)

    if diff["states_added"]:
        lines.append(f"  states added:    {diff['states_added']}")
    if diff["states_removed"]:
        lines.append(f"  states removed:  {diff['states_removed']}")
    if diff["transition_shifts"]:
        lines.append("  transition shifts:")
        for s in diff["transition_shifts"]:
            lines.append(f"    {s['transition']:40} {s['before']} -> {s['after']}")
    if diff["field_drift"]:
        lines.append("  field drift:")
        for d in diff["field_drift"]:
            if "tv_distance" in d:
                lines.append(f"    {d['field']:20} tv_distance={d['tv_distance']}")
            else:
                lines.append(
                    f"    {d['field']:20} mean {d['mean_before']} -> {d['mean_after']}"
                )
    return "\n".join(lines)


def _two_versions_to_compare(registry, name, v_from, v_to):
    vs = registry.versions(name)
    if len(vs) < 2 and (v_from is None or v_to is None):
        raise ValueError(
            f"'{name}' has {len(vs)} version(s) in the registry; need at least 2 "
            "to compute a diff (or pass --from/--to explicitly)"
        )
    v_to = v_to if v_to is not None else vs[-1]
    v_from = v_from if v_from is not None else vs[-2]
    return v_from, v_to


def cmd_list(args):
    registry = Registry(args.root)
    entries = registry.list()
    if args.format == "json":
        print(json.dumps(entries, indent=2))
        return 0
    if not entries:
        print("registry is empty")
        return 0
    for e in entries:
        print(
            f"{e['name']:24} versions={e['versions']!s:16} latest=v{e['latest']:<4} "
            f"journeys={e['journeys']} events={e['events']}"
        )
    return 0


def cmd_versions(args):
    registry = Registry(args.root)
    vs = registry.versions(args.name)
    if not vs:
        print(f"no fingerprint named '{args.name}'", file=sys.stderr)
        return 2
    if args.format == "json":
        print(json.dumps(vs))
    else:
        print(" ".join(f"v{v}" for v in vs))
    return 0


def cmd_show(args):
    registry = Registry(args.root)
    fp = registry.load(args.name, args.version)
    print(json.dumps(fp.summary(), indent=2, default=str))
    return 0


def cmd_changes(args):
    registry = Registry(args.root)

    if args.all:
        names = [e["name"] for e in registry.list()]
    elif args.name:
        names = [args.name]
    else:
        print("changes: pass a fingerprint NAME or --all", file=sys.stderr)
        return 2

    reports = []
    any_drift = False
    for name in names:
        vs = registry.versions(name)
        if len(vs) < 2 and (args.vfrom is None or args.vto is None):
            if args.all:
                continue  # nothing to diff yet for this one; skip silently
            print(f"error: {name}: {len(vs)} version(s), need >= 2 to diff",
                  file=sys.stderr)
            return 2
        v_from, v_to = _two_versions_to_compare(registry, name, args.vfrom, args.vto)
        fp_to = registry.load(name, v_to)
        fp_from = registry.load(name, v_from)
        diff = fp_to.diff(fp_from)
        any_drift = any_drift or has_drift(diff)
        reports.append({"name": name, "from": v_from, "to": v_to, "diff": diff})

    if args.format == "json":
        print(json.dumps(reports, indent=2))
    else:
        if not reports:
            print("no fingerprint has 2+ versions yet -- nothing to compare")
        for r in reports:
            print(format_diff_text(r["name"], r["from"], r["to"], r["diff"]))

    if args.fail_on_drift and any_drift:
        return 1
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="python -m fingerprints.registry_cli",
        description="Inspect the fingerprint registry and diff versions from the command line.",
    )
    p.add_argument("--root", default="data/registry",
                    help="registry directory (default: data/registry)")
    sub = p.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="list every fingerprint and its versions")
    p_list.add_argument("--format", choices=["text", "json"], default="text")
    p_list.set_defaults(func=cmd_list)

    p_versions = sub.add_parser("versions", help="list versions for one fingerprint")
    p_versions.add_argument("name")
    p_versions.add_argument("--format", choices=["text", "json"], default="text")
    p_versions.set_defaults(func=cmd_versions)

    p_show = sub.add_parser("show", help="print the summary of one version")
    p_show.add_argument("name")
    p_show.add_argument("--version", type=int, default=None,
                         help="defaults to the latest version")
    p_show.set_defaults(func=cmd_show)

    p_changes = sub.add_parser(
        "changes", help="show what changed between two versions (default: last two)")
    p_changes.add_argument("name", nargs="?", help="fingerprint name (omit with --all)")
    p_changes.add_argument("--all", action="store_true",
                            help="check every fingerprint that has 2+ versions")
    p_changes.add_argument("--from", dest="vfrom", type=int, default=None)
    p_changes.add_argument("--to", dest="vto", type=int, default=None)
    p_changes.add_argument("--format", choices=["text", "json"], default="text")
    p_changes.add_argument(
        "--fail-on-drift", action="store_true",
        help="exit 1 if any state/transition/field drift is found (for CI gates)")
    p_changes.set_defaults(func=cmd_changes)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
