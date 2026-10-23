"""Owner daemon: the single process that drains the relay into the mailbox.

``run_owner`` acquires the mailbox owner lock (so a second owner raises
:class:`~phoning.mailbox.OwnerBusy`), fetches the saved contacts with
:meth:`~phoning.retalk.Retalk.contacts`, and spawns ``retalk receive`` for those
named peers only (never ``--all``; agent-talk policy). Its NDJSON stdout is
consumed on a background thread into a :class:`queue.Queue` so the main loop
can poll ``stop_event`` at least every 0.2 s and never block indefinitely.
Contacts are refreshed every 30 s; a changed set restarts the child. With zero
contacts the daemon waits without spawning a child. When the child exits it is
restarted with capped exponential backoff until ``stop_event`` is set.

Each inbound record keeps both the verified evidence (``peer_fp``: the
transport-verified sender fingerprint, and ``raw``: the exact wire text as
received) and the decoded, untrusted ``env`` claim, so a forged envelope can be
audited later instead of silently ignored.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from phoning.envelope import decode
from phoning.mailbox import Mailbox, OwnerLock
from phoning.retalk import Retalk

__all__ = ["run_owner"]

_STOP_POLL: float = 0.2
_KILL_GRACE: float = 2.0
_BACKOFF_START: float = 1.0
_BACKOFF_MAX: float = 30.0
_CONTACTS_REFRESH: float = 30.0


class _Stream(Protocol):
    """Minimal stdout protocol (satisfied by ``io.StringIO`` and pipes)."""

    def readline(self) -> str: ...  # pragma: no cover - protocol


class _Child(Protocol):
    """Minimal subprocess protocol used by :func:`run_owner`."""

    stdout: _Stream | None

    def terminate(self) -> None: ...  # pragma: no cover - protocol

    def kill(self) -> None: ...  # pragma: no cover - protocol

    def wait(self, timeout: float | None = None) -> int: ...  # pragma: no cover


PopenFactory = Callable[..., _Child]


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _reader_loop(
    stdout: _Stream,
    out_queue: queue.Queue[str | None],
    lines_seen: threading.Event,
) -> None:
    """Read ``stdout`` line by line into ``out_queue`` until EOF.

    A ``None`` sentinel is pushed at EOF (or on read failure) so the main
    loop can distinguish "child finished" from "nothing yet".
    """
    try:
        while True:
            line = stdout.readline()
            if not line:
                break
            out_queue.put(line)
        lines_seen.set()
    except (OSError, ValueError):
        lines_seen.set()
    finally:
        out_queue.put(None)


def _handle_line(
    mailbox: Mailbox,
    line: str,
) -> bool:
    """Decode one NDJSON line and append it to ``mailbox``.

    Returns ``True`` when a message record was appended, ``False`` for
    non-message (contacts) or malformed lines. The record keeps the verified
    evidence (``peer_fp`` from the retalk ``from`` and ``raw`` = the exact
    wire text) alongside the decoded, untrusted ``env`` claim.
    """
    stripped = line.strip()
    if not stripped:
        return False
    try:
        obj: Any = json.loads(stripped)
    except json.JSONDecodeError:
        return False
    if not isinstance(obj, dict):
        return False
    rec: dict[str, Any] = cast(dict[str, Any], obj)
    text_any: Any = rec.get("text")
    if not isinstance(text_any, str):
        return False
    peer_any: Any = rec.get("name")
    if not isinstance(peer_any, str) or not peer_any:
        peer_any = rec.get("from")
    peer = peer_any if isinstance(peer_any, str) else ""
    fp_any: Any = rec.get("from")
    peer_fp: str | None = fp_any if isinstance(fp_any, str) else None
    env = decode(text_any, fallback_from=peer)
    group_any: Any = rec.get("group")
    room_any: Any = group_any if isinstance(group_any, str) and group_any else env.get("room")
    room: str | None = room_any if isinstance(room_any, str) else None
    tid_any: Any = rec.get("id")
    tid: str | None = tid_any if isinstance(tid_any, str) else None
    mailbox.append(
        {
            "direction": "in",
            "tid": tid,
            "peer": peer,
            "peer_fp": peer_fp,
            "room": room,
            "recv_ts": _now_iso(),
            "raw": text_any,
            "env": env,
        }
    )
    return True


def _stop_child(child: _Child) -> None:
    """Terminate then kill ``child`` after a short grace period."""
    with contextlib.suppress(OSError):
        child.terminate()
    deadline = time.monotonic() + _KILL_GRACE
    while time.monotonic() < deadline:
        try:
            rc = child.wait(timeout=0.1)
        except Exception:  # noqa: BLE001 - fake Popen may raise anything
            break
        del rc
        return
    with contextlib.suppress(OSError):
        child.kill()
    with contextlib.suppress(Exception):
        child.wait(timeout=_KILL_GRACE)


def _load_contacts(client: Retalk) -> list[str]:
    """Best-effort contact list; never raises, never blocks a restart."""
    try:
        raw = client.contacts()
    except Exception:  # noqa: BLE001 - transport errors must not kill the daemon
        return []
    out: list[str] = []
    seen: set[str] = set()
    for name in raw:
        if isinstance(name, str) and name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def run_owner(
    user_dir: str,
    session: str,
    *,
    interval: float | int = 2,
    stop_event: threading.Event | None = None,
    popen: PopenFactory | None = None,
    max_restarts: int | None = None,
    retalk: Retalk | None = None,
    mailbox: Mailbox | None = None,
) -> None:
    """Drain the relay into the mailbox while holding the owner lock.

    Reads only the saved contacts (named peers). Returns when ``stop_event``
    is set (or after ``max_restarts`` child exits, when bounded). Raises
    :class:`~phoning.mailbox.OwnerBusy` if another process holds the lock.
    """
    stop = stop_event if stop_event is not None else threading.Event()
    factory: PopenFactory = popen if popen is not None else cast(PopenFactory, _default_popen)
    client = retalk if retalk is not None else Retalk(user_dir)
    box = mailbox if mailbox is not None else Mailbox(user_dir)
    lock: OwnerLock = box.acquire_owner(session)
    try:
        _run_loop(box, client, interval, stop, factory, max_restarts)
    finally:
        lock.close()


def _default_popen(argv: list[str], **kwargs: Any) -> _Child:
    """Default child factory: :class:`subprocess.Popen` with text pipes."""
    import subprocess  # local import keeps import-time cost low

    return cast(
        _Child,
        subprocess.Popen(  # noqa: S603
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            **kwargs,
        ),
    )


def _drain(
    box: Mailbox,
    client: Retalk,
    interval: float | int,
    stop: threading.Event,
    factory: PopenFactory,
    peers: list[str],
) -> bool:
    """Spawn/consume one child until stop, child exit, or contact change.

    Returns ``True`` when ``stop`` is set (caller should exit), ``False``
    otherwise (caller should respawn, reloading contacts first).
    """
    argv = client.follow_cmd(interval, peers)
    try:
        child = factory(argv)
    except OSError:
        return stop.wait(_BACKOFF_START)
    out_queue: queue.Queue[str | None] = queue.Queue()
    reader_done = threading.Event()
    stdout = child.stdout
    if stdout is None:
        _stop_child(child)
        return stop.wait(_BACKOFF_START)
    thread = threading.Thread(
        target=_reader_loop,
        args=(stdout, out_queue, reader_done),
        daemon=True,
    )
    thread.start()
    next_refresh = time.monotonic() + _CONTACTS_REFRESH
    while not stop.is_set():
        try:
            line = out_queue.get(timeout=_STOP_POLL)
        except queue.Empty:
            line = ""
        else:
            if line is None:
                break
            _handle_line(box, line)
        if time.monotonic() >= next_refresh:
            latest = _load_contacts(client)
            if latest != peers:
                break
            next_refresh = time.monotonic() + _CONTACTS_REFRESH
    reader_done.wait(timeout=_KILL_GRACE)
    _stop_child(child)
    return stop.is_set()


def _run_loop(
    box: Mailbox,
    client: Retalk,
    interval: float | int,
    stop: threading.Event,
    factory: PopenFactory,
    max_restarts: int | None,
) -> None:
    """Spawn/consume/restart the follow child until ``stop`` is set.

    Contacts are reloaded immediately before every spawn (initial and every
    restart) so a spawn never uses a stale peer list.
    """
    backoff = _BACKOFF_START
    restarts = 0
    while not stop.is_set():
        peers = _load_contacts(client)
        if not peers:
            if stop.wait(_CONTACTS_REFRESH):
                return
            continue
        try:
            should_exit = _drain(box, client, interval, stop, factory, peers)
        except OSError:
            restarts += 1
            if _bounded(restarts, max_restarts):
                return
            if stop.wait(backoff):
                return
            backoff = min(backoff * 2, _BACKOFF_MAX)
            continue
        if should_exit or stop.is_set():
            return
        latest = _load_contacts(client)
        if latest != peers:
            backoff = _BACKOFF_START
            continue
        restarts += 1
        if _bounded(restarts, max_restarts):
            return
        if stop.wait(backoff):
            return
        backoff = min(backoff * 2, _BACKOFF_MAX)


def _bounded(restarts: int, max_restarts: int | None) -> bool:
    """True when ``max_restarts`` is set and ``restarts`` reached it."""
    return max_restarts is not None and restarts >= max_restarts
