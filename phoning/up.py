"""`phoning up` (docs/SPEC-v0.2.md §1, §7.5): keep the relay, owner and doorbell daemons alive.

``up`` is idempotent reconciliation: it starts what is missing and never
stops, signals or restarts anything already running. Stdlib only.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import io
import json
import math
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

from phoning import bridge as bridge_mod
from phoning import hooks, retention

__all__ = [
    "BRIDGE_START_TIMEOUT",
    "ConfigError",
    "launchagent_plist",
    "load_config",
    "lock_holder",
    "main",
    "owner_holder",
    "path_holder",
    "reconcile",
    "relay_up",
]

DEFAULT_CONFIG = "~/.config/phoning/up.json"
DEFAULT_LABEL = "dev.phoning.up"
DEFAULT_INTERVAL = 2
START_TIMEOUT = 10.0
BRIDGE_START_TIMEOUT: float = 30.0
_POLL = 0.1
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_QUIET_STATES = frozenset({"started", "failed", "unmanaged", "pruned"})

_OWNER_LOCK = "owner.lock"
_DOORBELL_LOCK = "doorbell.lock"

_DOORBELL_FLAGS: tuple[tuple[str, str], ...] = (
    ("reader", "--reader"),
    ("hook_reader", "--hook-reader"),
    ("ring", "--ring"),
    ("codex_home", "--codex-home"),
    ("codex_thread", "--codex-thread"),
    ("min_interval", "--min-interval"),
    ("poll", "--poll"),
)

_RING_VALUES: frozenset[str] = frozenset(
    {
        "none",
        "codex-app-server",
        "codex-queue",
        "claude-resume",
        "notify",
        "muse",
        "kimi",
        "openclaw",
    }
)
_RULES_KEYS: frozenset[str] = frozenset({"ring", "resume", "notify"})
_RULES_VALUES: frozenset[str] = frozenset({"allow", "approve", "prohibit"})
_SANDBOX_MODES: frozenset[str] = frozenset(
    {"read-only", "workspace-write", "danger-full-access"}
)

_DOORBELL_STR_KEYS: tuple[str, ...] = (
    "reader",
    "hook_reader",
    "codex_thread",
    "claude_session",
    "muse_session",
    "kimi_session",
    "openclaw_session",
    "openclaw_agent",
)
_DOORBELL_PATH_KEYS: tuple[str, ...] = (
    "codex_home",
    "rules_file",
    "notify_cmd",
    "codex_bin",
    "claude_bin",
    "claude_home",
    "claude_token_file",
    "muse_bin",
    "muse_home",
    "kimi_cwd",
)
_DOORBELL_BOOL_KEYS: tuple[str, ...] = ("resume_dead", "muse_resume")

_THREAD_KEYS: tuple[str, ...] = ("codex_thread", "claude_session", "muse_session")

_EXAMPLE: str = json.dumps(
    {
        "relay": {"url": "http://127.0.0.1:8766", "db": "~/.phoning/relay/relay.db"},
        "owners": [
            {"home": "~/.phoning", "name": "claude-lead", "session": "claude-lead-main"}
        ],
        "doorbells": [
            {"home": "~/.phoning", "name": "claude-lead", "ring": "codex-app-server"}
        ],
    },
    indent=2,
)


class ConfigError(ValueError):
    """Raised for a missing, unreadable, or invalid ``up`` config."""


# -- config -----------------------------------------------------------------


def _expand(path: Any, *, what: str) -> str:
    """Expand ``~`` in a required path string."""
    if not isinstance(path, str) or not path:
        msg = f"{what} must be a non-empty path string"
        raise ConfigError(msg)
    return os.path.expanduser(path)


def _load_relay(raw: Any) -> dict[str, Any]:
    """Validate and normalise the optional ``relay`` section."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError("relay must be an object")
    typed: dict[str, Any] = raw
    allowed = {"url", "db", "log", "server"}
    unknown = sorted(set(typed) - allowed)
    if unknown:
        msg = f"relay has unknown keys: {', '.join(unknown)}"
        raise ConfigError(msg)
    if "url" not in typed:
        raise ConfigError("relay requires 'url'")
    if "db" not in typed:
        raise ConfigError("relay requires 'db'")
    url = typed["url"]
    if not isinstance(url, str) or not url:
        raise ConfigError("relay 'url' must be a non-empty string")
    db = _expand(typed["db"], what="relay 'db'")
    raw_log = typed.get("log")
    log = (
        os.path.join(os.path.dirname(db), "relay.log")
        if raw_log is None
        else _expand(raw_log, what="relay 'log'")
    )
    server = typed.get("server")
    if server is not None:
        server = _expand(server, what="relay 'server'")
    out: dict[str, Any] = {"url": url, "db": db, "log": log}
    if server is not None:
        out["server"] = server
    return out


def _load_owner(raw: Any, index: int) -> dict[str, Any]:
    """Validate and normalise one entry of the ``owners`` list."""
    if not isinstance(raw, dict):
        raise ConfigError(f"owners[{index}] must be an object")
    typed: dict[str, Any] = raw
    allowed = {"home", "name", "session", "interval"}
    unknown = sorted(set(typed) - allowed)
    if unknown:
        msg = f"owners[{index}] has unknown keys: {', '.join(unknown)}"
        raise ConfigError(msg)
    for key in ("home", "name", "session"):
        if key not in typed:
            raise ConfigError(f"owners[{index}] requires '{key}'")
    home = _expand(typed["home"], what=f"owners[{index}] 'home'")
    name = typed["name"]
    if not isinstance(name, str) or not name:
        raise ConfigError(f"owners[{index}] 'name' must be a non-empty string")
    session = typed["session"]
    if not isinstance(session, str) or not session:
        raise ConfigError(f"owners[{index}] 'session' must be a non-empty string")
    interval_raw = typed.get("interval", DEFAULT_INTERVAL)
    if isinstance(interval_raw, bool) or not isinstance(interval_raw, int):
        raise ConfigError(f"owners[{index}] 'interval' must be an int")
    if interval_raw < 0:
        raise ConfigError(f"owners[{index}] 'interval' must be >= 0")
    return {"home": home, "name": name, "session": session, "interval": interval_raw}


def _load_doorbell(raw: Any, index: int) -> dict[str, Any]:
    """Validate and normalise one entry of the ``doorbells`` list."""
    if not isinstance(raw, dict):
        raise ConfigError(f"doorbells[{index}] must be an object")
    typed: dict[str, Any] = raw
    allowed = {
        "home",
        "name",
        "reader",
        "hook_reader",
        "ring",
        "codex_home",
        "codex_thread",
        "types",
        "min_interval",
        "intro",
        "poll",
        "rules",
        "rules_file",
        "resume_min_interval",
        "max_resumes_per_day",
        "resume_max_sandbox",
        "notify_cmd",
        "resume_dead",
        "codex_bin",
        "claude_session",
        "claude_bin",
        "claude_home",
        "claude_token_file",
        "muse_session",
        "muse_bin",
        "muse_home",
        "muse_resume",
        "muse_args",
        "kimi_session",
        "kimi_cwd",
        "openclaw_session",
        "openclaw_agent",
    }
    unknown = sorted(set(typed) - allowed)
    if unknown:
        msg = f"doorbells[{index}] has unknown keys: {', '.join(unknown)}"
        raise ConfigError(msg)
    for key in ("home", "name"):
        if key not in typed:
            raise ConfigError(f"doorbells[{index}] requires '{key}'")
    home = _expand(typed["home"], what=f"doorbells[{index}] 'home'")
    name = typed["name"]
    if not isinstance(name, str) or not name:
        raise ConfigError(f"doorbells[{index}] 'name' must be a non-empty string")
    out: dict[str, Any] = {"home": home, "name": name}
    for key in _DOORBELL_STR_KEYS:
        val = typed.get(key)
        if val is not None:
            if not isinstance(val, str) or not val:
                raise ConfigError(f"doorbells[{index}] '{key}' must be a non-empty string")
            out[key] = val
    ring_val = typed.get("ring")
    if ring_val is not None:
        if not isinstance(ring_val, str) or ring_val not in _RING_VALUES:
            want = ", ".join(sorted(_RING_VALUES))
            raise ConfigError(f"doorbells[{index}] 'ring' must be one of: {want}")
        out["ring"] = ring_val
    for key in _DOORBELL_PATH_KEYS:
        val = typed.get(key)
        if val is not None:
            out[key] = _expand(val, what=f"doorbells[{index}] '{key}'")
    for key in _DOORBELL_BOOL_KEYS:
        val = typed.get(key)
        if val is not None:
            if not isinstance(val, bool):
                raise ConfigError(f"doorbells[{index}] '{key}' must be a bool")
            out[key] = val
    rules_val = typed.get("rules")
    if rules_val is not None:
        if not isinstance(rules_val, dict):
            raise ConfigError(f"doorbells[{index}] 'rules' must be an object")
        rules_in: dict[Any, Any] = rules_val
        for rkey, rval in rules_in.items():
            if rkey not in _RULES_KEYS:
                raise ConfigError(f"doorbells[{index}] 'rules' has unknown key {rkey!r}")
            if not isinstance(rval, str) or rval not in _RULES_VALUES:
                raise ConfigError(
                    f"doorbells[{index}] 'rules[{rkey}]' must be one of: "
                    "allow, approve, prohibit"
                )
        out["rules"] = dict(rules_in)
    resume_min = typed.get("resume_min_interval")
    if resume_min is not None:
        if isinstance(resume_min, bool) or not isinstance(resume_min, (int, float)):
            raise ConfigError(f"doorbells[{index}] 'resume_min_interval' must be a number")
        if float(resume_min) < 0:
            raise ConfigError(f"doorbells[{index}] 'resume_min_interval' must be >= 0")
        out["resume_min_interval"] = float(resume_min)
    max_resumes = typed.get("max_resumes_per_day")
    if max_resumes is not None:
        if isinstance(max_resumes, bool) or not isinstance(max_resumes, int):
            raise ConfigError(f"doorbells[{index}] 'max_resumes_per_day' must be an int")
        if max_resumes < 0:
            raise ConfigError(f"doorbells[{index}] 'max_resumes_per_day' must be >= 0")
        out["max_resumes_per_day"] = max_resumes
    sandbox = typed.get("resume_max_sandbox")
    if sandbox is not None:
        if not isinstance(sandbox, str) or sandbox not in _SANDBOX_MODES:
            want = ", ".join(sorted(_SANDBOX_MODES))
            raise ConfigError(f"doorbells[{index}] 'resume_max_sandbox' must be one of: {want}")
        out["resume_max_sandbox"] = sandbox
    muse_args = typed.get("muse_args")
    if muse_args is not None:
        if not isinstance(muse_args, list) or not all(
            isinstance(item, str) and item for item in muse_args
        ):
            raise ConfigError(
                f"doorbells[{index}] 'muse_args' must be a list of non-empty strings"
            )
        out["muse_args"] = list(muse_args)
    types = typed.get("types")
    if types is not None:
        if not isinstance(types, list) or not all(isinstance(t, str) and t for t in types):
            raise ConfigError(f"doorbells[{index}] 'types' must be a list of strings")
        out["types"] = list(types)
    min_interval = typed.get("min_interval")
    if min_interval is not None:
        if isinstance(min_interval, bool) or not isinstance(min_interval, (int, float)):
            raise ConfigError(f"doorbells[{index}] 'min_interval' must be a number")
        out["min_interval"] = float(min_interval)
    intro = typed.get("intro")
    if intro is not None:
        if not isinstance(intro, bool):
            raise ConfigError(f"doorbells[{index}] 'intro' must be a bool")
        out["intro"] = intro
    poll = typed.get("poll")
    if poll is not None:
        if isinstance(poll, bool) or not isinstance(poll, (int, float)):
            raise ConfigError(f"doorbells[{index}] 'poll' must be a number")
        out["poll"] = float(poll)
    return out


def _load_bridge(raw: Any, index: int) -> dict[str, Any]:
    """Validate and normalise one entry of the ``bridges`` list."""
    if not isinstance(raw, dict):
        raise ConfigError(f"bridges[{index}] must be an object")
    typed: dict[str, Any] = raw
    allowed = {"home", "name", "identity", "peers", "session", "interval"}
    unknown = sorted(set(typed) - allowed)
    if unknown:
        msg = f"bridges[{index}] has unknown keys: {', '.join(unknown)}"
        raise ConfigError(msg)
    for key in ("home", "name", "identity", "peers", "session"):
        if key not in typed:
            raise ConfigError(f"bridges[{index}] requires '{key}'")
    home = _expand(typed["home"], what=f"bridges[{index}] 'home'")
    identity = _expand(typed["identity"], what=f"bridges[{index}] 'identity'")
    name = typed["name"]
    if not isinstance(name, str) or not name:
        raise ConfigError(f"bridges[{index}] 'name' must be a non-empty string")
    session = typed["session"]
    if not isinstance(session, str) or not session:
        raise ConfigError(f"bridges[{index}] 'session' must be a non-empty string")
    peers_raw = typed["peers"]
    if (
        not isinstance(peers_raw, list)
        or not peers_raw
        or not all(isinstance(p, str) and p for p in peers_raw)
    ):
        raise ConfigError(
            f"bridges[{index}] 'peers' must be a non-empty list of non-empty strings"
        )
    peers = list(peers_raw)
    out: dict[str, Any] = {
        "home": home,
        "name": name,
        "identity": identity,
        "peers": peers,
        "session": session,
    }
    if "interval" in typed:
        interval_raw = typed["interval"]
        if isinstance(interval_raw, bool) or not isinstance(interval_raw, (int, float)):
            raise ConfigError(f"bridges[{index}] 'interval' must be a number > 0")
        if float(interval_raw) <= 0:
            raise ConfigError(f"bridges[{index}] 'interval' must be a number > 0")
        out["interval"] = interval_raw
    return out


def _load_prune(raw: Any) -> dict[str, Any]:
    """Validate and normalise the optional top-level ``prune`` object."""
    if not isinstance(raw, dict):
        raise ConfigError("prune must be an object")
    typed: dict[str, Any] = raw
    allowed = {"every_hours", "keep_days", "keep_last", "archive"}
    unknown = sorted(set(typed) - allowed)
    if unknown:
        msg = f"prune has unknown keys: {', '.join(unknown)}"
        raise ConfigError(msg)
    every_raw: Any = typed.get("every_hours", 24)
    if isinstance(every_raw, bool) or not isinstance(every_raw, (int, float)):
        raise ConfigError("prune 'every_hours' must be a number > 0")
    if not math.isfinite(float(every_raw)) or float(every_raw) <= 0:
        raise ConfigError("prune 'every_hours' must be a number > 0")
    keep_days_raw: Any = typed.get("keep_days", 30)
    if isinstance(keep_days_raw, bool) or not isinstance(keep_days_raw, (int, float)):
        raise ConfigError("prune 'keep_days' must be a number >= 0")
    if not math.isfinite(float(keep_days_raw)) or float(keep_days_raw) < 0:
        raise ConfigError("prune 'keep_days' must be a number >= 0")
    keep_last_raw: Any = typed.get("keep_last", 500)
    if isinstance(keep_last_raw, bool) or not isinstance(keep_last_raw, int):
        raise ConfigError("prune 'keep_last' must be an int >= 0")
    if keep_last_raw < 0:
        raise ConfigError("prune 'keep_last' must be an int >= 0")
    archive_raw: Any = typed.get("archive", True)
    if not isinstance(archive_raw, bool):
        raise ConfigError("prune 'archive' must be a bool")
    return {
        "every_hours": every_raw,
        "keep_days": keep_days_raw,
        "keep_last": keep_last_raw,
        "archive": archive_raw,
    }


def load_config(path: str) -> dict[str, Any]:
    """Load and validate the ``up`` config file at ``path``.

    Unknown keys anywhere are rejected, every path gets ``~`` expanded, and
    ``relay.log``/``relay.server`` fall back to ``<db dir>/relay.log`` and a
    PATH lookup respectively. Raises :class:`ConfigError` on any problem.
    """
    expanded = os.path.expanduser(path)
    try:
        with open(expanded, encoding="utf-8") as fh:
            raw: Any = json.load(fh)
    except OSError as exc:
        msg = f"cannot read config {expanded}: {exc}"
        raise ConfigError(msg) from exc
    except ValueError as exc:
        msg = f"invalid JSON in config {expanded}: {exc}"
        raise ConfigError(msg) from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"config {expanded} must contain a JSON object")
    typed: dict[str, Any] = raw
    allowed = {"relay", "owners", "doorbells", "doorbells_from_registry", "bridges", "prune"}
    unknown = sorted(set(typed) - allowed)
    if unknown:
        msg = f"config has unknown keys: {', '.join(unknown)}"
        raise ConfigError(msg)
    owners_raw = typed.get("owners", [])
    if not isinstance(owners_raw, list):
        raise ConfigError("owners must be a list")
    owners = [_load_owner(item, i) for i, item in enumerate(owners_raw)]
    doorbells_raw = typed.get("doorbells", [])
    if not isinstance(doorbells_raw, list):
        raise ConfigError("doorbells must be a list")
    doorbells = [_load_doorbell(item, i) for i, item in enumerate(doorbells_raw)]
    bridges_raw = typed.get("bridges", [])
    if not isinstance(bridges_raw, list):
        raise ConfigError("bridges must be a list")
    bridges = [_load_bridge(item, i) for i, item in enumerate(bridges_raw)]
    cfg: dict[str, Any] = {"owners": owners}
    if doorbells:
        cfg["doorbells"] = doorbells
    if bridges:
        cfg["bridges"] = bridges
    relay = _load_relay(typed.get("relay"))
    if relay:
        cfg["relay"] = relay
    if "doorbells_from_registry" in typed:
        flag = typed["doorbells_from_registry"]
        if not isinstance(flag, bool):
            raise ConfigError("doorbells_from_registry must be a bool")
        if flag is True:
            cfg["doorbells_from_registry"] = True
    if "prune" in typed:
        cfg["prune"] = _load_prune(typed["prune"])
    return cfg


def _config_path(explicit: str | None) -> str:
    """Resolve ``--config``, else ``$PHONING_UP_CONFIG``, else the default path."""
    if explicit:
        return explicit
    override = os.environ.get("PHONING_UP_CONFIG")
    if override:
        return override
    return DEFAULT_CONFIG


# -- probing -----------------------------------------------------------------


def path_holder(path: str) -> dict[str, Any] | None:
    """Return the holder info while the lock file at ``path`` is held.

    Opens ``path`` read-only and tries a non-blocking shared ``flock``.
    ``BlockingIOError`` means the exclusive lock is held: the parsed JSON body
    is returned (``{}`` when unparseable). If the shared lock was acquired it
    is released at once and ``None`` is returned. A missing file also returns
    ``None``. The file is never created, written or appended to.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return None
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return _read_holder(fd)
        except OSError:
            return None
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)


def lock_holder(user_dir: str, lock_name: str) -> dict[str, Any] | None:
    """Return the holder info while ``<user_dir>/mailbox/<lock_name>`` is held.

    Opens the lock file read-only and tries a non-blocking shared ``flock``.
    ``BlockingIOError`` means the exclusive lock is held: the parsed JSON body
    is returned (``{}`` when unparseable). If the shared lock was acquired it
    is released at once and ``None`` is returned. A missing file also returns
    ``None``. The file is never written and ``audit.log`` is never appended to.
    """
    path = os.path.join(user_dir, "mailbox", lock_name)
    return path_holder(path)


def owner_holder(user_dir: str) -> dict[str, Any] | None:
    """Return the holder info while the owner lock is held, else ``None``.

    A thin wrapper over :func:`lock_holder` for ``owner.lock``; see that
    function for the exact probing semantics.
    """
    return lock_holder(user_dir, _OWNER_LOCK)


def _read_holder(fd: int) -> dict[str, Any]:
    """Parse the JSON body of the (open, read-only) lock ``fd``."""
    try:
        raw = os.pread(fd, 65536, 0)
    except OSError:
        return {}
    try:
        obj: Any = json.loads(raw.decode("utf-8", errors="replace") or "{}")
    except ValueError:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    return {}


def _relay_address(url: str) -> tuple[str, int]:
    """Return ``(host, port)`` from a relay URL, defaulting the port by scheme."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if not host:
        msg = f"relay url has no host: {url!r}"
        raise ConfigError(msg)
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return host, int(port)


def relay_up(url: str, timeout: float = 2.0) -> bool:
    """True when a TCP connect to the relay's host:port succeeds.

    The relay answers 404 on ``/``, so any successful connect counts as up.
    """
    try:
        host, port = _relay_address(url)
    except ConfigError:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# -- spawning ----------------------------------------------------------------


def _find_binary(name: str) -> str | None:
    """Locate ``name`` on PATH, else ``~/.local/bin/<name>`` when executable."""
    found = shutil.which(name)
    if found:
        return found
    fallback = os.path.join(os.path.expanduser("~"), ".local", "bin", name)
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


def _open_private_log(path: str) -> int:
    """Open ``path`` append-only 0600 (creating it as 0600) and return the fd."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return fd


def _spawn(
    argv: Sequence[str],
    *,
    env: dict[str, str],
    cwd: str,
    log_path: str,
) -> subprocess.Popen[Any]:
    """Start ``argv`` detached with its output appended to ``log_path``.

    Returns the :class:`subprocess.Popen` object so callers can poll it via
    :meth:`Popen.poll` and read :attr:`Popen.returncode`.
    """
    fd = _open_private_log(log_path)
    try:
        devnull = os.open(os.devnull, os.O_RDONLY)
        try:
            proc: subprocess.Popen[Any] = subprocess.Popen(  # noqa: S603
                list(argv),
                stdin=devnull,
                stdout=fd,
                stderr=fd,
                cwd=cwd,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(devnull)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    return proc


def _poll_relay(url: str, proc: subprocess.Popen[Any], deadline: float) -> bool:
    """Poll ``relay_up`` until ``deadline``; stop early if the child exits."""
    while True:
        if relay_up(url):
            return True
        if proc.poll() is not None:
            return relay_up(url)
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


def _poll_lock(
    user_dir: str, lock_name: str, proc: subprocess.Popen[Any], deadline: float
) -> bool:
    """Poll the lock holder until its pid matches ``proc.pid``.

    Stops as soon as the child has exited: a child that dies before taking
    the lock can never satisfy the condition.
    """
    while True:
        holder = lock_holder(user_dir, lock_name)
        if holder is not None and _pid_of(holder) == proc.pid:
            return True
        if proc.poll() is not None:
            holder = lock_holder(user_dir, lock_name)
            return holder is not None and _pid_of(holder) == proc.pid
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


def _poll_path(path: str, proc: subprocess.Popen[Any], deadline: float) -> bool:
    """Poll the lock at ``path`` until its holder pid matches ``proc.pid``.

    Stops as soon as the child has exited: a child that dies before taking
    the lock can never satisfy the condition.
    """
    while True:
        holder = path_holder(path)
        if holder is not None and _pid_of(holder) == proc.pid:
            return True
        if proc.poll() is not None:
            holder = path_holder(path)
            return holder is not None and _pid_of(holder) == proc.pid
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL)


def _poll_owner(user_dir: str, proc: subprocess.Popen[Any], deadline: float) -> bool:
    """Poll the owner lock until the holder pid matches ``proc.pid``."""
    return _poll_lock(user_dir, _OWNER_LOCK, proc, deadline)


def _poll_doorbell(user_dir: str, proc: subprocess.Popen[Any], deadline: float) -> bool:
    """Poll the doorbell lock until the holder pid matches ``proc.pid``."""
    return _poll_lock(user_dir, _DOORBELL_LOCK, proc, deadline)


def _pid_of(holder: dict[str, Any]) -> int | None:
    """Extract the integer ``pid`` from a lock holder dict, if any."""
    val = holder.get("pid")
    if isinstance(val, bool) or not isinstance(val, int):
        return None
    return val


def _owner_env(home: str) -> dict[str, str]:
    """``os.environ`` plus ``PHONING_HOME`` and a PATH that finds ``retalk``."""
    env = dict(os.environ)
    env["PHONING_HOME"] = home
    retalk = _find_binary("retalk")
    if retalk:
        prefix = os.path.dirname(retalk)
        existing = env.get("PATH", "")
        env["PATH"] = prefix + (os.pathsep + existing if existing else "")
    return env


# -- reconciliation ----------------------------------------------------------


def _reconcile_relay(
    relay: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """One relay item: up / would-start / started / failed / unmanaged."""
    url: str = relay["url"]
    item: dict[str, Any] = {"kind": "relay", "url": url}
    if relay_up(url):
        item["state"] = "up"
        return item
    host, port = _relay_address(url)
    if host not in _LOOPBACK_HOSTS:
        item["state"] = "unmanaged"
        return item
    if dry_run:
        item["state"] = "would-start"
        return item
    db: str = relay["db"]
    log: str = relay["log"]
    server = relay.get("server") or _find_binary("retalk-server")
    if not server or not os.path.isfile(server):
        item["state"] = "failed"
        item["error"] = "retalk-server not found; see docs/SPEC-v0.2.md"
        return item
    argv = [server, "--host", host, "--port", str(port), "--audience", url, "--db", db]
    env = dict(os.environ)
    env["RETALK_SERVER_DB"] = db
    db_dir = os.path.dirname(db)
    cwd = db_dir if db_dir else os.getcwd()
    try:
        os.makedirs(cwd, mode=0o700, exist_ok=True)
    except OSError as exc:
        item["state"] = "failed"
        item["error"] = f"cannot create relay db dir: {exc}; see {log}"
        return item
    try:
        proc = _spawn(argv, env=env, cwd=cwd, log_path=log)
    except OSError as exc:
        item["state"] = "failed"
        item["error"] = f"cannot start retalk-server: {exc}; see {log}"
        return item
    if _poll_relay(url, proc, time.monotonic() + START_TIMEOUT):
        item["state"] = "started"
        item["pid"] = proc.pid
    else:
        item["state"] = "failed"
        item["error"] = f"relay did not come up within {int(START_TIMEOUT)}s; see {log}"
    return item


def _reconcile_owner(
    owner: dict[str, Any],
    *,
    dry_run: bool,
    python: str,
) -> dict[str, Any]:
    """One owner item: no-identity / running / would-start / started / failed."""
    home: str = owner["home"]
    name: str = owner["name"]
    session: str = owner["session"]
    interval: int = owner["interval"]
    user_dir = os.path.join(home, name)
    item: dict[str, Any] = {"kind": "owner", "name": name, "home": home}
    if not os.path.isdir(os.path.join(user_dir, "identity")):
        item["state"] = "no-identity"
        return item
    holder = owner_holder(user_dir)
    if holder is not None:
        item["state"] = "running"
        pid = _pid_of(holder)
        if pid is not None:
            item["pid"] = pid
        return item
    if dry_run:
        item["state"] = "would-start"
        return item
    log = os.path.join(user_dir, "owner.log")
    argv = [
        python,
        "-m",
        "phoning.cli",
        "own",
        name,
        "--session",
        session,
        "--interval",
        str(interval),
    ]
    try:
        proc = _spawn(argv, env=_owner_env(home), cwd=user_dir, log_path=log)
    except OSError as exc:
        item["state"] = "failed"
        item["error"] = f"cannot start owner: {exc}; see {log}"
        return item
    if _poll_owner(user_dir, proc, time.monotonic() + START_TIMEOUT):
        item["state"] = "started"
        item["pid"] = proc.pid
        return item
    rc = proc.poll()
    if rc is not None:
        item["state"] = "failed"
        item["error"] = f"owner exited rc={rc}; see {log}"
        return item
    item["state"] = "failed"
    item["error"] = (
        f"owner did not take the lock within {int(START_TIMEOUT)}s; see {log}"
    )
    return item


def _doorbell_argv(python: str, bell: dict[str, Any]) -> list[str]:
    """Map a doorbell config entry to its ``phoning doorbell`` spawn command."""
    argv = [python, "-m", "phoning.cli", "doorbell", str(bell["name"])]
    for key, flag in _DOORBELL_FLAGS:
        if key in bell:
            argv += [flag, str(bell[key])]
    types = bell.get("types")
    if isinstance(types, list) and types:
        argv += ["--types", ",".join(str(t) for t in types)]
    if bell.get("intro") is True:
        argv.append("--intro")
    if "rules" in bell:
        argv += ["--rules", json.dumps(bell["rules"], sort_keys=True, separators=(",", ":"))]
    if "rules_file" in bell:
        argv += ["--rules-file", str(bell["rules_file"])]
    if "resume_min_interval" in bell:
        argv += ["--resume-min-interval", str(float(bell["resume_min_interval"]))]
    if "max_resumes_per_day" in bell:
        argv += ["--max-resumes-per-day", str(bell["max_resumes_per_day"])]
    if "resume_max_sandbox" in bell:
        argv += ["--resume-max-sandbox", str(bell["resume_max_sandbox"])]
    if "notify_cmd" in bell:
        argv += ["--notify-cmd", str(bell["notify_cmd"])]
    if "codex_bin" in bell:
        argv += ["--codex-bin", str(bell["codex_bin"])]
    if bell.get("resume_dead") is True:
        argv.append("--resume-dead")
    if "claude_session" in bell:
        argv += ["--claude-session", str(bell["claude_session"])]
    if "claude_bin" in bell:
        argv += ["--claude-bin", str(bell["claude_bin"])]
    if "claude_home" in bell:
        argv += ["--claude-home", str(bell["claude_home"])]
    if "claude_token_file" in bell:
        argv += ["--claude-token-file", str(bell["claude_token_file"])]
    if "muse_session" in bell:
        argv += ["--muse-session", str(bell["muse_session"])]
    if "muse_bin" in bell:
        argv += ["--muse-bin", str(bell["muse_bin"])]
    if "muse_home" in bell:
        argv += ["--muse-home", str(bell["muse_home"])]
    if bell.get("muse_resume") is False:
        argv.append("--muse-no-resume")
    muse_args = bell.get("muse_args")
    if isinstance(muse_args, list):
        for item in muse_args:
            argv.append(f"--muse-arg={item}")
    if "kimi_session" in bell:
        argv += ["--kimi-session", str(bell["kimi_session"])]
    if "kimi_cwd" in bell:
        argv += ["--kimi-cwd", str(bell["kimi_cwd"])]
    if "openclaw_session" in bell:
        argv += ["--openclaw-session", str(bell["openclaw_session"])]
    if "openclaw_agent" in bell:
        argv += ["--openclaw-agent", str(bell["openclaw_agent"])]
    return argv


def _reconcile_doorbell(
    bell: dict[str, Any],
    *,
    dry_run: bool,
    python: str,
) -> dict[str, Any]:
    """One doorbell item: no-mailbox / running / would-start / started / failed."""
    home: str = bell["home"]
    name: str = bell["name"]
    user_dir = os.path.join(home, name)
    item: dict[str, Any] = {"kind": "doorbell", "name": name, "home": home}
    if not os.path.exists(os.path.join(user_dir, "mailbox", "log.jsonl")):
        item["state"] = "no-mailbox"
        return item
    holder = lock_holder(user_dir, _DOORBELL_LOCK)
    if holder is not None:
        item["state"] = "running"
        pid = _pid_of(holder)
        if pid is not None:
            item["pid"] = pid
        return item
    if dry_run:
        item["state"] = "would-start"
        return item
    log = os.path.join(user_dir, "doorbell.log")
    argv = _doorbell_argv(python, bell)
    try:
        proc = _spawn(argv, env=_owner_env(home), cwd=user_dir, log_path=log)
    except OSError as exc:
        item["state"] = "failed"
        item["error"] = f"cannot start doorbell: {exc}; see {log}"
        return item
    if _poll_doorbell(user_dir, proc, time.monotonic() + START_TIMEOUT):
        item["state"] = "started"
        item["pid"] = proc.pid
        return item
    rc = proc.poll()
    if rc is not None:
        item["state"] = "failed"
        item["error"] = f"doorbell exited rc={rc}; see {log}"
        return item
    item["state"] = "failed"
    item["error"] = (
        f"doorbell did not take the lock within {int(START_TIMEOUT)}s; see {log}"
    )
    return item


def _reconcile_bridge(
    bridge: dict[str, Any],
    *,
    dry_run: bool,
    python: str,
) -> dict[str, Any]:
    """One bridge item: no-mailbox / no-identity / running / would-start / started / failed."""
    home: str = bridge["home"]
    name: str = bridge["name"]
    identity: str = bridge["identity"]
    session: str = bridge["session"]
    peers_any: Any = bridge.get("peers")
    peers: list[Any] = list(peers_any) if isinstance(peers_any, list) else []
    user_dir = os.path.join(home, name)
    item: dict[str, Any] = {"kind": "bridge", "name": name, "home": home}
    if not os.path.isfile(os.path.join(user_dir, "mailbox", "log.jsonl")):
        item["state"] = "no-mailbox"
        return item
    if not os.path.isdir(identity):
        item["state"] = "no-identity"
        return item
    lock_path = bridge_mod._lock_path(os.path.dirname(os.path.abspath(identity)))
    holder = path_holder(lock_path)
    if holder is not None:
        item["state"] = "running"
        pid = _pid_of(holder)
        if pid is not None:
            item["pid"] = pid
        return item
    if dry_run:
        item["state"] = "would-start"
        return item
    log = os.path.join(user_dir, "bridge.log")
    argv: list[str] = [
        python,
        "-m",
        "phoning.cli",
        "bridge",
        name,
        "--from-identity",
        identity,
    ]
    for peer in peers:
        argv += ["--peer", str(peer)]
    argv += ["--session", session, "--home", home]
    if "interval" in bridge:
        argv += ["--interval", str(bridge["interval"])]
    try:
        proc = _spawn(argv, env=_owner_env(home), cwd=user_dir, log_path=log)
    except OSError as exc:
        item["state"] = "failed"
        item["error"] = f"cannot start bridge: {exc}; see {log}"
        return item
    if _poll_path(lock_path, proc, time.monotonic() + BRIDGE_START_TIMEOUT):
        item["state"] = "started"
        item["pid"] = proc.pid
        return item
    rc = proc.poll()
    if rc is not None:
        if rc == 3:
            item["state"] = "running"
            seen = path_holder(lock_path)
            if seen is not None:
                pid = _pid_of(seen)
                if pid is not None:
                    item["pid"] = pid
            return item
        item["state"] = "failed"
        item["error"] = f"bridge exited rc={rc}; see {log}"
        return item
    item["state"] = "failed"
    item["error"] = (
        f"bridge did not take the lock within {int(BRIDGE_START_TIMEOUT)}s; see {log}"
    )
    return item


def _explicit_doorbell_keys(doorbells: Any) -> set[tuple[str, str]]:
    """Keys of the explicit doorbells as ``(abspath(home), name)`` pairs."""
    keys: set[tuple[str, str]] = set()
    if not isinstance(doorbells, list):
        return keys
    for bell in doorbells:
        if not isinstance(bell, dict):
            continue
        home_any = bell.get("home")
        name_any = bell.get("name")
        if isinstance(home_any, str) and home_any and isinstance(name_any, str) and name_any:
            keys.add((os.path.abspath(home_any), name_any))
    return keys


def _registry_bound_at(reg: dict[str, Any], session_id: str) -> str:
    """The ``bound_at`` string for ``session_id``, else the empty string."""
    entry = reg.get(session_id)
    if isinstance(entry, dict):
        bound = entry.get("bound_at")
        if isinstance(bound, str):
            return bound
    return ""


def _registry_bell(home: str, name: str, target: dict[str, Any]) -> dict[str, Any]:
    """Build the minimal doorbell dict for one ready registry target."""
    bell: dict[str, Any] = {"home": home, "name": name, "ring": target["ring"]}
    hook_reader = target.get("hook_reader")
    if isinstance(hook_reader, str) and hook_reader:
        bell["hook_reader"] = hook_reader
    for key in _THREAD_KEYS:
        if key in target:
            bell[key] = target[key]
    return bell


def _registry_items(
    explicit_keys: set[tuple[str, str]],
    *,
    dry_run: bool,
    python: str,
) -> list[dict[str, Any]]:
    """Reconcile one doorbell per ready registry entry.

    Candidates whose ``(home, name)`` matches an explicit entry are dropped
    silently; the rest are deduped by key with the newest ``(bound_at,
    session_id)`` winning. Read-only: the registry is never written.
    """
    try:
        reg = hooks._load_registry()
        targets = hooks._targets(reg)
    except Exception:
        return []
    if not isinstance(reg, dict) or not isinstance(targets, list):
        return []
    grouped: dict[tuple[str, str], list[tuple[str, str, dict[str, Any]]]] = {}
    for target in targets:
        if not isinstance(target, dict):
            continue
        if target.get("ring") is None:
            continue
        name_any = target.get("name")
        home_any = target.get("home")
        sid_any = target.get("session_id")
        if not isinstance(name_any, str) or not name_any:
            continue
        if not isinstance(home_any, str) or not home_any:
            continue
        if not isinstance(sid_any, str) or not sid_any:
            continue
        norm_home = os.path.abspath(os.path.expanduser(home_any))
        key = (norm_home, name_any)
        if key in explicit_keys:
            continue
        bound = _registry_bound_at(reg, sid_any)
        grouped.setdefault(key, []).append((bound, sid_any, target))
    items: list[dict[str, Any]] = []
    for key in sorted(grouped, key=lambda k: (k[1], k[0])):
        cands = grouped[key]
        _bound, winner_sid, winner_target = max(cands, key=lambda c: (c[0], c[1]))
        bell = _registry_bell(key[0], key[1], winner_target)
        item = _reconcile_doorbell(bell, dry_run=dry_run, python=python)
        item["source"] = "registry"
        items.append(item)
        if len(cands) > 1:
            others = sorted(sid for _, sid, _ in cands if sid != winner_sid)
            items.append(
                {
                    "kind": "doorbell",
                    "name": key[1],
                    "home": key[0],
                    "state": "skip: ambiguous",
                    "source": "registry",
                    "session": winner_sid,
                    "others": others,
                }
            )
    return items


def _prune_is_due(state_path: str, every_hours: Any, now: float) -> bool:
    """True when the prune state file says a run is due.

    Due means the file is missing, unreadable, not valid JSON, not an
    object, ``last_run`` is not a finite real number, ``last_run`` is in
    the future, or ``now - last_run >= every_hours * 3600``.
    """
    try:
        interval = float(every_hours)
    except (TypeError, ValueError):
        return True
    if not math.isfinite(interval) or interval <= 0:
        return True
    try:
        with open(state_path, encoding="utf-8") as fh:
            raw: Any = json.load(fh)
    except (OSError, ValueError):
        return True
    if not isinstance(raw, dict):
        return True
    last: Any = raw.get("last_run")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        return True
    last_f = float(last)
    if not math.isfinite(last_f):
        return True
    if last_f > now:
        return True
    return (now - last_f) >= interval * 3600.0


def _write_prune_state(state_path: str, now: float) -> None:
    """Atomically write ``{"last_run": now}`` to ``state_path`` mode 0600."""
    parent = os.path.dirname(state_path) or "."
    payload = json.dumps({"last_run": now}).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".prune-state-", suffix=".tmp")
    try:
        os.chmod(tmp, 0o600)
        offset = 0
        while offset < len(payload):
            n = os.write(fd, payload[offset:])
            if n <= 0:
                msg = "short write"
                raise OSError(msg)
            offset += n
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, 0o600)
        os.replace(tmp, state_path)
        os.chmod(state_path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _prune_rc(value: Any) -> int:
    """Normalise the ``retention.main`` return value to an exit code."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _reconcile_prune(
    owner: dict[str, Any],
    prune_cfg: dict[str, Any],
    *,
    dry_run: bool,
) -> dict[str, Any]:
    """One prune item per owner: no-mailbox / not-due / would-prune / pruned / clean / failed."""
    home_any: Any = owner.get("home")
    name_any: Any = owner.get("name")
    home = str(home_any) if isinstance(home_any, str) else ""
    name = str(name_any) if isinstance(name_any, str) else ""
    item: dict[str, Any] = {"kind": "prune", "name": name, "home": home}
    log_path = os.path.join(home, name, "mailbox", "log.jsonl")
    if not os.path.exists(log_path):
        item["state"] = "no-mailbox"
        return item
    every: Any = prune_cfg.get("every_hours", 24)
    keep_days: Any = prune_cfg.get("keep_days", 30)
    keep_last: Any = prune_cfg.get("keep_last", 500)
    archive: Any = prune_cfg.get("archive", True)
    now = time.time()
    state_path = os.path.join(home, name, "prune-state.json")
    if not _prune_is_due(state_path, every, now):
        item["state"] = "not-due"
        return item
    if dry_run:
        item["state"] = "would-prune"
        return item
    argv = [
        name,
        "--home",
        home,
        "--keep-days",
        str(keep_days),
        "--keep-last",
        str(keep_last),
        "--archive" if archive is True else "--no-archive",
        "--json",
    ]
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
            rc_any: Any = retention.main(argv)
    except SystemExit as exc:
        code: Any = exc.code
        text = "" if code is None else str(code)
        first = text.splitlines()[0] if text else ""
        if first:
            item["state"] = "failed"
            item["error"] = f"prune failed: SystemExit: {first}"
        else:
            item["state"] = "failed"
            item["error"] = "prune failed: SystemExit"
        return item
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        first = text.splitlines()[0] if text else ""
        cls = type(exc).__name__
        item["state"] = "failed"
        if first:
            item["error"] = f"prune failed: {cls}: {first}"
        else:
            item["error"] = f"prune failed: {cls}"
        return item
    rc = _prune_rc(rc_any)
    if rc != 0:
        item["state"] = "failed"
        item["error"] = f"prune failed: rc={rc}"
        return item
    pruned_int: int | None = None
    try:
        report: Any = json.loads(out_buf.getvalue() or "{}")
    except ValueError:
        report = None
    if isinstance(report, dict):
        maybe_pruned: Any = report.get("pruned")
        maybe_total: Any = report.get("total")
        if isinstance(maybe_pruned, int) and not isinstance(maybe_pruned, bool):
            pruned_int = maybe_pruned
            item["pruned"] = maybe_pruned
        if isinstance(maybe_total, int) and not isinstance(maybe_total, bool):
            item["total"] = maybe_total
    if pruned_int is not None and pruned_int > 0:
        item["state"] = "pruned"
    else:
        item["state"] = "clean"
    try:
        _write_prune_state(state_path, time.time())
    except OSError:
        item["warning"] = "cannot write prune-state.json"
    return item


def reconcile(
    cfg: dict[str, Any],
    *,
    dry_run: bool = False,
    python: str = sys.executable,
) -> list[dict[str, Any]]:
    """Start whatever is missing; one result item per relay, owner and doorbell.

    Items come back in config order (relay first, then owners, then
    doorbells, then bridges). Existing processes are never touched: ``up``
    only starts what is missing.
    """
    items: list[dict[str, Any]] = []
    relay = cfg.get("relay")
    if isinstance(relay, dict) and relay:
        items.append(_reconcile_relay(relay, dry_run=dry_run))
    owners = cfg.get("owners")
    if isinstance(owners, list):
        for owner in owners:
            if isinstance(owner, dict):
                items.append(_reconcile_owner(owner, dry_run=dry_run, python=python))
    doorbells = cfg.get("doorbells")
    if isinstance(doorbells, list):
        for bell in doorbells:
            if isinstance(bell, dict):
                items.append(_reconcile_doorbell(bell, dry_run=dry_run, python=python))
    if cfg.get("doorbells_from_registry") is True:
        explicit_keys = _explicit_doorbell_keys(doorbells)
        items.extend(_registry_items(explicit_keys, dry_run=dry_run, python=python))
    bridges = cfg.get("bridges")
    if isinstance(bridges, list):
        for entry in bridges:
            if isinstance(entry, dict):
                items.append(_reconcile_bridge(entry, dry_run=dry_run, python=python))
    prune_cfg = cfg.get("prune")
    if isinstance(prune_cfg, dict) and prune_cfg and isinstance(owners, list):
        for owner in owners:
            if isinstance(owner, dict):
                items.append(_reconcile_prune(owner, prune_cfg, dry_run=dry_run))
    return items


# -- launch agent ------------------------------------------------------------


def launchagent_plist(
    *,
    program: list[str],
    interval: int = 120,
    log_path: str,
    path_env: str,
    label: str = DEFAULT_LABEL,
    extra_env: dict[str, str] | None = None,
) -> bytes:
    """Build the launchd plist bytes for the ``phoning up`` job.

    ``AbandonProcessGroup`` keeps the daemons it starts alive after the job
    itself exits. The plist is only ever returned; nothing is installed.
    """
    environment: dict[str, str] = {"PATH": path_env}
    if extra_env:
        environment.update(extra_env)
    payload: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": list(program),
        "RunAtLoad": True,
        "StartInterval": int(interval),
        "AbandonProcessGroup": True,
        "ProcessType": "Background",
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": environment,
    }
    return plistlib.dumps(payload)


def _launchagent_path_env() -> str:
    """PATH for the LaunchAgent: tool dirs first, then the standard set."""
    dirs: list[str] = []
    for name in ("retalk", "retalk-server"):
        found = _find_binary(name)
        if found:
            d = os.path.dirname(found)
            if d not in dirs:
                dirs.append(d)
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    seen: list[str] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return os.pathsep.join(seen)


# -- CLI ---------------------------------------------------------------------


def _format_item(item: dict[str, Any]) -> str:
    """One human-readable line for an item."""
    kind = item.get("kind")
    state = str(item.get("state", "?"))
    if state.startswith("skip"):
        line = f"{kind} {item.get('name')}@{item.get('home')} {state}"
        sess = item.get("session")
        others = item.get("others")
        if isinstance(sess, str) and isinstance(others, list):
            names = [str(o) for o in others if isinstance(o, str)]
            if names:
                line += f" (kept {sess}; ignored {', '.join(names)})"
            else:
                line += f" (kept {sess})"
        return line
    if kind == "relay":
        line = f"relay {item.get('url')} {state}"
        if "pid" in item:
            line += f" pid {item['pid']}"
        if "error" in item:
            line += f" ({item['error']})"
        return line
    line = f"{kind} {item.get('name')}@{item.get('home')} {state}"
    if "pid" in item:
        line += f" pid {item['pid']}"
    if "error" in item:
        line += f" ({item['error']})"
    return line


def _build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``phoning up``."""
    parser = argparse.ArgumentParser(prog="phoning up", description=__doc__.splitlines()[0])
    parser.add_argument("--config", default=None, help="path to up.json")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="report only")
    parser.add_argument("--json", dest="as_json", action="store_true", help="JSON per line")
    parser.add_argument(
        "--print-launchagent",
        dest="print_launchagent",
        action="store_true",
        help="print the LaunchAgent plist and exit",
    )
    parser.add_argument("--interval", type=int, default=120, help="StartInterval for the plist")
    parser.add_argument(
        "--quiet",
        dest="quiet",
        action="store_true",
        help="print only started, failed or unmanaged items",
    )
    return parser


def _print_config_error(path: str, exc: ConfigError) -> None:
    """Write the exit-2 diagnostic: the path, the reason, a minimal example."""
    print(f"error: {exc}", file=sys.stderr)
    print(f"config path: {os.path.expanduser(path)}", file=sys.stderr)
    print("example:", file=sys.stderr)
    print(_EXAMPLE, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``phoning up``; see docs/SPEC-v0.2.md §1, §7.5."""
    args = _build_parser().parse_args(argv)
    quiet = bool(args.quiet)
    if args.print_launchagent:
        program = [sys.executable, "-m", "phoning.cli", "up"]
        if args.config:
            program += ["--config", args.config]
        if quiet:
            program += ["--quiet"]
        log_path = os.path.expanduser("~/Library/Logs/phoning-up.log")
        plist = launchagent_plist(
            program=program,
            interval=int(args.interval),
            log_path=log_path,
            path_env=_launchagent_path_env(),
        )
        sys.stdout.write(plist.decode("utf-8"))
        sys.stdout.flush()
        return 0
    config_path = _config_path(args.config)
    try:
        cfg = load_config(config_path)
    except ConfigError as exc:
        _print_config_error(config_path, exc)
        return 2
    items = reconcile(cfg, dry_run=bool(args.dry_run), python=sys.executable)
    shown = (
        [item for item in items if str(item.get("state")) in _QUIET_STATES]
        if quiet
        else items
    )
    out = sys.stdout
    for item in shown:
        if args.as_json:
            print(json.dumps(item, ensure_ascii=False), file=out)
        else:
            print(_format_item(item), file=out)
    out.flush()
    return 1 if any(item.get("state") == "failed" for item in items) else 0


if __name__ == "__main__":
    sys.exit(main())
