"""Acceptance: `"doorbells_from_registry": true` in up.json.

The session registry (`$PHONING_SESSIONS`) is read in-process through the hooks module. A stand-in "python" records the
argv and environment each doorbell would be spawned with, so nothing real is started.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from phoning import up
from phoning.mailbox import Mailbox

H_NEW = "2026-10-11T10:00:00Z"
H_OLD = "2026-10-10T10:00:00Z"


def entry(name: str, home: Path, harness: str, thread: str | None, *, bound_at: str | None = H_NEW,
          reader: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "home": str(home), "reader": reader or f"{name}-main",
                           "harness": harness, "thread": thread}
    if bound_at is not None:
        out["bound_at"] = bound_at
    return out


def write_registry(tmp: Path, reg: dict[str, Any]) -> Path:
    path = tmp / "sessions.json"
    path.write_text(json.dumps(reg))
    return path


def write_cfg(tmp: Path, **top: Any) -> Path:
    path = tmp / "up.json"
    path.write_text(json.dumps({"owners": [], **top}))
    return path


def boxes(home: Path, *names: str) -> None:
    for n in names:
        Mailbox(home / n)


def launcher(tmp: Path) -> tuple[str, Path]:
    log = tmp / "spawned.jsonl"
    exe = tmp / "fakepython"
    exe.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'argv': sys.argv[1:], 'home': os.environ.get('PHONING_HOME'),"
        " 'cwd': os.getcwd()}) + '\\n')\n")
    exe.chmod(0o755)
    return str(exe), log


def spawned(log: Path) -> dict[str, dict[str, Any]]:
    """argv/env records keyed by principal name."""
    out: dict[str, dict[str, Any]] = {}
    if log.exists():
        for line in log.read_text().splitlines():
            rec = json.loads(line)
            out[rec["argv"][3]] = rec  # [-m, phoning.cli, doorbell, NAME, ...]
    return out


def flag(argv: list[str], name: str) -> str:
    return argv[argv.index(name) + 1]


def plan(tmp: Path, cfg: Path, *, dry: bool = True) -> list[dict[str, Any]]:
    return up.reconcile(up.load_config(str(cfg)), dry_run=dry, python=launcher(tmp)[0])


def doorbells(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i["kind"] == "doorbell"]


def test_the_registry_is_ignored_unless_asked_for(home: Path, tmp_path: Path) -> None:
    boxes(home, "alice")
    write_registry(tmp_path, {"s1": entry("alice", home, "codex", "T1")})
    assert plan(tmp_path, write_cfg(tmp_path)) == []
    assert plan(tmp_path, write_cfg(tmp_path, doorbells_from_registry=False)) == []


def test_the_flag_must_be_a_bool(tmp_path: Path) -> None:
    for bad in ("yes", 1, None, [], {}):
        with pytest.raises(up.ConfigError):
            up.load_config(str(write_cfg(tmp_path, doorbells_from_registry=bad)))
    assert up.load_config(str(write_cfg(tmp_path, doorbells_from_registry=True))).get("doorbells_from_registry") is True


def test_ready_targets_become_doorbells_and_unready_ones_do_not(home: Path, tmp_path: Path) -> None:
    boxes(home, "alice", "bob", "carol", "dave", "erin")
    write_registry(tmp_path, {
        "s-codex": entry("alice", home, "codex", "T-A"),
        "s-claude": entry("bob", home, "claude", "S-B"),
        "s-muse": entry("carol", home, "muse", "M-C"),
        "s-unknown": entry("dave", home, "unknown", "X"),     # ring is null: unknown harness
        "s-nothread": entry("erin", home, "codex", None),     # ring is null: no thread
    })
    items = plan(tmp_path, write_cfg(tmp_path, doorbells_from_registry=True))
    assert [(i["kind"], i["name"], i["state"]) for i in items] == [
        ("doorbell", "alice", "would-start"), ("doorbell", "bob", "would-start"), ("doorbell", "carol", "would-start")]
    assert all(i.get("source") == "registry" and i["home"] == str(home) for i in items)


def test_registry_doorbells_are_spawned_with_the_target_s_flags(home: Path, tmp_path: Path) -> None:
    boxes(home, "alice", "bob", "carol")
    write_registry(tmp_path, {
        "s-codex": entry("alice", home, "codex", "T-A"),
        "s-claude": entry("bob", home, "claude", "S-B"),
        "s-muse": entry("carol", home, "muse", "M-C"),
    })
    exe, log = launcher(tmp_path)
    up.reconcile(up.load_config(str(write_cfg(tmp_path, doorbells_from_registry=True))), dry_run=False, python=exe)
    got = spawned(log)
    assert sorted(got) == ["alice", "bob", "carol"]
    a, b, c = (got[n]["argv"] for n in ("alice", "bob", "carol"))
    assert (flag(a, "--ring"), flag(a, "--codex-thread"), flag(a, "--hook-reader")) == ("codex-queue", "T-A", "alice-main")
    assert (flag(b, "--ring"), flag(b, "--claude-session"), flag(b, "--hook-reader")) == ("claude-resume", "S-B", "bob-main")
    assert (flag(c, "--ring"), flag(c, "--muse-session"), flag(c, "--hook-reader")) == ("muse", "M-C", "carol-main")
    for argv in (a, b, c):
        assert "--resume-dead" not in argv and "--rules" not in argv  # nothing riskier than the safe defaults
    assert got["alice"]["home"] == str(home) and got["alice"]["cwd"] == str(home / "alice")


def test_a_principal_without_a_mailbox_is_reported_not_started(home: Path, tmp_path: Path) -> None:
    write_registry(tmp_path, {"s1": entry("ghost", home, "codex", "T1")})
    (item,) = plan(tmp_path, write_cfg(tmp_path, doorbells_from_registry=True))
    assert (item["name"], item["state"], item["source"]) == ("ghost", "no-mailbox", "registry")


def test_the_newest_binding_wins_and_the_rest_are_listed(home: Path, tmp_path: Path) -> None:
    boxes(home, "alice")
    write_registry(tmp_path, {
        "s-old": entry("alice", home, "codex", "T-OLD", bound_at=H_OLD),
        "s-new": entry("alice", home, "codex", "T-NEW", bound_at=H_NEW),
        "s-mid": entry("alice", home, "claude", "S-MID", bound_at="2026-10-10T20:00:00Z"),
        "s-nodate": entry("alice", home, "codex", "T-ND", bound_at=None),
    })
    exe, log = launcher(tmp_path)
    items = up.reconcile(up.load_config(str(write_cfg(tmp_path, doorbells_from_registry=True))), dry_run=False,
                         python=exe)
    got = spawned(log)
    assert list(got) == ["alice"] and flag(got["alice"]["argv"], "--codex-thread") == "T-NEW"
    (skip,) = [i for i in items if str(i["state"]).startswith("skip")]
    assert skip["state"] == "skip: ambiguous" and skip["kind"] == "doorbell"
    assert (skip["name"], skip["home"], skip["session"]) == ("alice", str(home), "s-new")
    assert sorted(skip["others"]) == ["s-mid", "s-nodate", "s-old"]
    assert len(doorbells(items)) == 2  # the chosen doorbell plus the skip record


def test_equal_binding_times_resolve_the_same_way_every_run(home: Path, tmp_path: Path) -> None:
    boxes(home, "alice")
    write_registry(tmp_path, {"s-a": entry("alice", home, "codex", "T-A"), "s-b": entry("alice", home, "codex", "T-B")})
    cfg = write_cfg(tmp_path, doorbells_from_registry=True)
    first = plan(tmp_path, cfg)
    second = plan(tmp_path, cfg)
    assert first == second
    (skip,) = [i for i in first if i["state"] == "skip: ambiguous"]
    assert skip["session"] in ("s-a", "s-b") and skip["others"] == [({"s-a", "s-b"} - {skip["session"]}).pop()]


def test_the_same_name_under_two_homes_is_two_doorbells(home: Path, tmp_path: Path) -> None:
    other = tmp_path / "home2"
    boxes(home, "alice")
    boxes(other, "alice")
    write_registry(tmp_path, {"s1": entry("alice", home, "codex", "T1"), "s2": entry("alice", other, "codex", "T2")})
    items = plan(tmp_path, write_cfg(tmp_path, doorbells_from_registry=True))
    assert sorted(i["home"] for i in items) == sorted([str(home), str(other)])
    assert all(i["state"] == "would-start" for i in items)


def test_explicit_doorbells_win_over_the_registry(home: Path, tmp_path: Path) -> None:
    boxes(home, "alice", "bob")
    write_registry(tmp_path, {
        "s1": entry("alice", home, "codex", "T1"),
        "s2": entry("alice", home, "codex", "T2", bound_at=H_OLD),  # would be ambiguous if alice came from the registry
        "s3": entry("bob", home, "claude", "S3"),
    })
    cfg = write_cfg(tmp_path, doorbells_from_registry=True,
                    doorbells=[{"home": str(home), "name": "alice", "ring": "notify"}])
    exe, log = launcher(tmp_path)
    items = up.reconcile(up.load_config(str(cfg)), dry_run=False, python=exe)
    got = spawned(log)
    assert flag(got["alice"]["argv"], "--ring") == "notify" and "--codex-thread" not in got["alice"]["argv"]
    assert flag(got["bob"]["argv"], "--ring") == "claude-resume"
    assert [(i["name"], i.get("source")) for i in doorbells(items)] == [("alice", None), ("bob", "registry")]
    assert not [i for i in items if "skip" in str(i["state"])]  # an explicit entry means nothing was ambiguous


def test_an_explicit_home_with_a_tilde_still_matches(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    boxes(tmp_path / "hh", "alice")
    write_registry(tmp_path, {"s1": entry("alice", tmp_path / "hh", "codex", "T1")})
    cfg = write_cfg(tmp_path, doorbells_from_registry=True, doorbells=[{"home": "~/hh", "name": "alice"}])
    items = plan(tmp_path, cfg)
    assert [(i["name"], i.get("source")) for i in items] == [("alice", None)]


def test_a_missing_or_corrupt_registry_adds_nothing(home: Path, tmp_path: Path) -> None:
    cfg = write_cfg(tmp_path, doorbells_from_registry=True)
    assert plan(tmp_path, cfg) == []  # no sessions.json at all
    (tmp_path / "sessions.json").write_text("{not json")
    assert plan(tmp_path, cfg) == []
    (tmp_path / "sessions.json").write_text("[1, 2]")
    assert plan(tmp_path, cfg) == []
    assert up.main(["--config", str(cfg), "--dry-run"]) == 0


def test_up_never_writes_the_registry(home: Path, tmp_path: Path) -> None:
    boxes(home, "alice")
    reg = write_registry(tmp_path, {"s1": entry("alice", home, "codex", "T1")})
    before = (reg.read_bytes(), reg.stat().st_mtime_ns)
    plan(tmp_path, write_cfg(tmp_path, doorbells_from_registry=True))
    assert (reg.read_bytes(), reg.stat().st_mtime_ns) == before


def test_main_reports_registry_items_as_json_and_a_skip_is_not_a_failure(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    boxes(home, "alice")
    write_registry(tmp_path, {"s-old": entry("alice", home, "codex", "T-OLD", bound_at=H_OLD),
                              "s-new": entry("alice", home, "codex", "T-NEW")})
    cfg = write_cfg(tmp_path, doorbells_from_registry=True)
    assert up.main(["--config", str(cfg), "--dry-run", "--json"]) == 0
    lines = [json.loads(ln) for ln in capsys.readouterr().out.splitlines()]
    assert {ln["state"] for ln in lines} == {"would-start", "skip: ambiguous"}
    assert up.main(["--config", str(cfg), "--dry-run"]) == 0
    text = capsys.readouterr().out
    assert "skip: ambiguous" in text and "s-old" in text
