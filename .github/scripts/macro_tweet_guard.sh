#!/usr/bin/env bash
#
# macro_tweet_guard.sh - マクロ市況ツイート日次の guard 判定本体（2026-09-04 新設）。
#
# なぜ yml から切り出したか:
#   判定式を yml のインライン shell に書いたままだと、深夜着火・救援・上限打ち切りといった
#   分岐を実運用の発火待ちでしか検証できない。ここに出しておけば NOW_OVERRIDE で
#   疑似時刻を注入してローカルで単体検証できる（本ファイルの検証は 3 ケースで実施済み）。
#
# 解決している 2026-09-03 の実測欠陥:
#   (a) schedule が遅延して JST 00:15 に着火すると、時間帯ゲート（17 時未満は skip）に弾かれ、
#       救援枠が救援として機能しなかった。さらに対象日が「当日 = 翌 JST 日」になり、
#       前日の失敗 run を集計対象から外していた（実測 failed=0。正しくは failed=1）。
#       → JST 00:00〜04:59 の schedule 着火は「前日分の遅延着火」とみなし、
#         対象日を前日へ補正したうえで判定を続行する。
#   (b) 上限が done_fail>=2 かつ schedule 起点限定だった。
#       → 経路を問わず done_fail>=3 で打ち切る（暴走防止は残しつつ救援の芽を潰さない）。
#
# 入力（env）:
#   必須: GH_TOKEN GITHUB_REPOSITORY GITHUB_OUTPUT MY_RUN_ID EVENT
#   任意: UPSTREAM_CONCLUSION FORCE_RERUN INPUT_DATE
#   検証用: NOW_OVERRIDE  "YYYY-MM-DD HH:MM" 形式の疑似 JST 時刻（未指定なら実時刻）
#           STATS_OVERRIDE  check_delivery.sh を呼ばずに使う JSON（単体検証用）
#
# 出力: $GITHUB_OUTPUT へ proceed / reason / target_date / done_ok / done_fail / active
#
set -euo pipefail

: "${GITHUB_OUTPUT:?GITHUB_OUTPUT is required}"
EVENT="${EVENT:?EVENT is required}"
MY_RUN_ID="${MY_RUN_ID:-0}"
UPSTREAM_CONCLUSION="${UPSTREAM_CONCLUSION:-}"
FORCE_RERUN="${FORCE_RERUN:-false}"
INPUT_DATE="${INPUT_DATE:-}"

# 生成が意味を持つ JST の時間帯。夕刊が素材なので夕方以降。
EVENING_START_HOUR=17
# 遅延着火とみなす深夜帯の上限（この時刻未満なら「前日分の遅れた発火」と解釈する）。
LATE_NIGHT_END_HOUR=5
# 対象日あたりの失敗回数の上限（これ以上は Claude を呼ばずに打ち切る）。
FAIL_CAP=3

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
# 手動指定が最優先。次に「深夜着火した schedule は前日分」の補正。それ以外は当日。
DATE_NOTE="today"
if [ -n "$INPUT_DATE" ]; then
  TARGET_DATE="$INPUT_DATE"
  DATE_NOTE="manual"
elif [ "$EVENT" = "schedule" ] && [ "$NOW_HOUR" -lt "$LATE_NIGHT_END_HOUR" ]; then
  # GitHub cron の遅延（実測 +1h45m〜+3h15m）で日付をまたいだ着火。
  # 捨てずに前日分の救援として使う。
  TARGET_DATE=$(date -u -d "${NOW_DATE} -1 day" +%Y-%m-%d 2>/dev/null \
                || date -u -j -f %Y-%m-%d -v-1d "${NOW_DATE}" +%Y-%m-%d)
  DATE_NOTE="late_night_shift (fired ${NOW_HOUR}h JST on ${NOW_DATE})"
else
  TARGET_DATE="${NOW_DATE}"
fi

emit target_date "$TARGET_DATE"
echo "guard: event=${EVENT} now=${NOW_DATE} ${NOW_HOUR}h JST target_date=${TARGET_DATE} (${DATE_NOTE})"

# ---------- 1) 強制実行 / 上流失敗 ----------
if [ "$EVENT" = "workflow_dispatch" ] && [ "$FORCE_RERUN" = "true" ]; then
  emit proceed true
  emit reason "force_rerun (manual guard bypass)"
  exit 0
fi

# workflow_run 起点は上流（夕刊マクロ）が success の時のみ先へ進む。
# 夕刊本文がツイートの第一ソースであり、無ければ後段 precheck でどうせ止まるため
# ここで打ち切って Claude を起動しない（＝トークンを使わない）。
if [ "$EVENT" = "workflow_run" ] && [ "$UPSTREAM_CONCLUSION" != "success" ]; then
  emit proceed false
  emit reason "skip (upstream macro_report_evening conclusion=${UPSTREAM_CONCLUSION})"
  exit 0
fi

# 深夜でも夕方でもない schedule（JST 05:00〜16:59）は、対象日の判断がつかないため従来どおり skip。
if [ "$EVENT" = "schedule" ] \
   && [ "$NOW_HOUR" -ge "$LATE_NIGHT_END_HOUR" ] \
   && [ "$NOW_HOUR" -lt "$EVENING_START_HOUR" ]; then
  emit proceed false
  emit reason "skip (cron outside evening window: ${NOW_HOUR}h JST)"
  exit 0
fi

# ---------- 2) 対象日の実績を集計 ----------
# 実際に生成/送信を行うジョブ（generate）の結果だけを見る
# （guard が本体を skip した run まで success と数える誤認を避けるため）。
# TARGET_DATE_JST を渡すことで、深夜補正後は「前日分」を正しく数えられる。
if [ -n "${STATS_OVERRIDE:-}" ]; then
  STATS="$STATS_OVERRIDE"
else
  STATS=$(TARGET_DATE_JST="$TARGET_DATE" UNTIL_JST_END=true \
          bash "$(dirname "$0")/check_delivery.sh" macro_tweet_daily.yml generate "${MY_RUN_ID}")
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
  # ここが「トークンを無駄にしない」の要。成功済みの日は cron 枠が何本あっても
  # この分岐で終わり、Claude を 1 度も呼ばない。
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
