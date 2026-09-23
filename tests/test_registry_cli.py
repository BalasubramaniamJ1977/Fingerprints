import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fingerprints import simulate
from fingerprints.model import Fingerprint
from fingerprints.registry import Registry
from fingerprints.registry_cli import main


def _seed_deployment_versions(root):
    """Two versions of the same service fingerprint: v1 = release 2.13,
    v2 = release 2.14 (which introduces a new error state)."""
    releases, binding = simulate.simulate_deployment()
    registry = Registry(root)
    fp13 = Fingerprint(name="svc", **binding).fit(releases["v2.13"])
    fp14 = Fingerprint(name="svc", **binding).fit(releases["v2.14"])
    registry.save("svc", fp13)
    registry.save("svc", fp14)
    return registry


def test_list_reports_empty_registry(tmp_path, capsys):
    rc = main(["--root", str(tmp_path), "list", "--format", "json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out == []


def test_list_and_versions_after_save(tmp_path, capsys):
    _seed_deployment_versions(tmp_path)

    rc = main(["--root", str(tmp_path), "list", "--format", "json"])
    entries = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert entries[0]["name"] == "svc"
    assert entries[0]["versions"] == [1, 2]
    assert entries[0]["latest"] == 2

    rc = main(["--root", str(tmp_path), "versions", "svc", "--format", "json"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == [1, 2]


def test_versions_unknown_name_errors(tmp_path, capsys):
    rc = main(["--root", str(tmp_path), "versions", "nope"])
    assert rc == 2
    assert "no fingerprint named" in capsys.readouterr().err


def test_show_prints_summary(tmp_path, capsys):
    _seed_deployment_versions(tmp_path)
    rc = main(["--root", str(tmp_path), "show", "svc", "--version", "1"])
    summary = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert "fields" in summary and "top_paths" in summary


def test_changes_detects_new_state_between_default_last_two_versions(tmp_path, capsys):
    _seed_deployment_versions(tmp_path)
    rc = main(["--root", str(tmp_path), "changes", "svc", "--format", "json"])
    reports = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert reports[0]["from"] == 1 and reports[0]["to"] == 2
    assert "T06|ERROR" in reports[0]["diff"]["states_added"]


def test_changes_text_format_lists_states_added(tmp_path, capsys):
    _seed_deployment_versions(tmp_path)
    rc = main(["--root", str(tmp_path), "changes", "svc"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "svc: v1 -> v2" in out
    assert "T06|ERROR" in out


def test_changes_fail_on_drift_sets_exit_code(tmp_path, capsys):
    _seed_deployment_versions(tmp_path)
    rc = main(["--root", str(tmp_path), "changes", "svc", "--fail-on-drift"])
    capsys.readouterr()
    assert rc == 1


def test_changes_no_drift_when_versions_are_identical(tmp_path, capsys):
    releases, binding = simulate.simulate_deployment()
    registry = Registry(tmp_path)
    fp = Fingerprint(name="svc", **binding).fit(releases["v2.13"])
    registry.save("svc", fp)
    registry.save("svc", fp)

    rc = main(["--root", str(tmp_path), "changes", "svc", "--fail-on-drift"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no changes detected" in out


def test_changes_requires_two_versions(tmp_path, capsys):
    releases, binding = simulate.simulate_deployment()
    registry = Registry(tmp_path)
    registry.save("svc", Fingerprint(name="svc", **binding).fit(releases["v2.13"]))

    rc = main(["--root", str(tmp_path), "changes", "svc"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "need >= 2" in err


def test_changes_all_skips_fingerprints_with_a_single_version(tmp_path, capsys):
    releases, binding = simulate.simulate_deployment()
    registry = Registry(tmp_path)
    registry.save("only-one-version", Fingerprint(name="x", **binding).fit(releases["v2.13"]))
    _seed_deployment_versions(tmp_path / "unused")  # separate root, not visible here

    rc = main(["--root", str(tmp_path), "changes", "--all", "--format", "json"])
    reports = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert reports == []


def test_changes_without_name_or_all_errors(tmp_path, capsys):
    rc = main(["--root", str(tmp_path), "changes"])
    assert rc == 2
    assert "pass a fingerprint NAME or --all" in capsys.readouterr().err
