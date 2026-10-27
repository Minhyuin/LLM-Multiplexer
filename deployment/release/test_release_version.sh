#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Tests for release_version.sh — the gate that decides which version a release
# run is allowed to tag.
#
#   bash deployment/release/test_release_version.sh
#
# Every case builds a throwaway git repository in a temp directory, so nothing
# here reads or writes the portal repository, the network, or any tag that
# matters. Exit code 0 means every case matched its expected mode and exit code.
# ---------------------------------------------------------------------------
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="${HERE}/release_version.sh"
WORK="$(mktemp -d)"
trap 'rm -rf -- "$WORK"' EXIT

pass=0
fail=0

check() {     # check <name> <expected rc> <expected key=value>... -- <actual output> <actual rc>
  local name="$1" want_rc="$2" out="$3" got_rc="$4"
  shift 4
  local problem=""
  if [ "$got_rc" != "$want_rc" ]; then
    problem="exit code ${got_rc}, expected ${want_rc}"
  fi
  local want
  for want in "$@"; do
    if ! printf '%s\n' "$out" | grep -Fxq -- "$want"; then
      problem="${problem:+${problem}; }missing '${want}'"
    fi
  done
  if [ -n "$problem" ]; then
    fail=$((fail + 1))
    printf 'FAIL %s\n     %s\n     output:\n%s\n' "$name" "$problem" "$(printf '%s\n' "$out" | sed 's/^/       /')"
  else
    pass=$((pass + 1))
    printf 'ok   %s\n' "$name"
  fi
}

# new_repo <dir> [tag...] — a fresh repo with one commit, optionally tagged.
new_repo() {
  local dir="$1"; shift
  git init -q "$dir"
  git -C "$dir" config user.email test@example.invalid
  git -C "$dir" config user.name test
  printf 'x\n' > "$dir/f"
  git -C "$dir" add f
  git -C "$dir" commit -qm "feat: baseline"
  local tag
  for tag in "$@"; do
    git -C "$dir" tag "$tag"
  done
}

# commit_with <dir> <message> — a second commit carrying an arbitrary message.
commit_with() {
  local dir="$1" message="$2"
  printf '%s\n' "$message" > "$dir/msg"
  git -C "$dir" commit -q --allow-empty -F "$dir/msg"
}

# run_in <dir> <args...> — run the script with the repo as the working directory.
run_in() {
  local dir="$1"; shift
  ( cd "$dir" && bash "$SCRIPT" "$@" ) 2>&1
}

# --- auto mode -------------------------------------------------------------
REPO="$WORK/auto"
new_repo "$REPO" v1.9.1 v1.7.2 v1.8.0
out="$(run_in "$REPO")"; rc=$?
check "auto: no trailer, latest tag wins" 0 "$out" "$rc" \
  'mode=auto' 'version=' 'previous=1.9.1'

REPO="$WORK/auto-empty"
new_repo "$REPO"
out="$(run_in "$REPO")"; rc=$?
check "auto: repository without tags" 0 "$out" "$rc" 'mode=auto' 'previous='

REPO="$WORK/auto-prose"
new_repo "$REPO" v1.9.1
commit_with "$REPO" "feat: mention Release-As: 2.0.0 in prose but do not use it"
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "auto: prose mentioning the token is not a trailer" 0 "$out" "$rc" \
  'mode=auto' 'previous=1.9.1'

# --- override mode ---------------------------------------------------------
REPO="$WORK/override"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: fingerprint detection\n\nRelease-As: 1.9.2'
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "override: explicit version is used verbatim" 0 "$out" "$rc" \
  'mode=override' 'version=1.9.2' 'tag=v1.9.2' 'previous=1.9.1'

REPO="$WORK/override-prefixed"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: x\n\nRelease-As: v1.9.2'
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "override: an optional v prefix is normalised" 0 "$out" "$rc" \
  'mode=override' 'version=1.9.2' 'tag=v1.9.2'

REPO="$WORK/override-lowercase"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: x\n\nrelease-as:  1.9.2  '
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "override: key is case-insensitive, spaces tolerated" 0 "$out" "$rc" \
  'mode=override' 'version=1.9.2'

REPO="$WORK/override-first"
new_repo "$REPO"
commit_with "$REPO" $'feat: first release\n\nRelease-As: 0.1.0'
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "override: first release with no previous tag" 0 "$out" "$rc" \
  'mode=override' 'version=0.1.0' 'tag=v0.1.0' 'previous='

# --- already-released ------------------------------------------------------
REPO="$WORK/already"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: x\n\nRelease-As: 1.9.2'
git -C "$REPO" tag v1.9.2
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "already-released: tag exists on this very commit" 0 "$out" "$rc" \
  'mode=already-released' 'version=1.9.2' 'tag=v1.9.2'

# --- refusals --------------------------------------------------------------
REPO="$WORK/not-newer"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: x\n\nRelease-As: 1.8.0'
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "refuse: lower than the latest tag" 4 "$out" "$rc"

REPO="$WORK/equal"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: x\n\nRelease-As: 1.9.1'
git -C "$REPO" tag -d v1.9.1 >/dev/null
git -C "$REPO" tag v1.9.1 HEAD~1
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "refuse: tag exists on another commit" 3 "$out" "$rc"

REPO="$WORK/conflict"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: x\n\nRelease-As: 1.9.2'
git -C "$REPO" tag v1.9.2 HEAD~1
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "refuse: tag already published elsewhere" 3 "$out" "$rc"

REPO="$WORK/malformed"
new_repo "$REPO" v1.9.1
for bad in 1.9 1.9.2.1 01.9.2 1.9.2-rc1 1.9.2+build latest; do
  commit_with "$REPO" "feat: x"
  printf 'Release-As: %s\n' "$bad" > "$REPO/msg"
  out="$(run_in "$REPO" --message-file "$REPO/msg" --commit HEAD)"; rc=$?
  check "refuse: malformed version '${bad}'" 2 "$out" "$rc"
done

REPO="$WORK/zero"
new_repo "$REPO"
commit_with "$REPO" $'feat: x\n\nRelease-As: 0.0.0'
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "refuse: 0.0.0 is not releasable" 2 "$out" "$rc"

REPO="$WORK/two-trailers"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: x\n\nRelease-As: 1.9.2\nRelease-As: 1.9.3'
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "refuse: conflicting trailers" 2 "$out" "$rc"

REPO="$WORK/same-trailer-twice"
new_repo "$REPO" v1.9.1
commit_with "$REPO" $'feat: x\n\nRelease-As: 1.9.2\nRelease-As: 1.9.2'
out="$(run_in "$REPO" --commit HEAD)"; rc=$?
check "allow: the same trailer twice is not a conflict" 0 "$out" "$rc" \
  'mode=override' 'version=1.9.2'

REPO="$WORK/bad-commit"
new_repo "$REPO"
out="$(run_in "$REPO" --commit no-such-ref)"; rc=$?
check "refuse: unresolvable commit" 2 "$out" "$rc"

out="$( cd "$WORK" && bash "$SCRIPT" --commit HEAD )"; rc=$?
check "refuse: not inside a worktree" 2 "$out" "$rc"

printf '\n%d passed, %d failed\n' "$pass" "$fail"
[ "$fail" -eq 0 ]
