# Fingerprint worker release notes (v1.9.2)

This document covers only the portal-owned fingerprint worker: what ships, how the
deploy gateway treats it, how its runtime is installed with the release, and how to
roll it back. Use it together with `README_DEPLOY.md` and `RELEASE_CHECKLIST.md`.

The feature is **off by default**. While `FINGERPRINT_ENABLED` is false the UI
and new admissions are disabled, but the gateway still installs the worker unit and
keeps the worker running in its idle mode. Flipping the admin switch therefore
never requires a deploy or a manual systemd step. Releases carrying the dependency
lock still prepare and verify the SDK on every deploy, so enabling the feature
later does not need a different code build.

## What ships

| Artifact | Where | Purpose |
| --- | --- | --- |
| `fingerprints/` | release tree | models, services, views, queue, `run_fingerprint_worker`, `fingerprint_execute`, `prune_fingerprints` |
| `scripts/lm-detector_lock.json` | release tree | the audited pin: upstream commit, SDK version, whitelist, per-file size + SHA-256 |
| `scripts/prepare_fingerprint.py` | release tree | `bootstrap` (HTTPS, whitelist only) / `checkout` / `verify` / `manifest` |
| `scripts/build_sdk_wheel.py` | release tree | builds `lmfpd` from the verified tree and writes `lm-detector-wheel.json` |
| `scripts/verify_sdk_runtime.py` | release tree | offline proof that the pinned bank loads and scores in a given interpreter |
| `third_party/lm-detector/` | git submodule | the pinned upstream source; **not** in the codeload tarball |
| `pioneer-portal-fingerprint.service` | this directory | the worker unit template; the gateway renders and installs it on every deploy |

`lmfpd` is never installed from an index. The release venv gets it from the
wheelhouse built out of the verified tree, with `--no-index`.

## Environment

Portal `.env` (read by gunicorn, the worker and `manage.py`):

| Key | Default | Meaning |
| --- | --- | --- |
| `FINGERPRINT_ENABLED` | `false` | the single feature switch; off means no UI entry, no queue, no worker work |
| `FINGERPRINT_BASE_URL` | `http://127.0.0.1:8317` | local CPA data endpoint; no external host is contacted |
| `FINGERPRINT_CONCURRENCY` | `2` | bounded child processes inside the worker |
| `FINGERPRINT_QUEUE_LIMIT` | `20` | queued runs before new submissions are refused |
| `FINGERPRINT_DAILY_LIMIT` / `FINGERPRINT_ADMIN_DAILY_LIMIT` | `3` / `20` | per-user daily allowance |
| `FINGERPRINT_RUN_TIMEOUT` | `420` | hard per-run ceiling |

Gateway `deploy.env` (only affects the deploy run):

| Key | Default | Meaning |
| --- | --- | --- |
| `WORKER_SERVICE` | `pioneer-portal-fingerprint` | unit the gateway installs/stops/restarts; empty disables every worker hook |
| `WORKER_USER` / `WORKER_GROUP` | `pioneer` | service identity rendered into the unit template |
| `SYSTEMD_DIR` | `/etc/systemd/system` | where the rendered unit is installed |
| `SKIP_FINGERPRINT_DEPS` | `0` | `1` skips materialising the pinned dependency (debugging only) |
| `SKIP_SDK_RUNTIME_CHECK` | `0` | `1` skips the offline SDK proof in the release venv (debugging only) |

## Release-owned worker unit

The worker unit is part of the release, not an operator prerequisite. On every
deploy, before the code switch, `deploy.sh` renders
`pioneer-portal-fingerprint.service` from the candidate release with
`WORKER_USER`/`WORKER_GROUP`, installs it under `${SYSTEMD_DIR}` (default
`/etc/systemd/system`), runs `systemctl daemon-reload`, and enables it. If the
rendered unit is unchanged, the install is a no-op.

The unit resolves its code through `/srv/pioneer-portal` (the stable symlink) and
runs `/srv/pioneer-portal/.venv/bin/python manage.py run_fingerprint_worker`, so
`systemctl start` after a deploy always runs the release that is live right now.
The release tree is mounted read-only for the unit; the worker writes nothing but
the database and the journal.

The gateway stops the worker before the symlink flip and starts it again after the
portal health check, whether or not `FINGERPRINT_ENABLED` is on. While the flag is
off, the command only performs maintenance and sleeps; it does not claim queued
runs. This means enabling the feature later is a database-backed admin setting or
an `.env` change plus a restart, never a missing service.

## What a deploy does

1. Materialises `third_party/lm-detector` from the lock (whitelist only, size and
   SHA-256 verified), then re-verifies it. A mismatch aborts **before** anything
   live is touched.
2. Builds and hash-checks the `lmfpd` wheel; stages its own runtime dependencies
   under the release's `requirements.txt` pins.
3. Creates the release venv, installs `lmfpd` from the wheelhouse with
   `--no-index`, and runs `verify_sdk_runtime.py` in that exact interpreter with
   sockets disabled.
4. Writes `.fingerprint-release` (SDK version, wheel name and hash, lock commit,
   whether the runtime check was skipped) into the release directory.
5. Renders, installs and enables the worker unit from the candidate release, then
   stops the worker **before** the symlink flip. It starts it again only **after**
   the portal health check passes, even when the feature flag is off. A worker that
   will not come up is a deploy failure and rolls the code path back.

Refusals worth knowing:

- `FINGERPRINT_ENABLED` is on but the release has no `run_fingerprint_worker`
  command → deploy aborts with nothing live changed.
- The dependency tree does not match the lock → deploy aborts during staging.

## Checks after a deploy

```sh
systemctl is-active pioneer-portal-fingerprint          # active
journalctl -u pioneer-portal-fingerprint -n 50 --no-pager
cat /srv/pioneer-portal/.fingerprint-release            # sdk_version, wheel, lock
/srv/pioneer-portal/.venv/bin/python \
  /srv/pioneer-portal/scripts/verify_sdk_runtime.py    # exits 0, offline
cd /srv/pioneer-portal && .venv/bin/python manage.py \
  run_fingerprint_worker --once                         # executes queued work; see below
```

`--once` recovers abandoned leases, prunes old rows and processes queued runs before
exiting. It can send paid model requests when the feature is enabled. Use it only
with an explicitly approved test account and budget, or with the feature disabled
for a maintenance-only check. The SDK verification command above is offline.

## Rollback

Code rollback restores the previous release directory and its venv. It does not
reverse migrations and does not restore the database.

- The gateway stops the worker before flipping back and restarts it on the old
  release, so a worker never executes a retired tree against a newer schema.
- If the previous release has no worker command, the worker is left stopped and
  the gateway says so; that is the correct end state, not a failure.
- If the worker does not come back after a rollback, the gateway warns loudly and
  leaves the worker stopped; fingerprint detection stays down until the worker
  failure is fixed. No manual unit installation is involved.
- An additive fingerprint schema survives a code rollback; queued runs written by
  the newer release are simply not claimed until the feature is deployed again.

## Offline guarantees

- `bootstrap` fetches only the files named in the lock, over HTTPS, refusing
  redirects; a file that does not match its recorded size or hash fails the run.
- `lmfpd.load(<dir>)` does not touch the network, and `verify_sdk_runtime.py`
  disables sockets before importing it.
- The worker talks only to `FINGERPRINT_BASE_URL` (loopback by default) and to
  PostgreSQL.
- The detector source and reference data never follow a branch or resolve `latest`;
  `lmfpd` is installed from the verified local wheel only. Pinned build and other
  runtime dependencies may be downloaded from the configured package index during
  staging. Runtime scoring itself does not download code or data.
