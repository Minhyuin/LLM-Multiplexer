# Portal release safety checklist

The production gateway runs outside application releases at `/srv/pioneer-portal.deploy/gateway`. Updating the repository does not automatically update that external copy. Review and install gateway changes separately with the tag-polling timer stopped and the deployment lock held.

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

See `FINGERPRINT_RELEASE.md` for the gateway-owned runtime steps, environment keys and rollback semantics.

- Confirm `scripts/lm-detector_lock.json` is the reviewed pin and that the pinned submodule gitlink is committed at that exact commit. The release tarball contains no submodule content; the gateway materialises the whitelist itself on every deploy, whether the feature flag is on or off.
- Record the `lmfpd` version, wheel name, wheel SHA-256 and the lock commit from `verify_sdk_runtime.py`/the wheel manifest **before** the deploy, and compare them against `/srv/pioneer-portal/.fingerprint-release` afterwards.
- The gateway installs/refreshes `pioneer-portal-fingerprint.service` from the release (`deployment/gateway/pioneer-portal-fingerprint.service`), enables it, and starts it after the health check even while the feature is off. Do not install the unit by hand; review gateway changes separately because `/srv/pioneer-portal.deploy/gateway` is outside the application release.
- `FINGERPRINT_ENABLED` is an admin/runtime switch, not a runtime prerequisite. Confirm `systemctl is-active pioneer-portal-fingerprint` is `active` after every deploy; only then may an admin turn the feature on.
- Do not run fault-injection or queue-draining tests against the production database; `--once` on the live release is the only sanctioned smoke test, and it must use an approved test account or run while the feature is disabled.
- Rollback keeps the worker on the previous release and leaves it stopped only when that release has no worker command. An additive fingerprint schema survives the rollback; queued runs written by the newer release are not claimed until the feature is redeployed.

## Registration / invitation-code gates

See `REGISTRATION_RELEASE.md` for the commands, timer install steps and rollback
semantics.

- Confirm the feature commit is on `main` and that the release tag contains it
  (`git merge-base --is-ancestor <commit> <tag>`); a tag cut from a feature branch
  is overwritten by the next main release.
- The migration must stay additive (new tables or nullable/defaulted columns). Keep the
  production `ALLOW_AUTO_MIGRATE=1` behaviour only with the gateway's verified
  pre-migrate dump; expect `state/predeploy-<tag>-*.sql.gz` after the deploy.
- After the deploy, anonymous `GET /accounts/register/` must return 200 and
  `/users/admin/invitations/` must be reachable as staff. The health endpoint does
  not cover either page.
- Install `pioneer-portal-registration-sync.timer` only after the release is live;
  stop/disable it before a code rollback to a release without
  `sync_registration_keys`.
- Never store real invitation codes or secrets in the repository, unit files or
  deploy logs.

## Access and inherited operational boundaries

Django login middleware does not protect nginx aliases. The inspected media directory was empty; do not introduce private uploads without authenticated delivery. Public static assets and the minimal health endpoint remain intentionally public. Existing nonportal subscription routes are outside the community feature's authorization boundary.

The portal's existing IP endpoint uses HTTP, not HTTPS. Login-only access does not encrypt passwords or session cookies. Configure a dedicated HTTPS portal hostname before treating the endpoint as secure against network interception. Do not repurpose another application's HTTPS virtual host.

The legacy scheduled backup cron was explicitly deferred and is not repaired by this release. A validated one-off release backup is not a substitute for tested scheduled backups and off-host retention.
