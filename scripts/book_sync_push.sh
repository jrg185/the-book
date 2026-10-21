#!/usr/bin/env bash
# Book-sync push. The desk writes data/rh_cash.json, stages it, and calls this.
# Fills newer than the sync cursor are upserted before the commit, so cash
# and open positions cannot publish apart.
# A missing argument is "chore: sync rh_cash <UTC timestamp>" from the clock,
# not a fixed date and not the retry script's staged-file default.
# Usage: scripts/book_sync_push.sh ["commit message"]
#        scripts/book_sync_push.sh --self-test
set -u

here=$(cd "$(dirname "$0")" && pwd)

sync_fills_since_cursor() {
  if [ -n "${BOOK_SYNC_FILL_CMD:-}" ]; then
    eval "$BOOK_SYNC_FILL_CMD"
    return
  fi
  python3 "$here/sync_rh_kpi_trades.py" --from-rh
}

if [ "${1-}" = "--self-test" ]; then
  root=$(mktemp -d)
  trap 'rm -rf "$root"' EXIT
  sync_line=$(grep -n 'sync_rh_kpi_trades.py' "$0" | head -1 | cut -d: -f1)
  push_line=$(grep -n 'push_with_retry.sh' "$0" | head -1 | cut -d: -f1)
  if [ -z "$sync_line" ] || [ -z "$push_line" ] || [ "$sync_line" -gt "$push_line" ]; then
    echo "self-test: fill sync is not before the push" >&2
    exit 1
  fi
  git init -q -b main "$root/work"
  git -C "$root/work" config user.email "book-sync-test@example.com"
  git -C "$root/work" config user.name "book-sync-test"
  git -C "$root/work" config commit.gpgsign false
  git -C "$root/work" config core.hooksPath /dev/null
  mkdir -p "$root/work/data"
  printf 'base\n' > "$root/work/data/rh_cash.json"
  git -C "$root/work" add data/rh_cash.json
  git -C "$root/work" commit -q -m seed
  before=$(git -C "$root/work" rev-parse HEAD)
  printf 'next\n' > "$root/work/data/rh_cash.json"
  git -C "$root/work" add data/rh_cash.json
  if (cd "$root/work" && BOOK_SYNC_FILL_CMD='exit 1' "$here/book_sync_push.sh" "should not land"); then
    echo "self-test: a failed fill sync still pushed" >&2
    exit 1
  fi
  if [ "$(git -C "$root/work" rev-parse HEAD)" != "$before" ]; then
    echo "self-test: a failed fill sync created a commit" >&2
    exit 1
  fi
  echo "book_sync_push self-test ok"
  trap - EXIT
  rm -rf "$root"
  export BOOK_SYNC_FILL_CMD=true
  exec "$here/push_with_retry.sh" --self-test
fi

if ! sync_fills_since_cursor; then
  echo "rh_cash push refused: fills newer than the sync cursor were not upserted" >&2
  exit 1
fi

if [ $# -ge 1 ] && [ -n "${1-}" ]; then
  message=$1
else
  stamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  message="chore: sync rh_cash ${stamp}"
fi

exec "$here/push_with_retry.sh" "$message"
