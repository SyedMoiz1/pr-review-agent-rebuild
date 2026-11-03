#### What the System Does
**There are two services:** The api and the worker. 
- The API: - Takes requests sent to the /webhook endpoint, verifies the signature, validates the hmac provided and checks for valid headers, then inserts the event into the postgres webhook_events table. It then queues a job in the Redis Queue. 
- The Worker:  - takes job from queue, fetches the diff from github, and inserts it into the postgres diffs tables. 

#### Where the code and config live
- API: src/pr_review_agent_rebuild/webhook.py: app
- Worker: src/pr_review_agent_rebuild/worker.py
- Requires the following env variables: WEBHOOK_SECRET, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_DB, DATABASE_URL, REDIS_URL, GITHUB_TOKEN
- Github client: src/pr_review_agent_rebuild/github_client.py
- Exceptions class: src/pr_review_agent_rebuild/exceptions.py

#### The three commands that tell you the current state.
- check if services are active: systemctl is-active pr-review-api pr-review-worker postgresql@16-main redis-server
- check service status: systemctl status service-name
- check service logs: journalctl -u service-name -n 30 --no-pager

#### Failures & Fixes
1. Webhooks return 500, database unreachable
- symptom: database service did not start correctly so webhook returns 500
- Check: journalctl -u pr-review-api -n 50. Look for your StorageError line.
- Service Stopped: systemctl is-active postgresql@16-main. If stopped, start with systemctl start postgresql@16-main.
- Database rejects Auth: Credentials in /etc/pr-review-agent.env no longer match the role; fix with ALTER ROLE.
- Connections exhausted: SELECT count(*) FROM pg_stat_activity; and restart the API to drop its pool.
- Verify: Verify it is active -> systemctl is-active postgresql@16-main. Verify it answers -> sudo -u prreview psql "$DATABASE_URL" -c "SELECT 1;" to verify it answers.

2. Webhooks return 200 but no diffs appear.
- symptom: API's job ends at enqueue, so a 200 only means event was stored and queued. 
- Check: systemctl is-active pr-review-worker, then journalctl -u pr-review-worker -n 50
- Confirm where it stopped: redis-cli LLEN arq:queue for a backlog, and compare counts in webhook_events against diffs.
- Worker dead: start it 
- Redis down: start it
- Worker running but idle while the queue grows: It cant reach Redis, or REDIS_URL differs between two units. 
- Jobs ran and returned early: look for "already fetched" or "not found" warnings.
- Verify: enqueue one delivery, watch it complete and the row land. 

3. Jobs retrying forever
- Check: journalctl -u pr-review-worker | grep Retry
- Confirm: if Retry-After is present it is rate limiting; if not, GitHub is erroring.
- Rate-limited: Wait, check remaining quota on API
- Token expired or revoked: new token in .env file, then systemctl restart pr-review-worker
- Token lacks repo access: fix permissions on token settings
- Verify: the job completes, or fails permanently with your perm-error log rather than looping.

4. Worker crash-looping on start
- Check: systemctl status pr-review-worker, then journalctl -u pr-review-worker -n 100.
- Common Causes: missing env var -> KeyError at import. Schema mismatch after a deploy -> compare alembic_version against file in versions directory. Bad unit edit -> systemctl daemon-reload (this must run after any unit change)
- Fix: systemctl reset-failed pr-review-worker && systemctl start pr-review-worker

5. Disk full
- Check: df -h, then du -sh /var/lib/postgresql /var/lib/redis /var/log/journal.
- Usual culprits: the journal with no cap, Postgres WAL/Redis AOF growth.
- Fixes: journalctl --vacuum-size=200M for immediate relief, and SystemMaxUse= in /etc/systemd/journald.conf to stop it recurring. For Postgres, find the large tables and decide on retention. For Redis AOF, BGREWRITEAOF.
- Verify: df -h under threshold, and a webhook stores a row.

6. Service did not come back after reboot
- Check: systemctl is-enabled pr-review-api pr-review-worker. disabled means enable was never run, and that is the usual answer.
- Also check: systemctl status for a dependency failure. If the app started before Postgres accepted connections, After= ordering or a missing Wants= is the cause.
- Fix: systemctl enable --now <unit>.
- Verify: reboot again and confirm all four units come up untouched. Do not take the first success on faith.


#### Tracing a delivery end-to-end:
- Two identifiers link everything: delivery_id (from GitHub's X-GitHub-Delivery header, also the arq job id) and request_id (generated per request by the API middleware, carried into the job so it appears in both services).
- If you only have a delivery id from GitHub's Recent Deliveries tab, find its request id first. Command: `journalctl -u pr-review-api -u pr-review-worker --no-pager | grep "<delivery_id>"`
- If you already have a request id, from an error response or the x-request-id header, skip the step above.
- One command gives the whole story, both services, in order. Command: `journalctl -u pr-review-api -u pr-review-worker --no-pager | grep "<request_id>"`
- Expect these lines in this order. The first one missing is where it broke:
  - api: "Webhook number # for Pull Request # successfully verified" - HMAC passed
  - api: "Row successfully inserted for delivery id ..." - stored in webhook_events, job enqueued
  - api: "status": 200 - GitHub got its 200
  - worker: "PR Metadata received for repo: ..." - first GitHub call succeeded
  - worker: "PR Files received for repo: ... File count: N" - second GitHub call succeeded
  - worker: "Diffs and Diff_files insert completed for repo: ..." - stored in diffs and diff_files
- If the worker lines are missing entirely, the job was never picked up. Check the worker and the queue. Commands: `systemctl is-active pr-review-worker`, `redis-cli LLEN arq:queue`
- arq's own lines use the delivery id, not the request id. Command: `journalctl -u pr-review-worker --no-pager | grep "<delivery_id>"`. Look for → (started), ← (finished), ↻ (retrying), ! (failed).
- Confirm the event row exists. Command: `psql "$DATABASE_URL" -c "SELECT id, repo, pr_number FROM webhook_events WHERE delivery_id = '<delivery_id>';"`
- Confirm the diff row and its file count. Command: `psql "$DATABASE_URL" -c "SELECT d.id, d.repo, d.pr_number, d.head_sha, count(f.id) AS files FROM diffs d LEFT JOIN diff_files f ON f.diffref_id = d.id JOIN webhook_events e ON e.id = d.event_id WHERE e.delivery_id = '<delivery_id>' GROUP BY d.id;"`
- A webhook_events row with no matching diffs row means the job never completed. See failure 2.