#!/usr/bin/env bash
#
# standby_guard.sh - 「予備タイマー方式」の guard 本体（2026-08-05 新設）。
#
# 解決したい問題:
#   GitHub の schedule(cron) は実測 +1h45m〜+3h15m 遅れて着火する。従来は「本命（Cloudflare
#   Worker の workflow_dispatch）の 1 時間後」に予備 cron を置いていたが、遅延で本来の配信帯を
#   大きく外れ、時間帯ゲートに弾かれるか深夜配信になっていた。
#   そこで予備 cron は「十分早い時刻」に起票しておき、ジョブ内で目標時刻（WAKE_JST）まで待機して
#   から判定する。これで cron の着火時刻がぶれても配信の着地時刻が動かない。
#
# 動作:
#   1) schedule 起動なら WAKE_JST（JST HH:MM）まで sleep する。
#      ただし着火が想定外時間帯（WINDOW_START_JST 未満 / WINDOW_END_JST 超過）なら日付ズレ配信を
#      避けて skip する。
#   2) 起床後、本命 run が走行中なら最大 WAIT_MINUTES 分だけその完了を待つ（本命に配信を譲る）。
#   3) 配信ジョブ単位（check_delivery.sh）で本日の配信可否を判定し、$GITHUB_OUTPUT へ
#      proceed / reason / target_date を書く。
#   skip する場合も必ず理由をログへ出す（無言 skip の禁止）。
#
# 必須 env:
#   GH_TOKEN GITHUB_REPOSITORY GITHUB_OUTPUT
#   WF_FILE   : 対象 workflow ファイル名（例 pts_mover_report.yml）
#   SEND_JOB  : 実配信ジョブ名（例 build-and-send）
#   MY_RUN_ID : 自分の run id
#   EVENT     : github.event_name
#   WAKE_JST  : schedule 起動時の起床時刻 "HH:MM"（JST）
# 任意 env:
#   LANE_LABEL        : ログ用の経路名（既定 "予備"）
#   FORCE_RERUN       : "true" で重複ガードを無視
#   INPUT_DATE        : 対象日付の手動指定（空なら判定開始時点の JST 日付）
#   WINDOW_START_JST  : 既定 "12:00"（これより早い着火は日付ズレ扱いで skip）
#   WINDOW_END_JST    : 既定 "23:30"（これより遅い着火は日付ズレ扱いで skip）
#   WAIT_MINUTES      : 既定 30（本命 run の完了を待つ上限）
#   DISPATCH_OVERRIDES: "true" なら workflow_dispatch は配信済みでも実行する（朝刊系の既存仕様用）
#
set -euo pipefail

: "${GITHUB_REPOSITORY:?}"
: "${GITHUB_OUTPUT:?}"
WF_FILE="${WF_FILE:?WF_FILE is required}"
SEND_JOB="${SEND_JOB:?SEND_JOB is required}"
MY_RUN_ID="${MY_RUN_ID:?MY_RUN_ID is required}"
EVENT="${EVENT:?EVENT is required}"
LANE_LABEL="${LANE_LABEL:-予備}"
WINDOW_START_JST="${WINDOW_START_JST:-12:00}"
WINDOW_END_JST="${WINDOW_END_JST:-23:30}"
WAIT_MINUTES="${WAIT_MINUTES:-30}"
DISPATCH_OVERRIDES="${DISPATCH_OVERRIDES:-false}"

jst_min()  { echo $(( 10#$(TZ=Asia/Tokyo date +%H) * 60 + 10#$(TZ=Asia/Tokyo date +%M) )); }
jst_hhmm() { TZ=Asia/Tokyo date +%H:%M; }
to_min()   { echo $(( 10#${1%%:*} * 60 + 10#${1##*:} )); }
to_hhmm()  { printf '%02d:%02d' $(( $1 / 60 )) $(( $1 % 60 )); }

# 対象日は判定開始時点の JST 日付で固定する（配信が 24 時をまたいでも日付をズラさない）。
if [ -n "${INPUT_DATE:-}" ]; then
  TARGET_DATE="${INPUT_DATE}"
else
  TARGET_DATE=$(TZ=Asia/Tokyo date +%Y-%m-%d)
fi
echo "target_date=${TARGET_DATE}" >> "$GITHUB_OUTPUT"

if [ "$EVENT" = "workflow_dispatch" ] && [ "${FORCE_RERUN:-}" = "true" ]; then
  echo "force_rerun=true (手動): 重複ガードを無視して実行します"
  echo "proceed=true" >> "$GITHUB_OUTPUT"
  echo "reason=force_rerun (manual guard bypass)" >> "$GITHUB_OUTPUT"
  exit 0
fi

# ---------- 1) 予備タイマー: 起床時刻まで待機 ----------
if [ "$EVENT" = "schedule" ]; then
  WAKE=$(to_min "${WAKE_JST:?WAKE_JST is required for schedule runs}")
  W_START=$(to_min "$WINDOW_START_JST")
  W_END=$(to_min "$WINDOW_END_JST")
  NOW=$(jst_min)

  if [ "$NOW" -gt "$W_END" ] || [ "$NOW" -lt "$W_START" ]; then
    echo "skip 理由: cron が想定外の時間帯（$(jst_hhmm) JST / 許容 ${WINDOW_START_JST}〜${WINDOW_END_JST}）に着火したため。日付ズレ配信を避けて中止します。"
    echo "proceed=false" >> "$GITHUB_OUTPUT"
    echo "reason=skip (cron outside window: $(jst_hhmm) JST)" >> "$GITHUB_OUTPUT"
    exit 0
  fi

  if [ "$NOW" -lt "$WAKE" ]; then
    SEC=$(( (WAKE - NOW) * 60 ))
    echo "${LANE_LABEL}: 現在 $(jst_hhmm) JST → 起床予定 $(to_hhmm "$WAKE") JST まで ${SEC} 秒待機します（本命の発火を先に通すため）"
    sleep "$SEC"
    echo "${LANE_LABEL}: 起床しました（$(jst_hhmm) JST）"
  else
    echo "${LANE_LABEL}: 現在 $(jst_hhmm) JST（起床予定 $(to_hhmm "$WAKE") を過ぎているため即判定）"
  fi
fi

# ---------- 2) 配信状況の取得 ----------
read_stats() {
  STATS=$(bash "$(dirname "$0")/check_delivery.sh" "$WF_FILE" "$SEND_JOB" "$MY_RUN_ID")
  DELIVERED=$(echo "$STATS" | jq -r '.delivered')
  SENDING=$(echo "$STATS" | jq -r '.sending')
  DISPATCH_PENDING=$(echo "$STATS" | jq -r '.dispatch_pending')
  FAILED=$(echo "$STATS" | jq -r '.failed')
}
read_stats

# ---------- 3) 本命 run の走行中は完了を待つ ----------
if [ "$EVENT" = "schedule" ]; then
  DEADLINE=$(( $(date +%s) + WAIT_MINUTES * 60 ))
  while [ "$DELIVERED" -eq 0 ] && { [ "$SENDING" -gt 0 ] || [ "$DISPATCH_PENDING" -gt 0 ]; }; do
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      echo "本命 run の完了待ちが ${WAIT_MINUTES} 分を超えたため待機を打ち切り、${LANE_LABEL}で配信します（配信絶対の原則）。"
      break
    fi
    echo "本命 run が走行中（sending=${SENDING} dispatch_pending=${DISPATCH_PENDING}）。60 秒後に再確認します。"
    sleep 60
    read_stats
  done
fi

# ---------- 4) 判定 ----------
echo "判定材料 (${TARGET_DATE} / $(jst_hhmm) JST / event=${EVENT}): delivered=${DELIVERED} sending=${SENDING} dispatch_pending=${DISPATCH_PENDING} failed=${FAILED}"

if [ "$DELIVERED" -gt 0 ] && [ "$EVENT" = "workflow_dispatch" ] && [ "$DISPATCH_OVERRIDES" = "true" ]; then
  echo "本日すでに配信済みですが、手動/Worker の dispatch は意図的発火のため再発行します。"
  echo "proceed=true" >> "$GITHUB_OUTPUT"
  echo "reason=manual_dispatch (override delivered=${DELIVERED})" >> "$GITHUB_OUTPUT"
elif [ "$DELIVERED" -gt 0 ]; then
  echo "skip 理由: 本日はすでに ${SEND_JOB} が成功済み（${DELIVERED} 件）＝配信が完了しているため。"
  echo "proceed=false" >> "$GITHUB_OUTPUT"
  echo "reason=skip (already delivered today: ${SEND_JOB} success=${DELIVERED})" >> "$GITHUB_OUTPUT"
elif [ "$SENDING" -gt 0 ]; then
  echo "skip 理由: 別 run が今まさに配信中（sending=${SENDING}）のため。"
  echo "proceed=false" >> "$GITHUB_OUTPUT"
  echo "reason=skip (another run is delivering: sending=${SENDING})" >> "$GITHUB_OUTPUT"
elif [ "$EVENT" = "schedule" ] && [ "$FAILED" -ge 2 ]; then
  echo "skip 理由: 本日すでに ${FAILED} 回配信に失敗しており、cron の自動リトライ上限（2 回）に達したため。"
  echo "proceed=false" >> "$GITHUB_OUTPUT"
  echo "reason=retry_cap (cron stopped: failed=${FAILED})" >> "$GITHUB_OUTPUT"
else
  echo "proceed=true" >> "$GITHUB_OUTPUT"
  if [ "$FAILED" -gt 0 ]; then
    echo "reason=recovery (failed=${FAILED})" >> "$GITHUB_OUTPUT"
  else
    echo "reason=initial" >> "$GITHUB_OUTPUT"
  fi
fi
