#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Controlled release-version override for the portal release workflow.
#
#   The release workflow derives the next semantic version from conventional
#   commits. For v1.9.2 the version is a product decision — the model fingerprint
#   feature is not a bugfix, and the automatic bump would publish 1.10.0 (or
#   1.9.1-patch.1) instead. This script is the single place that decides which
#   version the tag step is allowed to create, so the decision is testable
#   instead of living in YAML.
#
#   Rules:
#     * No `Release-As:` trailer in the commit message  -> mode=auto. The
#       existing conventional-commit behaviour is untouched.
#     * `Release-As: 1.9.2` (the `v` is optional)         -> mode=override.
#       The version is passed to the tag step verbatim; nothing bumps it.
#     * The override must be well formed, unique and strictly increasing:
#         - exactly X.Y.Z, no prerelease/build metadata, no leading zeroes
#         - not already an existing tag; if it is, it must point at this very
#           commit (mode=already-released, so a manual tag cannot be raced)
#         - strictly greater than every existing vX.Y.Z tag, otherwise a
#           downgrade or a duplicate release would be published
#     * Several different overrides in one message, or a version that equals or
#       precedes the live one, abort the workflow BEFORE any tag is pushed.
#
#   Usage:
#     release_version.sh [--commit <sha>] [--message-file <file>] [--tag-prefix v]
#
#   Output (stdout, one KEY=VALUE per line, ready for $GITHUB_OUTPUT):
#     mode=auto|override|already-released
#     version=1.9.2 | (empty for auto)
#     tag=v1.9.2    | (empty for auto)
#     previous=<latest existing tag or empty>
#     reason=<one sentence, safe to log>
#
#   Exit codes: 0 ok, 2 malformed/unusable override, 3 conflict with an existing
#   tag, 4 not an increasing version.
# ---------------------------------------------------------------------------
set -euo pipefail

PREFIX="v"
COMMIT=""
MESSAGE_FILE=""

err() { printf 'error: %s\n' "$*" >&2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --commit)       COMMIT="${2:-}"; [ -n "$COMMIT" ] || { err "--commit needs a value"; exit 2; }; shift 2 ;;
    --message-file) MESSAGE_FILE="${2:-}"; [ -n "$MESSAGE_FILE" ] || { err "--message-file needs a value"; exit 2; }; shift 2 ;;
    --tag-prefix)   PREFIX="${2:-}"; shift 2 ;;
    -h|--help)      sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)              err "unknown argument '$1'"; exit 2 ;;
  esac
done

git rev-parse --git-dir >/dev/null 2>&1 || { err "not inside a git worktree"; exit 2; }

emit() { printf '%s=%s\n' "$1" "$2"; }

# --- the commit message ----------------------------------------------------
if [ -n "$MESSAGE_FILE" ]; then
  [ -f "$MESSAGE_FILE" ] || { err "no such message file: $MESSAGE_FILE"; exit 2; }
  MESSAGE="$(cat "$MESSAGE_FILE")"
else
  [ -n "$COMMIT" ] || COMMIT="$(git rev-parse HEAD 2>/dev/null || true)"
  [ -n "$COMMIT" ] || { err "could not resolve a commit"; exit 2; }
  git cat-file -e "${COMMIT}^{commit}" 2>/dev/null || { err "unknown commit ${COMMIT}"; exit 2; }
  MESSAGE="$(git log -1 --format=%B "$COMMIT")"
fi

# Trailer syntax: `Release-As: 1.9.2`, at the start of a line, optional `v`,
# case-insensitive key. Anything else on the line (a sentence) does not count,
# so prose that merely mentions the token cannot move a version.
mapfile -t RAW < <(printf '%s\n' "$MESSAGE" \
  | sed -nE 's/^[[:space:]]*[Rr][Ee][Ll][Ee][Aa][Ss][Ee]-[Aa][Ss][[:space:]]*:[[:space:]]*([^[:space:]]+)[[:space:]]*$/\1/p')

if [ "${#RAW[@]}" -eq 0 ]; then
  emit mode auto
  emit version ""
  emit tag ""
  emit previous "$(git tag --list "${PREFIX}[0-9]*" | sed "s/^${PREFIX}//" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1)"
  emit reason "no Release-As trailer; conventional-commit versioning applies"
  exit 0
fi

# --- normalise and validate the requested version --------------------------
REQUESTED="${RAW[0]}"
for value in "${RAW[@]}"; do
  if [ "${value#"${PREFIX}"}" != "${REQUESTED#"${PREFIX}"}" ]; then
    err "conflicting Release-As trailers: ${REQUESTED} and ${value}"
    exit 2
  fi
done

TARGET="${REQUESTED#"${PREFIX}"}"
if ! printf '%s' "$TARGET" | grep -Eq '^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$'; then
  err "Release-As must be X.Y.Z (optionally prefixed by '${PREFIX}'), without leading zeroes, prerelease or build metadata (got '${REQUESTED}')"
  exit 2
fi
[ "$TARGET" != "0.0.0" ] || { err "Release-As 0.0.0 is not a releasable version"; exit 2; }

# --- compare against what already exists -----------------------------------
EXISTING="$([ -n "$PREFIX" ] && printf '%s' "${PREFIX}${TARGET}" || printf '%s' "$TARGET")"
LATEST="$(git tag --list "${PREFIX}[0-9]*" | sed "s/^${PREFIX}//" | grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -V | tail -1 || true)"

if git rev-parse -q --verify "refs/tags/${EXISTING}" >/dev/null 2>&1; then
  # The tag already exists. Accept it only when it already points at this commit:
  # that is the same release, not a second one, and refusing would make a re-run
  # of the workflow fail while a real conflict stays an error.
  if [ -z "$COMMIT" ] && [ -n "$MESSAGE_FILE" ]; then
    err "tag ${EXISTING} already exists and cannot be compared without --commit"
    exit 3
  fi
  tagged_commit="$(git rev-list -n 1 "refs/tags/${EXISTING}")"
  if [ "$tagged_commit" = "$(git rev-parse "${COMMIT}^{commit}")" ]; then
    emit mode already-released
    emit version "$TARGET"
    emit tag "$EXISTING"
    emit previous "$LATEST"
    emit reason "tag ${EXISTING} already exists on this commit"
    exit 0
  fi
  err "tag ${EXISTING} already exists on commit ${tagged_commit}, not on $COMMIT — refusing to move a published tag"
  exit 3
fi

if [ -n "$LATEST" ]; then
  highest="$(printf '%s\n%s\n' "$LATEST" "$TARGET" | sort -V | tail -1)"
  if [ "$highest" != "$TARGET" ]; then
    if [ "$highest" = "$LATEST" ] && [ "$LATEST" = "$TARGET" ]; then
      err "Release-As ${TARGET} is already the latest released version ${LATEST}"
    else
      err "Release-As ${TARGET} is not newer than the latest released version ${LATEST}"
    fi
    exit 4
  fi
fi

emit mode override
emit version "$TARGET"
emit tag "$EXISTING"
emit previous "${LATEST:-}"
emit reason "explicit Release-As override; the tag step must use this version verbatim"
