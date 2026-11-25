"""Calls across computers: ``phoning remote host`` and ``phoning remote join``.

The host opens a Cloudflare quick tunnel to its existing relay and prints
**one link** (a call card). The other person's agent runs
``phoning remote join '<link>'``: one paste, one direction. The link carries
the host lines' fingerprints and a retalk invite code per line; joining
redeems each code with ``retalk request`` while a small **watcher**
(``phoning remote watch``) accepts them on the host and saves the joiner as a
contact.

Security notes
--------------

* The relay only ever sees ciphertext and public keys, so exposing it through
  a tunnel exposes no messages.
* **The link is a key to your lines**: anyone holding it can add themselves
  as a contact of every line on it until ``phoning remote host --stop``
  revokes the codes. Send it in a DM, never a public channel.
* Names learned from intros are cosmetic. Authority (who can accept a
  proposal) stays bound to fingerprints.

Stdlib only. Every subprocess runs with ``stdin=subprocess.DEVNULL`` and a
timeout; retalk calls use 60 s. Exit codes: 0 ok, 1 failed at run time,
2 usage or missing input, 3 refused.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from typing import Any, Final
from urllib.parse import urlsplit

from phoning import directory as directory_mod
from phoning import envelope as envelope_mod
from phoning import intro as intro_mod
from phoning import ops
from phoning import up as up_mod
from phoning.mailbox import Mailbox
from phoning.retalk import Retalk, RetalkError

__all__ = [
    "CardError",
    "main",
    "make_card",
    "parse_card",
    "pick_alias",
]

EXIT_OK: Final[int] = 0
EXIT_FAILED: Final[int] = 1
EXIT_USAGE: Final[int] = 2
EXIT_REFUSED: Final[int] = 3

_PREFIX: Final[str] = "call-card:1:"
_CARD_RE: Final[re.Pattern[str]] = re.compile(r"call-card:1:[A-Za-z0-9_-]+")
_WHO_RE: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9-]{0,23}")
_CODE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_-]{8,64}")
_HEX32_RE: Final[re.Pattern[str]] = directory_mod._FP_RE
_TUNNEL_RE: Final[re.Pattern[str]] = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
_LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})
_DEFAULT_RELAY: Final[str] = "http://127.0.0.1:8766"
_DEFAULT_DB: Final[str] = "~/.phoning/relay/relay.db"
_DEFAULT_PORT: Final[int] = 8766
_DEFAULT_WAIT: Final[float] = 120.0
_RETALK_TIMEOUT: Final[float] = 60.0
_PROBE_TIMEOUT: Final[float] = 10.0
_TUNNEL_POLL: Final[float] = 0.2
_TUNNEL_WAIT: Final[float] = 45.0
_PROBE_POLL: Final[float] = 2.0


class CardError(ValueError):
    """Raised when a link is not a valid call card; ``str()`` is the reason."""


# -- small helpers ----------------------------------------------------------


def _err(msg: str) -> None:
    """Print one flattened ``error:`` line to stderr."""
    print(f"error: {' '.join(msg.split())}", file=sys.stderr)


def _hint(msg: str) -> None:
    """Print one flattened ``hint:`` line to stderr."""
    print(f"hint: {' '.join(msg.split())}", file=sys.stderr)


def _resolve_home(explicit: str | None) -> str:
    """``--home`` else ``$PHONING_HOME`` else ``~/.phoning``."""
    if explicit:
        return os.path.expanduser(explicit)
    env = os.environ.get("PHONING_HOME")
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/.phoning")


def _resolve_config(explicit: str | None) -> str:
    """Reuse ``up``'s resolver: ``--config`` else ``$PHONING_UP_CONFIG`` else default."""
    return os.path.expanduser(up_mod._config_path(explicit))


def _state_dir(config_path: str) -> str:
    """The state dir is the directory holding the config file."""
    parent = os.path.dirname(os.path.abspath(config_path))
    return parent or "."


def _atomic_write(path: str, data: bytes, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` atomically (tmp, fsync, replace, chmod)."""
    parent = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".phoning-", suffix=".tmp")
    try:
        os.chmod(tmp, mode)
        offset = 0
        while offset < len(data):
            n = os.write(fd, data[offset:])
            if n <= 0:
                msg = "short write"
                raise OSError(msg)
            offset += n
        os.fsync(fd)
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)
    try:
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        os.chmod(path, mode)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _atomic_write_json(path: str, data: Any, mode: int = 0o600) -> None:
    """Atomically write ``json.dumps(data)`` plus a newline to ``path``."""
    payload = (json.dumps(data, sort_keys=True) + "\n").encode("utf-8")
    _atomic_write(path, payload, mode=mode)


def _read_json(path: str) -> Any:
    """Read and parse JSON at ``path``; ``None`` when missing or invalid."""
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _read_text_line(path: str) -> str:
    """Read the first line of ``path`` (stripped); ``""`` on failure."""
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            return fh.readline().strip()
    except OSError:
        return ""


def _pid_alive(pid: int) -> bool:
    """True when ``pid`` names a live process (``kill -0``)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _pid_command(pid: int, *, timeout: float = 10.0) -> str:
    """Return ``ps -o command= -p PID`` output (stripped), or ``""`` on failure."""
    if pid <= 0:
        return ""
    try:
        proc = subprocess.run(  # noqa: S603
            ["ps", "-o", "command=", "-p", str(pid)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _sigterm(pid: int) -> None:
    """SIGTERM ``pid`` ignoring errors. Never signals pid <= 1 (0 and negatives address process groups)."""
    if pid <= 1:
        return
    with contextlib.suppress(OSError):
        os.kill(pid, signal.SIGTERM)


def _wait_for_pid_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` is gone; ``True`` when gone, ``False`` on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_alive(pid):
            return True
        time.sleep(0.1)
    return not _pid_alive(pid)


def _wait_for_port_closed(port: int, timeout: float) -> bool:
    """Poll a connect to ``127.0.0.1:port`` until refused; ``True`` when closed."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                pass
        except OSError:
            return True
        time.sleep(0.1)
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            pass
    except OSError:
        return True
    return False


def _spawn_detached(argv: Sequence[str], log_path: str, env: dict[str, str] | None = None) -> int:
    """Start ``argv`` detached with output (truncated) at ``log_path``; return the pid."""
    parent = os.path.dirname(os.path.abspath(log_path)) or "."
    os.makedirs(parent, mode=0o700, exist_ok=True)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with contextlib.suppress(OSError):
        os.chmod(log_path, 0o600)
    try:
        devnull = os.open(os.devnull, os.O_RDONLY)
        try:
            proc: subprocess.Popen[Any] = subprocess.Popen(  # noqa: S603
                list(argv),
                stdin=devnull,
                stdout=fd,
                stderr=fd,
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
    return proc.pid


# -- cards ------------------------------------------------------------------


def _who_from_hostname() -> str:
    """Default ``who``: hostname up to the first dot, lowercased and slugged."""
    raw = socket.gethostname().split(".")[0].lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:24]
    return slug or "computer"


def _validate_line(obj: Any) -> dict[str, Any]:
    """Validate one card line ``obj``; ``CardError`` on any broken rule."""
    if not isinstance(obj, dict):
        raise CardError("a line is not an object")
    line: dict[str, Any] = obj
    name = line.get("name")
    if not isinstance(name, str) or not directory_mod.is_legal_name(name):
        raise CardError("a line has an illegal name")
    fp = line.get("fingerprint")
    if not isinstance(fp, str) or _HEX32_RE.fullmatch(fp) is None:
        raise CardError("a line has an illegal fingerprint")
    harness = line.get("harness")
    if harness is not None and not isinstance(harness, str):
        raise CardError("a line's harness must be a string or null")
    role = line.get("role")
    if role is not None and not isinstance(role, str):
        raise CardError("a line's role must be a string or null")
    code_any = line.get("code")
    code: str | None = None
    if code_any is not None:
        if not isinstance(code_any, str) or _CODE_RE.fullmatch(code_any) is None:
            raise CardError("a line's code is not a valid invite code")
        code = code_any
    return {
        "name": name,
        "fingerprint": fp,
        "harness": harness,
        "role": role,
        "code": code,
    }


def _validate_card(obj: Any) -> dict[str, Any]:
    """Validate a decoded card ``obj`` into its canonical form."""
    if not isinstance(obj, dict):
        raise CardError("the card is not a JSON object")
    card: dict[str, Any] = obj
    v = card.get("v")
    if isinstance(v, bool) or v != 1 or not isinstance(v, int):
        raise CardError("unsupported card version")
    relay = card.get("relay")
    if not isinstance(relay, str) or not (
        relay.startswith("http://") or relay.startswith("https://")
    ):
        raise CardError("the relay must be an http:// or https:// URL")
    if urlsplit(relay).hostname is None:
        raise CardError("the relay URL has no host")
    who = card.get("who")
    if not isinstance(who, str) or _WHO_RE.fullmatch(who) is None:
        raise CardError("the who label is not a legal short label")
    if "room" not in card:
        raise CardError("the room field is missing")
    room = card.get("room")
    if room is not None and (not isinstance(room, str) or not directory_mod.is_legal_name(room)):
        raise CardError("the room is not a legal name")
    lines_raw = card.get("lines")
    if not isinstance(lines_raw, list) or not 1 <= len(lines_raw) <= 64:
        raise CardError("the card must name 1 to 64 lines")
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in lines_raw:
        parsed = _validate_line(entry)
        if parsed["name"] in seen:
            raise CardError("line names must be unique")
        seen.add(parsed["name"])
        lines.append(parsed)
    return {
        "v": 1,
        "relay": relay,
        "who": who,
        "room": room if isinstance(room, str) else None,
        "lines": lines,
    }


def _decode_payload(payload: str) -> dict[str, Any]:
    """Decode one base64url payload into a validated card (raises :class:`CardError`)."""
    try:
        data = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
    except (ValueError, TypeError) as exc:
        raise CardError("the payload is not valid base64url") from exc
    try:
        obj: Any = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CardError("the payload is not valid JSON") from exc
    try:
        return _validate_card(obj)
    except ValueError as exc:  # e.g. urlsplit on a malformed bracketed host
        if isinstance(exc, CardError):
            raise
        raise CardError("the relay URL is malformed") from exc


def parse_card(text: str) -> dict[str, Any]:
    """Parse a link (or any text containing one) into a validated card dict.

    Removes every whitespace character (chat apps wrap long lines), finds the first ``call-card:1:`` match,
    decodes the base64url payload and validates it. Because removing whitespace also glues on any words
    that followed the link ("<link>\nsee you there"), the payload is tried in full first and then cut back
    at each place the original text had whitespace; the longest candidate that decodes and validates wins.
    A missing card, bad base64, bad JSON or any broken rule raises :class:`CardError`.
    """
    flat_chars: list[str] = []
    gap_before: list[bool] = []  # gap_before[i]: whitespace preceded flat char i in the original
    pending_gap = False
    for ch in text:
        if ch.isspace():
            pending_gap = True
            continue
        flat_chars.append(ch)
        gap_before.append(pending_gap)
        pending_gap = False
    flat = "".join(flat_chars)
    match = _CARD_RE.search(flat)
    if match is None:
        raise CardError("no call card found")
    start = match.start() + len(_PREFIX)
    ends = [match.end()] + [i for i in range(match.end() - 1, start, -1) if gap_before[i]]
    first_error: CardError | None = None
    for end in ends:
        try:
            return _decode_payload(flat[start:end])
        except CardError as exc:
            if first_error is None:
                first_error = exc
    assert first_error is not None
    raise first_error


def make_card(card: dict[str, Any]) -> str:
    """Encode ``card`` into the wire form ``call-card:1:<base64url>``.

    The result encodes the compact JSON (sorted keys, ``(",", ":")``
    separators) with no ``=`` padding, so ``parse_card(make_card(c)) == c``
    for every valid ``c``.
    """
    validated = _validate_card(card)
    data = json.dumps(validated, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _PREFIX + base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def pick_alias(name: str, who: str, taken: set[str]) -> str:
    """Return ``name`` when free, else ``who-name`` cut to 64, else with ``-2``...

    The counter is appended after cutting the base so the whole alias stays
    at most 64 characters (``cut64[:62] + "-2"``). ``taken`` is not mutated.
    """
    if name not in taken:
        return name
    base = f"{who}-{name}"[:64]
    if base not in taken:
        return base
    counter = 2
    while True:
        suffix = f"-{counter}"
        candidate = base[: 64 - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
        counter += 1


# -- lines ------------------------------------------------------------------


def _all_lines(home: str) -> list[str]:
    """Every ``<home>/NAME`` with a legal name and an ``identity/`` subdir, sorted."""
    if not os.path.isdir(home):
        return []
    try:
        entries = sorted(os.listdir(home))
    except OSError:
        return []
    out: list[str] = []
    for name in entries:
        if not directory_mod.is_legal_name(name):
            continue
        if os.path.isdir(os.path.join(home, name, "identity")):
            out.append(name)
    return out


def _line_relay(user_dir: str) -> str | None:
    """Return the line's relay URL (``Retalk(user_dir).relay``)."""
    return Retalk(user_dir).relay


def _user_dir(home: str, name: str) -> str:
    """``<home>/<name>``."""
    return os.path.join(home, name)


def _line_id(user_dir: str) -> str:
    """Return the line's fingerprint, or ``""`` on any failure."""
    try:
        return Retalk(user_dir).id()
    except (RetalkError, OSError, subprocess.TimeoutExpired):
        return ""


def _relay_port(url: str, default: int = 0) -> int:
    """Port of ``url`` (defaulted by scheme when absent), or ``default``."""
    parts = urlsplit(url)
    with contextlib.suppress(ValueError):
        port = parts.port
        if port is not None:
            return int(port)
    if parts.scheme == "https":
        return 443
    if parts.scheme == "http":
        return 80
    return default


def _is_loopback(url: str) -> bool:
    """True when ``url``'s host is a loopback name/address."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _card_line_from_dir(user_dir: str) -> tuple[str | None, str | None]:
    """Return ``(harness, role)`` from that line's published directory card, else (None, None)."""
    name = os.path.basename(user_dir.rstrip(os.sep))
    directory = directory_mod._resolve_directory()
    path = os.path.join(directory, f"{name}.json")
    card, _error = directory_mod._read_card_file(path)
    if card is None or not directory_mod.is_valid_card(card):
        return None, None
    harness = card.get("harness")
    role = card.get("role")
    return (
        harness if isinstance(harness, str) else None,
        role if isinstance(role, str) else None,
    )


# -- relay probe ------------------------------------------------------------


def _probe_once(url: str) -> bool:
    """POST ``{"tool":"info","args":{}}`` to ``url``; object body counts as reached."""
    import urllib.error
    import urllib.request

    payload = json.dumps({"tool": "info", "args": {}}).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310
        url,
        data=payload,
        method="POST",
        headers={
            "content-type": "application/json",
            "user-agent": "Mozilla/5.0 (phoning remote probe)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT) as resp:  # noqa: S310
            body = resp.read()
    except urllib.error.HTTPError as exc:
        with contextlib.suppress(OSError):
            body = exc.read()
        try:
            parsed: Any = json.loads(body.decode("utf-8", errors="replace"))
        except ValueError:
            return False
        return isinstance(parsed, dict)
    except (urllib.error.URLError, OSError, ValueError):
        return False
    try:
        parsed = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return False
    return isinstance(parsed, dict)


def _wait_reachable(url: str, wait_secs: float) -> bool:
    """Retry the probe every 2 s until ``wait_secs`` elapse; ``True`` once reached."""
    deadline = time.monotonic() + wait_secs
    while True:
        if _probe_once(url):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_PROBE_POLL)


def _wait_secs() -> float:
    """``$PHONING_REMOTE_WAIT`` (default 120), clamped to a sane non-negative float."""
    raw = os.environ.get("PHONING_REMOTE_WAIT")
    if raw is None:
        return _DEFAULT_WAIT
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_WAIT
    if val < 0:
        return 0.0
    return val


# -- tunnel -----------------------------------------------------------------


def _find_cloudflared() -> str | None:
    """Locate ``cloudflared``: ``$PHONING_CLOUDFLARED``, PATH, then Homebrew paths."""
    env = os.environ.get("PHONING_CLOUDFLARED")
    if env is not None:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        return None
    found = shutil.which("cloudflared")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/cloudflared", "/usr/local/bin/cloudflared"):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _tunnel_url_from_log(log_path: str) -> str | None:
    """First match of a quick-tunnel URL in ``log_path``."""
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:  # noqa: PTH123
            text = fh.read()
    except OSError:
        return None
    match = _TUNNEL_RE.search(text)
    return match.group(0) if match else None


def _read_tunnel_state(state: str) -> dict[str, Any] | None:
    """Read ``<state>/tunnel.json`` into ``{pid, url, port}`` or ``None``."""
    obj = _read_json(os.path.join(state, "tunnel.json"))
    if not isinstance(obj, dict):
        return None
    typed: dict[str, Any] = obj
    pid_any = typed.get("pid")
    port_any = typed.get("port")
    url_any = typed.get("url")
    if isinstance(pid_any, bool) or not isinstance(pid_any, int):
        return None
    if isinstance(port_any, bool) or not isinstance(port_any, int):
        return None
    if not isinstance(url_any, str) or not url_any:
        return None
    return {"pid": pid_any, "url": url_any, "port": port_any}


def _reuse_tunnel(state: str, port: int) -> str | None:
    """Return the live tunnel URL from ``tunnel.json`` when it targets ``port``."""
    held = _read_tunnel_state(state)
    if held is None or held["port"] != port:
        return None
    pid = int(held["pid"])
    if not _pid_alive(pid):
        return None
    if "cloudflared" not in _pid_command(pid):
        return None
    return str(held["url"])


def _start_tunnel(binary: str, state: str, port: int) -> tuple[str | None, str]:
    """Start a quick tunnel; return ``(url, "")`` on success, ``(None, msg)`` on failure."""
    quick = os.path.join(state, "cloudflared-quick.yml")
    log_path = os.path.join(state, "tunnel.log")
    try:
        _atomic_write(quick, b"", mode=0o600)
    except OSError:
        return None, f"cannot write {quick}"
    argv = [
        binary,
        "tunnel",
        "--config",
        quick,
        "--no-autoupdate",
        "--url",
        f"http://127.0.0.1:{port}",
    ]
    try:
        pid = _spawn_detached(argv, log_path)
    except OSError:
        return None, f"cannot start cloudflared; see {log_path}"
    deadline = time.monotonic() + _TUNNEL_WAIT
    url: str | None = None
    while time.monotonic() < deadline:
        url = _tunnel_url_from_log(log_path)
        if url is not None:
            break
        if not _pid_alive(pid):
            url = _tunnel_url_from_log(log_path)
            break
        time.sleep(_TUNNEL_POLL)
    if url is None:
        _sigterm(pid)
        return None, f"the Cloudflare tunnel did not start; see {log_path}"
    try:
        _atomic_write_json(
            os.path.join(state, "tunnel.json"),
            {"pid": pid, "url": url, "port": port},
        )
    except OSError:
        return None, f"cannot write tunnel.json; see {log_path}"
    return url, ""


# -- config -----------------------------------------------------------------


def _default_config() -> dict[str, Any]:
    """The starter config used by ``host`` when the file is missing."""
    return {
        "relay": {"url": _DEFAULT_RELAY, "db": _DEFAULT_DB},
        "owners": [],
    }


def _load_raw_config(path: str) -> dict[str, Any] | None:
    """Read the raw config JSON; ``None`` on parse/IO failure (caller decides)."""
    if not os.path.exists(path):
        return _default_config()
    obj = _read_json(path)
    if not isinstance(obj, dict):
        return None
    return dict(obj)


def _write_config(path: str, cfg: dict[str, Any], *, backup: bool) -> str | None:
    """Back up the old ``path`` when present and atomically write ``cfg`` (0600).

    Returns the backup path (or ``None``); raises :class:`OSError` on failure.
    The timestamp is made unique so a second write within the same second
    never overwrites the first backup.
    """
    backup_path: str | None = None
    if backup and os.path.exists(path):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        candidate = f"{path}.bak-{stamp}"
        n = 1
        while os.path.exists(candidate):
            candidate = f"{path}.bak-{stamp}-{n}"
            n += 1
        shutil.copy2(path, candidate)
        backup_path = candidate
    payload = (json.dumps(cfg, indent=2) + "\n").encode("utf-8")
    _atomic_write(path, payload, mode=0o600)
    return backup_path


def _relay_url_from_config(cfg: dict[str, Any]) -> str:
    """``relay.url`` from a raw config dict, else the default."""
    relay = cfg.get("relay")
    if isinstance(relay, dict):
        url = relay.get("url")
        if isinstance(url, str) and url:
            return url
    return _DEFAULT_RELAY


# -- restart ----------------------------------------------------------------


def _listener_pids(port: int, *, timeout: float = 10.0) -> list[int] | None:
    """PIDs listening on ``port`` via ``lsof``; ``None`` when lsof is missing or fails."""
    try:
        proc = subprocess.run(  # noqa: S603
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode not in (0, 1):  # 1 = nothing listening
        return None
    pids: list[int] = []
    for tok in proc.stdout.split():
        if tok.strip().isdigit():
            pids.append(int(tok))
    return pids


def _is_retalk_server(pid: int) -> bool:
    """True when ``pid``'s program (argv[0], or the script a Python runs) is ``retalk-server``."""
    tokens = _pid_command(pid).split()
    return any(os.path.basename(tok) == "retalk-server" for tok in tokens[:4])


def _check_origin(relay_url: str) -> tuple[str, int]:
    """Before anything is exposed: the relay must be on this computer and, if up, be retalk-server.

    Returns ``("", 0)`` when it is safe to put a tunnel in front of the relay port, else ``(error, rc)``.
    """
    if not _is_loopback(relay_url):
        return (f"the relay in the config ({relay_url}) is not on this computer; only a local relay can host",
                EXIT_USAGE)
    if not up_mod.relay_up(relay_url):
        return "", 0
    port = _relay_port(relay_url, _DEFAULT_PORT)
    pids = _listener_pids(port)
    if pids is None:
        return f"cannot tell which program listens on port {port} (lsof failed)", EXIT_FAILED
    if not pids:
        return f"cannot tell which program listens on port {port}", EXIT_FAILED
    for pid in pids:
        if not _is_retalk_server(pid):
            return f"port {port} is held by another program", EXIT_REFUSED
    return "", 0


def _flag_value(cmd: str, flag: str) -> str:
    """Extract the value of ``--flag VALUE`` / ``--flag=VALUE`` from a command line."""
    tokens = cmd.split()
    for idx, tok in enumerate(tokens):
        if tok == flag and idx + 1 < len(tokens):
            return tokens[idx + 1]
        if tok.startswith(flag + "="):
            return tok[len(flag) + 1:]
    return ""


def _listener_matches(pid: int, url: str, public: list[str], bind: str | None) -> bool:
    """True when ``pid``'s command carries our URL in ``--audience`` (and the bind)."""
    cmd = _pid_command(pid)
    if not cmd or "--audience" not in cmd:
        return False
    audience_value = _flag_value(cmd, "--audience")
    entries = set(audience_value.split(",")) if audience_value else set()
    needs = {up_mod._public_norm(url), *(up_mod._public_norm(p) for p in public)}
    have = {up_mod._public_norm(e) for e in entries if e}
    if not needs.issubset(have):
        return False
    return bind is None or _flag_value(cmd, "--host") == bind


def _ensure_relay(
    *,
    config_path: str,
    relay_url: str,
    relay_log: str,
    url: str,
    public: list[str],
    bind: str | None,
) -> tuple[bool, str, int]:
    """Ensure the relay runs and carries ``url``/``public``; ``(restarted, err, rc)``.

    A relay that is down is started (a computer hosting for the first time has none running yet).
    """
    port = _relay_port(relay_url, _DEFAULT_PORT)
    if up_mod.relay_up(relay_url):
        pids = _listener_pids(port)
        if not pids:
            return False, f"cannot tell which program listens on port {port}", EXIT_FAILED
        if not all(_is_retalk_server(pid) for pid in pids):
            return False, f"port {port} is held by another program", EXIT_REFUSED
        if any(_listener_matches(pid, url, public, bind) for pid in pids):
            return False, "", EXIT_OK
        for pid in pids:
            _sigterm(pid)
        # Wait on the port, not the pid: a relay whose parent has not reaped it yet lingers as a zombie.
        if not _wait_for_port_closed(port, 10.0):
            return False, "the relay did not stop", EXIT_FAILED
    try:
        cfg = up_mod.load_config(config_path)
    except up_mod.ConfigError as exc:
        return False, str(exc), EXIT_USAGE
    for item in up_mod.reconcile(cfg):
        if item.get("kind") == "relay" and item.get("state") not in ("up", "started"):
            return False, f"the relay did not start; see {relay_log}", EXIT_FAILED
    return True, "", EXIT_OK


# -- host -------------------------------------------------------------------


def _ensure_watcher(home: str) -> bool:
    """Ensure one ``phoning remote watch`` runs for ``home``; ``False`` when it could not be started."""
    pid_path = os.path.join(home, ".remote-watch.pid")
    with contextlib.suppress(ValueError):
        old = int(_read_text_line(pid_path))
        if _pid_alive(old) and f"phoning.remote watch --home {home}" in _pid_command(old):
            return True
    log_path = os.path.join(home, ".remote-watch.log")
    argv = [sys.executable, "-m", "phoning.remote", "watch", "--home", home]
    try:
        pid = _spawn_detached(argv, log_path)
    except OSError:
        return False
    try:
        _atomic_write(pid_path, f"{pid}\n".encode())
    except OSError:
        _sigterm(pid)
        return False
    return True


def _stop_watcher(home: str) -> None:
    """SIGTERM the watcher named by ``<home>/.remote-watch.pid`` (when it is ours)."""
    pid_path = os.path.join(home, ".remote-watch.pid")
    killed = False
    with contextlib.suppress(ValueError):
        pid = int(_read_text_line(pid_path))
        if _pid_alive(pid) and "phoning.remote watch" in _pid_command(pid):
            _sigterm(pid)
            _wait_for_pid_gone(pid, 10.0)
            killed = True
    if killed or not os.path.exists(pid_path):
        with contextlib.suppress(OSError):
            os.unlink(pid_path)


def _stop_tunnel(state: str) -> None:
    """SIGTERM the tunnel named by ``<state>/tunnel.json`` (when it is ours)."""
    held = _read_tunnel_state(state)
    if held is not None:
        pid = int(held["pid"])
        if _pid_alive(pid) and "cloudflared" in _pid_command(pid):
            _sigterm(pid)
            _wait_for_pid_gone(pid, 10.0)
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(state, "tunnel.json"))


def _ensure_invite(user_dir: str) -> str | None:
    """Return (and if needed create) a live permanent invite code for the line."""
    path = os.path.join(user_dir, "remote-invite")
    client = Retalk(user_dir)
    stored = _read_text_line(path)
    if stored:
        try:
            if stored in client.invite_codes():
                return stored
        except (RetalkError, OSError, subprocess.TimeoutExpired):
            pass
    try:
        code = client.invite_new()
    except (RetalkError, OSError, subprocess.TimeoutExpired):
        return None
    try:
        _atomic_write(path, (code + "\n").encode("utf-8"))
    except OSError:
        # An unrecorded code could never be revoked by --stop: revoke it now and fail.
        with contextlib.suppress(RetalkError, OSError, subprocess.TimeoutExpired):
            client.invite_revoke(code)
        return None
    return code


def _revoke_invites(home: str) -> list[str]:
    """Revoke every line's recorded invite code; return the lines whose code may still be live.

    A record is deleted only once retalk no longer lists its code as active, so a failed revoke can be
    retried by running ``--stop`` again.
    """
    failed: list[str] = []
    for name in _all_lines(home):
        udir = _user_dir(home, name)
        path = os.path.join(udir, "remote-invite")
        code = _read_text_line(path)
        if code:
            client = Retalk(udir)
            try:
                client.invite_revoke(code)
                still = code in client.invite_codes()
            except (RetalkError, OSError, subprocess.TimeoutExpired):
                still = True
            if still:
                failed.append(name)
                continue
        with contextlib.suppress(OSError):
            os.unlink(path)
    return failed


def _strip_public_url(config_path: str, url: str) -> None:
    """Remove ``url`` from ``relay.public`` (backup + atomic write; no restart)."""
    if not os.path.exists(config_path):
        return
    raw = _read_json(config_path)
    if not isinstance(raw, dict):
        return
    relay = raw.get("relay")
    if not isinstance(relay, dict):
        return
    pub = relay.get("public")
    if not isinstance(pub, list):
        return
    key = up_mod._public_norm(url)
    kept = [
        p for p in pub if isinstance(p, str) and p and up_mod._public_norm(p) != key
    ]
    if kept == [p for p in pub if isinstance(p, str) and p]:
        return
    if kept:
        relay["public"] = kept
    else:
        relay.pop("public", None)
    with contextlib.suppress(OSError):
        _write_config(config_path, raw, backup=True)


def _host_stop(home: str, config_path: str, *, as_json: bool) -> int:
    """``phoning remote host --stop``."""
    state = _state_dir(config_path)
    held = _read_tunnel_state(state)
    _stop_tunnel(state)
    _stop_watcher(home)
    failed = _revoke_invites(home)
    if held is not None:
        _strip_public_url(config_path, str(held["url"]))
    if as_json:
        print(json.dumps({"stopped": True, "not_revoked": failed}))
    else:
        print("stopped")
    if failed:
        _err(f"could not revoke the link's code for: {', '.join(failed)}")
        _hint("phoning remote host --stop (run it again; the tunnel is already closed)")
        return EXIT_FAILED
    return EXIT_OK


def _next_public(relay_block: dict[str, Any], url: str) -> list[str]:
    """The relay's public list with any other quick-tunnel entry dropped and ``url`` first."""
    out: list[str] = [url]
    seen: set[str] = {up_mod._public_norm(url)}
    pub_raw = relay_block.get("public")
    if isinstance(pub_raw, list):
        for entry in pub_raw:
            if not isinstance(entry, str) or not entry:
                continue
            if ".trycloudflare.com" in entry and up_mod._public_norm(entry) != up_mod._public_norm(url):
                continue
            key = up_mod._public_norm(entry)
            if key in seen:
                continue
            seen.add(key)
            out.append(entry)
    return out


def _cmd_host(args: argparse.Namespace) -> int:
    """``phoning remote host``: one run at a time per state dir (an exclusive lock), see :func:`_host_locked`."""
    config_path = _resolve_config(args.config)
    state = _state_dir(config_path)
    try:
        os.makedirs(state, mode=0o700, exist_ok=True)
        lock_fd = os.open(os.path.join(state, "remote-host.lock"),
                          os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as exc:
        _err(f"cannot lock {state}: {exc}")
        return EXIT_FAILED
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        return _host_locked(args)
    finally:
        os.close(lock_fd)


def _host_locked(args: argparse.Namespace) -> int:
    """Host under the lock; a tunnel this run started is closed again if a later step fails."""
    started: list[bool] = [False]
    rc = _host_steps(args, started)
    if rc != EXIT_OK and started[0]:
        _stop_tunnel(_state_dir(_resolve_config(args.config)))
    return rc


def _host_steps(args: argparse.Namespace, started: list[bool]) -> int:  # noqa: C901, PLR0911, PLR0912, PLR0915
    home = _resolve_home(args.home)
    config_path = _resolve_config(args.config)
    state = _state_dir(config_path)
    if args.stop:
        return _host_stop(home, config_path, as_json=bool(args.json))
    who = args.who or _who_from_hostname()
    if _WHO_RE.fullmatch(who) is None:
        _err(f"illegal --who label: {who}")
        return EXIT_USAGE
    # Each host's party line gets its own name, so joining a second call never merges two rosters.
    room = args.room if args.room is not None else f"call-{who}"
    if not directory_mod.is_legal_name(room):
        _err(f"illegal --room: {room}")
        return EXIT_USAGE
    cfg_raw = _load_raw_config(config_path)
    if cfg_raw is None:
        _err(f"cannot read config {config_path}")
        return EXIT_USAGE
    relay_url = _relay_url_from_config(cfg_raw)
    port = _relay_port(relay_url, _DEFAULT_PORT)

    # 0. Before anything is exposed: the port must be (or become) our own retalk relay.
    origin_err, origin_rc = _check_origin(relay_url)
    if origin_err:
        _err(origin_err)
        return origin_rc

    # 1. Public URL.
    url_given: str | None = args.url
    tunnel = False
    if url_given:
        url = str(url_given).strip()
        if not (url.startswith("http://") or url.startswith("https://")):
            _err("--url must be an http:// or https:// URL")
            return EXIT_USAGE
    else:
        reused = _reuse_tunnel(state, port)
        if reused is not None:
            url = reused
        else:
            binary = _find_cloudflared()
            if binary is None:
                _err("cloudflared is not installed")
                _hint("brew install cloudflared (or pass --url)")
                return EXIT_USAGE
            got, msg = _start_tunnel(binary, state, port)
            if got is None:
                _err(msg)
                return EXIT_FAILED
            url = got
            tunnel = True
            started[0] = True

    # 2. Config: ensure relay.public, maybe relay.bind.
    relay_block = cfg_raw.get("relay")
    if not isinstance(relay_block, dict):
        relay_block = {"url": _DEFAULT_RELAY, "db": _DEFAULT_DB}
        cfg_raw["relay"] = relay_block
    new_public = _next_public(relay_block, url)
    changed = False
    if relay_block.get("public") != new_public:
        relay_block["public"] = new_public
        changed = True
    if (url_given and not _is_loopback(url) and not url.startswith("https://")
            and relay_block.get("bind") != "0.0.0.0"):
        relay_block["bind"] = "0.0.0.0"
        changed = True
    if changed:
        backup_path: str | None = None
        try:
            backup_path = _write_config(config_path, cfg_raw, backup=True)
        except OSError:
            _err(f"cannot write config {config_path}")
            return EXIT_FAILED
        try:
            up_mod.load_config(config_path)
        except up_mod.ConfigError as exc:
            with contextlib.suppress(OSError, TypeError):
                if backup_path is not None and os.path.exists(backup_path):
                    shutil.copy2(backup_path, config_path)
            _err(str(exc))
            return EXIT_USAGE

    # 3. Restart the relay when needed.
    bind_any = relay_block.get("bind")
    bind_val = bind_any if isinstance(bind_any, str) and bind_any else None
    restarted, relay_err, relay_rc = _ensure_relay(
        config_path=config_path,
        relay_url=relay_url,
        relay_log=str(relay_block.get("log", "")),
        url=url,
        public=new_public[1:],
        bind=bind_val,
    )
    if relay_err:
        _err(relay_err)
        return relay_rc

    # 4. Invite codes; 5. watcher. A link is only printed when every line on it can be joined.
    line_entries: list[dict[str, Any]] = []
    relay_key = directory_mod.normalize_relay(relay_url)
    for name in _all_lines(home):
        udir = _user_dir(home, name)
        fp = _line_id(udir)
        if not fp:
            continue
        line_relay = Retalk(udir).relay or ""
        if directory_mod.normalize_relay(line_relay) != relay_key:
            print(f"skipping {name}: it uses another relay ({line_relay})", file=sys.stderr)
            continue
        code = _ensure_invite(udir)
        if code is None:
            _err(f"could not create an invite code for {name}")
            return EXIT_FAILED
        harness, role = _card_line_from_dir(udir)
        line_entries.append(
            {"name": name, "fingerprint": fp, "harness": harness, "role": role, "code": code}
        )
    if not _ensure_watcher(home):
        _err("could not start the watcher that lets the other side in")
        _hint(f"see {os.path.join(home, '.remote-watch.log')}")
        return EXIT_FAILED

    # 6. Reachability check.
    if not _wait_reachable(url, _wait_secs()):
        print(f"warning: {url} is not answering yet; wait a minute before the other side joins", file=sys.stderr)

    # 7. Output.
    link: str | None = None
    if line_entries:
        try:
            link = make_card(
                {"v": 1, "relay": url, "who": who, "room": room, "lines": line_entries}
            )
        except CardError:
            link = None
    if args.json:
        print(
            json.dumps(
                {
                    "url": url,
                    "config": config_path,
                    "restarted": restarted,
                    "tunnel": tunnel,
                    "link": link,
                }
            )
        )
        return EXIT_OK
    if link is None:
        print(
            f'no lines yet: create one with "phoning init NAME --relay http://127.0.0.1:{port}", '
            "then rerun this"
        )
        return EXIT_OK
    print(f"relay reachable at {url}")
    print("send this link to the other computer (a DM, not a public channel):")
    print(link)
    # One sentence the other person pastes into any coding agent; ONBOARD.md's fast path takes it from there.
    print(f"they paste this into their coding agent: Set up Phoning and join this call: {PHONING_REPO} {link}")
    return EXIT_OK


# -- watch ------------------------------------------------------------------


def _pending_path(home: str) -> str:
    """Path of ``<home>/.remote-pending.json``."""
    return os.path.join(home, ".remote-pending.json")


def _load_pending(home: str) -> dict[str, dict[str, str]]:
    """Read the pending map (``{"LINE:F": {line, alias}}``); empty on any failure."""
    obj = _read_json(_pending_path(home))
    if not isinstance(obj, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for key, val in obj.items():
        if not isinstance(key, str) or not isinstance(val, dict):
            continue
        line = val.get("line")
        alias = val.get("alias")
        if isinstance(line, str) and isinstance(alias, str):
            out[key] = {"line": line, "alias": alias}
    return out


def _save_pending(home: str, pending: dict[str, dict[str, str]]) -> None:
    """Atomically write the pending map (0600)."""
    with contextlib.suppress(OSError):
        _atomic_write_json(_pending_path(home), pending)


def _log(stderr_line: str) -> None:
    """Write one flattened line to stderr."""
    print(" ".join(stderr_line.split()), file=sys.stderr)


def _contact_name_for_fp(contacts: dict[str, str], fp: str) -> str | None:
    """Return the saved name for ``fp``, or ``None``."""
    for name, saved_fp in contacts.items():
        if saved_fp == fp:
            return name
    return None


def _read_all(box: Mailbox) -> list[dict[str, Any]]:
    """Read every record in ``box``'s log (via its private ``_read_all`` when present)."""
    reader = getattr(box, "_read_all", None)
    if callable(reader):
        result: list[dict[str, Any]] = reader()
        return result
    out: list[dict[str, Any]] = []
    try:
        with open(os.path.join(box.mailbox_dir, "log.jsonl"), encoding="utf-8") as fh:  # noqa: PTH123
            for raw in fh:
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError:
        return []
    return out


def _accept_requests(home: str, line: str, pending: dict[str, dict[str, str]]) -> None:
    """Pass 1: accept invite requests, rename nameless contacts to ``guest-<F8>``."""
    udir = _user_dir(home, line)
    client = Retalk(udir)
    try:
        events = client.invite_watch_once()
    except (RetalkError, OSError, subprocess.TimeoutExpired):
        _log(f"{line}: invite watch failed")
        return
    contacts = client.contacts_fp()
    for ev in events:
        if ev.get("kind") != "contact_accepted":
            continue
        fp_any = ev.get("from")
        if not isinstance(fp_any, str) or _HEX32_RE.fullmatch(fp_any) is None:
            continue
        fp = fp_any
        saved = _contact_name_for_fp(contacts, fp)
        if saved is not None and saved.startswith("guest-"):
            continue  # already handled by an earlier pass
        # Never keep a name the joiner chose (its retalk self-name or its intro): a board grants authority by
        # alias, so a link holder naming itself after an owner must not inherit that owner's decisions.
        # Every guest is saved as guest-<fp8> now and guest-<its line name> once its intro arrives.
        temp = f"guest-{fp[:8]}"
        # `retalk add` on a saved fingerprint renames it in place and keeps its session (verified);
        # removing first is never needed.
        try:
            client.add(fp, temp)
        except (RetalkError, OSError, subprocess.TimeoutExpired):
            _log(f"{line}: add {fp[:8]} as {temp} failed")
            continue
        if saved is not None:
            contacts.pop(saved, None)
        contacts[temp] = fp
        pending[f"{line}:{fp}"] = {"line": line, "alias": temp}


def _principal_of(rec: dict[str, Any]) -> str | None:
    """Return a legal ``from.principal`` from an inbound record, else ``None``."""
    if rec.get("direction") != "in":
        return None
    env_any = rec.get("env")
    if not isinstance(env_any, dict):
        return None
    frm_any = env_any.get("from")
    if not isinstance(frm_any, dict):
        return None
    principal = frm_any.get("principal")
    if isinstance(principal, str) and directory_mod.is_legal_name(principal):
        return principal
    return None


def _name_pending(
    home: str, line: str, pending: dict[str, dict[str, str]], fps_here: list[str]
) -> None:
    """Pass 2: rename a temp alias to the joiner's real principal (from their intro)."""
    udir = _user_dir(home, line)
    records = _read_all(Mailbox(udir))
    client = Retalk(udir)
    contacts = client.contacts_fp()
    local = set(_all_lines(home))
    for fp in fps_here:
        entry = pending.get(f"{line}:{fp}")
        if entry is None or entry["line"] != line:
            continue
        temp = entry["alias"]
        principal: str | None = None
        for rec in records:
            if rec.get("peer_fp") != fp:
                continue
            principal = _principal_of(rec)
            if principal is not None:
                break
        if principal is None:
            continue
        taken = ({n for n in contacts if n != temp} - {""}) | local
        alias = pick_alias(f"guest-{principal}"[:64], "guest", taken)
        try:
            client.add(fp, alias)
        except (RetalkError, OSError, subprocess.TimeoutExpired):
            _log(f"{line}: rename {fp[:8]} to {alias} failed")
            continue
        contacts.pop(temp, None)
        contacts[alias] = fp
        pending.pop(f"{line}:{fp}", None)
        _log(f"{line}: {fp[:8]} is now {alias}")


def _watch_pass(home: str) -> None:
    """One full watcher pass: accept requests, then name the pending guests."""
    lines = _all_lines(home)
    if not lines:
        return
    pending = _load_pending(home)
    for line in lines:
        before = {fp: dict(v) for fp, v in pending.items()}
        try:
            _accept_requests(home, line, pending)
        except Exception:  # noqa: BLE001 - never stop the loop
            _log(f"{line}: watch pass failed")
        if pending != before:
            _save_pending(home, pending)
    fps = sorted({k.split(":", 1)[1] for k in pending if ":" in k})
    if not fps:
        _save_pending(home, pending)
        return
    for line in lines:
        try:
            _name_pending(home, line, pending, fps)
        except Exception:  # noqa: BLE001
            _log(f"{line}: name pass failed")
    _save_pending(home, pending)


def _cmd_watch(args: argparse.Namespace) -> int:
    home = _resolve_home(args.home)
    if args.once:
        _watch_pass(home)
        return EXIT_OK
    interval = float(args.interval)
    if interval <= 0:
        interval = 2.0
    pid_path = os.path.join(home, ".remote-watch.pid")
    with contextlib.suppress(OSError):
        _atomic_write(pid_path, f"{os.getpid()}\n".encode())
    try:
        while True:
            _watch_pass(home)
            time.sleep(interval)
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        with contextlib.suppress(ValueError, OSError):
            stored = int(_read_text_line(pid_path))
            if stored == os.getpid():
                os.unlink(pid_path)


# -- join -------------------------------------------------------------------


def _parse_into(raw: str | None, home: str) -> tuple[list[str] | None, str]:
    """Parse ``--into`` into a validated list; return ``(names, err)``."""
    if raw is None:
        return None, ""
    names = [p.strip() for p in raw.split(",") if p.strip()]
    if not names:
        return None, "empty --into"
    known = set(_all_lines(home))
    for name in names:
        if not directory_mod.is_legal_name(name):
            return None, f"illegal name in --into: {name}"
        if name not in known:
            return None, f"unknown line in --into: {name}"
    return names, ""


def _relay_matches(line_relay: str | None, card_relay: str, host_public: list[str]) -> bool:
    """True when line's relay matches the card's relay (directly or via public)."""
    if line_relay is None:
        return False
    if directory_mod.normalize_relay(line_relay) == directory_mod.normalize_relay(card_relay):
        return True
    return any(directory_mod.normalize_relay(p) == directory_mod.normalize_relay(card_relay)
               for p in host_public)


def _host_config_public(cfg: dict[str, Any], line_relay: str) -> list[str]:
    """Configured ``relay.public`` when its ``relay.url`` equals ``line_relay``."""
    relay = cfg.get("relay")
    if not isinstance(relay, dict):
        return []
    url = relay.get("url")
    if not isinstance(url, str) or directory_mod.normalize_relay(url) != directory_mod.normalize_relay(line_relay):
        return []
    pub = relay.get("public")
    if not isinstance(pub, list):
        return []
    return [p for p in pub if isinstance(p, str)]


def _rewrite_relay(udir: str, new_relay: str) -> None:
    """Rewrite ``<udir>/relay`` to ``new_relay`` (atomic, 0600)."""
    path = os.path.join(udir, "relay")
    _atomic_write(path, (new_relay.strip() + "\n").encode("utf-8"))


def _has_saved_api_key(udir: str) -> bool:
    """True when the line's retalk identity has a relay access key saved (read-only look at its store)."""
    db = os.path.join(udir, "identity", "store.db")
    if not os.path.exists(db):
        return False
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2.0)
        try:
            row = con.execute("SELECT v FROM meta WHERE k='api_key'").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0])


def _is_owner_of(pid: int, name: str) -> bool:
    """True when ``pid`` is a phoning owner daemon for line ``name`` (never trust a lock file's pid blindly)."""
    tokens = _pid_command(pid).split()
    return pid > 1 and "own" in tokens and name in tokens and any("phoning" in t for t in tokens)


def _move_lines(
    home: str,
    moves: list[tuple[str, str, str]],
    config_path: str,
    *,
    quiet: bool = False,
) -> str:
    """Register every line with its new relay, rewrite its file; ``""`` on success."""
    for name, udir, card_relay in moves:
        try:
            Retalk(udir).register(card_relay)
            _rewrite_relay(udir, card_relay)
        except (RetalkError, OSError, subprocess.TimeoutExpired):
            return f"could not register {name} on {card_relay}"
    for name, udir, card_relay in moves:
        holder = up_mod.owner_holder(udir)
        pid_any = holder.get("pid") if isinstance(holder, dict) else None
        if isinstance(pid_any, int) and not isinstance(pid_any, bool) and _is_owner_of(pid_any, name):
            _sigterm(pid_any)
            _wait_for_pid_gone(pid_any, 5.0)
        if not quiet:
            print(f"moved {name} to {card_relay} (mail still waiting on the old relay stays there)")
    if os.path.exists(config_path):
        with contextlib.suppress(up_mod.ConfigError, OSError):
            up_mod.reconcile(up_mod.load_config(config_path))
    return ""


def _add_contacts(
    home: str,
    line: str,
    udir: str,
    card: dict[str, Any],
    local_fps: set[str],
    added: list[dict[str, str]],
    kept: list[dict[str, str]],
) -> str:
    """Add every card line to ``line`` as a contact; return ``""`` or an error."""
    client = Retalk(udir)
    contacts = client.contacts_fp()
    by_fp = {fp: name for name, fp in contacts.items() if name}
    taken = {n for n in contacts if n} | set(_all_lines(home))
    for entry in card["lines"]:
        fp = entry["fingerprint"]
        if fp in local_fps:
            continue
        if fp in by_fp:
            kept.append({"line": line, "peer": entry["name"], "alias": by_fp[fp]})
            continue
        alias = pick_alias(entry["name"], card["who"], taken)
        code = entry.get("code")
        try:
            if isinstance(code, str) and code:
                client.request(fp, code, alias)
            else:
                client.add(fp, alias)
        except (RetalkError, OSError, subprocess.TimeoutExpired):
            return f"could not reach {entry['name']} for {line} (is the link still live?)"
        added.append({"line": line, "peer": entry["name"], "alias": alias})
        by_fp[fp] = alias
        taken.add(alias)
        with contextlib.suppress(Exception):
            intro_mod.send_intro(udir, line, alias)
    return ""


def _room_members(
    home: str,
    line: str,
    udir: str,
    card: dict[str, Any],
    local_lines: list[str],
) -> list[str]:
    """Every alias ``line`` has for a card line plus every local line it knows."""
    contacts = Retalk(udir).contacts_fp()
    by_fp = {fp: name for name, fp in contacts.items() if name}
    members: list[str] = []
    for entry in card["lines"]:
        alias = by_fp.get(entry["fingerprint"])
        if alias:
            members.append(alias)
    for other in local_lines:
        if other == line:
            continue
        other_fp = _line_id(_user_dir(home, other))
        alias = by_fp.get(other_fp) if other_fp else None
        if alias:
            members.append(alias)
    return members


def _ensure_room(
    home: str,
    line: str,
    udir: str,
    room: str,
    card: dict[str, Any],
    local_lines: list[str],
) -> tuple[str, bool]:
    """Ensure ``line`` is in ``room`` with every card line; announce only when the room changed.

    Returns ``(error, announced)``. Running a join again with the same link changes nothing and sends nothing.
    """
    client = Retalk(udir)
    members = _room_members(home, line, udir, card, local_lines)
    try:
        current = client.group_members(room)
    except (RetalkError, OSError, subprocess.TimeoutExpired):
        current = []
    changed = False
    if not current:
        try:
            client.group_create(room, members)
        except (RetalkError, OSError, subprocess.TimeoutExpired):
            return f"could not create room {room} for {line}", False
        changed = True
        with contextlib.suppress(RetalkError, OSError, subprocess.TimeoutExpired):
            current = client.group_members(room)
    # `retalk group members` prints "alias<TAB>fingerprint" rows.
    have = {row.split("\t")[0].strip() for row in current}
    missing = [m for m in members if m not in have]
    if missing:
        try:
            client.group_add(room, missing)
        except (RetalkError, OSError, subprocess.TimeoutExpired):
            return f"could not add members to {room} for {line}", False
        changed = True
    if not changed:
        return "", False
    try:
        ops.send_message(udir, line, "remote", msg_type="status", subject="joined the call", room=room,
                         data={"state": "ACTIVE"})
    except (envelope_mod.EnvelopeError, ValueError, RetalkError, OSError, subprocess.TimeoutExpired) as exc:
        return f"could not send the join status to {room} from {line} ({type(exc).__name__}: {exc})", False
    return "", True


def _link_local_lines(home: str, local_lines: list[str]) -> None:
    """Make every pair of joining lines contacts of each other (best effort), so the room includes them all."""
    if len(local_lines) < 2:
        return
    fps = {name: _line_id(_user_dir(home, name)) for name in local_lines}
    for name in local_lines:
        client = Retalk(_user_dir(home, name))
        try:
            have = set(client.contacts_fp().values())
        except (RetalkError, OSError, subprocess.TimeoutExpired):
            continue
        for other in local_lines:
            other_fp = fps.get(other)
            if other == name or not other_fp or other_fp in have:
                continue
            with contextlib.suppress(RetalkError, OSError, subprocess.TimeoutExpired):
                client.add(other_fp, other)


def _short_link(text: str) -> str:
    """Return the first link in ``text`` or a short placeholder."""
    match = _CARD_RE.search(re.sub(r"\s", "", text))
    return match.group(0) if match else "<link>"


PHONING_REPO = "https://github.com/verygoodbread/phoning"
RESYNC_SECONDS = 180.0
RESYNC_INTERVAL = 5.0


def _spawn_resync(home: str, lines: Sequence[str]) -> None:
    """Start ``phoning remote resync`` detached for ``lines`` (best effort, never fails the join)."""
    if not lines:
        return
    argv = [sys.executable, "-m", "phoning.remote", "resync", "--home", home, "--lines", ",".join(lines)]
    with contextlib.suppress(OSError):
        _spawn_detached(argv, os.path.join(home, ".remote-resync.log"))


def _cmd_resync(args: argparse.Namespace) -> int:
    """Re-upload each line's unacknowledged outbox every few seconds for a while (see join step 8)."""
    home = _resolve_home(args.home)
    names = [n for n in str(args.lines).split(",") if n and directory_mod.is_legal_name(n)]
    deadline = time.monotonic() + float(args.seconds)
    while True:
        for name in names:
            with contextlib.suppress(RetalkError, OSError, subprocess.TimeoutExpired):
                Retalk(_user_dir(home, name)).sync()
        if args.once or time.monotonic() >= deadline:
            return EXIT_OK
        time.sleep(float(args.interval))


def _ensure_operator(config_path: str, home: str, name: str, relay: str) -> str:
    """Add ``name`` to the up config's owners (creating the config if needed) and start it.

    A missing config is created pointing at the call's relay, so ``phoning up`` never tries to start a
    local relay on a computer that only joins. Returns one line for the user ("" on failure: joining
    already worked, the operator is a convenience).
    """
    cfg: dict[str, Any] | None
    if os.path.exists(config_path):
        cfg = _read_json(config_path) if os.path.exists(config_path) else None
        if not isinstance(cfg, dict):
            return f"warning: {config_path} is not valid JSON; start {name}'s operator with: phoning own {name}"
        cfg = dict(cfg)
    else:
        cfg = {"relay": {"url": relay, "db": os.path.join(home, "relay", "relay.db")}, "owners": []}
    owners_any = cfg.get("owners")
    owners: list[Any] = list(owners_any) if isinstance(owners_any, list) else []
    home_abs = os.path.abspath(os.path.expanduser(home))
    present = any(
        isinstance(o, dict) and o.get("name") == name
        and os.path.abspath(os.path.expanduser(str(o.get("home", "")))) == home_abs
        for o in owners
    )
    if not present:
        owners.append({"home": home, "name": name, "session": f"{name}-operator", "interval": 2})
        cfg["owners"] = owners
        try:
            _write_config(config_path, cfg, backup=True)
            up_mod.load_config(config_path)
        except (OSError, up_mod.ConfigError):
            return f"warning: could not add {name} to {config_path}; start its operator with: phoning own {name}"
    try:
        items = up_mod.reconcile(up_mod.load_config(config_path))
    except (OSError, up_mod.ConfigError):
        return f"warning: could not start {name}'s operator; run: phoning up"
    for item in items:
        if item.get("kind") == "owner" and item.get("name") == name and item.get("state") in ("started", "running"):
            return f"{name}'s operator is running (saved in {config_path}; `phoning up` restarts it)"
    return f"warning: {name}'s operator did not start; run: phoning up"


def _cmd_join(args: argparse.Namespace) -> int:  # noqa: C901, PLR0911, PLR0912
    home = _resolve_home(args.home)
    config_path = _resolve_config(args.config)
    raw = args.text
    if raw == "-":
        try:
            text = sys.stdin.read()
        except OSError:
            text = ""
    else:
        text = raw
    try:
        card = parse_card(text)
    except CardError as exc:
        _err(f"not a valid link: {exc}")
        return EXIT_USAGE
    # The call's relay never needs an access key; one in the environment belongs to some other relay and
    # must not be sent to whoever runs this one (retalk sends RETALK_API_KEY with every request).
    os.environ.pop("RETALK_API_KEY", None)
    if not _wait_reachable(card["relay"], _wait_secs()):
        _err("the link's relay is not answering")
        _hint("ask for a fresh link (the host may have stopped the call)")
        return EXIT_FAILED
    if args.who is not None and _WHO_RE.fullmatch(args.who) is None:
        _err(f"illegal --who label: {args.who}")
        return EXIT_USAGE

    # 2. Local lines.
    names_into, into_err = _parse_into(args.into, home)
    if into_err:
        _err(into_err)
        return EXIT_USAGE
    all_lines = _all_lines(home)
    local: list[str]
    if names_into is not None:
        local = names_into
    elif args.name is not None:
        if not directory_mod.is_legal_name(args.name):
            _err(f"illegal --name: {args.name}")
            return EXIT_USAGE
        local = [args.name]
        if args.name not in all_lines:
            from phoning.cli import init_line  # noqa: PLC0415 - avoid a top-level import cycle

            try:
                os.makedirs(home, mode=0o700, exist_ok=True)
                # Claim the name before init_line writes the relay and identity files.
                os.mkdir(_user_dir(home, args.name), mode=0o700)
                init_line(home, args.name, card["relay"])
            except FileExistsError:
                _err(f"the path for line {args.name} already exists")
                _hint("choose a different --name")
                return EXIT_REFUSED
            except (RetalkError, OSError, subprocess.TimeoutExpired):
                _err(f"could not create line {args.name}")
                return EXIT_FAILED
    elif all_lines:
        local = all_lines
    else:
        _err("no local lines and no --name given")
        _hint("phoning remote join '<link>' --name YOUR-LINE")
        return EXIT_USAGE

    # 3. Own link.
    local_fps: set[str] = set()
    for name in local:
        fp = _line_id(_user_dir(home, name))
        if fp:
            local_fps.add(fp)
    card_fps = {entry["fingerprint"] for entry in card["lines"]}
    if card_fps & local_fps and card_fps.issubset(local_fps):
        _err("this is your own link")
        return EXIT_REFUSED

    # 4. Relay: check every line first, then move.
    cfg = _read_json(config_path) if os.path.exists(config_path) else None
    cfg_dict: dict[str, Any] = cfg if isinstance(cfg, dict) else {}
    moves: list[tuple[str, str, str]] = []
    for name in local:
        udir = _user_dir(home, name)
        line_relay = _line_relay(udir)
        host_public = _host_config_public(cfg_dict, line_relay) if line_relay else []
        if not _relay_matches(line_relay, card["relay"], host_public):
            if args.name is not None:
                _err(f"{name} already uses relay {line_relay}, the link uses {card['relay']}")
                _hint("choose a fresh --name to join without moving an existing line")
                return EXIT_REFUSED
            if names_into is None or args.no_move:
                _err(f"{name} uses relay {line_relay}, the link uses {card['relay']}")
                if args.no_move:
                    _hint("use --name NEW-LINE, or select lines with --into and omit --no-move to move them")
                else:
                    _hint("use --name NEW-LINE, or explicitly select the lines to move with --into")
                return EXIT_REFUSED
            if _has_saved_api_key(udir):
                # Moving would send that relay's saved access key to the call's relay.
                _err(f"{name} has an access key saved for its relay {line_relay}; it can't move to the call")
                _hint("phoning remote join '<link>' --name NEW-LINE")
                return EXIT_REFUSED
            moves.append((name, udir, card["relay"]))
    if moves:
        move_err = _move_lines(home, moves, config_path, quiet=bool(args.json))
        if move_err:
            _err(move_err)
            return EXIT_FAILED

    # 5. Contacts, 6. intros.
    added: list[dict[str, str]] = []
    kept: list[dict[str, str]] = []
    for name in local:
        udir = _user_dir(home, name)
        err = _add_contacts(home, name, udir, card, local_fps, added, kept)
        if err:
            _err(err)
            return EXIT_FAILED

    # 7. Room. One local line (the first) creates or extends the party line with every card line and every
    # other local line, and announces it; the others pick the room up from that announcement. If each line
    # created its own group, retalk would give them different group ids and the host would see two rooms.
    room = card.get("room")
    announced = False
    if isinstance(room, str) and room:
        _link_local_lines(home, local)
        first = local[0]
        room_err, announced = _ensure_room(home, first, _user_dir(home, first), room, card, local)
        if room_err:
            _err(room_err)
            return EXIT_FAILED

    # 8. Retry delivery. Mail a joining line sends before the host's watcher accepts it (the intros and the
    # room status above) is not delivered until the sender re-uploads its outbox; retalk does that on the
    # sender's next send or sync. A short detached loop syncs each line so the host gets the intros (and
    # learns the names) within seconds of accepting, even if nobody here sends anything else.
    if added or announced:
        _spawn_resync(home, local)

    # 9. Ensure the named line has an operator, including when retrying an interrupted join.
    operator_note = ""
    if args.name is not None:
        operator_note = _ensure_operator(config_path, home, args.name, str(card["relay"]))

    # 10. Output.
    if args.json:
        print(
            json.dumps(
                {
                    "added": added,
                    "kept": kept,
                    "moved": [m[0] for m in moves],
                    "room": room if isinstance(room, str) else None,
                }
            )
        )
        return EXIT_OK
    for entry in added:
        print(f"{entry['line']} added {entry['peer']} as {entry['alias']}")
    for entry in kept:
        print(f"{entry['line']} already has {entry['peer']} as {entry['alias']}")
    outsiders = [entry for entry in card["lines"] if entry["fingerprint"] not in local_fps]
    if isinstance(room, str) and room:
        print(
            f'done: on the call "{room}" with {len(outsiders)} lines from {card["who"]}; '
            "their side picks you up within a minute"
        )
    else:
        print("done: every line here can now call every line on the link")
    if operator_note:
        print(operator_note)
    return EXIT_OK


# -- card -------------------------------------------------------------------


def _cmd_card(args: argparse.Namespace) -> int:  # noqa: C901
    home = _resolve_home(args.home)
    config_path = _resolve_config(args.config)
    who = args.who or _who_from_hostname()
    if _WHO_RE.fullmatch(who) is None:
        _err(f"illegal --who label: {who}")
        return EXIT_USAGE
    room = args.room
    if room is not None and not directory_mod.is_legal_name(room):
        _err(f"illegal --room: {room}")
        return EXIT_USAGE
    names = list(args.name) if args.name else _all_lines(home)
    if not names:
        _err("no lines to name in a card")
        _hint("phoning remote host")
        return EXIT_USAGE
    relays: list[str] = []
    for name in names:
        udir = _user_dir(home, name)
        if not os.path.isdir(os.path.join(udir, "identity")):
            _err(f"no identity for {name}")
            return EXIT_USAGE
        relay = _line_relay(udir)
        if relay is None:
            _err(f"no relay recorded for {name}")
            return EXIT_USAGE
        relays.append(relay)
    if len({directory_mod.normalize_relay(r) for r in relays}) > 1:
        _err("lines are on different relays")
        return EXIT_REFUSED
    shared = relays[0]
    url = args.url
    if url is None:
        cfg = _read_json(config_path) if os.path.exists(config_path) else None
        cfg_dict: dict[str, Any] = cfg if isinstance(cfg, dict) else {}
        relay_cfg = cfg_dict.get("relay")
        public: list[str] = []
        config_url: str | None = None
        if isinstance(relay_cfg, dict):
            cur = relay_cfg.get("url")
            if isinstance(cur, str):
                config_url = cur
            pub = relay_cfg.get("public")
            if isinstance(pub, list):
                public = [p for p in pub if isinstance(p, str)]
        if (
            public
            and config_url is not None
            and directory_mod.normalize_relay(config_url) == directory_mod.normalize_relay(shared)
        ):
            url = public[0]
        elif not _is_loopback(shared):
            url = shared
        else:
            _err("no public URL for these lines")
            _hint("phoning remote host")
            return EXIT_USAGE
    lines_out: list[dict[str, Any]] = []
    for name in names:
        udir = _user_dir(home, name)
        fp = _line_id(udir)
        if not fp or _HEX32_RE.fullmatch(fp) is None:
            _err(f"cannot read the fingerprint of {name}")
            return EXIT_FAILED
        harness, role = _card_line_from_dir(udir)
        lines_out.append(
            {
                "name": name,
                "fingerprint": fp,
                "harness": harness,
                "role": role,
                "code": None,
            }
        )
    try:
        link = make_card({"v": 1, "relay": url, "who": who, "room": room, "lines": lines_out})
    except CardError as exc:
        _err(f"cannot build the card: {exc}")
        return EXIT_USAGE
    if args.json:
        print(json.dumps({"card": link, "url": url, "lines": list(names)}))
    else:
        print(link)
    return EXIT_OK


# -- status -----------------------------------------------------------------


def _cmd_status(args: argparse.Namespace) -> int:
    home = _resolve_home(args.home)
    config_path = _resolve_config(args.config)
    state = _state_dir(config_path)
    held = _read_tunnel_state(state)
    tunnel_url = str(held["url"]) if held is not None else None
    watcher_pid = 0
    with contextlib.suppress(ValueError):
        watcher_pid = int(_read_text_line(os.path.join(home, ".remote-watch.pid")))
    watcher = (
        watcher_pid > 0
        and _pid_alive(watcher_pid)
        and "phoning.remote watch" in _pid_command(watcher_pid)
    )
    names = _all_lines(home)
    local_set = set(names)
    lines_out: list[dict[str, Any]] = []
    for name in names:
        udir = _user_dir(home, name)
        relay = _line_relay(udir) or ""
        is_up = bool(relay) and up_mod.relay_up(relay)
        remote = 0
        with contextlib.suppress(RetalkError, OSError, subprocess.TimeoutExpired):
            aliases = Retalk(udir).contacts()
            remote = sum(1 for alias in aliases if alias and alias not in local_set)
        lines_out.append(
            {"name": name, "relay": relay, "up": is_up, "remote_contacts": remote}
        )
    if args.json:
        print(
            json.dumps(
                {"tunnel": tunnel_url, "watcher": bool(watcher), "lines": lines_out}
            )
        )
        return EXIT_OK
    if tunnel_url is None:
        print("tunnel none")
    else:
        # "up" = the cloudflared process is alive. Probing the URL would block: a fresh quick tunnel can
        # take over a minute before its hostname resolves.
        tpid = int(held["pid"]) if held is not None else 0
        state_word = "up" if _pid_alive(tpid) and "cloudflared" in _pid_command(tpid) else "down"
        print(f"tunnel {tunnel_url} {state_word}")
    print("watcher " + ("running" if watcher else "stopped"))
    for item in lines_out:
        up_word = "up" if item["up"] else "down"
        print(
            f"{item['name']}  {item['relay']}  {up_word}  "
            f"{item['remote_contacts']} remote contacts"
        )
    return EXIT_OK


# -- CLI --------------------------------------------------------------------


_HOST_EPILOG: str = (
    "Security: the relay only ever sees ciphertext and public keys, so exposing it "
    "through a tunnel exposes no messages. THE LINK IS A KEY TO YOUR LINES: anyone "
    "holding it can add themselves as a contact of every line on it until "
    "'phoning remote host --stop' revokes the codes. Send it in a DM, never a public "
    "channel. Names learned from intros are cosmetic; authority stays bound to "
    "fingerprints."
)


def _common(p: argparse.ArgumentParser) -> None:
    """Add ``--home``/``--config`` to a subcommand parser."""
    p.add_argument("--home", default=None, help="phoning home (else $PHONING_HOME, ~/.phoning)")
    p.add_argument(
        "--config",
        default=None,
        help="up.json path (else $PHONING_UP_CONFIG, ~/.config/phoning/up.json)",
    )


def _add_join_args(p: argparse.ArgumentParser) -> None:
    """Add the ``join``/``accept`` arguments to a subcommand parser."""
    p.add_argument("text", help="the link (or any text containing it); '-' reads stdin")
    _common(p)
    selection = p.add_mutually_exclusive_group()
    selection.add_argument("--into", help="comma-separated existing lines to join and move if needed")
    selection.add_argument("--name", help="join with a dedicated line, creating it if needed")
    p.add_argument("--no-move", dest="no_move", action="store_true", help="refuse to move lines")
    p.add_argument("--who", default=None, help="label for this computer")
    p.add_argument("--json", dest="json", action="store_true", help="JSON output")
    p.set_defaults(func="join")


def _build_parser() -> argparse.ArgumentParser:
    """Argument parser for ``phoning remote``."""
    parser = argparse.ArgumentParser(
        prog="phoning remote",
        description=(
            "Call across computers: one link, one paste. 'host' opens a tunnel to "
            "this computer's relay and prints the link; the other computer's agent "
            "runs 'join <link>'."
        ),
        epilog=_HOST_EPILOG,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_host = sub.add_parser(
        "host", help="make this relay reachable and print the link", epilog=_HOST_EPILOG
    )
    _common(p_host)
    p_host.add_argument("--url", default=None, help="public URL (else a Cloudflare quick tunnel)")
    p_host.add_argument("--room", default=None, help="room name (default: call)")
    p_host.add_argument("--who", default=None, help="label for this computer")
    p_host.add_argument("--stop", action="store_true", help="stop the tunnel and revoke invite codes")
    p_host.add_argument("--json", dest="json", action="store_true", help="JSON output")
    p_host.set_defaults(func="host")

    _add_join_args(sub.add_parser("join", help="join a call from a link"))
    _add_join_args(sub.add_parser("accept", help="alias of 'join'"))

    p_card = sub.add_parser("card", help="print a link without invite codes")
    _common(p_card)
    p_card.add_argument("name", nargs="*", help="window lines (default: every line)")
    p_card.add_argument("--url", default=None, help="public URL to advertise")
    p_card.add_argument("--room", default=None, help="room name or null")
    p_card.add_argument("--who", default=None, help="label for this computer")
    p_card.add_argument("--json", dest="json", action="store_true", help="JSON output")
    p_card.set_defaults(func="card")

    p_status = sub.add_parser("status", help="show tunnel, watcher and per-line state")
    _common(p_status)
    p_status.add_argument("--json", dest="json", action="store_true", help="JSON output")
    p_status.set_defaults(func="status")

    p_watch = sub.add_parser("watch", help="accept invites and name guests on this computer")
    _common(p_watch)
    p_watch.add_argument("--interval", type=float, default=2.0, help="loop interval seconds")
    p_watch.add_argument("--once", action="store_true", help="one pass and exit")
    p_watch.set_defaults(func="watch")

    p_resync = sub.add_parser("resync", help="(internal) retry delivery of a joining line's first mail")
    _common(p_resync)
    p_resync.add_argument("--lines", required=True, help="comma-separated line names")
    p_resync.add_argument("--seconds", type=float, default=RESYNC_SECONDS, help="how long to keep retrying")
    p_resync.add_argument("--interval", type=float, default=RESYNC_INTERVAL, help="seconds between retries")
    p_resync.add_argument("--once", action="store_true", help="one pass and exit")
    p_resync.set_defaults(func="resync")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for ``phoning remote`` (see docs/spec/remote.md)."""
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    func: str = getattr(args, "func", "")
    try:
        if func == "host":
            return _cmd_host(args)
        if func == "join":
            return _cmd_join(args)
        if func == "card":
            return _cmd_card(args)
        if func == "status":
            return _cmd_status(args)
        if func == "watch":
            return _cmd_watch(args)
        if func == "resync":
            return _cmd_resync(args)
    except RetalkError as exc:
        _err(f"retalk failed (rc={exc.rc})")
        return EXIT_FAILED
    except (OSError, subprocess.TimeoutExpired):
        _err("a subprocess failed")
        return EXIT_FAILED
    parser.print_usage(sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
