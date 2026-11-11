# Browser QA scripts (5 November 2026, I19(m)/I19(p)/I20)

Playwright (Chromium) checks run against an ISOLATED local backend with a fresh TEST database (never the shared port 8000;
the parent QA server used port 8035). They create TEST enquiries only.

```
export MMAI_QA_PASSWORD=...   # the isolated server's staff password for user "bill"
python3 tools/qa/qa_i19m.py                 # public: couple-of-minutes wording, Submit label, green return action, 1280/375
python3 tools/qa/qa_public_change_i19m.py   # public: Change -> screen -> return -> Submit, one POST, no staff email UI
python3 tools/qa/qa_adults_i11d.py          # 8 Nov 2026 (I8 / I11(d)): no sex/DOB/age/guardian controls anywhere; ONE all-occupants 18+ declaration
                                            # (adults_confirm boolean), renewal on identity/move-in/party change, staff card + strict PATCH,
                                            # list filter/badge, phone form + email copy, never-declared legacy record. Env: MMAI_QA_BASE,
                                            # MMAI_QA_PASSWORD, optional MMAI_QA_DB (sqlite path of the disposable server), MMAI_QA_SHOTS
python3 tools/qa/qa_features.py             # 8 Nov 2026 (apartment and building features 3.11/3.12; origin questions 5.3/5.4 retired): 26 screens,
                                            # 20 labels in 4 groups = server catalogue, "matter most" cap 3 + prune, collect()/POST lists, no origin
                                            # controls; HomeBot extras/extras_top; staff detail rows + edit chips (4-of-3 refused), apartment
                                            # Unknown/Yes/No editor + parking charge, export .xlsx (31 columns), template upload (103 rows, no
                                            # errors, staff values kept), HomeBot staff detail. Env: MMAI_QA_BASE (default 8078), MMAI_QA_PASSWORD
python3 tools/qa/qa_intro.py                # 8 Nov 2026 (sex / current age for INTRODUCTIONS ONLY): "aboutyou" screen 1.6/1.7 with purpose notice,
                                            # Another term reveal, per-party-member controls, intro-only edit keeps the 18+ declaration,
                                            # collect()/POST keys (no retired dob/sex/pref keys), staff Introduction details card + member
                                            # edits via exact PATCH bodies, "Not yet given by" flag, plain wording. Env: MMAI_QA_BASE (default
                                            # 8078), MMAI_QA_PASSWORD, MMAI_QA_SHOTS
python3 tools/qa/qa_t6_staff.py             # 8 Nov 2026 (T6 staff readiness / introduction guards, T5 history; T10 plain presentation: G1 seeded with U1/U2 + "under A16" + compared-with text, G5 reviewed-no-draft): self-contained - seeds a
                                            # throw-away SQLite file with four groups through the real API and the test suite's AI doubles
                                            # (no model), starts its own server on 8078, then checks: ready filter/counter vs the server's
                                            # predicate, current draft (copy/email to every person's own address, editable), stale draft
                                            # (banner, copy/email/save disabled, history kept, redraft offered), earlier round named from
                                            # its immutable ref map, T5 deletion of a replaced-out member (brief/review/intro/allocation
                                            # name gone, group kept), legacy rows "not named here", 375 px overflow, no rule codes. Env:
                                            # MMAI_QA_PORT (default 8078, never 8079), MMAI_QA_DB, MMAI_QA_SHOTS
python3 tools/qa/qa_staff_i19p.py           # staff: summary, Change/return, all-section validation, one POST, email preview from the
                                            # SAVED record (409 replay shows original answers), reopen = 0 POST, Start again clears,
                                            # pending GET + restart, GET 500, malformed answers_json -> recoverable error
```
Base URL is `http://127.0.0.1:8035/` inside the scripts; edit `BASE` for another isolated port.
