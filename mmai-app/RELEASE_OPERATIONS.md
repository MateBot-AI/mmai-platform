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

`MMAI_TRAVEL_AGENT_TOOLS=1` with `GOOGLE_MAPS_AGENT_TOOLS_API_KEY` set turns on the
live travel check, which uses Google Maps Experience Agent Tools (its `computeRoutes`
call, Essentials pay-as-you-go). When a staff member opens an enquiry's options (or
presses "Check travel times now"), the server asks for journey times for the
apartments still in the running, applies the travel rules in memory and returns the
re-assessed options for that one page view. It needs `MMAI_HOMEBOT_MATCHING` on and
current, in-date stored options. It is off by default and has not been switched on or
tested against the live service.

The earlier switch `MMAI_ROUTING_LIVE` and the key `GOOGLE_ROUTES_API_KEY` no longer
turn anything on in this path. Remove `MMAI_ROUTING_LIVE` from the host's settings
when the new switches are entered; a Routes API key must not be reused as the Agent
Tools key.

- Three ways of travelling are offered on the HomeBot form and requested from the
  service: walking, bicycle and public transport. Public transport is one combined
  choice (the service does not say which vehicles a journey uses). No car journey is
  ever requested, and the sender refuses any request that names one.
- Each request carries five fields only: origin address, destination address, travel
  mode, alternative routes and the departure time. No traffic-aware option, route
  line, toll, fuel or other dearer feature is asked for, and the sender refuses a
  request carrying any other field.
- Answers given on earlier versions of the form are kept exactly as given. Tram, Train,
  Bus and Car are never requested and never read as "Public transport"; "Bike or
  scooter" is measured as a bicycle journey, as it was before. A party holding Tram,
  Train, Bus or Car reads "Travel time needs confirmation" on every option until a
  staff member, having heard from the applicant, changes the answer in "Change the
  party's requirements". Those answers cannot be added to any enquiry.
- Nothing from the service, and nothing worked out from it, is stored, cached between
  requests or logged, with the one exception set out under "What that step writes"
  below. The stored options, their history, the saved AI review packet
  and the decline records hold no journey content and keep reading "Travel time needs
  confirmation". The only record is an audit row holding this application's own
  request counts, written before each stage of a check is sent: one row normally, a
  second when a shortlisted apartment failed the travel check and its replacement had
  to be measured. The row names no apartment, person, journey or result.
- Sources. Each journey answer carries a source title and link from the service. The
  page shows, for each option, "Sources: Google Maps" followed by those links, straight
  after the AI's assessment for that option, without a further click. When there is
  no AI assessment the same sources line still shows with that option's journey figures. "Google Maps" is plain text, never
  translated, wrapped or restyled. A link is shown only if it is an https address on a
  Google host; a journey whose link is anything else is treated as not measured, and
  a journey the service sends with no link at all is shown with "Sources: Google Maps"
  alone. The
  Google Maps logo (the unaltered file) remains at the head of the live options and
  beside the per-person figures, labelled "Estimated weekday departure at 08:00
  Melbourne; not a guaranteed journey time". No map is shown.
- Walking. Whenever a walking journey is shown, the page shows the service's own
  notice: "WALK routes are in beta and might sometimes be missing clear sidewalks or
  pedestrian paths."
- Request limits (each has a safe default and falls back to it if unreadable):
  `MMAI_ROUTING_LIVE_MAX_REQUESTS` per check (200), `MMAI_ROUTING_LIVE_DAILY_REQUESTS`
  per UTC day (1000), `MMAI_ROUTING_LIVE_MONTHLY_REQUESTS` per UTC month (4000). The
  names are unchanged from the earlier build. When a limit is reached no request is
  made and travel reads as needing confirmation. The count for a check is recorded
  before its first request is sent. The limits are enforced by this application from
  its audit records; also set a daily quota for the service in the provider console as
  a second stop.
- The check runs by itself when the options are opened, once per list per page; a
  page reload runs it again. `MMAI_ROUTING_LIVE_AUTO=0` keeps the button but stops the
  check running by itself.
- Cost basis, from the provider's published
  [overview](https://developers.google.com/maps/ai/maps-experience-agent-tools) (read
  16 November 2026): Essentials requests are free up to 10,000 a month and US$7.00 per
  1,000 after that. The monthly default of 4,000 is below the free amount, provided
  nothing else on the same billing account uses it. This reading has not been
  confirmed against a billing report; check the first month's report before raising
  the limit. The approved allowance for this service is A$200 a month.
- A decline recorded for an apartment that was offered from the live view keeps the
  saved details for that apartment (travel time needing confirmation), never the live
  journey times.
- Journey times held in the open page are dropped after 30 minutes, whether or not
  the page is opened again.
- This switch is independent of `MMAI_ROUTING` and the rights file above, which stay
  off: they govern stored journey evidence and its use by the saved AI review, and no
  clearance for that exists. That stored path was built for the earlier form choices
  and does not request "Public transport" or "Bicycle" journeys at all.
- Travel is checked last (approved 16 November 2026). Apartments are chosen and
  ordered on dates, layout, price and features only; a journey time plays no part in
  that order. Journeys are then measured for the shortlisted apartments only, at most
  three. One that fails (the closest majority's average is over double the stated
  time) is replaced by the next apartment in the order and the replacement is
  measured; the other shortlisted apartments are kept. An unknown journey is not a
  failure: that apartment stays and reads "Travel time needs confirmation". The list
  is never longer than three and is not padded; alternatives (different area, one
  bathroom) appear only when fewer than three full matches remain, inside the same
  maximum, and are travel-checked the same way. There is no fourth option. Apartments
  that were not shortlisted are not measured and say so. Apartments that could not be
  checked automatically are shown only into whatever room is left under the same
  maximum of three rows; any others are in the list of other apartments with the
  reason.
- All stages of one check share one 45-second time budget. A stage that would start
  after it is spent is not sent; those journeys read as needing confirmation.
- The saved options use the same order and the same maximum of three. Because nothing
  measured may be stored, the saved options never show a replacement: they keep
  reading "Travel time needs confirmation" and the live view shows the travel-checked
  result. Saved options prepared before this build are marked out of date and
  have to be prepared again.

### The AI's assessment of the travel-checked shortlist

`MMAI_TRAVEL_AI_REPLY=1` (in addition to the switches above) has the AI assess each
apartment in the travel-checked shortlist, using the journey results of the check just
made. It is off by default.

- It runs only when the call goes to ALM's own Anthropic account
  (`ANTHROPIC_API_KEY`, no proxy) and uses `claude-sonnet-5-5`. On any other route no
  call is made and the page says so.
- The model is given, for each shortlisted apartment: person 1, 2, 3; each person's
  fastest accepted journey in minutes and by which way of travelling; whether that is
  within that person's stated limit; the group result; and the apartment's other
  points already held with the saved options (full match or alternative, whole rent,
  days from the requested dates, empty days, what is still open). No names, addresses
  or places of work or study.
- Reading check. Every figure is worked out in code first. Per apartment the model
  reports how many people are within their own limit, over it and not known, which
  people, and the group result. Code checks each against its own figures.
- Judgement. Per apartment the model then gives a travel rating (good, acceptable or
  poor where the group result is within the doubled limit), the strengths and
  weaknesses it thinks matter most, chosen from a fixed list of statements, and its
  advice to staff; and one order of preference across the shortlist. These choices are
  the model's. Code does not make them. It refuses a reply whose choice is untrue for
  that apartment, leaves out something that must be flagged (an unknown journey, a
  person over their own limit, a majority average over the stated time, a long
  journey, an alternative), contradicts the group result, or contradicts itself. The
  advice line follows from what is listed and from the open points; an apartment with
  anyone over their own limit can never read "Suitable to offer".
  The model cannot pass an apartment that failed, rate an unknown journey, or advise
  offering an apartment that still has open points.
- No sentence written by the model is shown. Each sentence on the page is this
  application's own wording for a statement the model chose. An independent review of
  an earlier draft, which let the model add its own note, found wrong sentences that
  passed the checks; that note was removed.
- Any mismatch, missing field or extra field withholds the whole assessment and the
  page says so.
- Held unless assessed. The travel-checked shortlist is shown as "AI-assessed" only
  when the saved AI review finished with the verdict approve, that review covered
  and agreed with every apartment shown, and this assessment was produced, accepted
  and covers every apartment shown. In every other case (switch off, model
  unavailable, assessment withheld, nothing measured, saved review not complete,
  saved review ended in a hold, saved review did not agree with an apartment shown,
  an apartment shown that the saved review did not cover) the page shows "Held, not
  a ready shortlist" with the reason. The journey figures stay visible. There is no
  rules-only result.
- How a replacement is reviewed. The saved options now also name the reserves: up to
  three apartments next in the same order, which are the ones that step in when a
  shortlisted apartment fails the travel check. They are not shown as options. The
  saved AI review (review contract "homebot-review-v2") is given each reserve with
  the same recorded facts as a listed option (offer, cost, dates, bedding, features,
  open points) and must return a note for it; a reserve's journeys read as an open
  point because they have not been measured. A reserve does not change the review's
  verdict; the review records for each reserve whether it agrees. When a reserve
  steps in, it has therefore had the full review, and that check's assessment adds
  the journeys measured in that check, which are not stored. The saved review covers
  the listed options, the alternatives, the unresolved candidates and the reserves;
  an alternative it reviewed as an alternative counts as reviewed if it steps in.
- A replacement from beyond the reviewed reserves. When a check needs a replacement
  the saved review has not covered, and that is the only thing in the way, the
  result of that check is held and the next three candidates in the same order are
  added to the saved options' reserves and handed to the same review queue. The
  review is bound to the reserves, so it is made again for the options with the
  longer list (one model call by the review worker, none inside the check). While
  it is outstanding the page says the review has not completed; once it is
  complete, a check of the same options can read "AI-assessed". One batch per
  check, never while a review is outstanding, and at most four batches (twelve
  reserves) for one set of options; after that the result stays held and the page
  says staff decide. If the hand-over fails, nothing is written and the next check
  asks again.
- What that step writes. The longer reserve list (candidates chosen and ordered
  before travel was looked at, with the same stored facts as every other candidate)
  and one audit row (action "homebot.reserve_review") holding the scenario, the
  options' number, how many reserves were added and how many there now are. It
  writes no journey, time, result or failed apartment. It does show that a check
  needed more candidates than had been reviewed; that is the one thing saved as a
  consequence of a travel check, and it should be weighed against the service's
  no-storage terms before the switch is set.
- What is still held. More than twelve replacements for one set of options. Options
  saved before this package hold no reserves; they are out of date under the new
  matching version and are rebuilt and reviewed again before use.
- System text. The saved review and the travel assessment are each sent under
  HomeBot's own system text. Before this package both went out under the MateBot
  system line, because the HomeBot text was defined and never passed on. MateBot's
  calls, their system line and the model are unchanged.
- A person whose journey is not fully measured is "not known": never counted as within
  a limit and never as over it. If no journey in the check is fully measured the model
  is not asked.
- "Within their own limit" (each person against the stated limit) and the group result
  (the closest majority's average against the doubled limit) are reported separately.
- The assessment is returned to the page once and is not stored, cached or logged. A
  failure is logged as a category only. The saved AI review never receives journey
  content.

### Conditions before either switch is set

None of these is established by this package; each needs its own evidence.

1. The Agent Tools key belongs to an eligible ALM billing account, is restricted to
   this service, and has a daily quota set.
2. For the AI reply: the retention and training settings of the Anthropic account
   used by `ANTHROPIC_API_KEY` have been checked by someone with access to that
   account against the service's condition that the model receiving its journey
   content does not cache, store or train on it
   ([Maps service terms, section 10](https://cloud.google.com/maps-platform/terms/maps-service-terms),
   [overview](https://developers.google.com/maps/ai/maps-experience-agent-tools)),
   and any retention that remains under the account's terms (for example content a
   provider keeps for safety review) has been put to Bill and accepted or resolved.
   This is a property of the account, not of this code, and has not been checked.
3. The public Privacy notice says that journey estimates are passed to the AI provider
   for this assessment, and that wording is approved. The notices in this package have
   not been changed for it.
4. A first controlled live check has been made and read: a Melbourne weekday 08:00
   public transport journey returns a time and a source link in the expected shape. A
   source link that is not an https address on a Google host would show as "not
   measured" and would need to be looked at before going further.

## Controlled startup

Keep `MMAI_SCHEDULER=0`, `MMAI_AI_QUEUE=0`, `MMAI_HOMEBOT_MATCHING=0`,
`MMAI_NOTIFICATIONS=0`, `MMAI_MAIL_MODE=off`, `MMAI_ALERT_MODE=off`,
`MMAI_ROUTING=0`, `MMAI_TRAVEL_AGENT_TOOLS=0` and `MMAI_TRAVEL_AI_REPLY=0` until the corresponding controlled acceptance is authorised.
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
