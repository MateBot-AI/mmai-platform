"""T6 / T4(f) fact catalogue (8 November 2026).

Every claim the AI makes in a review (findings, verdict, replace_refs reasons) or a staff brief must point at fact ids from this
catalogue. The catalogue is built deterministically from the launch-neutral packet the model was given, so a cited id proves:
  * the fact exists in ALM's records for that person / apartment / rule check (not invented),
  * who it belongs to and who supplied it (provenance): a party member's own ref carries the PARTY-WIDE answers the enquirer gave
    for the whole party (I16(d): dates, party composition) with source = the enquirer's ref, and PERSONAL fields (study/work,
    routine, tidiness, social style, guests, diet) as explicitly MISSING - they are not recorded for that person and are never
    inherited from the lead,
  * that no excluded attribute can be cited: the catalogue contains no name, country, language, sex, age or free text, because the
    packet does not.

What validation does NOT prove: that the prose attached to a fact id is a true or sensible reading of it. The checks here are
structural (existence, ownership, scope, recorded-vs-missing); the staff reading the brief remain the judge of the words.

r6 (8 November 2026): `coverage` is a code-derived count of who in the packet answered the lifestyle / compatibility questions
themselves and who is a companion whose party-wide answers came from their enquirer. It goes into the packets and the results,
and `coverage_claim_problems` rejects model prose of the exact shape "<n> of <m> people ... no/not/without/lack ... lifestyle /
recorded / compatibility ..." when n/m disagree with it (a real seven-person run said "six of seven" for five companions). It
is a narrow consistency check on that one sentence shape, not a reading of what the model meant elsewhere.
"""
from __future__ import annotations

import re
from decimal import Decimal

PERSONAL_FIELDS = ("doing", "school", "course", "level", "routine", "tidy", "social", "guests", "diet")
PARTY_FIELDS = ("party", "party_size", "move_in", "move_out",      # I16(d): the enquirer answers these for the whole party
                "room_first", "rooms_accepted", "apt_sizes_accepted", "bunk_ok", "mate_max", "smoke", "smoke_ok")   # r7: raw preferences, same provenance
# r7: A16 household figures as citable rule facts, lossless strings from the SAME BudgetResult the check used (no re-arithmetic):
# R.A<id>.budget.<field>. Units are explicit; allowance_fraction is a raw fraction (0.10 = ten percent), never re-interpreted.
HOUSEHOLD_BUDGET_FACTS = {"total_weekly_rent_exact": "AUD per week", "whole_apartment_max_exact": "AUD per week",
                          "allowance_fraction": "fraction of the household maximum (0.10 = ten percent)", "limit_exact": "AUD per week"}
HOUSEHOLD_BUDGET_FIELDS = tuple(HOUSEHOLD_BUDGET_FACTS)   # staff-visible fields (staff_view.FACT_LABELS must label each)
APARTMENT_FIELDS = ("type_label", "suburb", "bills_included", "available_from", "lease_start", "lease_end", "stay_days", "rate_band", "total_weekly", "extra_bed")
# rate_band replaces the bare rate_kind code ('6m'): the packet carries an explicit A6 pricing-band description plus the exact stay_days,
# so a model cannot read the band as the lease length (a 1 Feb - 10 Jul stay is 159 days, not six months).
FEATURE_UNRECORDED = (None, "", "Unknown", "unknown", "unverified")   # apartment_features.compare says 'unverified' when staff have not recorded yes/no
FACT_IDS_MAX = 14      # response size limit per note (lets a seven-person group cite every member); over it the item is rejected
COMPARE_REFS_MAX = 6   # r5: a member note may compare its subject with up to six housemates (seven-person group) ...
COMPARE_IDS_MAX = 14   # ... citing at most 14 counterpart fact ids in total, separately from the subject's own 14
COMPARABLE_FIELDS = PERSONAL_FIELDS + PARTY_FIELDS   # counterpart evidence is a housemate's same-field answer; never an apartment or rule id
NOTE_WORDS_MAX = 30    # a note over this is rejected (the prompts ask for 25); root-approved engineering limit, not a rule about content
RULE_NAMES = ("dates", "budget", "beds", "smoking", "adults", "capacity", "housemates")
# Increment A (9 Nov 2026): subject-attributed budget evidence. R.A<id>.budget.share.<ref> is the A12 guide share from the deterministic
# bed allocation (source: the rules calculation); R.A<id>.budget.stated_max.<ref> is that person's I14 stated weekly maximum, a stated
# answer reported by the enquirer for the whole party (reported_by). Both carry "ref" so a member note may cite them as the person's own
# evidence, and scope "rule" so they never count as the person's own lifestyle / compatibility answers (coverage reads PERSONAL_FIELDS).
PER_PERSON_BUDGET_FACTS = {"share": ("weekly_share_text", "weekly_share", "AUD per week"), "stated_max": ("stated_weekly_max_exact", "stated_weekly_max", "AUD per week")}
# wording guards that read the CITED facts (context-aware, never a blanket word ban):
_AGREED_WORDS = re.compile(r"\b(agreed|accepted|signed off)\b", re.I)
_SHARE_NOUNS = re.compile(r"\b(shares?(?!\s+(?:a|an|the|with)\b)|contributions?|split|amounts?|figures?|rent|weekly|\d+(?:\.\d+)?)\b", re.I)
_NOT_YET = re.compile(r"\b(not|never|yet|still|to be|once|if|until|unless|before|pending|would be|will be|needs? to be|has to be|must be|should be|can be|could be|rather than|instead of|without)\s+(?:\w+\s+){0,2}$", re.I)
_PREFERENCE_WORDS = r"(?:first choice|first preference|prefers?|preferred|would prefer|preference for|asked for|requested|wants?|wanted|chose|chosen)"
_ROOM_NOUNS = r"(?:rooms?|bedrooms?|apartments?|flat|beds?|bunks?|share|sharing|housemates?|twin|single|double|king|size|sizes|own|alone|couple)"
_FIRST_CHOICE_FWD = re.compile(r"\b" + _PREFERENCE_WORDS + r"\b(?:\s+\S+){0,5}?\s+\S*\b" + _ROOM_NOUNS + r"\b", re.I)
_FIRST_CHOICE_BWD = re.compile(r"\b" + _ROOM_NOUNS + r"\b[^.;,]{0,30}?\b(?:is|was|as|being|remains?)\s+(?:\S+\s+){0,2}?(?:first choice|first preference|preferred|preference)\b", re.I)


def agreed_share_wording(note: str) -> str | None:
    """The clause (split at . ; ,) that calls a share agreed / accepted / signed off: it must name the share (share, contribution,
    split, amount, figure, rent, a number) in the SAME clause, and the agreed-word must not be negated or conditional ('not agreed',
    'yet to be agreed', 'once agreed'). 'Bo agreed to an early routine; the share is proposed' is not about the share and passes."""
    for clause in re.split(r"[.;,]", note or ""):
        for m in _AGREED_WORDS.finditer(clause):
            if not _SHARE_NOUNS.search(clause):
                break
            if _NOT_YET.search(clause[:m.start()]):
                continue
            return clause.strip()
    return None


def first_choice_wording(note: str) -> str | None:
    """A preference / request word tied to a ROOM-OR-APARTMENT noun in the same breath ('first choice is to share with one other',
    'prefers an own room', 'asked for a 3-bedroom'); 'accepts a twin room and prefers early nights' is about the evenings and passes,
    and a negated or hedged form in the same clause ('not a recorded first choice', 'rather than a first choice') is honest and passes."""
    for clause in re.split(r"[.;,]", note or ""):
        m = _FIRST_CHOICE_FWD.search(clause) or _FIRST_CHOICE_BWD.search(clause)
        if not m:
            continue
        pref = re.search(r"\b" + _PREFERENCE_WORDS + r"\b", clause, re.I)
        if pref and _NOT_YET.search(clause[:pref.start()]):
            continue
        return m.group(0).strip()
    return None
FACT_ID = re.compile(r"^[A-Za-z0-9_\-]+(\.[A-Za-z0-9_\-]+)+$")


def _recorded(v) -> bool:
    return v not in (None, "", [], {})


def feature_fact_key(label) -> str:
    """The single normalisation of a feature LABEL into its fact-id suffix (A<id>.feature.<suffix>). Used by the catalogue and by
    every consumer that maps a cited feature fact back to the apartment_features catalogue key."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(label or "")).strip("_").lower()


def catalogue(payload: dict) -> dict[str, dict]:
    """fact_id -> {ref|apartment|rule, field, value, scope, source, status}. `payload` is the queue packet (members_for_ai,
    person_refs, apartments, optional rule_checks). Deterministic; no database access."""
    facts: dict[str, dict] = {}
    for m in payload.get("members_for_ai") or []:
        ref = m["ref"]
        for f in PERSONAL_FIELDS:
            v = m.get(f)
            facts[f"{ref}.{f}"] = {"ref": ref, "field": f, "value": v if _recorded(v) else None, "scope": "personal", "source": ref,
                                   "status": "recorded" if _recorded(v) else "missing"}
        for f in PARTY_FIELDS:
            v = m.get(f)
            facts[f"{ref}.{f}"] = {"ref": ref, "field": f, "value": v if _recorded(v) else None, "scope": "party-wide", "source": ref,
                                   "status": "recorded" if _recorded(v) else "missing"}
        if m.get("couples"):
            facts[f"{ref}.couples"] = {"ref": ref, "field": "couples", "value": m["couples"], "scope": "party-wide", "source": ref, "status": "recorded"}
        # GAP-T4c v2: validated practical facts from the person's own notes (neutral projection only). Personal scope, self-reported
        # and unverified; a note never becomes a rule. Ids are stable: U1.notes.<category> (a second item of the same category gets .2 ...).
        used: dict[str, int] = {}
        for it in m.get("notes_facts") or []:
            if not isinstance(it, dict) or not it.get("category"):
                continue
            n = used.get(it["category"], 0) + 1
            used[it["category"]] = n
            fid = f"{ref}.notes.{it['category']}" + (f".{n}" if n > 1 else "")
            facts[fid] = {"ref": ref, "field": f"notes:{it['category']}", "value": {"strength": it.get("strength"), "value": it.get("value")},
                          "scope": "personal", "source": f"{ref}; {it.get('basis') or 'self-report (free text), unverified'}", "status": "recorded"}
        for pm in m.get("party_members") or []:
            for f in PARTY_FIELDS:   # reported by the enquirer for the party; usable, with that provenance
                v = m.get(f)
                facts[f"{pm}.{f}"] = {"ref": pm, "field": f, "value": v if _recorded(v) else None, "scope": "party-wide", "source": ref,
                                      "status": "recorded" if _recorded(v) else "missing"}
            for f in PERSONAL_FIELDS:   # never inherited
                facts[f"{pm}.{f}"] = {"ref": pm, "field": f, "value": None, "scope": "personal", "source": None, "status": "missing"}
            facts[f"{pm}.party_of"] = {"ref": pm, "field": "party_of", "value": ref, "scope": "party-wide", "source": ref, "status": "recorded"}
    step_free_wanted = sorted({m["ref"] for m in payload.get("members_for_ai") or []
                               for it in (m.get("notes_facts") or []) if isinstance(it, dict) and it.get("category") == "step_free_access" and it.get("strength") == "essential"})
    for a in payload.get("apartments") or []:
        A = f"A{a['apartment_id']}"
        for f in APARTMENT_FIELDS:
            v = a.get(f)
            facts[f"{A}.{f}"] = {"apartment": a["apartment_id"], "field": f, "value": v if _recorded(v) else None, "scope": "apartment", "source": "listing",
                                 "status": "recorded" if _recorded(v) else "missing"}
        if a.get("allocation_for_ai") is not None:
            facts[f"{A}.rooms"] = {"apartment": a["apartment_id"], "field": "rooms", "value": a["allocation_for_ai"], "scope": "apartment", "source": "rules", "status": "recorded"}
        if a.get("exit_exception") is not None:
            facts[f"R.{A}.exit_exception"] = {"apartment": a["apartment_id"], "field": "exit_exception",
                                             "value": a["exit_exception"], "scope": "rule",
                                             "source": "current booking-specific staff authority", "status": "recorded"}
        # 9 Nov 2026 (triage fix 1) / 10 Nov 2026 (T3(d) C3, A13(a)): when a member's own notes make step-free access ESSENTIAL,
        # the apartment-level fact the need turns on is in the catalogue. It is RECORDED only from the apartment's own staff
        # record (apartments.step_free_access: 1 confirmed step-free, 0 recorded as not step-free; never a lift, an address or
        # another apartment); with no record it is explicitly MISSING, so a review or brief can file the gap against the id. The
        # staff hold gate (notes_facts.hold_gate) remains the only outcome: a review never treats the need as met or unmet.
        if step_free_wanted:
            sfa = a.get("step_free_access")
            rec = sfa in (0, 1, True, False)
            facts[f"{A}.step_free_access"] = {"apartment": a["apartment_id"], "field": "step_free_access", "value": (bool(sfa) if rec else None), "wanted_by": list(step_free_wanted),
                                              "scope": "apartment", "source": ("staff record" if rec else "listing"), "status": ("recorded" if rec else "missing")}
        for feat in a.get("features") or []:   # apartment_features.compare: available/unavailable, or unverified (= not recorded: a gap, never evidence)
            key = feature_fact_key(feat.get("label") or feat.get("key") or "")
            if key:
                facts[f"{A}.feature.{key}"] = {"apartment": a["apartment_id"], "field": f"feature:{feat.get('label') or key}", "value": feat.get("recorded"),
                                               "wanted_by": feat.get("wanted_by") or [], "scope": "apartment", "source": "listing",
                                               "status": "recorded" if feat.get("recorded") not in FEATURE_UNRECORDED else "missing"}
        for rc in a.get("rule_checks") or []:
            facts[f"R.{A}.{rc['rule']}"] = {"apartment": a["apartment_id"], "field": rc["rule"], "value": rc.get("result"), "detail": rc.get("detail"),
                                            "scope": "rule", "source": rc.get("source") or "deterministic rule check", "status": "recorded"}
            if rc["rule"] == "budget":   # r7: the A16 household figures, only when the check produced them (no figures -> no fact to cite)
                hh = (rc.get("detail") or {}).get("household") or {}
                for f, unit in HOUSEHOLD_BUDGET_FACTS.items():
                    if _recorded(hh.get(f)):
                        facts[f"R.{A}.budget.{f}"] = {"apartment": a["apartment_id"], "field": f, "value": str(hh[f]), "unit": unit, "scope": "rule",
                                                      "source": rc.get("source") or "deterministic rule check", "status": "recorded"}
                src = rc.get("source") or "deterministic rule check"
                for ref, pp in ((rc.get("detail") or {}).get("per_person") or {}).items():   # Increment A: subject-attributed, lossless, with provenance
                    if not isinstance(pp, dict):
                        continue
                    for f, (text_key, num_key, unit) in PER_PERSON_BUDGET_FACTS.items():
                        if text_key in pp:
                            val = pp[text_key]   # current packet shape; None here is an explicit rejection by the producer (unusable amount): no fact, no fallback
                        else:
                            val = _legacy_money_text(pp.get(num_key), positive=(f == "stated_max"))   # older packet shape: the stored number, validated, as text (no arithmetic, no rounding)
                        if val is None:
                            continue
                        entry = {"apartment": a["apartment_id"], "ref": ref, "field": f, "value": str(val), "unit": unit, "scope": "rule", "status": "recorded"}
                        if f == "share":
                            entry["source"] = f"{src}; A12 guide share from the deterministic bed allocation, a proposal for the group's agreement"
                        else:
                            entry["source"] = f"{src}; I14 stated weekly maximum, a stated answer"
                            entry["reported_by"] = pp.get("reported_by") or ref
                        facts[f"R.{A}.budget.{f}.{ref}"] = entry
    return facts


def _legacy_money_text(v, *, positive: bool) -> str | None:
    """A2 projection safety (9 Nov 2026): an older packet's per-person number becomes a fact only if it is a valid money amount
    under the same validator the rules use (finite, not negative, positive where a stated maximum must be, in bounds); the text
    is the stored value itself, never a rounded or re-derived figure. NaN, infinities, negatives, zero maxima and non-numbers are
    not facts."""
    if not _recorded(v) or isinstance(v, bool) or not isinstance(v, (int, float, str, Decimal)):
        return None
    from . import budget as budget_mod
    try:
        budget_mod.money(v, positive=positive)
    except (ValueError, TypeError, ArithmeticError):
        return None
    return str(v).strip()


def ids_for_ref(cat: dict[str, dict], ref: str) -> list[str]:
    return [k for k, v in cat.items() if v.get("ref") == ref]


def owner(cat: dict[str, dict], fid: str) -> str | None:
    f = cat.get(fid)
    return f.get("ref") if f else None


# Conservative wording guard for free-text notes in reviews and briefs. The packets carry no sex, age, nationality, country,
# language, religion or ethnicity, so any reasoning in those terms is either invented or inferred and the output is unusable.
# Allowed: the adult declaration facts (18 at move-in / adults confirmed), which ARE recorded rule facts.
_ALLOWED_AGE_PHRASES = re.compile(r"\b(aged?\s*18(\s*\+|\s*or\s*(over|older)|\s*at\s*move[- ]?in)?|over\s*18|18\s*\+|under\s*18|adults?(\s+(declaration|attestation|confirmed|confirmation|only))?|18\s*at\s*move[- ]?in)\b", re.I)
_TRAIT_WORDS = re.compile(r"\b(age|ages|aged|ageing|aging|older|younger|elderly|young|gender|sex|sexes|sexual|nationality|nationalities|ethnic\w*|race|racial|"
                          r"religio\w*|muslim|christian|hindu|buddhist|jewish|sikh|country|countries|cultur\w*|background|backgrounds|female|females|male|males|woman|women|man|men|"
                          r"girl|girls|boy|boys|lady|ladies|guy|guys|he|she|his|her|hers|him|himself|herself|language|languages|speak\w*|accent|asian|european|african|"
                          r"indian|chinese|korean|japanese|vietnamese|swedish|german|french|australian|american|latin\w*|arab\w*|foreign\w*|"
                          r"international|compatriot\w*|countrym[ae]n)\b", re.I)
# Known gap (stated, not hidden): bare country names ("from India") and languages used as course names are not matched; the guard
# is a wording check on words the neutral packet can never justify, not a semantic bias detector.


def policy_violations(text: str) -> list[str]:
    """Words in a free note that reason from a trait the packets never carried (sex, age, nationality/country, language, religion,
    ethnicity, pronouns). Adult-declaration phrases are removed before scanning. This is a wording guard, deliberately
    conservative; it cannot prove a note is true or free of subtler bias."""
    if not isinstance(text, str) or not text:
        return []
    cleaned = _ALLOWED_AGE_PHRASES.sub(" ", text)
    hits = sorted({m.group(0).lower() for m in _TRAIT_WORDS.finditer(cleaned)})
    return hits


def check_items(items, cat: dict[str, dict], *, ref: str | None, allow_missing: bool, require_own: bool, label: str, allow_gap: bool = True) -> tuple[list[dict], list[str]]:
    """Validate a list of {fact_ids: [...], note: str} items. Returns (clean items, problems).
    ref:          the person the items are about (None for group-level items)
    allow_missing: items may cite facts whose status is 'missing' (missing_facts / discussion points), else only recorded facts
    kind:         an item may carry kind 'evidence' (default; recorded facts only unless allow_missing) or 'gap' (an explicit
                  missing-data observation: must cite >=1 fact that is NOT recorded). A gap item is kept, labelled, and never
                  counts as the evidence a verdict needs. Structural only: the validator cannot tell whether a 'gap' note
                  smuggles a positive claim ("commute meets requirement") - that limitation is stated, not hidden.
    allow_gap:    False for lists that must hold positive or negative EVIDENCE (brief strengths, tensions, what could work / cause
                  issues): a kind 'gap' item there is rejected, so an unknown can never be dressed up as a strength
    require_own:  at least one cited fact must belong to `ref` (a person cannot be described with only other people's facts)
    A person-scoped item may cite: that person's own facts (incl. party-wide facts reported by the lead), apartment facts, rule
    facts. It may NOT cite another person's PERSONAL fact (that is how a lead's study/work/lifestyle would be copied onto a party
    member).
    compared_with (r5, person-scoped items only): a housemate's SAME-FIELD facts as separately attributed counterpart evidence,
    [{"ref": "U2", "fact_ids": ["U2.routine", ...]}]. Each counterpart is another ref in the catalogue (not the subject), each
    counterpart fact is RECORDED (a companion's personal fields are missing, so they can never be compared), is a personal or
    party-wide field, and the subject's OWN fact for that same field must be recorded and cited in fact_ids (so it is a
    comparison, not other-person proof). Counterpart facts never satisfy require_own or the minimum-content checks."""
    problems: list[str] = []
    if not isinstance(items, list):
        return [], [f"{label}: not a list"]
    clean = []
    for i, it in enumerate(items):
        if not isinstance(it, dict) or not isinstance(it.get("note"), str) or not it["note"].strip():
            problems.append(f"{label}[{i}]: not an object with a note")
            continue
        fids = it.get("fact_ids")
        if not isinstance(fids, list) or not fids or not all(isinstance(x, str) for x in fids):
            problems.append(f"{label}[{i}]: no fact_ids")
            continue
        if len(fids) > FACT_IDS_MAX:
            problems.append(f"{label}[{i}]: {len(fids)} fact ids exceed the response size limit of {FACT_IDS_MAX}")
            continue
        n_words = len(it["note"].split())
        if n_words > NOTE_WORDS_MAX:
            problems.append(f"{label}[{i}]: note of {n_words} words exceeds the response size limit of {NOTE_WORDS_MAX}")
            continue
        hits = policy_violations(it["note"])
        if hits:
            problems.append(f"{label}[{i}]: note reasons from a trait the packet never carried ({', '.join(hits[:4])})")
            continue
        wp = wording_problems(it["note"], fids, cat, ref=ref)
        if wp:
            problems.append(f"{label}[{i}]: {wp}")
            continue
        kind = it.get("kind", "evidence")
        if kind not in ("evidence", "gap"):
            problems.append(f"{label}[{i}]: kind {kind!r} is not evidence/gap")
            continue
        if kind == "gap" and not allow_gap:
            problems.append(f"{label}[{i}]: a gap item cannot stand as evidence here; unknowns belong under missing_facts or discussion_points")
            continue
        own = False
        bad = False
        n_missing = 0
        for fid in fids:
            f = cat.get(fid)
            if f is None:
                problems.append(f"{label}[{i}]: unknown fact {fid!r}")
                bad = True
                continue
            if f["status"] == "missing":
                n_missing += 1
                if kind != "gap" and not allow_missing:
                    problems.append(f"{label}[{i}]: {fid} is not recorded (cannot support an evidence finding; use kind 'gap' to flag it as unknown)")
                    bad = True
            if ref is not None and f.get("ref") not in (None, ref) and f.get("scope") == "personal":
                problems.append(f"{label}[{i}]: {fid} is {f['ref']}'s personal fact, not {ref}'s (a comparison cites the housemate's same field under compared_with)")
                bad = True
            if f.get("ref") == ref:
                own = True
        if kind == "gap" and n_missing == 0 and not bad:
            problems.append(f"{label}[{i}]: a gap item must cite at least one fact that is not recorded")
            bad = True
        if bad:
            continue
        if require_own and ref is not None and not own:
            problems.append(f"{label}[{i}]: cites no fact of {ref}")
            continue
        item = {"fact_ids": list(dict.fromkeys(fids)), "note": it["note"].strip(), "kind": kind}
        if it.get("compared_with") not in (None, []):
            cmp, cp = _check_compared_with(it.get("compared_with"), fids, cat, ref=ref, label=f"{label}[{i}]")
            if cp:
                problems += cp
                continue
            item["compared_with"] = cmp
        clean.append(item)
    return clean, problems


# Guard (3), 9 Nov 2026 (root live-replay finding; clause-aware after root's A3 review): course, school, level, doing and campus-distance
# labels alone do not establish timetable alignment. A person MAY have typed timing into a free-text answer; that is quoted as recorded,
# for that person, and never generalised to others. So a note may not ASSERT that timetables / class schedules line up or will fit unless
# the recorded facts themselves say so (whole recorded value quoted). Judged clause by
# clause: a clause that invites discussion ("worth comparing timetables") or negates / flags the gap ("class times are not
# recorded", "a shared campus does not establish that class times overlap") passes, as does a question; a cited fact's WHOLE recorded value quoted
# verbatim (a course called "Timetable Design", a typed course answer) is masked before judging - the same words elsewhere are judged; "both study
# Engineering at the same RMIT campus" (what is recorded) passes. Bare "schedule(s)" is judged only when the note rests on study
# facts and cites no routine (sleep-pattern) fact, since a routine note may honestly say "schedules". No blanket word ban.
STUDY_FIELDS = ("doing", "school", "course", "level", "campus_km")   # campus_km: no longer supplied (M4); kept so an old cited id is still judged
_TIMETABLE = re.compile(r"\b(time ?tables?|class(?:es)? (?:times?|schedules?|hours)|lecture (?:times?|schedules?)|(?:study|uni|university|campus|contact) "
                        r"(?:hours|schedules?|days)|attendance|on campus (?:at|around) the same (?:time|hours))\b", re.I)
_SCHEDULE = re.compile(r"\bschedules?\b", re.I)
_ASSERTS = re.compile(r"\b(line up|lines up|align(?:s|ed|ment)?|overlap(?:s|ping)?|match(?:es|ing)?|coincide|similar|same|compatible|together|in sync|"
                      r"likely|probably|will|should|would|can|could|easy|convenient|fit|fits|suit|suits|work|works)\b", re.I)
_NEGATED = re.compile(r"\b(not|no|never|none|nothing|isn't|aren't|don't|doesn't|won't|cannot|can't|without|unknown|unrecorded|unclear|whether)\b", re.I)
_NEGATED_IDIOM = re.compile(r"\b(not only|not just|not least|no doubt|without doubt|without question)\b", re.I)
_INVITES = re.compile(r"\b(discuss(?:ed|ing)?|talk|compared?|comparing|agree|work out|sort out|ask|check|confirm|find out|worth (?:raising|covering|comparing|checking))\b", re.I)
_NOTE_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_NOTE_CLAUSE = re.compile(r"\s*[;:]\s*|\s+-\s+|\s+\u2014\s+|,\s+(?=(?:so|but|which|while|although|though|and|or|since|because)\b)|"
                          r"\s+\b(?:so|but|while|although|though|since|because|and|or)\b\s+", re.I)
_RECORDED_SPAN = "[recorded]"


def _mask_recorded_values(note: str, literal) -> str:
    """Replace each cited fact's WHOLE recorded value, where it appears verbatim in the note, with a neutral token. Only the exact
    recorded span is exempt (a course called 'Timetable Design', a typed answer quoted in full); the same words elsewhere are judged."""
    out = str(note or "")
    for v in sorted({str(v) for v in (literal or ()) if v not in (None, "") and len(str(v).strip()) >= 4}, key=len, reverse=True):
        out = re.sub(re.escape(v.strip()), _RECORDED_SPAN, out, flags=re.I)
    return out


# 9 Nov 2026, C200 live-1 groups 14/21 ("...any timetable overlap with others are unknown"): the timetable phrase and a NOMINAL
# overlap/alignment form the SUBJECT, and the clause's own predicate states explicit uncertainty about it. Only that shape counts after
# the asserting span: optional relational suffix (with/between/among/across/for + 1-3 words), copula, then one of a CLOSED list of
# uncertainty predicates. A finite verb ("timetables align ..."), any other trailing words, positive certainty ("undeniable") or a
# plain "not <word>" ("not disputed") never negate (root counterexamples, three rounds).
# r4 (9 Nov 2026, root replay of C200 live-1 group 21: "...any study or timetable overlap with U1 or U3 is unknown."): the clause splitter
# cuts a coordinated ref suffix ("U1 or U3") BEFORE the nominal matcher can see the copula, so an honest unknown was rejected. Narrow
# repair: a FULLY BOUND nominal-uncertainty span - timetable phrase, at most one word, nominal overlap/alignment, optional relational
# suffix of 1-3 tokens with at most one coordinated token ("with U1 or U3", "between U1 and U2"), copula, closed uncertainty predicate -
# is masked with a neutral token before clause splitting; everything outside that exact span is validated unchanged. No whole-item,
# whole-sentence or cross-clause exemption; no lookahead beyond the bound span.
_BOUND_UNCERTAIN = re.compile(_TIMETABLE.pattern + r"(?:\s+[\w'\u2019-]+)?\s+(?:overlap|overlaps|alignment)\b"
                              r"(?:\s+(?:with|between|among|across|for)\s+(?:[\w'\u2019-]+,?\s*){1,3}(?:(?:or|and)\s+[\w'\u2019-]+\s*)?)?"
                              r"\s*(?:is|are|remains?|stays?)\s+"
                              r"(?:unknown|unrecorded|unclear|uncertain|undetermined|unconfirmed|unverified|not (?:yet )?(?:recorded|known|established|confirmed|verified)|"
                              r"yet to be (?:confirmed|established|checked)|to be confirmed)"
                              r"(?: and worth confirming)?\b(?=[.!?]?\s*$)", re.I)
_UNCERTAIN_SPAN = "[uncertain]"
# A nominal "timetable fit ... is unknown" is also an honest gap.
# All nominal masks are sentence-end bound: masking must
# not launder a trailing endorsement such as "unknown, though likely fine".
_BOUND_FIT_UNCERTAIN = re.compile(_TIMETABLE.pattern + r"(?:\s+[\w'\u2019-]+)?\s+fit\b"
                                r"(?:\s+(?:with|between|among|across|for)\s+(?:[\w'\u2019-]+,?\s*){1,3}(?:(?:or|and)\s+[\w'\u2019-]+\s*)?)?"
                                r"\s*(?:is|are|remains?|stays?)\s+"
                                r"(?:unknown|unrecorded|unclear|uncertain|undetermined|unconfirmed|unverified|not (?:yet )?(?:recorded|known|established|confirmed|verified)|"
                                r"yet to be (?:confirmed|established|checked)|to be confirmed)\b(?=[.!?]?\s*$)", re.I)

# Bare schedule overlap can also be the subject of an explicit unknown.
# Only a closed, sentence-ending uncertainty is exempt; never a positive
# finite alignment or a trailing endorsement. A request to confirm is not one.
_BOUND_SCHEDULE_UNCERTAIN = re.compile(
    _SCHEDULE.pattern + r"\s+(?:overlap|overlaps|alignment)\b"
    r"(?:\s+(?:with|between|among|across|for)\s+(?:[\w'\u2019-]+,?\s*){1,3}"
    r"(?:(?:or|and)\s+[\w'\u2019-]+\s*)?)?"
    r"\s*(?:is|are|remains?|stays?)\s+"
    r"(?:unknown|unrecorded|unclear|uncertain|undetermined|unconfirmed|unverified|"
    r"not (?:yet )?(?:recorded|known|established|confirmed|verified)|"
    r"yet to be (?:confirmed|established|checked)|to be confirmed)"
    r"(?: and worth confirming)?\b(?=[.!?]?\s*$)", re.I)

# A narrowly stated refusal to infer is also a gap, not an alignment claim.
_BOUND_REFUSAL = re.compile(
    r"(?:" + _TIMETABLE.pattern + "|" + _SCHEDULE.pattern + r")"
    r"\s+(?:overlap|alignment|fit)\s+cannot be "
    r"(?:inferred|determined|assessed|established|confirmed)"
    r"(?: from (?:the )?recorded facts)?\b(?=[.!?]?\s*$)", re.I)


def _mask_bound_uncertainty(sentence: str) -> str:
    """Replace each fully bound nominal-uncertainty span with a neutral token so the clause splitter cannot cut inside it."""
    sentence = _BOUND_REFUSAL.sub(_UNCERTAIN_SPAN, sentence)
    sentence = _BOUND_SCHEDULE_UNCERTAIN.sub(_UNCERTAIN_SPAN, sentence)
    return _BOUND_UNCERTAIN.sub(_UNCERTAIN_SPAN, _BOUND_FIT_UNCERTAIN.sub(_UNCERTAIN_SPAN, sentence))


def _locally_negated(clause: str, lo: int, hi: int) -> bool:
    """A negation / unknown marker within the asserting span (32 characters before the earlier of phrase and marker, to the later end);
    'not only' and similar idioms are not negations."""
    window = clause[max(0, lo - 32):hi]
    return bool(_NEGATED.search(_NEGATED_IDIOM.sub(" ", window)))


def timetable_inference(note: str, routine_cited: bool, literal=()) -> str | None:
    """The timetable phrase of the first clause of `note` that ASSERTS a timetable / class-schedule alignment, else None.
    `literal`: recorded values of the cited facts; only their exact spans in the note are the facts' own wording, not an inference."""
    for sentence in _NOTE_SENTENCE.split(_mask_recorded_values(note, literal)):
        if sentence.rstrip().endswith("?"):          # a question asks; it does not assert
            continue
        for clause in _NOTE_CLAUSE.split(_mask_bound_uncertainty(sentence)):
            if not clause or not clause.strip() or _INVITES.search(clause):
                continue
            m = _TIMETABLE.search(clause) or (None if routine_cited else _SCHEDULE.search(clause))
            if not m:
                continue
            a = _ASSERTS.search(clause)
            if not a:
                continue
            if _locally_negated(clause, min(m.start(), a.start()), max(m.end(), a.end())):
                continue
            return m.group(0).strip()
    return None


def wording_problems(note: str, fids: list, cat: dict[str, dict], ref: str | None = None) -> str | None:
    """Context-aware wording guards that read what the note CITES (Increment A, 9 Nov 2026). (1) A note resting on a proposed share
    fact (R.A<id>.budget.share.<ref>) may not call that share agreed / accepted / signed off - it is an A12 proposal for the group's
    agreement; 'not agreed', 'yet to be agreed', 'to be agreed' are honest and pass, and an 'agreed' about something else in another clause is not judged. (2) A note resting on an ACCEPTED-preference fact
    (rooms_accepted, apt_sizes_accepted, bunk_ok, mate_max) may not present it as a first choice / preference / request unless the
    person's room_first fact is also cited, in which case the first choice is honestly on the table; the preference word must be tied to a room/apartment noun, so 'prefers early nights' is not judged. (3) No note may assert that timetables / class
    schedules line up (nothing records one; clause-aware, negations / invitations / literal recorded wording pass - timetable_inference); bare 'schedule' is judged only on study facts without a routine fact. No other words are judged."""
    cited = [cat.get(f) or {} for f in fids if isinstance(f, str)]
    if any(isinstance(f, str) and ".budget.share." in f for f in fids):
        hit = agreed_share_wording(note)
        if hit:
            return f"describes a proposed A12 guide share as agreed or accepted ('{hit}'); shares are proposals for the group's agreement"
    accepted = [f for f in cited if f.get("field") in ("rooms_accepted", "apt_sizes_accepted", "bunk_ok", "mate_max")]
    own_first = [f for f in cited if f.get("field") == "room_first" and (ref is None or f.get("ref") == ref)]   # another person's first choice licenses nothing about this one
    if accepted and not own_first:
        hit = first_choice_wording(note)
        if hit:
            return f"describes an accepted room/apartment preference as a first choice / preference / request ('{hit}'); it is what the party said it would accept (cite room_first for the first choice)"
    study = any(f.get("field") in STUDY_FIELDS for f in cited)
    routine = any(f.get("field") == "routine" for f in cited)
    literal = [f.get("value") for f in cited if f.get("status") == "recorded"]
    # a timetable assertion is refused whatever is cited (nothing records one); bare 'schedule' only when it rests on study facts
    hit = timetable_inference(note, routine_cited=(routine or not study), literal=literal)
    if hit:
        return f"asserts a timetable / class schedule ('{hit}') that the recorded facts do not support; course, school and campus labels alone do not establish timetable alignment"
    return None


def _check_compared_with(cw, own_fids: list, cat: dict[str, dict], *, ref: str | None, label: str) -> tuple[list[dict], list[str]]:
    """r5 counterpart evidence (see check_items). Returns (clean entries, problems); any problem rejects the whole item."""
    if ref is None:
        return [], [f"{label}: compared_with is only for a person's own brief items (group items already cite everyone)"]
    if not isinstance(cw, list) or not all(isinstance(e, dict) for e in cw):
        return [], [f"{label}: compared_with is not a list of {{ref, fact_ids}} objects"]
    if len(cw) > COMPARE_REFS_MAX:
        return [], [f"{label}: compared_with names {len(cw)} housemates, over the response size limit of {COMPARE_REFS_MAX}"]
    group_refs = {v.get("ref") for v in cat.values() if v.get("ref")}
    own_fields = {cat[f]["field"] for f in own_fids if f in cat and cat[f].get("ref") == ref and cat[f]["status"] == "recorded"}
    problems: list[str] = []
    clean: list[dict] = []
    total = 0
    seen_refs: set[str] = set()
    for e in cw:
        other = e.get("ref")
        fids = e.get("fact_ids")
        if not isinstance(other, str) or other not in group_refs:
            problems.append(f"{label}: compared_with ref {other!r} is not a member of this group")
            continue
        if other == ref:
            problems.append(f"{label}: compared_with cannot compare {ref} with themselves")
            continue
        if other in seen_refs:
            problems.append(f"{label}: compared_with repeats {other}")
            continue
        seen_refs.add(other)
        if not isinstance(fids, list) or not fids or not all(isinstance(x, str) for x in fids):
            problems.append(f"{label}: compared_with[{other}] has no fact_ids")
            continue
        total += len(fids)
        for fid in fids:
            f = cat.get(fid)
            if f is None:
                problems.append(f"{label}: compared_with cites unknown fact {fid!r}")
            elif f.get("ref") != other:
                problems.append(f"{label}: compared_with[{other}] cites {fid}, which is not {other}'s fact")
            elif f.get("field") not in COMPARABLE_FIELDS:
                problems.append(f"{label}: compared_with[{other}] cites {fid}, not a personal or party-wide answer")
            elif f["status"] != "recorded":
                problems.append(f"{label}: compared_with[{other}] cites {fid}, which is not recorded for {other} (a companion's personal answers are never recorded)")
            elif f["field"] not in own_fields:
                problems.append(f"{label}: compared_with[{other}] cites {fid} but {ref}'s own recorded {ref}.{f['field']} is not in fact_ids (a comparison needs both sides)")
        clean.append({"ref": other, "fact_ids": list(dict.fromkeys(fids))})
    if total > COMPARE_IDS_MAX:
        problems.append(f"{label}: compared_with cites {total} counterpart facts, over the response size limit of {COMPARE_IDS_MAX}")
    return (clean if not problems else []), problems


# ---------------------------------------------------------------- r6 coverage: who answered for themselves
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12}
_NUM = r"(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_PEOPLE = r"(?:residents?|people|persons?|members?|housemates?|flatmates?|roommates?|refs?|applicants?|tenants?|occupants?|enquirers?|adults?|of them)"
# "<n> of <m> <people noun>", or "<n> of the <m>" directly followed by a verb about having / lacking (people implied)
COVERAGE_CLAIM = re.compile(rf"\b{_NUM}\s+(?:out\s+of|of)\s+(?:the\s+|these\s+|all\s+)?{_NUM}(?:\s+{_PEOPLE}\b|(?=\s+(?:have|has|had|are|is|were|lack\w*|gave|with|without)\b))", re.I)
# the negative coverage phrase: a negation followed within three words by an EXPLICIT lifestyle / compatibility word, or by
# "personal" directly qualifying answers / facts / profile / habits, in the same CLAUSE as the count. Field-specific words
# (routine, guests, university, school...), bare 'answers' / 'profiles' after arbitrary words, and 'recorded' on its own are
# deliberately NOT triggers: "three of seven have no recorded routine", "six of seven people lack university answers" or "two of
# seven people asked for a gym that is unrecorded" are different counts that may be true and are left unjudged.
_COVERAGE_WORDS = r"(?:lifestyle|compatibility|personal\s+(?:compatibility\s+|lifestyle\s+)?(?:answers?|facts?|profiles?|habits?))"
_NEGATIVE_COVERAGE = re.compile(rf"\b(?:no|not|without|lack\w*|missing|never|un)(?:\s*\w+){{0,3}}?\s*{_COVERAGE_WORDS}\b", re.I)
_POSITIVE_COVERAGE = re.compile(rf"\b(?:have|has|with|gave|provided|recorded|answered|supplied)\b(?:\s+\w+){{0,3}}?\s+{_COVERAGE_WORDS}\b", re.I)
_CLAUSE_BREAK = re.compile(r",|;|:|\s-\s|\bwhile\b|\bwhereas\b|\bbut\b|\band\b|\bwhich\b", re.I)
_SENTENCE = re.compile(r"[^.;!?]+")


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUM_WORDS[tok.lower()]


def coverage(payload: dict) -> dict:
    """Deterministic people/coverage block for a packet (no database): every ref, who answered the lifestyle / compatibility
    questions (PERSONAL_FIELDS: at least one recorded, not necessarily all) themselves, who is a companion carrying only the
    party-wide answers their enquirer gave, each
    party with party_size AS STATED (which counts the enquirer) next to the companions actually listed, and a staff line.
    Wording deliberately says 'independently recorded lifestyle / compatibility answers': companions do have introduction
    profiles outside this packet, so 'no personal facts' would overstate it."""
    members = payload.get("members_for_ai") or []
    refs: list[str] = []
    with_own: list[str] = []
    without: list[str] = []
    parties = []
    for m in members:
        ref = m["ref"]
        refs.append(ref)
        (with_own if any(_recorded(m.get(f)) for f in PERSONAL_FIELDS) else without).append(ref)
        comps = list(m.get("party_members") or [])
        for pm in comps:
            refs.append(pm)
            without.append(pm)
        if comps or (m.get("party_size") not in (None, "", 1, "1")):
            parties.append({"enquirer": ref, "companions": comps, "party_size_as_stated_including_enquirer": m.get("party_size"),
                            "people_in_party_including_enquirer": 1 + len(comps)})
    total = len(refs)
    line = (f"Coverage: {len(with_own)} of {total} people ({', '.join(with_own) or 'none'}) gave their own lifestyle / compatibility answers (at least one recorded; "
            f"single unanswered questions are listed under missing facts); "
            f"{len(without)} ({', '.join(without) or 'none'}) have no independently recorded lifestyle / compatibility answers in this packet"
            + (" - companions' dates and party details were given by their enquirer." if any(p["companions"] for p in parties) else "."))
    return {"total": total, "refs": refs, "with_own_lifestyle_answers": with_own, "without_own_lifestyle_answers": without,
            "parties": parties, "party_size_counts_the_enquirer": True, "line": line}


def coverage_claim_problems(text, cov: dict, label: str) -> list[str]:
    """r6 guard, deliberately narrow: in each CLAUSE of `text` that says '<n> of <m> <people-noun>' AND carries a negative
    coverage phrase (no / not / without / lack / missing / un- ... lifestyle / compatibility / personal answers|facts|profile),
    n must equal the number WITHOUT own lifestyle answers and m the total; the same clause with a positive phrase (have / with /
    gave ... lifestyle / compatibility / personal ...) must give the number WITH own answers. Anything else is untouched: counts
    of missing features, campus distances, universities, single fields (routine, guests), bare 'answers' after other words,
    clauses with several counts - nothing about the sentence's wider meaning is proven."""
    if not isinstance(text, str) or not text:
        return []
    problems: list[str] = []
    for sent in _SENTENCE.findall(text):
        # judge each count only against the coverage phrase in its OWN clause ("two of seven have their own lifestyle answers,
        # while five of seven have no recorded lifestyle answers" is two true claims); a clause with several counts is left alone
        for clause in _CLAUSE_BREAK.split(sent):
            claims = list(COVERAGE_CLAIM.finditer(clause))
            if len(claims) != 1:
                continue
            mt = claims[0]
            n, m = _num(mt.group(1)), _num(mt.group(2))
            neg = _NEGATIVE_COVERAGE.search(clause)
            pos = _POSITIVE_COVERAGE.search(clause)
            if neg:
                want = len(cov["without_own_lifestyle_answers"])
                if (n, m) != (want, cov["total"]):
                    problems.append(f"{label}: coverage count '{mt.group(0)}' disagrees with the packet ({want} of {cov['total']} people have no independently recorded lifestyle / compatibility answers)")
            elif pos:
                want = len(cov["with_own_lifestyle_answers"])
                if (n, m) != (want, cov["total"]):
                    problems.append(f"{label}: coverage count '{mt.group(0)}' disagrees with the packet ({want} of {cov['total']} people gave their own lifestyle / compatibility answers)")
    return problems


def _fact_bit(fid: str, cat: dict[str, dict]) -> str:
    """One cited fact as 'id=value', with provenance: missing facts say so, party-wide facts name who reported them."""
    f = cat.get(fid) or {}
    if f.get("status") == "missing":
        return f"{fid}: not recorded" + (f" ({f['value']})" if f.get("value") not in (None, "") else "")
    if f.get("scope") == "party-wide" and f.get("source") and f.get("source") != f.get("ref"):
        return f"{fid}={f.get('value')!s} (reported by {f['source']} for the party)"
    if f.get("reported_by") and f.get("reported_by") != f.get("ref"):
        return f"{fid}={f.get('value')!s} {f.get('unit') or ''}".rstrip() + f" (reported by {f['reported_by']} for the party)"
    if f.get("unit"):
        return f"{fid}={f.get('value')!s} {f['unit']}"
    return f"{fid}={f.get('value')!s}"


def render(item: dict, cat: dict[str, dict]) -> str:
    """Safe factual rendering of one validated item: the note plus the cited facts as 'id=value' (party-wide facts labelled with
    their source), so a reader can see what the words rest on. r5: counterpart facts under compared_with are rendered in their
    own bracket attributed to the housemate they belong to, with the same provenance labels."""
    bits = [_fact_bit(fid, cat) for fid in item.get("fact_ids") or []]
    head = ("[gap - not recorded] " if item.get("kind") == "gap" else "") + item["note"]
    out = head + ("  [" + "; ".join(bits) + "]" if bits else "")
    for e in item.get("compared_with") or []:
        out += f"  [compared with {e['ref']}: " + "; ".join(_fact_bit(fid, cat) for fid in e.get("fact_ids") or []) + "]"
    return out
