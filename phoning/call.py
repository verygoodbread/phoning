"""``phoning eavesdrop`` and ``phoning join``: a human on the group call.

``eavesdrop`` prints the room's board and its recent backlog, then every new record as it lands.
It never sends and never moves a reader cursor, so it cannot hide mail from the agents' own
readers. ``join`` does the same and sends each line typed on stdin into the room as ``chat``.
Inside a join: ``/ask SUBJECT`` sends an ask, ``/board`` reprints the board, ``/leave`` (or
end of input, or Ctrl-C) hangs up.

A join says hello and goodbye: it sends a ``status`` "<name> joined the call" when it connects and
"<name> left the call" when it hangs up (``announce=False`` skips both), so the human shows on the
board's "who is doing what". Everything a join sends carries ``data.human = true``; agents' hooks
turn that into a short cue to greet the human by name when they reply.

Records are shown with the transport-verified sender (a mismatched claim is flagged
``(claims X)``) and with control characters stripped, like every other surface.
"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from collections.abc import Callable, Sequence
from typing import IO, Any

from phoning import board as board_mod
from phoning import ops
from phoning import text as text_mod
from phoning.envelope import EnvelopeError
from phoning.mailbox import Mailbox
from phoning.retalk import RetalkError

EXIT_OK = 0
EXIT_USAGE = 2

SUBJECT_MAX = 80
HELP_TEXT = "/ask SUBJECT  send an ask   /board  show the board   /leave  hang up   anything else is chat"

SendFn = Callable[..., str]


def _home(explicit: str | None) -> str:
    if explicit:
        return os.path.expanduser(explicit)
    override = os.environ.get("PHONING_HOME")
    if override:
        return override
    return os.path.expanduser(os.path.join("~", ".phoning"))


def _on_call(rec: dict[str, Any], room: str | None, peer: str | None) -> bool:
    """True when ``rec`` belongs to this call: the room, or the direct line with ``peer``."""
    rec_room = rec.get("room")
    if rec_room is None:
        env = rec.get("env")
        rec_room = env.get("room") if isinstance(env, dict) else None
    if room is not None:
        return bool(rec_room == room)
    return rec_room is None and rec.get("peer") == peer


def _seq(rec: dict[str, Any]) -> int:
    seq = rec.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) else -1


def format_line(rec: dict[str, Any], name: str) -> str:
    """One record as call text: ``HH:MM:SS sender/session type: subject`` plus quoted body."""
    view = ops.sanitize(rec, local=name)
    env_any: Any = view.get("env")
    env: dict[str, Any] = env_any if isinstance(env_any, dict) else {}
    frm_any: Any = env.get("from")
    frm: dict[str, Any] = frm_any if isinstance(frm_any, dict) else {}
    ts_any: Any = env.get("ts") or rec.get("recv_ts") or ""
    ts = str(ts_any)
    clock = ts[11:19] if len(ts) >= 19 else ts
    sender = text_mod.clean_line(str(view.get("sender") or "?"))
    session = text_mod.clean_line(str(frm.get("session") or "?"))
    claimed = view.get("claimed")
    claim = f" (claims {text_mod.clean_line(str(claimed))})" if claimed else ""
    typ = text_mod.clean_line(str(env.get("type") or "?"))
    subject = text_mod.clean_line(str(env.get("subject") or ""))
    lines = [f"{clock} {sender}/{session}{claim} {typ}: {subject}"]
    body_any: Any = env.get("body")
    if isinstance(body_any, str) and body_any.strip() and body_any.strip() != subject:
        lines.extend(f"  | {ln}" for ln in text_mod.clean_block(body_any).splitlines())
    return "\n".join(lines)


def render_board(records: list[dict[str, Any]], name: str, room: str | None, peer: str | None) -> str:
    on_call = [r for r in records if _on_call(r, room, peer)]
    return board_mod.render(board_mod.fold(on_call, {}, local=name))


def split_line(line: str) -> tuple[str, str]:
    """Return ``(subject, body)`` for a typed line: the body carries what an 80-char subject can't."""
    line = text_mod.clean_line(line)
    if len(line) <= SUBJECT_MAX:
        return line, ""
    return line[: SUBJECT_MAX - 3] + "...", line


def _stdin_lines(stream: IO[str], out: queue.Queue[str | None]) -> None:
    try:
        for line in stream:
            out.put(line)
    except (OSError, ValueError):
        pass
    out.put(None)


def display_name(name: str) -> str:
    """The name a human shows on a call: the line name without a ``human-`` prefix."""
    return name[len("human-"):] if name.startswith("human-") and len(name) > len("human-") else name


def run_call(
    name: str,
    *,
    home: str,
    room: str | None,
    peer: str | None,
    session: str,
    talk: bool,
    announce: bool = True,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
    backlog: int = 10,
    poll: float = 0.5,
    once: bool = False,
    send: SendFn = ops.send_message,
) -> int:
    """Eavesdrop on (``talk=False``) or join (``talk=True``) one call until hang-up."""
    out = stdout if stdout is not None else sys.stdout
    udir = os.path.join(home, name)
    if not os.path.isdir(os.path.join(udir, "mailbox")):  # checked first: Mailbox() would create it
        print(f"error: no mailbox for {name} under {home}", file=sys.stderr)
        return EXIT_USAGE
    box = Mailbox(udir)
    where = f"party line {room}" if room is not None else f"direct line with {peer}"

    def say(msg: str) -> None:
        print(msg, file=out, flush=True)

    records = box._read_all()
    say(render_board(records, name, room, peer))
    on_call = [r for r in records if _on_call(r, room, peer)]
    say(f"--- {'joined' if talk else 'listening in on'} {where} as {session if talk else name}; last {min(backlog, len(on_call))} of {len(on_call)} ---")
    for rec in on_call[-backlog:] if backlog > 0 else []:
        say(format_line(rec, name))
    if talk:
        say(HELP_TEXT)
    last = max((_seq(r) for r in records), default=-1)
    sent: set[str] = set()
    lines: queue.Queue[str | None] = queue.Queue()
    if talk:
        reader = threading.Thread(target=_stdin_lines, args=(stdin if stdin is not None else sys.stdin, lines), daemon=True)
        reader.start()

    def deliver(msg_type: str, subject: str, body: str = "", data: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"human": True, **(data or {})}
        try:
            eid = send(udir, name, session, msg_type=msg_type, subject=subject, body=body, to=peer, room=room,
                       data=payload)
        except (EnvelopeError, ValueError, RetalkError) as exc:
            say(f"error: not sent: {text_mod.clean_line(str(exc))}")
            return
        sent.add(eid)

    def hang_up() -> int:
        if announce:
            deliver("status", f"{session} left the call"[:SUBJECT_MAX],
                    data={"state": "IDLE", "presence": "left"})
        say(f"--- hung up on {where} ---")
        return EXIT_OK

    if talk and announce:
        deliver("status", f"{session} joined the call"[:SUBJECT_MAX], data={"state": "ACTIVE", "presence": "joined"})

    def send_line(raw: str) -> bool:
        """Handle one typed line; False means hang up."""
        line = raw.rstrip("\n")
        if not line.strip():
            return True
        word, _, rest = line.strip().partition(" ")
        msg_type = "chat"
        if word in ("/leave", "/hangup", "/bye"):
            return False
        if word == "/board":
            say(render_board(box._read_all(), name, room, peer))
            return True
        if word == "/ask":
            if not rest.strip():
                say("usage: /ask SUBJECT")
                return True
            msg_type, line = "ask", rest
        elif word.startswith("/"):
            say(HELP_TEXT)
            return True
        subject, body = split_line(line)
        deliver(msg_type, subject, body)
        return True

    try:
        while True:
            if talk:
                while True:
                    try:
                        item = lines.get_nowait()
                    except queue.Empty:
                        break
                    if item is None or not send_line(item):
                        return hang_up()
            for rec in box._read_all():
                if _seq(rec) <= last:
                    continue
                last = _seq(rec)
                env = rec.get("env")
                if isinstance(env, dict) and env.get("eid") in sent:
                    continue
                if _on_call(rec, room, peer):
                    say(format_line(rec, name))
            if once:
                return EXIT_OK
            time.sleep(poll)
    except KeyboardInterrupt:
        return hang_up() if talk else _quiet_exit(say, where)


def _quiet_exit(say: Callable[[str], None], where: str) -> int:
    say(f"--- stopped listening to {where} ---")
    return EXIT_OK


def _parser(prog: str, talk: bool) -> argparse.ArgumentParser:
    what = (
        "join a group call: watch it live and type to talk (lines are sent as chat)"
        if talk
        else "listen in on a group call: the board, the backlog, then every new message (read-only)"
    )
    p = argparse.ArgumentParser(prog=prog, description=what, epilog=HELP_TEXT if talk else None)
    p.add_argument("name", help="your local principal (its owner daemon must be running to hear new mail)")
    line = p.add_mutually_exclusive_group(required=True)
    line.add_argument("--room", help="the party line (room) to open")
    line.add_argument("--to", help="a direct line with one peer instead of a room")
    p.add_argument("--home", help="phoning home dir")
    p.add_argument("--backlog", type=int, default=10, help="how many earlier messages to show first")
    p.add_argument("--poll", type=float, default=0.5, help="seconds between checks for new mail")
    p.add_argument("--once", action="store_true", help="print what is there now and exit")
    if talk:
        p.add_argument("--as", dest="display", default=None,
                       help="the name you show on the call (default: your line name without 'human-')")
        p.add_argument("--quiet", action="store_true", help="don't announce that you joined or left")
    return p


def _main(argv: Sequence[str], talk: bool) -> int:
    prog = "phoning join" if talk else "phoning eavesdrop"
    args = _parser(prog, talk).parse_args(list(argv))
    if args.poll <= 0 or args.backlog < 0:
        print("error: --poll must be > 0 and --backlog >= 0", file=sys.stderr)
        return EXIT_USAGE
    session = (getattr(args, "display", None) or display_name(args.name)) if talk else "eavesdrop"
    return run_call(
        args.name,
        home=_home(args.home),
        room=args.room,
        peer=args.to,
        session=session,
        talk=talk,
        backlog=args.backlog,
        poll=args.poll,
        once=args.once,
        announce=not getattr(args, "quiet", False),
    )


def join_main(argv: Sequence[str]) -> int:
    return _main(argv, talk=True)


def eavesdrop_main(argv: Sequence[str]) -> int:
    return _main(argv, talk=False)
