"""Muse Code waker adapter (docs/spec/muse.md, docs/research/muse.md).

Self-contained: stdlib only, no back-imports. Every ring function returns
one line (``rang:``, ``resumed:``, ``skip:``, ``ring failed:``) and never
raises. Fail closed: no subprocess is started unless liveness is known.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "RESUMABLE_PROVIDERS",
    "SEND_TIMEOUT",
    "RESUME_TIMEOUT",
    "NUDGE_MAX",
    "Reach",
    "muse_home",
    "muse_exe",
    "find_session_dir",
    "resolve_target",
    "reachable",
    "wake",
    "resume_headless",
    "ring_muse",
    "main",
]

RESUMABLE_PROVIDERS: tuple[str, ...] = ("echo", "meta")
SEND_TIMEOUT: float = 30.0
RESUME_TIMEOUT: float = 900.0
NUDGE_MAX: int = 4000

_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_STATUS_RE = re.compile(r"[a-z_]{1,32}")
_ERROR_RE = re.compile(r"[a-z0-9_]{1,48}")


def _effective_home(arg: str | None) -> str:
    """Resolve a muse home argument to a directory string."""
    if arg is not None:
        return os.path.expanduser(arg)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return os.path.join(xdg, "muse")
    return os.path.expanduser("~/.local/share/muse")


def muse_home(muse_home: str | None = None) -> str:
    """Return the Muse data root for ``arg`` or the environment."""
    return _effective_home(muse_home)


def muse_exe(muse_bin: str | None = None) -> str | None:
    """Return an executable ``muse`` path or ``None``."""
    if muse_bin:
        expanded = os.path.expanduser(muse_bin)
        try:
            if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
                return expanded
        except OSError:
            pass
        return None
    return shutil.which("muse")


def _valid_nudge(value: object) -> bool:
    """Check the fixed wake text shape (never peer-controlled)."""
    if not isinstance(value, str):
        return False
    if len(value) < 1 or len(value) > NUDGE_MAX:
        return False
    if value.startswith("-"):
        return False
    for ch in value:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            return False
    return True


def _has_control(text: str) -> bool:
    """Return True when ``text`` holds an ASCII control character."""
    for ch in text:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            return True
    return False


def find_session_dir(home: str, session_id: str) -> str | None:
    """Return the session directory for ``session_id`` or ``None``."""
    if not isinstance(home, str) or not isinstance(session_id, str):
        return None
    if _UUID_RE.fullmatch(session_id) is None:
        return None
    lowered = session_id.lower()
    base = Path(home) / "sessions"
    try:
        matches = list(base.glob(f"*/*/*/{lowered}/session.jsonl"))
    except OSError:
        return None
    if len(matches) != 1:
        return None
    return str(matches[0].parent)


def _current_name(session_file: str) -> str | None:
    """Return the current session name recorded in one ``session.jsonl``."""
    name: str | None = None
    try:
        with open(session_file, encoding="utf-8", errors="replace") as fh:  # noqa: PTH123
            for raw in fh:
                if "session.name.changed" not in raw:
                    continue
                try:
                    obj: Any = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if obj.get("payload_type") != "session.name.changed":
                    continue
                payload: Any = obj.get("payload")
                if not isinstance(payload, dict):
                    continue
                if "new_name" not in payload:
                    continue
                nxt: Any = payload["new_name"]
                if isinstance(nxt, str):
                    name = nxt
                elif nxt is None:
                    name = None
    except OSError:
        return None
    return name


def _read_facts(session_dir: str) -> tuple[str | None, str | None, str | None]:
    """Read workspace, provider and current name for a session dir."""
    workspace: str | None = None
    provider: str | None = None
    name: str | None = None
    meta_found = False
    path = os.path.join(session_dir, "session.jsonl")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:  # noqa: PTH123
            for idx, raw in enumerate(fh):
                if not meta_found and idx < 400 and "runtime.session.metadata" in raw:
                    try:
                        obj: Any = json.loads(raw)
                    except ValueError:
                        obj = None
                    if isinstance(obj, dict) and obj.get("payload_type") == "runtime.session.metadata":
                        payload: Any = obj.get("payload")
                        if isinstance(payload, dict):
                            record: Any = payload.get("record")
                            if isinstance(record, dict):
                                ws: Any = record.get("workspace_root")
                                pv: Any = record.get("provider_id")
                                workspace = ws if isinstance(ws, str) and ws else None
                                provider = pv if isinstance(pv, str) and pv else None
                                meta_found = True
                            else:
                                meta_found = True
                        else:
                            meta_found = True
                if "session.name.changed" in raw:
                    try:
                        obj2: Any = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(obj2, dict):
                        continue
                    if obj2.get("payload_type") != "session.name.changed":
                        continue
                    payload2: Any = obj2.get("payload")
                    if not isinstance(payload2, dict):
                        continue
                    if "new_name" not in payload2:
                        continue
                    nxt2: Any = payload2["new_name"]
                    if isinstance(nxt2, str):
                        name = nxt2
                    elif nxt2 is None:
                        name = None
    except OSError:
        return (None, None, None)
    return (workspace, provider, name)


def resolve_target(target: str, muse_home: str | None = None) -> tuple[str | None, str]:
    """Resolve a UUID or session name to ``(uuid, reason)``."""
    if not isinstance(target, str):
        return (None, "bad target")
    stripped = target.strip()
    if stripped == "" or len(stripped) > 128 or _has_control(stripped):
        return (None, "bad target")
    if _UUID_RE.fullmatch(stripped) is not None:
        lowered = stripped.lower()
        home = _effective_home(muse_home)
        found = find_session_dir(home, lowered)
        if found is not None:
            return (lowered, "")
        return (None, "no such session")
    home = _effective_home(muse_home)
    base = Path(home) / "sessions"
    try:
        if not base.is_dir():
            return (None, "no such session")
    except OSError:
        return (None, "no such session")
    try:
        candidates = list(base.glob("*/*/*/*/session.jsonl"))
    except OSError:
        return (None, "no such session")
    matches: list[str] = []
    for cand in candidates:
        sid_dir = cand.parent.name
        if _UUID_RE.fullmatch(sid_dir) is None:
            continue
        cur = _current_name(str(cand))
        if cur is not None and cur == stripped:
            matches.append(sid_dir.lower())
    if len(matches) == 1:
        return (matches[0], "")
    if not matches:
        return (None, "no such session")
    return (None, "ambiguous name")


def _probe_lock(session_dir: str) -> tuple[str, str]:
    """Probe liveness via a non-blocking shared flock."""
    lock_path = os.path.join(session_dir, ".session.lock")
    try:
        fd = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return ("unknown", "no lock file")
    except OSError:
        return ("unknown", "lock unreadable")
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            code = exc.errno
            if isinstance(exc, BlockingIOError) or code in (errno.EWOULDBLOCK, errno.EAGAIN):
                return ("live", "session lock held")
            return ("unknown", "lock probe failed")
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return ("dead", "session lock free")
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


@dataclass(frozen=True)
class Reach:
    """Read-only reachability snapshot for one Muse session."""

    state: str
    session_id: str | None = None
    name: str | None = None
    workspace: str | None = None
    provider: str | None = None
    session_dir: str | None = None
    reason: str = ""

    @property
    def resumable(self) -> bool:
        """True when a headless ``exec`` resume could work."""
        return (
            self.state == "dead"
            and self.provider in RESUMABLE_PROVIDERS
            and isinstance(self.workspace, str)
            and os.path.isdir(self.workspace)
        )

    def as_dict(self) -> dict[str, object]:
        """Return all fields plus ``resumable`` as plain JSON values."""
        return {
            "state": self.state,
            "session_id": self.session_id,
            "name": self.name,
            "workspace": self.workspace,
            "provider": self.provider,
            "session_dir": self.session_dir,
            "reason": self.reason,
            "resumable": self.resumable,
        }


def reachable(target: str, muse_home: str | None = None) -> Reach:
    """Resolve ``target`` and probe liveness without starting muse."""
    home = _effective_home(muse_home)
    sid, reason = resolve_target(target, home)
    if sid is None:
        return Reach(state="missing", reason=reason)
    sess_dir = find_session_dir(home, sid)
    if sess_dir is None:
        return Reach(state="missing", reason="no such session")
    workspace, provider, name = _read_facts(sess_dir)
    state, probe_reason = _probe_lock(sess_dir)
    return Reach(
        state=state,
        session_id=sid,
        name=name,
        workspace=workspace,
        provider=provider,
        session_dir=sess_dir,
        reason=probe_reason,
    )


def _base_env() -> dict[str, str]:
    """Child environment pinning the launcher behaviour."""
    env = dict(os.environ)
    env["MUSE_NO_AUTO_UPDATE"] = "1"
    env["MUSE_LOGIN"] = "0"
    return env


def wake(
    target: str,
    fixed_nudge: str,
    *,
    muse_bin: str | None = None,
    muse_home: str | None = None,
    timeout: float = SEND_TIMEOUT,
) -> str:
    """Ring a live session via ``session-message send``; return one line."""
    try:
        if not _valid_nudge(fixed_nudge):
            return "ring failed: invalid nudge"
        exe = muse_exe(muse_bin)
        if exe is None:
            return "ring failed: no muse executable"
        probed = reachable(target, muse_home)
        if probed.state == "missing":
            return f"skip: unreachable: {probed.reason}"
        if probed.state == "unknown":
            return "skip: liveness unknown"
        if probed.state == "dead":
            return "skip: not running (resume_headless wakes ended sessions)"
        if probed.session_id is None:
            return "skip: liveness unknown"
        argv = [exe, "session-message", "send", "--target", probed.session_id, "--json"]
        env = _base_env()
        env["MUSE_EXPERIMENTAL_EXTERNAL_AGENT_INGRESS"] = "1"
        try:
            done: Any = subprocess.run(
                argv,
                input=fixed_nudge.encode("utf-8"),
                capture_output=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return "ring failed: timed out"
        except OSError as exc:
            return f"ring failed: {type(exc).__name__}"
        except Exception as exc:
            return f"ring failed: {type(exc).__name__}"
        rc = int(done.returncode)
        raw_out: Any = done.stdout
        if isinstance(raw_out, bytes):
            text = raw_out.decode("utf-8", errors="replace")
        elif isinstance(raw_out, str):
            text = raw_out
        else:
            text = ""
        try:
            parsed: Any = json.loads(text) if text.strip() else None
        except ValueError:
            return f"ring failed: rc={rc}"
        if not isinstance(parsed, dict):
            return f"ring failed: rc={rc}"
        if "error_code" in parsed:
            code: Any = parsed["error_code"]
            if isinstance(code, str) and _ERROR_RE.fullmatch(code) is not None:
                return f"skip: {code}"
            return f"ring failed: rc={rc}"
        if rc != 0:
            return f"ring failed: rc={rc}"
        status: Any = parsed.get("status")
        if isinstance(status, str) and _STATUS_RE.fullmatch(status) is not None:
            return f"rang: {status}"
        return "rang: sent"
    except Exception as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            return "ring failed: timed out"
        return f"ring failed: {type(exc).__name__}"


def resume_headless(
    target: str,
    fixed_nudge: str,
    cwd: str | None = None,
    *,
    muse_bin: str | None = None,
    muse_home: str | None = None,
    home: str | None = None,
    extra_args: Sequence[str] = (),
    timeout: float = RESUME_TIMEOUT,
) -> str:
    """Revive an ended session via ``exec --session-id``; one line."""
    try:
        if not _valid_nudge(fixed_nudge):
            return "ring failed: invalid nudge"
        exe = muse_exe(muse_bin)
        if exe is None:
            return "ring failed: no muse executable"
        probed = reachable(target, muse_home)
        if probed.state == "missing":
            return f"skip: unreachable: {probed.reason}"
        if probed.state == "unknown":
            return "skip: liveness unknown"
        if probed.state == "live":
            return "skip: live (a running Muse session is never resumed)"
        if probed.provider not in RESUMABLE_PROVIDERS:
            return "skip: unreachable: provider cannot be resumed headlessly"
        if probed.workspace is None or not os.path.isdir(probed.workspace):
            return "skip: unreachable: workspace gone"
        proc_cwd: str
        if cwd is not None:
            if not isinstance(cwd, str) or not os.path.isdir(cwd):
                return "skip: unreachable: cwd gone"
            proc_cwd = cwd
        else:
            if not isinstance(probed.workspace, str):
                return "skip: unreachable: workspace gone"
            proc_cwd = probed.workspace
        for item in extra_args:
            if not isinstance(item, str):
                return "ring failed: invalid extra args"
            if "\x00" in item:
                return "ring failed: invalid extra args"
            if item in ("--session-id", "--workspace", "--provider"):
                return "ring failed: invalid extra args"
            if (
                item.startswith("--session-id=")
                or item.startswith("--workspace=")
                or item.startswith("--provider=")
            ):
                return "ring failed: invalid extra args"
        if probed.session_id is None or probed.session_dir is None:
            return "skip: liveness unknown"
        fresh_state, _ = _probe_lock(probed.session_dir)
        if fresh_state == "live":
            return "skip: live (a running Muse session is never resumed)"
        if fresh_state != "dead":
            return "skip: liveness unknown"
        if not isinstance(probed.provider, str) or not isinstance(probed.workspace, str):
            return "skip: unreachable: workspace gone"
        argv = [
            exe,
            "exec",
            "--json",
            "--provider",
            probed.provider,
            "--workspace",
            probed.workspace,
            "--session-id",
            probed.session_id,
            *list(extra_args),
            "--",
            fixed_nudge,
        ]
        env = _base_env()
        if home is not None:
            env["PHONING_HOME"] = os.path.abspath(os.path.expanduser(home))
        try:
            done: Any = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                cwd=proc_cwd,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return "ring failed: timed out"
        except OSError as exc:
            return f"ring failed: {type(exc).__name__}"
        except Exception as exc:
            return f"ring failed: {type(exc).__name__}"
        return f"resumed: rc={int(done.returncode)}"
    except Exception as exc:
        if isinstance(exc, subprocess.TimeoutExpired):
            return "ring failed: timed out"
        return f"ring failed: {type(exc).__name__}"


def ring_muse(
    target: str,
    fixed_nudge: str,
    *,
    muse_bin: str | None = None,
    muse_home: str | None = None,
    resume_dead: bool = True,
    cwd: str | None = None,
    home: str | None = None,
    extra_args: Sequence[str] = (),
) -> str:
    """Route to ``wake`` (live) or ``resume_headless`` (ended)."""
    try:
        if not _valid_nudge(fixed_nudge):
            return "ring failed: invalid nudge"
        exe = muse_exe(muse_bin)
        if exe is None:
            return "ring failed: no muse executable"
        probed = reachable(target, muse_home)
        if probed.state == "missing":
            return f"skip: unreachable: {probed.reason}"
        if probed.state == "unknown":
            return "skip: liveness unknown"
        if probed.state == "live":
            return wake(target, fixed_nudge, muse_bin=muse_bin, muse_home=muse_home)
        if not resume_dead:
            return "skip: not running (resume not enabled)"
        return resume_headless(
            target,
            fixed_nudge,
            cwd,
            muse_bin=muse_bin,
            muse_home=muse_home,
            home=home,
            extra_args=extra_args,
        )
    except Exception as exc:
        return f"ring failed: {type(exc).__name__}"


def main(argv: Sequence[str] | None = None) -> int:
    """Print ``Reach.as_dict()`` for ``reach TARGET``; read-only."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] != "reach":
        print("usage: muse_ring reach TARGET [--muse-home DIR]", file=sys.stderr)
        return 2
    rest = args[1:]
    target: str | None = None
    home_val: str | None = None
    idx = 0
    while idx < len(rest):
        item = rest[idx]
        if item == "--muse-home":
            if idx + 1 >= len(rest):
                print("usage: muse_ring reach TARGET [--muse-home DIR]", file=sys.stderr)
                return 2
            home_val = rest[idx + 1]
            idx += 2
        elif item.startswith("-"):
            print("usage: muse_ring reach TARGET [--muse-home DIR]", file=sys.stderr)
            return 2
        else:
            if target is not None:
                print("usage: muse_ring reach TARGET [--muse-home DIR]", file=sys.stderr)
                return 2
            target = item
            idx += 1
    if target is None:
        print("usage: muse_ring reach TARGET [--muse-home DIR]", file=sys.stderr)
        return 2
    probed = reachable(target, home_val)
    print(json.dumps(probed.as_dict()))
    if probed.state in ("live", "dead"):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
