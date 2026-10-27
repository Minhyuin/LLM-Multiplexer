# Portal release safety checklist

The production gateway runs outside application releases at `/root/pioneer-portal.deploy/gateway`. Updating the repository does not automatically update that external copy. Review and install gateway changes separately with the tag-polling timer stopped and the deployment lock held.

## Required release gates

- Limit scope to the portal; never use production paths for fault-injection tests.
- Stop `pioneer-portal-deploy.timer` before maintenance and confirm the deployment service has no running process. Keep the serving portal running during preparation.
- Record the live symlink target, gateway/configuration hashes, deployed tag and previous-release state. Preserve the live and rollback directories.
- Obtain a PostgreSQL backup with a checked command exit status, protect it with root-only permissions, and restore it into a distinct validation database. A nonempty gzip file alone is insufficient evidence.
- Review the exact migration plan and SQL. Keep `ALLOW_AUTO_MIGRATE=0`; apply approved backward-compatible migrations explicitly before activation. Use lock/statement timeouts appropriate to the operation.
- Test the candidate against PostgreSQL and representative roles. Do not run test suites against the production database.
- Pin the reviewed application commit and verify the release tag resolves to that commit before deployment.
- Build each virtualenv at its final absolute path. Collect release-local static assets before flipping the live symlink.
- The public health endpoint returns 200, login returns 200 and protected business pages redirect anonymous requests to login. The homepage is NOT an HTTP-200 anonymous health probe.
- Health probes must work for both the candidate and the previous version so code rollback can be checked. Check authenticated functionality separately, not just `/health/`.
- A date-tag to semantic-version transition requires an explicit operator `--tag`; timer-triggered downgrades remain blocked. Never falsify the deployed-tag state to bypass ordering.
- Rollback restores code/virtualenv/static assets via the old symlink. It does not reverse migrations or restore the database automatically. Additive community tables can remain during code rollback, preserving any newly written content.
- Verify external login redirects, role access, branding, static files, service status and unchanged identities of nonportal services. Rearm the polling timer only after successful verification.

## Fingerprint worker gates (v1.9.2, feature off by default)

See `FINGERPRINT_RELEASE.md` for the install steps, environment keys and rollback semantics.

- Confirm `scripts/lm-detector_lock.json` is the reviewed pin and that the pinned submodule gitlink is committed at that exact commit. The release tarball contains no submodule content; the gateway materialises the whitelist itself.
- Record the `lmfpd` version, wheel name, wheel SHA-256 and the lock commit from `verify_sdk_runtime.py`/the wheel manifest **before** the deploy, and compare them against `/root/pioneer-portal/.fingerprint-release` afterwards.
- If the release venv's offline runtime check was skipped (`SKIP_SDK_RUNTIME_CHECK=1`), treat the deploy as unverified and run `scripts/verify_sdk_runtime.py` in `/root/pioneer-portal/.venv` by hand before enabling the feature.
- Install `pioneer-portal-fingerprint.service` once per host and leave it stopped; the gateway stops it before the symlink flip and starts it only after the health check passes. Starting it by hand before installing the release that ships `run_fingerprint_worker` runs code that does not exist.
- Keep `FINGERPRINT_ENABLED=false` until the migration is applied, the unit is installed and `manage.py run_fingerprint_worker --once` exits cleanly on the live release. Turning the flag on with the unit missing, or with a release that has no worker command, makes the next deploy refuse.
- Do not run fault-injection or queue-draining tests against the production database; `--once` on the live release is the only sanctioned smoke test and it must be run while the feature is still disabled.
- Rollback keeps the worker on the previous release and leaves it stopped when that release has no worker command. An additive fingerprint schema survives the rollback; queued runs written by the newer release are not claimed until the feature is redeployed.

## Access and inherited operational boundaries

Django login middleware does not protect nginx aliases. The inspected media directory was empty; do not introduce private uploads without authenticated delivery. Public static assets and the minimal health endpoint remain intentionally public. Existing nonportal subscription routes are outside the community feature's authorization boundary.

The portal's existing IP endpoint uses HTTP, not HTTPS. Login-only access does not encrypt passwords or session cookies. Configure a dedicated HTTPS portal hostname before treating the endpoint as secure against network interception. Do not repurpose another application's HTTPS virtual host.

The legacy scheduled backup cron was explicitly deferred and is not repaired by this release. A validated one-off release backup is not a substitute for tested scheduled backups and off-host retention.
