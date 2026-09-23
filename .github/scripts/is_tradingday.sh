#!/usr/bin/env bash
# is_tradingday.sh [YYYY-MM-DD]
#   Japan-market trading-day check (same rule as the Cloudflare Worker's holiday block).
#   Default date = today in JST. Exit 0 = trading day (Mon-Fri and not listed in
#   jp_holidays.txt), exit 1 = market closed, exit 2 = bad argument / missing file.
#   Prints one line: tradingday=true|false reason=<ok|weekend|holiday:<date>>
#   Pure bash + grep + date (no python) so guard jobs can run it before any setup.
set -u
HOLIDAY_FILE="${JP_HOLIDAYS_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/jp_holidays.txt}"
D="${1:-$(TZ=Asia/Tokyo date +%F)}"

if ! [[ "$D" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
  echo "is_tradingday: invalid date '$D' (expected YYYY-MM-DD)" >&2
  exit 2
fi
if [ ! -f "$HOLIDAY_FILE" ]; then
  echo "is_tradingday: holiday file not found: $HOLIDAY_FILE" >&2
  exit 2
fi

DOW="$(date -d "$D" +%u 2>/dev/null || true)"   # 1=Mon .. 7=Sun
if [ -z "$DOW" ]; then
  echo "is_tradingday: cannot parse date '$D'" >&2
  exit 2
fi

if [ "$DOW" -ge 6 ]; then
  echo "tradingday=false reason=weekend"
  exit 1
fi
if tr -d '\r' < "$HOLIDAY_FILE" | grep -qx -- "$D"; then
  echo "tradingday=false reason=holiday:$D"
  exit 1
fi
echo "tradingday=true reason=ok"
exit 0
