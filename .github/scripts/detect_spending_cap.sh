#!/usr/bin/env bash
#
# detect_spending_cap.sh - Claude Code action の失敗が「利用枠切れ」かどうかだけを判定する（2026-09-05 新設）。
#
# なぜ必要か:
#   実測（2026-09-04 run 33870602080 / 33875016222）では、gen1 が
#     "Spending cap reached resets 1:10pm"
#   で exit 1 したあと、60 秒待機 → gen2、180 秒待機 → gen3 と回っていた。
#   枠のリセットまでは数十分あるため 60 / 180 秒の待機では全く届かず、
#   gen2 / gen3 は 1.5 秒で即時拒否されるだけの無駄な消費になっていた。
#   さらに悪いことに、この空振りは standby_guard.sh の retry_cap（当日 2 回失敗で
#   cron の自動リトライを停止）を食い潰し、枠が戻ってからの自動復旧経路を
#   自分で塞いでしまう。そこで枠切れだけは即座に見分けて後続 gen を打ち切る。
#
# 判定は「枠切れか否か」の 1 点のみ。一時障害（529 等）と確定失敗の区別は
# 従来どおり各 workflow 側のリトライ構造に任せる（本スクリプトは何も壊さない）。
#
# 入力（env）:
#   任意: EXECUTION_FILE  action の execution_file 出力（失敗時も書き出される）
#         ATTEMPT         ログ表示用の試行番号
#         GITHUB_OUTPUT   未設定なら標準出力のみ（ローカル検証用）
#
# 出力: $GITHUB_OUTPUT へ
#   capped=true|false  枠切れなら true
#   resets=<文字列>    リセット時刻が読み取れた場合のみ（例: 1:10pm）。読めなければ空。
#
set -euo pipefail

ATTEMPT="${ATTEMPT:-?}"
EXECUTION_FILE="${EXECUTION_FILE:-}"

# 実測の文言は "Spending cap reached resets 1:10pm"。
# 表記揺れ（usage limit / limit reached 等）も拾えるようにするが、
# 枠切れ以外を巻き込まないよう語の組み合わせで縛る。
CAP_RE='[Ss]pending cap reached|[Uu]sage limit reached|[Cc]laude usage limit'

emit() {
  echo "capped=$1"
  echo "resets=$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "capped=$1" >> "$GITHUB_OUTPUT"
    echo "resets=$2" >> "$GITHUB_OUTPUT"
  fi
}

if [ -z "$EXECUTION_FILE" ] || [ ! -s "$EXECUTION_FILE" ]; then
  # 判定材料が無い場合は枠切れと断定しない（従来どおりのリトライへ倒す）。
  echo "attempt ${ATTEMPT}: execution_file が無いため枠切れ判定はしません"
  emit "false" ""
  exit 0
fi

# result / error 系のテキストだけを抜く（プロンプト本文やツール出力の巻き添えを避ける）。
HAYSTACK=$(jq -r '
    [ .. | objects
      | (.result? // empty), (.error? // empty),
        (select(.type? == "text") | .text? // empty)
    ] | map(select(type == "string")) | join("\n")
  ' "$EXECUTION_FILE" 2>/dev/null || cat "$EXECUTION_FILE")

if ! echo "$HAYSTACK" | grep -qE "$CAP_RE"; then
  echo "attempt ${ATTEMPT}: 枠切れの文言は検出されませんでした"
  emit "false" ""
  exit 0
fi

# リセット時刻を拾えたら通知文面へ回す（拾えなくても判定は変えない）。
RESETS=$(echo "$HAYSTACK" \
  | grep -oE '[Rr]esets[[:space:]]+[0-9]{1,2}(:[0-9]{2})?[[:space:]]*([AaPp][Mm])?' \
  | head -1 \
  | sed -E 's/^[Rr]esets[[:space:]]+//' || true)

echo "attempt ${ATTEMPT}: Claude 利用枠の上限を検出しました（後続の再試行は打ち切ります）"
[ -n "$RESETS" ] && echo "attempt ${ATTEMPT}: リセット時刻の表記: ${RESETS}"
emit "true" "${RESETS}"
