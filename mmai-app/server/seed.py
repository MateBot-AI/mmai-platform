"""Synthetic TEST enquiries (brief section 4). Every record is flagged is_test=1, has a TEST last name and a
@test.mmai.invalid email. Staff can delete all of them in one action. Deterministic (seeded) so tests are repeatable."""
from __future__ import annotations

import random
from datetime import date, timedelta

from . import lease_dates, rules, submissions
from .enquiries import create_enquiry
from .institutions import INSTITUTIONS

FIRST = {
    "Sweden": ["Anna", "Erik", "Elsa", "Oskar", "Maja", "Lukas"], "Norway": ["Ingrid", "Magnus", "Sofie"], "Germany": ["Lena", "Jonas", "Mia", "Felix"],
    "France": ["Camille", "Louis", "Chloé"], "Italy": ["Giulia", "Marco", "Sofia"], "Spain": ["Lucía", "Pablo"], "Netherlands": ["Sanne", "Daan"],
    "United Kingdom": ["Olivia", "Harry", "Amelia", "George"], "Ireland": ["Aoife", "Conor"], "United States": ["Emma", "Liam", "Ava"], "Canada": ["Noah", "Chloe"],
    "Brazil": ["Ana", "Gabriel", "Beatriz"], "Colombia": ["Valentina", "Santiago"], "Chile": ["Catalina", "Matías"],
    "China": ["Wei", "Li", "Yan", "Jun"], "Hong Kong": ["Ka Yan", "Chun"], "Japan": ["Yuki", "Haruto", "Sakura"], "South Korea": ["Ji-woo", "Min-jun"],
    "Taiwan": ["Yu-ting", "Chia-hao"], "Singapore": ["Wei Ling", "Jia Hao"], "Vietnam": ["Linh", "Minh", "Thu"], "Indonesia": ["Putri", "Budi"],
    "Thailand": ["Nok", "Somchai"], "Malaysia": ["Aisyah", "Wei Jie"], "Philippines": ["Maria", "Jose"],
    "India": ["Priya", "Arjun", "Ananya", "Rohan"], "Pakistan": ["Ayesha", "Ali"], "Nepal": ["Sita", "Bikash"], "Bangladesh": ["Farhan", "Nusrat"], "Sri Lanka": ["Dilani", "Kasun"],
    "Australia": ["Jack", "Ruby"], "New Zealand": ["Aroha", "Tane"], "Kenya": ["Wanjiru", "Kamau"], "Turkey": ["Elif", "Mehmet"], "Saudi Arabia": ["Fatimah", "Omar"],
}
LANGS = {
    "Sweden": ["Swedish", "English"], "Norway": ["Norwegian", "English"], "Germany": ["German", "English"], "France": ["French", "English"], "Italy": ["Italian", "English"],
    "Spain": ["Spanish", "English"], "Netherlands": ["Dutch", "English"], "United Kingdom": ["English"], "Ireland": ["English"], "United States": ["English"], "Canada": ["English", "French"],
    "Brazil": ["Portuguese", "English"], "Colombia": ["Spanish", "English"], "Chile": ["Spanish", "English"], "China": ["Mandarin", "English"], "Hong Kong": ["Cantonese", "English"],
    "Japan": ["Japanese", "English"], "South Korea": ["Korean", "English"], "Taiwan": ["Mandarin", "English"], "Singapore": ["English", "Mandarin"], "Vietnam": ["Vietnamese", "English"],
    "Indonesia": ["Indonesian", "English"], "Thailand": ["Thai", "English"], "Malaysia": ["Malay", "English"], "Philippines": ["Filipino", "English"], "India": ["Hindi", "English"],
    "Pakistan": ["Urdu", "English"], "Nepal": ["Nepali", "English"], "Bangladesh": ["Bengali", "English"], "Sri Lanka": ["Sinhala", "English"], "Australia": ["English"],
    "New Zealand": ["English"], "Kenya": ["English"], "Turkey": ["Turkish", "English"], "Saudi Arabia": ["Arabic", "English"],
}
TYPED = {"United Kingdom": ["UK", "England", "United Kingdom", "Scotland"], "United States": ["USA", "US", "United States"], "South Korea": ["Korea", "South Korea"],
         "Netherlands": ["Holland", "Netherlands", "the Netherlands"], "China": ["China", "PRC", "china"], "Vietnam": ["Viet Nam", "Vietnam"]}
INTROS = [
    "Hi! I'm {n} from {c}, starting a {lvl} at {school} in {m}. I love cooking, running along the Yarra and weekend trips.",
    "Hey, I'm {n}. Coming to Melbourne for {lvl} study. Pretty relaxed, into football and music, happy to share meals.",
    "I'm {n}, {c}-born, {lvl} student. Quiet, tidy, early classes. Looking for friendly flatmates who respect shared spaces.",
    "{n} here. Working holiday for a year, hospitality shifts so odd hours. Social but considerate. Coffee snob.",
    "Hello, I'm {n}. First time living abroad. Would love housemates to explore the city with. I cook a lot of {c} food!",
]
ELSE = ["", "", "", "I have mild asthma so a smoke-free home matters to me.", "I play guitar (quietly!).", "I work night shifts twice a week.",
        "Vegetarian household preferred but not essential.", "I'd love to be near the library.", "My partner may visit for two weeks in April."]


def _pick(r, seq):
    return seq[r.randrange(len(seq))]


def _exit_day_after(r: random.Random, earliest: date) -> date:
    """A random day inside the first A10(b) move-out window that still has days on or after `earliest`."""
    for yr in (earliest.year, earliest.year + 1, earliest.year + 2):
        for m1, d1, m2, d2, _label in lease_dates.EXIT_WINDOWS:
            lo, hi = max(date(yr, m1, d1), earliest), date(yr, m2, d2)
            if lo <= hi:
                return lo + timedelta(days=r.randint(0, (hi - lo).days))
    raise AssertionError("unreachable: every year has three exit windows")


def build_enquiry(r: random.Random, i: int, scenario: str, today: date, listed: list[date] = ()) -> dict:
    country = _pick(r, list(FIRST))
    name = _pick(r, FIRST[country])
    # dates: a share tracks the listed apartments' availability (A17 keeps a group's future vacancy within 5% of the stay, so
    # demo enquirers must want to move in close to when something is actually free); the rest cluster around the Jan/Feb and
    # July cycles plus some odd months
    cycle = _pick(r, (["listed"] * 4 if listed else []) + ["janfeb", "janfeb", "janfeb", "janfeb_july", "janfeb_july", "july", "oct", "quiet"])
    if scenario == "janfeb_july":
        cycle = "janfeb_july"
    y = today.year + 1
    if cycle == "listed":
        # move in a few days after the apartment is free (or a few days from now when it is free already) and leave in a
        # move-out window at least five months later
        free_now = any(d <= today for d in listed)
        wanted = today if free_now and r.random() < 0.5 else _pick(r, listed)   # half of them want something that is free already
        move_in = max(wanted, today + timedelta(days=3)) + timedelta(days=r.randint(0, 4))
        move_out = _exit_day_after(r, move_in + timedelta(days=_pick(r, [150, 150, 150, 240, 330])))
    elif cycle in ("janfeb", "janfeb_july"):
        move_in = date(y, r.choice([1, 2]), r.randint(5, 27))
        if cycle == "janfeb_july":
            move_out = date(y, 7, r.randint(1, 20))
        else:
            move_out = _pick(r, [date(y, 11, r.randint(1, 30)), date(y, 12, r.randint(1, 5)), date(y + 1, 1, r.randint(10, 31)), date(y + 1, 2, r.randint(1, 15)), date(y, 7, r.randint(1, 31))])
    elif cycle == "july":
        move_in = date(today.year if today.month < 7 else y, 7, r.randint(1, 25))
        move_out = _pick(r, [date(move_in.year, 11, r.randint(1, 30)), date(move_in.year + 1, 2, r.randint(1, 15)), date(move_in.year + 1, 7, r.randint(1, 20))])
    elif cycle == "oct":
        move_in = date(today.year, r.choice([10, 11]), r.randint(1, 28))
        if move_in < today:
            move_in = today + timedelta(days=r.randint(7, 40))
        move_out = _pick(r, [date(y, 2, r.randint(1, 15)), date(y, 7, r.randint(1, 20))])
    else:
        move_in = today + timedelta(days=r.randint(10, 120))
        move_out = move_in + timedelta(days=r.randint(100, 400))
    kind = rules.guide_kind_for(move_in, move_out)
    guide = rules.price_guide(kind)
    # scenario tweaks (I8: test people have no sex or date of birth; every enquiry carries the 6.4 all-occupants declaration)
    rooms = _pick(r, [["Own room"], ["Own room", "Share a room with one other person"], ["Share a room with one other person"],
                      ["Share a room with one other person", "Share a room with two others"], ["Own room", "Share a room with one other person", "Share a room with two others"]])
    party, party_size, members = "Alone", 1, []
    couples_answer, couples = None, []  # 3.10 / I16(f): only for "With friends"
    if scenario == "couple":
        party, party_size = "Couple sharing one bed", 2
        rooms = ["Couple sharing one bed"] + ([] if r.random() < 0.6 else ["Share a room with one other person"])
        members = [{"name": f"{_pick(r, FIRST[country])} TEST-Partner", "email": f"partner{i}@test.mmai.invalid", "birth_country_raw": country}]  # I16(b)
    elif scenario in ("friends", "friends_couple"):
        party, party_size = "With friends", (r.randint(2, 3) if scenario == "friends" else r.randint(3, 4))
        rooms = ["Own room", "Share a room with one other person"]
        couples_answer = "No"
        if scenario == "friends_couple":  # I16(f): the enquirer states the couple in 3.10 (enquirer + Person 2, or Persons 2 and 3)
            couples_answer, couples = "Yes", [_pick(r, [["lead", "m1"], ["m1", "m2"]])]
            rooms = ["Couple sharing one bed", "Own room", "Share a room with one other person"]
        members = [{"name": f"{_pick(r, FIRST[country])} TEST-Friend{k}", "email": f"friend{i}_{k}@test.mmai.invalid",
                    "birth_country_raw": _pick(r, [country, country, country, "Australia", "Germany"])} for k in range(1, party_size)]  # I16(b): friends may be born elsewhere
    sizes = _pick(r, [["2 bedrooms", "3 bedrooms"], ["Studio", "1 bedroom", "2 bedrooms"], ["2 bedrooms"], ["3 bedrooms"], ["1 bedroom", "2 bedrooms", "3 bedrooms"], SIZE_ALL])
    if party == "Couple sharing one bed":
        sizes = _pick(r, [["Studio", "1 bedroom"], ["Studio", "1 bedroom", "2 bedrooms"], ["2 bedrooms", "3 bedrooms"]])
    # budget option consistent with rooms + sizes
    occ = "single" if rooms == ["Own room"] or party == "Couple sharing one bed" else ("triple" if "Share a room with two others" in rooms and r.random() < 0.5 else "twin")
    code_pref = {"Studio": "Studio", "1 bedroom": "1BR", "2 bedrooms": _pick(r, ["2B1B", "2B2B"]), "3 bedrooms": "3B"}[sizes[-1]]
    opts = [o for o in guide if o["type_code"] == code_pref and o["occupancy"] == occ] or [o for o in guide if o["type_code"] == code_pref]
    budget = _pick(r, opts)["id"]
    smoke = "Yes, outside only (balcony or courtyard)" if scenario == "smoker" or r.random() < 0.12 else "No"
    smoke_ok = "Yes" if smoke != "No" or r.random() < 0.55 else "No"
    bunk = "No" if scenario == "bunk_no" or r.random() < 0.6 else "Yes"
    # launch scope (8 Nov 2026): no 5.3/5.4 origin-preference answers are generated (the questions are retired, like sex/dob)
    doing = _pick(r, ["Studying"] * 7 + ["Working holiday", "Working", "Other"])
    school = _pick(r, INSTITUTIONS[:-1])["name"] if doing == "Studying" else None
    lvl = _pick(r, ["English", "Diploma", "Bachelor", "Master's", "PhD", "Exchange"])
    typed_country = _pick(r, TYPED[country]) if country in TYPED else country
    if scenario == "typo_country":
        typed_country = {"Sweden": "Sweeden", "Germany": "Germny", "Vietnam": "Veitnam"}.get(country, country[:-1] + "x")
    first_choice = rooms[0]
    mate_ideal = r.randint(1, 4)
    a = dict(
        first_name=name, last_name=f"TEST-{i:03d}", email=f"test{i:03d}@test.mmai.invalid", phone_code="+61", phone=f"04{r.randint(10000000, 99999999)}",
        contact_method=_pick(r, ["Email", "WhatsApp", "Phone", "WeChat"]),
        birth_country_raw=typed_country, live_country_raw=_pick(r, [country, country, "Australia"]), languages=LANGS[country] + (["Other"] if r.random() < 0.05 else []),
        city="Melbourne", move_in=move_in.isoformat(), move_in_flex=_pick(r, ["Exact date", "± 1 week", "± 1 week", "± 2 weeks", "± 1 month"]),
        move_out=move_out.isoformat(), move_out_flex=_pick(r, ["Exact date", "± 1 week", "± 2 weeks", "± 1 month", "Open to extending"]),
        budget_option=budget, apt_sizes=sizes, mate_ideal=mate_ideal, mate_max=min(6, mate_ideal + r.randint(0, 3)), rooms=rooms, bunk_ok=bunk, room_first=first_choice,
        party=party, party_size=party_size, party_members=members, couples_answer=couples_answer, couples=couples, doing=doing, school=school, course=_pick(r, ["Business", "Engineering", "Nursing", "IT", "Design", "Law", "Arts"]) if doing == "Studying" else None,
        level=lvl if doing == "Studying" else None, course_start=(move_in + timedelta(days=14)).isoformat() if doing == "Studying" else None,
        course_end=(move_out - timedelta(days=14)).isoformat() if doing == "Studying" else None, work=_pick(r, ["Hospitality, CBD", "Not yet known", "Retail, South Yarra"]) if doing.startswith("Work") else None,
        areas=_pick(r, [[], ["Melbourne CBD"], ["South Yarra"], ["Melbourne CBD", "South Yarra"], ["No preference"]]), travel=_pick(r, ["5 minutes", "15 minutes", "30 minutes", "45 minutes", "60+ minutes", None]),
        smoke=smoke, smoke_ok=smoke_ok,
        routine=_pick(r, ["Early riser", "Night owl", "Varies", None]), tidy=_pick(r, ["Very tidy", "Average", "Relaxed", None]), social=_pick(r, ["Quiet and private", "Friendly but independent", "Very social", None]),
        guests=_pick(r, ["Rarely", "Sometimes", "Often", None]), diet=_pick(r, ["None", "None", "Vegetarian", "Vegan", "Halal", None]),
        else_text=_pick(r, ELSE), intro=_pick(r, INTROS).format(n=name, c=country, lvl=lvl, school=(school or "work").split(" –")[0], m=move_in.strftime("%B")),
        source=_pick(r, ["Facebook", "Instagram", "Google", "Flatmates", "Education agent", "Hostel", "Friend or referral", "ALM website"]),
        consent_store=True, consent_share=r.random() < 0.92, adults_confirm=True, consent_marketing=r.random() < 0.5,
    )
    if not a["consent_share"]:
        a["consent_share"] = True  # 6.3 is required on the form; we keep a few 'share declined' via staff edit below
        a["_share_declined"] = True
    return a


SIZE_ALL = ["Studio", "1 bedroom", "2 bedrooms", "3 bedrooms"]


def _listed_date(v) -> date | None:
    """An apartment's stored availability as a date, or None when the cell is not a usable ISO date (the seed just skips it)."""
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def seed_test_data(c, n: int = 180, seed: int = 20261023, today: date | None = None) -> dict:
    today = today or lease_dates.business_today()   # the same business date the matching engine counts A17 vacancy from
    r = random.Random(seed)
    listed = sorted({_listed_date(x[0]) for x in c.execute("SELECT available_from FROM apartments WHERE available_from IS NOT NULL").fetchall()} - {None})
    listed = [d for d in listed if d >= today - timedelta(days=90)]   # long-vacant rows are stale listings, not a move-in season
    scenarios = (["plain"] * 110 + ["couple"] * 14 + ["friends"] * 10 + ["friends_couple"] * 4 + ["bunk_no"] * 10 + ["smoker"] * 10 + ["plain"] * 6 + ["janfeb_july"] * 12 + ["plain"] * 12 + ["typo_country"] * 6)
    r.shuffle(scenarios)
    created, failed = 0, []
    for i, sc in enumerate(scenarios[:n], start=1):
        a = build_enquiry(r, i, sc, today, listed)
        share_declined = a.pop("_share_declined", False)
        channel = _pick(r, ["Landing page", "Landing page", "Landing page", "Emailed form", "Phone"])
        created_at = (today - timedelta(days=r.randint(0, 60))).isoformat() + "T09:00:00+00:00"
        try:
            e = create_enquiry(c, a, channel, staff="Reservations 1" if channel == "Phone" else None, is_test=True, use_ai_countries=False, created_at=created_at)
            created += 1
            if share_declined:
                c.execute("UPDATE enquiries SET consent_share=0 WHERE id=?", (e["id"],))
        except Exception as ex:
            failed.append({"i": i, "scenario": sc, "error": getattr(ex, "errors", str(ex))})
    # a few old TEST records to demonstrate the T5 purge
    for k in range(3):
        a = build_enquiry(r, 900 + k, "plain", today, listed)
        a.pop("_share_declined", None)
        try:
            e = create_enquiry(c, a, "Landing page", is_test=True, use_ai_countries=False, created_at=(today - timedelta(days=400)).isoformat() + "T09:00:00+00:00")
            created += 1
        except Exception as ex:
            failed.append({"i": 900 + k, "error": getattr(ex, "errors", str(ex))})
    c.commit()
    return {"created": created, "failed": failed}


class TestDataClearRefused(Exception):
    """G-SL2 (11 Nov 2026): the test-data clear found a contradiction between test flags and ownership (a group of mixed TEST and
    REAL members, or a REAL-flagged row attached to a TEST record and vice versa) and REFUSED before writing anything. A plain
    exception, deliberately NOT a RetentionGuardError: it is a data finding, not a guard failure. The message carries counts only."""


def _census(c, ids: list[int], hb_ids: list[int]) -> tuple[dict, int]:
    """Reads only. Returns ({X1..X7 counts}, unrelated REAL orphan rows). X* > 0 = the clear would act on a row whose flag and
    ownership disagree; orphans = REAL-flagged rows whose enquiry no longer exists (not test data: left unchanged, counted)."""
    from .submissions import HOMEBOT_SCOPE
    def n(sql, args=()):
        return int(c.execute(sql, args).fetchone()[0] or 0)
    def inlist(vals):
        vals = list(vals)
        return ("(" + ",".join("?" * len(vals)) + ")", vals) if vals else ("(NULL)", [])
    ph_ids, a_ids = inlist(ids)
    ph_hb, a_hb = inlist(hb_ids)
    x = {}
    x["X1"] = n("""SELECT COUNT(*) FROM groups g
                   WHERE EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test=1)
                     AND EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test<>1)""")
    x["X2"] = n(f"SELECT COUNT(*) FROM mate_links WHERE (is_test=0 AND enquiry_id IN {ph_ids}) OR (is_test=1 AND enquiry_id IN (SELECT id FROM enquiries WHERE is_test=0))", a_ids)
    x["X3"] = n(f"""SELECT COUNT(*) FROM mate_requests WHERE (is_test=1 AND (root_enquiry_id IN (SELECT id FROM enquiries WHERE is_test=0) OR friend_enquiry_id IN (SELECT id FROM enquiries WHERE is_test=0)))
                    OR (is_test=0 AND (root_enquiry_id IN {ph_ids} OR friend_enquiry_id IN {ph_ids}))""", a_ids + a_ids)
    x["X4"] = (n(f"""SELECT COUNT(*) FROM enquiry_submissions WHERE scope<>? AND ((is_test=0 AND enquiry_id IN {ph_ids})
                     OR (is_test=1 AND enquiry_id IN (SELECT id FROM enquiries WHERE is_test=0)))""", [HOMEBOT_SCOPE] + a_ids)
               + n(f"""SELECT COUNT(*) FROM enquiry_submissions WHERE scope=? AND ((is_test=0 AND enquiry_id IN {ph_hb})
                       OR (is_test=1 AND enquiry_id IN (SELECT id FROM homebot_enquiries WHERE is_test=0)))""", [HOMEBOT_SCOPE] + a_hb))
    x["X5"] = n(f"SELECT COUNT(*) FROM internal_alert_outbox WHERE is_test=0 AND record_type='homebot_enquiry' AND record_id IN {ph_hb}", a_hb)
    # Count each authority once even when several ownership checks disagree.
    # Unknown legacy provenance is a refusal, not an inferred TEST classification.
    x["X6"] = n("""SELECT COUNT(*) FROM exit_exceptions x WHERE
        x.is_test IS NULL OR x.is_test NOT IN (0,1)
        OR NOT ((x.bot='matebot' AND x.group_id IS NOT NULL AND x.homebot_enquiry_id IS NULL)
             OR (x.bot='homebot' AND x.group_id IS NULL AND x.homebot_enquiry_id IS NOT NULL))
        OR (x.bot='matebot' AND (
            NOT EXISTS (SELECT 1 FROM exit_exception_members m WHERE m.exception_id=x.id)
            OR EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id
                       WHERE gm.group_id=x.group_id AND e.is_test<>x.is_test)
            OR EXISTS (SELECT 1 FROM exit_exception_members m JOIN enquiries e ON e.id=m.enquiry_id
                       WHERE m.exception_id=x.id AND e.is_test<>x.is_test)
            OR (x.is_test=1 AND (
                NOT EXISTS (SELECT 1 FROM groups g WHERE g.id=x.group_id)
                OR NOT EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id
                               WHERE gm.group_id=x.group_id AND e.is_test=1)))))
        OR (x.bot='homebot' AND (
            EXISTS (SELECT 1 FROM exit_exception_members m WHERE m.exception_id=x.id)
            OR EXISTS (SELECT 1 FROM homebot_enquiries e WHERE e.id=x.homebot_enquiry_id AND e.is_test<>x.is_test)
            OR (x.is_test=1 AND NOT EXISTS (SELECT 1 FROM homebot_enquiries e WHERE e.id=x.homebot_enquiry_id))))""")
    x["X6"] += n("""SELECT COUNT(*) FROM exit_exception_members m
                    LEFT JOIN exit_exceptions x ON x.id=m.exception_id WHERE x.id IS NULL""")
    # HomeBot receipts inherit their dataset solely from the owner, with no
    # independent is_test flag. An orphan's ownership is unknown: refuse clearing.
    x["X7"] = n("""SELECT COUNT(*) FROM homebot_email_outbox o
                  LEFT JOIN homebot_enquiries e ON e.id=o.enquiry_id WHERE e.id IS NULL""")
    orphans = (n("SELECT COUNT(*) FROM mate_links WHERE is_test=0 AND enquiry_id NOT IN (SELECT id FROM enquiries)")
               + n("SELECT COUNT(*) FROM mate_requests WHERE is_test=0 AND (root_enquiry_id NOT IN (SELECT id FROM enquiries) OR friend_enquiry_id NOT IN (SELECT id FROM enquiries))")
               + n("SELECT COUNT(*) FROM enquiry_submissions WHERE is_test=0 AND enquiry_id IS NOT NULL AND scope<>? AND enquiry_id NOT IN (SELECT id FROM enquiries)", (HOMEBOT_SCOPE,))
               + n("SELECT COUNT(*) FROM enquiry_submissions WHERE is_test=0 AND enquiry_id IS NOT NULL AND scope=? AND enquiry_id NOT IN (SELECT id FROM homebot_enquiries)", (HOMEBOT_SCOPE,))
               + n("SELECT COUNT(*) FROM internal_alert_outbox WHERE is_test=0 AND record_type='homebot_enquiry' AND record_id NOT IN (SELECT id FROM homebot_enquiries)")
               + n("""SELECT COUNT(*) FROM exit_exceptions x WHERE x.is_test=0 AND (
                    NOT EXISTS (SELECT 1 FROM apartments a WHERE a.id=x.apartment_id)
                    OR (x.bot='matebot' AND NOT EXISTS (SELECT 1 FROM groups g WHERE g.id=x.group_id))
                    OR (x.bot='homebot' AND NOT EXISTS (SELECT 1 FROM homebot_enquiries e WHERE e.id=x.homebot_enquiry_id)))"""))
    return x, orphans


def delete_test_data(c, staff: str | None = None) -> int:
    """Clear TEST data only, as ONE retention-guard step (G-SL2, 11 Nov 2026): capture (reads) -> contradiction census (reads) ->
    refuse with counts if any flag/ownership contradiction exists (nothing written) -> TEST-only deletions through the one group
    deletion helper -> explicit-id tombstones for both bots -> one audit row with actor and counts in the same transaction.
    REAL rows, including REAL-flagged orphans whose enquiry is gone, are never deleted or tombstoned here. Returns the number of
    TEST MateBot enquiries removed (unchanged contract)."""
    from . import db as dbm, homebot, stock_lifecycle
    from .db import audit
    with dbm.retention_guard(c, label="delete test data"):
        ids = [int(r_["id"]) for r_ in c.execute("SELECT id FROM enquiries WHERE is_test=1 ORDER BY id").fetchall()]
        hb_ids = [int(r_["id"]) for r_ in c.execute("SELECT id FROM homebot_enquiries WHERE is_test=1 ORDER BY id").fetchall()]
        # groups with at least one TEST member and no non-TEST member (memberless or unrelated groups are not test data)
        gids = [int(r_["id"]) for r_ in c.execute("""SELECT g.id FROM groups g
                    WHERE EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test=1)
                      AND NOT EXISTS (SELECT 1 FROM group_members gm JOIN enquiries e ON e.id=gm.enquiry_id WHERE gm.group_id=g.id AND e.is_test<>1)
                    ORDER BY g.id""").fetchall()]
        x, orphans = _census(c, ids, hb_ids)
        total = sum(x.values())
        if total:
            raise TestDataClearRefused(f"test data not cleared: {total} contradiction(s) between test flags and ownership "
                                       f"(groups {x['X1']}, links {x['X2']}, requests {x['X3']}, submission keys {x['X4']}, alerts {x['X5']}, exceptions {x['X6']}, receipt owners {x['X7']}); resolve them first")
        exception_count = int(c.execute("SELECT COUNT(*) FROM exit_exceptions WHERE is_test=1").fetchone()[0])
        stock_lifecycle.delete_groups(c, gids)
        c.execute("DELETE FROM enquiries WHERE is_test=1")
        c.execute("DELETE FROM mate_requests WHERE is_test=1")
        c.execute("DELETE FROM mate_links WHERE is_test=1")
        c.execute("DELETE FROM form_drafts WHERE is_test=1")
        submissions.tombstone(c, ids)  # submission keys of the deleted test enquiries keep only an opaque tombstone (reply and hash cleared)
        hb_done = homebot.delete_test_data(c)  # H14: TEST HomeBot enquiries (separate tables) and their keys go in the same clear
        if sorted(hb_done) != hb_ids:
            raise TestDataClearRefused(f"test data not cleared: HomeBot test set changed during the clear ({len(hb_done)} vs {len(hb_ids)})")   # guard rolls back everything
        if c.execute("SELECT COUNT(*) FROM exit_exceptions WHERE is_test=1").fetchone()[0]:
            raise TestDataClearRefused("test data not cleared: exit exception cascade was incomplete; nothing was cleared")
        audit(c, staff, "delete_test_data", None, None,
              f"{len(ids)} MateBot and {len(hb_ids)} HomeBot test enquiries, {len(gids)} groups cleared; "
              f"{exception_count} exit exceptions cleared; {orphans} unrelated real orphan rows left unchanged")
        return len(ids)
