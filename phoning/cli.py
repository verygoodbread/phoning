"""Command line interface for phoning v0.

Subcommands (see ``docs/SPEC-v0.md`` and ``docs/SPEC-v0.2.md``)::

    phoning init NAME --relay URL
    phoning id NAME
    phoning add NAME FINGERPRINT --as ALIAS
    phoning room create NAME ROOM --members a,b
    phoning own NAME --session SID [--interval 2]
    phoning send NAME (--room ROOM | --to ALIAS) --type T --subject S ...
    phoning read NAME --reader R [--types a,b] [--room ROOM] [--peek] [--json] [--sanitized]
    phoning wait NAME --reader R [--timeout 600] [--json] [--sanitized]
    phoning board NAME [--room ROOM] [--owners FILE.json]
    phoning up [--config F] ...
    phoning hook EVENT ...
    phoning web NAME ...
    phoning mcp NAME ...
    phoning doorbell NAME [--ring KIND] [--rules JSON] [--rules-file F] [--approve ID | --deny ID] ...
    phoning intro NAME --to ALIAS ...
    phoning profile NAME ...
    phoning prune NAME [--keep-days N] [--keep-last N] [--dry-run]
    phoning directory publish|list|add ...
    phoning bridge NAME --from-identity DIR --peer P --session S

Exit codes: 0 ok, 1 nothing/timeout, 2 usage/validation, 3 refused.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import json
import os
import signal
import sys
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from phoning import board as board_mod
from phoning import daemon as daemon_mod
from phoning import envelope as envelope_mod
from phoning import ops
from phoning import text as text_mod
from phoning.envelope import EnvelopeError
from phoning.mailbox import Mailbox, OwnerBusy
from phoning.retalk import Retalk, RetalkError

EXIT_OK: int = 0
EXIT_NOTHING: int = 1
EXIT_USAGE: int = 2
EXIT_REFUSED: int = 3


def home_dir() -> str:
    """Return ``$PHONING_HOME`` or ``~/.phoning``."""
    override = os.environ.get("PHONING_HOME")
    if override:
        return override
    return os.path.expanduser(os.path.join("~", ".phoning"))


def user_dir_for(name: str) -> str:
    """Return ``<home>/<name>`` for principal ``name``."""
    return os.path.join(home_dir(), name)


def resolve_session(explicit: str | None = None) -> str:
    """Resolve the sending session.

    Precedence: explicit ``--session`` argument, ``$PHONING_SESSION``,
    ``$CLAUDE_CODE_SESSION_ID``, ``$CODEX_THREAD_ID``, else ``"cli"``.
    Empty strings are treated as missing.
    """
    if explicit:
        return explicit
    for key in ("PHONING_SESSION", "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"):
        val = os.environ.get(key)
        if val:
            return val
    return "cli"


def now_iso() -> str:
    """Current UTC time as ISO-8601 with ``Z`` suffix."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def parse_comma_list(raw: str | None) -> list[str] | None:
    """Split a comma-separated flag value, dropping empties."""
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def effective_room(rec: dict[str, Any]) -> Any:
    """Effective room for filtering: top-level ``room`` else ``env.room``."""
    top = rec.get("room")
    if top is not None:
        return top
    env = rec.get("env")
    if isinstance(env, dict):
        return env.get("room")
    return None


def format_human(rec: dict[str, Any]) -> str:
    """One-line human form: ``#<seq> <ts> <p>/<s> [<room>] <type>: <subject>``."""
    seq = rec.get("seq", "?")
    env_any = rec.get("env")
    env: dict[str, Any] = env_any if isinstance(env_any, dict) else {}
    frm_any = env.get("from")
    frm: dict[str, Any] = frm_any if isinstance(frm_any, dict) else {}
    principal = frm.get("principal", "?")
    session = frm.get("session", "?")
    typ = env.get("type", "?")
    subject = env.get("subject", "")
    ts_any = env.get("ts")
    ts = str(ts_any) if isinstance(ts_any, str) and ts_any else str(rec.get("recv_ts", ""))
    room_any = rec.get("room")
    if room_any is None:
        room_any = env.get("room")
    if isinstance(room_any, str) and room_any:
        return f"#{seq} {ts} {principal}/{session} [{room_any}] {typ}: {subject}"
    return f"#{seq} {ts} {principal}/{session} {typ}: {subject}"


def format_record(rec: dict[str, Any]) -> str:
    """Alias of :func:`format_human` for backwards compatibility."""
    return format_human(rec)


def read_all_records(box: Mailbox) -> list[dict[str, Any]]:
    """Return every record in the mailbox log, oldest first."""
    reader = getattr(box, "_read_all", None)
    if callable(reader):
        result: list[dict[str, Any]] = reader()
        return result
    log_path = os.path.join(box.mailbox_dir, "log.jsonl")
    out: list[dict[str, Any]] = []
    try:
        with open(log_path, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except FileNotFoundError:
        return []
    return out


# Phone-book names for the commands. Both spellings always work; main() maps these before parsing.
ALIASES: dict[str, str] = {
    "number": "id",
    "save-contact": "add",
    "line": "room",
    "operator": "own",
    "voicemail": "read",
    "listen": "wait",
    "group-call": "board",
    "dial-tone": "up",
    "switchboard": "web",
    "ring": "doorbell",
    "hello": "intro",
    "caller-id": "profile",
    "clear-voicemail": "prune",
    "phonebook": "directory",
    "forward": "bridge",
    "hook-up": "connect",
}
ALIASES_TEXT = "phone names also work: " + ", ".join(f"{a} ({c})" for a, c in ALIASES.items())


def build_parser() -> argparse.ArgumentParser:
    """Build the ``phoning`` argument parser."""
    parser = argparse.ArgumentParser(prog="phoning", epilog=ALIASES_TEXT)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="create identity and mailbox")
    p_init.add_argument("name", help="principal name")
    p_init.add_argument("--relay", required=True, help="relay URL")
    p_init.set_defaults(func="init")

    p_id = sub.add_parser("id", help="print fingerprint")
    p_id.add_argument("name", help="principal name")
    p_id.set_defaults(func="id")

    p_add = sub.add_parser("add", help="add a peer")
    p_add.add_argument("name", help="local principal name")
    p_add.add_argument("fingerprint", help="peer fingerprint")
    p_add.add_argument("--as", dest="alias", required=True, help="local alias for peer")
    p_add.set_defaults(func="add")

    p_room = sub.add_parser("room", help="group rooms")
    room_sub = p_room.add_subparsers(dest="room_cmd", required=True)
    p_create = room_sub.add_parser("create", help="create a group room")
    p_create.add_argument("name", help="local principal name")
    p_create.add_argument("room", help="room name")
    p_create.add_argument("--members", required=True, help="comma-separated aliases")
    p_create.set_defaults(func="room_create")

    p_own = sub.add_parser("own", help="run the owner daemon in foreground")
    p_own.add_argument("name", help="principal name")
    p_own.add_argument("--session", default=None, help="owner session id")
    p_own.add_argument("--interval", type=float, default=2.0, help="follow interval seconds")
    p_own.set_defaults(func="own")

    p_send = sub.add_parser(
        "send",
        help="send a typed message",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=envelope_mod.RULES_TEXT,
    )
    p_send.add_argument("name", help="sending principal name")
    dest = p_send.add_mutually_exclusive_group(required=True)
    dest.add_argument("--room", default=None, help="group room")
    dest.add_argument("--to", default=None, help="peer alias")
    p_send.add_argument("--type", dest="msg_type", required=True, help="envelope type")
    p_send.add_argument("--subject", required=True, help="subject 1..80 chars")
    p_send.add_argument("--body", default=None, help="message body")
    p_send.add_argument("--body-file", dest="body_file", default=None, help="read body from file")
    p_send.add_argument("--session", default=None, help="sending session id")
    p_send.add_argument("--thread", default=None, help="thread eid")
    p_send.add_argument(
        "--reply-to",
        dest="reply_to",
        default=None,
        help='eid, or "last" (newest inbound from --to/--room)',
    )
    p_send.add_argument("--data", default=None, help="JSON object string")
    p_send.add_argument("--refs", default=None, help="comma-separated refs")
    p_send.set_defaults(func="send")

    p_read = sub.add_parser("read", help="read mailbox records")
    p_read.add_argument("name", help="principal name")
    p_read.add_argument("--reader", required=True, help="reader cursor name")
    p_read.add_argument("--types", default=None, help="comma-separated envelope types")
    p_read.add_argument("--room", default=None, help="filter by room")
    p_read.add_argument("--peek", action="store_true", help="do not commit cursor")
    p_read.add_argument("--json", dest="as_json", action="store_true", help="JSON per line")
    p_read.add_argument(
        "--sanitized",
        dest="sanitized",
        action="store_true",
        help="sanitized JSON per line (implies --json)",
    )
    p_read.set_defaults(func="read")

    p_wait = sub.add_parser("wait", help="wait for new records")
    p_wait.add_argument("name", help="principal name")
    p_wait.add_argument("--reader", required=True, help="reader cursor name")
    p_wait.add_argument("--timeout", type=float, default=600.0, help="timeout seconds")
    p_wait.add_argument("--json", dest="as_json", action="store_true", help="JSON per line")
    p_wait.add_argument(
        "--sanitized",
        dest="sanitized",
        action="store_true",
        help="sanitized JSON per line (implies --json)",
    )
    p_wait.add_argument(
        "--include-own",
        dest="include_own",
        action="store_true",
        help="include outbound (own) records",
    )
    p_wait.set_defaults(func="wait")

    p_board = sub.add_parser("board", help="render the folded board")
    p_board.add_argument("name", help="principal name")
    p_board.add_argument("--room", default=None, help="filter by room")
    p_board.add_argument("--owners", default=None, help="owners JSON file")
    p_board.set_defaults(func="board")

    p_up = sub.add_parser("up", help="start the relay and daemons that are not running")
    p_up.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_up.set_defaults(func="up")

    p_hook = sub.add_parser("hook", help="Claude Code / Codex hook: surface unread records")
    p_hook.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_hook.set_defaults(func="hook")

    p_web = sub.add_parser("web", help="local viewer (read-only unless --allow-send)")
    p_web.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_web.set_defaults(func="web")

    p_mcp = sub.add_parser("mcp", help="MCP stdio server")
    p_mcp.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_mcp.set_defaults(func="mcp")

    p_doorbell = sub.add_parser(
        "doorbell",
        help="wake an idle session or notify a human on actionable mail (rules, approvals)",
    )
    p_doorbell.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_doorbell.set_defaults(func="doorbell")

    p_intro = sub.add_parser("intro", help="send a first-contact intro to a peer")
    p_intro.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_intro.set_defaults(func="intro")

    p_profile = sub.add_parser("profile", help="show or update this identity's profile")
    p_profile.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_profile.set_defaults(func="profile")

    p_prune = sub.add_parser("prune", help="trim a mailbox log and archive what is removed")
    p_prune.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_prune.set_defaults(func="prune")

    p_directory = sub.add_parser(
        "directory", help="same-machine directory of identity cards (publish, list, add)"
    )
    p_directory.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_directory.set_defaults(func="directory")

    p_bridge = sub.add_parser(
        "bridge", help="read a legacy agent-talk identity into a phoning mailbox"
    )
    p_bridge.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_bridge.set_defaults(func="bridge")

    p_eavesdrop = sub.add_parser(
        "eavesdrop", help="listen in on a group call: board, backlog, then new messages live (read-only)"
    )
    p_eavesdrop.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_eavesdrop.set_defaults(func="eavesdrop")

    p_join = sub.add_parser("join", help="join a group call as a human: watch it live and type to talk")
    p_join.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_join.set_defaults(func="join")

    p_connect = sub.add_parser(
        "connect", help="put an agent harness on a line: MCP tools, instructions, delivery (list, snippet, HARNESS)"
    )
    p_connect.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_connect.set_defaults(func="connect")

    return parser


def make_parser() -> argparse.ArgumentParser:
    """Alias of :func:`build_parser`."""
    return build_parser()


def _cmd_init(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    relay: str = args.relay
    os.makedirs(udir, mode=0o700, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(udir, 0o700)
    relay_path = os.path.join(udir, "relay")
    payload = (relay.strip() + "\n").encode("utf-8")
    fd = os.open(relay_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
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
    with contextlib.suppress(OSError):
        os.chmod(relay_path, 0o600)
    Mailbox(udir)
    client = Retalk(udir)
    try:
        fp = client.init()
    except RetalkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NOTHING
    print(fp)
    return EXIT_OK


def _cmd_id(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    client = Retalk(udir)
    try:
        fp = client.id()
    except RetalkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NOTHING
    print(fp)
    return EXIT_OK


def _cmd_add(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    client = Retalk(udir)
    try:
        client.add(args.fingerprint, args.alias)
    except RetalkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NOTHING
    return EXIT_OK


def _cmd_room_create(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    members = parse_comma_list(args.members) or []
    client = Retalk(udir)
    try:
        client.group_create(args.room, members)
    except RetalkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NOTHING
    return EXIT_OK


def _cmd_own(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    session = resolve_session(args.session)
    interval: float = float(args.interval)
    stop = threading.Event()

    def _handle(signum: int, frame: Any) -> None:
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError, RuntimeError):
            signal.signal(sig, _handle)
    try:
        daemon_mod.run_owner(udir, session, interval=interval, stop_event=stop)
    except OwnerBusy as exc:
        holder = exc.holder if isinstance(exc.holder, dict) else {}
        sess = holder.get("session", "?")
        pid = holder.get("pid", "?")
        print(f"mailbox owned by session {sess} (pid {pid})", file=sys.stderr)
        return EXIT_REFUSED
    except KeyboardInterrupt:
        return EXIT_OK
    return EXIT_OK


def _cmd_send(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    session = resolve_session(args.session)
    if args.body is not None and args.body_file is not None:
        print("error: --body and --body-file are mutually exclusive", file=sys.stderr)
        return EXIT_USAGE
    if args.body_file is not None:
        try:
            with open(args.body_file, encoding="utf-8") as fh:
                body = fh.read()
        except OSError as exc:
            print(f"error: cannot read --body-file: {exc}", file=sys.stderr)
            return EXIT_USAGE
    else:
        body = args.body if args.body is not None else ""
    data: dict[str, Any] | None = None
    if args.data is not None:
        try:
            parsed: Any = json.loads(args.data)
        except json.JSONDecodeError as exc:
            print(f"error: invalid --data JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(parsed, dict):
            print("error: --data must be a JSON object", file=sys.stderr)
            return EXIT_USAGE
        data = parsed
    refs: list[str] | None = None
    if args.refs is not None:
        refs = [p.strip() for p in str(args.refs).split(",") if p.strip()]
    try:
        eid = ops.send_message(
            udir,
            args.name,
            session,
            msg_type=args.msg_type,
            subject=args.subject,
            body=body,
            to=args.to,
            room=args.room,
            thread=args.thread,
            reply_to=args.reply_to,
            data=data,
            refs=refs,
        )
    except (EnvelopeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except RetalkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_NOTHING
    print(json.dumps({"eid": eid}))
    return EXIT_OK


def _sanitized_record(rec: dict[str, Any], name: str) -> dict[str, Any]:
    """``ops.sanitize`` with ``subject``/``body`` run through text hygiene.

    ``--sanitized`` output feeds an agent directly, so the subject goes
    through :func:`text.clean_line` and the body through
    :func:`text.clean_block` (docs/SPEC-v0.2.md §7.6).
    """
    out = ops.sanitize(rec, local=name)
    env_any: Any = out.get("env")
    if isinstance(env_any, dict):
        env: dict[str, Any] = env_any
        subject: Any = env.get("subject")
        if isinstance(subject, str):
            env["subject"] = text_mod.clean_line(subject)
        body: Any = env.get("body")
        if isinstance(body, str):
            env["body"] = text_mod.clean_block(body)
    return out


def _cmd_read(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    box = Mailbox(udir)
    types = parse_comma_list(args.types)
    try:
        if args.peek:
            recs = box.peek(args.reader, types=types, room=args.room)
        else:
            recs = box.read(args.reader, types=types, room=args.room)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not recs:
        return EXIT_OK
    if args.sanitized:
        for rec in recs:
            print(json.dumps(_sanitized_record(rec, args.name), ensure_ascii=False))
    elif args.as_json:
        for rec in recs:
            print(json.dumps(rec, ensure_ascii=False))
    else:
        for rec in recs:
            print(format_human(rec))
    return EXIT_OK


def _cmd_wait(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    box = Mailbox(udir)
    try:
        timeout = float(args.timeout)
    except (TypeError, ValueError):
        print("error: invalid --timeout", file=sys.stderr)
        return EXIT_USAGE
    include_own: bool = bool(getattr(args, "include_own", False))
    try:
        recs = box.wait(args.reader, timeout=timeout, include_own=include_own)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    if not recs:
        return EXIT_NOTHING
    if args.sanitized:
        for rec in recs:
            print(json.dumps(_sanitized_record(rec, args.name), ensure_ascii=False))
    elif args.as_json:
        for rec in recs:
            print(json.dumps(rec, ensure_ascii=False))
    else:
        for rec in recs:
            print(format_human(rec))
    return EXIT_OK


def _cmd_board(args: argparse.Namespace) -> int:
    udir = user_dir_for(args.name)
    owners: dict[str, Any] = {}
    if args.owners is not None:
        try:
            with open(args.owners, encoding="utf-8") as fh:
                loaded: Any = json.load(fh)
        except (OSError, ValueError) as exc:
            print(f"error: cannot load --owners file: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(loaded, dict):
            print("error: owners file must contain a JSON object", file=sys.stderr)
            return EXIT_USAGE
        owners = loaded
    box = Mailbox(udir)
    records = read_all_records(box)
    if args.room is not None:
        records = [r for r in records if effective_room(r) == args.room]
    folded = board_mod.fold(records, owners, local=args.name)
    print(board_mod.render(folded))
    return EXIT_OK


_DELEGATED: dict[str, tuple[str, str]] = {
    "up": ("phoning.up", "main"),
    "hook": ("phoning.hooks", "main"),
    "web": ("phoning.web", "main"),
    "mcp": ("phoning.mcp", "main"),
    "doorbell": ("phoning.doorbell", "main"),
    "intro": ("phoning.intro", "main"),
    "profile": ("phoning.intro", "profile_main"),
    "prune": ("phoning.retention", "main"),
    "directory": ("phoning.directory", "main"),
    "bridge": ("phoning.bridge", "main"),
    "eavesdrop": ("phoning.call", "eavesdrop_main"),
    "join": ("phoning.call", "join_main"),
    "connect": ("phoning.connect", "main"),
}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for the ``phoning`` console script."""
    raw = list(argv) if argv is not None else sys.argv[1:]
    if raw and raw[0] in ALIASES:
        raw[0] = ALIASES[raw[0]]
    if raw and raw[0] in _DELEGATED:
        module_name, func_name = _DELEGATED[raw[0]]
        mod: Any = importlib.import_module(module_name)
        entry: Any = getattr(mod, func_name)
        return int(entry(raw[1:]))
    parser = build_parser()
    args = parser.parse_args(raw)
    func: str = getattr(args, "func", "")
    if func == "init":
        return _cmd_init(args)
    if func == "id":
        return _cmd_id(args)
    if func == "add":
        return _cmd_add(args)
    if func == "room_create":
        return _cmd_room_create(args)
    if func == "own":
        return _cmd_own(args)
    if func == "send":
        return _cmd_send(args)
    if func == "read":
        return _cmd_read(args)
    if func == "wait":
        return _cmd_wait(args)
    if func == "board":
        return _cmd_board(args)
    parser.print_usage(sys.stderr)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
