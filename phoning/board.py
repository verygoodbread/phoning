"""Board: a pure fold over envelopes into a human-readable markdown view.

Authority is bound to the **transport-verified sender**, not the principal the
sender claims: for inbound records the trusted principal is ``rec["peer"]``
(the local saved alias of the verified fingerprint), for outbound records it is
``local``. ``env.from.principal`` is kept as untrusted metadata.

``owners`` maps a scope prefix to the principal(s) allowed to decide a proposal
in that scope. The longest matching prefix wins; when nothing matches only the
proposer may decide. Decisions from a mismatched or non-owner sender are
recorded but marked ``NOT EFFECTIVE``.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Mapping, Sequence
from typing import Any

from phoning import text as text_mod


def _matches(pattern: str, scope: str) -> bool:
    """Return True when ``pattern`` covers ``scope``."""
    if not pattern:
        return False
    if "*" in pattern or "?" in pattern or "[" in pattern:
        return fnmatch.fnmatch(scope, pattern)
    return scope == pattern or scope.startswith(pattern)


def owners_of(
    owners: Mapping[str, str | Sequence[str]], scope: str
) -> list[str]:
    """Return the effective owner principals for ``scope``.

    Longest matching prefix wins. Returns ``[]`` when nothing matches;
    callers fall back to the proposer.
    """
    best: list[str] = []
    best_len = -1
    for pat, who in owners.items():
        if not isinstance(pat, str):
            continue
        if not _matches(pat, scope):
            continue
        if len(pat) <= best_len:
            continue
        if isinstance(who, str):
            best = [who]
        elif isinstance(who, Sequence):
            best = [str(x) for x in who]
        else:
            best = [str(who)]
        best_len = len(pat)
    return best


def _env_of(item: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap a mailbox record to its envelope, tolerating raw envelopes."""
    inner: Any = item.get("env")
    if isinstance(inner, dict) and "type" in inner:
        return inner
    return item


def _string_list(raw: Any) -> list[str]:
    """Coerce ``raw`` to a list of strings (string -> single element)."""
    if isinstance(raw, str):
        return [raw] if raw else []
    if isinstance(raw, Sequence):
        return [str(x) for x in raw]
    if raw is None:
        return []
    return [str(raw)]


def _trusted_principal(
    item: Mapping[str, Any], env: Mapping[str, Any], local: str | None
) -> tuple[str, str]:
    """Return ``(trusted, claimed)`` principals for one record.

    Inbound records carry the transport-verified alias in ``peer``; outbound
    records belong to the local principal (or the claimed principal when
    ``local`` is None).
    """
    frm_any: Any = env.get("from")
    frm: Mapping[str, Any] = frm_any if isinstance(frm_any, Mapping) else {}
    claimed_any: Any = frm.get("principal")
    claimed = claimed_any if isinstance(claimed_any, str) and claimed_any else "?"
    direction_any: Any = item.get("direction")
    if direction_any == "out":
        trusted = local if local else claimed
        return trusted, claimed
    peer_any: Any = item.get("peer")
    if isinstance(peer_any, str) and peer_any:
        return peer_any, claimed
    return claimed, claimed


def _declared_session(env: Mapping[str, Any]) -> str:
    """Return the declaring session (self-asserted, never authenticated)."""
    frm_any: Any = env.get("from")
    frm: Mapping[str, Any] = frm_any if isinstance(frm_any, Mapping) else {}
    se_any: Any = frm.get("session")
    return se_any if isinstance(se_any, str) and se_any else "?"


def fold(
    records: Sequence[Mapping[str, Any]],
    owners: Mapping[str, str | Sequence[str]],
    *,
    local: str | None = None,
) -> dict[str, Any]:
    """Fold records (or raw envelopes, oldest first) into a board dict.

    Authority and identity come from the transport-verified sender: inbound
    records are attributed to ``rec["peer"]``, outbound ones to ``local``.
    When that differs from ``env.from.principal`` the claim is kept as
    untrusted metadata and any decision from the record fails closed.

    Returns ``{"status": ..., "proposals": ..., "receipts": ...,
    "open_asks": ...}`` with ``"asks"`` kept as an alias of ``open_asks``.
    Never raises on malformed entries; they are skipped where unusable.
    """
    status: dict[tuple[str, str], dict[str, str]] = {}
    proposals: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    receipts: list[Mapping[str, Any]] = []
    asks: list[Mapping[str, Any]] = []
    envs: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
    replied_to: set[str] = set()

    for item in records:
        if not isinstance(item, Mapping):
            continue
        env = _env_of(item)
        if not isinstance(env, Mapping):
            continue
        envs.append((item, env))
        rt: Any = env.get("reply_to")
        if isinstance(rt, str) and rt:
            replied_to.add(rt)

    for item, env in envs:
        t: Any = env.get("type")
        if not isinstance(t, str):
            continue
        data: Any = env.get("data")
        data_dict: Mapping[str, Any] | None = (
            data if isinstance(data, dict) else None
        )
        if t == "status":
            if data_dict is None:
                continue
            state: Any = data_dict.get("state")
            if state not in ("ACTIVE", "IDLE", "BLOCKED"):
                continue
            principal, _claimed = _trusted_principal(item, env, local)
            session = _declared_session(env)
            subj: Any = env.get("subject")
            ts: Any = env.get("ts")
            status[(principal, session)] = {
                "state": str(state),
                "subject": str(subj) if isinstance(subj, str) else "",
                "ts": str(ts) if isinstance(ts, str) else "",
            }
        elif t == "propose":
            eid: Any = env.get("eid")
            if not (isinstance(eid, str) and eid):
                continue
            if eid in proposals:
                continue
            principal, claimed = _trusted_principal(item, env, local)
            session = _declared_session(env)
            scope_raw: Any = data_dict.get("scope") if data_dict is not None else ""
            scope = scope_raw if isinstance(scope_raw, str) else ""
            matched = owners_of(owners, scope)
            if not matched and principal:
                matched = [principal]
            subj2: Any = env.get("subject")
            proposals[eid] = {
                "eid": eid,
                "scope": scope,
                "subject": str(subj2) if isinstance(subj2, str) else "",
                "by": {"principal": principal, "session": session},
                "claimed": claimed,
                "owners": matched,
                "state": "OPEN",
                "decisions": [],
            }
            order.append(eid)
        elif t in ("accept", "reject"):
            rt2: Any = env.get("reply_to")
            if not (isinstance(rt2, str) and rt2):
                continue
            prop = proposals.get(rt2)
            if prop is None:
                continue
            principal, claimed = _trusted_principal(item, env, local)
            session = _declared_session(env)
            dts: Any = env.get("ts")
            eff_list = _string_list(prop.get("owners", []))
            mismatch = principal != claimed
            effective = principal in eff_list if eff_list else True
            if mismatch:
                effective = False
                reason = (
                    f"claimed principal mismatch "
                    f"(claimed {claimed}, sent by {principal})"
                )
            else:
                reason = ""
            if not effective and not eff_list:
                reason = "no owner for scope"
            elif not effective and not mismatch and eff_list:
                reason = "needs " + "/".join(eff_list)
            decisions: Any = prop.get("decisions")
            if not isinstance(decisions, list):
                decisions = []
                prop["decisions"] = decisions
            decisions.append(
                {
                    "type": t,
                    "by": {"principal": principal, "session": session},
                    "claimed": claimed,
                    "effective": bool(effective),
                    "reason": reason,
                    "ts": str(dts) if isinstance(dts, str) else "",
                }
            )
            if effective and prop.get("state") == "OPEN":
                prop["state"] = "ACCEPTED" if t == "accept" else "REJECTED"
        elif t == "receipt":
            receipts.append(env)
        elif t == "ask":
            eid3: Any = env.get("eid")
            if isinstance(eid3, str) and eid3:
                asks.append(env)

    open_asks = [a for a in asks if str(a.get("eid", "")) not in replied_to]
    ordered = [proposals[eid] for eid in order if eid in proposals]
    return {
        "status": status,
        "proposals": ordered,
        "receipts": receipts,
        "open_asks": open_asks,
        "asks": open_asks,
    }


def render(board: Mapping[str, Any]) -> str:
    """Render a folded board as markdown.

    Each line is passed through :func:`phoning.text.clean_line` so untrusted
    subjects, scopes and sessions can never forge board structure.
    """
    lines: list[str] = [
        "# Board (derived)",
        "",
        "_Sessions are self-asserted and shown as declared (declared session)._",
        "",
        "## Who is doing what",
    ]
    status_any: Any = board.get("status", {})
    if isinstance(status_any, dict):
        try:
            pairs = sorted(
                status_any.items(),
                key=lambda kv: (
                    str(kv[0][0]) if isinstance(kv[0], tuple) else str(kv[0]),
                    str(kv[0][1])
                    if isinstance(kv[0], tuple) and len(kv[0]) > 1
                    else "",
                ),
            )
        except Exception:
            pairs = list(status_any.items())
        for key, val in pairs:
            if isinstance(key, tuple) and len(key) == 2:
                pr, se = str(key[0]), str(key[1])
            else:
                continue
            if isinstance(val, (list, tuple)) and len(val) >= 3:
                st, sub, ts = str(val[0]), str(val[1]), str(val[2])
            elif isinstance(val, Mapping):
                st = str(val.get("state", ""))
                sub = str(val.get("subject", ""))
                ts = str(val.get("ts", ""))
            else:
                continue
            lines.append(
                f"- {pr}/{se} (declared session): **{st}** - {sub} ({ts})"
            )
    lines += ["", "## Proposals"]
    props_any: Any = board.get("proposals", [])
    if isinstance(props_any, list):
        for p in props_any:
            if not isinstance(p, Mapping):
                continue
            state = str(p.get("state", "OPEN")).upper()
            subject = str(p.get("subject", ""))
            scope = str(p.get("scope", ""))
            owner_list = _string_list(p.get("owners", []))
            owners_str = "/".join(owner_list) if owner_list else "(unowned)"
            by_any: Any = p.get("by", {})
            if isinstance(by_any, Mapping):
                by_pr = str(by_any.get("principal", "?"))
                by_se = str(by_any.get("session", "?"))
            else:
                by_pr, by_se = "?", "?"
            lines.append(
                f"- [{state}] {subject}  scope=`{scope}` "
                f"owner={owners_str}  by {by_pr}/{by_se} (declared session)"
            )
            decs_any: Any = p.get("decisions", [])
            if isinstance(decs_any, list):
                for d in decs_any:
                    if not isinstance(d, Mapping):
                        continue
                    dtype = str(d.get("type", ""))
                    dby: Any = d.get("by", {})
                    if isinstance(dby, Mapping):
                        dpr = str(dby.get("principal", "?"))
                        dse = str(dby.get("session", "?"))
                    else:
                        dpr, dse = "?", "?"
                    if bool(d.get("effective", False)):
                        tag = "effective"
                    else:
                        reason = str(d.get("reason", ""))
                        tag = (
                            f"NOT EFFECTIVE ({reason})"
                            if reason
                            else f"NOT EFFECTIVE (needs {owners_str})"
                        )
                    lines.append(
                        f"    - {dtype} by {dpr}/{dse} (declared session): {tag}"
                    )
    lines += ["", "## Receipts"]
    rec_any: Any = board.get("receipts", [])
    if isinstance(rec_any, list):
        for r in rec_any:
            if not isinstance(r, Mapping):
                continue
            subj = str(r.get("subject", ""))
            f_any: Any = r.get("from", {})
            if isinstance(f_any, Mapping):
                fpr = str(f_any.get("principal", "?"))
                fse = str(f_any.get("session", "?"))
            else:
                fpr, fse = "?", "?"
            refs: Any = r.get("refs", [])
            lines.append(
                f"- {subj} <- {fpr}/{fse} refs="
                f"{list(refs) if isinstance(refs, list) else refs}"
            )
    lines += ["", "## Open asks"]
    open_any: Any = board.get("open_asks", board.get("asks", []))
    if isinstance(open_any, list):
        for a in open_any:
            if not isinstance(a, Mapping):
                continue
            subj_a = str(a.get("subject", ""))
            fa: Any = a.get("from", {})
            if isinstance(fa, Mapping):
                apr = str(fa.get("principal", "?"))
                ase = str(fa.get("session", "?"))
            else:
                apr, ase = "?", "?"
            ts_a = str(a.get("ts", ""))
            lines.append(f"- {subj_a} by {apr}/{ase} ({ts_a})")
    return "\n".join(text_mod.clean_line(line) for line in lines)
