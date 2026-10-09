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

Recovery on shared hosting without Terminal
------------------------------------------

Upload the updated server, queue module and `scripts/run_booking_notifications.py`.
In Setup Python App, use Execute Python Script with:

    scripts/run_booking_notifications.py

This only inspects bookings and writes `data/sales_enquiries.queue.status.json`
(or the corresponding configured queue path). Download that JSON with File
Manager to see each channel's state. It contains IDs and delivery states, not
customer details or SMTP credentials.

To deliver pending channels once, use:

    scripts/run_booking_notifications.py --drain

Already sent and uncertain channels will not be resent. A drain handles up to
20 bookings. Configure cPanel Cron Jobs to run this every minute, using the
Python executable from the application's virtual environment activation command:

    /home/salewellco/virtualenv/repositories/swt_flask_project/3.11/bin/python /home/salewellco/repositories/swt_flask_project/scripts/run_booking_notifications.py --drain >> /home/salewellco/repositories/swt_flask_project/data/booking-worker.log 2>&1

The executable above is an example; use the actual virtualenv path shown by
cPanel. Cron loads `device.env`; variables set only in the Python Selector UI
may need to be added to that server-only file for cron. Never commit passwords.

After cron is configured, set `SALES_BOOKING_BACKGROUND_ENABLED=false` in the
web application's environment and restart it once. Intake still saves bookings
and wakes no delivery thread; cron owns notification delivery. The runner skips
MySQL startup maintenance and all web background workers. Its audit channel
may still attempt a MySQL write after sending notifications.

Frequent SIGTERM events themselves still require the hosting provider to
identify the shutdown trigger. This runner removes notification delivery's
dependency on the lifetime of a web worker.
