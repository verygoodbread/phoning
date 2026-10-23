"""Thin wrapper around the ``retalk`` CLI.

Every command is executed as::

    retalk <cmd> ... --dir <user_dir>/identity -r <relay> -np

The runner is injectable so tests can exercise argv shapes without touching
the network or the filesystem: ``runner(argv, *, stdin=None, timeout=60) ->
(rc, out, err)``. Errors never embed stdout/stderr bodies; they carry the
return code and a short command head only.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from collections.abc import Callable, Sequence
from typing import Any, Final

Runner = Callable[..., "tuple[int, str, str]"]

DEFAULT_TIMEOUT: Final[float] = 60.0
_CMD_HEAD: Final[int] = 3


class RetalkError(RuntimeError):
    """Raised when ``retalk`` exits non-zero.

    Deliberately does **not** include stdout/stderr bodies, which can carry
    secrets; only ``rc`` and the first few argv elements are reported.
    """

    def __init__(self, rc: int, cmd_head: Sequence[str]) -> None:
        self.rc: int = rc
        self.cmd_head: tuple[str, ...] = tuple(cmd_head)
        rendered = " ".join(shlex.quote(str(a)) for a in self.cmd_head)
        super().__init__(f"retalk failed with rc={rc}: {rendered}")


def _default_runner(
    argv: list[str],
    *,
    stdin: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, str, str]:
    """Run ``argv`` via :mod:`subprocess`, capturing text output."""
    proc = subprocess.run(  # noqa: S603
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return proc.returncode, proc.stdout, proc.stderr


def _parse_ids(out: str) -> list[str]:
    """Collect every ``"id"`` field from NDJSON/JSON output, in order."""
    ids: list[str] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj: Any = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            typed: dict[str, Any] = obj
            val: Any = typed.get("id")
            if isinstance(val, str) and val:
                ids.append(val)
    return ids


def _last_nonempty_line(out: str) -> str:
    """Return the last non-empty line of ``out`` (stripped)."""
    for line in reversed(out.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _name_of(obj: Any) -> str | None:
    """Best-effort name/peer extraction from one contact object."""
    if isinstance(obj, str):
        return obj or None
    if isinstance(obj, dict):
        typed: dict[str, Any] = obj
        for key in ("name", "peer", "alias"):
            val: Any = typed.get(key)
            if isinstance(val, str) and val:
                return val
        return None
    return None


def _parse_contacts(out: str) -> list[str]:
    """Parse saved peer names from ``retalk contacts --json`` output.

    Accepts a JSON array of objects/strings, a single object/list per line
    (NDJSON), or a dict mapping names to contact records. Non-JSON lines are
    treated as bare names. Order-preserving and duplicate-free.
    """
    names: list[str] = []
    seen: set[str] = set()

    def _add(name: str | None) -> None:
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    def _walk(obj: Any) -> None:
        if isinstance(obj, list):
            for item in obj:
                _add(_name_of(item))
        elif isinstance(obj, dict):
            typed: dict[str, Any] = obj
            direct = _name_of(typed)
            if direct is not None and (
                "name" in typed or "peer" in typed or "alias" in typed
            ):
                _add(direct)
                return
            for key in typed:
                if isinstance(key, str):
                    _add(key)

    text = out.strip()
    if text:
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            _walk(parsed)
            return names
    for line in out.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj: Any = json.loads(stripped)
        except json.JSONDecodeError:
            _add(stripped)
            continue
        _walk(obj)
    return names


class Retalk:
    """Bound ``retalk`` client for one phoning user directory."""

    def __init__(
        self,
        user_dir: str | os.PathLike[str],
        runner: Runner | None = None,
        *,
        relay: str | None = None,
        binary: str = "retalk",
    ) -> None:
        self.user_dir: str = os.fspath(user_dir)
        self.identity_dir: str = os.path.join(self.user_dir, "identity")
        self.relay: str | None = relay if relay is not None else self._read_relay()
        self.binary: str = binary
        self._runner: Runner = runner if runner is not None else _default_runner

    def _read_relay(self) -> str | None:
        """Read ``<user_dir>/relay`` if present, else ``None``."""
        path = os.path.join(self.user_dir, "relay")
        try:
            with open(path, encoding="utf-8") as fh:  # noqa: PTH123
                val = fh.readline().strip()
        except OSError:
            return None
        return val or None

    # -- invocation -----------------------------------------------------
    def _argv(self, *args: str, include_dir: bool = True) -> list[str]:
        """Build ``retalk <args> [--dir ... -r ... -np]``."""
        argv: list[str] = [self.binary, *args]
        if include_dir:
            argv += ["--dir", self.identity_dir]
            if self.relay:
                argv += ["-r", self.relay]
            argv.append("-np")
        return argv

    def _run(
        self,
        argv: list[str],
        *,
        stdin: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        allow_failure: bool = False,
    ) -> tuple[int, str, str]:
        """Invoke the runner; raise :class:`RetalkError` on non-zero rc."""
        rc, out, err = self._runner(argv, stdin=stdin, timeout=timeout)
        if rc != 0 and not allow_failure:
            raise RetalkError(rc, argv[:_CMD_HEAD])
        return rc, out, err

    # -- commands -------------------------------------------------------
    def init(self, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Create the identity (``retalk init``) and return the fingerprint."""
        argv = [self.binary, "init", "--dir", self.identity_dir]
        if self.relay:
            argv += ["-r", self.relay]
        argv.append("-np")
        os.makedirs(self.identity_dir, exist_ok=True)
        _rc, out, _err = self._run(argv, timeout=timeout)
        last = _last_nonempty_line(out)
        if last:
            return last
        return self.id()

    def id(self, *, timeout: float = DEFAULT_TIMEOUT) -> str:
        """Return this identity's fingerprint (``retalk id``)."""
        argv = self._argv("id")
        _rc, out, _err = self._run(argv, timeout=timeout)
        return _last_nonempty_line(out)

    def add(
        self,
        fp: str,
        alias: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Add a peer (``retalk add FP --peer ALIAS --verify``)."""
        argv = self._argv("add", fp, "--peer", alias, "--verify")
        self._run(argv, timeout=timeout)

    def contacts(self, *, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
        """Return the saved peer names (``retalk contacts --json``)."""
        argv = self._argv("contacts", "--json")
        _rc, out, _err = self._run(argv, timeout=timeout, allow_failure=True)
        return _parse_contacts(out)

    def group_create(
        self,
        room: str,
        members: Sequence[str],
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        """Create a group room with ``members`` (comma-joined)."""
        argv = self._argv("group", "create", room, "--members", ",".join(members))
        self._run(argv, timeout=timeout)

    def group_members(self, room: str, *, timeout: float = DEFAULT_TIMEOUT) -> list[str]:
        """Return the members of ``room`` (best-effort parse of output lines)."""
        argv = self._argv("group", "members", room)
        _rc, out, _err = self._run(argv, timeout=timeout)
        members: list[str] = []
        for line in out.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj: Any = json.loads(stripped)
            except json.JSONDecodeError:
                members.append(stripped)
                continue
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, str):
                        members.append(item)
            elif isinstance(obj, dict):
                typed: dict[str, Any] = obj
                val: Any = typed.get("members")
                if isinstance(val, list):
                    for item in val:
                        if isinstance(item, str):
                            members.append(item)
                else:
                    members.append(stripped)
            else:
                members.append(stripped)
        return members

    def send_peer(
        self,
        peer: str,
        text: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> str:
        """Send ``text`` to peer alias ``peer``; return the transport id."""
        argv = self._argv("send", "--peer", peer, text)
        _rc, out, _err = self._run(argv, timeout=timeout)
        ids = _parse_ids(out)
        return ids[0] if ids else ""

    def send_group(
        self,
        room: str,
        text: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> list[str]:
        """Send ``text`` to group ``room``; return every receipt id."""
        argv = self._argv("send", "--group", room, text)
        _rc, out, _err = self._run(argv, timeout=timeout)
        return _parse_ids(out)

    def follow_cmd(
        self, interval: float | int = 2, peers: Sequence[str] = ()
    ) -> list[str]:
        """argv for ``receive --peer A --peer B ... --follow --interval N --quiet --save``.

        Never uses ``--all`` or ``--peers-only``: agents read only their named
        peers.
        """
        secs = int(interval) if float(interval).is_integer() else float(interval)
        args: list[str] = ["receive"]
        for peer in peers:
            args += ["--peer", str(peer)]
        args += ["--follow", "--interval", str(secs), "--quiet", "--save"]
        return self._argv(*args)


__all__ = ["DEFAULT_TIMEOUT", "Retalk", "RetalkError", "Runner"]
