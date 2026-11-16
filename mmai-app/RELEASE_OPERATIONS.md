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

## Live travel check (shown, not stored)

`MMAI_ROUTING_LIVE=1` with `GOOGLE_ROUTES_API_KEY` set turns on a separate, narrower
use of the same provider: when a staff member opens an enquiry's options (or presses
"Check travel times now"), the server asks for journey times for the apartments still
in the running, applies the travel rules in memory and returns the re-assessed options
for that one page view. It needs `MMAI_HOMEBOT_MATCHING` on and current, in-date
stored options. It is off by default and is not approved for use.

- Nothing from the provider, and nothing worked out from it, is stored, cached between
  requests, logged or passed to the AI review. The stored options, their history, the
  review packet and the decline records are unchanged and keep reading "Travel time
  needs confirmation". The only record is one audit row per check holding this
  application's own request counts.
- The page shows the provider's own Google Maps logo (the unaltered dark-grey file from
  its [attribution assets](https://developers.google.com/maps/documentation/routes/policies),
  18px high, labelled "Google Maps") at the head of the live options and beside each
  apartment's journey figures, with the label "Estimated weekday departure at 08:00
  Melbourne; not a guaranteed journey time". No map is shown.
- Request limits (each has a safe default and falls back to it if unreadable):
  `MMAI_ROUTING_LIVE_MAX_REQUESTS` per check (200), `MMAI_ROUTING_LIVE_DAILY_REQUESTS`
  per UTC day (1000), `MMAI_ROUTING_LIVE_MONTHLY_REQUESTS` per UTC month (4000). When
  a limit is reached no request is made and travel reads as needing confirmation.
  The count for a check is recorded before its first request is sent. The limits are
  enforced by this application from its audit records; the provider account has a
  budget alert only, so also set a daily quota for the Routes API in the provider
  console as a second stop.
- The check runs by itself when the options are opened, once per list per page; a
  page reload runs it again. `MMAI_ROUTING_LIVE_AUTO=0` keeps the button but stops the
  check running by itself.
- Cost basis, from the provider's published
  [price tiers](https://developers.google.com/maps/billing-and-pricing/sku-details/routes)
  and [price list](https://developers.google.com/maps/billing-and-pricing/pricing)
  (read 16 November 2026): each successful request is billed under one tier. The
  walking, bicycle, tram, train and bus requests sent here use none of the features the
  provider lists for its middle or highest tier, which by that list leaves the lowest
  tier (10,000 free requests a month). The monthly default of 4,000 is below that and
  below the middle tier's free allowance (5,000), so at the published prices the
  default should cost nothing, provided nothing else on the same billing account uses
  these tiers. This reading has not been confirmed against a billing report; check the
  first month's report before raising the limit.
- Journeys are requested for five ways of travelling only: walking, bicycle, tram,
  train and bus. No car journey is ever requested, by this check or by the stored
  journey path. A party that accepted Car keeps that answer: it is not swapped for
  another way of travelling, the car journey stays not measured, and every option for
  that party reads "Travel time needs confirmation", whether Car was the only answer or
  one of several. The HomeBot form still offers Car; removing it, or assessing such a
  party on its other answers alone, needs Bill's ruling.
- A decline recorded for an apartment that was offered from the live view keeps the
  saved details for that apartment (travel time needing confirmation), never the live
  journey times.
- Journey times held in the open page are dropped after 30 minutes, whether or not
  the page is opened again.
- This switch is independent of `MMAI_ROUTING` and the rights file above, which stay
  off: they govern stored journey evidence and its use by the AI review, and no
  clearance for that exists.

Contract position, stated as a risk and not as a permission. Storing or caching the
provider's journey results is outside its standard terms, which is why this path keeps
nothing. Whether working out an average, a pass or fail and an order from those
results inside a single request is allowed is not settled by the wording: the terms
say not to "create content based on Google Maps Content"
([Google Maps Platform Terms 3.2.3(c)](https://cloud.google.com/maps-platform/terms)).
An independent review on 16 November 2026 classed this as an unresolved, moderate
contract risk: not a clear breach and not cleared. Switching this on therefore needs
the business owner's decision to accept that risk, or a written answer from the
provider, or a different provider.

The site's published terms must also tell users that it uses Google Maps features and
content and that their use is subject to the Google Maps/Google Earth Additional Terms
of Service and the Google Privacy Policy, and the site needs a publicly accessible
Terms of Use and Privacy Policy
([Google Maps Platform Terms 3.2.2](https://cloud.google.com/maps-platform/terms),
[Routes API policies](https://developers.google.com/maps/documentation/routes/policies)).
That wording is the business owner's to approve; it is not supplied by this package
and no such pages exist in it.

## Controlled startup

Keep `MMAI_SCHEDULER=0`, `MMAI_AI_QUEUE=0`, `MMAI_HOMEBOT_MATCHING=0`,
`MMAI_NOTIFICATIONS=0`, `MMAI_MAIL_MODE=off`, `MMAI_ALERT_MODE=off`,
`MMAI_ROUTING=0` and `MMAI_ROUTING_LIVE=0` until the corresponding controlled acceptance is authorised.
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
