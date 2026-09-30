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
- Frontend colors use CSS variables. Table cells need data-label attributes for the narrow-screen layout. The overview may say all containers are current only when every check succeeded.
- Individual container failures must not abort a batch update. Keep update jobs mutually exclusive and allow a running check job to be reused.

## Release and Deployment

Before release, run all three regression suites, keep the version in webapp/app/server.py and the image tag in webapp/docker-compose.yml aligned, and wait for CI test/build/smoke jobs to pass before deploying. Do not deploy to the router or NAS unless the user asks for deployment. Verify the existing data volume is reused after any deployment.
