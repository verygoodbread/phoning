"""Acceptance: `"prune"` in up.json (docs/spec/retention.md).

`phoning up` runs `retention.main` in-process for each configured owner mailbox, at most once per `every_hours`. The last
run lives in `<home>/<name>/prune-state.json` (0600). Real mailboxes under a temporary home; nothing else is touched.
"""

from __future__ import annotations

import json
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from phoning import envelope, retention, up
from phoning.mailbox import Mailbox


def iso(days_ago: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def add(box: Mailbox, days_ago: float) -> None:
    env = envelope.make("chat", "bob", "s1", "s", "body")
    env["ts"] = iso(days_ago)
    box.append({"direction": "in", "tid": env["eid"], "peer": "bob", "peer_fp": None, "room": None,
                "recv_ts": iso(days_ago), "raw": envelope.encode(env), "env": env})


def make_box(home: Path, name: str = "alice", old: int = 6, fresh: int = 1) -> Mailbox:
    box = Mailbox(home / name)
    for _ in range(old):
        add(box, 100)
    for _ in range(fresh):
        add(box, 0)
    return box


def cfg_file(tmp: Path, owners: list[str], prune: Any = ..., **extra: Any) -> Path:
    home = tmp / "home"
    body: dict[str, Any] = {"owners": [{"home": str(home), "name": n, "session": f"{n}-s"} for n in owners], **extra}
    if prune is not ...:
        body["prune"] = prune
    path = tmp / "up.json"
    path.write_text(json.dumps(body))
    return path


def lines(home: Path, name: str = "alice") -> int:
    log = home / name / "mailbox" / "log.jsonl"
    return len([ln for ln in log.read_text().splitlines() if ln.strip()])


def pstate(home: Path, name: str = "alice") -> dict[str, Any]:
    out: dict[str, Any] = json.loads((home / name / "prune-state.json").read_text())
    return out


def prune_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [i for i in items if i["kind"] == "prune"]


class Spy:
    """Wraps retention.main: records argv, can force a return code, can refuse to be called."""

    def __init__(self, mp: pytest.MonkeyPatch, *, rc: int | None = None, forbid: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.real = retention.main

        def spy(argv: Any = None) -> int:
            assert not forbid, "retention.main must not be called"
            self.calls.append(list(argv or []))
            return rc if rc is not None else self.real(argv)

        mp.setattr(retention, "main", spy)


# ------------------------------------------------------------------ config


def test_prune_defaults(tmp_path: Path) -> None:
    cfg = up.load_config(str(cfg_file(tmp_path, [], prune={})))
    assert cfg["prune"] == {"every_hours": 24, "keep_days": 30, "keep_last": 500, "archive": True}


def test_prune_values_are_kept(tmp_path: Path) -> None:
    cfg = up.load_config(str(cfg_file(tmp_path, [], prune={"every_hours": 0.5, "keep_days": 0, "keep_last": 0,
                                                            "archive": False})))
    assert cfg["prune"] == {"every_hours": 0.5, "keep_days": 0, "keep_last": 0, "archive": False}


def test_without_a_prune_key_nothing_is_configured(tmp_path: Path) -> None:
    assert "prune" not in up.load_config(str(cfg_file(tmp_path, ["alice"])))


@pytest.mark.parametrize(
    "prune",
    [
        True, False, None, "daily", [], 24,
        {"every_hours": 0}, {"every_hours": -1}, {"every_hours": True}, {"every_hours": "24"},
        {"keep_days": -1}, {"keep_days": "30"}, {"keep_days": True},
        {"keep_last": -1}, {"keep_last": 1.5}, {"keep_last": True}, {"keep_last": "500"},
        {"archive": "yes"}, {"archive": 1},
        {"every": 24},
    ],
)
def test_bad_prune_sections_are_config_errors(tmp_path: Path, prune: Any) -> None:
    with pytest.raises(up.ConfigError):
        up.load_config(str(cfg_file(tmp_path, [], prune=prune)))


# ----------------------------------------------------------------- dry run


def test_dry_run_reports_would_prune_and_touches_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home)
    Spy(monkeypatch, forbid=True)
    before = (home / "alice" / "mailbox" / "log.jsonl").read_bytes()
    items = up.reconcile(up.load_config(str(cfg_file(tmp_path, ["alice", "ghost"], prune={}))), dry_run=True)
    assert [(i["name"], i["state"]) for i in prune_items(items)] == [("alice", "would-prune"), ("ghost", "no-mailbox")]
    assert (home / "alice" / "mailbox" / "log.jsonl").read_bytes() == before
    assert not (home / "alice" / "prune-state.json").exists()
    assert not (home / "alice" / "mailbox" / "archive").exists()


def test_dry_run_of_a_recently_pruned_mailbox_says_not_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home)
    (home / "alice" / "prune-state.json").write_text(json.dumps({"last_run": time.time() - 3600}))
    Spy(monkeypatch, forbid=True)
    items = up.reconcile(up.load_config(str(cfg_file(tmp_path, ["alice"], prune={}))), dry_run=True)
    assert [i["state"] for i in prune_items(items)] == ["not-due"]


# --------------------------------------------------------------- real runs


def test_a_due_mailbox_is_pruned_in_process_and_remembered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home, old=6, fresh=2)
    spy = Spy(monkeypatch)
    cfg = up.load_config(str(cfg_file(tmp_path, ["alice"], prune={"keep_days": 30, "keep_last": 1})))
    t0 = time.time()
    items = up.reconcile(cfg)
    (item,) = prune_items(items)
    assert item["state"] == "pruned" and item["name"] == "alice" and item["home"] == str(home)
    assert item["pruned"] == 6 and item["total"] == 8
    assert lines(home) == 2
    assert len(spy.calls) == 1
    # the argv is a real `phoning prune` command line
    ns = retention._build_parser().parse_args(spy.calls[0])
    assert (ns.name, ns.home, ns.keep_days, ns.keep_last, ns.archive) == ("alice", str(home), 30.0, 1, True)
    st = pstate(home)
    assert t0 - 1 <= st["last_run"] <= time.time() + 1
    assert stat.S_IMODE((home / "alice" / "prune-state.json").stat().st_mode) == 0o600
    archives = list((home / "alice" / "mailbox" / "archive").iterdir())
    assert len(archives) == 1  # archive: true is the default


def test_it_does_not_run_again_before_every_hours(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home)
    spy = Spy(monkeypatch)
    cfg = up.load_config(str(cfg_file(tmp_path, ["alice"], prune={"keep_last": 1})))
    assert [i["state"] for i in prune_items(up.reconcile(cfg))] == ["pruned"]
    add(Mailbox(home / "alice"), 200)  # more garbage arrives
    n = lines(home)
    assert [i["state"] for i in prune_items(up.reconcile(cfg))] == ["not-due"]
    assert len(spy.calls) == 1 and lines(home) == n


def test_it_runs_again_once_every_hours_have_passed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home)
    (home / "alice" / "prune-state.json").write_text(json.dumps({"last_run": time.time() - 25 * 3600}))
    spy = Spy(monkeypatch)
    cfg = up.load_config(str(cfg_file(tmp_path, ["alice"], prune={"keep_last": 1})))
    assert [i["state"] for i in prune_items(up.reconcile(cfg))] == ["pruned"]
    assert len(spy.calls) == 1
    # a longer interval keeps the same 25 h-old state not-due
    (home / "alice" / "prune-state.json").write_text(json.dumps({"last_run": time.time() - 25 * 3600}))
    cfg2 = up.load_config(str(cfg_file(tmp_path, ["alice"], prune={"every_hours": 48})))
    assert [i["state"] for i in prune_items(up.reconcile(cfg2))] == ["not-due"]
    assert len(spy.calls) == 1


@pytest.mark.parametrize("junk", ["{not json", "[1, 2]", '{"last_run": "yesterday"}', '{"last_run": true}', "",
                                  '{"last_run": 1e999}', '{"last_run": 9999999999999}'])
def test_an_unreadable_or_future_state_file_counts_as_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           junk: str) -> None:
    home = tmp_path / "home"
    make_box(home)
    (home / "alice" / "prune-state.json").write_text(junk)
    Spy(monkeypatch)
    (item,) = prune_items(up.reconcile(up.load_config(str(cfg_file(tmp_path, ["alice"], prune={"keep_last": 1})))))
    assert item["state"] == "pruned"
    assert pstate(home)["last_run"] <= time.time() + 1  # rewritten with a sane value


def test_archive_false_discards(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home)
    spy = Spy(monkeypatch)
    cfg = up.load_config(str(cfg_file(tmp_path, ["alice"], prune={"keep_last": 1, "archive": False})))
    assert [i["state"] for i in prune_items(up.reconcile(cfg))] == ["pruned"]
    assert not (home / "alice" / "mailbox" / "archive").exists()
    assert retention._build_parser().parse_args(spy.calls[0]).archive is False


def test_each_owner_has_its_own_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home, "alice")
    make_box(home, "bob")
    (home / "bob" / "prune-state.json").write_text(json.dumps({"last_run": time.time() - 60}))
    spy = Spy(monkeypatch)
    cfg = up.load_config(str(cfg_file(tmp_path, ["alice", "bob"], prune={"keep_last": 1})))
    got = {i["name"]: i["state"] for i in prune_items(up.reconcile(cfg))}
    assert got == {"alice": "pruned", "bob": "not-due"}
    assert [c[0] for c in spy.calls] == ["alice"]


def test_a_failing_prune_is_reported_and_retried_next_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    make_box(home)
    Spy(monkeypatch, rc=1)
    cfgp = cfg_file(tmp_path, ["alice"], prune={})
    (item,) = prune_items(up.reconcile(up.load_config(str(cfgp))))
    assert item["state"] == "failed" and "prune" in item["error"] and "rc=1" in item["error"]
    assert not (home / "alice" / "prune-state.json").exists()  # not recorded, so it is retried
    assert up.main(["--config", str(cfgp), "--json"]) == 1  # failures show in the exit code like any other item
    capsys.readouterr()


def test_an_exception_inside_prune_does_not_kill_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home)

    def boom(argv: Any = None) -> int:
        raise OSError("disk on fire")

    monkeypatch.setattr(retention, "main", boom)
    (item,) = prune_items(up.reconcile(up.load_config(str(cfg_file(tmp_path, ["alice"], prune={})))))
    assert item["state"] == "failed" and "disk on fire" in item["error"]


def test_prune_output_never_leaks_into_up_s_own_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    make_box(home)
    Spy(monkeypatch)
    cfgp = cfg_file(tmp_path, ["alice"], prune={"keep_last": 1})
    assert up.main(["--config", str(cfgp), "--json"]) in (0, 1)
    out = capsys.readouterr().out
    decoded = [json.loads(ln) for ln in out.splitlines()]  # every line is one of up's own JSON items
    assert [d["kind"] for d in decoded if d["kind"] == "prune"] == ["prune"]
    assert all("kind" in d for d in decoded)


def test_quiet_mode_prints_a_prune_but_not_a_not_due(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    make_box(home)
    Spy(monkeypatch)
    cfgp = cfg_file(tmp_path, ["alice"], prune={"keep_last": 1})
    assert up.main(["--config", str(cfgp), "--quiet"]) in (0, 1)
    assert "prune alice" in capsys.readouterr().out
    assert up.main(["--config", str(cfgp), "--quiet"]) in (0, 1)
    assert "prune" not in capsys.readouterr().out


def test_prune_items_come_last(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home)
    (home / "alice" / "identity").mkdir()
    Spy(monkeypatch, forbid=True)
    cfg = up.load_config(str(cfg_file(tmp_path, ["alice"], prune={},
                                      doorbells=[{"home": str(home), "name": "alice"}])))
    assert [i["kind"] for i in up.reconcile(cfg, dry_run=True)] == ["owner", "doorbell", "prune"]


def test_a_live_owner_does_not_stop_a_prune(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """retention takes the append lock itself; up only needs the mailbox to exist."""
    home = tmp_path / "home"
    box = make_box(home)
    lock = box.acquire_owner("owner-session")
    try:
        Spy(monkeypatch)
        cfg = up.load_config(str(cfg_file(tmp_path, ["alice"], prune={"keep_last": 1})))
        states = {i["kind"]: i["state"] for i in up.reconcile(cfg)}
        assert states["owner"] == "running" or states["owner"] == "no-identity"
        assert states["prune"] == "pruned"
    finally:
        lock.close()


def test_nothing_to_prune_is_clean_and_still_remembered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    make_box(home, old=0, fresh=3)
    spy = Spy(monkeypatch)
    cfg = up.load_config(str(cfg_file(tmp_path, ["alice"], prune={})))
    (item,) = prune_items(up.reconcile(cfg))
    assert item["state"] == "clean" and item["pruned"] == 0 and item["total"] == 3
    assert lines(home) == 3 and "last_run" in pstate(home)
    assert [i["state"] for i in prune_items(up.reconcile(cfg))] == ["not-due"]
    assert len(spy.calls) == 1
