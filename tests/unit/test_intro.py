"""Unit tests for phoning.intro (no network)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from phoning import intro


def test_build_intro_subject_body_data() -> None:
    subject, body, data = intro.build_intro("alice", {"role": "dev", "scope": "x"})
    assert subject == "intro: alice (dev)"
    assert "role: dev" in body and "scope: x" in body
    assert data == {"state": "ACTIVE", "intro": {"name": "alice", "role": "dev", "scope": "x"}}


def test_build_intro_no_role_and_truncation() -> None:
    subject, _body, _data = intro.build_intro("alice", {})
    assert subject == "intro: alice"
    long_name = "n" * 200
    subject2, _, _ = intro.build_intro(long_name, {"role": "r" * 200})
    assert len(subject2) <= 80
    assert "\n" not in subject2 and "\x00" not in subject2


def test_build_intro_cleans_controls_and_fp8() -> None:
    subject, body, _ = intro.build_intro("a\nlice", {"role": "r\noot"}, fp8="ab12cd34")
    assert "\n" not in subject
    assert "fingerprint: ab12cd34" in body


def test_load_profile_missing_returns_empty(tmp_path: Path) -> None:
    assert intro.load_profile(str(tmp_path / "nobody")) == {}


def test_profile_main_merges_and_is_private(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    home = tmp_path / "home"
    os.environ["PHONING_HOME"] = str(home)
    assert intro.profile_main(["bob", "--role", "R", "--scope", "S"]) == 0
    prof = home / "bob" / "profile.json"
    assert stat.S_IMODE(prof.stat().st_mode) == 0o600
    assert json.loads(prof.read_text()) == {"role": "R", "scope": "S"}
    capsys.readouterr()
    assert intro.profile_main(["bob", "--harness", "H"]) == 0
    assert json.loads(prof.read_text()) == {"role": "R", "scope": "S", "harness": "H"}


def test_send_intro_uses_fingerprint(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from phoning import ops as ops_mod

    user_dir = tmp_path / "home" / "alice"
    user_dir.mkdir(parents=True)
    (user_dir / "profile.json").write_text(json.dumps({"role": "R"}))
    sent: dict[str, Any] = {}

    class FakeRetalk:
        def __init__(self, _d: str) -> None:
            pass

        def id(self) -> str:
            return "ff00ff00" + "0" * 24

    def fake_send(u: str, n: str, s: str, **kw: Any) -> str:
        sent.update(kw)
        sent["_session"] = s
        return "e" * 32

    monkeypatch.setattr(intro, "Retalk", FakeRetalk)
    monkeypatch.setattr(ops_mod, "send_message", fake_send)
    eid = intro.send_intro(str(user_dir), "alice", "bob", "doorbell")
    assert eid == "e" * 32
    assert sent["subject"] == "intro: alice (R)"
    assert sent["_session"] == "doorbell"
