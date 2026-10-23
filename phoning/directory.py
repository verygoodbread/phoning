"""Same-machine onboarding via a shared local directory.

The directory is a **local, same-user** convenience, not a trust root.
Cards live under ``$PHONING_DIRECTORY`` (else ``~/.config/phoning/directory``)
with mode 0700 and each card mode 0600. A card is only a claim; the thing
that makes a contact trustworthy is ``retalk add --verify`` against the
relay. ``--mutual`` writes into another identity's contacts only by local
file ownership: both identity directories belong to the user running the
command. It is never a cross-user or cross-machine mechanism.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Sequence
from typing import Any, Final

from phoning.retalk import Retalk, RetalkError
from phoning.text import clean_line

_NAME_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_FP_RE: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{32}")


def is_legal_name(name: str) -> bool:
    """Return True when ``name`` matches the principal-name grammar."""
    return _NAME_RE.fullmatch(name) is not None


def is_legal_fingerprint(fp: str) -> bool:
    """Return True when ``fp`` is exactly 32 lowercase hex characters."""
    return _FP_RE.fullmatch(fp) is not None


def normalize_relay(url: str) -> str:
    """Compare relays after stripping whitespace and trailing slashes."""
    return url.strip().rstrip("/")


def is_well_formed_card(obj: Any) -> bool:
    """Return True when ``obj`` has the card shape (fingerprint unchecked)."""
    if not isinstance(obj, dict):
        return False
    card: dict[str, Any] = obj
    for key in ("name", "fingerprint", "relay", "harness", "role", "published_at"):
        if key not in card:
            return False
    name = card["name"]
    if not isinstance(name, str) or not is_legal_name(name):
        return False
    if not isinstance(card["fingerprint"], str):
        return False
    relay = card["relay"]
    if not isinstance(relay, str) or relay == "":
        return False
    harness = card["harness"]
    if harness is not None and not isinstance(harness, str):
        return False
    role = card["role"]
    if role is not None and not isinstance(role, str):
        return False
    return isinstance(card["published_at"], str)


def is_valid_card(obj: Any) -> bool:
    """Return True when ``obj`` is well-formed with a legal fingerprint."""
    if not is_well_formed_card(obj):
        return False
    card: dict[str, Any] = obj
    fp = card["fingerprint"]
    return isinstance(fp, str) and is_legal_fingerprint(fp)


def _resolve_home(explicit: str | None) -> str:
    """Return the phoning home dir (explicit flag, env, then default)."""
    if explicit is not None:
        return explicit
    env = os.environ.get("PHONING_HOME")
    if env:
        return env
    return os.path.expanduser("~/.phoning")


def _resolve_directory() -> str:
    """Return the card directory (env, then default)."""
    env = os.environ.get("PHONING_DIRECTORY")
    if env:
        return os.path.expanduser(env)
    return os.path.expanduser("~/.config/phoning/directory")


def _err(msg: str) -> None:
    """Print one ``error:`` line to stderr."""
    flat = " ".join(msg.split())
    print(f"error: {flat}", file=sys.stderr)


def _utc_now_z() -> str:
    """Return current UTC time as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_card(obj: dict[str, Any]) -> dict[str, Any]:
    """Return the six known card keys (extra keys dropped)."""
    return {
        "name": obj["name"],
        "fingerprint": obj["fingerprint"],
        "relay": obj["relay"],
        "harness": obj["harness"],
        "role": obj["role"],
        "published_at": obj["published_at"],
    }


def _read_card_file(path: str) -> tuple[dict[str, Any] | None, str | None]:
    """Read and parse a card file.

    Return ``(card, error)`` where exactly one element is not None.
    """
    try:
        with open(path, encoding="utf-8") as fh:  # noqa: PTH123
            raw = fh.read()
    except OSError as exc:
        return None, f"cannot read card: {exc.strerror or 'unreadable'}"
    try:
        obj: Any = json.loads(raw)
    except json.JSONDecodeError:
        return None, "invalid card JSON"
    if not isinstance(obj, dict):
        return None, "invalid card JSON"
    card: dict[str, Any] = obj
    return card, None


def _first_bad_ownership_path(paths: Sequence[str]) -> str | None:
    """Return the first path that is not provably owned+writable, else None."""
    try:
        uid = os.getuid()
    except AttributeError:  # pragma: no cover - non-POSIX
        return paths[0] if paths else None
    for path in paths:
        try:
            lst = os.lstat(path)
            if stat.S_ISLNK(lst.st_mode):
                return path
            if not stat.S_ISDIR(lst.st_mode):
                return path
            st = os.stat(path)
            if st.st_uid != uid:
                return path
            if not os.access(path, os.W_OK):
                return path
        except OSError:
            return path
    return None


def _cmd_publish(args: argparse.Namespace) -> int:
    name = str(args.name)
    harness: str | None = args.harness
    role: str | None = args.role
    home = _resolve_home(args.home if isinstance(args.home, str) else None)
    if not is_legal_name(name):
        _err(f"illegal name: {name}")
        return 2
    user_dir = os.path.join(home, name)
    identity_dir = os.path.join(user_dir, "identity")
    if not os.path.isdir(identity_dir):
        _err(f"no identity for {name}")
        return 2
    client = Retalk(user_dir)
    try:
        fp = client.id()
    except RetalkError as exc:
        _err(f"retalk id failed (rc={exc.rc})")
        return 1
    except (OSError, subprocess.TimeoutExpired):
        _err("retalk id failed")
        return 1
    if not is_legal_fingerprint(fp):
        _err("retalk returned an invalid fingerprint")
        return 1
    relay = client.relay
    if relay is None:
        _err(f"no relay recorded for {name}")
        return 2
    directory = _resolve_directory()
    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    except OSError:
        _err(f"cannot create directory {directory}")
        return 1
    card: dict[str, Any] = {
        "name": name,
        "fingerprint": fp,
        "relay": relay,
        "harness": harness,
        "role": role,
        "published_at": _utc_now_z(),
    }
    card_path = os.path.join(directory, f"{name}.json")
    tmp_path = f"{card_path}.tmp-{os.getpid()}"
    with contextlib.suppress(OSError):
        os.unlink(tmp_path)
    try:
        fd = os.open(tmp_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        _err(f"cannot write card for {name}")
        return 1
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(card) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, card_path)
        os.chmod(card_path, 0o600)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        _err(f"cannot write card for {name}")
        return 1
    print(json.dumps(card))
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    want_json: bool = bool(args.json)
    directory = _resolve_directory()
    if not os.path.isdir(directory):
        return 0
    try:
        entries = sorted(os.listdir(directory))
    except OSError:
        _err(f"cannot read directory {directory}")
        return 1
    cards: list[dict[str, Any]] = []
    for fname in entries:
        if not fname.endswith(".json"):
            continue
        stem = fname[:-5]
        path = os.path.join(directory, fname)
        card, _error = _read_card_file(path)
        if card is None or not is_valid_card(card) or card.get("name") != stem:
            print(f"skipped: {fname}", file=sys.stderr)
            continue
        cards.append(_canonical_card(card))
    cards.sort(key=lambda c: str(c["name"]))
    for card in cards:
        if want_json:
            print(json.dumps(card))
        else:
            harness = card["harness"]
            role = card["role"]
            h_str = harness if isinstance(harness, str) else "-"
            r_str = role if isinstance(role, str) else "-"
            fp = str(card["fingerprint"])
            line = f"{card['name']}  {fp[:8]}  {h_str}  {r_str}  {card['relay']}"
            print(clean_line(line))
    return 0


def _cmd_add(args: argparse.Namespace) -> int:  # noqa: C901, PLR0912
    name = str(args.name)
    peer = str(args.peer)
    alias = str(args.alias) if args.alias is not None else peer
    mutual_alias = str(args.mutual_alias) if args.mutual_alias is not None else name
    home = _resolve_home(args.home if isinstance(args.home, str) else None)
    peer_home_raw = args.peer_home
    peer_home: str | None = peer_home_raw if isinstance(peer_home_raw, str) else None
    mutual: bool = bool(args.mutual)

    for candidate in (name, peer, alias):
        if not is_legal_name(candidate):
            _err(f"illegal name: {candidate}")
            return 2
    if args.mutual_alias is not None and not is_legal_name(mutual_alias):
        _err(f"illegal name: {mutual_alias}")
        return 2
    if args.alias is not None and not is_legal_name(alias):
        _err(f"illegal name: {alias}")
        return 2
    if name == peer:
        _err(f"cannot add {name} to itself")
        return 2
    user_dir = os.path.join(home, name)
    if not os.path.isdir(os.path.join(user_dir, "identity")):
        _err(f"no identity for {name}")
        return 2
    directory = _resolve_directory()
    card_path = os.path.join(directory, f"{peer}.json")
    if not os.path.isfile(card_path):
        _err(f"no card for {peer}")
        return 2
    card, _read_error = _read_card_file(card_path)
    if card is None or not is_well_formed_card(card):
        _err(f"invalid card for {peer}")
        return 2
    if card["name"] != peer:
        _err(f"card name mismatch: expected {peer}")
        return 3
    client = Retalk(user_dir)
    name_relay = client.relay
    if name_relay is None:
        _err(f"no relay recorded for {name}")
        return 2
    card_relay = str(card["relay"])
    if normalize_relay(card_relay) != normalize_relay(name_relay):
        _err(f"relay mismatch for {peer}")
        return 3
    card_fp = str(card["fingerprint"])
    if not is_legal_fingerprint(card_fp):
        _err(f"malformed fingerprint in card for {peer}")
        return 3
    try:
        name_fp = client.id()
    except RetalkError as exc:
        _err(f"retalk id failed (rc={exc.rc})")
        return 1
    except (OSError, subprocess.TimeoutExpired):
        _err("retalk id failed")
        return 1
    if not is_legal_fingerprint(name_fp):
        _err("retalk returned an invalid fingerprint")
        return 1
    if card_fp == name_fp:
        _err("that is your own fingerprint")
        return 3

    peer_user_dir: str | None = None
    peer_fp: str | None = None
    if mutual:
        if peer_home is None:
            _err("--mutual needs --peer-home")
            return 2
        peer_user_dir = os.path.join(peer_home, peer)
        if not os.path.isdir(os.path.join(peer_user_dir, "identity")):
            _err(f"no identity for {peer}")
            return 2
        bad = _first_bad_ownership_path(
            [user_dir, os.path.join(user_dir, "identity"), peer_user_dir,
             os.path.join(peer_user_dir, "identity")]
        )
        if bad is not None:
            _err(
                "--mutual refused: consent is by local file ownership "
                f"and I cannot show it for {bad}"
            )
            return 3
        peer_client = Retalk(peer_user_dir)
        try:
            peer_fp = peer_client.id()
        except RetalkError as exc:
            _err(f"retalk id failed (rc={exc.rc})")
            return 1
        except (OSError, subprocess.TimeoutExpired):
            _err("retalk id failed")
            return 1
        if peer_fp != card_fp:
            _err("peer fingerprint does not match its card")
            return 3
        peer_relay = peer_client.relay
        if peer_relay is None or normalize_relay(peer_relay) != normalize_relay(name_relay):
            _err(f"relay mismatch for {peer}")
            return 3

    try:
        client.add(card_fp, alias)
    except RetalkError as exc:
        _err(f"retalk add failed (rc={exc.rc}); the contact may be saved but unverified")
        return 1
    except (OSError, subprocess.TimeoutExpired):
        _err("retalk add failed (rc=1); the contact may be saved but unverified")
        return 1
    if not mutual:
        print(f"added {peer} as {alias} to {name} (verified)")
        return 0
    assert peer_user_dir is not None
    assert peer_fp is not None
    peer_client2 = Retalk(peer_user_dir)
    try:
        peer_client2.add(name_fp, mutual_alias)
    except RetalkError as exc:
        print(f"added {peer} as {alias} to {name} (verified)")
        print(f"{peer} was added to {name}; {name} was NOT added to {peer}")
        _err(f"retalk add failed (rc={exc.rc}); the contact may be saved but unverified")
        return 1
    except (OSError, subprocess.TimeoutExpired):
        print(f"added {peer} as {alias} to {name} (verified)")
        print(f"{peer} was added to {name}; {name} was NOT added to {peer}")
        _err("retalk add failed (rc=1); the contact may be saved but unverified")
        return 1
    print(f"added {peer} as {alias} to {name} (verified)")
    print(f"added {name} as {mutual_alias} to {peer} (verified)")
    print("mutual add by local file ownership: same user owns both homes")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="phoning directory",
        description=(
            "Same-machine onboarding via a local card directory. "
            "The directory is a same-user convenience, not a trust root; "
            "contacts are only trusted after retalk add --verify. "
            "--mutual writes into another identity's contacts only by local "
            "file ownership (both homes owned by the current user); it is "
            "never a cross-user or cross-machine mechanism."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    pub = sub.add_parser("publish", help="Publish this machine's card.")
    pub.add_argument("name", help="Principal name to publish.")
    pub.add_argument("--harness", default=None, help="Harness label.")
    pub.add_argument("--role", default=None, help="Role label.")
    pub.add_argument("--home", default=None, help="Phoning home dir.")

    lst = sub.add_parser("list", help="List directory cards.")
    lst.add_argument("--json", dest="json", action="store_true", help="One JSON object per line.")

    add = sub.add_parser("add", help="Add a peer from its card (verified).")
    add.add_argument("name", help="Local principal adding the peer.")
    add.add_argument("peer", help="Peer card name to add.")
    add.add_argument("--as", dest="alias", default=None, help="Alias for the peer.")
    add.add_argument("--home", default=None, help="Phoning home dir.")
    add.add_argument("--peer-home", dest="peer_home", default=None, help="Peer phoning home for --mutual.")
    add.add_argument(
        "--mutual",
        action="store_true",
        help=(
            "Also add NAME to PEER's contacts, only by local file ownership "
            "(both homes owned by the current user); never cross-user."
        ),
    )
    add.add_argument("--mutual-alias", dest="mutual_alias", default=None, help="Alias for NAME on PEER side.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Dispatch ``publish``, ``list`` and ``add`` subcommands."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "publish":
        return _cmd_publish(args)
    if args.command == "list":
        return _cmd_list(args)
    if args.command == "add":
        return _cmd_add(args)
    parser.print_usage(sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
