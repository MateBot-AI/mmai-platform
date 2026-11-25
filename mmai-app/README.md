# MateBot AI and HomeBot

Two enquiry frontends share one FastAPI backend and staff workspace. The MMAI Rule Register governs the software; this source package is not a production deployment or release approval.

## Package layout

- **`mmai-app/client/`:** MateBot public form, phone-entry form and staff workspace.
- **`homebot-app/`:** combined chooser and HomeBot whole-apartment enquiry form, served at `/homebot/`.
- **`mmai-app/server/`:** eligibility, group generation, evidence-validated AI review, staff briefs, introductions, HomeBot shortlists and notification outboxes.
- **`mmai-app/tests/` and `tools/`:** regression checks, sanitised export and isolated recovery tools.
- **`mmai-app/data/`:** dated reference apartment workbooks, not confirmation of current availability.

## Build and controlled startup

Read `DEPLOY_DIGITALOCEAN.md`, `RELEASE_OPERATIONS.md` and `NOTIFICATION_OPERATIONS.md` before preparing a host. Export with `python mmai-app/tools/export_production.py --root PACKAGE --destination NEW_DIRECTORY`, then build from the exported repository root with `docker build -f mmai-app/Dockerfile .`.

Keep scheduling, AI queue, HomeBot matching, routing and all outgoing delivery off during preparation. The App Platform spec has disabled switches and no secret values. Resource creation, external recipients, live processing and publication require their existing approvals.

Use `claude-sonnet-5-5` with the authorised ALM credential only. Invalid or unavailable AI results stay held; a rules-only candidate is not a ready match. Staff review introductions and communicate with customers using their existing tools.

## Forms and records

MateBot serves at `/`; the combined chooser and HomeBot serve at `/homebot/`. Self-hosted handoffs stay on that shared origin so saved-form tokens reach the same backend. HomeBot review mode defaults on and marks its submissions TEST. Explicit TEST links remain separate from real records.

Google Ads tracking: `client/tracking.js` keeps the click id and campaign names an enquirer arrives with (`gclid`, `gbraid`, `wbraid`, `utm_campaign`, `utm_ad_group`, `utm_keyword`; this browser only, 90 days) and both public forms send them as one extra `tracking` object. The server cleans them (`server/tracking.py`), saves them in nullable columns on `enquiries` and `homebot_enquiries`, and staff see them read-only; they take no part in matching and are never shown to the public. `client/gtm.js` loads Google Tag Manager on the public pages only (never the staff workspace) and each form pushes one `alm_bot_complete` event to `dataLayer` after a saved public enquiry. The Content-Security-Policy in `server/security.py` and the meta tag in `client/index.html` allow only the Google hosts Tag Manager needs; keep the two in step.

Public submission receipts and internal attention alerts use separate persisted outboxes. Off/held, dry-run, attempted and received are different outcomes. A phone-copy preview remains staff-led and does not establish automatic email delivery.

Matching uses only the factors Bill has approved (Rule Register M10–M13, latest 23 November 2026): the housemate sex preference "Same sex only" is a hard filter on Female/Male, and age range, culture/language interest and languages are soft preferences that only raise a score. Sex (multi-select) and current age are asked of everyone. Missing features or unmeasured travel are not confirmed facts; essential needs and unresolved journeys remain visible for staff review.

## Verification and operations

Run regression tests against isolated synthetic SQLite and PostgreSQL databases, with workers and external services disabled unless a particular controlled live test is authorised. Preserve first outcomes, source manifests and test-run bindings; passing component tests alone does not establish full acceptance.

Do not upload secrets, customer records, local databases, private model-output evidence or preview account material. Bootstrap tests create synthetic accounts. Restore and rollback drills must use an isolated database; never automatically redeliver ambiguous outbox rows after recovery.

The detailed operational guides identify Anna's account/inventory/recipient tasks and Bill's purchase/release gates. Current source-test results belong in the engineering checkpoint, not in historical claims in this README.
