"""Profiles and introductions (docs/SPEC-v0.2.md §7.3).

``<user_dir>/profile.json`` holds ``{"role","scope","write_scope","harness"}``
with every field an optional string. The doorbell sends an intro ``status``
on first direct contact; ``phoning intro`` sends one on demand.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from phoning import ops
from phoning.retalk import Retalk
from phoning.text import clean_line

__all__ = ["build_intro", "load_profile", "main", "profile_main", "send_intro"]

PROFILE_FIELDS: tuple[str, ...] = ("role", "scope", "write_scope", "harness")


def _resolve_home(home_arg: str | None) -> str:
    """Resolve ``--home`` else ``$PHONING_HOME`` else ``~/.phoning``."""
    if home_arg:
        return os.path.expanduser(home_arg)
    env = os.environ.get("PHONING_HOME")
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/.phoning")


def _profile_path(user_dir: str) -> str:
    """Filesystem path of ``profile.json`` for ``user_dir``."""
    return os.path.join(user_dir, "profile.json")


def load_profile(user_dir: str) -> dict[str, Any]:
    """Return the stored profile, or ``{}`` when there is none.

    Only the known fields with string values are kept. Missing files,
    invalid JSON and non-dict bodies all yield ``{}``.
    """
    path = _profile_path(user_dir)
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            data: Any = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key in PROFILE_FIELDS:
        val: Any = data.get(key)
        if isinstance(val, str) and val:
            out[key] = val
    return out


def _atomic_write_json(path: str, data: dict[str, Any]) -> None:
    """Write ``data`` to ``path`` atomically with mode 0600."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    with contextlib.suppress(OSError):
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def build_intro(
    name: str, profile: dict[str, Any], fp8: str | None = None
) -> tuple[str, str, dict[str, Any]]:
    """Build ``(subject, body, data)`` for ``name``'s introduction.

    Subject is ``intro: NAME (ROLE)`` or ``intro: NAME``, truncated to 80
    characters with no control characters. Body holds one ``key: value``
    line per set field plus ``fingerprint: FP8`` when known. Data is
    ``{"state": "ACTIVE", "intro": {...profile, "name": NAME}}``.
    """
    clean_name = clean_line(name)
    role_any: Any = profile.get("role")
    role = clean_line(role_any) if isinstance(role_any, str) else ""
    subject = f"intro: {clean_name} ({role})" if role else f"intro: {clean_name}"
    subject = clean_line(subject)[:80]
    lines: list[str] = []
    for key in PROFILE_FIELDS:
        val_any: Any = profile.get(key)
        if isinstance(val_any, str) and val_any:
            cleaned = clean_line(val_any)
            if cleaned:
                lines.append(f"{key}: {cleaned}")
    if isinstance(fp8, str) and fp8:
        lines.append(f"fingerprint: {clean_line(fp8)}")
    body = "\n".join(lines)
    if body:
        body += "\n"
    inner: dict[str, Any] = {"name": name}
    for key in PROFILE_FIELDS:
        val2: Any = profile.get(key)
        if isinstance(val2, str) and val2:
            inner[key] = val2
    data: dict[str, Any] = {"state": "ACTIVE", "intro": inner}
    return subject, body, data


def send_intro(user_dir: str, name: str, to: str, session: str = "doorbell") -> str:
    """Send ``name``'s intro ``status`` to ``to`` and return its eid.

    The fingerprint prefix comes from ``Retalk(user_dir).id()[:8]`` and is
    omitted on any error. Delivery goes through :func:`ops.send_message`.
    """
    profile = load_profile(user_dir)
    fp8: str | None = None
    try:
        full = Retalk(user_dir).id()
        if isinstance(full, str) and full:
            fp8 = full[:8]
    except Exception:
        fp8 = None
    subject, body, data = build_intro(name, profile, fp8)
    return ops.send_message(
        user_dir,
        name,
        session,
        msg_type="status",
        subject=subject,
        body=body,
        to=to,
        data=data,
    )


def _now_iso() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix (unused helper)."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def profile_main(argv: Sequence[str] | None = None) -> int:
    """Back ``phoning profile``: merge flags into the profile and print it."""
    parser = argparse.ArgumentParser(prog="phoning profile", description="Show or update a phoning profile.")
    parser.add_argument("name", help="local principal name")
    parser.add_argument("--home", default=None, help="phoning home dir")
    parser.add_argument("--role", default=None, help="role string")
    parser.add_argument("--scope", default=None, help="scope string")
    parser.add_argument("--write-scope", dest="write_scope", default=None, help="write scope string")
    parser.add_argument("--harness", default=None, help="harness string")
    args = parser.parse_args(list(argv) if argv is not None else None)
    home = _resolve_home(args.home)
    user_dir = os.path.join(home, args.name)
    profile = load_profile(user_dir)
    changed = False
    if args.role is not None:
        profile["role"] = args.role
        changed = True
    if args.scope is not None:
        profile["scope"] = args.scope
        changed = True
    if args.write_scope is not None:
        profile["write_scope"] = args.write_scope
        changed = True
    if args.harness is not None:
        profile["harness"] = args.harness
        changed = True
    if changed:
        os.makedirs(user_dir, mode=0o700, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(user_dir, 0o700)
        _atomic_write_json(_profile_path(user_dir), profile)
        profile = load_profile(user_dir)
    print(json.dumps(profile))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Back ``phoning intro``: send this identity's intro status to a peer."""
    parser = argparse.ArgumentParser(prog="phoning intro", description="Send an intro status.")
    parser.add_argument("name", help="local principal name")
    parser.add_argument("--to", required=True, help="peer alias")
    parser.add_argument("--session", default=None, help="sending session")
    parser.add_argument("--home", default=None, help="phoning home dir")
    args = parser.parse_args(list(argv) if argv is not None else None)
    home = _resolve_home(args.home)
    user_dir = os.path.join(home, args.name)
    session = args.session or os.environ.get("PHONING_SESSION") or "intro"
    eid = send_intro(user_dir, args.name, args.to, session)
    print(json.dumps({"eid": eid}))
    return 0
