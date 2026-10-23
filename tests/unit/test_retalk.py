"""Unit tests for the retalk wrapper: argv shape and output parsing."""

from __future__ import annotations

from typing import Any

from phoning.retalk import Retalk, RetalkError


def _rec() -> list[Any]:
    return []


def _runner_factory(
    responses: dict[str, tuple[int, str, str]],
    seen: list[list[str]],
):
    def _runner(
        argv: list[str], *, stdin: str | None = None, timeout: float = 60.0
    ) -> tuple[int, str, str]:
        seen.append(list(argv))
        key = argv[1] if len(argv) > 1 else ""
        return responses.get(key, (0, "", ""))

    return _runner


def test_argv_includes_dir_relay_np(tmp_path) -> None:
    seen: list[list[str]] = []
    r = Retalk(tmp_path, _runner_factory({"id": (0, "FP", "")}, seen), relay="relay://x")
    assert r.id() == "FP"
    argv = seen[0]
    assert argv[0] == "retalk"
    assert "--dir" in argv
    assert "-r" in argv
    assert argv[-1] == "-np"
    assert "relay://x" in argv


def test_follow_cmd_named_peers_only(tmp_path) -> None:
    r = Retalk(tmp_path, _rec(), relay="relay://x")
    argv = r.follow_cmd(2, ["alice", "bob"])
    assert "--all" not in argv
    assert "--peers-only" not in argv
    assert argv.count("--peer") == 2
    assert "alice" in argv and "bob" in argv
    assert "--follow" in argv
    assert "--quiet" in argv
    assert "--save" in argv
    assert argv[-1] == "-np"


def test_follow_cmd_with_zero_peers_has_no_peer_flag(tmp_path) -> None:
    r = Retalk(tmp_path, _rec(), relay="relay://x")
    argv = r.follow_cmd(2, [])
    assert "--peer" not in argv
    assert "--all" not in argv


def test_contacts_parses_list_of_objects(tmp_path) -> None:
    payload = '[{"name": "alice"}, {"peer": "bob"}, {"alias": "carol"}]'
    r = Retalk(tmp_path, _runner_factory({"contacts": (0, payload, "")}, []))
    assert r.contacts() == ["alice", "bob", "carol"]


def test_contacts_parses_mapping(tmp_path) -> None:
    payload = '{"alice": {"id": "1"}, "bob": {"id": "2"}}'
    r = Retalk(tmp_path, _runner_factory({"contacts": (0, payload, "")}, []))
    assert r.contacts() == ["alice", "bob"]


def test_contacts_parses_ndjson(tmp_path) -> None:
    payload = '{"name": "alice"}\n{"name": "bob"}\n'
    r = Retalk(tmp_path, _runner_factory({"contacts": (0, payload, "")}, []))
    assert r.contacts() == ["alice", "bob"]


def test_contacts_parses_bare_lines(tmp_path) -> None:
    payload = "alice\nbob\n"
    r = Retalk(tmp_path, _runner_factory({"contacts": (0, payload, "")}, []))
    assert r.contacts() == ["alice", "bob"]


def test_contacts_dedupes(tmp_path) -> None:
    payload = '[{"name": "alice"}, {"name": "alice"}]'
    r = Retalk(tmp_path, _runner_factory({"contacts": (0, payload, "")}, []))
    assert r.contacts() == ["alice"]


def test_contacts_tolerates_failure(tmp_path) -> None:
    r = Retalk(tmp_path, _runner_factory({"contacts": (1, "", "")}, []))
    assert r.contacts() == []


def test_retalk_error_carries_no_bodies(tmp_path) -> None:
    def _fail(argv: list[str], *, stdin: str | None = None, timeout: float = 60.0):
        return 1, "SECRET-OUT", "SECRET-ERR"

    r = Retalk(tmp_path, _fail)
    try:
        r.id()
    except RetalkError as exc:
        assert "SECRET-OUT" not in str(exc)
        assert "SECRET-ERR" not in str(exc)
        assert exc.rc == 1
    else:  # pragma: no cover
        raise AssertionError("expected RetalkError")
