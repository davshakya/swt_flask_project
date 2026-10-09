Booking intake and notification delivery
=======================================

Bookings are committed to `data/sales_enquiries.queue.sqlite3` before the browser
receives its success redirect. The location follows `SALES_ENQUIRY_BACKUP_PATH`:
the queue replaces that path's suffix with `.queue.sqlite3`. Keep this file on
the server's persistent local disk, writable by Passenger, and include it in
backups. It is separate from the telemetry MySQL database.

Each form includes a submission token. Retrying the same token and booking
details returns the original booking. Identical details submitted with another
token (including an older client without tokens) are also suppressed for ten
minutes. Changed booking details can be submitted immediately.

Passenger workers drain pending notifications at startup and every five seconds.
An OS file lock prevents concurrent delivery across workers. Each channel's
state is committed before and after delivery; no queue transaction is held
while calling SMTP, WhatsApp, or MySQL. Pending channels survive restarts.
The booking success redirect uses the cached homepage visitor count.

Inspect the `bookings` table's `states` JSON for delivery results. `sent` means
the channel reported success; `failed` means it returned false (for example,
missing SMTP configuration); `review` means an exception or worker interruption
left delivery uncertain. `complete` means processing finished, not that every
channel succeeded. Failures are also logged with the booking ID.

Delivery failures are not automatically resent. SMTP cannot guarantee exactly
once delivery: it can accept a message immediately before a connection failure.
For `review` entries, check the mail provider's delivery log before resending.
After resolving a failure, an operator can remove only that channel from the
`states` JSON and set `complete=0` for that booking to retry it. Preserve the
other channel states, including successfully sent messages.

The original JSONL backup is still written after notification processing. The
queue remains the authoritative record if the backup or MySQL audit fails.
These changes remove MySQL and notification calls from successful booking
requests; they do not repair hosting-level MySQL disconnects or startup delays.
