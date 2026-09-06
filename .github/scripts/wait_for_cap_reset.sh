#!/usr/bin/env bash
#
# wait_for_cap_reset.sh - Claude 利用枠のリセットを待つ待機ゲート（設計案・未投入）。
#
# 配置先（投入時）: report-pipelines/.github/scripts/wait_for_cap_reset.sh
#
# なぜ必要か:
#   detect_spending_cap.sh は「枠切れかどうか」を見分けるところまでを担うが、
#   枠が戻ってから再実行する仕組みが無いため、当日未配信のまま run が終わる。
#   本スクリプトは同一 run 内で枠のリセットまで待機し、後続の復旧生成ステップへ道を繋ぐ。
#
# 設計上の最重要事項（リセット時刻を絶対時刻として信用しない）:
#   実測のエラー文言は "Spending cap reached resets 1:10pm" で、
#   タイムゾーンも日付も含まれない。2 サンプル（2026-09-04 / 2026-09-05）に
#   候補タイムゾーンを当てはめると待ち時間は 5 分〜953 分まで割れ、
#   実ログから 1 つに絞る材料は無い（検証済み）。
#   そのため待機の主軸は「固定間隔」に置き、リセット文字列からは
#   タイムゾーンに依存しない「分」だけを補助的に使う。
#
# 待機の刻み（指数バックオフにしない理由）:
#   枠のリセットは指数的に遠のく現象ではなく一定周期で必ず戻る。
#   指数的に伸ばすと後半の待ちが無駄に長くなりリセット直後を取り逃すため、
#   ほぼ等間隔（55 / 115 / 175 分）で刻む。
#
# 入力（env）:
#   必須:
#     ATTEMPT             何回目の待機か（1 始まり）
#   任意:
#     RESETS_MINUTE       リセット時刻の「分」（0-59）。detect_spending_cap.sh の resets_minute。
#                         空なら分合わせをせず素の間隔で待つ（フォールバック）。
#     JOB_DEADLINE_EPOCH  この job が終了しなければならない epoch 秒。
#                         残り時間で待ちきれない場合は待機せず should_retry=false を返す。
#     MARGIN_MINUTES      起床後に生成へ使う余裕（既定 40 分）。
#     DRY_RUN             "true" なら sleep せず計算結果だけ出力（検証用）。
#     GITHUB_OUTPUT       未設定なら標準出力のみ（ローカル検証用）。
#
# 出力: $GITHUB_OUTPUT へ
#   should_retry=true|false   待機して再試行してよいか
#   waited_minutes=<数>       実際に待った分数（should_retry=false なら 0）
#   reason=<文字列>           判断理由（無言 skip の禁止）
#
set -euo pipefail

ATTEMPT="${ATTEMPT:?ATTEMPT is required}"
RESETS_MINUTE="${RESETS_MINUTE:-}"
MARGIN_MINUTES="${MARGIN_MINUTES:-40}"
DRY_RUN="${DRY_RUN:-false}"

# 待機間隔（分）。ATTEMPT 番目の値を使う。表を超えたら再試行しない。
INTERVALS=(55 115 175)

emit() {
  echo "should_retry=$1"
  echo "waited_minutes=$2"
  echo "reason=$3"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    {
      echo "should_retry=$1"
      echo "waited_minutes=$2"
      echo "reason=$3"
    } >> "$GITHUB_OUTPUT"
  fi
}

# ---------- 1) 何分待つかを決める ----------
IDX=$(( ATTEMPT - 1 ))
if [ "$IDX" -lt 0 ] || [ "$IDX" -ge "${#INTERVALS[@]}" ]; then
  echo "待機ゲート: ${ATTEMPT} 回目は待機表（${#INTERVALS[*]} 段）の範囲外のため、これ以上は待ちません。"
  emit "false" "0" "attempt ${ATTEMPT} exceeds retry table (${#INTERVALS[@]} steps)"
  exit 0
fi
WAIT_MIN="${INTERVALS[$IDX]}"

# ---------- 2) リセットの「分」に寄せる（タイムゾーン非依存の微調整） ----------
# 例: 待機 55 分後の時刻が :47 で RESETS_MINUTE=10 なら、+23 分して :10 に合わせる。
# 枠のリセットは一定周期で起きるため、分を合わせるとリセット直後を捉えやすい。
ADJUST=0
if [ -n "$RESETS_MINUTE" ] && [ "$RESETS_MINUTE" -ge 0 ] 2>/dev/null && [ "$RESETS_MINUTE" -le 59 ] 2>/dev/null; then
  NOW_MIN=$(date +%M)
  WAKE_MIN=$(( (10#$NOW_MIN + WAIT_MIN) % 60 ))
  ADJUST=$(( (RESETS_MINUTE - WAKE_MIN + 60) % 60 ))
  # 分合わせで 59 分も余計に待つのは本末転倒なので、30 分を超える寄せはしない。
  if [ "$ADJUST" -gt 30 ]; then
    echo "待機ゲート: 分合わせに ${ADJUST} 分を要するため見送ります（素の間隔で待機）。"
    ADJUST=0
  fi
  [ "$ADJUST" -gt 0 ] && echo "待機ゲート: リセットの分（:${RESETS_MINUTE}）へ寄せるため ${ADJUST} 分を追加します。"
else
  echo "待機ゲート: リセット時刻の分が取れていないため、分合わせをせず素の間隔で待機します。"
fi
TOTAL_MIN=$(( WAIT_MIN + ADJUST ))

# ---------- 3) job の残り時間で待ちきれるかを確認（無限待機の防止） ----------
if [ -n "${JOB_DEADLINE_EPOCH:-}" ]; then
  NOW_EPOCH=$(date +%s)
  REMAIN_MIN=$(( (JOB_DEADLINE_EPOCH - NOW_EPOCH) / 60 ))
  NEED_MIN=$(( TOTAL_MIN + MARGIN_MINUTES ))
  if [ "$REMAIN_MIN" -lt "$NEED_MIN" ]; then
    echo "待機ゲート: 残り ${REMAIN_MIN} 分では「待機 ${TOTAL_MIN} 分 + 生成 ${MARGIN_MINUTES} 分」に届かないため待機しません。"
    emit "false" "0" "insufficient job time (remain=${REMAIN_MIN}min need=${NEED_MIN}min)"
    exit 0
  fi
  echo "待機ゲート: 残り ${REMAIN_MIN} 分・必要 ${NEED_MIN} 分のため待機できます。"
fi

# ---------- 4) 待機 ----------
WAKE_AT=$(date -u -d "+${TOTAL_MIN} minutes" '+%H:%M' 2>/dev/null || echo "?")
echo "待機ゲート: ${ATTEMPT} 回目・${TOTAL_MIN} 分待機します（UTC 約 ${WAKE_AT} 起床予定）。"
echo "待機ゲート: 枠のリセットは時間が経てば必ず戻るため、この待機は設備障害への対処ではありません。"

if [ "$DRY_RUN" = "true" ]; then
  echo "待機ゲート: DRY_RUN のため実際の sleep はしません。"
  emit "true" "$TOTAL_MIN" "dry-run (would wait ${TOTAL_MIN}min)"
  exit 0
fi

sleep $(( TOTAL_MIN * 60 ))
echo "待機ゲート: 起床しました。枠が戻っているか再生成で確認します。"
emit "true" "$TOTAL_MIN" "waited ${TOTAL_MIN}min for cap reset (attempt ${ATTEMPT})"
