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
- Only a signed-in staff request can start a check. Submitting the public form never
  does. A test enquiry is refused before any request unless
  `MMAI_ROUTING_LIVE_TEST_DATA=1` (default off), which is for a controlled synthetic
  acceptance only; see the MateBot section below.
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
  twelve apartments next in the same order, which are the ones that step in when a
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
- The reserves are fixed before any journey is measured. They are taken from the
  order on the other points when the options are prepared, and reviewed with the
  options in the same single review. Nothing about the reserves is decided, added
  or written because of a journey result: a travel check does not change the saved
  options, their hand-over to the review or the review rows, and its only records
  are the count-only request rows. (An earlier build of this branch sent further
  reserves for review when a check needed them; that step, its audit action and
  its two hold reasons have been removed.)
- The bound. Twelve reserves for one set of options, never the whole apartment
  list, and never more than three options shown. A replacement from beyond the
  twelve has not been reviewed: the result of that check is held and the page says
  staff decide how to proceed. The saved options do not change.
- Size of the review. A full pool makes the review request about 69,000 characters
  (three options and twelve reserves, measured on test data) and its reply carries
  fifteen option notes. The output budget now follows the number of options:
  2,300 tokens plus 200 for each option (raised on 16 November 2026 from 2,000
  plus 150), never below the earlier fixed 1,800 and never above the 8,000
  ceiling; 5,300 for a full pool, and 7,950 for the one compact retry. It is
  sized so that the longest reply the validator accepts fits at three characters
  a token with two fifths to spare for a full pool (9,504 characters on test
  data) and at least a fifth for every smaller one. That figure is an engineering estimate; it has not
  been measured against the model, and neither have the time and input cost of a
  full-pool review: run one controlled review before relying on it. Every option, reserve or not, still needs its
  own valid note, and a reply cut off twice is recorded as held, not as a review.
  A larger request for every review is the cost of this design, whether or not a
  travel check ever needs a reserve.
- Options saved before this package hold no reserves; they are out of date under
  the new matching version and are rebuilt and reviewed again before use.
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

## MateBot groups: what is and is not done for travel

The live travel check now covers MateBot groups as well as HomeBot options. It
is built and tested with stand-in journey and model replies only. No provider
or model call has been made for it, nothing is switched on, and it has not been
run on the host. What remains open is listed at the end of this section and is
not to be reported as done.

Built in this package:

- **Distance is not used anywhere in ranking.** The rule score that orders a
  group's apartments has an areas part and no distance part. The straight-line
  distance to the place of study is no longer given to Claude in the group
  review or the staff summary, is no longer stored with a group's apartments,
  and is no longer shown on the group page, where it could be read as a measured
  journey. The review request now says that travel is not part of the ranking
  and that no distance is supplied. This changes the review contract
  (`2026-11-16.no-distance`): when the AI queue next runs, every approved group
  is queued for a fresh review, and until that review arrives the group is not
  shown as ready. Plan for those review calls when the queue is switched on.
- **An optional travel choices question** on the MateBot form (one nullable
  column, `enquiries.travel_modes`): Walk, Bicycle, Public transport. Nothing is
  pre-selected and no answer is stored as no answer. An enquiry saved before the
  question existed keeps no answer; none is inferred. The question wording is
  derived from the HomeBot form; only the three choices are Bill's. Once
  answered, the answer is part of the group's review evidence, so a later edit
  makes the saved review out of date like any other answer; an enquiry that
  never answered keeps its existing evidence unchanged.
- **Three at most.** A group still stores up to six ranked apartments. Staff see
  at most three as the shortlist (the current offer included); the others are
  reserves in their ranked order, with no offer button.
- **The live final travel check** (`server/matebot_live.py`, address
  `POST /api/groups/{id}/travel-check`, staff only; a signed-out request is
  refused). It runs when a staff member opens a group (when
  `MMAI_ROUTING_LIVE_AUTO` is on) and again on request. The automatic run
  happens once for an opened group on a page: it is not repeated after a
  failure, after the result is cleared from the page at 30 minutes, or while a
  check is already running. A new result needs the "Check again" button or a
  page reload. It uses the same switch, key, request shape, attribution and
  allowances as the HomeBot check; the per-check, per-day and per-month
  allowances (200, 1,000 and 4,000 requests) are one total shared by both, and
  one check runs at a time across both.
  - **Test records.** A group with any test member, and a HomeBot test enquiry,
    are refused before any request unless `MMAI_ROUTING_LIVE_TEST_DATA=1`
    (default off; the exact value `1` only). Anyone can make a test record from
    the public form, so this switch is turned on only for a controlled
    synthetic acceptance and turned off again afterwards. Submitting a form
    never sends a provider request for either bot.
  - People are counted, not enquiries: the enquirer and each further person in
    their party, each once.
  - A journey is requested only to a destination that was supplied and is an
    address: for a person who is studying, the verified address of the
    institution they chose; for a person who is working or on a working
    holiday, their place-of-work answer when the whole answer is one full
    street address and nothing else: an optional unit or level, a street
    number, a street name and type, a suburb, an optional state, and a
    street-address postcode (which must belong to the state when one is
    written). It is sent exactly as typed (spaces tidied); the stored answer is
    never changed.
    Each person's journeys are requested only by the travel choices given for
    them.
  - A destination that exists and cannot be measured is never completed or
    guessed: no place-of-work answer, "Not yet known", a suburb, an employer's
    name, an address without a postcode, a postcode that is not a
    street-address postcode or not the written state's, an address with any
    other words before or after it, a phone number, a second street, or a place
    of study that is not one on file. The check errs towards refusing: some
    real addresses with unusual street or suburb names are refused too.
  - Limit of the address check: the application holds no list of suburbs, so
    the one or two words written where the suburb goes are not checked to be a
    real suburb, nor the postcode to be that suburb's. Common business words
    there are refused; another business name written in place of the suburb
    would be accepted and sent with its street and postcode. That person reads "travel time needs confirmation", still counts as a
    person with a destination, and the group result is "needs confirmation".
  - A person who answered "Other" was not asked for a place of study or work,
    and a person in a party who is not the enquirer was never asked for one of
    their own. They have no destination and are not counted in the travel
    calculation. A party shares the enquirer's choices and travel time.
  - Travel is checked last. The stored apartments are taken in their stored
    order, the current offer first; the first three are checked together; one
    that fails is replaced by the next stored candidate, which is then checked.
    Never more than three are shown as the shortlist, and nothing is searched
    for beyond the stored candidates.
  - When every person states the same maximum travel time the H28 calculation
    applies as written. When the times differ (H28(j), approved 16 November
    2026): the closest strict majority is chosen by journey time, never by the
    size of anyone's stated time, and its average journey is compared with
    twice the average of those same people's stated times. The comparison is
    made on exact totals; a total exactly at the doubled figure passes. "60+
    minutes" counts as 60. Someone far away, or with a short stated time,
    who is outside that majority does not decide the result; they are warned
    about. A person is warned about when their journey is over double their
    own stated time or over the group's doubled figure; a warning never
    excludes an apartment. HomeBot's calculation is unchanged.
  - When any person in the group has not stated a maximum travel time, no
    figure is combined and no default is used: each person's journey is shown
    against their own stated time and the group result is "needs
    confirmation". Such an apartment keeps its place; it is neither passed nor
    failed.
  - People level on journey time at the edge of the closest majority. This
    case is not ruled; it is handled under the existing unknown-journey
    safeguard and is recorded in the register as a matter raised. Nobody is
    picked by position or by stated time. If the result would differ with who
    is counted, the group result is "needs confirmation". If it is the same
    whoever is counted, that result is given, the level people are named as
    tied rather than chosen, and the doubled figure is shown as its range when
    it is not one number.
  - Every result says who it covers. A result is worked out over the people
    with a destination. Others in a party were never asked for one, and some
    people give none; their travel is unverified and the page says so by
    position and name. A pass that leaves someone out is shown as a pass for
    the people measured, not for the whole group.
  - The apartment currently offered is "travel confirmed" only when this
    page's check gave a pass that covers every person and the AI's assessment
    of it was accepted. A known failure reads "failed travel". Everything else
    reads "provisional".
  - Claude's assessment (`server/matebot_travel_reply.py`, behind
    `MMAI_TRAVEL_AI_REPLY`, which stays off): one request per check for at most
    three apartments, and one shorter second request only if the first was cut
    off, so at most two; 1,200 output tokens (1,800 for the second); positions and
    figures only (no name,
    address, institution or destination). Code checks every count, every person
    named and the group result against its own figures, and withholds the whole
    reply on any difference. Claude cannot rate or advise offering an apartment
    whose group result needs confirmation.
  - A shortlist reads "AI-assessed" only when the group's saved review is
    current and approved, that review covered every apartment shown, and this
    check's assessment was accepted. Anything else reads "Held", with the
    reason, and the list is headed "Provisional shortlist (not confirmed)".
    Before a check, and when checks are off, the group's apartments carry a
    "Provisional" notice.
  - Nothing from the journey provider or from Claude is stored. The only record
    is a count of requests (`matebot.travel_check` in the audit log), written
    before they leave.
- **Hand-off from HomeBot.** Travel choices already ticked on the HomeBot form
  are carried into the MateBot saved form exactly as ticked, only the three
  current choices, and are reviewed on their own screen. The hand-off screen
  comes before HomeBot's travel question, so this applies only to someone who
  answered it and then went back to change their party. An earlier Tram, Train,
  Bus or Car answer is never carried or read as another choice.

Not done, and not to be reported as done:

- **Most work destinations will read "needs confirmation".** The MateBot form
  asks for "Suburb or employer, or 'Not yet known'", so a full street address
  is rarely given. Work journeys are measured only when one is. The form tip
  and the staff wording about this are engineering copy written to match the
  code; they are not wording Bill has approved. Asking for a
  work address is a change to the question, which is Bill's to make; it has not
  been made.
- **Party members** who are not the enquirer have no destination of their own
  on the MateBot form.
- **Secondary destinations** (a second place of work or study) are not asked on
  the MateBot form and are not measured.
- **A missing travel time** makes the whole group "needs confirmation". The
  travel time question is optional on the form, so many real groups will read
  that way until every member has answered it.
- **The result is not saved**, so no stored list can show it. What is done
  instead:
  - The groups list and the overview count come from the saved review. Their
    badge now reads "Ready for reservations · travel not confirmed" and is no
    longer green. The overview count and the list filter count the same groups
    as before and are labelled "Ready for reservations (travel not
    confirmed)".
  - On the group page the green "Ready to circulate." line appears only when
    the page's check has confirmed the current offer on travel. A known
    failure reads "Not ready to circulate" and switches off drafting, copying,
    emailing and Mark Introduced, and the Offer button of each apartment that
    failed. Anything else reads "Provisionally ready: travel not confirmed."
    with the reason. While checks are switched off, every ready group reads
    that way.
  - The server checks one apartment live, without Claude, at each staff action
    that presents it to the group: offering it, drafting the introduction for
    it, Mark Introduced, introducing a revised group, and acknowledging a
    changed offer. A known failure refuses the action and changes nothing. A
    group that changed while the provider was being asked, or between that
    check and the action's own step, is refused and must be reloaded. Every
    other outcome lets the action go on and is reported once, in words, as
    what it is: passed for everyone, passed for part of the group, needs
    confirmation, no destinations, or not checked (switched off, a test
    record, the allowance used, another check running). None of those is a
    failure and only the first is a pass. Each of these checks uses the same
    shared allowance and is recorded only as a count of requests.
  - Not gated: Group confirmed, recording a lease, introducing the remaining
    members after someone leaves, and the automatic moves after a stock
    change. Those record what has happened or are not a staff presentation of
    an apartment. Staff still write and send every message. Nobody is moved
    and no group is split because of a journey.
  - This cannot stop an email that staff sent before a failure was known. If
    Mark Introduced is refused after the introduction went out, the message
    says to tell the group the apartment is being changed.
- **The readiness, tie and coverage wording** on these screens is engineering
  copy written to match the code; it is not wording Bill has approved.
- **Not run against the provider or Claude.** The live check for groups has only
  been run with stand-in replies. One controlled check on the host is still
  needed, and Google journey content must not go to Claude until the retention
  question is settled.
- The public transport choice still cannot exclude a ferry or another vehicle
  the provider counts as transit; this is the same limit as for HomeBot.

## Controlled startup

Keep `MMAI_SCHEDULER=0`, `MMAI_AI_QUEUE=0`, `MMAI_HOMEBOT_MATCHING=0`,
`MMAI_NOTIFICATIONS=0`, `MMAI_MAIL_MODE=off`, `MMAI_ALERT_MODE=off`,
`MMAI_ROUTING=0`, `MMAI_TRAVEL_AGENT_TOOLS=0`, `MMAI_TRAVEL_AI_REPLY=0` and `MMAI_ROUTING_LIVE_TEST_DATA=0` until the corresponding controlled acceptance is authorised.
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
