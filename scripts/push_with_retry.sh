#!/usr/bin/env bash
# Commit what is staged and push it to main, rebasing onto a newer main first.
# Usage: scripts/push_with_retry.sh ["commit message"]
#        scripts/push_with_retry.sh --self-test
# A missing or empty message becomes "chore: sync <what> <UTC timestamp>".
# <what> is the staged basenames with the suffix stripped, sorted and unique.
# The timestamp is UTC at commit time, not a fixed date.
# Callers that pass a message keep that message, including no-changes and
# the rebase/retry loop:
#   .github/workflows/export-kpi.yml
#     "Export scrubbed KPI views to data JSON"
#     "Mark KPI export stale"
#   .github/workflows/og-card.yml
#     "Render daily og.png from data JSON"
#   scripts/book_sync_push.sh
#     always passes an explicit subject (book-sync at fddff28 did not)
# Idempotent: nothing staged means nothing to commit, and the step exits 0.
# A data/* conflict keeps this run's freshly generated file. Any other conflict fails.
set -u

default_sync_message() {
  local stamp names name base
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  names=""
  while IFS= read -r name; do
    [ -n "$name" ] || continue
    base=${name##*/}
    base=${base%.*}
    [ -n "$base" ] || continue
    case ",$names," in
      *",$base,"*) continue ;;
    esac
    if [ -z "$names" ]; then
      names=$base
    else
      names="$names,$base"
    fi
  done < <(git diff --cached --name-only | LC_ALL=C sort -u)
  if [ -z "$names" ]; then
    names="changes"
  fi
  printf 'chore: sync %s %s' "$names" "$stamp"
}

rebase_onto_remote() {
  git fetch -q origin "$branch" || return 1
  if git rebase -q "origin/$branch"; then
    return 0
  fi
  while [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; do
    conflicts=$(git diff --name-only --diff-filter=U)
    if [ -z "$conflicts" ]; then
      GIT_EDITOR=true git rebase --continue >/dev/null 2>&1 || { git rebase --abort; return 1; }
      continue
    fi
    for path in $conflicts; do
      case "$path" in
        data/*)
          # In a rebase "theirs" is the commit being replayed: this run's export.
          git checkout --theirs -- "$path" && git add -- "$path" ;;
        *)
          echo "Non-data conflict on $path; refusing to resolve automatically."
          git rebase --abort
          return 1 ;;
      esac
    done
    GIT_EDITOR=true git rebase --continue >/dev/null 2>&1 || true
  done
  return 0
}

push_retry_self_test() {
  local script caller bare work subject stamp now got delta today remote_readme
  script=$(readlink -f "$0")
  caller=$(dirname "$script")/book_sync_push.sh
  # Not local: the EXIT trap runs after this function's locals are gone.
  PUSH_RETRY_TEST_ROOT=$(mktemp -d)
  trap 'rm -rf "${PUSH_RETRY_TEST_ROOT:-}"' EXIT
  bare="$PUSH_RETRY_TEST_ROOT/origin.git"
  work="$PUSH_RETRY_TEST_ROOT/work"

  git init -q --bare "$bare"
  git --git-dir="$bare" symbolic-ref HEAD refs/heads/main
  git init -q -b main "$work"
  git -C "$work" config user.email "push-retry-test@example.com"
  git -C "$work" config user.name "push-retry-test"
  git -C "$work" config commit.gpgsign false
  git -C "$work" config core.hooksPath /dev/null
  mkdir -p "$work/data"
  printf 'seed\n' > "$work/README"
  printf 'base\n' > "$work/data/rh_cash.json"
  git -C "$work" add README data/rh_cash.json
  git -C "$work" commit -q -m "seed"
  git -C "$work" remote add origin "$bare"
  git -C "$work" push -q origin HEAD:main

  run_at() {
    local bin=$1
    shift
    (
      cd "$work"
      unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
      export PUSH_ATTEMPTS=1 PUSH_BRANCH=main
      "$bin" "$@"
    )
  }

  fail() {
    echo "self-test: $*" >&2
    exit 1
  }

  # Nothing staged exits 0 with or without a message, and does not commit.
  before=$(git -C "$work" rev-parse HEAD)
  out=$(run_at "$script" "explicit should not commit") || fail "no-changes with a message exited non-zero"
  printf '%s\n' "$out" | grep -q "Nothing to commit." || fail "no-changes with a message did not say so"
  [ "$(git -C "$work" rev-parse HEAD)" = "$before" ] || fail "no-changes with a message created a commit"
  out=$(run_at "$script") || fail "no-changes with no message exited non-zero"
  printf '%s\n' "$out" | grep -q "Nothing to commit." || fail "no-changes with no message did not say so"
  [ "$(git -C "$work" rev-parse HEAD)" = "$before" ] || fail "no-changes with no message created a commit"

  # With an argument, that subject is kept even when the staged file is rh_cash.
  printf 'explicit\n' > "$work/data/rh_cash.json"
  git -C "$work" add data/rh_cash.json
  run_at "$script" "rh_cash: book-sync refresh" || fail "with-argument push failed"
  subject=$(git -C "$work" log -1 --format=%s)
  [ "$subject" = "rh_cash: book-sync refresh" ] || fail "with-argument subject was '$subject'"
  [ "$(git --git-dir="$bare" log -1 --format=%s main)" = "$subject" ] || fail "with-argument commit was not pushed"

  # No argument builds chore: sync <staged basename> <UTC now>.
  printf '{"book":1}\n' > "$work/data/live_book.json"
  git -C "$work" add data/live_book.json
  run_at "$script" || fail "no-argument push failed"
  subject=$(git -C "$work" log -1 --format=%s)
  [[ "$subject" =~ ^chore:\ sync\ live_book\ [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
    || fail "no-argument subject was '$subject'"
  today=$(date -u +%Y-%m-%d)
  case "$subject" in
    *" $today"T*) ;;
    *) fail "no-argument timestamp is not today UTC: $subject" ;;
  esac
  stamp=${subject##* }
  now=$(date -u +%s)
  got=$(date -u -d "$stamp" +%s)
  delta=$((now - got))
  if [ "$delta" -lt 0 ]; then delta=$((-delta)); fi
  [ "$delta" -le 120 ] || fail "no-argument timestamp is not current: $subject"
  [ "$(git --git-dir="$bare" log -1 --format=%s main)" = "$subject" ] || fail "no-argument commit was not pushed"
  # A given message must not be rewritten into the default shape.
  case "$subject" in
    "rh_cash: book-sync refresh") fail "no-argument reused the previous explicit subject" ;;
  esac

  # Book-sync passes an explicit rh_cash subject even if something else is staged.
  printf 'other\n' > "$work/data/other.json"
  git -C "$work" add data/other.json
  run_at "$caller" || fail "book-sync caller failed"
  subject=$(git -C "$work" log -1 --format=%s)
  [[ "$subject" =~ ^chore:\ sync\ rh_cash\ [0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] \
    || fail "book-sync subject was '$subject'"
  case "$subject" in
    *other*) fail "book-sync fell through to the staged-file default: $subject" ;;
  esac
  stamp=${subject##* }
  now=$(date -u +%s)
  got=$(date -u -d "$stamp" +%s)
  delta=$((now - got))
  if [ "$delta" -lt 0 ]; then delta=$((-delta)); fi
  [ "$delta" -le 120 ] || fail "book-sync timestamp is not current: $subject"

  printf 'custom\n' >> "$work/data/other.json"
  git -C "$work" add data/other.json
  run_at "$caller" "rh_cash: hourly refresh (no new fills)" || fail "book-sync explicit argument failed"
  subject=$(git -C "$work" log -1 --format=%s)
  [ "$subject" = "rh_cash: hourly refresh (no new fills)" ] || fail "book-sync argument subject was '$subject'"

  # A newer main is rebased in before the push.
  other="$PUSH_RETRY_TEST_ROOT/other"
  git clone -q "$bare" "$other"
  git -C "$other" config user.email "push-retry-test@example.com"
  git -C "$other" config user.name "push-retry-test"
  git -C "$other" config commit.gpgsign false
  git -C "$other" config core.hooksPath /dev/null
  sync_other() {
    git -C "$other" fetch -q origin
    git -C "$other" reset -q --hard origin/main
  }
  printf 'remote-note\n' > "$other/data/note.json"
  git -C "$other" add data/note.json
  git -C "$other" commit -q -m "remote note"
  git -C "$other" push -q origin HEAD:main
  printf 'local-note\n' > "$work/data/local.json"
  git -C "$work" add data/local.json
  run_at "$script" "local after remote" || fail "rebase onto newer main failed"
  git -C "$work" merge-base --is-ancestor "$(git -C "$other" rev-parse HEAD)" HEAD \
    || fail "push did not rebase onto the newer main"
  [ "$(git --git-dir="$bare" log -1 --format=%s main)" = "local after remote" ] || fail "rebased commit was not pushed"
  [ -f "$work/data/note.json" ] || fail "rebase dropped the remote note"

  # A data/* conflict keeps this run's file.
  sync_other
  printf 'remote-cash\n' > "$other/data/rh_cash.json"
  git -C "$other" add data/rh_cash.json
  git -C "$other" commit -q -m "remote cash"
  git -C "$other" push -q origin HEAD:main
  printf 'local-cash\n' > "$work/data/rh_cash.json"
  git -C "$work" add data/rh_cash.json
  run_at "$script" "keep this run" || fail "data conflict was not resolved"
  [ "$(cat "$work/data/rh_cash.json")" = "local-cash" ] || fail "data conflict did not keep this run's file"
  git --git-dir="$bare" show main:data/rh_cash.json > "$PUSH_RETRY_TEST_ROOT/pushed-cash"
  [ "$(cat "$PUSH_RETRY_TEST_ROOT/pushed-cash")" = "local-cash" ] || fail "pushed cash is not this run's file"

  # Any other conflict fails and does not publish the local file.
  sync_other
  printf 'remote-readme\n' > "$other/README"
  git -C "$other" add README
  git -C "$other" commit -q -m "remote readme"
  git -C "$other" push -q origin HEAD:main
  printf 'local-readme\n' > "$work/README"
  git -C "$work" add README
  if run_at "$script" "should not publish"; then
    fail "non-data conflict succeeded"
  fi
  if [ -d "$(git -C "$work" rev-parse --git-path rebase-merge)" ] || [ -d "$(git -C "$work" rev-parse --git-path rebase-apply)" ]; then
    fail "rebase was left in progress after a non-data conflict"
  fi
  remote_readme=$(git --git-dir="$bare" show main:README)
  [ "$remote_readme" = "remote-readme" ] || fail "non-data conflict was pushed: '$remote_readme'"

  echo "push_with_retry self-test ok"
}

if [ "${1-}" = "--self-test" ]; then
  push_retry_self_test
  exit $?
fi

branch="${PUSH_BRANCH:-main}"
attempts="${PUSH_ATTEMPTS:-3}"

if git diff --cached --quiet; then
  echo "Nothing to commit."
  exit 0
fi

if [ $# -ge 1 ] && [ -n "$1" ]; then
  message=$1
else
  message=$(default_sync_message)
fi

echo "Commit: $message"
git commit -q -m "$message"

for attempt in $(seq 1 "$attempts"); do
  if rebase_onto_remote && git push origin "HEAD:$branch"; then
    echo "Pushed on attempt $attempt."
    exit 0
  fi
  echo "Push attempt $attempt failed; retrying."
  sleep $((attempt * 5))
done
echo "Push failed after $attempts attempts."
exit 1
