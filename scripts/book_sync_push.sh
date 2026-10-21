#!/usr/bin/env bash
# Book-sync push. The desk writes data/rh_cash.json, stages it, and calls this.
# Always passes an explicit subject into scripts/push_with_retry.sh.
# A missing argument is "chore: sync rh_cash <UTC timestamp>" from the clock,
# not a fixed date and not the retry script's staged-file default.
# Usage: scripts/book_sync_push.sh ["commit message"]
#        scripts/book_sync_push.sh --self-test
set -u

here=$(cd "$(dirname "$0")" && pwd)

if [ "${1-}" = "--self-test" ]; then
  exec "$here/push_with_retry.sh" --self-test
fi

if [ $# -ge 1 ] && [ -n "${1-}" ]; then
  message=$1
else
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  message="chore: sync rh_cash ${stamp}"
fi

exec "$here/push_with_retry.sh" "$message"
