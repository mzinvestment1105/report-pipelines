#!/usr/bin/env bash
#
# check_delivery.sh - 「本日もう配信できたか」を run 単位ではなく配信ジョブ単位で判定する。
#
# 背景（2026-08-05 修正）:
#   従来の guard は同一 workflow の当日 run を集計し conclusion=="success" を「本日配信済み」と
#   数えていた。ところが guard が時間帯ゲート等で本体ジョブを skip した run も run 全体としては
#   success で終わるため、「配信していないのに配信済み」と誤認する。
#   夜間PTS では GitHub cron の遅延着火（+1h45m〜+3h15m）で予備 run が深夜に走り、
#   時間帯ゲートで本体 skip → run は success → 翌日 21:00 の本命発火がこの残骸を見て無言 skip、
#   という連鎖で平日のほとんどが未配信になっていた。
#   そのため run ではなく「配信ジョブ（send_job）の結果」だけを見る。
#
# 使い方:
#   bash .github/scripts/check_delivery.sh <workflow_file> <send_job> [self_run_id] [since_iso8601]
#     workflow_file : 例 pts_mover_report.yml
#     send_job      : 配信/更新を実際に行うジョブ名（例 build-and-send / merge-and-send / generate / update）
#     self_run_id   : 集計から除外する自分の run id（省略/0 で除外なし）
#     since_iso8601 : 集計開始時刻（省略時は当日 00:00 JST）
#
# 必要な環境変数: GH_TOKEN（actions:read 権限）・GITHUB_REPOSITORY
#
# 標準出力: 1 行の JSON
#   {"today":"YYYY-MM-DD","since":"...","total":N,"delivered":N,"sending":N,
#    "dispatch_pending":N,"schedule_pending":N,"failed":N}
#     delivered        : send_job が success で終わった run 数（＝実際に配信できた回数）
#     sending          : send_job が今まさに queued/in_progress の run 数
#     dispatch_pending : workflow_dispatch 起点でまだ完了しておらず未配信の run 数（本命が走行中）
#     schedule_pending : schedule 起点でまだ完了しておらず未配信の run 数（予備が走行中/待機中）
#     failed           : send_job が failure/cancelled/timed_out で終わった run 数（+ startup_failure の run）
# 標準エラー: run ごとの内訳（GHA ログ用）
#
set -euo pipefail

WF="${1:?usage: check_delivery.sh <workflow_file> <send_job> [self_run_id] [since_iso8601]}"
SEND_JOB="${2:?usage: check_delivery.sh <workflow_file> <send_job> [self_run_id] [since_iso8601]}"
SELF_RUN_ID="${3:-0}"
SINCE="${4:-}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

TODAY_JST=$(TZ=Asia/Tokyo date +%Y-%m-%d)
if [ -z "$SINCE" ]; then
  SINCE="${TODAY_JST}T00:00:00+09:00"
fi

RUNS=$(gh api -X GET "repos/${REPO}/actions/workflows/${WF}/runs" \
  -f "created=>=${SINCE}" \
  -f per_page=50 \
  --jq "[.workflow_runs[] | select(.id != ${SELF_RUN_ID}) | {id: .id, event: .event, status: .status, conclusion: (.conclusion // \"none\")}]")

TOTAL=$(echo "$RUNS" | jq 'length')
DELIVERED=0
SENDING=0
DISPATCH_PENDING=0
SCHEDULE_PENDING=0
FAILED=0

echo "[check_delivery] ${WF} send_job=${SEND_JOB} since=${SINCE} runs=${TOTAL} (self ${SELF_RUN_ID} を除外)" >&2

while IFS=$'\t' read -r RID REV RST RCC; do
  [ -n "${RID:-}" ] || continue

  if [ "$RCC" = "startup_failure" ]; then
    FAILED=$((FAILED + 1))
    echo "[check_delivery]   run ${RID} (${REV}/${RST}/${RCC}): 配信ジョブ未起動（startup_failure）" >&2
    continue
  fi

  JOB=$(gh api -X GET "repos/${REPO}/actions/runs/${RID}/jobs" -f per_page=100 \
        --jq "[.jobs[] | select(.name == \"${SEND_JOB}\")] | .[-1] // {}" 2>/dev/null || echo '{}')
  JOB_STATUS=$(echo "$JOB" | jq -r '.status // "absent"')
  JOB_CONCLUSION=$(echo "$JOB" | jq -r '.conclusion // "none"')

  case "$JOB_CONCLUSION" in
    success)
      DELIVERED=$((DELIVERED + 1))
      ;;
    failure|cancelled|timed_out)
      FAILED=$((FAILED + 1))
      ;;
  esac

  case "$JOB_STATUS" in
    queued|in_progress|waiting|pending|requested)
      SENDING=$((SENDING + 1))
      ;;
  esac

  # 未完了 run のうち、まだ配信できていないもの（本命 or 予備が進行中）を経路別に数える
  if [ "$RST" != "completed" ] && [ "$JOB_CONCLUSION" != "success" ]; then
    if [ "$REV" = "workflow_dispatch" ]; then
      DISPATCH_PENDING=$((DISPATCH_PENDING + 1))
    else
      SCHEDULE_PENDING=$((SCHEDULE_PENDING + 1))
    fi
  fi

  echo "[check_delivery]   run ${RID} (${REV}/${RST}/${RCC}): ${SEND_JOB}=${JOB_STATUS}/${JOB_CONCLUSION}" >&2
done <<EOF
$(echo "$RUNS" | jq -r '.[] | [(.id|tostring), .event, .status, .conclusion] | @tsv')
EOF

echo "[check_delivery] => delivered=${DELIVERED} sending=${SENDING} dispatch_pending=${DISPATCH_PENDING} schedule_pending=${SCHEDULE_PENDING} failed=${FAILED}" >&2

jq -nc \
  --arg today "$TODAY_JST" \
  --arg since "$SINCE" \
  --argjson total "$TOTAL" \
  --argjson delivered "$DELIVERED" \
  --argjson sending "$SENDING" \
  --argjson dispatch_pending "$DISPATCH_PENDING" \
  --argjson schedule_pending "$SCHEDULE_PENDING" \
  --argjson failed "$FAILED" \
  '{today: $today, since: $since, total: $total, delivered: $delivered, sending: $sending, dispatch_pending: $dispatch_pending, schedule_pending: $schedule_pending, failed: $failed}'
