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
#      避けて skip する。2026-09-23 から、この時間帯判定の前に「本日すでに配信済みか」を見て、
#      配信済みなら通知なしで降りる（配信済みの日に「未配信」と通知する誤報の防止）。
#      時間帯外で降りる場合は window_side=early（受付開始前）/ late（受付終了後）も出力する。
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
#   CAP_AWARE_RETRY   : "true" で枠切れ由来の失敗を retry_cap から除外する（2026-09-07 追加・既定 false）
#   CAP_RETRY_LIMIT   : 枠切れ由来の失敗を許容する上限（既定 3）。これを超えたら cron を止める。
#   REQUIRE_TRADINGDAY: "1" で対象日が日本市場の非営業日（土日・祝日・東証年末年始）なら
#                       Discord 通知なしで proceed=false / reason=skip (market closed: ...) を返す
#                       （2026-09-23 追加・既定 off。判定は is_tradingday.sh + jp_holidays.txt）。
#
# 【2026-09-07 追加・CAP_AWARE_RETRY の背景】
#   従来の retry_cap は「当日 2 回失敗で cron の自動リトライ停止」だが、
#   Claude 利用枠切れ（Spending cap）由来の失敗も同じ 2 回に数えていた。
#   枠切れは設備障害ではなく時間が経てば必ず戻るため、これを数えると
#   枠が戻ってからの自動復旧経路を自分で塞ぐことになる。
#   実測: 2026-09-04 run 33875929742 が
#     「本日すでに 2 回配信に失敗しており、cron の自動リトライ上限（2 回）に達した」
#   で skip し、当日未配信のまま終わった。
#   そこで枠切れ由来を別枠（CAP_RETRY_LIMIT）で数え、実質失敗数だけを retry_cap に掛ける。
#   既定は false のため、この env を渡さない既存 workflow の挙動は一切変わらない。
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
CAP_AWARE_RETRY="${CAP_AWARE_RETRY:-false}"
CAP_RETRY_LIMIT="${CAP_RETRY_LIMIT:-3}"

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

# ---------- 0) 営業日ガード（REQUIRE_TRADINGDAY=1 のときのみ・2026-09-23 追加） ----------
# 既定は未設定（off）でこのブロックは丸ごと素通りするため、この env を渡さない workflow
# （mover_weekly / sector_report_weekly_full 等、土日月に金曜分を出すレーン）の挙動は一切変わらない。
# 休場日は予備タイマーの待機・通知より前に降りる（⏱/🚨 通知は reason に反応しないため鳴らない）。
if [ "${REQUIRE_TRADINGDAY:-}" = "1" ]; then
  TD_RC=0
  TD_LINE=$(bash "$(dirname "$0")/is_tradingday.sh" "$TARGET_DATE") || TD_RC=$?
  if [ "$TD_RC" -eq 1 ]; then
    TD_REASON="${TD_LINE##*reason=}"
    echo "skip 理由: 対象日 ${TARGET_DATE} は日本市場の休場日（${TD_REASON}）のため。Discord 通知は出しません。"
    echo "::notice title=${WF_FILE} skipped::market closed (${TD_REASON}) - no report, no Discord notification"
    echo "proceed=false" >> "$GITHUB_OUTPUT"
    echo "reason=skip (market closed: ${TD_REASON})" >> "$GITHUB_OUTPUT"
    exit 0
  elif [ "$TD_RC" -ne 0 ]; then
    echo "is_tradingday.sh failed (rc=${TD_RC}); 営業日判定を省いて通常の判定を続けます" >&2
  else
    echo "営業日判定: ${TD_LINE}"
  fi
fi

# ---------- 1) 予備タイマー: 起床時刻まで待機 ----------
if [ "$EVENT" = "schedule" ]; then
  WAKE=$(to_min "${WAKE_JST:?WAKE_JST is required for schedule runs}")
  W_START=$(to_min "$WINDOW_START_JST")
  W_END=$(to_min "$WINDOW_END_JST")
  NOW=$(jst_min)

  if [ "$NOW" -gt "$W_END" ] || [ "$NOW" -lt "$W_START" ]; then
    # 2026-09-23 修正（PM 判断・運用通知の平易化）: 「本日すでに配信済みか」を時間帯判定より先に見る。
    # 従来は時間帯判定が先だったため、21:00 に配信済みの日でも予備 cron が 23:30 以降に遅延着火すると
    # 「未配信」の 🚨 が出ていた（配信済みの日に未配信と通知する誤報）。
    # 取得に失敗した場合は 0 件扱いにして従来どおり時間帯判定へ進む（guard 自体は落とさない）。
    OW_DELIVERED=0
    if OW_STATS=$(bash "$(dirname "$0")/check_delivery.sh" "$WF_FILE" "$SEND_JOB" "$MY_RUN_ID"); then
      OW_DELIVERED=$(echo "$OW_STATS" | jq -r '.delivered // 0' 2>/dev/null || echo 0)
    else
      echo "配信状況の取得に失敗しました（時間帯判定は続行します）" >&2
    fi
    case "$OW_DELIVERED" in (''|*[!0-9]*) OW_DELIVERED=0 ;; esac
    if [ "$OW_DELIVERED" -gt 0 ]; then
      echo "skip 理由: 想定外の時間帯（$(jst_hhmm) JST）の着火ですが、本日はすでに ${SEND_JOB} が成功済み（${OW_DELIVERED} 件）＝配信が完了しているため。通知は出しません。"
      echo "proceed=false" >> "$GITHUB_OUTPUT"
      echo "reason=skip (already delivered today: ${SEND_JOB} success=${OW_DELIVERED})" >> "$GITHUB_OUTPUT"
      exit 0
    fi
    # window_side: early = 受付開始前（深夜〜午前。その日の本番はまだこれから）/ late = 受付終了後（その日はもう作れない）
    if [ "$NOW" -lt "$W_START" ]; then
      echo "window_side=early" >> "$GITHUB_OUTPUT"
      echo "skip 理由: cron が受付開始前（$(jst_hhmm) JST / 許容 ${WINDOW_START_JST}〜${WINDOW_END_JST}）に着火したため。本日分は通常の時刻に作るため、ここでは中止します。"
    else
      echo "window_side=late" >> "$GITHUB_OUTPUT"
      echo "skip 理由: cron が受付終了後（$(jst_hhmm) JST / 許容 ${WINDOW_START_JST}〜${WINDOW_END_JST}）に着火したため。日付ズレ配信を避けて中止します。"
    fi
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
WAIT_EXPIRED=false
if [ "$EVENT" = "schedule" ]; then
  DEADLINE=$(( $(date +%s) + WAIT_MINUTES * 60 ))
  while [ "$DELIVERED" -eq 0 ] && { [ "$SENDING" -gt 0 ] || [ "$DISPATCH_PENDING" -gt 0 ]; }; do
    if [ "$(date +%s)" -ge "$DEADLINE" ]; then
      echo "本命 run の完了待ちが ${WAIT_MINUTES} 分を超えたため待機を打ち切り、${LANE_LABEL}で配信します（配信絶対の原則）。"
      # 2026-09-12 修正: 待機を打ち切った事実をフラグに残す。
      # 従来はここで break した直後、下の判定が同じ sending>0 を見て
      # 「別 run が配信中」で skip し、打ち切りの意味が消えていた（実測 run 34602683209）。
      WAIT_EXPIRED=true
      break
    fi
    echo "本命 run が走行中（sending=${SENDING} dispatch_pending=${DISPATCH_PENDING}）。60 秒後に再確認します。"
    sleep 60
    read_stats
  done
fi

# ---------- 3.5) 枠切れ由来の失敗を切り分ける（CAP_AWARE_RETRY=true のときのみ） ----------
# 既定は false のため、この env を渡さない既存 workflow ではここは素通りし挙動は変わらない。
CAPPED_FAILED=0
EFFECTIVE_FAILED="$FAILED"
if [ "$CAP_AWARE_RETRY" = "true" ]; then
  CAPPED_FAILED=$(bash "$(dirname "$0")/count_capped_failures.sh" "$TARGET_DATE" "$MY_RUN_ID" 2>/dev/null || echo 0)
  # 数え損ねた場合（API 失敗等）は 0 に倒す＝従来どおり厳しい側で止める（安全側）。
  case "$CAPPED_FAILED" in (''|*[!0-9]*) CAPPED_FAILED=0 ;; esac
  if [ "$CAPPED_FAILED" -gt "$FAILED" ]; then CAPPED_FAILED="$FAILED"; fi
  EFFECTIVE_FAILED=$(( FAILED - CAPPED_FAILED ))
  echo "枠切れの切り分け: failed=${FAILED} のうち ${CAPPED_FAILED} 件が Claude 利用枠切れ由来（設備障害ではなく時間で戻るもの）。実質失敗数=${EFFECTIVE_FAILED}"
fi

# ---------- 4) 判定 ----------
echo "判定材料 (${TARGET_DATE} / $(jst_hhmm) JST / event=${EVENT}): delivered=${DELIVERED} sending=${SENDING} dispatch_pending=${DISPATCH_PENDING} failed=${FAILED} capped_failed=${CAPPED_FAILED} effective_failed=${EFFECTIVE_FAILED}"

if [ "$DELIVERED" -gt 0 ] && [ "$EVENT" = "workflow_dispatch" ] && [ "$DISPATCH_OVERRIDES" = "true" ]; then
  echo "本日すでに配信済みですが、手動/Worker の dispatch は意図的発火のため再発行します。"
  echo "proceed=true" >> "$GITHUB_OUTPUT"
  echo "reason=manual_dispatch (override delivered=${DELIVERED})" >> "$GITHUB_OUTPUT"
elif [ "$DELIVERED" -gt 0 ]; then
  echo "skip 理由: 本日はすでに ${SEND_JOB} が成功済み（${DELIVERED} 件）＝配信が完了しているため。"
  echo "proceed=false" >> "$GITHUB_OUTPUT"
  echo "reason=skip (already delivered today: ${SEND_JOB} success=${DELIVERED})" >> "$GITHUB_OUTPUT"
elif [ "$SENDING" -gt 0 ] && [ "$WAIT_EXPIRED" != "true" ]; then
  echo "skip 理由: 別 run が今まさに配信中（sending=${SENDING}）のため。"
  echo "proceed=false" >> "$GITHUB_OUTPUT"
  echo "reason=skip (another run is delivering: sending=${SENDING})" >> "$GITHUB_OUTPUT"
elif [ "$SENDING" -gt 0 ]; then
  # 2026-09-12 追加: 待機上限まで待っても配信が完了しなかった場合。
  # 「配信中」に見えても実際には配信できていない（失敗直前・ハング・遅延）ため、
  # ここで skip すると当日未配信のまま run が success で終わり誰も気づけない（false green）。
  # 配信絶対の原則に従い、待ちきった側が自分で配信する。
  # 二重配信は build-and-send ジョブ単位の concurrency（cancel-in-progress: false）が防ぐ。
  echo "待機上限（${WAIT_MINUTES} 分）まで待っても配信が完了しなかったため、sending=${SENDING} でも ${LANE_LABEL}で配信します（配信絶対の原則）。"
  echo "proceed=true" >> "$GITHUB_OUTPUT"
  echo "reason=recovery (wait expired while sending=${SENDING} failed=${FAILED})" >> "$GITHUB_OUTPUT"
elif [ "$EVENT" = "schedule" ] && [ "$EFFECTIVE_FAILED" -ge 2 ]; then
  echo "skip 理由: 本日すでに ${EFFECTIVE_FAILED} 回（枠切れ由来を除く）配信に失敗しており、cron の自動リトライ上限（2 回）に達したため。"
  echo "proceed=false" >> "$GITHUB_OUTPUT"
  echo "reason=retry_cap (cron stopped: effective_failed=${EFFECTIVE_FAILED} capped=${CAPPED_FAILED})" >> "$GITHUB_OUTPUT"
elif [ "$EVENT" = "schedule" ] && [ "$CAP_AWARE_RETRY" = "true" ] && [ "$CAPPED_FAILED" -ge "$CAP_RETRY_LIMIT" ]; then
  # 枠切れ由来だけで上限に達した場合。無限に cron を回して枠を焼かないための別枠上限。
  echo "skip 理由: 本日は Claude 利用枠切れによる失敗が ${CAPPED_FAILED} 回に達し、枠切れ用の再試行上限（${CAP_RETRY_LIMIT} 回）を超えたため。"
  echo "proceed=false" >> "$GITHUB_OUTPUT"
  echo "reason=cap_retry_limit (capped=${CAPPED_FAILED}/${CAP_RETRY_LIMIT})" >> "$GITHUB_OUTPUT"
else
  echo "proceed=true" >> "$GITHUB_OUTPUT"
  if [ "$FAILED" -gt 0 ]; then
    echo "reason=recovery (failed=${FAILED})" >> "$GITHUB_OUTPUT"
  else
    echo "reason=initial" >> "$GITHUB_OUTPUT"
  fi
fi
