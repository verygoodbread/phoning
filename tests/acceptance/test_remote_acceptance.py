"""Acceptance: calls across computers, `phoning remote` (docs/spec/remote.md, Part B).

Two computers are simulated on this one: ONE real retalk-server listening on 0.0.0.0 that accepts
signatures for both http://127.0.0.1:PORT (the host's own lines) and http://LANIP:PORT (the guest's
lines, as if they came in over the network). Each computer is its own PHONING_HOME + up.json +
directory. Every subprocess gets temp paths; nothing here touches ~/.phoning, ~/.config/phoning, the live
relay on 8766, a real Cloudflare tunnel, or `retalk receive` on an owned identity.

`PHONING_CLOUDFLARED` always points at a fake script (a failing one unless a test needs a tunnel), so
a real `cloudflared` on this machine is never started. The host's background watcher (spawned by
`remote host`) is killed right after `host`, and the tests drive `remote watch --once` themselves.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.acceptance.helpers import free_port

pytestmark = pytest.mark.skipif(
    shutil.which("retalk") is None or shutil.which("retalk-server") is None,
    reason="retalk and retalk-server must be installed",
)

ROOT = Path(__file__).resolve().parents[2]
HEX32 = re.compile(r"[0-9a-f]{32}")
LINK_RE = re.compile(r"call-card:1:[A-Za-z0-9_-]+")
TUNNEL_URL = "https://phoning-remote-acceptance-test.trycloudflare.com"


# -- small helpers -------------------------------------------------------------------------


def _decode(link: str) -> dict[str, Any]:
    """Decode a link WITHOUT phoning.remote (so a broken parse_card cannot hide a wrong link)."""
    m = LINK_RE.search(re.sub(r"\s", "", link))
    assert m, f"no link in {link!r}"
    payload = m.group(0)[len("call-card:1:"):]
    obj: dict[str, Any] = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    return obj


def _wait(pred: Callable[[], Any], timeout: float, what: str, step: float = 1.0) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        got = pred()
        if got:
            return got
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")


def _port_open(host: str, port: int) -> bool:
    try:
        socket.create_connection((host, port), timeout=0.5).close()
        return True
    except OSError:
        return False


def _command_of(pid: int) -> str:
    p = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True,
                       timeout=10, stdin=subprocess.DEVNULL)
    return p.stdout.strip()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _listener_pids(port: int) -> list[int]:
    p = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"], capture_output=True,
                       text=True, timeout=10, stdin=subprocess.DEVNULL)
    return [int(x) for x in p.stdout.split() if x.strip().isdigit()]


def _kill_if(pid: int, must_contain: str) -> None:
    """SIGTERM pid only when its command line contains `must_contain` (never anything else)."""
    if pid > 0 and _alive(pid) and must_contain in _command_of(pid):
        with contextlib.suppress(OSError):
            os.kill(pid, signal.SIGTERM)


def _kill_relay_on(port: int) -> None:
    for pid in _listener_pids(port):
        _kill_if(pid, "retalk-server")
    _wait(lambda: not _port_open("127.0.0.1", port), 15, f"port {port} to close", 0.2)


def _kill_pidfile(path: Path, must_contain: str) -> None:
    with contextlib.suppress(OSError, ValueError):
        _kill_if(int(path.read_text().strip()), must_contain)


def _lan_ip() -> str | None:
    """A non-loopback IPv4 of this machine that this machine can actually connect to."""
    cands: list[str] = []
    for target in ("10.255.255.255", "192.168.255.255", "172.31.255.255", "8.8.8.8"):
        with contextlib.suppress(OSError), socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target, 1))
            cands.append(s.getsockname()[0])
    with contextlib.suppress(OSError):
        cands += socket.gethostbyname_ex(socket.gethostname())[2]
    for ip in dict.fromkeys(cands):
        if ip.startswith("127.") or ip == "0.0.0.0":
            continue
        with socket.socket() as srv:
            srv.bind(("0.0.0.0", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            try:
                socket.create_connection((ip, port), timeout=1).close()
            except OSError:
                continue
        return ip
    return None


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


# -- the world -----------------------------------------------------------------------------


class Net:
    def __init__(self, tmp: Path, port: int, lan: str) -> None:
        self.tmp, self.port, self.lan = tmp, port, lan
        self.loop_url = f"http://127.0.0.1:{port}"
        self.lan_url = f"http://{lan}:{port}"
        self.n = 0
        self.no_cloudflared = _script(tmp / "fakebin-fail" / "cloudflared", "exit 1\n")

    def computer(self, label: str, line_relay: str, *, public: list[str] | None = None,
                 config: dict[str, Any] | None | str = "default") -> Computer:
        self.n += 1
        return Computer(self, self.tmp / f"{label}{self.n}", line_relay, public=public, config=config)


ROOM_A = "call-comp-a"  # host "comp-a"'s party line (each host's room is call-<who>)


class Computer:
    """One simulated computer: PHONING_HOME, up.json (in its own state dir), card directory."""

    def __init__(self, net: Net, root: Path, line_relay: str, *, public: list[str] | None,
                 config: dict[str, Any] | None | str) -> None:
        self.net, self.root, self.line_relay = net, root, line_relay
        self.home = root / "home"
        self.state = root / "state"
        self.config = self.state / "up.json"
        self.home.mkdir(parents=True)
        self.state.mkdir(parents=True)
        if config == "default":
            relay: dict[str, Any] = {"url": net.loop_url, "db": str(root / "relaydb" / "relay.db")}
            if public:
                relay["public"] = public
            self.config.write_text(json.dumps({"relay": relay, "owners": []}))
        elif isinstance(config, dict):
            self.config.write_text(json.dumps(config))
        self.env = dict(os.environ, PHONING_HOME=str(self.home), PHONING_UP_CONFIG=str(self.config),
                        PHONING_DIRECTORY=str(root / "cards"), PHONING_CLOUDFLARED=str(net.no_cloudflared),
                        PHONING_REMOTE_WAIT="20", PYTHONPATH=str(ROOT))
        self.env.pop("PHONING_SESSION", None)
        self.fps: dict[str, str] = {}
        self.owners: list[subprocess.Popen[bytes]] = []

    # commands
    def phoning(self, *args: str, input: str | None = None, timeout: int = 120,
               env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, "-m", "phoning.cli", *args], env=env or self.env,
                              capture_output=True, text=True, timeout=timeout,
                              input=input, stdin=None if input is not None else subprocess.DEVNULL)

    def remote(self, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        return self.phoning("remote", *args, **kw)

    def ok(self, *args: str, **kw: Any) -> subprocess.CompletedProcess[str]:
        p = self.remote(*args, **kw)
        assert p.returncode == 0, (args, p.returncode, p.stdout, p.stderr)
        return p

    def js(self, *args: str, **kw: Any) -> dict[str, Any]:
        out: dict[str, Any] = json.loads(self.ok(*args, "--json", **kw).stdout)
        return out

    def init(self, name: str, relay: str | None = None) -> str:
        p = self.phoning("init", name, "--relay", relay or self.line_relay)
        assert p.returncode == 0, (p.stdout, p.stderr)
        fp = p.stdout.strip().split()[-1]
        assert HEX32.fullmatch(fp)
        self.fps[name] = fp
        return fp

    def udir(self, name: str) -> Path:
        return self.home / name

    def relay_of(self, name: str) -> str:
        return (self.udir(name) / "relay").read_text().strip()

    def retalk(self, name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["retalk", *args, "--dir", str(self.udir(name) / "identity"),
                               "-r", self.relay_of(name), "-np"], capture_output=True, text=True,
                              timeout=60, stdin=subprocess.DEVNULL)

    def contacts(self, name: str) -> dict[str, str]:
        """alias -> fingerprint, from `retalk contacts --json`."""
        out: dict[str, str] = {}
        for line in self.retalk(name, "contacts", "--json").stdout.splitlines():
            line = line.strip()
            if line.startswith("{"):
                rec = json.loads(line)
                out[rec["name"]] = rec["fingerprint"]
        return out

    def group_fps(self, name: str, room: str) -> set[str] | None:
        p = self.retalk(name, "group", "members", room)
        if p.returncode != 0:
            return None
        return set(HEX32.findall(p.stdout))

    def own(self, name: str, session: str = "own") -> subprocess.Popen[bytes]:
        p = subprocess.Popen([sys.executable, "-m", "phoning.cli", "own", name, "--session", session,
                              "--interval", "1"], env=self.env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.owners.append(p)
        return p

    def stop_owners(self) -> None:
        for p in self.owners:
            p.terminate()
        for p in self.owners:
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()
        self.owners.clear()

    def records(self, name: str, reader: str = "test") -> list[dict[str, Any]]:
        p = self.phoning("read", name, "--reader", reader, "--json", "--peek")
        assert p.returncode == 0, p.stderr
        return [json.loads(ln) for ln in p.stdout.splitlines() if ln.strip()]

    def wait_record(self, name: str, pred: Callable[[dict[str, Any]], bool], timeout: float = 60,
                    what: str = "record") -> dict[str, Any]:
        def find() -> dict[str, Any] | None:
            return next((r for r in self.records(name) if pred(r)), None)
        found: dict[str, Any] = _wait(find, timeout, f"{name}: {what}")
        return found

    def kill_watcher(self) -> None:
        _kill_pidfile(self.home / ".remote-watch.pid", "phoning.remote watch")

    def cleanup(self) -> None:
        self.stop_owners()
        self.kill_watcher()
        with contextlib.suppress(OSError, ValueError, KeyError):
            t = json.loads((self.state / "tunnel.json").read_text())
            _kill_if(int(t["pid"]), "cloudflared")


@pytest.fixture(scope="module")
def net(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Net]:
    lan = _lan_ip()
    if lan is None:
        pytest.skip("no reachable non-loopback IPv4 on this machine")
    tmp = tmp_path_factory.mktemp("remote")
    port = free_port()
    loop_url, lan_url = f"http://127.0.0.1:{port}", f"http://{lan}:{port}"
    db = tmp / "relay.db"
    srv = subprocess.Popen(
        ["retalk-server", "--host", "0.0.0.0", "--port", str(port), "--audience", f"{loop_url},{lan_url}",
         "--db", str(db)],
        env=dict(os.environ, RETALK_SERVER_DB=str(db)), cwd=tmp,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        _wait(lambda: _port_open("127.0.0.1", port) and _port_open(lan, port), 15, "relay", 0.1)
        yield Net(tmp, port, lan)
    finally:
        srv.terminate()
        srv.wait(timeout=10)


# -- the main story: A hosts, B joins with one paste ------------------------------------------


class Pair:
    def __init__(self, a: Computer, b: Computer, link: str, host: dict[str, Any],
                 join: subprocess.CompletedProcess[str], room_b: dict[str, set[str] | None],
                 first_watch: dict[str, dict[str, str]]) -> None:
        self.a, self.b, self.link, self.host, self.join = a, b, link, host, join
        self.room_b, self.first_watch = room_b, first_watch


@pytest.fixture(scope="module")
def pair(net: Net) -> Iterator[Pair]:
    a = net.computer("hostA", net.loop_url)
    b = net.computer("guestB", net.lan_url, config={"owners": []})
    try:
        a.init("ana")
        a.init("anb")
        pub = a.phoning("directory", "publish", "ana", "--harness", "claude", "--role", "lead")
        assert pub.returncode == 0, pub.stderr
        host = a.js("host", "--url", net.lan_url, "--who", "comp-a")
        watcher_pid = int((a.home / ".remote-watch.pid").read_text().strip())
        assert _alive(watcher_pid) and "phoning.remote watch" in _command_of(watcher_pid)
        a.kill_watcher()  # the test drives `remote watch --once` itself
        _wait(lambda: not _alive(watcher_pid), 10, "watcher to stop", 0.2)
        link = host["link"]
        assert isinstance(link, str)

        b.init("bea")
        b.init("beb")
        add = b.phoning("add", "bea", b.fps["beb"], "--as", "beb")
        assert add.returncode == 0, add.stderr
        join = b.remote("join", link, "--who", "comp-b")
        assert join.returncode == 0, (join.stdout, join.stderr)
        room_b = {n: b.group_fps(n, ROOM_A) for n in ("bea", "beb")}

        # host side, pass 1: accept the contact requests -> temporary guest aliases
        a.ok("watch", "--once")
        first_watch = {n: a.contacts(n) for n in ("ana", "anb")}
        # now the owners can receive the intros, and later passes rename the guests
        a.own("ana", "A")
        a.own("anb", "A2")

        def renamed() -> bool:
            a.remote("watch", "--once")
            return all(a.contacts(n).get(f"guest-{x}") == b.fps[x] for n in ("ana", "anb") for x in ("bea", "beb"))
        _wait(renamed, 90, "guests renamed to their line names", 1.0)
        # restart the owners so they pick up the real aliases without waiting for the 30 s refresh
        a.stop_owners()
        a.own("ana", "A")
        a.own("anb", "A2")
        b.own("bea", "B")
        b.own("beb", "B2")
        time.sleep(2)
        yield Pair(a, b, link, host, join, room_b, first_watch)
    finally:
        a.cleanup()
        b.cleanup()


def test_host_with_url_sets_bind_public_and_backs_up_the_config(net: Net, pair: Pair) -> None:
    h = pair.host
    assert h["url"] == net.lan_url
    assert h["config"] == str(pair.a.config)
    assert h["tunnel"] is False and h["restarted"] is False  # our relay already binds 0.0.0.0 + LAN audience
    cfg = json.loads(pair.a.config.read_text())
    assert cfg["relay"]["url"] == net.loop_url
    assert cfg["relay"]["bind"] == "0.0.0.0"
    assert cfg["relay"]["public"][0] == net.lan_url
    assert _mode(pair.a.config) == 0o600
    baks = list(pair.a.state.glob("up.json.bak-*"))
    assert len(baks) == 1
    assert "public" not in json.loads(baks[0].read_text())["relay"]


def test_host_link_carries_the_public_url_codes_and_directory_cards(net: Net, pair: Pair) -> None:
    from phoning.remote import parse_card

    card = parse_card(pair.link)
    assert card == _decode(pair.link)
    assert card["v"] == 1 and card["relay"] == net.lan_url
    assert card["who"] == "comp-a" and card["room"] == ROOM_A
    lines = {ln["name"]: ln for ln in card["lines"]}
    assert set(lines) == {"ana", "anb"}
    assert lines["ana"]["fingerprint"] == pair.a.fps["ana"]
    assert lines["anb"]["fingerprint"] == pair.a.fps["anb"]
    assert lines["ana"]["harness"] == "claude" and lines["ana"]["role"] == "lead"
    assert lines["anb"]["harness"] is None and lines["anb"]["role"] is None
    for name in ("ana", "anb"):
        code = lines[name]["code"]
        assert re.fullmatch(r"[A-Za-z0-9_-]{8,64}", code)
        inv = pair.a.udir(name) / "remote-invite"
        assert inv.read_text().strip() == code and _mode(inv) == 0o600


def test_host_rerun_reuses_invite_codes_and_writes_nothing(net: Net, pair: Pair) -> None:
    before = pair.a.config.read_bytes()
    again = pair.a.js("host", "--url", net.lan_url, "--who", "comp-a")
    try:
        assert again["restarted"] is False
        codes1 = {ln["name"]: ln["code"] for ln in _decode(pair.link)["lines"]}
        codes2 = {ln["name"]: ln["code"] for ln in _decode(again["link"])["lines"]}
        assert codes1 == codes2
        assert pair.a.config.read_bytes() == before
        assert len(list(pair.a.state.glob("up.json.bak-*"))) == 1
    finally:
        pair.a.kill_watcher()


def test_join_redeems_the_link_and_reports_contacts(pair: Pair) -> None:
    out = pair.join.stdout
    for line in ("bea", "beb"):
        for peer in ("ana", "anb"):
            assert f"{line} added {peer} as {peer}" in out, out
    assert re.search(r'done: on the call "call-comp-a" with \d+ lines from comp-a', out), out
    fa = pair.a.fps
    assert pair.b.contacts("bea") == {"ana": fa["ana"], "anb": fa["anb"], "beb": pair.b.fps["beb"]}
    # joining lines also save each other, so they can all be on one party line (review #11)
    assert pair.b.contacts("beb") == {"ana": fa["ana"], "anb": fa["anb"], "bea": pair.b.fps["bea"]}
    assert pair.join.stderr.count("error:") == 0


def test_join_builds_the_room_on_the_joining_side(pair: Pair) -> None:
    fa, fb = pair.a.fps, pair.b.fps
    # One joining line (the first, bea) creates the room with every card line and every other joining line;
    # beb gets the same group from bea's announcement once its operator reads it, never a second group of
    # its own (two groups would show up as two rooms on the host: review #11).
    assert pair.room_b["bea"] == {fa["ana"], fa["anb"], fb["beb"]}
    assert pair.room_b["beb"] is None
    # an adopted roster is the sender's roster, which lists beb itself too
    _wait(lambda: {fa["ana"], fa["anb"], fb["bea"]} <= (pair.b.group_fps("beb", ROOM_A) or set()), 60,
          "beb to adopt bea's room", 1.0)


def test_watch_once_accepts_requests_under_guest_aliases(pair: Pair) -> None:
    fb = pair.b.fps
    for name in ("ana", "anb"):
        got = pair.first_watch[name]
        for x in ("bea", "beb"):
            assert got.get(f"guest-{fb[x][:8]}") == fb[x], (name, got)
        assert "" not in got


def test_watch_renames_guests_from_their_intros_and_clears_pending(pair: Pair) -> None:
    fb = pair.b.fps
    for name in ("ana", "anb"):
        c = pair.a.contacts(name)
        # a guest is never saved under a name it chose itself: guest-<its line name>
        assert c == {"guest-bea": fb["bea"], "guest-beb": fb["beb"]}, c
    pending_path = pair.a.home / ".remote-pending.json"
    if pending_path.exists():
        assert json.loads(pending_path.read_text() or "{}") == {}
        assert _mode(pending_path) == 0o600


def test_host_lines_receive_the_intro_and_the_room_status(pair: Pair) -> None:
    fb = pair.b.fps
    pair.a.wait_record("ana", lambda r: r["direction"] == "in" and r["peer_fp"] == fb["bea"]
                       and r["env"]["from"]["principal"] == "bea", what="intro from bea")
    joined = pair.a.wait_record(
        "ana", lambda r: r["direction"] == "in" and r["peer_fp"] == fb["bea"]
        and r["env"]["subject"] == "joined the call", what="room status from bea")
    assert joined["env"]["type"] == "status" and joined["room"] == ROOM_A


def test_second_join_changes_nothing(pair: Pair) -> None:
    before = {n: pair.b.contacts(n) for n in ("bea", "beb")}
    rooms = {n: pair.b.group_fps(n, ROOM_A) for n in ("bea", "beb")}
    res = pair.b.js("join", pair.link, "--who", "comp-b")
    assert res["added"] == [] and res["moved"] == [] and res["room"] == ROOM_A
    assert {(k["line"], k["peer"], k["alias"]) for k in res["kept"]} == {
        (ln, peer, peer) for ln in ("bea", "beb") for peer in ("ana", "anb")}
    assert {n: pair.b.contacts(n) for n in ("bea", "beb")} == before
    assert {n: pair.b.group_fps(n, ROOM_A) for n in ("bea", "beb")} == rooms
    # `accept` is the same command
    again = pair.b.js("accept", pair.link, "--who", "comp-b")
    assert again["added"] == []
    # and a further watch pass on the host changes nothing either
    a_before = {n: pair.a.contacts(n) for n in ("ana", "anb")}
    pair.a.ok("watch", "--once")
    assert {n: pair.a.contacts(n) for n in ("ana", "anb")} == a_before


def test_messages_flow_both_ways_with_real_names(pair: Pair) -> None:
    a, b = pair.a, pair.b
    p = b.phoning("send", "bea", "--to", "ana", "--type", "chat", "--subject", "hello-from-b", "--session", "s")
    assert p.returncode == 0, p.stderr
    rec = a.wait_record("ana", lambda r: r["env"]["subject"] == "hello-from-b", what="hello-from-b")
    assert rec["direction"] == "in" and rec["peer"] == "guest-bea" and rec["peer_fp"] == b.fps["bea"]
    parent_eid = rec["env"]["eid"]
    p = a.phoning("send", "ana", "--to", "guest-bea", "--type", "chat", "--subject", "hello-from-a",
                  "--reply-to", "last", "--session", "A")
    assert p.returncode == 0, p.stderr
    rec = b.wait_record("bea", lambda r: r["env"]["subject"] == "hello-from-a", what="hello-from-a")
    assert rec["direction"] == "in" and rec["peer"] == "ana" and rec["peer_fp"] == a.fps["ana"]
    assert rec["env"]["reply_to"] == parent_eid


def test_board_across_computers_binds_authority_to_the_remote_owner(pair: Pair) -> None:
    a, b = pair.a, pair.b
    owners = a.root / "owners.json"
    owners.write_text(json.dumps({"remote/": "guest-bea"}))
    p = a.phoning("send", "ana", "--to", "guest-bea", "--type", "propose", "--subject", "ship the remote call",
                 "--session", "A", "--data", '{"scope": "remote/link"}')
    assert p.returncode == 0, p.stderr
    eid = json.loads(p.stdout)["eid"]
    p = b.phoning("send", "beb", "--to", "ana", "--type", "accept", "--subject", "CONFIRMED",
                 "--reply-to", eid, "--session", "s")
    assert p.returncode == 0, p.stderr
    a.wait_record("ana", lambda r: r["env"]["type"] == "accept" and r["peer"] == "guest-beb", what="beb accept")
    board = a.phoning("board", "ana", "--owners", str(owners)).stdout
    assert "NOT EFFECTIVE" in board and "OPEN" in board, board
    p = b.phoning("send", "bea", "--to", "ana", "--type", "accept", "--subject", "approved by owner",
                 "--reply-to", eid, "--session", "s")
    assert p.returncode == 0, p.stderr

    def accepted() -> str | None:
        out = a.phoning("board", "ana", "--owners", str(owners)).stdout
        return out if "ACCEPTED" in out else None
    _wait(accepted, 60, "proposal ACCEPTED on the host's board")


def test_status_lists_lines_tunnel_and_watcher(net: Net, pair: Pair) -> None:
    p = pair.a.ok("status")
    lines = p.stdout.splitlines()
    assert any(ln.startswith("tunnel") and "none" in ln for ln in lines), p.stdout
    assert "watcher stopped" in lines, p.stdout
    url = re.escape(net.loop_url)
    assert any(re.match(rf"^ana\s+{url}\s+up\s+2 remote contacts$", ln) for ln in lines), p.stdout
    assert any(re.match(rf"^anb\s+{url}\s+up\s+2 remote contacts$", ln) for ln in lines), p.stdout
    js = pair.a.js("status")
    assert js["tunnel"] is None and js["watcher"] is False
    assert len(js["lines"]) == 2
    assert {ln["name"] for ln in js["lines"]} == {"ana", "anb"}
    # bea's contacts: ana, anb (remote) and beb (a local line, not remote)
    pb = pair.b.ok("status")
    lan = re.escape(net.lan_url)
    assert any(re.match(rf"^bea\s+{lan}\s+up\s+2 remote contacts$", ln) for ln in pb.stdout.splitlines())
    assert any(re.match(rf"^beb\s+{lan}\s+up\s+2 remote contacts$", ln) for ln in pb.stdout.splitlines())


# -- join edge cases (cheap links from `remote card`: no invite codes) --------------------------


def _host(net: Net, *names: str) -> tuple[Computer, str]:
    """A host whose up.json already lists the LAN URL as public; returns (computer, link)."""
    a = net.computer("cardA", net.loop_url, public=[net.lan_url])
    for n in names:
        a.init(n)
    card = a.js("card", "--who", "comp-x")
    return a, card["card"]


def test_card_is_a_code_free_link_on_the_public_url(net: Net) -> None:
    a, link = _host(net, "ca1", "ca2")
    card = _decode(link)
    assert card["relay"] == net.lan_url and card["who"] == "comp-x"
    assert [ln["name"] for ln in card["lines"]] == ["ca1", "ca2"]
    assert all(ln["code"] is None for ln in card["lines"])
    js = a.js("card", "ca2")
    assert js["lines"] == ["ca2"] and js["url"] == net.lan_url
    assert [ln["name"] for ln in _decode(js["card"])["lines"]] == ["ca2"]
    text = a.ok("card").stdout.strip()
    assert LINK_RE.fullmatch(text)
    # default who comes from the host name
    raw = socket.gethostname().split(".")[0].lower()
    who = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")[:24] or "computer"
    assert _decode(text)["who"] == who
    # --url overrides
    assert _decode(a.js("card", "--url", "https://x.example.com")["card"])["relay"] == "https://x.example.com"
    # errors
    assert a.remote("card", "ghost").returncode == 2
    assert a.remote("card", "--who", "Bad_Label").returncode == 2


def test_card_refuses_a_loopback_only_relay(net: Net) -> None:
    a = net.computer("loopA", net.loop_url)  # config relay without public
    a.init("lo1")
    p = a.remote("card")
    assert p.returncode == 2 and p.stdout.strip() == ""
    err = p.stderr.strip().splitlines()
    assert err[0].startswith("error:") and err[-1].startswith("hint:") and len(err) <= 2
    assert "phoning remote host" in err[-1]
    # no config file at all: same answer
    a.config.unlink()
    p = a.remote("card")
    assert p.returncode == 2 and "phoning remote host" in p.stderr


def test_join_refuses_an_invalid_link(net: Net) -> None:
    b = net.computer("badB", net.lan_url, config={"owners": []})
    b.init("bad1")
    for text in ("hello there", "call-card:1:AAAAA", "call-card:1:" + "e30"):  # e30 = "{}"
        p = b.remote("join", text)
        assert p.returncode == 2, (text, p.stdout, p.stderr)
        assert p.stderr.startswith("error: not a valid link:"), p.stderr
    assert b.contacts("bad1") == {}


def test_join_refuses_your_own_link(net: Net) -> None:
    a, link = _host(net, "own1", "own2")
    p = a.remote("join", link)
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert "error: this is your own link" in p.stderr
    assert a.contacts("own1") == {} and a.contacts("own2") == {}


def test_join_exits_1_when_the_links_relay_is_not_answering(net: Net) -> None:
    a, link = _host(net, "dead1")
    card = _decode(link)
    card["relay"] = f"http://127.0.0.1:{free_port()}"
    from phoning.remote import make_card

    dead = make_card({**card, "lines": [{**ln, "code": None} for ln in card["lines"]]})
    b = net.computer("deadB", net.lan_url, config={"owners": []})
    b.init("dz1")
    env = dict(b.env, PHONING_REMOTE_WAIT="2")
    t0 = time.time()
    p = b.remote("join", dead, env=env, timeout=60)
    assert p.returncode == 1, (p.stdout, p.stderr)
    assert "not answering" in p.stderr
    assert p.stderr.strip().splitlines()[-1].startswith("hint:")
    assert time.time() - t0 < 30
    assert b.contacts("dz1") == {}


def test_join_only_moves_explicitly_selected_lines(net: Net) -> None:
    a, link = _host(net, "mv-host")
    b = net.computer("moveB", net.lan_url, config={"owners": []})
    b.init("mv1")                         # on the link's relay URL already
    b.init("mv2", relay=net.loop_url)     # same server, but a URL the link does not use
    p = b.remote("join", link)
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert "--into" in p.stderr and "--name" in p.stderr
    p = b.remote("join", link, "--into", "mv1,mv2", "--no-move")
    assert p.returncode == 3, (p.stdout, p.stderr)
    assert f"error: mv2 uses relay {net.loop_url}, the link uses {net.lan_url}" in p.stderr
    assert "hint:" in p.stderr and "--no-move" in p.stderr
    # nothing changed for ANY line
    assert b.contacts("mv1") == {} and b.contacts("mv2") == {}
    assert b.relay_of("mv2") == net.loop_url

    p = b.remote("join", link, "--into", "mv2")
    assert p.returncode == 0, (p.stdout, p.stderr)
    assert f"moved mv2 to {net.lan_url}" in p.stdout
    assert "mv1" not in [ln.split()[1] for ln in p.stdout.splitlines() if ln.startswith("moved ")]
    relay_file = b.udir("mv2") / "relay"
    assert relay_file.read_text().strip() == net.lan_url and _mode(relay_file) == 0o600
    fh = a.fps["mv-host"]
    assert b.contacts("mv1") == {} and b.contacts("mv2") == {"mv-host": fh}
    now_compatible = b.js("join", link)
    assert now_compatible["moved"] == []
    assert now_compatible["added"] == [{"line": "mv1", "peer": "mv-host", "alias": "mv-host"}]
    assert b.contacts("mv1") == {"mv-host": fh}
    again = b.js("join", link)
    assert again["moved"] == [] and again["added"] == [] and len(again["kept"]) == 2


@pytest.mark.parametrize("existing_lines", [False, True])
def test_join_with_name_uses_a_dedicated_line(net: Net, existing_lines: bool) -> None:
    a, link = _host(net, "nm-host")
    b = net.computer("nameB", net.lan_url, config={"owners": []})
    if existing_lines:
        b.init("old", relay=net.loop_url)
        b.init("other")
        conflict = b.remote("join", link, "--name", "old")
        assert conflict.returncode == 3 and "fresh --name" in conflict.stderr
        assert b.remote("join", link, "--name", "newb", "--into", "old").returncode == 2
    else:
        p = b.remote("join", link)
        assert p.returncode == 2
        assert "hint:" in p.stderr and "--name" in p.stderr
        assert list(b.home.iterdir()) == []
    res = b.js("join", link, "--name", "newb")
    assert (b.udir("newb") / "identity").is_dir()
    assert b.relay_of("newb") == net.lan_url
    assert res["added"] == [{"line": "newb", "peer": "nm-host", "alias": "nm-host"}]
    assert res["room"] is None
    assert b.contacts("newb") == {"nm-host": a.fps["nm-host"]}
    # the new line got an operator: saved under owners and running, so it receives mail with no more setup
    from phoning import up as up_mod

    cfg = json.loads(b.config.read_text())
    assert any(o.get("name") == "newb" for o in cfg["owners"])
    holder = _wait(lambda: up_mod.owner_holder(str(b.udir("newb"))), 20, "newb's operator", 0.5)
    pid = int(holder["pid"])
    try:
        assert _alive(pid)
        again = b.js("join", link, "--name", "newb")
        assert again == {"added": [], "kept": res["added"], "moved": [], "room": None}
        assert up_mod.owner_holder(str(b.udir("newb"))) == holder
        if existing_lines:
            assert b.relay_of("old") == net.loop_url and b.relay_of("other") == net.lan_url
            assert b.contacts("old") == {} and b.contacts("other") == {}
    finally:
        os.kill(pid, signal.SIGTERM)
        _wait(lambda: not _alive(pid), 10, "newb's operator to stop", 0.2)


def test_join_from_stdin_picks_an_alias_when_names_collide(net: Net) -> None:
    a, link = _host(net, "twin")
    b = net.computer("twinB", net.lan_url, config={"owners": []})
    b.init("twin")  # a different identity with the same line name
    wrapped = "join this call please:\n" + "\n".join(link[i:i + 30] for i in range(0, len(link), 30)) + "\n"
    res = json.loads(b.ok("join", "-", "--json", input=wrapped).stdout)
    assert res["added"] == [{"line": "twin", "peer": "twin", "alias": "comp-x-twin"}]
    assert b.contacts("twin") == {"comp-x-twin": a.fps["twin"]}


def test_join_into_limits_the_lines(net: Net) -> None:
    a, link = _host(net, "in-host")
    b = net.computer("intoB", net.lan_url, config={"owners": []})
    b.init("in1")
    b.init("in2")
    assert b.remote("join", link, "--into", "in1,ghost").returncode == 2
    assert b.contacts("in1") == {} and b.contacts("in2") == {}
    res = b.js("join", link, "--into", "in2")
    assert [x["line"] for x in res["added"]] == ["in2"]
    assert b.contacts("in1") == {} and b.contacts("in2") == {"in-host": a.fps["in-host"]}


def test_status_reports_a_down_relay_and_exits_0(net: Net) -> None:
    c = net.computer("downC", net.loop_url)
    c.init("dn1")
    dead = f"http://127.0.0.1:{free_port()}"
    (c.udir("dn1") / "relay").write_text(dead + "\n")
    p = c.ok("status")
    assert any(re.match(rf"^dn1\s+{re.escape(dead)}\s+down\s+0 remote contacts$", ln)
               for ln in p.stdout.splitlines()), p.stdout


def test_cli_wires_remote_and_long_distance(net: Net) -> None:
    c = net.computer("cliC", net.loop_url)
    top = c.phoning("--help").stdout
    assert "remote" in top and "long-distance" in top
    p = c.phoning("long-distance", "status", "--json")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout)["lines"] == []
    helptext = c.remote("host", "--help").stdout
    assert "ciphertext" in helptext and "--stop" in helptext


# -- host with a (fake) Cloudflare quick tunnel -----------------------------------------------


def _fake_cloudflared(root: Path, url: str) -> Path:
    """Logs its argv, prints a quick-tunnel banner with `url` on stderr, then stays up until TERM."""
    return _script(root / "fakebin" / "cloudflared",
                   f'echo "$@" >> "{root}/cloudflared-calls.log"\n'
                   'echo "INF Requesting new quick Tunnel on trycloudflare.com..." 1>&2\n'
                   f'echo "INF |  {url}  |" 1>&2\n'
                   "trap 'kill $! 2>/dev/null; exit 0' TERM\n"
                   "sleep 300 &\nwait\n")


def test_host_starts_a_tunnel_restarts_the_relay_and_stop_undoes_it(net: Net) -> None:
    port2 = free_port()
    h = net.computer("tunH", f"http://127.0.0.1:{port2}", config={})
    url2 = f"http://127.0.0.1:{port2}"
    db = h.root / "relaydb" / "relay.db"
    db.parent.mkdir(parents=True)
    h.config.write_text(json.dumps({"relay": {"url": url2, "db": str(db),
                                              "public": ["https://old-one.trycloudflare.com",
                                                         "http://10.9.9.9:1"]},
                                    "owners": []}))
    fake = _fake_cloudflared(h.root, TUNNEL_URL)
    h.env.update(PHONING_CLOUDFLARED=str(fake), PHONING_REMOTE_WAIT="3")
    # a relay already running with a loopback-only audience: host must restart it
    old = subprocess.Popen(["retalk-server", "--host", "127.0.0.1", "--port", str(port2),
                            "--audience", url2, "--db", str(db)],
                           env=dict(os.environ, RETALK_SERVER_DB=str(db)), cwd=db.parent,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    try:
        _wait(lambda: _port_open("127.0.0.1", port2), 15, "old relay", 0.1)
        first = h.js("host")
        assert first["url"] == TUNNEL_URL and first["tunnel"] is True
        assert first["restarted"] is True and first["link"] is None
        old.wait(timeout=15)  # our relay got SIGTERM
        _wait(lambda: _port_open("127.0.0.1", port2), 20, "restarted relay", 0.2)
        cmds = [_command_of(p) for p in _listener_pids(port2)]
        assert any("retalk-server" in c and TUNNEL_URL in c for c in cmds), cmds

        cfg = json.loads(h.config.read_text())
        assert cfg["relay"]["public"] == [TUNNEL_URL, "http://10.9.9.9:1"]
        assert "bind" not in cfg["relay"]  # a tunnel needs no extra interface
        assert len(list(h.state.glob("up.json.bak-*"))) == 1
        tj = json.loads((h.state / "tunnel.json").read_text())
        assert tj["url"] == TUNNEL_URL and tj["port"] == port2
        assert _alive(int(tj["pid"])) and "cloudflared" in _command_of(int(tj["pid"]))
        assert _mode(h.state / "tunnel.json") == 0o600 and _mode(h.state / "tunnel.log") == 0o600
        calls = (h.root / "cloudflared-calls.log").read_text().splitlines()
        quick = h.state / "cloudflared-quick.yml"
        assert calls == [f"tunnel --config {quick} --no-autoupdate --url http://127.0.0.1:{port2}"]
        assert quick.read_bytes() == b"" and _mode(quick) == 0o600

        h.init("h1")
        p = h.ok("host")
        assert f"relay reachable at {TUNNEL_URL}" in p.stdout
        assert "send this link to the other computer (a DM, not a public channel):" in p.stdout
        assert ("they paste this into their coding agent: Set up Phoning and join this call: "
                "https://github.com/verygoodbread/phoning call-card:1:") in p.stdout
        assert "is not answering yet" in p.stdout + p.stderr  # fake tunnel never answers
        link = next(ln for ln in p.stdout.splitlines() if LINK_RE.fullmatch(ln.strip()))
        card = _decode(link)
        assert card["relay"] == TUNNEL_URL and card["room"] == f"call-{card['who']}"
        assert [ln["name"] for ln in card["lines"]] == ["h1"]
        code = card["lines"][0]["code"]
        assert (h.udir("h1") / "remote-invite").read_text().strip() == code
        # reused: no second tunnel, no second config write
        assert len((h.root / "cloudflared-calls.log").read_text().splitlines()) == 1
        assert len(list(h.state.glob("up.json.bak-*"))) == 1
        watcher = int((h.home / ".remote-watch.pid").read_text().strip())
        assert "phoning.remote watch" in _command_of(watcher)
        st = h.ok("status").stdout.splitlines()
        assert any(ln.startswith(f"tunnel {TUNNEL_URL}") and "up" in ln for ln in st), st
        assert "watcher running" in st

        stop = h.ok("host", "--stop")
        assert "stopped" in stop.stdout
        _wait(lambda: not _alive(int(tj["pid"])), 10, "tunnel to stop", 0.2)
        _wait(lambda: not _alive(watcher), 10, "watcher to stop", 0.2)
        assert not (h.state / "tunnel.json").exists()
        assert not (h.home / ".remote-watch.pid").exists()
        assert not (h.udir("h1") / "remote-invite").exists()
        listed = [json.loads(ln) for ln in h.retalk("h1", "invite", "list", "--json").stdout.splitlines()
                  if ln.startswith("{")]
        # retalk keeps revoked codes in the list, marked inactive
        assert all(o.get("active") is False for o in listed if o.get("code") == code)
        assert TUNNEL_URL not in json.loads(h.config.read_text())["relay"].get("public", [])
        assert h.ok("host", "--stop").returncode == 0  # nothing running: still fine
    finally:
        h.cleanup()
        if old.poll() is None:
            old.terminate()
            old.wait(timeout=10)
        _kill_relay_on(port2)


def test_host_reports_a_tunnel_that_never_starts(net: Net) -> None:
    port2 = free_port()
    h = net.computer("tunFail", f"http://127.0.0.1:{port2}", config={})
    h.config.write_text(json.dumps({"relay": {"url": f"http://127.0.0.1:{port2}",
                                              "db": str(h.root / "relay.db")}, "owners": []}))
    before = h.config.read_bytes()
    bad = _script(h.root / "fakebin" / "cloudflared", 'echo "ERR failed to request quick Tunnel" 1>&2\nexit 1\n')
    h.env["PHONING_CLOUDFLARED"] = str(bad)
    try:
        t0 = time.time()
        p = h.remote("host", timeout=90)
        assert p.returncode == 1, (p.stdout, p.stderr)
        assert "error: the Cloudflare tunnel did not start; see" in p.stderr
        assert str(h.state / "tunnel.log") in p.stderr
        assert time.time() - t0 < 60
        assert h.config.read_bytes() == before
        assert not (h.state / "tunnel.json").exists()
        assert not _port_open("127.0.0.1", port2)
    finally:
        h.cleanup()
        _kill_relay_on(port2)


@pytest.mark.skipif(shutil.which("cloudflared") is not None
                    or any(Path(p).exists() for p in ("/opt/homebrew/bin/cloudflared",
                                                      "/usr/local/bin/cloudflared")),
                    reason="a real cloudflared is installed; never risk starting a real tunnel")
def test_host_without_cloudflared_says_how_to_install_it(net: Net) -> None:
    h = net.computer("noCf", net.loop_url)
    before = h.config.read_bytes()
    env = dict(h.env)
    env.pop("PHONING_CLOUDFLARED")
    p = h.remote("host", env=env)
    assert p.returncode == 2
    err = p.stderr.strip().splitlines()
    assert err[0] == "error: cloudflared is not installed"
    assert err[1].startswith("hint:") and "brew install cloudflared" in err[1]
    assert h.config.read_bytes() == before


# -- fixes from the 2026-11-22 skeptic review (docs/spec/remote.md) --------------------------------


def test_host_refuses_to_tunnel_a_port_held_by_another_program(net: Net) -> None:
    """Review #2: the origin is checked BEFORE any tunnel exists, so a dev server is never published."""
    port = free_port()
    h = net.computer("devH", f"http://127.0.0.1:{port}", config={})
    h.config.write_text(json.dumps({"relay": {"url": f"http://127.0.0.1:{port}",
                                              "db": str(h.root / "r" / "relay.db")}, "owners": []}))
    fake = _fake_cloudflared(h.root, TUNNEL_URL)
    h.env.update(PHONING_CLOUDFLARED=str(fake), PHONING_REMOTE_WAIT="2")
    dev = subprocess.Popen([sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                           cwd=h.root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL)
    try:
        _wait(lambda: _port_open("127.0.0.1", port), 15, "dev server", 0.1)
        p = h.remote("host")
        assert p.returncode == 3, (p.stdout, p.stderr)
        assert "held by another program" in p.stderr
        assert not (h.root / "cloudflared-calls.log").exists()  # no tunnel was ever started
        assert not (h.state / "tunnel.json").exists()
        assert dev.poll() is None  # and the dev server was left alone
    finally:
        dev.terminate()
        dev.wait(timeout=10)


def test_cold_host_starts_its_relay(net: Net) -> None:
    """Review #9: a computer hosting for the first time has no relay running yet; host starts it."""
    port = free_port()
    h = net.computer("coldH", f"http://127.0.0.1:{port}", config={})
    db = h.root / "r" / "relay.db"
    h.config.write_text(json.dumps({"relay": {"url": f"http://127.0.0.1:{port}", "db": str(db)},
                                    "owners": []}))
    fake = _fake_cloudflared(h.root, TUNNEL_URL)
    h.env.update(PHONING_CLOUDFLARED=str(fake), PHONING_REMOTE_WAIT="2")
    assert not _port_open("127.0.0.1", port)
    try:
        res = h.js("host")
        assert res["link"] is None and res["url"] == TUNNEL_URL
        _wait(lambda: _port_open("127.0.0.1", port), 20, "relay started by host", 0.2)
        cmds = [_command_of(p) for p in _listener_pids(port)]
        assert any("retalk-server" in c and TUNNEL_URL in c for c in cmds), cmds
    finally:
        h.ok("host", "--stop")
        for pid in _listener_pids(port):
            if "retalk-server" in _command_of(pid):
                os.kill(pid, signal.SIGTERM)
        _wait(lambda: not _port_open("127.0.0.1", port), 15, "relay to stop", 0.2)


def test_rejoin_sends_nothing_new(pair: Pair) -> None:
    """Review #16: an unchanged join re-adds no members and sends no second 'joined the call'."""
    def outs(name: str) -> int:
        log = pair.b.udir(name) / "mailbox" / "log.jsonl"
        return sum(1 for ln in log.read_text().splitlines() if '"direction": "out"' in ln)
    before = {n: outs(n) for n in ("bea", "beb")}
    pair.b.ok("join", pair.link, "--who", "comp-b")
    assert {n: outs(n) for n in ("bea", "beb")} == before
