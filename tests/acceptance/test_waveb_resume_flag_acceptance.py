"""Acceptance: a `ring` action can never resume (docs/spec/rules.md §1).

The doorbell decides the action (`ring` or `resume`) from a liveness probe and applies that action's rule. The ring
function it then calls re-probes on its own, so a session that died in between would be resumed under the `ring` rule
(no `resume` rule, no guardrails, nothing recorded). The boundary check: `resume_dead` is true only for a `resume` action.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from phoning import doorbell, harness_ring, muse_ring
from tests.acceptance.test_harness_support import (
    SID as KIMI_SID,
)
from tests.acceptance.test_harness_support import (
    FakeKimiWeb,
    fake_kimi,
    fake_lsof,
    fake_openclaw,
    fake_ps,
    make_kimi_home,
    oc_session,
)
from tests.acceptance.test_muse_ring_acceptance import Holder, session
from tests.acceptance.test_rules_common import mail

OC_SID = "ops-incident-42"


class Spy:
    def __init__(self, mp: pytest.MonkeyPatch, module: Any, func: str) -> None:
        self.kwargs: list[dict[str, Any]] = []
        real = getattr(module, func)

        def wrapper(*args: Any, **kwargs: Any) -> str:
            self.kwargs.append(dict(kwargs))
            out: str = real(*args, **kwargs)
            return out

        mp.setattr(module, func, wrapper)


def muse_args(home: Path, w: dict[str, Any], *extra: str) -> list[str]:
    return ["alice", "--once", "--home", str(home), "--ring", "muse", "--muse-session", "quiet-harbor",
            "--muse-bin", w["exe"], "--muse-home", str(w["store"]), "--hook-reader", "alice-main",
            "--min-interval", "0", *extra]


def test_a_muse_wake_never_carries_permission_to_resume(home: Path, world: dict[str, Any], holders: list[Holder],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch, muse_ring, "ring_muse")
    holders.append(Holder(session(world)))
    mail(home)
    assert doorbell.main(muse_args(home, world, "--rules", '{"resume":"prohibit"}')) == 0
    assert [k["resume_dead"] for k in spy.kwargs] == [False]


def test_a_muse_resume_does(home: Path, world: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    spy = Spy(monkeypatch, muse_ring, "ring_muse")
    session(world)
    mail(home)
    assert doorbell.main(muse_args(home, world)) == 0
    assert [k["resume_dead"] for k in spy.kwargs] == [True]


def test_kimi_web_wake_never_carries_permission_to_resume(home: Path, tmp_path: Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    work = tmp_path / "work"
    khome, _sdir = make_kimi_home(tmp_path, work)
    exe, _log = fake_kimi(tmp_path)
    ps = fake_ps(tmp_path, [(1, "/sbin/launchd")])
    lsof = fake_lsof(tmp_path, {})
    monkeypatch.setenv("PATH", os.pathsep.join([str(ps.parent), str(lsof.parent), os.environ.get("PATH", "")]))
    web = FakeKimiWeb(subscribed=[KIMI_SID])
    try:
        web.register(khome)
        spy = Spy(monkeypatch, harness_ring, "ring_harness")
        mail(home)
        assert doorbell.main(["alice", "--once", "--home", str(home), "--ring", "kimi", "--kimi-session", KIMI_SID,
                              "--kimi-bin", str(exe), "--kimi-home", str(khome), "--hook-reader", "alice-main",
                              "--min-interval", "0", "--resume-dead"]) == 0
        assert [k["resume_dead"] for k in spy.kwargs] == [False]
        assert len([r for r in web.requests if r["method"] == "POST"]) == 1
    finally:
        web.close()


def test_openclaw_gateway_wake_never_carries_permission_to_resume(home: Path, tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    exe, _log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)])
    spy = Spy(monkeypatch, harness_ring, "ring_harness")
    mail(home)
    assert doorbell.main(["alice", "--once", "--home", str(home), "--ring", "openclaw", "--openclaw-session", OC_SID,
                          "--openclaw-bin", str(exe), "--hook-reader", "alice-main", "--min-interval", "0",
                          "--resume-dead"]) == 0
    assert [k["resume_dead"] for k in spy.kwargs] == [False]


def test_openclaw_headless_resume_does(home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe, _log = fake_openclaw(tmp_path, sessions=[oc_session(OC_SID)], health="down")
    spy = Spy(monkeypatch, harness_ring, "ring_harness")
    mail(home)
    assert doorbell.main(["alice", "--once", "--home", str(home), "--ring", "openclaw", "--openclaw-session", OC_SID,
                          "--openclaw-bin", str(exe), "--hook-reader", "alice-main", "--min-interval", "0",
                          "--resume-dead"]) == 0
    assert [k["resume_dead"] for k in spy.kwargs] == [True]
