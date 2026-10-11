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

## Access and inherited operational boundaries

Django login middleware does not protect nginx aliases. The inspected media directory was empty; do not introduce private uploads without authenticated delivery. Public static assets and the minimal health endpoint remain intentionally public. Existing nonportal subscription routes are outside the community feature's authorization boundary.

The portal's existing IP endpoint uses HTTP, not HTTPS. Login-only access does not encrypt passwords or session cookies. Configure a dedicated HTTPS portal hostname before treating the endpoint as secure against network interception. Do not repurpose another application's HTTPS virtual host.

The legacy scheduled backup cron was explicitly deferred and is not repaired by this release. A validated one-off release backup is not a substitute for tested scheduled backups and off-host retention.
