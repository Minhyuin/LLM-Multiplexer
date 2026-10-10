# Pioneer Portal — deploy gateway

Makes a release tag on the portal repository show up on this server by itself,
with a health check and automatic rollback. It touches **only** the portal:
the proxy, the usage keeper, nginx config and PostgreSQL config are never
written to.

```
        GitHub                        server
  ┌──────────────────┐        ┌──────────────────────────────────────┐
  │  fork            │        │  pioneer-portal-deploy.timer (5 min) │
  │  Minhyuin/       │        │            │                          │
  │  LLM-Multiplexer │◀───────┤            ▼                          │
  │                  │  poll  │  deploy.sh                            │
  │  tag v1.2.3 ─────┼───────▶│   1 download tag tarball              │
  └──────────────────┘        │   2 stage into .releases/v1.2.3       │
                              │   3 pip wheels → install into venv    │
                              │   4 db backup → migrate               │
                              │   5 flip symlink (atomic)             │
                              │   6 collectstatic, restart gunicorn   │
                              │   7 health check → else flip back     │
                              └──────────────────────────────────────┘
```

## Layout on the server

| Path | Role |
|---|---|
| `/root/pioneer-portal` | **symlink** to the live release (nginx and systemd keep using this exact path, which is why nothing else has to change) |
| `/root/pioneer-portal.releases/<tag>/` | one immutable directory per deployed tag |
| `/root/pioneer-portal.shared/` | shared state (`.env`, `.venv`, `media`, `staticfiles`); each release symlinks to it, so no deploy or prune can destroy it |
| `/root/pioneer-portal.deploy/state/` | `last_deployed_tag`, `previous_release`, `LAST_FAILURE`, pre-deploy DB dumps |
| `/root/pioneer-portal.deploy/torch/` | staging + wheelhouse (cleaned after every run) |
| `/var/log/pioneer-portal/deploy.log` | one line per event; `build.log` holds pip/django output |
| `/root/pioneer-portal/deployment/gateway/` | this directory |

## Day-to-day

A release is: **commit to `main` → tag it → push the tag.** Nothing else.

```bash
git tag -a v2026.10.10.1 -m "credentials page: real quota refresh"
git push fork main --follow-tags
```

Within 5 minutes the server picks it up. Watch it:

```bash
tail -f /var/log/pioneer-portal/deploy.log
systemctl list-timers pioneer-portal-deploy.timer
```

Only tags matching `DEPLOY_TAG_PATTERN` (`v*`) are ever deployed, and the
highest version wins — `sort -V` ordering, so `v10` > `v9`. Scratch tags
cannot trigger a deploy. A tag older than what is live is **refused**, so a
mistaken tag cannot silently downgrade production.

### Commands

```bash
G=/root/pioneer-portal/deployment/gateway/deploy.sh
$G --check              # validate config, show remote tags, change nothing
$G                      # what the timer runs
$G --baseline           # record the highest remote tag as deployed WITHOUT deploying
$G --tag v1.2.3         # force-deploy a specific tag (also the way to force a downgrade)
$G --dry-run            # report what would happen
$G --convert v0.0.1     # one-time bootstrap only (see below)
```

## Recovery

**A deploy fails the health check** → the script flips the symlink back and
restarts by itself, and writes `/root/pioneer-portal.deploy/state/LAST_FAILURE`.
Check `deploy.log`. If migrations had already run, the *code* is back but the
*schema* is not — restore from the matching pre-deploy dump in `state/`:

```bash
zcat /root/pioneer-portal.deploy/state/predeploy-<tag>-<stamp>.sql.gz \
  | psql -h 127.0.0.1 -U lab_portal -d lab_portal
```

**Roll back to a previous release deliberately** (code only, schema untouched):

```bash
ln -sfn /root/pioneer-portal.releases/<older-tag> /root/pioneer-portal.new
mv -Tf /root/pioneer-portal.new /root/pioneer-portal
systemctl restart pioneer-portal
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8002/health/
```

The old release directories are kept until `KEEP_RELEASES` is exceeded, and the
current + previous one are never pruned.

**Stop the machinery:**

```bash
systemctl disable --now pioneer-portal-deploy.timer
```

## Design decisions worth knowing

* **The symlink is the deploy.** `ln -sfn` + `mv -T` is atomic, so gunicorn and
  nginx never observe a partially written tree. This is also why nginx needed
  no edit at all: its `alias /root/pioneer-portal/staticfiles/` resolves through
  the symlink.
* **codeload tarball, not `git clone`.** A tag tarball cannot drift, needs no
  `.git` in the release, and was measurably more reliable from this host than
  cloning over TLS.
* **Wheels are built before anything live changes.** A dependency problem fails
  the deploy while the old release is still serving.
* **The DB backup is verified, not trusted.** `pg_dump` must exit 0, the gzip
  must pass `-t`, and the dump must contain at least 5 `CREATE TABLE` lines.
  The old cron job wrote 20-byte empty files for months precisely because it
  checked nothing.
* **Peer auth is avoided.** `pg_dump` runs as root, so it must go over TCP
  (`-h 127.0.0.1`) with `PGPASSWORD`; the unix socket uses `peer` and would fail.
* **No GitHub API.** `git ls-remote` has no rate limit to exhaust.
* **No credentials on the server.** The repository is read anonymously; nothing
  is ever pushed from here, so there is no token or key on this host.

## First-time bootstrap (already done, kept for rebuilds)

Bootstrap is what turns the ordinary directory into a symlink plus release 0.
It is the only operation here that moves the live tree, so it is a single
explicit command rather than something the timer could ever reach:

```bash
G=/root/pioneer-portal/deployment/gateway/deploy.sh

# 1. convert: stop the service, lift .env/.venv/media/staticfiles into
#    /root/pioneer-portal.shared, restart, health-check, then move the code
#    tree itself to /root/pioneer-portal.releases/<tag> and symlink it back.
#    The tag given here is recorded as already deployed.
$G --convert v0.0.1

# 2. verify BEFORE enabling the timer
$G --check
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1/health/

# 3. start polling
systemctl enable --now pioneer-portal-deploy.timer
```

After step 1 the service is reachable at exactly the same paths as before
(`/root/pioneer-portal/lab_portal`, `.venv`, `.env`), because `--convert`
verifies the shared symlinks resolve *before* it moves anything.
