# Project Guidance

This repository manages Docker containers on iKuai 4.0 routers. The command-line updater and the webapp/ application are separate supported interfaces; the Web application is the primary daily-use interface. Read webapp/README.md for Web deployment details and API_REFERENCE.md before changing iKuai API calls.

## Local Environment

- Keep credentials in the ignored local .env file. Never print or commit passwords, tokens, or authenticated API payloads. Let scripts read credentials from their configured files.
- .env, state.json, _mine/, caches, and generated backup files are local-only. Do not add them to Git. _mine/ contains captured iKuai frontend bundles and can be regenerated when needed.
- Prefer available Python 3.13 and Node.js runtimes for the zero-dependency regression suites. fnos_ssh.py and deploy_webapp.py require a Python environment with Paramiko.

## Regression Tests

Run all three suites after code changes:

```powershell
python _test/test_webapp.py
node _test/test_frontend.js
node _test/test_frontend_ux.js
```

The frontend timing suite must run with ?autocheck=0 in its test setup. Tests that start background threads or HTTP servers must join/stop them during teardown. For behavioral fixes, add coverage that exercises the affected production path and check that reverting the fix makes the test fail.

## Invariants

- docker_container.update requires a complete repository:tag image value. Handle comma-separated image tags with tag_matches() and determine container image ownership from image records, not the container image field.
- Do not remove the _persist_lock around jobs persistence. Keep dry_run scoped to each Job, not mutable global configuration.
- Preserve the data volume name ikuai-updater-data.
- Login must keep the real form, autocomplete="username", and autocomplete="current-password"; do not send WWW-Authenticate to unauthenticated browser requests. Preserve Basic authentication for scripts and CI. Missing AUTH_PASS with AUTH_USER configured must fail closed.
- Frontend colors use CSS variables. Table cells need data-label attributes for the narrow-screen layout. The overview may say all containers are current only when every check succeeded AND no container is pinned to a fixed version tag whose repository latest has moved on. Detect pins with A.is_version_tag (pure version numbers such as 1.37.3 / v2.7); variant and semantic tags (1.37.3-alpine, latest, ssh, slim) are not pins and must never be flagged — they are parallel variants, not stale versions. Switching a pinned container back to latest goes through the confirmation dialog with the "switch to follow latest" wording.
- Whether a container already uses the target image must be decided from the container's OWN declared tag, never from an image record's tag set. An iKuai image record can carry several tags at once (an observed record was tagged "1.37.3,latest"), so matching the tag set reports a container pinned to 1.37.3 as already running latest and silently skips the update. iKuai refuses to re-pull a repo:tag it already holds locally ("已存在相同内容") and never compares content, so when a tag switch would land on the same record (owner tag set contains both the container's current tag and the target tag) the switch must fail loudly with the actionable hint to delete the image record from iKuai's Local Images, never report "already latest".
- Individual container failures must not abort a batch update. Keep update jobs mutually exclusive and allow a running check job to be reused.
- `/api/overview` must return `history_brief()` records, never raw history. The frontend decides whether a history row's log button is clickable from `log_count`; raw records have no such field, so every log button goes grey after each auto-refresh. Keep the log bodies out of the overview too — they were 75% of its payload.
- Version checks must keep registry source health (ikuai_api.order_sources / mark_source_bad): a dead mirror is retried once, then moved to the end of the fallback chain for SOURCE_COOLDOWN seconds. Dropping it makes every image wait out the same timeout (measured: 118s for 7 images). HTTP 404 must never mark a source bad — that is "this mirror lacks this image", not a broken mirror.
- Check jobs query upstream concurrently (jobs.CHECK_WORKERS). Results and per-image debug lines must still be emitted in container order.
- Notification channels come from environment variables only (NOTIFY_WEBHOOK_URL / NOTIFY_WEBHOOK_TOKEN / NOTIFY_PUSHPLUS_TOKEN). Never send the full URL or token to the browser — `/api/settings` exposes labels only. Sends are asynchronous and must never block job teardown; dry-run jobs are not announced.
- Before triggering a pull, resolve the tag upstream (ikuai_api.remote_tag_exists) and fail immediately when every source answers 404. iKuai does not error on a tag that does not exist — it queues a download that never completes and makes the job wait out the whole pull timeout, then reports "timeout" as the reason. Never treat "unknown" (all sources unreachable) as "tag missing".

## Release and Deployment

Before release, run all three regression suites, keep the version in webapp/app/server.py and the image tag in webapp/docker-compose.yml aligned, and wait for CI test/build/smoke jobs to pass before deploying. Do not deploy to the router or NAS unless the user asks for deployment. Verify the existing data volume is reused after any deployment.
