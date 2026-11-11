# Notification delivery controls

This increment implements fixed HomeBot submission copies and consumption of the existing internal attention outbox. It adds no customer match messages, new alert event kinds or public-release authority.

## Configuration

- Mail and alerts remain off by default. Existing sender configuration, TEST-recipient restrictions and Microsoft 365 authorisation still apply; do not enable real sending until separately authorised.
- `MMAI_NOTIFICATIONS=1` enables independent dispatch when a queue's existing mail/alert mode is enabled. If omitted, it inherits `MMAI_SCHEDULER` (default `1`); therefore explicitly set notifications to `1` if matching scheduling is disabled but authorised notifications should run.
- Restart the process after configuration changes. One worker per enabled queue checks once per second, consuming at most 25 due rows per pass. A slow queue does not block other notification queues or the matching loop. This is not a guaranteed one-second inbox-delivery SLA.
- Immediate customer-receipt kicks remain as well as periodic retry/restart recovery. Atomic claims prevent two consumers from sending the same queued row concurrently.
- Enabling delivery never releases held historical rows. A missing original HomeBot answer snapshot is held rather than reconstructed from staff-edited answers.

## Delivery states and review

- `queued` means waiting; `dryrun` means no message was sent. `sent` means provider acceptance, not receipt in an inbox.
- A process interruption during sending, a Microsoft Graph response with unknown acceptance (`GraphAmbiguous`), or a failure to save acknowledgement after provider acceptance leaves `sending` for investigation. All three queues use this conservative Graph policy. Do not reset it blindly to queued: check provider records first.
- Definite transient rejections retain bounded retries. SMTP/OS failures retain the existing retry semantics; not every SMTP disconnect reveals whether a provider accepted a message. Exactly-once delivery is not guaranteed.
- HomeBot operations: `python -m server.homebot_receipts status` shows counts and IDs requiring review without customer addresses or answer bodies. MateBot exposes receipt status on the staff record; internal alerts retain `python -m server.alerts status`.
- Deletion, TEST clearing and retention purge remove HomeBot snapshots with their enquiry. A deletion after the final pre-send existence check cannot recall an already in-flight message.

## Verification boundary

All development tests use synthetic records and blocked/captured transports. Live Microsoft 365 acceptance and inbox receipt, authorised operational recovery, and production release remain separate gates.
