#!/usr/bin/env bash
# Commit what is staged and push it to main, rebasing onto a newer main first.
# Usage: scripts/push_with_retry.sh "commit message"
# Idempotent: nothing staged means nothing to commit, and the step exits 0.
# A data/* conflict keeps this run's freshly generated file. Any other conflict fails.
set -u
message="${1:?commit message required}"
branch="${PUSH_BRANCH:-main}"
attempts="${PUSH_ATTEMPTS:-3}"

if git diff --cached --quiet; then
  echo "Nothing to commit."
  exit 0
fi
git commit -q -m "$message"

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
