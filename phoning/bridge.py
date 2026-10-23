"""Legacy bridge: feed a legacy agent-talk identity into a phoning mailbox.

Legacy retalk identities such as ``~/.agent-talk/users/claude-lead/identity``
receive plain text from senders like ``codex-review``. A phoning principal that
is only reachable through its phoning identity never sees those messages. This
module runs ``retalk receive`` on the legacy identity and appends what it finds
to the principal's phoning mailbox, so ``phoning read`` / ``wait`` / hooks see
legacy mail with no extra tooling.

Because ``retalk receive`` consumes (messages already handed over are never
served again) the bridge must be the sole reader for its peers. It therefore
refuses to start unless it can show it is alone: a ``flock``-based lock next to
the legacy identity, and no live ``follow.*.pid`` follower. See
``docs/spec/bridge.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from phoning.envelope import decode
from phoning.mailbox import Mailbox
from phoning.retalk import Retalk

__all__ = ["main", "run_bridge"]

_STOP_POLL: float = 0.2
_JOIN_TIMEOUT: float = 2.0
_KILL_GRACE: float = 2.0
_BACKOFF_START: float = 1.0
_BACKOFF_MAX: float = 30.0

_PEER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_HEX32_RE = re.compile(r"[0-9a-fA-F]{32}")

_LOG_PREFIX = "phoning bridge: "

EXIT_OK = 0
EXIT_FATAL = 1
EXIT_USAGE = 2
EXIT_REFUSED = 3


class _Stream(Protocol):
    """Minimal stdout protocol (satisfied by ``io.StringIO`` and pipes)."""

    def readline(self) -> str: ...  # pragma: no cover - protocol


class _Child(Protocol):
    """Minimal subprocess protocol used by :func:`run_bridge`."""

    stdout: _Stream | None

    def terminate(self) -> None: ...  # pragma: no cover - protocol

    def kill(self) -> None: ...  # pragma: no cover - protocol

    def wait(self, timeout: float | None = None) -> int: ...  # pragma: no cover


PopenFactory = Callable[..., _Child]


class BridgeError(Exception):
    """A refused start (exit 3) or a usage/missing-input failure (exit 2)."""

    def __init__(self, message: str, *, code: int) -> None:
        self.code: int = code
        super().__init__(message)


class _Stopped(Exception):
    """Internal: a signal arrived during startup; unwind to exit 0."""


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _log(message: str) -> None:
    """One event line on stderr, prefixed ``phoning bridge:``."""
    print(f"{_LOG_PREFIX}{message}", file=sys.stderr, flush=True)


def _legacy_name(identity_dir: str) -> str:
    """Legacy user name for ``bridge:<legacy>``.

    ``basename(dirname(DIR))`` when ``basename(DIR) == "identity"`` (the
    usual ``.../users/<name>/identity`` layout), else ``basename(DIR)``.
    """
    abs_dir = os.path.abspath(identity_dir)
    if os.path.basename(abs_dir) == "identity":
        return os.path.basename(os.path.dirname(abs_dir))
    return os.path.basename(abs_dir)


def _resolve_home(home: str | None) -> str:
    """``--home``, else ``$PHONING_HOME``, else ``~/.phoning``."""
    if home is not None:
        return home
    env = os.environ.get("PHONING_HOME")
    if env:
        return env
    return os.path.join(os.path.expanduser("~"), ".phoning")


def _read_relay(legacy_user_dir: str) -> str | None:
    """First line of ``<legacy user dir>/relay`` if present, else ``None``."""
    path = os.path.join(legacy_user_dir, "relay")
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            val = fh.readline().strip()
    except OSError:
        return None
    return val or None


def _make_client(identity_dir: str, relay: str | None) -> Retalk:
    """Build a :class:`Retalk` bound to the legacy identity directory.

    The legacy identity dir is ``DIR`` itself (not ``<parent>/identity``), so
    the client is built with the parent as the user dir and its ``identity_dir``
    is then overridden to ``DIR``.
    """
    client = Retalk(os.path.dirname(os.path.abspath(identity_dir)), relay=relay)
    client.identity_dir = identity_dir
    return client


# -- checks ---------------------------------------------------------------


def _check_mailbox(home: str, name: str) -> str:
    """Ensure ``<home>/<name>/mailbox/log.jsonl`` exists; return the user dir."""
    user_dir = os.path.join(home, name)
    log = os.path.join(user_dir, "mailbox", "log.jsonl")
    if not os.path.isfile(log):
        raise BridgeError(f"no mailbox for {name}", code=EXIT_USAGE)
    return user_dir


def _check_identity_dir(identity_dir: str) -> None:
    """Ensure the legacy identity dir exists and is a directory."""
    if not os.path.isdir(identity_dir):
        raise BridgeError(f"not a legacy identity directory: {identity_dir}", code=EXIT_USAGE)


def _check_peers(client: Retalk, peers: Sequence[str]) -> list[str]:
    """Fail closed unless every non-fingerprint peer exists in contacts.

    Returns the deduplicated peer list in input order. A peer that is a
    32-hex user id needs no contact entry. An unlistable or empty contact
    list is a usage error (never fall back to ``--all``).
    """
    unique: list[str] = []
    seen: set[str] = set()
    for peer in peers:
        if peer not in seen:
            seen.add(peer)
            unique.append(peer)
    try:
        contacts = client.contacts()
    except Exception:  # noqa: BLE001 - any transport failure fails closed
        contacts = []
    known = {name for name in contacts if isinstance(name, str) and name}
    if not known:
        raise BridgeError("could not list legacy contacts", code=EXIT_USAGE)
    for peer in unique:
        if _HEX32_RE.fullmatch(peer):
            continue
        if peer not in known:
            raise BridgeError(f"unknown legacy peer {peer}", code=EXIT_USAGE)
    return unique


def _check_followers(legacy_user_dir: str) -> None:
    """Refuse (exit 3) when a ``follow.*.pid`` file names a live process."""
    try:
        entries = sorted(os.listdir(legacy_user_dir))
    except OSError:
        return
    for entry in entries:
        if not entry.startswith("follow.") or not entry.endswith(".pid"):
            continue
        path = os.path.join(legacy_user_dir, entry)
        pid = _read_pid(path)
        if pid is None:
            continue
        if _pid_alive(pid):
            raise BridgeError(
                f"legacy follower {path} is live (pid {pid}); stop it first "
                "so the bridge is the only reader",
                code=EXIT_REFUSED,
            )


def _read_pid(path: str) -> int | None:
    """Parse a pid file body; ``None`` when unreadable, non-int or <= 0."""
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            body = fh.read().strip()
    except OSError:
        return None
    try:
        pid = int(body)
    except ValueError:
        return None
    return pid if pid > 0 else None


def _pid_alive(pid: int) -> bool:
    """True when ``os.kill(pid, 0)`` says the process exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# -- lock -----------------------------------------------------------------


def _lock_path(legacy_user_dir: str) -> str:
    """``<legacy user dir>/bridge.lock``."""
    return os.path.join(legacy_user_dir, "bridge.lock")


def _existing_lock_pid(path: str) -> str:
    """Read the pid out of an existing lock body when possible."""
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            body: Any = json.load(fh)
    except (OSError, ValueError):
        return "?"
    if isinstance(body, dict):
        typed: dict[str, Any] = body
        pid: Any = typed.get("pid")
        if isinstance(pid, int):
            return str(pid)
    return "?"


class _Lock:
    """Held ``flock`` on the bridge lock file, with a JSON body."""

    def __init__(self, fd: int, path: str) -> None:
        self.fd: int = fd
        self.path: str = path

    def write_body(self, body: dict[str, Any]) -> None:
        """Overwrite the lock file with ``body`` (after the lock is taken)."""
        data = json.dumps(body).encode("utf-8")
        os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.write(self.fd, data)

    def close(self) -> None:
        """Release the flock and close the descriptor."""
        with contextlib.suppress(OSError):
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self.fd)


def _acquire_lock(identity_dir: str) -> _Lock:
    """Take an exclusive, non-blocking ``flock`` next to the identity dir.

    Raises :class:`BridgeError` (exit 3) when another process holds it.
    """
    legacy_user_dir = os.path.dirname(os.path.abspath(identity_dir))
    path = _lock_path(legacy_user_dir)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        holder = _existing_lock_pid(path)
        os.close(fd)
        raise BridgeError(
            f"bridge already running for {identity_dir} (pid {holder})",
            code=EXIT_REFUSED,
        ) from None
    with contextlib.suppress(OSError):
        os.fchmod(fd, 0o600)
    return _Lock(fd, path)


# -- follow loop ----------------------------------------------------------


def _reader_loop(
    stdout: _Stream,
    out_queue: queue.Queue[str | None],
) -> None:
    """Read ``stdout`` line by line into ``out_queue``; ``None`` marks EOF."""
    try:
        while True:
            line = stdout.readline()
            if not line:
                break
            out_queue.put(line)
    except (OSError, ValueError):
        pass
    finally:
        out_queue.put(None)


def _append_record(mailbox: Mailbox, legacy: str, line: str) -> int | None:
    """Decode one NDJSON line and append it to ``mailbox``.

    Returns the appended mailbox sequence number, or ``None`` for non-message
    (``kind: "contact"``), malformed or empty lines. ``raw`` keeps the exact
    text; ``peer_fp`` keeps the transport-verified sender.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        obj: Any = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    rec: dict[str, Any] = cast(dict[str, Any], obj)
    if rec.get("kind") == "contact":
        return None
    text_any: Any = rec.get("text")
    if not isinstance(text_any, str):
        return None
    peer_any: Any = rec.get("name")
    if not isinstance(peer_any, str) or not peer_any:
        peer_any = rec.get("from")
    peer = peer_any if isinstance(peer_any, str) else ""
    fp_any: Any = rec.get("from")
    peer_fp: str | None = fp_any if isinstance(fp_any, str) else None
    tid_any: Any = rec.get("id")
    tid: str | None = tid_any if isinstance(tid_any, str) else None
    env = decode(text_any, fallback_from=peer)
    seq = mailbox.append(
        {
            "direction": "in",
            "tid": tid,
            "peer": peer,
            "peer_fp": peer_fp,
            "room": None,
            "recv_ts": _now_iso(),
            "raw": text_any,
            "env": env,
            "via": f"bridge:{legacy}",
        }
    )
    if seq is not None:
        _log(f"appended seq={seq} peer={peer}")
    return seq


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


def _exit_code(child: _Child) -> str:
    """Best-effort numeric exit code of an already-stopped child, else ``?``."""
    try:
        rc = child.wait(timeout=0.0)
    except Exception:  # noqa: BLE001 - fake Popen may raise anything
        return "?"
    return str(rc)


def _default_popen(argv: list[str], **kwargs: Any) -> _Child:
    """Default child factory: :class:`subprocess.Popen` in a new session."""
    import subprocess  # local import keeps import-time cost low

    return cast(
        _Child,
        subprocess.Popen(  # noqa: S603
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            start_new_session=True,
            **kwargs,
        ),
    )


def _drain(
    mailbox: Mailbox,
    legacy: str,
    client: Retalk,
    interval: float | int,
    stop: threading.Event,
    factory: PopenFactory,
    peers: list[str],
) -> tuple[bool, bool, str]:
    """Spawn/consume one child until stop or child exit.

    Returns ``(stop_set, delivered, rc)``: ``stop_set`` tells the caller to
    return, ``delivered`` says at least one message line was appended (reset
    the backoff), ``rc`` is the child's exit code as a string (``?`` when
    unknown).

    Nothing consumed may be dropped: the child has already taken its lines off
    the relay, so when the loop ends for any reason (stop requested or EOF)
    the child is stopped, the reader thread is joined and every line still
    queued is appended even though the stop flag is set.
    """
    argv = client.follow_cmd(interval, peers)
    try:
        child = factory(argv)
    except OSError:
        return stop.is_set(), False, "?"
    out_queue: queue.Queue[str | None] = queue.Queue()
    stdout = child.stdout
    if stdout is None:
        _stop_child(child)
        return stop.is_set(), False, _exit_code(child)
    thread = threading.Thread(
        target=_reader_loop,
        args=(stdout, out_queue),
        daemon=True,
    )
    thread.start()
    delivered = False
    try:
        while not stop.is_set():
            try:
                line = out_queue.get(timeout=_STOP_POLL)
            except queue.Empty:
                continue
            if line is None:
                break
            if _append_record(mailbox, legacy, line) is not None:
                delivered = True
    finally:
        # Stop the child first: it has already taken its lines off the relay,
        # so nothing may be dropped. Then join the reader and append every
        # line still queued, even though the stop flag is set.
        _stop_child(child)
        with contextlib.suppress(RuntimeError):
            thread.join(timeout=_JOIN_TIMEOUT)
        while True:
            try:
                line = out_queue.get_nowait()
            except queue.Empty:
                break
            if line is None:
                continue
            if _append_record(mailbox, legacy, line) is not None:
                delivered = True
    return stop.is_set(), delivered, _exit_code(child)


def run_bridge(
    user_dir: str,
    identity_dir: str,
    peers: Sequence[str],
    session: str,
    *,
    interval: float | int = 2,
    stop_event: threading.Event | None = None,
    popen: PopenFactory | None = None,
    max_restarts: int | None = None,
    mailbox: Mailbox | None = None,
    client: Retalk | None = None,
) -> None:
    """Follow the legacy identity and append what it finds to the mailbox.

    ``user_dir`` is the phoning principal's user dir (``<home>/NAME``); the
    caller has already verified its mailbox exists and that its peer list is
    known. ``identity_dir`` is the legacy retalk identity directory. Returns
    when ``stop_event`` is set or, when ``max_restarts`` is given, after that
    many child exits. The caller holds the bridge lock.

    When ``client`` is not given it is built from ``identity_dir`` exactly as
    :func:`main` does, so the loop always follows the legacy identity.
    """
    del session
    stop = stop_event if stop_event is not None else threading.Event()
    factory: PopenFactory = popen if popen is not None else cast(PopenFactory, _default_popen)
    box = mailbox if mailbox is not None else Mailbox(user_dir)
    legacy = _legacy_name(identity_dir)
    follow_peers = list(peers)
    if client is not None:
        tuned = client
    else:
        relay = _read_relay(os.path.dirname(os.path.abspath(identity_dir)))
        tuned = _make_client(identity_dir, relay)
    backoff = _BACKOFF_START
    restarts = 0
    while not stop.is_set():
        should_exit, delivered, rc = _drain(
            box, legacy, tuned, interval, stop, factory, follow_peers
        )
        if delivered:
            backoff = _BACKOFF_START
        if should_exit or stop.is_set():
            return
        restarts += 1
        if max_restarts is not None and restarts >= max_restarts:
            return
        _log(f"child exited rc={rc}; restart in {backoff:g}s")
        if stop.wait(backoff):
            return
        backoff = min(backoff * 2, _BACKOFF_MAX)


# -- entry point ----------------------------------------------------------


class _PeerAction(argparse.Action):
    """Validate ``--peer`` values as they are parsed.

    ``--peer --all`` and ``--peer -x`` must be rejected as invalid peers
    rather than swallowed by argparse as option-like tokens.
    """

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,
        option_string: str | None = None,
    ) -> None:
        if values is None:
            parser.error(f"invalid --peer value: {values}")
            return
        value = values if isinstance(values, str) else str(values)
        if not _valid_peer(value):
            parser.error(f"invalid --peer value: {value}")
        items = getattr(namespace, self.dest, None)
        current: list[str] = list(items) if isinstance(items, list) else []
        current.append(value)
        setattr(namespace, self.dest, current)


def _build_parser() -> argparse.ArgumentParser:
    """Argparse parser for ``phoning bridge``."""
    parser = argparse.ArgumentParser(
        prog="phoning bridge",
        description="Read a legacy agent-talk identity into a phoning mailbox.",
    )
    parser.add_argument("name", metavar="NAME", help="phoning principal name")
    parser.add_argument(
        "--from-identity",
        required=True,
        metavar="DIR",
        help="legacy retalk identity directory (the one passed to retalk --dir)",
    )
    parser.add_argument(
        "--peer",
        action=_PeerAction,
        default=[],
        metavar="P",
        help="saved legacy peer name or 32-hex user id (repeatable)",
    )
    parser.add_argument("--session", required=True, metavar="S", help="label for lock/log lines")
    parser.add_argument("--home", default=None, metavar="DIR", help="phoning home (default ~/.phoning)")
    parser.add_argument("--relay", default=None, metavar="URL", help="legacy relay URL")
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        metavar="N",
        help="seconds between legacy polls (default 2)",
    )
    return parser


def _valid_peer(value: str) -> bool:
    """True when a ``--peer`` value can never look like a retalk option."""
    return _PEER_RE.fullmatch(value) is not None


def main(argv: Sequence[str] | None = None) -> int:
    """Run the bridge; return the process exit code (0, 1, 2 or 3)."""
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_USAGE
        return code

    name = cast(str, args.name)
    identity_dir = cast(str, args.from_identity)
    session = cast(str, args.session)
    home_arg = cast(str | None, args.home)
    relay_arg = cast(str | None, args.relay)
    interval = cast(float, args.interval)
    raw_peers = cast(list[str], args.peer)

    if not raw_peers:
        _log("at least one --peer is required")
        return EXIT_USAGE
    for peer in raw_peers:
        if not _valid_peer(peer):
            _log(f"invalid --peer value: {peer}")
            return EXIT_USAGE
    if not (interval > 0):
        _log("--interval must be positive")
        return EXIT_USAGE

    # Install the signal handlers *before* any startup work that can block
    # (contact listing, flock). Otherwise a SIGTERM that arrives during
    # startup kills the process with the default disposition and the exit
    # code is the negative signal number instead of 0.
    stop = threading.Event()

    def _handle_signal(signum: int, frame: Any) -> None:
        del signum, frame
        stop.set()

    old_term = signal.signal(signal.SIGTERM, _handle_signal)
    old_int = signal.signal(signal.SIGINT, _handle_signal)
    lock: _Lock | None = None
    started = False
    exit_code = EXIT_OK
    try:
        home = _resolve_home(home_arg)
        user_dir = _check_mailbox(home, name)
        _check_identity_dir(identity_dir)

        legacy_user_dir = os.path.dirname(os.path.abspath(identity_dir))
        relay = relay_arg if relay_arg is not None else _read_relay(legacy_user_dir)
        client = _make_client(identity_dir, relay)

        peers = _check_peers(client, raw_peers)
        _check_followers(legacy_user_dir)
        if stop.is_set():
            raise _Stopped

        lock = _acquire_lock(identity_dir)
        if stop.is_set():
            raise _Stopped
        lock.write_body(
            {
                "pid": os.getpid(),
                "since": _now_iso(),
                "name": name,
                "session": session,
                "peers": peers,
            }
        )
        legacy = _legacy_name(identity_dir)
        _log(f"started {legacy} -> {name} peers={','.join(peers)}")
        started = True
        try:
            run_bridge(
                user_dir,
                identity_dir,
                peers,
                session,
                interval=interval,
                stop_event=stop,
                client=client,
            )
        except Exception as exc:  # noqa: BLE001 - never leak a lock on a crash
            _log(f"fatal: {type(exc).__name__}")
            exit_code = EXIT_FATAL
        return exit_code
    except BridgeError as exc:
        _log(str(exc))
        return exc.code
    except _Stopped:
        return EXIT_OK
    finally:
        stop.set()
        if started:
            _log("stopped")
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)
        if lock is not None:
            lock.close()


if __name__ == "__main__":  # pragma: no cover - module entry point
    sys.exit(main())
