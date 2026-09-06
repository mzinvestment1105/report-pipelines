#!/usr/bin/env bash
#
# count_capped_failures.sh - 当日の失敗のうち「Claude 利用枠切れが原因のもの」を数える（設計案・未投入）。
#
# 配置先（投入時）: report-pipelines/.github/scripts/count_capped_failures.sh
#
# なぜ必要か:
#   standby_guard.sh の retry_cap は「当日 2 回失敗したら cron の自動リトライを止める」安全装置。
#   ところが枠切れ由来の失敗もこの 2 回に数えられるため、枠が戻ってからの自動復旧経路まで
#   自分で塞いでしまう（実測: 2026-09-04 run 33875929742 が
#   「本日すでに 2 回配信に失敗しており、cron の自動リトライ上限（2 回）に達した」で skip）。
#   枠切れは設備障害ではなく時間で必ず戻るため、一般の失敗とは別枠で数える必要がある。
#
# 判定材料に run ログを使わない理由:
#   ログ本文を見るには run ごとに zip をダウンロードして展開する必要があり、
#   guard の実行時間と API 使用量を大きく押し上げる。
#   代わりに、生成ジョブが枠切れを検知した時にマーカー用アーティファクトを 1 つ上げておき、
#   その有無だけを Artifacts API で数える（1 回の API 呼び出しで済む）。
#
# マーカーの命名（生成側と本スクリプトで一致させること）:
#   spending-cap-<TARGET_DATE>-<run_id>       例: spending-cap-2026-09-04-33870602080
#
# 使い方:
#   bash .github/scripts/count_capped_failures.sh <target_date> [self_run_id]
#
# 必要な環境変数: GH_TOKEN（actions:read 権限）・GITHUB_REPOSITORY
#
# 標準出力: 枠切れ由来の失敗数（整数 1 行）
# 標準エラー: 内訳（GHA ログ用）
#
set -euo pipefail

TARGET_DATE="${1:?usage: count_capped_failures.sh <target_date> [self_run_id]}"
SELF_RUN_ID="${2:-0}"
REPO="${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}"

PREFIX="spending-cap-${TARGET_DATE}-"

# Artifacts API は期限切れ（expired）のものも返すため、有効なものだけを数える。
# 自分自身の run が上げたマーカーは除外する（自分の失敗で自分を止めないため）。
ARTIFACTS=$(gh api -X GET "repos/${REPO}/actions/artifacts" -f per_page=100 \
  --jq "[.artifacts[]
         | select(.expired == false)
         | select(.name | startswith(\"${PREFIX}\"))
         | select((.workflow_run.id // 0) != ${SELF_RUN_ID})
         | {name: .name, run: (.workflow_run.id // 0)}]" 2>/dev/null || echo '[]')

COUNT=$(echo "$ARTIFACTS" | jq 'length')

echo "[count_capped_failures] ${TARGET_DATE} prefix=${PREFIX} self=${SELF_RUN_ID} => ${COUNT} 件" >&2
echo "$ARTIFACTS" | jq -r '.[] | "[count_capped_failures]   \(.name) (run \(.run))"' >&2 || true

echo "$COUNT"
