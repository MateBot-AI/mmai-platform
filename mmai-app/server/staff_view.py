"""Staff-facing view of the T6 review / brief / introduction history for one group (8 Nov 2026).

Read-only: this module turns the rows ai_queue / review_loop write (review_rounds, group_briefs) into plain-language payloads
for the dashboard. It never decides readiness (ai_queue.is_ready / can_circulate do) and never shows register codes or raw
fact ids as prose (T10): every cited fact is rendered as "<person>'s <label>: <value>" in a separate evidence list, with the
AI's note kept as the readable sentence.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from .db import pj, row, rows

log = logging.getLogger("mmai.staff_view")

FACT_LABELS = {
    "doing": "what they do in Australia", "school": "institution", "course": "course", "level": "study level",
    "routine": "daily routine", "tidy": "tidiness", "social": "social style", "guests": "guests", "diet": "diet",
    "party": "enquiring as", "party_size": "people in the party", "move_in": "move-in date", "move_out": "move-out date", "couples": "couples in the party",
    "type_label": "apartment type", "suburb": "suburb", "bills_included": "bills included", "available_from": "available from",
    "lease_start": "lease start", "lease_end": "lease end", "rate_kind": "rate", "total_weekly": "whole-apartment weekly rent", "extra_bed": "extra-bed charge",
    "campus_km": "distance to campus (km)", "dates": "dates check", "budget": "budget check", "beds": "beds check", "smoking": "smoking check",
    "adults": "18+ declaration check", "capacity": "capacity check", "parking_charge": "car parking charge",
    # apartment packet fields that previously fell back to the raw id
    "stay_days": "length of stay (days)", "rate_band": "rate band",
    # housing-answer and household-budget facts the stage C packet carries (wording agreed with the queue owner, 8 Nov 2026)
    "room_first": "first-choice room arrangement", "rooms_accepted": "room arrangements accepted", "apt_sizes_accepted": "apartment sizes accepted",
    "bunk_ok": "bunk bed accepted", "mate_max": "maximum number of other housemates accepted (not counting themselves)", "smoke": "smokes", "smoke_ok": "okay living with a smoker",
    "total_weekly_rent_exact": "whole-apartment weekly rent including the extra bed", "whole_apartment_max_exact": "household's combined weekly budget maximum",
    "allowance_fraction": "allowed rent above the household maximum (as a fraction, 0.1 = one tenth)", "limit_exact": "exact weekly rent limit for the household",
    # increment A (9 Nov 2026, agreed with the lead builder): subject-attributed budget evidence R.A<id>.budget.share.<ref> /
    # .stated_max.<ref> and the housemates check R.A<id>.housemates with its detail keys
    "share": "proposed weekly share", "stated_max": "stated weekly maximum", "housemates": "housemates check",
    "accepts_at_most_other_housemates": "most other housemates they accept (not counting themselves)",
    "other_people_in_this_group": "other people in this group (not counting themselves)",
    "counts": "what is counted",
}
# 5-segment subject facts: the fixed plain wording per field. The share is a proposal, never an agreed amount (A12/GP13); the
# stated maximum is the person's own answer, sometimes reported by the lead for the whole party (I16(d)).
_SUBJECT_FACT = {
    "share": "{who}'s proposed weekly share at {where} (a proposal for the group's agreement, not an agreed amount)",
    "stated_max": "{who}'s stated weekly maximum (budget check at {where})",
}
_REPORTED_BY = " (reported by {who} for the party)"


def _subject_text(check: str, field: str, who: str, where: str) -> str:
    """R.A12.budget.share.U1 -> \"Ana's proposed weekly share at 12 Smith St (...)\"; unknown field -> generic person-attributed words."""
    tpl = _SUBJECT_FACT.get(field) if check == "budget" else None
    if tpl:
        return tpl.format(who=who, where=where)
    return f"{who}'s {_label(field)} ({_label(check)} at {where})"
_BIT = re.compile(r"^(?P<id>[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)+)(?:=(?P<value>.*?)|: (?P<missing>not recorded))(?: \(reported by (?P<src>[A-Za-z0-9_\-]+) for the party\))?$")


def _label(field: str) -> str:
    if field in FACT_LABELS:
        return FACT_LABELS[field]
    if field.startswith("feat_"):
        return field[5:].replace("_", " ")
    return field.replace("_", " ")


def _check_label(check: str, detail: str | None) -> str:
    """'budget' -> "budget check"; ('budget', 'limit_exact') -> "exact weekly rent limit for the household (budget check)"."""
    if detail:
        return f"{_label(detail)} ({_label(check)})"
    return _label(check)


class EarlierMembers(dict):
    """Name map for a round written about an EARLIER member set: refs cannot be attributed to today's members, so every
    lookup answers with a neutral label instead of a (possibly wrong) name."""
    def get(self, key, default=None):
        return "an earlier member"

    def __contains__(self, key):
        return False


# T10: register codes the AI may quote in prose become the plain requirement they stand for (never the code). Any other
# register-shaped token (a section letter + number, optionally a sub-letter in brackets) becomes "the agreed requirements".
RULE_LABELS = {
    "B3": "the non-smoking rule", "B5": "the one whole lease with everyone jointly liable", "P3": "the group size of two to seven people",
    "A4": "the bedding options", "A5": "the extra-bed charge", "A6": "the 6- and 12-month rate rules", "A7": "the weekly pricing basis", "A8": "the monthly conversion",
    "A9": "the minimum lease period", "A10": "the lease cycle and accepted exit windows", "A11": "the internet-not-included rule", "A12": "the rent split within the group",
    "A14": "the apartment type codes", "A16": "the total-rent allowance", "A17": "the vacancy cap and date checks",
    "GP2": "the staff-led introduction", "GP3": "the replacement rules", "GP5": "every bedroom occupied", "GP6": "the apartment suggestions for the group",
    "GP7": "one group per person", "GP8": "no holding of apartments", "GP9": "the rejected-member rules", "GP10": "the no-replacement-found rules",
    "GP11": "the bring-a-mate (linked friends) rules", "GP12": "at least one outside housemate", "GP13": "individual group responses", "GP14": "apartment rejections and reconsideration",
    "I7": "the must-match / preference classification", "I8": "sex and age (introductions and the sex / age preferences)", "I11": "the minimum age", "I14": "the budget question (the most you can pay)",
    "I16": "the couple and group details", "T4": "the matching approach", "T5": "data retention", "T6": "the AI review before circulation", "T10": "the plain staff interface",
}
_RULE_CONTEXT = re.compile(r"(?:\b(?:under|per|rule|rules|see|within|against|breach(?:es|ing)?|violat(?:es|ing)|clause|by|in)\s+|\(\s*)$", re.I)
# staff-render repair (9 Nov 2026): a bare A<id> that is NOT a register code (A54, A82 ...) is always an apartment reference. It
# reads as the address when the id is in the text's own apartment basis (the group's list, or the saved review basis for older
# text) and otherwise as an explicit neutral phrase - never as "the agreed requirements", which mislabelled apartments.
# "In A54" / "cheaper in A50" (weak cues: in, by, within, against) keep the address when the apartment is in the basis; the
# strong rule cues (under, per, rule(s), see, clause, breach, violat, an opening bracket) and a sub-letter still read as a rule
# for the register codes A4-A17.
_WEAK_CUE = re.compile(r"\b(?:in|by|within|against)\s+$", re.I)
APT_UNKNOWN = "an apartment no longer on this group's list"
# L (8 Nov 2026): a BARE machine key the AI copied into prose ("limit 1348.6 at allowance_fraction 0.1", "recorded bunk_ok of No")
# becomes its plain label in the SAME single pass as the refs above. Only the known underscore-bearing FACT_LABELS keys, exact
# lowercase, whole token (the shared lookarounds keep prefix_allowance_fraction / bunk_ok_suffix literal); single-word keys such as
# routine, social, diet or budget are ordinary English and are never touched. The value next to the key is left exactly as written.
MACHINE_KEYS = tuple(sorted((k for k in FACT_LABELS if "_" in k), key=len, reverse=True))
_REF = re.compile(r"(?<![A-Za-z0-9_-])(?:R\.(?P<rapt>A\d+)\.(?P<rfield>[a-z][a-z0-9_]*)(?:\.(?P<rsub>[a-z][a-z0-9_]*)(?:\.(?P<rref>[UI]\d+(?:-m\d+)?))?)?|(?P<apt>A\d+)(?:\.(?P<afield>[a-z][a-z0-9_]*))?"
                  r"|(?P<who>[UI]\d+(?:-m\d+)?)(?:\.(?P<field>[a-z][a-z0-9_]*))?|(?P<code>(?:GP|[ABCHIPT])\d+[a-z]?)"
                  r"|(?P<key>" + "|".join(re.escape(k) for k in MACHINE_KEYS) + r"))(?P<sub>\([a-z]\))?(?![A-Za-z0-9_-])")
_PERSON_REF = re.compile(r"^[UI]\d+(?:-m\d+)?$")
_ORDINAL = ("one member", "another member", "a third member", "a fourth member", "a fifth member", "a sixth member", "a seventh member", "an eighth member")


class Namer:
    """ref -> words for ONE piece of text. Known refs use the row's own immutable map; a ref the map does not know (an earlier
    member set, a legacy row, an unexpected ref) gets a stable neutral label in order of first appearance ("one member",
    "another member", ...) so a sentence stays readable without attributing it to anyone it may not describe."""
    def __init__(self, names: dict | None):
        self.names = names if isinstance(names, dict) and not isinstance(names, EarlierMembers) else {}
        self.neutral: dict[str, str] = {}

    def __call__(self, ref: str) -> str:
        if ref in self.names and self.names[ref]:
            return self.names[ref]
        if ref not in self.neutral:
            self.neutral[ref] = _ORDINAL[len(self.neutral)] if len(self.neutral) < len(_ORDINAL) else "a further member"
        return self.neutral[ref]


def plain_text(text, names: dict | None, apts: dict | None = None, namer: Namer | None = None) -> str:
    """T10: one-pass substitution of the AI's internal references in a sentence - U1 / U1-m1 / I2 -> the person's name from the
    row's immutable map (deleted -> "a former member", unknown -> neutral), U1.routine -> "Ana's daily routine", A12 -> the
    address, R.A12.budget -> "budget check at <address>". Replacements are never re-scanned, so inserted names and addresses
    are left exactly as they are."""
    if not text:
        return text or ""
    apts = apts or {}
    who = namer or Namer(names)

    def rule(code: str) -> str:
        return RULE_LABELS.get(code.upper().rstrip("abcdefghijklmnopqrstuvwxyz"), RULE_LABELS.get(code, "the agreed requirements"))

    def repl(m: re.Match) -> str:
        if m.group("key"):                              # bare machine key in prose -> its plain label; nothing else changes
            return _label(m.group("key")) + (m.group("sub") or "")
        if m.group("rapt"):                             # R.A12.budget, or a detail of a check: R.A12.budget.limit_exact
            if m.group("rref"):                         # R.A12.budget.share.U1-m1: one person's own evidence, consumed whole in this pass
                return _subject_text(m.group("rfield"), m.group("rsub"), who(m.group("rref")), apts.get(m.group("rapt"), "the apartment"))
            return f"{_check_label(m.group('rfield'), m.group('rsub'))} at {apts.get(m.group('rapt'), 'the apartment')}"
        if m.group("apt"):
            if m.group("afield"):                       # A12.total_weekly: apartment evidence
                return f"{apts.get(m.group('apt'), 'an apartment')}: {_label(m.group('afield'))}"
            # bare A16: a register rule when quoted as one ("under A16", "(A16)", "A16(a)") or when no such apartment is in the
            # text's apartment basis; the address when it is one of those apartments and not quoted as a rule. A bare A<id>
            # that is not a register code (A54) is an apartment: the address, or an explicit neutral phrase when the id is
            # outside the basis - never a rule label
            code = m.group("apt")
            is_rule_code = code in RULE_LABELS
            before = text[:m.start()]
            if m.group("sub") or (is_rule_code and (code not in apts or (_RULE_CONTEXT.search(before) and not _WEAK_CUE.search(before)))):
                return rule(code)
            if code in apts:
                return apts[code]
            return rule(code) if is_rule_code else APT_UNKNOWN
        if m.group("who"):
            ref = m.group("who")
            if ref not in who.names and ref.startswith("I") and (m.group("sub") or ref in RULE_LABELS):
                return rule(ref)                        # I16(d): a register rule, not an introduction ref
            person = who(ref)
            return f"{person}'s {_label(m.group('field'))}" if m.group("field") else person
        return rule(m.group("code"))
    return _REF.sub(repl, str(text))


_DUP_ORDINAL = ("first listed", "second listed", "third listed", "fourth listed", "fifth listed", "sixth listed", "seventh listed", "eighth listed")
FORMER_MEMBER = "a former member"      # proven to have left: retired identity, deleted row
EARLIER_MEMBER = "an earlier member"   # cannot be attributed (same words as the EarlierMembers map label)
_NEUTRAL_LABELS = (FORMER_MEMBER, EARLIER_MEMBER)


def distinct_names(c, names: dict[str, str], idents: dict) -> dict[str, str]:
    """G23 (9 Nov 2026): two refs that resolve to the SAME display name ("Mateo and Mateo") must stay tellable apart in staff prose
    without showing U-codes. Where the colliding people are leads with recorded full names that differ, the full recorded names
    are used; otherwise (identical full names, a party member's single recorded name, or two retired people) each gets the
    same words plus a neutral ordinal in the order of the saved member map ("Mateo (first listed)", "another former member").
    Display only: packets, selection and the saved maps are untouched; refs with a unique name are returned exactly as before."""
    by_name: dict[str, list[str]] = {}
    for ref, nm in names.items():
        by_name.setdefault(nm, []).append(ref)
    out = dict(names)
    for nm, refs in by_name.items():
        if len(refs) < 2:
            continue
        if nm in _NEUTRAL_LABELS:   # neutral words are never widened from today's rows: "a former member" / "another former member"
            for i, ref in enumerate(refs):
                out[ref] = nm if i == 0 else (f"another {nm.split(' ', 1)[1]}" if i == 1 else f"a further {nm.split(' ', 1)[1]} ({_DUP_ORDINAL[i] if i < len(_DUP_ORDINAL) else 'later listed'})")
            continue
        full: dict[str, str] = {}
        for ref in refs:
            slot = (idents or {}).get(ref) or {}   # the RESOLVED slot the shown name came from (never a saved id that was not proven)
            if slot.get("party_member_id") is None and slot.get("enquiry_id") is not None:
                r = row(c.execute("SELECT first_name, last_name FROM enquiries WHERE id=?", (slot["enquiry_id"],)))
                if r and (r.get("first_name") or "").strip() == nm and (r.get("last_name") or "").strip():
                    full[ref] = f"{r['first_name'].strip()} {r['last_name'].strip()}"
            elif slot.get("party_member_id") is not None:
                full[ref] = nm   # a party member has one recorded name; it is already what is shown
        if len(full) == len(refs) and len(set(full.values())) == len(refs):
            out.update(full)
            continue
        for i, ref in enumerate(refs):
            out[ref] = f"{nm} ({_DUP_ORDINAL[i] if i < len(_DUP_ORDINAL) else 'later listed'})"
    return out


_PROVEN_GONE = ("left", "barred", "enquiry_deleted")   # retired reasons that PROVE the person is no longer on the row


def _identity_changed_at(c, enquiry_id, party_member_id=None) -> str | None:
    """The latest moment ANY identity on this slot was retired - the lead slot of an enquiry row (GP9(e) takeover: the row kept its
    id and now names a different human) or one party_members row (identity reset). identities.reconcile's 'conservative reset'
    (name/email changed -> retire + new uid) is included: succession on a slot is not proof of the same human, so an identity
    edit is a change event like any other. None when the slot never changed (or has no identity history)."""
    if enquiry_id is None and party_member_id is None:
        return None
    if party_member_id is not None:
        r = row(c.execute("SELECT MAX(retired_at) AS t FROM people_identities WHERE party_member_id=? AND retired_at IS NOT NULL", (party_member_id,)))
    else:
        r = row(c.execute("SELECT MAX(retired_at) AS t FROM people_identities WHERE enquiry_id=? AND role='lead' AND retired_at IS NOT NULL", (enquiry_id,)))
    return (r or {}).get("t") or None


def _whole_second_utc(value) -> datetime | None:
    """A stored timestamp as an aware datetime at whole-second precision in UTC; None when missing or not a valid ISO-8601 value
    (fail neutral: a malformed stamp proves nothing). A naive value is taken as UTC (db.now_iso writes UTC with an offset)."""
    if value is None or not isinstance(value, str) or not value.strip():
        return None
    try:
        d = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).replace(microsecond=0)


def _written_after(written_at, event_at) -> bool | None:
    """True when the text was written strictly after the event, False when before, None when unknowable: a missing or malformed
    timestamp, or the same whole second (db.now_iso has second resolution, so a row stamped in the event's second is not proven
    either side). Compared as validated datetimes in UTC, never as strings."""
    w, e = _whole_second_utc(written_at), _whole_second_utc(event_at)
    if w is None or e is None or w == e:
        return None
    return w > e


def _live_identity(c, person_uid: str) -> dict | None:
    """The identity row the saved uid stands for TODAY, only when that very uid is still active. A retired uid is never followed
    along superseded_by_uid: that records succession on a slot, not proof of the same human. Unknown -> None."""
    i = row(c.execute("SELECT * FROM people_identities WHERE person_uid=?", (person_uid,)))
    if not i or i.get("retired_at"):
        return None
    return i


def _retired_label(c, person_uid: str) -> str:
    """Words for a uid that no longer resolves: proven gone (left / barred / enquiry_deleted) -> "a former member"; an identity
    edit (the slot's details changed - same human or a replacement, unprovable) or an unknown uid -> neutral."""
    i = row(c.execute("SELECT retired_reason FROM people_identities WHERE person_uid=?", (person_uid,)))
    if i and i.get("retired_reason") in _PROVEN_GONE:
        return FORMER_MEMBER
    return EARLIER_MEMBER


def _slot_name(c, enquiry_id, party_member_id) -> str | None:
    if party_member_id is not None:
        r = row(c.execute("SELECT name AS n FROM party_members WHERE id=? AND enquiry_id=?", (party_member_id, enquiry_id)))
    else:
        r = row(c.execute("SELECT first_name AS n FROM enquiries WHERE id=?", (enquiry_id,)))
    return (r or {}).get("n") or None


def _resolve_ident(c, ident: dict, written_at) -> tuple[str, dict | None]:
    """One saved ref -> (words, resolved slot or None). The slot {enquiry_id, party_member_id} is returned ONLY when the words are a
    real current name proven to be the person the text describes; it is what any later widening (duplicate first names) may read.
    Neutral words carry no slot.
    - saved person_uid (writer-side addition): authoritative. Still active -> that identity's current slot and name wherever the
      person now sits (a promoted companion is named as today's lead); retired or unknown -> _retired_label. No timestamp needed;
      the saved row ids are never consulted.
    - no uid (legacy maps): the saved slot is used only when proven unchanged since the text was written: no identity change on the
      slot -> current name; a change (takeover, reset) strictly before the text -> current name; strictly after -> "a former member"
      (lead slot) / neutral (party slot: the row still exists with a changed identity); unknowable order (no/malformed timestamp,
      same second) -> neutral. A party row that is gone -> "a former member", or neutral when its enquiry's lead slot changed (the
      missing row may be the promoted person). Today's row is never read for text that may describe someone else."""
    uid = ident.get("person_uid")
    if uid:
        live = _live_identity(c, str(uid))
        if live is None:
            # an unknown uid whose saved enquiry row no longer exists is proven gone (identities cascade with the enquiry; ids are never reused)
            if ident.get("enquiry_id") is not None and not row(c.execute("SELECT id FROM people_identities WHERE person_uid=?", (str(uid),))) \
                    and not row(c.execute("SELECT id FROM enquiries WHERE id=?", (ident["enquiry_id"],))):
                return FORMER_MEMBER, None
            return _retired_label(c, str(uid)), None
        slot = {"enquiry_id": live["enquiry_id"], "party_member_id": live.get("party_member_id")}
        name = _slot_name(c, slot["enquiry_id"], slot["party_member_id"])
        return (name, slot) if name else (FORMER_MEMBER, None)
    eid, pmid = ident.get("enquiry_id"), ident.get("party_member_id")
    if eid is None and pmid is None:
        return FORMER_MEMBER, None
    name = _slot_name(c, eid, pmid)
    if not name:
        if pmid is not None and _identity_changed_at(c, eid):
            return EARLIER_MEMBER, None
        return FORMER_MEMBER, None
    changed = _identity_changed_at(c, eid, pmid)
    if changed:
        after = _written_after(written_at, changed)
        if after is None:
            return EARLIER_MEMBER, None
        if not after:
            return (FORMER_MEMBER if pmid is None else EARLIER_MEMBER), None
    return name, {"enquiry_id": eid, "party_member_id": pmid}


def _name_for_ident(c, ident: dict, written_at) -> str:
    return _resolve_ident(c, ident, written_at)[0]


def names_from_refs(c, refs_json, written_at=None) -> dict[str, str] | None:
    """ref -> name from the IMMUTABLE map the writer stored with the row (refs_json: {ref: {enquiry_id, party_member_id[, person_uid]}}),
    looked up now through people_identities (see _resolve_ident): a person since deleted shows as "a former member"; a slot that
    changed identity after the text was written (GP9(e) takeover, identity reset) never lends today's name to it. `written_at`: the
    row's created_at, used only to order a legacy map (no person_uid) against such a change. Duplicate display names are widened
    through the RESOLVED slots only (never the saved ids). None when the row has no map (legacy)."""
    refs = pj(refs_json, None)
    if not isinstance(refs, dict) or not refs:
        return None
    out, slots = {}, {}
    for ref, ident in refs.items():
        out[ref], slot = _resolve_ident(c, ident if isinstance(ident, dict) else {}, written_at)
        if slot is not None:
            slots[ref] = slot
    return distinct_names(c, out, slots)


def ref_names(c, gid: int, evidence_hash: str | None = None, refs_json=None, written_at=None) -> dict[str, str]:
    """ref -> the person's first name (lead) or recorded name (party member). Preference: the row's own immutable map
    (refs_json, written before the AI call); failing that, ai_queue's identity mapping of the CURRENT members, which is only
    valid for text written about the current member set - so with an evidence hash that differs from the group's present hash
    (members replaced, facts changed) names are withheld rather than guessed."""
    from . import ai_queue
    fixed = names_from_refs(c, refs_json, written_at)
    if fixed is not None:
        return fixed
    g = row(c.execute("SELECT * FROM groups WHERE id=?", (gid,)))
    if not g:
        return {}
    if evidence_hash is not None and (not g.get("evidence_hash") or evidence_hash != g.get("evidence_hash")):
        return EarlierMembers()
    try:
        idents = ai_queue._ref_identities(c, g)
    except Exception:  # pragma: no cover - a group with no resolvable members
        return {}
    # GP9(e): the evidence hash does not change when an enquiry row changes person, so an equal hash is not proof that today's
    # members are the people the text describes. Any member row with a takeover not proven earlier than the text -> withheld.
    for ident in idents.values():
        changed = _identity_changed_at(c, ident.get("enquiry_id"), ident.get("party_member_id"))
        if changed and _written_after(written_at, changed) is not True:
            return EarlierMembers()
    out = {}
    for ref, ident in idents.items():
        if ident.get("party_member_id"):
            r = row(c.execute("SELECT name FROM party_members WHERE id=?", (ident["party_member_id"],)))
            out[ref] = (r or {}).get("name") or ref
        else:
            r = row(c.execute("SELECT first_name FROM enquiries WHERE id=?", (ident["enquiry_id"],)))
            out[ref] = (r or {}).get("first_name") or ref
    return distinct_names(c, out, idents)


def apartment_names(c, gid: int, basis_ids=None) -> dict[str, str]:
    """A<id> -> address for the group's present apartments plus, for text written earlier, the apartments of its SAVED basis
    (basis_ids: the apartment ids the review round ranked). An apartment since removed from the group keeps its own identity
    when it still exists; one deleted outright is left out, so plain_text falls back to an explicit neutral phrase."""
    out = {}
    for a in rows(c.execute("SELECT ga.apartment_id, a.address FROM group_apartments ga JOIN apartments a ON a.id=ga.apartment_id WHERE ga.group_id=?", (gid,))):
        out[f"A{a['apartment_id']}"] = a["address"]
    g = row(c.execute("SELECT g.apartment_id, a.address FROM groups g LEFT JOIN apartments a ON a.id=g.apartment_id WHERE g.id=?", (gid,)))
    if g and g.get("apartment_id") is not None and g.get("address"):   # a group held with no current offer names no apartment here
        out.setdefault(f"A{g['apartment_id']}", g["address"])
    for aid in basis_ids or ():
        key = f"A{aid}"
        if key not in out:
            r = row(c.execute("SELECT address FROM apartments WHERE id=?", (aid,)))
            if r and r.get("address"):
                out[key] = r["address"]
    return out


def basis_apartment_ids(review_json) -> list[int]:
    """The apartment ids a saved review round ranked (review_json["apartments"]), in its order; [] for a legacy row."""
    rj = pj(review_json, {}) if not isinstance(review_json, dict) else review_json
    out = []
    for a in (rj or {}).get("apartments") or []:
        if isinstance(a, dict) and isinstance(a.get("apartment_id"), int):
            out.append(a["apartment_id"])
    return out


def _source_round(matches: list[dict], g: dict) -> dict | None:
    """Which of several valid rounds with the SAME saved words is the source of the text stored on the group? apply_review records
    a round (valid=1) BEFORE it checks the evidence is still current, so a later valid row may have been discarded with nothing
    applied. Persisted markers: groups.ai_assessed_at is the applied round's created_at (same write), so any match created
    AFTER it was never applied and is dropped, and a match created AT it is preferred. Among what remains, one shared refs map is
    used; disagreeing maps with no marker mean None (neutral words) - the same words are never attributed by guessing."""
    if not matches:
        return None
    mark = g.get("ai_assessed_at")
    if mark:
        matches = [r for r in matches if not r.get("created_at") or r["created_at"] <= mark]
        exact = [r for r in matches if r.get("created_at") == mark]
        if exact:
            matches = exact          # timestamps are whole seconds (db.now_iso), so a discarded round can share the marker: the tie is still judged on refs
    if not matches:
        return None
    if len({json.dumps(pj(r.get("refs_json"), None), sort_keys=True) for r in matches}) > 1:
        return None                  # same words, same second, different people: do not guess
    return matches[0]


def _reason_basis_round(c, g: dict, reasons: dict[int, str]) -> dict | None:
    """The review round whose saved apartment reasons are exactly the stored group_apartments.ai_reason texts and which is the
    persisted source of them (see _source_round). A valid-but-discarded later round with the same texts and a different refs
    map is never used; disagreeing candidates with no marker mean None (neutral words)."""
    matches = []
    for r in rows(c.execute("SELECT id, refs_json, evidence_hash, review_json, rationale, created_at FROM review_rounds WHERE group_id=? AND valid=1 ORDER BY id DESC", (g["id"],))):
        rj = pj(r.get("review_json"), {}) or {}
        saved = {a.get("apartment_id"): (a.get("reason") or "").strip() for a in rj.get("apartments") or [] if isinstance(a, dict)}
        if all(saved.get(aid) == (txt or "").strip() for aid, txt in reasons.items()):
            r["basis_ids"] = basis_apartment_ids(rj)
            matches.append(r)
    return _source_round(matches, g)


def apartment_reasons(c, g: dict, apts: list[dict]) -> None:
    """group_apartments.ai_reason for the staff card, in plain words, in place. The reasons are attributed through the
    review round that wrote them AND was applied (its immutable refs map, its apartment basis); with no such round, names
    are withheld (neutral words) and only the group's present apartments are named - today's identities are never assigned
    to text that may describe other people (stale membership, a retired ref, a discarded later round with the same words)."""
    reasons = {a["apartment_id"]: a["ai_reason"] for a in apts if a.get("ai_reason")}
    if not reasons:
        return
    r = _reason_basis_round(c, g, reasons)
    if r and r.get("refs_json"):
        names = names_from_refs(c, r["refs_json"], r.get("created_at"))
    elif r and r.get("evidence_hash") and r.get("evidence_hash") == g.get("evidence_hash"):
        names = ref_names(c, g["id"], r.get("evidence_hash"), None, r.get("created_at"))
    else:
        names = EarlierMembers()
    names = names if names is not None else EarlierMembers()
    amap = apartment_names(c, g["id"], (r or {}).get("basis_ids"))
    namer = Namer(names)
    for a in apts:
        if a.get("ai_reason"):
            a["ai_reason"] = plain_text(a["ai_reason"], names, amap, namer)


def describe_fact(fid: str, value, names: dict, apts: dict, missing: bool = False, source: str | None = None) -> str:
    """'U1.routine' + 'Early riser' -> "Ana's daily routine: Early riser"; 'R.A12.budget' -> "Budget check at 12 Smith St: pass"."""
    parts = fid.split(".")
    if parts[0] == "R" and len(parts) >= 5 and _PERSON_REF.match(parts[4]):
        # subject-attributed rule evidence (R.A12.budget.share.U1-m1): the person's own words, the lead's report named separately
        who = names.get(parts[4]) or Namer(names)(parts[4])
        txt = _subject_text(parts[2], parts[3], who, apts.get(parts[1], "the apartment"))
        txt = txt[0].upper() + txt[1:]
        if missing:
            return txt + " (not recorded)"
        s = f"{txt}: {value}"
        if source and source != parts[4]:
            s += _REPORTED_BY.format(who=names.get(source) or Namer(names)(source))
        return s
    if parts[0] == "R" and len(parts) >= 3:
        where = apts.get(parts[1], "the apartment")
        txt = f"{_check_label(parts[2], parts[3] if len(parts) > 3 else None).capitalize()} at {where}"
    elif parts[0] in apts:
        txt = f"{apts[parts[0]]}: {_label(parts[1])}"
    else:
        who = names.get(parts[0], parts[0])
        txt = f"{who}'s {_label(parts[1])}" if len(parts) > 1 else who
    if missing:
        return txt + " (not recorded)"
    s = f"{txt}: {value}"
    if source and source != parts[0]:
        s += f" (answered by {names.get(source, source)} for the whole party)"
    return s


_BRACKET = re.compile(r"  \[(?P<body>.*?)\](?=  \[|$)", re.S)
_GAP = "[gap - not recorded] "


def _fact_line(fid: str, names: dict, apts: dict, prefix: str = "") -> str:
    parts = str(fid).split(".")
    if parts[0] == "R" and len(parts) >= 5 and _PERSON_REF.match(parts[4]):
        who = names.get(parts[4]) or Namer(names)(parts[4])
        s = _subject_text(parts[2], parts[3], who, apts.get(parts[1], "the apartment"))
        s = s[0].upper() + s[1:]
    elif parts[0] == "R" and len(parts) >= 3:
        s = f"{_label(parts[2]).capitalize()} at {apts.get(parts[1], 'the apartment')}"
    elif parts[0] in apts:
        s = f"{apts[parts[0]]}: {_label(parts[-1])}"
    else:
        s = f"{names.get(parts[0], parts[0])}'s {_label(parts[-1])}" if len(parts) > 1 else names.get(parts[0], parts[0])
    return prefix + s


def friendly_item(item, names: dict, apts: dict) -> dict:
    """One brief note -> {note, evidence: [plain strings]}. Accepts the structured item or the writer's rendered string
    ("note  [own facts]  [compared with U2: counterpart facts]"). Counterpart facts (r5 compared_with) are listed as
    "Compared with <housemate>'s <field>: <value>"; the note itself is put into plain words (no U1 / fact ids)."""
    namer = Namer(names)
    look = {ref: namer(ref) for ref in (names or {})} if not isinstance(names, EarlierMembers) else {}

    class _Look(dict):
        def get(self, key, default=None):   # unknown ref -> the same neutral word plain_text would use
            return look[key] if key in look else namer(key)
    look = _Look(look)
    if isinstance(item, str):
        text = item
        brackets = [b.group("body") for b in _BRACKET.finditer(text)]
        first = _BRACKET.search(text)
        note = (text[:first.start()] if first else text).strip()
        if note.startswith(_GAP):
            note = "Not recorded: " + note[len(_GAP):]
        ev = []
        for body in brackets:
            cmp = re.match(r"^compared with (?P<ref>[UI]\d+(?:-m\d+)?): ?(?P<bits>.*)$", body, re.S)
            prefix = "Compared with " if cmp else ""
            for bit in (cmp.group("bits") if cmp else body).split("; "):
                b = _BIT.match(bit.strip())
                if b:
                    line = describe_fact(b.group("id"), b.group("value"), look, apts, missing=bool(b.group("missing")), source=b.group("src"))
                    ev.append(prefix + line)
                elif bit.strip():
                    ev.append(prefix + plain_text(bit.strip(), names, apts, namer))
        return {"note": plain_text(note, names, apts, namer), "evidence": ev}
    note = str((item or {}).get("note") or "").strip()
    if (item or {}).get("kind") == "gap" and note:
        note = "Not recorded: " + note
    ev = [_fact_line(fid, look, apts) for fid in (item or {}).get("fact_ids") or []]
    for e in (item or {}).get("compared_with") or []:
        for fid in (e or {}).get("fact_ids") or []:
            line = _fact_line(fid, look, apts)
            ev.append("Compared with " + line)
    return {"note": plain_text(note, names, apts, namer), "evidence": ev}


SECTIONS = (("strengths", "What is likely to work"), ("what_could_work", "What could work well"), ("tensions", "Possible tensions"),
            ("what_could_cause_issues", "What could cause issues"), ("discussion_points", "Worth discussing with the group"), ("missing_facts", "Not recorded (ask, do not assume)"))


def _sections(body: dict, names: dict, apts: dict) -> list[dict]:
    rendered = body.get("rendered") or {}
    out = []
    for key, title in SECTIONS:
        items = rendered.get(key) if rendered.get(key) else body.get(key)
        if not items:
            continue
        out.append({"key": key, "title": title, "items": [friendly_item(x, names, apts) for x in items]})
    return out


def _currency(c, g: dict) -> dict:
    """Evidence currency of the group's stored assessment at read time (ai_queue.evidence_currency); read-only, never a provider call."""
    from . import ai_queue
    try:
        return ai_queue.evidence_currency(c, g)
    except Exception as ex:  # noqa: BLE001 - a currency check that cannot be made never labels old text current
        log.warning("group %s: evidence currency could not be computed (%s)", g.get("id"), type(ex).__name__)
        return {"current": False, "reasons": ["currency could not be checked"], "carveout": False}


def brief_view(c, gid: int, g: dict) -> dict | None:
    """The latest group brief with its member briefs, labelled current or historical. None when none was ever written."""
    b = row(c.execute("SELECT * FROM group_briefs WHERE group_id=? AND kind='group' ORDER BY id DESC LIMIT 1", (gid,)))
    if not b:
        return None
    rr = row(c.execute("SELECT review_json FROM review_rounds WHERE group_id=? AND valid=1 AND evidence_hash=? ORDER BY id DESC LIMIT 1", (gid, b.get("evidence_hash") or "")))
    names, apts = ref_names(c, gid, b.get("evidence_hash") or "", b.get("refs_json"), b.get("created_at")), apartment_names(c, gid, basis_apartment_ids(rr.get("review_json")) if rr else None)
    namer = Namer(names)
    # succ3 (N4): current = written on the group's present evidence AND that evidence is still current at read time (review contract,
    # rule version, model, members' validated notes, recomputed compatibility evidence) - a stored hash match alone is not currency
    cur = _currency(c, g)
    current = b["status"] == "ok" and g.get("brief_status") == "ok" and bool(g.get("evidence_hash")) and b.get("evidence_hash") == g.get("evidence_hash") and cur["current"]
    gbody = pj(b.get("body_json"), {}) or {}
    out = {"status": b["status"], "written_at": b["created_at"], "round": b["round"], "current": current, "error": BRIEF_FAILED if b.get("error") else None,
           "currency_reasons": cur["reasons"],
           "label": "Current staff brief" if current else ("Staff brief failed" if b["status"] == "failed" else "Earlier staff brief: the facts have changed since it was written, so it is history, not the current assessment"),
           "names_available": not isinstance(names, EarlierMembers),
           "coverage": _coverage_line(gbody, names, apts, namer),   # r6: the row's OWN stored block, never recomputed from today's rows
           "sections": _sections(gbody, names, apts), "members": []}
    for m in rows(c.execute("SELECT * FROM group_briefs WHERE group_id=? AND kind='member' AND round=? AND created_at=? ORDER BY id", (gid, b["round"], b["created_at"]))):
        body = pj(m.get("body_json"), {}) or {}
        out["members"].append({"ref": m["person_ref"], "name": names.get(m["person_ref"], m["person_ref"]), "enquiry_id": m.get("enquiry_id"), "party_member_id": m.get("party_member_id"),
                               "individually_recorded": body.get("individually_recorded", True), "coverage": _coverage_line(body, names, apts, namer),
                               "sections": _sections(body, names, apts)})
    return out


def _coverage_line(body: dict, names: dict, apts: dict, namer) -> str | None:
    """The r6 code-derived coverage line stored WITH the row (body_json["coverage"]["line"] / review_json["coverage"]["line"]),
    rendered through the same immutable name map as the rest of that row. Rows written before r6 have no block: None, never a
    line recomputed from whoever is in the group today."""
    cov = (body or {}).get("coverage") if isinstance(body, dict) else None
    if not isinstance(cov, dict) or not cov.get("line"):
        return None
    return plain_text(str(cov["line"]), names, apts, namer) or None


REVIEW_SET_ASIDE = "AI review set aside: the AI reply could not be verified, so the group stays held for staff; retry the assessment or ask a manager"
BRIEF_FAILED = "The staff brief could not be verified, so the group stays held for staff; retry the assessment or ask a manager"
AI_FAILED = "The AI assessment did not complete{attempts}; the group is not ready for reservations - retry the assessment or ask a manager"


def review_view(c, gid: int, g: dict) -> dict | None:
    """The latest review round in plain words; current only when it is valid and matches the group's present evidence.
    The rationale is rendered with that round's OWN immutable name map (never today's members for an older round); a reply
    the validator set aside gets a plain label; its schema message stays on the database row only (nothing is logged)."""
    r = row(c.execute("SELECT * FROM review_rounds WHERE group_id=? ORDER BY id DESC LIMIT 1", (gid,)))
    if not r:
        return None
    cur = _currency(c, g)   # succ3 (N4): read-time currency, not the stored hash alone
    current = bool(r.get("valid")) and r.get("verdict") == "approve" and bool(g.get("evidence_hash")) and r.get("evidence_hash") == g.get("evidence_hash") and cur["current"]
    verdict = {"approve": "approved", "revise": "asked for a revision", "hold": "held for staff", "reject": "not approved"}.get(r.get("verdict"), r.get("verdict") or "no verdict")
    names, apts = ref_names(c, gid, r.get("evidence_hash") or "", r.get("refs_json"), r.get("created_at")), apartment_names(c, gid, basis_apartment_ids(r.get("review_json")))
    namer = Namer(names)
    replace = [namer(x) for x in (pj(r.get("replace_refs_json"), []) or []) if isinstance(x, str)]
    coverage = _coverage_line(pj(r.get("review_json"), {}) or {}, names, apts, namer)   # r6 code-derived line from the row's own block
    return {"verdict": r.get("verdict"), "verdict_text": verdict, "rating": r.get("rating"), "rationale": plain_text(r.get("rationale"), names, apts, namer) or None,
            "coverage": coverage, "replace_names": replace, "round": r.get("round"), "reviewed_at": r["created_at"],
            "valid": bool(r.get("valid")), "names_available": not isinstance(names, EarlierMembers), "current": current, "currency_reasons": cur["reasons"],
            "label": f"Current AI review ({verdict})" if current else ("Earlier AI review: the facts, rules or model have changed since, so it is history, not the current assessment" if r.get("valid") else REVIEW_SET_ASIDE)}


def rationale_for_group(c, g: dict) -> str | None:
    """groups.ai_rationale for the staff card, in plain words. The text is attributed through the review round it was copied
    from (same rationale, same evidence); when no such round exists the refs become neutral words - today's names are never
    assigned to text that may describe other people."""
    text = g.get("ai_rationale")
    if not text:
        return text
    r = _source_round(rows(c.execute("SELECT refs_json, evidence_hash, review_json, rationale, created_at FROM review_rounds WHERE group_id=? AND rationale=? AND valid=1 ORDER BY id DESC", (g["id"], text))), g)
    apts = apartment_names(c, g["id"], basis_apartment_ids(r.get("review_json")) if r else None)
    if r and r.get("refs_json"):
        return plain_text(text, names_from_refs(c, r["refs_json"], r.get("created_at")), apts)
    if r and r.get("evidence_hash") and r.get("evidence_hash") == g.get("evidence_hash"):
        return plain_text(text, ref_names(c, g["id"], r.get("evidence_hash"), None, r.get("created_at")), apts)
    return plain_text(text, EarlierMembers(), apts)


HOLD_PLAIN = (
    ("A member's notes could not be read after repeated attempts", "notes_unread",
     "The AI could not read one member's notes after the automatic retries, so the group has not been assessed; retry the AI assessment, or read the notes yourself and decide"),   # C4 (ai_queue.NOTES_HELD_LABEL)
    ("AI review: hold.", "review_hold", "The AI review held this group for staff: {rest}"),
    ("Staff brief failed:", "brief_failed", BRIEF_FAILED),
    ("Staff brief:", "brief_failed", BRIEF_FAILED),
    ("Brief discarded:", "stale_data", "The staff brief was discarded because a person it described was deleted or the membership changed; the group is queued for a fresh assessment"),
    ("Facts changed while", "stale_data", "The facts changed while the staff brief was being written; the group is queued for a fresh assessment"),
    ("Earlier assessment superseded", "review_pending", "An earlier assessment was superseded; the group is waiting for a review under the current rules"),
    ("Earlier approval superseded", "review_pending", "An earlier approval was superseded because the rules, review contract or model changed; the group is waiting for a fresh review and brief"),
)


def hold_view(c, g: dict) -> dict | None:
    """groups.held_reason for staff: {category, label} in plain, actionable words. Raw text stays on the row; refs in a review
    hold / revision are named through the round written against the group's present evidence, else neutrally."""
    raw = (g.get("held_reason") or "").strip()
    if not raw:
        if g.get("brief_status") == "pending" and g.get("ai_status") == "ok":
            return {"category": "brief_pending", "label": "The AI has approved the group; the staff brief is being written before it can be ready"}
        return None
    if raw.startswith("AI review: revise "):
        head, _, rest = raw.partition(". ")
        r = row(c.execute("SELECT refs_json, evidence_hash, created_at FROM review_rounds WHERE group_id=? AND verdict='revise' ORDER BY id DESC LIMIT 1", (g["id"],)))
        names = ref_names(c, g["id"], (r or {}).get("evidence_hash") or "", (r or {}).get("refs_json"), (r or {}).get("created_at")) if r and r.get("evidence_hash") == g.get("evidence_hash") else EarlierMembers()
        namer = Namer(names)
        who = [namer(x.strip()) for x in head[len("AI review: revise "):].split(",") if x.strip()]
        rest = plain_text(rest, names, apartment_names(c, g["id"]), namer)
        return {"category": "revision_pending", "label": f"The AI review asked for a revision - replace {', '.join(who) or 'a member'}" + (f": {rest}" if rest else "")}
    for prefix, cat, label in HOLD_PLAIN:
        if raw.startswith(prefix):
            if "{rest}" in label:
                r = row(c.execute("SELECT refs_json, evidence_hash, created_at FROM review_rounds WHERE group_id=? AND verdict='hold' ORDER BY id DESC LIMIT 1", (g["id"],)))
                names = ref_names(c, g["id"], (r or {}).get("evidence_hash") or "", (r or {}).get("refs_json"), (r or {}).get("created_at")) if r and r.get("evidence_hash") == g.get("evidence_hash") else EarlierMembers()
                label = label.format(rest=plain_text(raw[len(prefix):].strip(), names, apartment_names(c, g["id"])))
            return {"category": cat, "label": label}
    if "deleted (retention)" in raw:
        return {"category": "stale_data", "label": plain_text(raw, EarlierMembers(), apartment_names(c, g["id"]))}
    return {"category": "held", "label": "The group is held for staff; see the readiness reasons above or ask a manager"}


def ai_error_label(g: dict) -> str | None:
    """groups.ai_error for staff: a generic, honest line; the technical message stays on the database row only."""
    if not g.get("ai_error"):
        return None
    n = int(g.get("ai_attempts") or 0)
    return AI_FAILED.format(attempts=f" after {n} attempt{'s' if n != 1 else ''}" if n else "")


def intro_history(c, gid: int, limit: int = 8) -> list[dict]:
    """Introduction drafts and stale markers, newest first, each labelled; text is kept as history, never as 'current'."""
    out = []
    for r in rows(c.execute("SELECT * FROM group_briefs WHERE group_id=? AND kind='intro' ORDER BY id DESC LIMIT ?", (gid, limit))):
        body = pj(r.get("body_json"), {}) or {}
        out.append({"id": r["id"], "status": r["status"], "at": r["created_at"], "basis": body.get("basis"), "by": body.get("by"), "reason": body.get("reason"),
                    "text": body.get("text"), "evidence_hash": r.get("evidence_hash"),
                    "label": {"ok": "AI draft", "edited": "Staff-edited draft", "template": "Template (not an AI draft)", "stale": "Marked out of date"}.get(r["status"], r["status"])})
    return out
