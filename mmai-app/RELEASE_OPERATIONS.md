# MMAI release operations

This source candidate is not a production deployment or a release approval. All
account, resource-purchase, recipient and public-release gates in the Rule Register
remain in force.

## Build and source hygiene

- Export: run `python mmai-app/tools/export_production.py --root PACKAGE --destination NEW_DIRECTORY`.
- Build from the exported repository root: `docker build -f mmai-app/Dockerfile .`.
- Both frontends are included. The backend serves HomeBot at `/homebot/`.
- Preview staff password hashes, SQLite databases, reports and runtime records are
  excluded. Bootstrap regression tests generate their own synthetic account seed.
- The dated apartment workbook is a reference fixture, not confirmation of today's
  stock. Anna must supply and verify current inventory before customer processing.
- The App Platform spec is a disabled preparation template, not an approved purchase
  or a verified quotation. Confirm configuration, Sydney availability, total cost,
  billing entity, DNS and access before applying it. Automatic deployment on push is off.

## Routing permissions and operation

The adapter is disabled by default. `MMAI_ROUTING=1` additionally requires
`MMAI_ROUTING_RIGHTS_FILE`, an external JSON document containing verified contract
evidence, not a public form checkbox. Required fields are `provider=google-routes`,
`verified=true`, a nonempty `evidence_reference`, timezone-qualified `valid_until`,
`derived_results=true`, `history_retention=enquiry-lifetime`, and both
`persist_fields` and `ai_fields` containing exactly `duration`, `mode`, `departure`,
and `attribution`. Also set `fresh_seconds` (60–86400) and
`max_requests_per_job` (1–1000). No such clearance is supplied with this package.

The standard terms prohibit caching except where expressly allowed and restrict
creating content; the AI example concerns improving models, not an explicit blanket
ban on inference ([Google Maps terms](https://cloud.google.com/maps-platform/terms)).
Routes-specific terms explicitly allow latitude/longitude caching but do not grant
the duration-history permission this workflow needs
([service-specific terms](https://cloud.google.com/maps-platform/terms/maps-service-terms)).
Obtain suitable written rights or choose a suitable licensed provider before reliance.
Do not fill the verification fields merely to bypass this gate.

Returned transit legs are checked, because requested transit modes are preferences,
not a guarantee that other modes will be excluded
([Routes transit documentation](https://developers.google.com/maps/documentation/routes/transit-route)).
Walking connections within a transit journey are retained. Bicycle routes represent
the bike part of “Bike or scooter”, not a claimed scooter route. Durations use exact
seconds, including recurring fractional minutes. Missing, unsupported or refused
journeys need confirmation, never a confirmed pass.

Each job has a request-count ceiling, no implicit retries, and a 60-second start
window. This is NOT a monthly spending cap. Configure provider quotas and billing
alerts and verify their behaviour against the separate A$200 allowance. Evidence
records Google Maps attribution, the Melbourne weekday 08:00 departure, policy
digest and expiry; changed or expired permission/estimates invalidate readiness.

## Controlled startup

Keep `MMAI_SCHEDULER=0`, `MMAI_AI_QUEUE=0`, `MMAI_HOMEBOT_MATCHING=0`,
`MMAI_NOTIFICATIONS=0`, `MMAI_MAIL_MODE=off`, `MMAI_ALERT_MODE=off` and
`MMAI_ROUTING=0` until the corresponding controlled acceptance is authorised.
Keep `MMAI_HOMEBOT_REVIEW_BUILD=1` during preparation. Self-hosted HomeBot links
to the same origin's MateBot form and forces its review submissions to TEST.
Set this flag to `0` only as part of the separately approved release; it does not
enable background processing or sending. Explicit query/hash TEST links still work.
Runtime model is only `claude-sonnet-5-5`, with the eligible ALM credential. No fallback
model or personal credential is permitted.

Use a single application instance with PostgreSQL. Enter first staff passwords
through the host's secret environment settings; never commit them. Check each
individual login and disable initial-password variables after provisioning.
Verify HTTPS, cookie/security headers, proxy handling, rate limits, frame origins,
staff/public separation and the configured public link base on the actual host.
Local tests do not establish these properties on DigitalOcean.

## Backup, isolated restore and rollback

Quiesce writes and disable all consumers first. Create an empty isolated PostgreSQL
database named `mmai_restore_...`; set `SOURCE_DATABASE_URL` and
`RESTORE_DATABASE_URL` securely, then run
`python mmai-app/tools/recovery_drill.py --output NEW_PRIVATE_DIRECTORY`.
The drill refuses a nonempty destination, compares every restored row and field,
and writes the dump digest and evidence without logging credential values.

Restart the restored database with the same disabled switches. Check staff login,
saved enquiries, shortlist history and every receipt/alert queue state. Never replay
`sending` rows: reconcile ambiguous sends with the provider as described in
`NOTIFICATION_OPERATIONS.md`. A restored backup is not permission to redeliver mail.

Retain the previous tested source artifact and its manifest. Roll back the application
only after checking backward schema compatibility against an isolated restored copy.
Do not overwrite production with a backup or erase newer submissions by assumption.
A production restore requires a separately reviewed recovery window and reconciliation
of records received after the backup. No destructive rollback command is supplied.

## Remaining external acceptance

Anna supplies current stock, account connections, mailbox administrator/type,
controlled recipients and staff testers. Received-message checks require actual
mailbox evidence; transport captures or API acceptance alone are insufficient.
Actual hosting and DNS checks, licensed routing access and final public release
remain separate from local engineering verification.
