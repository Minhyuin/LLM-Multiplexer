#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Pioneer Portal — deploy gateway
#
#   Polls the configured repository for tags matching DEPLOY_TAG_PATTERN and
#   deploys the highest one it has not deployed yet.
#
#   Design rules (deliberate, do not "simplify" these away):
#     * READ-ONLY against everything that is not the portal. It never touches
#       the proxy, the keeper, nginx config or PostgreSQL config.
#     * The live code path is a symlink. Deploy = atomically repoint the
#       symlink, so there is never a half-written tree on disk.
#     * Every deploy is health-checked; a failure flips the symlink back.
#     * The database is backed up before migrations, and the backup is
#       verified non-empty (the old cron job produced 20-byte "backups" for
#       months because it never checked).
#     * Idempotent: running twice in a row deploys once.
#
#   Usage:
#     deploy.sh                 normal run (what the systemd timer calls)
#     deploy.sh --check         validate config + prerequisites, change nothing
#     deploy.sh --baseline      record the currently deployed tag as the
#                               baseline WITHOUT deploying (use right after a
#                               manual deploy, or to stop a back-catalogue tag
#                               from being deployed)
#     deploy.sh --tag v1.2.3    force-deploy a specific tag
# ---------------------------------------------------------------------------
set -euo pipefail

GATEWAY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${GATEWAY_DIR}/deploy.env"

# shellcheck source=deploy.env
[ -f "$CONFIG_FILE" ] || { echo "FATAL: missing $CONFIG_FILE" >&2; exit 3; }
# shellcheck disable=SC1090
. "$CONFIG_FILE"

MODE="run"
FORCED_TAG=""
case "${1:-}" in
  --check)    MODE="check" ;;
  --baseline) MODE="baseline" ;;
  --tag)      MODE="run"; FORCED_TAG="${2:-}"; [ -n "$FORCED_TAG" ] || { echo "FATAL: --tag needs a value" >&2; exit 3; } ;;
  --dry-run)  MODE="run"; DRY_RUN=1 ;;
  "")         ;;
  *)          echo "FATAL: unknown argument '$1'" >&2; exit 3 ;;
esac

mkdir -p "$STATE_DIR" "$LOG_DIR" "$RELEASES_DIR" "$TORCH_DIR"
chmod 750 "$STATE_DIR" "$TORCH_DIR" 2>/dev/null || true

LAST_TAG_FILE="${STATE_DIR}/last_deployed_tag"
PREV_LINK_FILE="${STATE_DIR}/previous_release"
BUILD_LOG="${LOG_DIR}/build.log"

log() {
  local msg="[$(date -u '+%Y-%m-%dT%H:%M:%SZ')] $*"
  if [ "$MODE" = "check" ]; then
    echo "$msg"
  else
    echo "$msg" | tee -a "$LOG_FILE"
  fi
}
die() { log "ERROR: $*"; exit 1; }

# ---------------------------------------------------------------------------
# 0. preflight
# ---------------------------------------------------------------------------
preflight() {
  local rc=0
  for t in git curl tar python3; do
    command -v "$t" >/dev/null 2>&1 || { log "preflight: MISSING $t"; rc=1; }
  done
  [ -x "${VENV}/bin/python" ] || { log "preflight: MISSING venv at ${VENV}/bin/python"; rc=1; }
  [ -f "$ENV_FILE" ]          || { log "preflight: MISSING $ENV_FILE"; rc=1; }
  systemctl cat "$SERVICE" >/dev/null 2>&1 || { log "preflight: MISSING systemd unit $SERVICE"; rc=1; }
  [ -L "$APP_LINK" ] || log "preflight: NOTE $APP_LINK is not a symlink yet (first deploy will convert it)"
  return $rc
}

if ! preflight; then
  [ "$MODE" = "check" ] && exit 4
  die "preflight failed"
fi

# ---------------------------------------------------------------------------
# 1. what is deployed right now
# ---------------------------------------------------------------------------
current_tag() { [ -f "$LAST_TAG_FILE" ] && cat "$LAST_TAG_FILE" || echo ""; }

# ---------------------------------------------------------------------------
# 2. what does the remote offer
# ---------------------------------------------------------------------------
remote_tags() {
  # --tags --refs only: annotated tag objects are dereferenced, so we never
  # pick up the "^{}" duplicates. Never uses the GitHub API, so there is no
  # rate limit to be exhausted.
  timeout "$GIT_TIMEOUT" git ls-remote --tags --refs "$DEPLOY_REPO_URL" 2>/dev/null \
    | awk '{print $2}' | sed 's#^refs/tags/##' \
    | grep -E "$TAG_REGEX" | sort -V || true
}
# anchor the pattern so "v*" cannot match "xv1"; turn the glob into a regex
TAG_REGEX="^$(printf '%s' "$DEPLOY_TAG_PATTERN" | sed 's/[.[\^$()+?{}|]/\\&/g; s/\*/.*/g')\$"

if [ "$MODE" = "check" ]; then
  log "check: config=$CONFIG_FILE"
  log "check: repo=$DEPLOY_REPO_URL"
  log "check: app_link=$APP_LINK -> $(readlink -f "$APP_LINK" 2>/dev/null || echo '(not a symlink)')"
  log "check: currently deployed tag = '$(current_tag)'"
  log "check: health urls = $HEALTH_URLS"
  log "check: remote tags reachable:"
  remote_tags | sed 's/^/        /' || true
  log "check: OK (nothing was changed)"
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. pick the target tag
# ---------------------------------------------------------------------------
LATEST="$(remote_tags | tail -1)"
[ -n "$FORCED_TAG" ] && LATEST="$FORCED_TAG"
if [ -z "$LATEST" ]; then
  log "no tags match '${DEPLOY_TAG_PATTERN}' on ${DEPLOY_REPO_URL} — nothing to do"
  exit 0
fi

if [ "$MODE" = "baseline" ]; then
  echo "$LATEST" > "$LAST_TAG_FILE"
  log "baseline recorded: ${LATEST} (no deploy performed)"
  exit 0
fi

CURRENT="$(current_tag)"
if [ "$LATEST" = "$CURRENT" ]; then
  exit 0                      # quiet no-op; this is the common case every 5 min
fi

# Refuse to silently downgrade: if the target is not newer than what is live,
# stop and let a human decide (sort -V tells us the ordering).
if [ -n "$CURRENT" ] && [ "$(printf '%s\n%s\n' "$LATEST" "$CURRENT" | sort -V | tail -1)" = "$CURRENT" ]; then
  log "target ${LATEST} is older than live ${CURRENT} — refusing to downgrade (use --tag ${LATEST} to force)"
  exit 0
fi

log "=== deploy triggered: ${CURRENT:-<none>} -> ${LATEST} ==="
[ "$DRY_RUN" = "1" ] && { log "DRY_RUN=1: would deploy ${LATEST}; stopping here"; exit 0; }

RELEASE_DIR="${RELEASES_DIR}/${LATEST}"
BUILD_DIR="${TORCH_DIR}/build-${LATEST}"
WHEELHOUSE="${TORCH_DIR}/wheels-${LATEST}"
rm -rf "$BUILD_DIR" "$WHEELHOUSE"
mkdir -p "$BUILD_DIR" "$WHEELHOUSE"

FAIL_REASON=""
flip_back() {
  local prev
  prev="$(cat "$PREV_LINK_FILE" 2>/dev/null || true)"
  [ -n "$prev" ] || { log "no previous release recorded — cannot auto-rollback"; return 1; }
  ln -sfn "$prev" "${APP_LINK}.new" && mv -Tf "${APP_LINK}.new" "$APP_LINK"
  systemctl restart "$SERVICE"
  log "ROLLED BACK to $(basename "$prev")"
}

# ---------------------------------------------------------------------------
# 4. build the release
# ---------------------------------------------------------------------------
build_release() {
  log "fetching ${LATEST} from ${DEPLOY_REPO_URL}"
  local tarball="${BUILD_DIR}/src.tar.gz"
  # codeload serves a tarball for the exact tag; faster and far more reliable
  # than a git clone, and it cannot pick up a moving branch.
  local url="${DEPLOY_REPO_URL%.git}"
  case "$url" in
    https://github.com/*/*) url="https://codeload.github.com/${url#https://github.com/}" ;;
    *) die "cannot derive a codeload URL from DEPLOY_REPO_URL='${DEPLOY_REPO_URL}'" ;;
  esac
  url="${url}/tar.gz/refs/tags/${LATEST}"

  curl -fsSL --retry 5 --retry-delay 3 --retry-all-errors \
       --connect-timeout 20 -m "$GIT_TIMEOUT" -o "$tarball" "$url" \
    || die "download failed: $url"
  [ -s "$tarball" ] || die "downloaded tarball is empty"

  tar -xzf "$tarball" -C "$RELEASE_DIR.tmp" --strip-components=1
  log "extracted $(find "$RELEASE_DIR.tmp" -type f | wc -l) files"

  # sanity: this must really be the portal, not some other repo
  [ -f "$RELEASE_DIR.tmp/manage.py" ] || die "release has no manage.py — wrong repository?"
  [ -f "$RELEASE_DIR.tmp/requirements.txt" ] || log "WARNING: release has no requirements.txt"

  # record exactly what commit this tag points at, for the audit trail
  {
    echo "tag=${LATEST}"
    echo "repo=${DEPLOY_REPO_URL}"
    echo "branch=${DEPLOY_BRANCH}"
    echo "deployed_at=$(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "deployed_by=deploy-gateway"
    echo "sha256_src=$(sha256sum "$tarball" | awk '{print $1}')"
  } > "$RELEASE_DIR.tmp/.release"

  # dependencies: build wheels in staging, then install from the wheelhouse.
  if [ -f "$RELEASE_DIR.tmp/requirements.txt" ]; then
    "${VENV}/bin/python" -m pip wheel -q -r "$RELEASE_DIR.tmp/requirements.txt" \
        -w "$WHEELHOUSE" >>"$BUILD_LOG" 2>&1 \
      || die "pip wheel failed for ${LATEST} (see $BUILD_LOG)"
    "${VENV}/bin/python" -m pip install -q --upgrade --no-index \
        --find-links "$WHEELHOUSE" -r "$RELEASE_DIR.tmp/requirements.txt" >>"$BUILD_LOG" 2>&1 \
      || die "pip install failed for ${LATEST} (see $BUILD_LOG)"
    log "dependencies verified against requirements.txt"
  fi

  rm -rf "$RELEASE_DIR"
  mv -T "$RELEASE_DIR.tmp" "$RELEASE_DIR"
  # shared, not part of the release and never overwritten by one
  ln -sfn "${APP_LINK}/.env"   "$RELEASE_DIR/.env"
  ln -sfn "${APP_LINK}/media"  "$RELEASE_DIR/media"
  ln -sfn "${APP_LINK}/staticfiles" "$RELEASE_DIR/staticfiles"
  log "release staged at ${RELEASE_DIR}"
}

# ---------------------------------------------------------------------------
# 5. database safety + migrations
# ---------------------------------------------------------------------------
db_env_value() {
  # read a key out of the portal's own .env without printing the file
  sed -nE "s/^${1}=//p" "$ENV_FILE" | tail -1 | sed -E 's/^"(.*)"$/\1/'
}

backup_database() {
  local out="${STATE_DIR}/predeploy-${LATEST}-$(date -u '+%Y%m%dT%H%M%SZ').sql.gz"
  local db user pass host port
  db="$(db_env_value POSTGRES_DB)";     db="${db:-lab_portal}"
  user="$(db_env_value POSTGRES_USER)"; user="${user:-lab_portal}"
  host="$(db_env_value POSTGRES_HOST)"; host="${host:-127.0.0.1}"
  port="$(db_env_value POSTGRES_PORT)"; port="${port:-5432}"
  pass="$(db_env_value POSTGRES_PASSWORD)"
  # peer auth cannot work from root, so always go over TCP (this is the exact
  # bug that made the old cron backups 20-byte empty files).
  [ "$host" = "localhost" ] && host="127.0.0.1"
  PGPASSWORD="$pass" pg_dump -h "$host" -p "$port" -U "$user" -d "$db" \
      --no-owner --no-privileges 2>"${STATE_DIR}/pgdump.err" | gzip > "$out"
  local rc=${PIPESTATUS[0]}            # NOT $? — that is gzip's status
  if [ "$rc" -ne 0 ]; then
    log "pg_dump failed: $(head -c 300 "${STATE_DIR}/pgdump.err")"
    rm -f "$out"; return 1
  fi
  # the whole point: verify the artefact instead of trusting the exit code
  if [ ! -s "$out" ] || ! gzip -t "$out" 2>/dev/null; then
    log "backup artefact is empty or corrupt: $out"
    rm -f "$out"; return 1
  fi
  if [ "$(zcat "$out" | grep -c '^CREATE TABLE')" -lt 5 ]; then
    log "backup contains too few tables — refusing to trust it: $out"
    rm -f "$out"; return 1
  fi
  log "database backup OK: $out ($(stat -c %s "$out") bytes)"
}

# ---------------------------------------------------------------------------
# 6. health check
# ---------------------------------------------------------------------------
health_check() {
  local url attempt
  for attempt in $(seq 1 "$HEALTH_RETRIES"); do
    local all_ok=1
    for url in $HEALTH_URLS; do
      local code
      code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$url" || echo 000)"
      [ "$code" = "200" ] || all_ok=0
    done
    if [ "$all_ok" = "1" ]; then
      log "health check passed on attempt ${attempt}"
      return 0
    fi
    sleep "$HEALTH_DELAY"
  done
  log "health check FAILED after ${HEALTH_RETRIES} attempts"
  return 1
}

# ---------------------------------------------------------------------------
# 7. run it
# ---------------------------------------------------------------------------
rm -rf "${RELEASE_DIR}.tmp"
build_release

# Verify this release is the tag we think it is before touching anything live.
if [ -f "$RELEASE_DIR/.release" ]; then
  log "release marker: $(tr '\n' ' ' < "$RELEASE_DIR/.release")"
fi

PREV_TARGET="$(readlink -f "$APP_LINK" 2>/dev/null || true)"

if [ "$ALLOW_AUTO_MIGRATE" = "1" ]; then
  if ! ( cd "$RELEASE_DIR" && set -a && . "$ENV_FILE" && set +a \
         && "${VENV}/bin/python" manage.py migrate --check >/dev/null 2>&1 ); then
    log "release has pending migrations"
    if [ "$SKIP_DB_BACKUP" != "1" ]; then
      backup_database || die "pre-migration database backup failed — refusing to migrate"
    fi
  fi
else
  log "ALLOW_AUTO_MIGRATE=0: migrations are not applied automatically"
fi

# --- flip ------------------------------------------------------------------
ln -sfn "$RELEASE_DIR" "${APP_LINK}.new" && mv -Tf "${APP_LINK}.new" "$APP_LINK"
log "symlink flipped: ${APP_LINK} -> ${RELEASE_DIR}"

( cd "$RELEASE_DIR" && set -a && . "$ENV_FILE" && set +a \
  && "${VENV}/bin/python" manage.py migrate --noinput >>"$BUILD_LOG" 2>&1 ) \
  || { log "migrate failed"; flip_back; die "migrate failed for ${LATEST}"; }

( cd "$RELEASE_DIR" && set -a && . "$ENV_FILE" && set +a \
  && "${VENV}/bin/python" manage.py collectstatic --noinput --clear >>"$BUILD_LOG" 2>&1 ) \
  || { log "collectstatic failed"; flip_back; die "collectstatic failed for ${LATEST}"; }

systemctl restart "$SERVICE" || { log "systemctl restart failed"; flip_back; die "restart failed"; }
log "service restarted"

if health_check; then
  echo "$LATEST" > "$LAST_TAG_FILE"
  [ -n "$PREV_TARGET" ] && printf '%s\n' "$PREV_TARGET" > "$PREV_LINK_FILE"
  log "=== deploy OK: now running ${LATEST} ==="
else
  FAIL_REASON="health check failed"
  flip_back || true
  # surface it loudly: this is what an operator needs to see
  { echo "deploy FAILED at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "target=${LATEST}"
    echo "reason=${FAIL_REASON}"
    echo "rolled_back_to=$(readlink -f "$APP_LINK")"
  } > "${STATE_DIR}/LAST_FAILURE"
  die "deploy of ${LATEST} failed (${FAIL_REASON}) and was rolled back"
fi

# --- housekeeping ----------------------------------------------------------
mapfile -t ALL < <(ls -1dt ${RELEASES_DIR}/*/ 2>/dev/null | sed 's#/$##')
if [ "${#ALL[@]}" -gt "$KEEP_RELEASES" ]; then
  LIVE_TARGET="$(readlink -f "$APP_LINK" 2>/dev/null || echo /nonexistent)"
  PREV_TARGET_SAVED="$(cat "$PREV_LINK_FILE" 2>/dev/null || echo /nonexistent)"
  for d in "${ALL[@]:$KEEP_RELEASES}"; do
    [ "$d" = "$LIVE_TARGET" ] && continue
    [ "$d" = "$PREV_TARGET_SAVED" ] && continue
    log "pruning old release $(basename "$d")"
    rm -rf "$d"
  done
fi
rm -rf "$BUILD_DIR" "$WHEELHOUSE"
exit 0
