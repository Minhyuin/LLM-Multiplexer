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
#     deploy.sh --convert <tag> one-time bootstrap: turn the existing real
#                               directory into the first release and replace it
#                               with the stable symlink (see README, "bootstrap")
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
  --convert)  MODE="convert"; FORCED_TAG="${2:-}"; [ -n "$FORCED_TAG" ] || { echo "FATAL: --convert needs a tag name" >&2; exit 3; } ;;
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
# 0. recovery for the bootstrap failure mode.
#
#    --convert lifts .env / .venv / media / staticfiles out of the code tree
#    while the service is stopped. If anything after that point fails, those
#    items are the only thing standing between the portal and a dead service,
#    so the recovery path is defined right here and both the health check and
#    backup helpers are declared before anything can call them.
# ---------------------------------------------------------------------------
SHARED_DIR="${SHARED_DIR:-${APP_LINK}.shared}"
SHARED_ITEMS=(.env .venv media staticfiles)

restore_app_link() {
  local item
  for item in "${SHARED_ITEMS[@]}"; do
    [ -e "${SHARED_DIR}/${item}" ] || continue
    if [ -L "${APP_LINK}/${item}" ] && [ "$(readlink "${APP_LINK}/${item}")" = "${SHARED_DIR}/${item}" ]; then
      continue
    fi
    if [ -e "${APP_LINK}/${item}" ] && [ ! -L "${APP_LINK}/${item}" ]; then
      rm -rf "${APP_LINK:?}/${item}"
    fi
    ln -sfn "${SHARED_DIR}/${item}" "${APP_LINK}/${item}"
    log "restored ${APP_LINK}/${item} -> ${SHARED_DIR}/${item}"
  done
}

health_check() {
  # $1 = "quiet" to suppress progress lines (used by flip_back, which reports
  # its own outcome and must not look like a second deploy attempt).
  local quiet="${1:-}"
  local spec url want code attempt all_ok
  local -a hdr=()
  # NOTE: `[ -n ... ] && hdr=(...)` would be the last command of this function
  # body and would abort the whole script under `set -e` whenever the test is
  # false. Always use a real if.
  if [ -n "${HEALTH_HOST:-}" ]; then
    hdr=(-H "Host: ${HEALTH_HOST}")
  fi
  for attempt in $(seq 1 "$HEALTH_RETRIES"); do
    all_ok=1
    for spec in $HEALTH_CHECKS; do
      url="${spec%:*}"                 # strips only the final :status
      want="${spec##*:}"
      [ "$want" = "$url" ] && want=200 # no ":status" given
      code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "${hdr[@]}" "$url" || echo 000)"
      [ "$code" = "$want" ] || all_ok=0
    done
    if [ "$all_ok" = "1" ]; then
      [ "$quiet" = "quiet" ] || log "health check passed on attempt ${attempt}"
      break
    fi
    if [ "$attempt" = "$HEALTH_RETRIES" ]; then
      [ "$quiet" = "quiet" ] || log "health check FAILED after ${HEALTH_RETRIES} attempts"
      return 1
    fi
    sleep "$HEALTH_DELAY"
  done
  if [ -n "${EXTRA_HEALTH_CHECK:-}" ]; then
    url="${EXTRA_HEALTH_CHECK%:*}"
    want="${EXTRA_HEALTH_CHECK##*:}"
    if [ "$want" = "$url" ]; then want=200; fi
    code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$url" || echo 000)"
    if [ "$code" = "$want" ]; then
      [ "$quiet" = "quiet" ] || log "extra health check passed: $url -> $code"
    else
      [ "$quiet" = "quiet" ] || log "extra health check FAILED: $url -> $code (wanted $want)"
      return 1
    fi
  fi
  return 0
}

# ---------------------------------------------------------------------------
# 1. preflight
# ---------------------------------------------------------------------------
preflight() {
  local rc=0
  local t
  for t in git curl tar python3 pg_dump; do
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
# 1b. one-time bootstrap: real directory -> symlink + release 0
#
#     Shared state (.env, .venv, media, staticfiles) is lifted OUT of the code
#     tree into SHARED_DIR and symlinked back in. A release is therefore pure
#     code, and no release or prune can ever touch the environment file, the
#     virtualenv, uploaded media or collected static files.
#
#     The code directory itself is NOT moved: it becomes
#     ${RELEASES_DIR}/<tag> by renaming the existing top-level directory into
#     the releases directory, which is already in place. If that rename fails,
#     the original tree is restored and the service is brought back up.
# ---------------------------------------------------------------------------
convert_mode() {
  local tag="$1"
  [ -L "$APP_LINK" ] && die "convert: $APP_LINK is already a symlink; nothing to convert"
  [ -d "$APP_LINK" ] || die "convert: $APP_LINK is not a directory"
  # SHARED_DIR/RELEASES_DIR must sit outside the code tree, otherwise moving
  # the tree would move them along with it.
  case "$SHARED_DIR/" in
    "$APP_LINK"/*) die "convert: SHARED_DIR='$SHARED_DIR' must NOT be inside $APP_LINK" ;;
  esac
  case "$RELEASES_DIR/" in
    "$APP_LINK"/*) die "convert: RELEASES_DIR='$RELEASES_DIR' must NOT be inside $APP_LINK" ;;
  esac

  log "=== convert: bootstrapping the symlink layout ==="
  systemctl stop "$SERVICE" || die "convert: cannot stop $SERVICE"
  log "convert: $SERVICE stopped"

  local item
  mkdir -p "$SHARED_DIR"
  for item in "${SHARED_ITEMS[@]}"; do
    if [ -e "${APP_LINK}/${item}" ] && [ ! -L "${APP_LINK}/${item}" ]; then
      mv -T "${APP_LINK}/${item}" "${SHARED_DIR}/${item}" \
        || { restore_app_link; systemctl start "$SERVICE"; die "convert: cannot move ${item} into shared storage"; }
      log "convert: moved ${item} into shared storage"
    fi
  done
  restore_app_link

  # systemd and nginx see identical paths before and after, so nothing is edited
  systemctl start "$SERVICE" || { restore_app_link; die "convert: cannot start $SERVICE"; }
  log "convert: $SERVICE started through the shared symlinks"
  if ! health_check; then
    log "convert: portal is NOT healthy yet — the code tree is untouched and still at $APP_LINK"
    log "convert: check journalctl -u $SERVICE, then re-run: deploy.sh --convert $tag"
    exit 1
  fi

  mkdir -p "${RELEASES_DIR}/${tag}"
  if ! mv -T "$APP_LINK" "${RELEASES_DIR}/${tag}"; then
    log "convert: renaming the code tree failed — restoring it"
    rmdir "${RELEASES_DIR}/${tag}" 2>/dev/null || true
    ln -sfn "${RELEASES_DIR}/${tag}" "$APP_LINK"   # only safe if the rename really did not happen
    die "convert: could not move $APP_LINK into $RELEASES_DIR"
  fi
  ln -sfn "${RELEASES_DIR}/${tag}" "$APP_LINK"
  # .env must stay reachable at the stable path in its own right: the portal's
  # EnvironmentFile and this script both read $APP_LINK/.env
  ln -sfn "${SHARED_DIR}/.env" "${APP_LINK}/.env"

  echo "$tag" > "$LAST_TAG_FILE"
  log "convert: ${APP_LINK} -> $(readlink -f "$APP_LINK")"
  log "=== convert OK: recorded ${tag} as the live release ==="
  log "next: bash $GATEWAY_DIR/deploy.sh --check, then enable the timer"
  exit 0
}

if [ "$MODE" = "convert" ]; then
  convert_mode "$FORCED_TAG"
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
  log "check: health checks = $HEALTH_CHECKS"
  log "check: remote tags reachable:"
  remote_tags | sed 's/^/        /' || true
  log "check: OK (nothing was changed)"
  exit 0
fi

# ---------------------------------------------------------------------------
# 3. pick the target tag
# ---------------------------------------------------------------------------
LATEST="$(remote_tags | tail -1 || true)"
# NOTE: an `[ -n ... ] && var=...` one-liner is fatal here — the variable
# assignment would be the last command, so a false test exits the script under
# `set -e` right when a tag was passed on the command line.
if [ -n "$FORCED_TAG" ]; then
  LATEST="$FORCED_TAG"
fi
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
if [ "$DRY_RUN" = "1" ]; then
  log "DRY_RUN=1: would deploy ${LATEST}; stopping here"
  exit 0
fi

RELEASE_DIR="${RELEASES_DIR}/${LATEST}"
BUILD_DIR="${TORCH_DIR}/build-${LATEST}"
WHEELHOUSE="${TORCH_DIR}/wheels-${LATEST}"
rm -rf "$BUILD_DIR" "$WHEELHOUSE"
mkdir -p "$BUILD_DIR" "$WHEELHOUSE"

FAIL_REASON=""
flip_back() {
  local prev rc
  prev="$(cat "$PREV_LINK_FILE" 2>/dev/null || true)"
  [ -n "$prev" ] || { log "no previous release recorded — cannot auto-rollback"; return 1; }
  ln -sfn "$prev" "${APP_LINK}.new" && mv -Tf "${APP_LINK}.new" "$APP_LINK"
  systemctl restart "$SERVICE"
  # systemctl returns as soon as the process is started, which is a second or
  # two before gunicorn answers. Without waiting here there is a brief window
  # where the portal returns 502 to real users; a rollback must not leave one.
  if health_check quiet; then
    log "ROLLED BACK to $(basename "$prev") (serving again)"
    return 0
  else
    rc=$?
    log "ROLLED BACK to $(basename "$prev") but it is NOT healthy either — manual attention required"
    return "$rc"
  fi
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
  # codeload generates a tarball on first request, so a tag pushed a minute ago
  # can answer 404 or an empty body for a while. Resolve the commit too: the
  # SHA endpoint is generated on demand the same way but is a second,
  # independent path to the same bytes, which makes the retry meaningful rather
  # than just a repeat of the same request.
  local sha
  sha="$(git ls-remote "$DEPLOY_REPO_URL" "refs/tags/${LATEST}" 2>/dev/null \
         | awk '$2 ~ /\^\{\}$/ {print $1; exit} $2 ~ /refs\/tags\// {first=$1} END {if (first != "") print first}')"
  [ -n "$sha" ] || log "WARNING: could not resolve ${LATEST} to a commit; falling back to the tag URL only"

  local attempt ok=0 u
  for attempt in 1 2 3 4 5 6; do
    # rebuild from the base every time: appending to $url in place would make
    # each retry longer than the last and the second one would never 404 cleanly
    if [ $((attempt % 2)) = 1 ] || [ -z "$sha" ]; then
      u="${url}/tar.gz/refs/tags/${LATEST}"
    else
      u="${url}/tar.gz/${sha}"
    fi
    if curl -fsSL --retry 3 --retry-delay 4 --retry-all-errors \
         --connect-timeout 20 -m "$GIT_TIMEOUT" -o "$tarball" "$u" \
       && [ -s "$tarball" ]; then
      ok=1
      log "downloaded release tarball on attempt ${attempt}"
      break
    fi
    log "download attempt ${attempt} failed: $u"
    sleep 5
  done
  if [ "$ok" != "1" ]; then
    die "download failed for ${LATEST} (commit ${sha:-unresolved}) — codeload did not serve the tarball"
  fi
  [ -s "$tarball" ] || die "downloaded tarball is empty"

  # create the extraction directory here: nothing else does, and `tar`
  # cannot create it itself
  rm -rf "$RELEASE_DIR.tmp"
  mkdir -p "$RELEASE_DIR.tmp"
  tar -xzf "$tarball" -C "$RELEASE_DIR.tmp" --strip-components=1
  log "extracted $(find "$RELEASE_DIR.tmp" -type f | wc -l) files"

  # sanity: this must really be the portal, not some other repo
  [ -f "$RELEASE_DIR.tmp/manage.py" ] || die "release has no manage.py — wrong repository?"
  [ -f "$RELEASE_DIR.tmp/requirements.txt" ] || log "WARNING: release has no requirements.txt"

  # record exactly what commit this tag points at, for the audit trail
  {
    echo "tag=${LATEST}"
    echo "commit=${sha:-unknown}"
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
  for item in "${SHARED_ITEMS[@]}"; do
    ln -sfn "${SHARED_DIR}/${item}" "$RELEASE_DIR/${item}"
  done
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
  # NOT $? — that is gzip's status. Write the pipeline status out explicitly so
  # this does not depend on how a given bash build treats assignments.
  local rc
  printf '%s' "${PIPESTATUS[0]}" > "${STATE_DIR}/.pgdump.rc"
  rc="$(cat "${STATE_DIR}/.pgdump.rc")"
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

# Every failure from here on has the same shape: the symlink is already pointing
# at the new release, so roll back, prove the portal is serving again, and only
# then leave a record. Recording BEFORE flip_back would risk systemd killing the
# run mid-rollback and leaving the portal on the bad release with no trace of why.
record_failure_and_die() {
  FAIL_REASON="$1"
  log "FAILED: ${FAIL_REASON}"
  flip_back || true
  { echo "deploy FAILED at $(date -u '+%Y-%m-%dT%H:%M:%SZ')"
    echo "target=${LATEST}"
    echo "reason=${FAIL_REASON}"
    echo "rolled_back_to=$(readlink -f "$APP_LINK")"
  } > "${STATE_DIR}/LAST_FAILURE"
  log "ERROR: deploy of ${LATEST} failed (${FAIL_REASON}); see ${STATE_DIR}/LAST_FAILURE"
  exit 1
}

( cd "$RELEASE_DIR" && set -a && . "$ENV_FILE" && set +a \
  && "${VENV}/bin/python" manage.py migrate --noinput >>"$BUILD_LOG" 2>&1 ) \
  || record_failure_and_die "migrate failed for ${LATEST} (see ${BUILD_LOG})"

( cd "$RELEASE_DIR" && set -a && . "$ENV_FILE" && set +a \
  && "${VENV}/bin/python" manage.py collectstatic --noinput --clear >>"$BUILD_LOG" 2>&1 ) \
  || record_failure_and_die "collectstatic failed for ${LATEST} (see ${BUILD_LOG})"

systemctl restart "$SERVICE" || record_failure_and_die "systemctl restart failed"
log "service restarted"

if health_check; then
  echo "$LATEST" > "$LAST_TAG_FILE"
  [ -n "$PREV_TARGET" ] && printf '%s\n' "$PREV_TARGET" > "$PREV_LINK_FILE"
  log "=== deploy OK: now running ${LATEST} ==="
else
  record_failure_and_die "health check failed after ${HEALTH_RETRIES} attempts"
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
