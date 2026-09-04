#!/usr/bin/env bash
#
# classify_failure.sh - Claude Code action の失敗を「一時障害」と「確定失敗」に分ける（2026-09-04 新設）。
#
# なぜ必要か:
#   両方を同じ「失敗」として扱うと、どちらかを必ず損なう。
#     一律で粘る  → 確定失敗の日に同じ入力を何度も投げ、約 36k の入力トークンを無駄に燃やす。
#     一律で諦める → 529 のような一時障害の日に、ほぼ 0 トークンで済む再試行を放棄して未発行になる。
#   そこで「再試行に意味があるか」だけを判定する。
#
# 分類:
#   transient = リクエスト自体が拒否/中断された一時障害。
#       実測（2026-09-03 run 33762959285）: result JSON が
#         "API Error: 529 {...overloaded_error...}" / input_tokens=0 / output_tokens=0 /
#         total_cost_usd=0
#       ＝ Claude は 1 トークンも生成していない。再試行のコストは実質ゼロなので粘る。
#   permanent = Claude が実際に動いたうえで成果物を作らなかった。
#       プロンプト側の意図的中止（ABORT）や、エラー語が無いのに出力が無い場合。
#       同じ入力を投げ直しても結論は変わらないため、同一 run 内では再試行しない。
#
# 入力（env）:
#   必須: TARGET_DATE
#   任意: EXECUTION_FILE  action の execution_file 出力（失敗時も書き出される）
#         ATTEMPT         ログ表示用の試行番号
#         WORKDIR         成果物を探す基準ディレクトリ（既定 private-repo）
#         GITHUB_OUTPUT   未設定なら標準出力のみ（ローカル検証用）
#
# 出力: $GITHUB_OUTPUT へ kind=transient|permanent と reason
#
set -euo pipefail

: "${TARGET_DATE:?TARGET_DATE is required}"
ATTEMPT="${ATTEMPT:-?}"
WORKDIR="${WORKDIR:-private-repo}"
EXECUTION_FILE="${EXECUTION_FILE:-}"

# 一時障害と判断するエラー語（大文字小文字を無視して探す）。
# 5xx は「50x/52x のステータス」を拾う。4xx の rate_limit は語として別に列挙する。
TRANSIENT_RE='529|overloaded|rate.?limit|too.many.requests|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|socket hang up|timed? ?out|fetch failed|network error|Internal Server Error|Bad Gateway|Service Unavailable|Gateway Time-?out|"status":[[:space:]]*5[0-9][0-9]|Error: 5[0-9][0-9]'
# 確定失敗（Claude が動いて意図的に止めた）を示す語。
PERMANENT_RE='ABORT|中止します|生成を中止|要件を満たせ|前提条件が満たされ|異常終了|ツイート未生成|生成せず'

emit() {
  echo "kind=$1"
  echo "reason=$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "kind=$1"   >> "$GITHUB_OUTPUT"
    echo "reason=$2" >> "$GITHUB_OUTPUT"
  fi
}

OUT_FILE="${WORKDIR}/research/sns/${TARGET_DATE}_macro_tweet.md"

# 成果物が既にあるなら、そもそも失敗として扱う必要がない（Verify 側が成功と判定する）。
if [ -s "$OUT_FILE" ]; then
  emit "permanent" "attempt ${ATTEMPT}: 成果物が存在するため再試行不要"
  exit 0
fi

# 途中まで書かれた不完全ファイルは退避する（削除しない = 不可逆削除の禁止）。
# 次の試行が白紙から書き直せるようにするため。
if [ -f "$OUT_FILE" ]; then
  mv "$OUT_FILE" "${RUNNER_TEMP:-/tmp}/${TARGET_DATE}_macro_tweet.attempt${ATTEMPT}.partial.md"
  echo "attempt ${ATTEMPT}: 不完全な出力を退避しました（削除はしていません）"
fi

# ---------- 判定材料の収集 ----------
# 第一の材料は execution_file。失敗時も action が書き出すため最も確実。
HAYSTACK=""
if [ -n "$EXECUTION_FILE" ] && [ -s "$EXECUTION_FILE" ]; then
  # result / error 系のテキストだけを抜く（プロンプト本文やツール出力の巻き添えを避ける）。
  HAYSTACK=$(jq -r '
      [ .. | objects
        | (.result? // empty), (.error? // empty),
          (select(.type? == "text") | .text? // empty)
      ] | map(select(type == "string")) | join("\n")
    ' "$EXECUTION_FILE" 2>/dev/null || cat "$EXECUTION_FILE")
  echo "attempt ${ATTEMPT}: execution_file を判定材料に使用します (${EXECUTION_FILE})"
  HAVE_EVIDENCE=true
else
  # execution_file が無いのは「Claude が動いて何も作らなかった」ではなく
  # 「action がログを書く前に落ちた」＝インフラ側の一時障害である可能性が高い。
  # 証拠が無い状態を確定失敗として扱うと、粘れば取れる日を落とす。
  echo "attempt ${ATTEMPT}: execution_file が無い（action がログ出力前に落ちた可能性）"
  HAVE_EVIDENCE=false
fi

# usage が全て 0 なら、トークンを消費しないまま拒否された ＝ 一時障害の強い証拠。
ZERO_USAGE=false
if [ -n "$EXECUTION_FILE" ] && [ -s "$EXECUTION_FILE" ]; then
  if jq -e '[.. | objects | select(has("total_cost_usd"))] | length > 0
            and (map(.total_cost_usd) | add == 0)' "$EXECUTION_FILE" >/dev/null 2>&1; then
    ZERO_USAGE=true
  fi
fi

# ---------- 分類 ----------
# 確定失敗の語を優先する（Claude が動いた証拠のほうが強い）。
if echo "$HAYSTACK" | grep -qiE "$PERMANENT_RE"; then
  emit "permanent" "attempt ${ATTEMPT}: Claude が意図的に中止（同一 run 内では再試行しない）"
elif echo "$HAYSTACK" | grep -qiE "$TRANSIENT_RE"; then
  emit "transient" "attempt ${ATTEMPT}: 一時障害を検出（再試行する）"
elif [ "$ZERO_USAGE" = "true" ]; then
  emit "transient" "attempt ${ATTEMPT}: トークン消費ゼロで終了（リクエスト拒否とみなし再試行する）"
elif [ "$HAVE_EVIDENCE" = "false" ]; then
  # 判定材料そのものが無いケース。トークンを消費した証拠も無いため、
  # インフラ側の一時障害とみなして 1 度だけ粘る側に倒す。
  # （確定失敗ならこの後の試行でも成果物が出ず、Verify が最終的に失敗と判定する。）
  emit "transient" "attempt ${ATTEMPT}: 判定材料が無く消費実績も不明のためインフラ障害とみなす"
else
  # エラー語も無く、出力も無く、トークンは消費された。
  # 同じ入力で投げ直しても同じ結果になる可能性が高いため再試行しない。
  emit "permanent" "attempt ${ATTEMPT}: エラー語なしに未生成（再試行してもトークンを消費するだけ）"
fi
