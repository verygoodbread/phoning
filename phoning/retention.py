"""Retention: ``phoning prune`` trims a mailbox ``log.jsonl``.

The log is append-only and holds decrypted bodies, so it grows forever.
``prune`` removes records that are no longer needed, archives what it
removes (unless ``--no-archive``), and never disturbs readers or a live
owner daemon.

Locking follows ``Mailbox.append``: an exclusive ``flock`` on ``log.jsonl``
is held for the whole read-decide-write and the log is rewritten in place
in the same inode. A staged ``.ready`` file holds the intended content for
crash recovery.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import gzip
import json
import os
import re
import sys
import tempfile
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, cast

__all__ = ["main"]

_NAME_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_.-]{1,64}")

_KEEP_BY_ORDER: tuple[str, ...] = (
    "tail",
    "recent",
    "unread",
    "thread",
    "decisions",
    "opaque",
)

_TS_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


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
    """Best-effort fsync of directory ``path``."""
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
    """Create ``path`` as ``0o700``, tightening an existing directory."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


def _parse_ts(raw: Any) -> datetime | None:
    """Parse ``raw`` to an aware UTC datetime, else ``None``.

    Accepts ISO-8601 (``Z`` suffix allowed) plus a few common forms.
    Naive results are read as UTC. Non-strings never parse.
    """
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.strip()
    if not text:
        return None
    iso = text
    if iso.endswith(("Z", "z")):
        iso = iso[:-1] + "+00:00"
    parsed: datetime | None = None
    with contextlib.suppress(ValueError):
        parsed = datetime.fromisoformat(iso)
    if parsed is None:
        for fmt in _TS_FORMATS:
            try:
                parsed = datetime.strptime(text, fmt)
            except ValueError:
                continue
            break
        else:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _ts_seconds(raw: Any) -> float | None:
    """Epoch seconds for ``raw``, or ``None`` when unparseable."""
    dt = _parse_ts(raw)
    if dt is None:
        return None
    return dt.timestamp()


def _record_seq(obj: Mapping[str, Any]) -> int | None:
    """Return ``obj['seq']`` when it is a non-bool int, else ``None``."""
    seq = obj.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return None
    return seq


def _env_of(obj: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return ``obj['env']`` when it is a mapping, else ``{}``."""
    env = obj.get("env")
    if isinstance(env, Mapping):
        return env
    return {}


def _parse_line(line: bytes) -> dict[str, Any] | None:
    """Parse ``line`` as a JSON object with an int ``seq``, else ``None``."""
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    typed = cast(dict[str, Any], obj)
    if _record_seq(typed) is None:
        return None
    return typed


def _split_lines(data: bytes) -> list[bytes]:
    """Split ``data`` keeping terminators; a trailing partial is preserved."""
    return data.splitlines(keepends=True)


def _resolve_home(home: str | None) -> str:
    """Resolve home from ``--home``, ``$PHONING_HOME`` or ``~/.phoning``."""
    if home:
        return os.path.expanduser(home)
    env = os.environ.get("PHONING_HOME")
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/.phoning")


def _read_cursors(cursors_dir: str) -> tuple[dict[str, int], list[str]]:
    """Read ``*.json`` cursors; bad files count as ``0`` with a warning."""
    cursors: dict[str, int] = {}
    warnings: list[str] = []
    try:
        names = sorted(os.listdir(cursors_dir))
    except OSError:
        return cursors, warnings
    for name in names:
        if not name.endswith(".json"):
            continue
        reader = name[: -len(".json")]
        path = os.path.join(cursors_dir, name)
        try:
            with open(path, encoding="utf-8") as fh:
                obj = json.load(fh)
        except (OSError, ValueError):
            cursors[reader] = 0
            warnings.append(f"unreadable cursor for reader {reader!r}")
            continue
        if not isinstance(obj, dict):
            cursors[reader] = 0
            warnings.append(f"invalid cursor for reader {reader!r}")
            continue
        seq = cast(dict[str, Any], obj).get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            cursors[reader] = 0
            warnings.append(f"invalid cursor for reader {reader!r}")
            continue
        cursors[reader] = seq
    return cursors, warnings


def _min_cursor(cursors: Mapping[str, int]) -> tuple[int | None, str | None]:
    """Lowest committed ``(seq, reader)``; ``(None, None)`` when empty."""
    if not cursors:
        return None, None
    reader = min(cursors, key=lambda r: (cursors[r], r))
    return cursors[reader], reader


def _rank(reasons: set[str]) -> str:
    """Winning rule for ``reasons`` in ``_KEEP_BY_ORDER`` precedence."""
    for name in _KEEP_BY_ORDER:
        if name in reasons:
            return name
    return "opaque"


def _compute_keep_set(
    records: list[tuple[int | None, dict[str, Any] | None, bytes]],
    cursors: Mapping[str, int],
    keep_days: float,
    keep_last: int,
    now: float,
) -> tuple[list[int], dict[str, int]]:
    """Decide which record indices survive; attribute each once.

    ``records`` is ``(seq, parsed_or_None, raw_line)`` in log order.
    Returns ``(keep_indices, kept_by)`` where ``kept_by`` follows the
    ``tail, recent, unread, thread, decisions, opaque`` precedence.
    """
    n = len(records)
    reasons: list[set[str]] = [set() for _ in range(n)]
    seq_to_indices: dict[int, list[int]] = {}
    for i, (seq, _o, _l) in enumerate(records):
        if seq is not None:
            seq_to_indices.setdefault(seq, []).append(i)
    int_seqs = [s for s, _o, _l in records if s is not None]
    max_seq = max(int_seqs) if int_seqs else None
    for i, (seq, obj, _l) in enumerate(records):
        if seq is None or obj is None:
            reasons[i].add("opaque")
    if int_seqs and max_seq is not None:
        tail = sorted(set(int_seqs))
        tail = tail[-keep_last:] if keep_last > 0 else []
        if max_seq not in tail:
            tail.append(max_seq)
        for s in tail:
            for i in seq_to_indices.get(s, ()):
                reasons[i].add("tail")
    cutoff = now - keep_days * 86400.0
    for i, (_s, obj, _l) in enumerate(records):
        if obj is None:
            continue
        env = _env_of(obj)
        ts = obj.get("recv_ts")
        if ts is None:
            ts = env.get("ts")
        secs = _ts_seconds(ts)
        if secs is None or secs >= cutoff:
            reasons[i].add("recent")
    low, _r = _min_cursor(cursors)
    if low is not None:
        for i, (seq, obj, _l) in enumerate(records):
            if obj is None or seq is None:
                continue
            if seq > low:
                reasons[i].add("unread")
    eid_to_index: dict[str, int] = {}
    replies: dict[str, list[int]] = {}
    for i, (_s, obj, _l) in enumerate(records):
        if obj is None:
            continue
        env = _env_of(obj)
        eid = env.get("eid")
        if isinstance(eid, str) and eid and eid not in eid_to_index:
            eid_to_index[eid] = i
        reply_to = env.get("reply_to")
        if isinstance(reply_to, str) and reply_to:
            replies.setdefault(reply_to, []).append(i)
    queue: deque[int] = deque(i for i in range(n) if reasons[i])
    while queue:
        i = queue.popleft()
        obj = records[i][1]
        if obj is None:
            continue
        env = _env_of(obj)
        for key in ("thread", "reply_to"):
            ref = env.get(key)
            if not isinstance(ref, str) or not ref:
                continue
            j = eid_to_index.get(ref)
            if j is not None and not reasons[j]:
                reasons[j].add("thread")
                queue.append(j)
        if env.get("type") in ("ask", "propose"):
            eid = env.get("eid")
            if not isinstance(eid, str) or not eid:
                continue
            for j in replies.get(eid, ()):
                if not reasons[j]:
                    reasons[j].add("decisions")
                    queue.append(j)
    keep = [i for i in range(n) if reasons[i]]
    kept_by: dict[str, int] = {k: 0 for k in _KEEP_BY_ORDER}
    for i in keep:
        kept_by[_rank(reasons[i])] += 1
    return keep, kept_by


def _merge_recovery(ready_data: bytes, current_data: bytes) -> bytes:
    """Complete a staged rewrite: ``.ready`` plus later ``seq`` records."""
    ready_lines = _split_lines(ready_data)
    seen: set[int] = set()
    ready_max = 0
    for line in ready_lines:
        obj = _parse_line(line)
        if obj is None:
            continue
        seq = _record_seq(obj)
        if seq is None:
            continue
        seen.add(seq)
        if seq > ready_max:
            ready_max = seq
    extra: list[bytes] = []
    for line in _split_lines(current_data):
        obj = _parse_line(line)
        if obj is None:
            continue
        seq = _record_seq(obj)
        if seq is None or seq <= ready_max or seq in seen:
            continue
        seen.add(seq)
        extra.append(line)
    return b"".join(ready_lines) + b"".join(extra)


def _archive_path(archive_dir: str, stamp: str) -> str:
    """Fresh archive path for ``stamp``; never an existing file."""
    base = os.path.join(archive_dir, f"log-{stamp}.jsonl.gz")
    if not os.path.exists(base):
        return base
    n = 1
    while True:
        cand = os.path.join(archive_dir, f"log-{stamp}-{n}.jsonl.gz")
        if not os.path.exists(cand):
            return cand
        n += 1


def _write_archive(archive_dir: str, stamp: str, pruned: bytes) -> str:
    """Gzip ``pruned`` into ``archive_dir`` (0700, file 0600); return path."""
    _ensure_private_dir(archive_dir)
    dest = _archive_path(archive_dir, stamp)
    fd, tmp = tempfile.mkstemp(dir=archive_dir, prefix=".prune-", suffix=".tmp")
    try:
        os.chmod(tmp, 0o600)
        with os.fdopen(fd, "wb") as fh:
            with gzip.GzipFile(fileobj=fh, mode="wb", mtime=0) as gz:
                gz.write(pruned)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, dest)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _fsync_dir(archive_dir)
    return dest


def _read_fd(fd: int) -> bytes:
    """Read the whole file behind ``fd`` via a ``pread`` loop."""
    size = os.fstat(fd).st_size
    if size <= 0:
        return b""
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
    return b"".join(chunks)


def _apply_in_place(fd: int, data: bytes) -> None:
    """Rewrite ``fd`` in place: truncate, write at 0, fsync (same inode)."""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    _write_all(fd, data)
    os.fsync(fd)


def _stage_ready(mailbox_dir: str, data: bytes) -> str:
    """Atomically stage ``data`` as ``log.jsonl.prune.ready``."""
    partial = os.path.join(mailbox_dir, "log.jsonl.prune.partial")
    ready = os.path.join(mailbox_dir, "log.jsonl.prune.ready")
    fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.chmod(partial, 0o600)
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(partial, ready)
    _fsync_dir(mailbox_dir)
    return ready


def _run(
    log_path: str,
    mailbox_dir: str,
    cursors: Mapping[str, int],
    *,
    keep_days: float,
    keep_last: int,
    archive: bool,
    dry_run: bool,
    report: dict[str, Any],
) -> int:
    """Run one prune pass under the caller-visible lock; return exit code."""
    now = datetime.now(UTC).timestamp()
    partial = os.path.join(mailbox_dir, "log.jsonl.prune.partial")
    ready = os.path.join(mailbox_dir, "log.jsonl.prune.ready")
    archive_dir = os.path.join(mailbox_dir, "archive")
    flags = os.O_RDONLY if dry_run else os.O_RDWR
    fd = os.open(log_path, flags)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH if dry_run else fcntl.LOCK_EX)
        try:
            current = _read_fd(fd)
            pending = os.path.exists(ready)
            if dry_run and pending:
                report["recovery_pending"] = True
            if pending and not dry_run:
                with open(ready, "rb") as fh:
                    ready_data = fh.read()
                merged = _merge_recovery(ready_data, current)
                _apply_in_place(fd, merged)
                with contextlib.suppress(OSError):
                    os.unlink(ready)
                with contextlib.suppress(OSError):
                    os.unlink(partial)
                _fsync_dir(mailbox_dir)
                current = merged
                report["recovered"] = True
            elif not dry_run and os.path.exists(partial):
                with contextlib.suppress(OSError):
                    os.unlink(partial)
            lines = _split_lines(current)
            report["bytes_before"] = len(current)
            report["total"] = len(lines)
            recs: list[tuple[int | None, dict[str, Any] | None, bytes]] = []
            for line in lines:
                if not line.strip():
                    recs.append((None, None, line))
                    continue
                obj = _parse_line(line)
                if obj is None:
                    recs.append((None, None, line))
                else:
                    recs.append((_record_seq(obj), obj, line))
            keep_idx, kept_by = _compute_keep_set(recs, cursors, keep_days, keep_last, now)
            is_kept = [False] * len(recs)
            for i in keep_idx:
                is_kept[i] = True
            kept_data = b"".join(recs[i][2] for i in range(len(recs)) if is_kept[i])
            report["kept"] = len(keep_idx)
            report["pruned"] = report["total"] - report["kept"]
            report["kept_by"] = kept_by
            report["bytes_after"] = len(kept_data)
            if not report["pruned"] or dry_run:
                return 0
            if archive:
                pruned_data = b"".join(recs[i][2] for i in range(len(recs)) if not is_kept[i])
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
                report["archive"] = _write_archive(archive_dir, stamp, pruned_data)
            _stage_ready(mailbox_dir, kept_data)
            _apply_in_place(fd, kept_data)
            with contextlib.suppress(OSError):
                os.unlink(ready)
            _fsync_dir(mailbox_dir)
            return 0
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        os.close(fd)


def _build_parser() -> argparse.ArgumentParser:
    """Build the ``phoning prune`` argument parser."""
    parser = argparse.ArgumentParser(
        prog="phoning prune",
        description="Trim a mailbox log and archive what is removed.",
    )
    parser.add_argument("name", help="mailbox name")
    parser.add_argument(
        "--keep-days", type=float, default=30.0, help="keep records newer than N days"
    )
    parser.add_argument(
        "--keep-last", type=int, default=500, help="keep the last N records by seq"
    )
    parser.add_argument(
        "--archive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="archive pruned records (default) or discard them",
    )
    parser.add_argument("--dry-run", action="store_true", help="report only")
    parser.add_argument("--json", action="store_true", help="print a JSON report")
    parser.add_argument("--home", default=None, help="phoning home directory")
    return parser


def _validate_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Reject negative retentions; argparse handles the rest."""
    keep_days = float(cast(float, args.keep_days))
    keep_last = int(cast(int, args.keep_last))
    if keep_days < 0:
        parser.error("--keep-days must be >= 0")
    if keep_last < 0:
        parser.error("--keep-last must be >= 0")


def _valid_name(name: str) -> bool:
    """True when ``name`` is a safe mailbox directory name."""
    if name in (".", ".."):
        return False
    return _NAME_RE.fullmatch(name) is not None


def _emit_human(report: Mapping[str, Any], name: str, dry_run: bool) -> None:
    """Print one human-readable summary line."""
    total = int(report["total"])
    kept = int(report["kept"])
    pruned = int(report["pruned"])
    if not pruned:
        if dry_run:
            print(f"[dry-run] nothing to prune for {name} ({total} records)")
        else:
            print(f"nothing to prune for {name} ({total} records)")
        return
    prefix = "[dry-run] would prune" if dry_run else "pruned"
    arch = report.get("archive")
    if isinstance(arch, str) and arch:
        print(f"{prefix} {name}: {pruned} of {total} records (kept {kept}; archive {arch})")
    else:
        print(f"{prefix} {name}: {pruned} of {total} records (kept {kept}; no archive)")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``phoning prune``; return a process exit code."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _validate_args(args, parser)
    name = str(cast(str, args.name))
    if not _valid_name(name):
        print(f"error: invalid mailbox name {name!r}", file=sys.stderr)
        return 2
    home = _resolve_home(cast("str | None", args.home))
    user_dir = os.path.join(home, name)
    mailbox_dir = os.path.join(user_dir, "mailbox")
    log_path = os.path.join(mailbox_dir, "log.jsonl")
    cursors_dir = os.path.join(mailbox_dir, "cursors")
    dry_run = bool(args.dry_run)
    keep_days = float(cast(float, args.keep_days))
    keep_last = int(cast(int, args.keep_last))
    use_archive = bool(args.archive)
    if not os.path.exists(log_path):
        print(f"error: no mailbox for {name} at {user_dir}", file=sys.stderr)
        return 2
    cursors, warnings = _read_cursors(cursors_dir)
    low, who = _min_cursor(cursors)
    report: dict[str, Any] = {
        "name": name,
        "dry_run": dry_run,
        "total": 0,
        "kept": 0,
        "pruned": 0,
        "kept_by": {k: 0 for k in _KEEP_BY_ORDER},
        "min_cursor": low,
        "min_cursor_reader": who,
        "bytes_before": 0,
        "bytes_after": 0,
        "archive": None,
        "recovered": False,
        "recovery_pending": False,
        "warnings": list(warnings),
    }
    try:
        rc = _run(
            log_path,
            mailbox_dir,
            cursors,
            keep_days=keep_days,
            keep_last=keep_last,
            archive=use_archive,
            dry_run=dry_run,
            report=report,
        )
    except OSError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if rc != 0:
        return rc
    if bool(args.json):
        print(json.dumps(report, ensure_ascii=False))
    else:
        _emit_human(report, name, dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
