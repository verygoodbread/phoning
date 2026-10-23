"""Single-owner mailbox with non-destructive multi-reader cursors.

Layout under ``<user_dir>/mailbox/``::

    log.jsonl               append-only records, one JSON object per line
    owner.lock              flock held by the relay-draining owner (+ info JSON)
    cursors/<reader>.json   {"seq": N} per reader (last committed seq)
    audit.log               refused owner attempts, one JSON object per line

Record shape::

    {"seq": int, "direction": "in"|"out", "tid": str|None, "peer": str,
     "recv_ts": str, "room": str|None, "env": {...}}

Only the standard library is used. Both the append path (``log.jsonl``) and
the single-owner path (``owner.lock``) use :mod:`fcntl` locks and every
append is followed by ``fsync``.
"""

import contextlib
import fcntl
import json
import os
import re
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast

__all__ = ["Mailbox", "OwnerBusy", "OwnerLock"]

_READER_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_.-]{1,64}")


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _validate_reader(reader: object) -> str:
    """Return ``reader`` if it is a legal reader name, else raise ValueError."""
    if not isinstance(reader, str):
        msg = f"invalid reader name {reader!r}: must match [A-Za-z0-9_.-]{{1,64}}"
        raise ValueError(msg)
    if _READER_RE.fullmatch(reader) is None:
        msg = f"invalid reader name {reader!r}: must match [A-Za-z0-9_.-]{{1,64}}"
        raise ValueError(msg)
    return reader


def _validate_session(session: object) -> str:
    """Return ``session`` if it is a non-empty string, else raise ValueError."""
    if not isinstance(session, str) or not session:
        msg = "session must be a non-empty string"
        raise ValueError(msg)
    return session


def _write_all(fd: int, data: bytes) -> None:
    """Write all of ``data`` to ``fd`` (loop over short writes)."""
    offset = 0
    while offset < len(data):
        n = os.write(fd, data[offset:])
        if n <= 0:
            msg = "short write"
            raise OSError(msg)
        offset += n


def _fsync_dir(path: str) -> None:
    """Best-effort fsync of the directory ``path``."""
    try:
        dfd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(dfd)
    finally:
        os.close(dfd)


def _ensure_private_dir(path: str) -> None:
    """Create ``path`` as a ``0o700`` directory.

    Uses ``mode=0o700`` for creation and then ``chmod 0o700`` so
    pre-existing directories are tightened to owner-only.
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def _chmod_private_file(path: str) -> None:
    """Best-effort chmod of ``path`` to ``0o600`` (missing file is ignored)."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def _effective_room(rec: dict[str, Any]) -> Any:
    """Room used for filtering: top-level ``room`` else ``env.room``."""
    top = rec.get("room")
    if top is not None:
        return top
    env = rec.get("env")
    if isinstance(env, dict):
        return env.get("room")
    return None


class OwnerBusy(RuntimeError):
    """Raised when the owner lock is already held.

    Carries the current holder info (``session``/``pid``/``since``) in
    :attr:`holder` and in the message so CLI layers can name the holder.
    """

    def __init__(
        self,
        holder: dict[str, Any] | str | None = None,
        message: str | None = None,
    ) -> None:
        if isinstance(holder, str) and message is None:
            message = holder
            holder_dict: dict[str, Any] = {}
        elif isinstance(holder, dict):
            holder_dict = dict(holder)
        elif holder is None:
            holder_dict = {}
        else:
            holder_dict = {}
        self.holder: dict[str, Any] = holder_dict
        if message is None:
            sess = holder_dict.get("session", "?")
            pid = holder_dict.get("pid", "?")
            since = holder_dict.get("since", "?")
            message = (
                f"mailbox already owned by session {sess!r} "
                f"(pid {pid}, since {since}); "
                "read it with `wait/read --reader <you>` instead of acquiring owner"
            )
        super().__init__(message)


class OwnerLock:
    """Handle for a held owner lock.

    Keeps the ``owner.lock`` file descriptor open (which is what holds the
    flock). Closing releases the lock so it can be re-acquired.
    """

    __slots__ = ("_closed", "_fd", "_path", "info")

    def __init__(self, fd: int, path: str, info: dict[str, Any]) -> None:
        self._fd: int = fd
        self._path: str = path
        self.info: dict[str, Any] = dict(info)
        self._closed: bool = False

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has been called."""
        return self._closed

    @property
    def session(self) -> Any:
        """Session that acquired this lock."""
        return self.info.get("session")

    def close(self) -> None:
        """Release the flock and close the underlying fd (idempotent)."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(OSError):
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(self._fd)

    def __enter__(self) -> "OwnerLock":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __del__(self) -> None:
        with contextlib.suppress(OSError):
            self.close()


class Mailbox:
    """Append-only log plus per-reader cursors plus a single owner lock."""

    def __init__(self, user_dir: str | os.PathLike[str]) -> None:
        self.user_dir: str = os.fspath(user_dir)
        self.mailbox_dir: str = os.path.join(self.user_dir, "mailbox")
        self.log_path: str = os.path.join(self.mailbox_dir, "log.jsonl")
        self.owner_lock_path: str = os.path.join(self.mailbox_dir, "owner.lock")
        self.cursors_dir: str = os.path.join(self.mailbox_dir, "cursors")
        self.audit_path: str = os.path.join(self.mailbox_dir, "audit.log")
        _ensure_private_dir(self.user_dir)
        _ensure_private_dir(self.mailbox_dir)
        _ensure_private_dir(self.cursors_dir)
        fd = os.open(self.log_path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        _chmod_private_file(self.log_path)
        if os.path.exists(self.owner_lock_path):
            _chmod_private_file(self.owner_lock_path)
        if os.path.exists(self.audit_path):
            _chmod_private_file(self.audit_path)
        try:
            names = os.listdir(self.cursors_dir)
        except OSError:
            names = []
        for name in names:
            if name.endswith(".json"):
                _chmod_private_file(os.path.join(self.cursors_dir, name))

    def _cursor_path(self, reader: str) -> str:
        """Filesystem path for ``reader``'s cursor file."""
        return os.path.join(self.cursors_dir, reader + ".json")

    def _read_all(self) -> list[dict[str, Any]]:
        """Return every well-formed record in log order under a shared lock."""
        records: list[dict[str, Any]] = []
        try:
            with open(self.log_path, encoding="utf-8") as fh:  # noqa: PTH123
                with contextlib.suppress(OSError):
                    fcntl.flock(fh.fileno(), fcntl.LOCK_SH)
                try:
                    for line in fh:
                        if not line.strip():
                            continue
                        try:
                            obj = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(obj, dict):
                            records.append(cast(dict[str, Any], obj))
                finally:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except FileNotFoundError:
            return []
        return records

    def append(self, rec: dict[str, Any]) -> int:
        """Append ``rec``, assigning the next monotonic ``seq``.

        Holds an exclusive flock on ``log.jsonl`` while scanning for the
        last ``seq`` and writing, then fsyncs. When ``rec`` carries a
        non-None ``tid``, a record with the same ``(direction, tid)`` is
        not duplicated: the existing ``seq`` is returned instead.
        """
        if not isinstance(rec, dict):
            msg = "rec must be a dict"
            raise ValueError(msg)
        fd = os.open(self.log_path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            size = os.fstat(fd).st_size
            data = b""
            if size > 0:
                chunks: list[bytes] = []
                remaining = size
                offset = 0
                while remaining > 0:
                    chunk = os.pread(fd, remaining, offset)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    offset += len(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
            text = data.decode("utf-8", errors="replace")
            last = 0
            seen: dict[tuple[Any, Any], int] = {}
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                typed = cast(dict[str, Any], obj)
                seq_any = typed.get("seq")
                if isinstance(seq_any, int) and not isinstance(seq_any, bool):
                    if seq_any > last:
                        last = seq_any
                    tid_any = typed.get("tid")
                    if tid_any is not None:
                        key = (typed.get("direction"), tid_any)
                        if key not in seen:
                            seen[key] = seq_any
            new_tid = rec.get("tid")
            if new_tid is not None:
                new_key = (rec.get("direction"), new_tid)
                existing = seen.get(new_key)
                if existing is not None:
                    return existing
            new_seq = last + 1
            stored: dict[str, Any] = dict(rec)
            stored["seq"] = new_seq
            if not stored.get("recv_ts"):
                stored["recv_ts"] = _now_iso()
            payload = (json.dumps(stored, ensure_ascii=False) + "\n").encode("utf-8")
            os.lseek(fd, 0, os.SEEK_END)
            _write_all(fd, payload)
            os.fsync(fd)
            return new_seq
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def cursor(self, reader: str) -> int:
        """Last committed ``seq`` for ``reader`` (0 when none/invalid)."""
        _validate_reader(reader)
        path = self._cursor_path(reader)
        try:
            with open(path, encoding="utf-8") as fh:  # noqa: PTH123
                obj = json.load(fh)
        except (OSError, ValueError):
            return 0
        if not isinstance(obj, dict):
            return 0
        typed = cast(dict[str, Any], obj)
        seq = typed.get("seq")
        if isinstance(seq, bool):
            return 0
        if isinstance(seq, int) and seq >= 0:
            return seq
        return 0

    def peek(
        self,
        reader: str,
        *,
        limit: int = 1000,
        types: Sequence[str] | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Records with ``seq`` past ``reader``'s cursor, without committing.

        ``types`` keeps only records whose ``env.type`` is listed;
        ``room`` keeps only records whose effective room matches.
        """
        _validate_reader(reader)
        if limit <= 0:
            return []
        cur = self.cursor(reader)
        recs = self._read_all()
        out: list[dict[str, Any]] = []
        for rec in recs:
            seq = rec.get("seq")
            if isinstance(seq, bool) or not isinstance(seq, int):
                continue
            if seq <= cur:
                continue
            out.append(rec)
        if types is not None:
            allowed = set(types)
            filtered: list[dict[str, Any]] = []
            for rec in out:
                env = rec.get("env")
                if isinstance(env, dict) and env.get("type") in allowed:
                    filtered.append(rec)
            out = filtered
        if room is not None:
            out = [r for r in out if _effective_room(r) == room]
        return out[:limit]

    def commit(self, reader: str, seq: int) -> None:
        """Persist ``seq`` as ``reader``'s cursor (atomic replace + fsync)."""
        _validate_reader(reader)
        if isinstance(seq, bool) or not isinstance(seq, int):
            msg = "seq must be an int >= 0"
            raise ValueError(msg)
        if seq < 0:
            msg = "seq must be >= 0"
            raise ValueError(msg)
        _ensure_private_dir(self.cursors_dir)
        path = self._cursor_path(reader)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"seq": seq}, fh)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
        os.replace(tmp, path)
        _chmod_private_file(path)
        _fsync_dir(self.cursors_dir)

    def read(
        self,
        reader: str,
        *,
        limit: int = 1000,
        types: Sequence[str] | None = None,
        room: str | None = None,
    ) -> list[dict[str, Any]]:
        """Peek then commit to the last returned record (if any)."""
        recs = self.peek(reader, limit=limit, types=types, room=room)
        if recs:
            last = recs[-1].get("seq")
            if isinstance(last, int) and not isinstance(last, bool):
                self.commit(reader, last)
        return recs

    def wait(
        self,
        reader: str,
        *,
        timeout: float = 600.0,
        poll: float = 0.2,
        include_own: bool = False,
    ) -> list[dict[str, Any]]:
        """Block until inbound records are available; return ``[]`` at timeout.

        Unless ``include_own`` is true, only records with
        ``direction == "in"`` wake the waiter; outbound (own) records are
        skipped without committing anything. :meth:`peek` and :meth:`read`
        keep returning both directions.

        Never commits; the caller decides when to :meth:`commit`.
        """
        _validate_reader(reader)
        if timeout < 0:
            timeout = 0.0
        if poll <= 0:
            poll = 0.05
        deadline = time.monotonic() + timeout
        while True:
            if include_own:
                recs = self.peek(reader)
                if recs:
                    return recs
            else:
                cur = self.cursor(reader)
                inbound: list[dict[str, Any]] = []
                for rec in self._read_all():
                    seq = rec.get("seq")
                    if isinstance(seq, bool) or not isinstance(seq, int):
                        continue
                    if seq <= cur:
                        continue
                    if rec.get("direction") != "in":
                        continue
                    inbound.append(rec)
                    if len(inbound) >= 1000:
                        break
                if inbound:
                    return inbound
            now = time.monotonic()
            if now >= deadline:
                return []
            remaining = deadline - now
            time.sleep(min(poll, remaining))

    def _read_holder(self) -> dict[str, Any]:
        """Best-effort read of the current owner-lock holder info."""
        try:
            with open(self.owner_lock_path, encoding="utf-8") as fh:  # noqa: PTH123
                obj = json.load(fh)
        except (OSError, ValueError):
            return {}
        if isinstance(obj, dict):
            return cast(dict[str, Any], dict(obj))
        return {}

    def _append_audit(self, session: str, holder: dict[str, Any]) -> None:
        """Append one JSON line describing a refused owner attempt."""
        line = (
            json.dumps(
                {
                    "ts": _now_iso(),
                    "session": session,
                    "pid": os.getpid(),
                    "holder": holder,
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        payload = line.encode("utf-8")
        fd = os.open(self.audit_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            _write_all(fd, payload)
            os.fsync(fd)
        finally:
            os.close(fd)

    def acquire_owner(self, session: str) -> OwnerLock:
        """Try to become the single owner; non-blocking.

        On success the ``owner.lock`` file holds ``{"session","pid","since"}``
        and the returned :class:`OwnerLock` keeps the flock until closed.
        On failure an audit line is appended and :class:`OwnerBusy`
        (carrying the holder dict) is raised.
        """
        _validate_session(session)
        _ensure_private_dir(self.mailbox_dir)
        _ensure_private_dir(self.cursors_dir)
        fd = os.open(self.owner_lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = self._read_holder()
            try:
                self._append_audit(session, holder)
            finally:
                os.close(fd)
            raise OwnerBusy(holder) from exc
        info: dict[str, Any] = {
            "session": session,
            "pid": os.getpid(),
            "since": _now_iso(),
        }
        payload = (json.dumps(info, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            _write_all(fd, payload)
            os.fsync(fd)
        except OSError:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        return OwnerLock(fd, self.owner_lock_path, info)
