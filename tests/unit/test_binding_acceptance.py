"""Acceptance: decisions bind to the transport-verified sender, not the claimed envelope principal.

The attack: peer `observer` can send a valid envelope claiming
from.principal = "codex" and "accept" a scope codex owns. The retalk transport already tells us
who really sent it (the saved alias of the sender's verified fingerprint) — that, not the claim,
is the principal for authority. Claimed fields stay as untrusted metadata.
"""

from __future__ import annotations

from typing import Any

from phoning import board
from phoning.envelope import make

OWNERS = {"schemas/": "codex"}


def _in(peer: str, env: dict[str, Any], seq: int) -> dict[str, Any]:
    return {"seq": seq, "direction": "in", "tid": f"t{seq}", "peer": peer, "room": "ops",
            "recv_ts": "2026-10-11T09:00:00Z", "env": env}


def _out(env: dict[str, Any], seq: int) -> dict[str, Any]:
    return {"seq": seq, "direction": "out", "tid": f"t{seq}", "peer": None, "room": "ops",
            "recv_ts": "2026-10-11T09:00:00Z", "env": env}


def _proposal() -> dict[str, Any]:
    return make("propose", "claude", "A", "add a field", data={"scope": "schemas/api.v1.json"})


def test_forged_principal_cannot_accept() -> None:
    prop = _proposal()
    forged = make("accept", "codex", "evil", "CONFIRMED", reply_to=prop["eid"])
    b = board.fold([_out(prop, 1), _in("observer", forged, 2)], OWNERS, local="claude")
    text = board.render(b)
    assert "ACCEPTED" not in text
    assert "mismatch" in text.lower()


def test_real_owner_accept_is_effective() -> None:
    prop = _proposal()
    ok = make("accept", "codex", "vega", "approved", reply_to=prop["eid"])
    b = board.fold([_out(prop, 1), _in("codex", ok, 2)], OWNERS, local="claude")
    assert "ACCEPTED" in board.render(b)


def test_outbound_records_use_the_local_principal() -> None:
    prop = make("propose", "codex", "vega", "self scope", data={"scope": "schemas/x"})
    acc = make("accept", "codex", "vega", "mine", reply_to=prop["eid"])
    b = board.fold([_in("codex", prop, 1), _out(acc, 2)], OWNERS, local="codex")
    assert "ACCEPTED" in board.render(b)


def test_status_who_uses_transport_identity_and_declared_session() -> None:
    st = make("status", "codex", "lyra", "standup", data={"state": "IDLE"})
    b = board.fold([_in("observer", st, 1)], OWNERS, local="claude")
    text = board.render(b)
    assert "observer" in text  # the real sender, not the claimed "codex"
