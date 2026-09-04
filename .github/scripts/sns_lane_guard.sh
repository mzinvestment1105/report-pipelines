#!/usr/bin/env bash
#
# sns_lane_guard.sh - SNS レーン別ツイート workflow の共通 guard 判定（2026-09-04 新設）。
#
# 位置づけ:
#   macro_tweet_guard.sh の判定構造をそのまま踏襲した汎用版。
#   マクロ市況ツイートは「夕刊マクロの完走を待つ」という固有の上流依存と
#   「夕方以降でないと意味がない」という時間帯ゲートを持つため専用スクリプトのままとし、
#   本ファイルは上流依存と時間帯を env で受け取る形にして AI レーン・D ラボレーンで共用する。
#
# 解決している欠陥（macro_tweet_guard.sh と同じ・2026-09-03 の実測に基づく）:
#   (a) GitHub cron の遅延（実測 +1h45m〜+3h15m）で JST 00:00〜04:59 に着火した schedule を
#       捨てず、「前日分の遅延着火」とみなして対象日を前日へ補正する。
#   (b) 当日すでに成功している場合は Claude を 1 度も呼ばずに skip する（トークン消費ゼロ）。
#   (c) 対象日の失敗が上限に達したら打ち切る（暴走防止）。
#
# 入力（env）:
#   必須: GITHUB_OUTPUT EVENT WORKFLOW_FILE SEND_JOB
#   任意: GH_TOKEN GITHUB_REPOSITORY MY_RUN_ID UPSTREAM_CONCLUSION FORCE_RERUN INPUT_DATE
#         REQUIRE_UPSTREAM  "true" のとき workflow_run 起点で上流 success を要求する（既定 true）
#         WINDOW_START_HOUR 生成が意味を持つ JST 時刻の下限（既定 0 = 時間帯ゲートなし）
#         LATE_NIGHT_END_HOUR 深夜遅延着火とみなす上限（既定 5）
#         FAIL_CAP          対象日あたりの失敗上限（既定 3）
#   検証用: NOW_OVERRIDE  "YYYY-MM-DD HH:MM" 形式の疑似 JST 時刻
#           STATS_OVERRIDE check_delivery.sh を呼ばずに使う JSON
#
# 出力: $GITHUB_OUTPUT へ proceed / reason / target_date / done_ok / done_fail / active
#
set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
EVENT="${EVENT:?EVENT is required}"
WORKFLOW_FILE="${WORKFLOW_FILE:?WORKFLOW_FILE is required}"
SEND_JOB="${SEND_JOB:?SEND_JOB is required}"

MY_RUN_ID="${MY_RUN_ID:-0}"
UPSTREAM_CONCLUSION="${UPSTREAM_CONCLUSION:-}"
FORCE_RERUN="${FORCE_RERUN:-false}"
INPUT_DATE="${INPUT_DATE:-}"
REQUIRE_UPSTREAM="${REQUIRE_UPSTREAM:-true}"
WINDOW_START_HOUR="${WINDOW_START_HOUR:-0}"
LATE_NIGHT_END_HOUR="${LATE_NIGHT_END_HOUR:-5}"
FAIL_CAP="${FAIL_CAP:-3}"

# ---------- 現在時刻（検証時は NOW_OVERRIDE で注入） ----------
if [ -n "${NOW_OVERRIDE:-}" ]; then
  NOW_DATE="${NOW_OVERRIDE%% *}"
  NOW_HOUR=$((10#$(echo "${NOW_OVERRIDE##* }" | cut -d: -f1)))
else
  NOW_DATE=$(TZ=Asia/Tokyo date +%Y-%m-%d)
  NOW_HOUR=$((10#$(TZ=Asia/Tokyo date +%H)))
fi

emit() { echo "$1=$2" >> "$GITHUB_OUTPUT"; }

# ---------- 0) 対象日の決定 ----------
DATE_NOTE="today"
if [ -n "$INPUT_DATE" ]; then
  TARGET_DATE="$INPUT_DATE"
  DATE_NOTE="manual"
elif [ "$EVENT" = "schedule" ] && [ "$NOW_HOUR" -lt "$LATE_NIGHT_END_HOUR" ]; then
  TARGET_DATE=$(date -u -d "${NOW_DATE} -1 day" +%Y-%m-%d 2>/dev/null \
                || date -u -j -f %Y-%m-%d -v-1d "${NOW_DATE}" +%Y-%m-%d)
  DATE_NOTE="late_night_shift (fired ${NOW_HOUR}h JST on ${NOW_DATE})"
else
  TARGET_DATE="${NOW_DATE}"
fi

emit target_date "$TARGET_DATE"
echo "guard: wf=${WORKFLOW_FILE} event=${EVENT} now=${NOW_DATE} ${NOW_HOUR}h JST target_date=${TARGET_DATE} (${DATE_NOTE})"

# ---------- 1) 強制実行 / 上流失敗 / 時間帯 ----------
if [ "$EVENT" = "workflow_dispatch" ] && [ "$FORCE_RERUN" = "true" ]; then
  emit proceed true
  emit reason "force_rerun (manual guard bypass)"
  exit 0
fi

if [ "$EVENT" = "workflow_run" ] && [ "$REQUIRE_UPSTREAM" = "true" ] \
   && [ "$UPSTREAM_CONCLUSION" != "success" ]; then
  emit proceed false
  emit reason "skip (upstream conclusion=${UPSTREAM_CONCLUSION})"
  exit 0
fi

# 時間帯ゲート。WINDOW_START_HOUR=0 なら無効（AI・D ラボは素材が朝に揃うため既定で無効）。
if [ "$WINDOW_START_HOUR" -gt 0 ] && [ "$EVENT" = "schedule" ] \
   && [ "$NOW_HOUR" -ge "$LATE_NIGHT_END_HOUR" ] \
   && [ "$NOW_HOUR" -lt "$WINDOW_START_HOUR" ]; then
  emit proceed false
  emit reason "skip (cron outside window: ${NOW_HOUR}h JST < ${WINDOW_START_HOUR}h)"
  exit 0
fi

# ---------- 2) 対象日の実績を集計 ----------
if [ -n "${STATS_OVERRIDE:-}" ]; then
  STATS="$STATS_OVERRIDE"
else
  STATS=$(TARGET_DATE_JST="$TARGET_DATE" UNTIL_JST_END=true \
          bash "$(dirname "$0")/check_delivery.sh" "$WORKFLOW_FILE" "$SEND_JOB" "${MY_RUN_ID}")
fi

DONE_OK=$(echo "$STATS" | jq -r '.delivered')
DONE_FAIL=$(echo "$STATS" | jq -r '.failed')
ACTIVE=$(( $(echo "$STATS" | jq -r '.sending') \
         + $(echo "$STATS" | jq -r '.dispatch_pending') \
         + $(echo "$STATS" | jq -r '.schedule_pending') ))

echo "guard: target=${TARGET_DATE} active=${ACTIVE} done_ok=${DONE_OK} done_fail=${DONE_FAIL} (excluding self ${MY_RUN_ID})"
emit active "$ACTIVE"
emit done_ok "$DONE_OK"
emit done_fail "$DONE_FAIL"

# ---------- 3) 判定 ----------
if [ "$DONE_OK" -gt 0 ]; then
  emit proceed false
  emit reason "skip (already succeeded for ${TARGET_DATE}: done_ok=${DONE_OK})"
elif [ "$ACTIVE" -gt 0 ]; then
  emit proceed false
  emit reason "skip (active run exists: active=${ACTIVE})"
elif [ "$DONE_FAIL" -ge "$FAIL_CAP" ]; then
  emit proceed false
  emit reason "retry_cap (stopped: done_fail=${DONE_FAIL} >= ${FAIL_CAP})"
else
  emit proceed true
  if [ "$DONE_FAIL" -gt 0 ]; then
    emit reason "recovery (done_fail=${DONE_FAIL} for ${TARGET_DATE})"
  else
    emit reason "initial"
  fi
fi
