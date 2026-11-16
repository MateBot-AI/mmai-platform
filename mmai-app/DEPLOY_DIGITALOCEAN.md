# MateBot and HomeBot: controlled DigitalOcean preparation

This guide replaces the historical preview-migration instructions. It prepares a private deployment; it does not authorise purchasing resources, publishing the forms, sending messages or processing customers.

## Existing accounts and source

- **Ownership:** MateBot AI is separate from ALM. Preserve the existing DigitalOcean team and existing communication services; do not create replacement accounts or move numbers.
- **Repository:** use the existing private `MateBot-AI/mmai-platform` repository. Reverify its state before uploading tested source, and do not change membership or permissions.
- **Package:** export sanitised source with `tools/export_production.py`. The repository root contains both `mmai-app/` and `homebot-app/`; build with `docker build -f mmai-app/Dockerfile .` from that root.
- **Excluded material:** never upload credentials, customer records, preview passwords, local databases or private runtime evidence. Do not migrate the preview database by default.

## Resource approval before creation

The `.do/app.yaml` file is a disabled preparation template, not a verified quote or purchase approval. Anna must confirm the correct company/team, Sydney region, proposed application and PostgreSQL sizes, billing currency, total cost, backup arrangements, DNS and access with Bill before creating anything.

The approved allowances are A$100/month hosting, A$100/month provisional AI and a separate A$200/month routing. They are not forecasts or provider-enforced caps. Obtain approval for the actual configuration and cost, and keep automatic deployment on push disabled.

### Verified preparation facts, 11 November 2026

App Platform lists Sydney as available for dynamic applications ([DigitalOcean availability](https://docs.digitalocean.com/products/app-platform/details/availability/)). The template's one `apps-s-1vcpu-1gb` application instance is US$12/month with 150 GiB outbound allowance; a 2 GiB instance is US$25/month with 200 GiB, and additional outbound traffic is US$0.02/GiB ([App Platform pricing](https://docs.digitalocean.com/products/app-platform/details/pricing/), [USD billing](https://docs.digitalocean.com/platform/billing/taxes/)).

A 1 GiB single-node PostgreSQL cluster starts at US$15/month, but is not highly available and is recommended by the provider for preliminary development/testing ([PostgreSQL pricing](https://docs.digitalocean.com/products/databases/postgresql/details/pricing/)). Thus the template-sized preparation baseline is US$27/month, or US$40/month with a 2 GiB application instance, before tax, currency conversion, overages or additional services; these are arithmetic estimates, not an account-specific quotation or an approved production configuration ([application prices](https://docs.digitalocean.com/products/app-platform/details/pricing/), [database prices](https://docs.digitalocean.com/products/databases/postgresql/details/pricing/)). Do not describe either single-node combination as highly available, promise it fits the A$100 allowance, or assume it has passed the hosted load/recovery checks.

The provider has also changed Standard Edition availability for new accounts from 15 October 2026 and for all accounts from 30 November 2026: new Standard Edition clusters cannot add standby/read-only nodes or use sizes above 4 GiB ([PostgreSQL pricing](https://docs.digitalocean.com/products/databases/postgresql/details/pricing/)). Verify the actual account's available edition, resilience options and total price with Anna and Bill; do not assume an old standby-node quote still applies.

The application specification attaches a separately provisioned managed database: `production: true` requires `cluster_name`; database-creation keys `size` and `num_nodes` do not belong in that attachment ([App specification](https://docs.digitalocean.com/products/app-platform/reference/app-spec/)). After separate resource approval, create or select the intended Sydney managed cluster, create an appropriately scoped application database/user, replace `replace-with-approved-sydney-cluster`, and select the actual database/user in the attachment configuration. Validate/propose the final specification in the authorised account and review its quoted cost before creating the app; the provider documents a proposal endpoint for this purpose ([App Platform pricing](https://docs.digitalocean.com/products/app-platform/details/pricing/)). No cluster, user, billing change or app is created by this preparation document.

## Initial private startup

Use one application instance and an isolated PostgreSQL database. Keep all these settings disabled:

```text
MMAI_SCHEDULER=0
MMAI_AI_QUEUE=0
MMAI_HOMEBOT_MATCHING=0
MMAI_NOTIFICATIONS=0
MMAI_MAIL_MODE=off
MMAI_ALERT_MODE=off
MMAI_ROUTING=0
MMAI_HOMEBOT_REVIEW_BUILD=1
```

Protect the preparation host from public access. Use synthetic records and explicit TEST links, not live customer information. HomeBot review mode marks its form and MateBot handoff as TEST; this is not a substitute for host access control.

Enter `DATABASE_URL` and initial staff passwords through the host's secret settings. Provision Bill and the named individual staff accounts using the bootstrap procedure; verify sign-in and remove initial-password variables afterward. Never put passwords or database URLs into source or reports.

The only approved runtime model is `claude-sonnet-5-5`, using the authorised ALM Anthropic credential. Do not configure a fast-model substitution, fallback model or different account. A model change needs Bill's approval.

## Forms and same-origin links

The shared backend serves MateBot at `/` and HomeBot's combined chooser at `/homebot/`. The self-hosted chooser and incomplete-party handoff point to the same backend's `/#/`, not the old hosted preview. `/homebot` redirects to `/homebot/` so relative scripts load correctly; query-string TEST flags are preserved.

HomeBot remains in review mode by default. Only after the relevant acceptance and Bill's release approval may the host set `MMAI_HOMEBOT_REVIEW_BUILD=0`; this changes form presentation/TEST defaults, not any background-worker or delivery switch. Verify both ordinary and TEST journeys afterward.

Set `MMAI_PUBLIC_BASE_URL` to the actual approved HTTPS origin. `app.matebot.com.au` in the template is a proposal requiring DNS/ownership verification, not an established live address. Verify all saved-form, Bring-a-mate, staff and individual-response links on the real host.

## Private testing behind a sign-in (pilot gate)

Set `MMAI_PILOT_GATE=1` before the service is given any public address. While it is set:

- Every page, file and API call needs an allowed signed-in staff user. Without one, only these answer: the sign-in page `/pilot/` with its two files, the sign-in call `POST /api/auth/login`, the session check `GET /api/auth/session`, and `GET /api/health`, which replies only `{"ok": true}`.
- Allowed users are the manager account by default. To allow named others, set `MMAI_PILOT_USERS` to a comma-separated list of usernames; that list then replaces the default, so include the manager if he should keep access. Other staff are refused at sign-in and their earlier sessions do not pass.
- Refused requests are decided before any handler, scheduler check or sweep runs, and write nothing. The preview-only visitor header is ignored.
- Every new enquiry, HomeBot enquiry and saved form is stored as TEST, whatever the page sent.
- Every reply carries `Cache-Control: private, no-store` and `Strict-Transport-Security`, so the host's edge cache holds nothing.
- Any value other than unset, empty, `0`, `off`, `false` or `no` turns the gate on, so a mistyped value closes the site rather than opening it.

Order of work on the host: (1) add `MMAI_PILOT_GATE=1` and `MMAI_PUBLIC_BASE_URL`, deploy, and confirm from the container console that `/` and `/api/apartments` are refused without sign-in and `/api/health` returns only `{"ok":true}`; (2) only then add the public HTTP route (`http_port: 8080` and an ingress rule for `/`) so the domain certificate can be issued; (3) check the public address the same way before anyone signs in. Removing the variable restores ordinary behaviour and is a release decision for Bill, not a configuration tidy-up.

Known limit while testing: repeated wrong passwords for a username pause sign-in for that username for 15 minutes, which an outsider who knows the username can trigger; this is a nuisance, not an entry.

## External acceptance and release sequence

Anna supplies current inventory, verifies staff accounts and authorises the existing Microsoft 365 mailbox and controlled recipients. Follow `NOTIFICATION_OPERATIONS.md`; provider acceptance is not evidence of receipt, and manual phone-copy previews are not automatic submission receipts.

Routing stays off until the required provider rights, eligible credentials, modes, Melbourne departure handling, quotas and billing controls are verified. Follow `RELEASE_OPERATIONS.md`; never populate a rights document merely to bypass its gate.

Before public use, verify HTTPS, security headers, cookies, reverse-proxy handling, rate limits, frame origins, both full enquiry journeys, staff workflows, received notifications and a managed-host backup/restore drill. Test rollback against an isolated restored copy, without overwriting newer records or replaying ambiguous messages.

Present the evidence and remaining limitations to Bill. Activate only the separately approved switches and controlled pilot, and publish or embed the combined chooser on ALM's WordPress site only after final release approval. Do not change ALM's existing WordPress/email hosting or automatically activate advertisements.
