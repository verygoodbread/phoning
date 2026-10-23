"""Waker adapters for harnesses that can host a phoning session (docs/spec/providers.md).

A registry of per-harness "waker" adapters so a doorbell can start a turn in a session running on Kimi Code or OpenClaw:

- ``reachable(target)``: what is this session's state and which wake path is open right now?
- ``wake(target, fixed_nudge)``: start a turn in a session that something already hosts.
- ``resume_headless(target, fixed_nudge, cwd)``: start a turn in a session that nothing hosts.

The adapters never invent a session, never act when liveness is unknown, and never put anything but the caller's fixed
nudge into a prompt. Stdlib only; every subprocess uses an argv list, stdin ``/dev/null`` and a timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPResponse
from typing import Any, Protocol

__all__ = [
    "HARNESSES",
    "KIMI_RESUME_TIMEOUT",
    "MAX_NUDGE_CHARS",
    "OPENCLAW_TURN_TIMEOUT",
    "OPENCLAW_WAKE_TIMEOUT",
    "PROBE_TIMEOUT",
    "RECENT_WRITE_SECONDS",
    "WEB_TIMEOUT",
    "KimiWaker",
    "Liveness",
    "OpenClawWaker",
    "Target",
    "Waker",
    "main",
    "make_waker",
    "ring_harness",
    "valid_nudge",
]

HARNESSES: tuple[str, ...] = ("kimi", "openclaw")

PROBE_TIMEOUT: float = 15.0
WEB_TIMEOUT: float = 5.0
KIMI_RESUME_TIMEOUT: float = 900.0
OPENCLAW_WAKE_TIMEOUT: float = 660.0
OPENCLAW_TURN_TIMEOUT: int = 600
RECENT_WRITE_SECONDS: float = 45.0
MAX_NUDGE_CHARS: int = 4000

MAX_RESPONSE_BYTES: int = 1 << 20
_SESSION_ID_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


@dataclass(frozen=True)
class Target:
    """The session a ring should target."""

    harness: str
    session: str
    cwd: str | None = None
    agent: str | None = None


@dataclass(frozen=True)
class Liveness:
    """What is known about a session and which wake path is open."""

    state: str
    path: str
    detail: str


class Waker(Protocol):
    """One harness adapter."""

    name: str

    def reachable(self, target: Target) -> Liveness: ...
    def wake(self, target: Target, fixed_nudge: str) -> str: ...
    def resume_headless(self, target: Target, fixed_nudge: str, cwd: str | None) -> str: ...


def valid_nudge(text: str) -> bool:
    """Return True only for a nudge the adapters may send as a prompt.

    A valid nudge is a ``str`` of length 1..``MAX_NUDGE_CHARS``, contains no
    character below U+0020 (so no newline or tab) and does not start with
    ``-`` (which a harness could read as a flag).
    """
    if not isinstance(text, str):
        return False
    if not text or len(text) > MAX_NUDGE_CHARS:
        return False
    if text.startswith("-"):
        return False
    return all(ord(ch) >= 0x20 for ch in text)


def _valid_session_id(sid: str) -> bool:
    """Return True when ``sid`` is a safe, bounded session id."""
    if not isinstance(sid, str) or not sid or len(sid) > 128:
        return False
    if sid[0] not in _SESSION_ID_OK or sid[0] in "._-":
        return False
    return all(ch in _SESSION_ID_OK for ch in sid)


def _resolve_executable(explicit: str | None, name: str) -> str | None:
    """Return the executable to run: explicit path when usable, else ``which(name)``."""
    if explicit:
        path = os.path.expanduser(explicit)
        try:
            st = os.stat(path)
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode) or not (st.st_mode & 0o111):
            return None
        return path
    return shutil.which(name)


def _kimi_home(explicit: str | None) -> str:
    """Kimi base dir: option else ``$KIMI_CODE_HOME`` else ``~/.kimi-code``."""
    if explicit:
        return os.path.expanduser(explicit)
    env = os.environ.get("KIMI_CODE_HOME")
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/.kimi-code")


def _read_text_limited(resp: HTTPResponse) -> str | None:
    """Read a capped response body, or ``None`` when malformed/too large."""
    try:
        raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except Exception:
        return None
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        return None
    try:
        return raw.decode("utf-8", "replace")
    except Exception:
        return None


def _json_object(text: str | None) -> dict[str, Any] | None:
    """Parse ``text`` as a JSON object, or ``None``."""
    if text is None:
        return None
    try:
        obj: Any = json.loads(text)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _pid_alive(pid: int) -> bool:
    """Return False only when the kernel says ``pid`` does not exist."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _realpath(path: str) -> str:
    """Resolve ``path`` for comparison."""
    return os.path.realpath(os.path.expanduser(path))


# --------------------------------------------------------------------- Kimi


class _KimiInstance:
    """One live ``kimi web`` server found in the instance registry."""

    def __init__(self, host: str, port: int, token: str, pid: int) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.pid = pid

    def _conn(self) -> HTTPConnection:
        return HTTPConnection(self.host, self.port, timeout=WEB_TIMEOUT)

    def get(self, path: str) -> tuple[int, dict[str, Any] | None]:
        """GET ``path`` with bearer auth; return ``(status, json)``."""
        conn = self._conn()
        try:
            conn.request("GET", path, headers={"Authorization": f"Bearer {self.token}"})
            resp = conn.getresponse()
            return resp.status, _json_object(_read_text_limited(resp))
        finally:
            conn.close()

    def post_json(self, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any] | None]:
        """POST JSON to ``path`` with bearer auth; return ``(status, json)``."""
        conn = self._conn()
        try:
            body = json.dumps(payload)
            conn.request(
                "POST",
                path,
                body=body,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                    "Content-Length": str(len(body.encode("utf-8"))),
                },
            )
            resp = conn.getresponse()
            return resp.status, _json_object(_read_text_limited(resp))
        finally:
            conn.close()


class KimiWaker:
    """Adapter for Kimi Code sessions (docs/spec/providers.md section 3)."""

    name = "kimi"

    def __init__(
        self,
        kimi_bin: str | None = None,
        kimi_home: str | None = None,
        ps_bin: str | None = None,
        lsof_bin: str | None = None,
    ) -> None:
        self._kimi_bin = kimi_bin
        self._home = _kimi_home(kimi_home)
        self._ps_bin = ps_bin
        self._lsof_bin = lsof_bin

    # ---------------------------------------------------------- internals

    def _exe(self) -> str | None:
        return _resolve_executable(self._kimi_bin, "kimi")

    def _session_dir(self, sid: str) -> str | None:
        """Newest ``<home>/sessions/*/<sid>`` directory, or ``None``."""
        base = os.path.join(self._home, "sessions")
        try:
            roots = list(os.scandir(base))
        except OSError:
            return None
        newest: str | None = None
        newest_mtime = -1.0
        for root in roots:
            try:
                if not root.is_dir():
                    continue
            except OSError:
                continue
            candidate = os.path.join(root.path, sid)
            state = os.path.join(candidate, "state.json")
            try:
                st = os.stat(state)
            except OSError:
                continue
            if newest is None or st.st_mtime > newest_mtime:
                newest, newest_mtime = candidate, st.st_mtime
        return newest

    def _session_cwd(self, sdir: str) -> str | None:
        """Authoritative session cwd from ``state.json``, or ``None``."""
        try:
            with open(os.path.join(sdir, "state.json"), encoding="utf-8") as fh:
                data: Any = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        cwd: Any = data.get("cwd")
        if isinstance(cwd, str) and cwd:
            return cwd
        return None

    def _token(self) -> str | None:
        """Token from ``<home>/server.token`` when it is a 0600 regular file."""
        path = os.path.join(self._home, "server.token")
        try:
            st = os.lstat(path)
        except OSError:
            return None
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
            return None
        if st.st_mode & 0o077:
            return None
        try:
            with open(path, encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError:
            return None
        return token or None

    def _instances(self) -> tuple[list[_KimiInstance], str | None]:
        """Live web instances plus an optional ``unknown`` reason."""
        base = os.path.join(self._home, "server", "instances")
        try:
            names = sorted(os.listdir(base))
        except OSError:
            return [], None
        token: str | None = None
        token_loaded = False
        instances: list[_KimiInstance] = []
        for name in names:
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(base, name), encoding="utf-8") as fh:
                    data: Any = json.load(fh)
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            pid: Any = data.get("pid")
            port: Any = data.get("port")
            if not isinstance(pid, int) or isinstance(pid, bool):
                continue
            if not isinstance(port, int) or isinstance(port, bool):
                continue
            if not _pid_alive(pid):
                continue
            host: Any = data.get("host")
            if not isinstance(host, str) or host not in _LOOPBACK_HOSTS:
                return [], "web server not on loopback"
            if not token_loaded:
                token = self._token()
                token_loaded = True
            if token is None:
                return [], "web token unusable"
            instances.append(_KimiInstance(host, port, token, pid))
        return instances, None

    def _web_status(
        self, instances: list[_KimiInstance], sid: str
    ) -> tuple[Liveness | None, _KimiInstance | None]:
        """Probe each instance for subscription/busy; return a decision or the subscriber."""
        subscribed: _KimiInstance | None = None
        busy_found = False
        for inst in instances:
            try:
                code, conns = inst.get("/api/v1/connections")
            except Exception:
                return Liveness("unknown", "none", "web server unreachable"), None
            if code != 200 or conns is None or conns.get("code") != 0:
                return Liveness("unknown", "none", "web server unreachable"), None
            data: Any = conns.get("data")
            connections: Any = data.get("connections") if isinstance(data, dict) else None
            if isinstance(connections, list):
                for conn in connections:
                    if not isinstance(conn, dict):
                        continue
                    subs: Any = conn.get("subscriptions")
                    if isinstance(subs, list) and sid in subs:
                        subscribed = inst
                        break
            if subscribed is not None:
                break
            try:
                scode, status = inst.get(f"/api/v1/sessions/{sid}/status")
            except Exception:
                return Liveness("unknown", "none", "web server unreachable"), None
            if scode != 200 or status is None or status.get("code") != 0:
                return Liveness("unknown", "none", "web server unreachable"), None
            sdata: Any = status.get("data")
            if isinstance(sdata, dict) and sdata.get("busy") is True:
                busy_found = True
        if subscribed is not None:
            return Liveness("live", "web", "ui subscribed"), subscribed
        if busy_found:
            return Liveness("live", "none", "busy in web server"), None
        return None, None

    def _ps_lines(self) -> list[str] | None:
        """Run ``ps -axo pid=,comm=``; return its lines or ``None`` on failure."""
        exe = self._ps_bin or shutil.which("ps")
        if not exe:
            return None
        try:
            done = subprocess.run(
                [exe, "-axo", "pid=,comm="],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=PROBE_TIMEOUT,
            )
        except Exception:
            return None
        if done.returncode != 0:
            return None
        out: Any = done.stdout
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        return text.splitlines()

    def _process_cwd(self, pid: int) -> str | None:
        """Cwd of ``pid``; ``None`` means unreadable."""
        proc = f"/proc/{pid}/cwd"
        if os.path.exists(proc):
            try:
                return os.readlink(proc)
            except OSError:
                return None
        exe = self._lsof_bin or shutil.which("lsof")
        if not exe:
            return None
        try:
            done = subprocess.run(
                [exe, "-a", "-d", "cwd", "-Fn", "-p", str(pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=PROBE_TIMEOUT,
            )
        except Exception:
            return None
        out: Any = done.stdout
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        for line in text.splitlines():
            if line.startswith("n") and len(line) > 1:
                return line[1:]
        return None

    # ------------------------------------------------------------- probe

    def _probe(self, target: Target) -> tuple[Liveness, _KimiInstance | None, str | None]:
        """Return ``(liveness, subscribed instance, session cwd)``."""
        sid = target.session
        if not _valid_session_id(sid):
            return Liveness("absent", "none", "bad session id"), None, None
        sdir = self._session_dir(sid)
        if sdir is None:
            return Liveness("absent", "none", "no session file"), None, None
        session_cwd = self._session_cwd(sdir)
        if session_cwd is None:
            return Liveness("unknown", "none", "session file unreadable"), None, None

        instances, inst_err = self._instances()
        if inst_err is not None:
            return Liveness("unknown", "none", inst_err), None, session_cwd
        web_decision, subscribed = self._web_status(instances, sid)

        scanner, scan_err = self._scan_processes(instances, session_cwd)
        if scan_err is not None:
            return Liveness("unknown", "none", scan_err), None, session_cwd
        if scanner is not None:
            return scanner, None, session_cwd

        wire = os.path.join(sdir, "agents", "main", "wire.jsonl")
        try:
            mtime = os.stat(wire).st_mtime
        except FileNotFoundError:
            mtime = None
        except OSError:
            return Liveness("unknown", "none", "session file unreadable"), None, session_cwd
        if mtime is not None and (time.time() - mtime) < RECENT_WRITE_SECONDS:
            return Liveness("live", "none", "recent session write"), None, session_cwd

        if web_decision is not None:
            return web_decision, subscribed, session_cwd
        return Liveness("dead", "headless", "no live kimi found"), None, session_cwd

    def _scan_processes(
        self, instances: list[_KimiInstance], session_cwd: str
    ) -> tuple[Liveness | None, str | None]:
        """Look for a live ``kimi`` process in the session cwd."""
        lines = self._ps_lines()
        if lines is None:
            return None, "process scan failed"
        me = os.getpid()
        excluded = {inst.pid for inst in instances}
        for line in lines:
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            pid_raw, comm = parts[0], parts[1].strip()
            try:
                pid = int(pid_raw)
            except ValueError:
                continue
            if pid == me or pid in excluded:
                continue
            base = os.path.basename(comm)
            if base not in ("kimi", "kimi-code"):
                continue
            cwd = self._process_cwd(pid)
            if cwd is None:
                return None, "cannot read process cwd"
            if _realpath(cwd) == _realpath(session_cwd):
                return Liveness("live", "none", "kimi process in session cwd"), None
        return None, None

    # ------------------------------------------------------------ public

    def reachable(self, target: Target) -> Liveness:
        """Return the session's state and open wake path; never raises."""
        try:
            live, _, _ = self._probe(target)
            return live
        except Exception:
            return Liveness("unknown", "none", "probe failed")

    def wake(self, target: Target, fixed_nudge: str) -> str:
        """Start a turn through the Kimi web REST API; never raises."""
        try:
            if not valid_nudge(fixed_nudge):
                return "ring failed: invalid nudge"
            live, subscribed, _ = self._probe(target)
            if live.path == "web" and subscribed is not None:
                payload = {"content": [{"type": "text", "text": fixed_nudge}]}
                try:
                    code, resp = subscribed.post_json(
                        f"/api/v1/sessions/{target.session}/prompts", payload
                    )
                except Exception:
                    return "ring failed: web unreachable"
                if code == 200 and resp is not None and resp.get("code") == 0:
                    return "rang: web"
                if code == 200 and resp is not None:
                    env_code: Any = resp.get("code")
                    if isinstance(env_code, int) and not isinstance(env_code, bool):
                        return f"ring failed: web code={env_code}"
                return f"ring failed: web http={code}"
            return self._skip_line(live)
        except Exception as exc:
            return f"ring failed: {exc.__class__.__name__}"

    def _skip_line(self, live: Liveness) -> str:
        """The fixed skip line for a liveness verdict that cannot be acted on."""
        if live.state == "absent":
            return "skip: absent: no session"
        if live.state == "unknown":
            return "skip: liveness unknown"
        if live.state == "live":
            return (
                "skip: live (kimi sessions outside a server cannot be woken; "
                "the hook delivers on the next prompt)"
            )
        return "skip: dead (use resume_headless)"

    def resume_headless(self, target: Target, fixed_nudge: str, cwd: str | None) -> str:
        """Resume a provably dead Kimi session headlessly; never raises."""
        try:
            if not valid_nudge(fixed_nudge):
                return "ring failed: invalid nudge"
            live, _, session_cwd = self._probe(target)
            if live.state == "absent":
                return "skip: absent: no session"
            if live.state == "unknown":
                return "skip: liveness unknown"
            if live.state == "live" or live.path != "headless":
                return "skip: live (not resuming a session something may hold)"
            want = cwd if cwd is not None else target.cwd
            if (
                want is not None
                and session_cwd is not None
                and _realpath(want) != _realpath(session_cwd)
            ):
                return "skip: cwd mismatch"
            exe = self._exe()
            if exe is None:
                return "ring failed: no kimi executable"
            env = dict(os.environ)
            env["KIMI_CODE_HOME"] = self._home
            try:
                done = subprocess.run(
                    [exe, "-S", target.session, "-p", fixed_nudge],
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=KIMI_RESUME_TIMEOUT,
                    cwd=session_cwd,
                    env=env,
                )
            except subprocess.TimeoutExpired:
                return "ring failed: timed out"
            except Exception as exc:
                return f"ring failed: {exc.__class__.__name__}"
            return f"resumed: rc={done.returncode}"
        except Exception as exc:
            return f"ring failed: {exc.__class__.__name__}"


# ----------------------------------------------------------------- OpenClaw


def _gateway_state(payload: object) -> str:
    """Classify one parsed ``health`` payload: ``up``/``down``/``unknown``."""
    if not isinstance(payload, dict):
        return "unknown"
    if payload.get("ok") is True:
        return "up"
    err: Any = payload.get("error")
    if (
        isinstance(err, dict)
        and err.get("type") == "gateway_transport_error"
        and err.get("kind") == "closed"
    ):
        return "down"
    return "unknown"


class OpenClawWaker:
    """Adapter for OpenClaw sessions (docs/spec/providers.md section 4)."""

    name = "openclaw"

    def __init__(self, openclaw_bin: str | None = None) -> None:
        self._openclaw_bin = openclaw_bin

    def _exe(self) -> str | None:
        return _resolve_executable(self._openclaw_bin, "openclaw")

    def _run(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess[bytes] | None:
        """Run ``argv`` with the process rules; ``None`` on any failure."""
        try:
            return subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
            )
        except Exception:
            return None

    def _lookup(
        self, exe: str, target: Target
    ) -> tuple[str, tuple[str, str, str] | None]:
        """Return ``(verdict, (agent_id, session_id, status))``.

        Verdict is ``found``, ``absent`` or ``unknown``. The match is by
        ``sessionId`` or ``key``; ``target.agent`` narrows it.
        """
        done = self._run(
            [exe, "sessions", "--json", "--all-agents", "--limit", "all"], PROBE_TIMEOUT
        )
        if done is None or done.returncode != 0:
            return "unknown", None
        out: Any = done.stdout
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        obj = _json_object(text)
        if obj is None:
            return "unknown", None
        sessions: Any = obj.get("sessions")
        if not isinstance(sessions, list):
            return "unknown", None
        matches: list[tuple[str, str, str, str]] = []
        for entry in sessions:
            if not isinstance(entry, dict):
                continue
            sid: Any = entry.get("sessionId")
            key: Any = entry.get("key")
            agent: Any = entry.get("agentId")
            status: Any = entry.get("status")
            if not isinstance(sid, str) or not sid:
                continue
            if target.agent is not None and agent != target.agent:
                continue
            if sid == target.session or (isinstance(key, str) and key == target.session):
                matches.append(
                    (
                        agent if isinstance(agent, str) else "",
                        sid,
                        status if isinstance(status, str) else "",
                        key if isinstance(key, str) else "",
                    )
                )
        if not matches:
            return "absent", None
        uniq = {(m[0], m[1], m[3]) for m in matches}
        if len(uniq) > 1:
            return "unknown", None
        first = matches[0]
        return "found", (first[0], first[1], first[2])

    def _health(self, exe: str) -> str:
        """Classify ``openclaw health``: ``up``/``down``/``unknown``."""
        done = self._run([exe, "health", "--json"], PROBE_TIMEOUT)
        if done is None:
            return "unknown"
        out: Any = done.stdout
        text = out.decode("utf-8", "replace") if isinstance(out, bytes) else str(out)
        obj = _json_object(text)
        if obj is None:
            return "unknown"
        if obj.get("ok") is True and done.returncode == 0:
            return "up"
        if (
            done.returncode != 0
            and isinstance(obj.get("error"), dict)
            and obj["error"].get("type") == "gateway_transport_error"
            and obj["error"].get("kind") == "closed"
        ):
            return "down"
        return "unknown"

    def reachable(self, target: Target) -> Liveness:
        """Return the session's state and open wake path; never raises."""
        try:
            exe = self._exe()
            if exe is None:
                return Liveness("unknown", "none", "no executable")
            verdict, match = self._lookup(exe, target)
            if verdict == "absent":
                return Liveness("absent", "none", "no such session")
            if verdict == "unknown":
                return Liveness("unknown", "none", "session lookup failed")
            assert match is not None
            health = self._health(exe)
            if health == "unknown":
                return Liveness("unknown", "none", "gateway state unknown")
            if health == "down":
                return Liveness("dead", "headless", "gateway down")
            if match[2] == "running":
                return Liveness("live", "gateway", "run in flight")
            return Liveness("idle", "gateway", "gateway up")
        except Exception:
            return Liveness("unknown", "none", "probe failed")

    def wake(self, target: Target, fixed_nudge: str) -> str:
        """Start a turn through the OpenClaw gateway; never raises."""
        try:
            if not valid_nudge(fixed_nudge):
                return "ring failed: invalid nudge"
            exe = self._exe()
            if exe is None:
                return "ring failed: no openclaw executable"
            verdict, match = self._lookup(exe, target)
            if verdict == "absent":
                return "skip: absent: no session"
            if verdict == "unknown":
                return "skip: liveness unknown"
            assert match is not None
            health = self._health(exe)
            if health == "unknown":
                return "skip: liveness unknown"
            if health == "down":
                return "skip: gateway down (use resume_headless)"
            agent, sid, _status = match
            argv = [
                exe,
                "agent",
                "--agent",
                agent,
                "--session-id",
                sid,
                "--message",
                fixed_nudge,
                "--json",
                "--timeout",
                str(OPENCLAW_TURN_TIMEOUT),
            ]
            try:
                done = subprocess.run(
                    argv,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    timeout=OPENCLAW_WAKE_TIMEOUT,
                )
            except subprocess.TimeoutExpired:
                return "ring failed: timed out"
            except Exception as exc:
                return f"ring failed: {exc.__class__.__name__}"
            if done.returncode == 0:
                return "rang: gateway"
            return f"ring failed: rc={done.returncode}"
        except Exception as exc:
            return f"ring failed: {exc.__class__.__name__}"

    def resume_headless(self, target: Target, fixed_nudge: str, cwd: str | None) -> str:
        """Resume an OpenClaw session locally when the gateway is proven down; never raises."""
        try:
            if not valid_nudge(fixed_nudge):
                return "ring failed: invalid nudge"
            exe = self._exe()
            if exe is None:
                return "ring failed: no openclaw executable"
            verdict, match = self._lookup(exe, target)
            if verdict == "absent":
                return "skip: absent: no session"
            if verdict == "unknown":
                return "skip: liveness unknown"
            assert match is not None
            health = self._health(exe)
            if health == "unknown":
                return "skip: liveness unknown"
            if health == "up":
                return "skip: live (gateway up; use wake)"
            agent, sid, _status = match
            argv = [
                exe,
                "agent",
                "--local",
                "--agent",
                agent,
                "--session-id",
                sid,
                "--message",
                fixed_nudge,
                "--json",
                "--timeout",
                str(OPENCLAW_TURN_TIMEOUT),
            ]
            run_cwd: str | None = cwd if (cwd and os.path.isdir(cwd)) else None
            try:
                if run_cwd is not None:
                    done = subprocess.run(
                        argv,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=OPENCLAW_WAKE_TIMEOUT,
                        cwd=run_cwd,
                    )
                else:
                    done = subprocess.run(
                        argv,
                        stdin=subprocess.DEVNULL,
                        capture_output=True,
                        timeout=OPENCLAW_WAKE_TIMEOUT,
                    )
            except subprocess.TimeoutExpired:
                return "ring failed: timed out"
            except Exception as exc:
                return f"ring failed: {exc.__class__.__name__}"
            return f"resumed: rc={done.returncode}"
        except Exception as exc:
            return f"ring failed: {exc.__class__.__name__}"


# ---------------------------------------------------------------- registry


def make_waker(harness: str, **options: Any) -> Waker:
    """Return a waker for ``harness``; ``ValueError`` on an unknown harness."""
    if harness == "kimi":
        return KimiWaker(**options)
    if harness == "openclaw":
        return OpenClawWaker(**options)
    raise ValueError(f"unknown harness: {harness}")


def _skip_for(live: Liveness, waker: Waker) -> str:
    """The skip line the waker's ``wake`` would give for a non-actionable path."""
    if live.state == "absent":
        return "skip: absent: no session"
    if live.state == "unknown":
        return "skip: liveness unknown"
    if live.state == "dead" and live.path == "headless":
        if waker.name == "openclaw":
            return "skip: gateway down (use resume_headless)"
        return "skip: dead (use resume_headless)"
    if waker.name == "kimi":
        return (
            "skip: live (kimi sessions outside a server cannot be woken; "
            "the hook delivers on the next prompt)"
        )
    return "skip: live (gateway up; use wake)"


def ring_harness(
    harness: str,
    session: str,
    nudge: str,
    *,
    resume_dead: bool = False,
    cwd: str | None = None,
    agent: str | None = None,
    **options: Any,
) -> str:
    """Ring one harness session; return a single result line and never raise."""
    try:
        if harness not in HARNESSES:
            return "ring failed: unknown harness"
        if not valid_nudge(nudge):
            return "ring failed: invalid nudge"
        waker = make_waker(harness, **options)
        target = Target(harness, session, cwd=cwd, agent=agent)
        live = waker.reachable(target)
        if live.path in ("web", "gateway"):
            return waker.wake(target, nudge)
        if live.path == "headless":
            if not resume_dead:
                return "skip: dead (resume_dead not set)"
            return waker.resume_headless(target, nudge, cwd)
        return _skip_for(live, waker)
    except Exception as exc:
        return f"ring failed: {exc.__class__.__name__}"


# ------------------------------------------------------------------- main


def main(argv: Sequence[str] | None = None) -> int:
    """Back ``phoning reach``: print one JSON object describing a session; read-only."""
    parser = argparse.ArgumentParser(
        prog="phoning reach", description="Report a harness session's reachability."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    reach = sub.add_parser("reach", help="print one JSON object")
    reach.add_argument("--harness", required=True, choices=list(HARNESSES))
    reach.add_argument("--session", required=True)
    reach.add_argument("--cwd", default=None)
    reach.add_argument("--agent", default=None)
    reach.add_argument("--kimi-bin", dest="kimi_bin", default=None)
    reach.add_argument("--kimi-home", dest="kimi_home", default=None)
    reach.add_argument("--ps-bin", dest="ps_bin", default=None)
    reach.add_argument("--lsof-bin", dest="lsof_bin", default=None)
    reach.add_argument("--openclaw-bin", dest="openclaw_bin", default=None)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command != "reach":
        return 2
    options: dict[str, Any] = {}
    if args.harness == "kimi":
        if args.kimi_bin is not None:
            options["kimi_bin"] = args.kimi_bin
        if args.kimi_home is not None:
            options["kimi_home"] = args.kimi_home
        if args.ps_bin is not None:
            options["ps_bin"] = args.ps_bin
        if args.lsof_bin is not None:
            options["lsof_bin"] = args.lsof_bin
    else:
        if args.openclaw_bin is not None:
            options["openclaw_bin"] = args.openclaw_bin
    waker = make_waker(args.harness, **options)
    target = Target(args.harness, args.session, cwd=args.cwd, agent=args.agent)
    live = waker.reachable(target)
    obj = {
        "harness": args.harness,
        "session": args.session,
        "state": live.state,
        "path": live.path,
        "detail": live.detail,
    }
    sys.stdout.write(json.dumps(obj) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
