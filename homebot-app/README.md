# HomeBot frontend

HomeBot collects whole-apartment enquiries and shares the backend and staff workspace with MateBot. This source is not a live deployment; account, delivery, provider and release gates remain separate.

## Routes and self-hosted entry

The combined chooser is `/homebot/#/`, the introduction is `#/homebot` and the questionnaire is `#/homebot/form`. The backend redirects `/homebot` to `/homebot/` so relative assets resolve correctly, preserving query parameters.

On the shared host, the backend rewrites `mmai-api-base` to the same origin and `matebot-url` plus fallback links to `/#/`. Both chooser and incomplete-party handoffs therefore use the same backend's saved-form records, never an unrelated preview.

## Review and TEST mode

`MMAI_HOMEBOT_REVIEW_BUILD` defaults to review mode. Only the exact value `0` makes the self-hosted page render production-mode presentation; this is a release setting, not permission to activate any service. Review mode marks HomeBot submissions and MateBot handoffs TEST.

Outside review mode, `test=1` in either the URL query or hash query makes TEST mode sticky for the current page and its in-page links. Production-mode ordinary enquiries and TEST records must remain distinct.

For a separately hosted frontend, configure the API and MateBot meta values explicitly at deployment; do not infer authentication or connectivity from a rendered page. The source HTML remains a review build by default.

## Submission and staff workflow

The form saves to the shared backend and uses the returned record reference. HomeBot matching, routing and notification workers are separately controlled; a saved enquiry is not proof of a match or received email.

Incomplete parties choosing MateBot use the authorised saved-form handoff; those declining housemate assistance remain HomeBot enquiries under the approved current-party/planning rules. Customer messages and introductions stay staff-led apart from the approved fixed receipts.

Read `../mmai-app/RELEASE_OPERATIONS.md`, `DEPLOY_DIGITALOCEAN.md` and `NOTIFICATION_OPERATIONS.md` for controlled preparation, permissions, recovery and release requirements.
