#!/usr/bin/env bash
#
# check_attempt.sh - Claude 生成の 1 試行ごとに「成果物ができたか」と「1 応答の出力上限に何回達したか」を
# 記録し、次の試行に渡すプロンプト（通常版 / 縮小モード版）を決める（2026-10-04 新設）。
#
# なぜ必要か:
#   新しい claude-code-base-action（Claude Code CLI 2.x）では、1 応答の出力上限に達しても致命エラーに
#   ならず、CLI が "Output token limit hit. Resume directly — ..." を差し込んで続行し、step は success で
#   終わる。旧 CLI のように「失敗したら再試行」の判定だけでは、上限到達で成果物が保存されなかった試行を
#   成功と取り違え、再試行せずに誌面が欠ける（2026-10-01・10-02 動意日次テーマ欄の事故）。
#   そこで試行ごとに execution_file の上限到達メッセージを数え、成果物の実在と合わせて判定する。
#
# モード（env MODE）:
#   attempt（既定） 試行直後に呼ぶ。出力: ok / limit_hits / total_hits / shrink / next_prompt / cause
#   summary         全試行の後に呼ぶ。出力: ok / total_hits / attempts / cause
#
# 入力（env）:
#   ATTEMPT         ログ表示用の試行名（1 / 2 / recovery-1 等）
#   OUTCOME         生成 step の outcome（success / failure / cancelled）
#   EXECUTION_FILE  生成 step の execution_file 出力（無くてもよい）
#   PROMPT_FILE     元のプロンプトファイル（縮小モード版はこの末尾にブロックを足した複製）
#   OUT_FILES       成果物のパス（空白区切り・全て非空なら成果物あり）。空なら outcome だけで判定する
#   SLOT            同一ジョブ内で状態を分ける名前（既定 main）
#   MISSING_NOUN    原因文の末尾語（既定「成果物なし」）
#   LABEL           summary の原因文の先頭に付ける枠名（例: テーマ欄）。空なら付けない
#
# 状態ファイル: ${RUNNER_TEMP}/gen_attempts/<SLOT>.state（同一ジョブ内の試行間で上限到達回数を合算する）。
#
# 注意: execution_file は全試行で同じパス（$RUNNER_TEMP/claude-execution-output.json）に上書きされる。
#   試行が書き出し前に落ちると前回の内容が残るため、処理済みファイルの sha256 を覚えて二重計上を防ぐ。
#
set -uo pipefail

MODE="${MODE:-attempt}"
SLOT="${SLOT:-main}"
ATTEMPT="${ATTEMPT:-?}"
OUTCOME="${OUTCOME:-}"
EXECUTION_FILE="${EXECUTION_FILE:-}"
PROMPT_FILE="${PROMPT_FILE:-}"
OUT_FILES="${OUT_FILES:-}"
MISSING_NOUN="${MISSING_NOUN:-成果物なし}"
LABEL="${LABEL:-}"

STATE_DIR="${RUNNER_TEMP:-/tmp}/gen_attempts"
STATE="${STATE_DIR}/${SLOT}.state"
mkdir -p "$STATE_DIR"

LIMIT_MSG='Output token limit hit'
SHRINK_BLOCK='【再試行・縮小モード】前回の試行は 1 応答の出力上限に達して成果物を保存できなかった。思考を最小にし、節を 1 つ考えたら即 Write で保存し、1 応答で 3,000 字を超えて書かない。目標字数は本文規定の 6 割でよい。'

TOTAL_HITS=0
ATTEMPTS=0
LAST_SHA=""
LAST_OK=""
LAST_OUTCOME=""
if [ -f "$STATE" ]; then
  # 状態ファイルは本スクリプトだけが書く（数値・sha256・英単語のみ）。
  # shellcheck disable=SC1090
  . "$STATE"
fi

emit() {
  echo "$1=$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}

make_cause() {
  # $1=ok $2=total_hits $3=last_outcome
  if [ "$1" = "true" ]; then
    echo ""
  elif [ "${2:-0}" -gt 0 ]; then
    echo "出力上限到達 ${2} 回・${MISSING_NOUN}"
  elif [ "$3" = "failure" ] || [ "$3" = "cancelled" ]; then
    echo "生成エラー・${MISSING_NOUN}"
  else
    echo "${MISSING_NOUN}"
  fi
}

if [ "$MODE" = "summary" ]; then
  if [ "$ATTEMPTS" -eq 0 ]; then
    echo "summary: 生成試行は実行されていません（slot=${SLOT}）"
    emit ok ""
    emit total_hits "0"
    emit attempts "0"
    emit cause ""
    exit 0
  fi
  CAUSE="$(make_cause "$LAST_OK" "$TOTAL_HITS" "$LAST_OUTCOME")"
  if [ -n "$CAUSE" ] && [ -n "$LABEL" ]; then
    CAUSE="${LABEL}: ${CAUSE}"
  fi
  echo "summary: slot=${SLOT} attempts=${ATTEMPTS} ok=${LAST_OK} total_hits=${TOTAL_HITS}"
  if [ "$TOTAL_HITS" -gt 0 ]; then
    echo "::warning title=出力上限到達::${LABEL:+${LABEL} }1 応答の出力上限に計 ${TOTAL_HITS} 回達しました（試行 ${ATTEMPTS} 回・成果物 ${LAST_OK}）"
  fi
  emit ok "$LAST_OK"
  emit total_hits "$TOTAL_HITS"
  emit attempts "$ATTEMPTS"
  emit cause "$CAUSE"
  exit 0
fi

# ---------- attempt モード ----------
HITS=0
if [ -n "$EXECUTION_FILE" ] && [ -s "$EXECUTION_FILE" ]; then
  SHA="$(sha256sum "$EXECUTION_FILE" | cut -d' ' -f1)"
  if [ "$SHA" = "$LAST_SHA" ]; then
    echo "attempt ${ATTEMPT}: execution_file が前回の試行と同一（この試行は書き出し前に終了）。上限到達は数えません"
  else
    # CLI が差し込む再開指示（assistant 以外のメッセージ）だけを数える。
    # assistant の発言と最終 result は、同じ語を引用しても数えない。
    if ! HITS="$(jq --arg m "$LIMIT_MSG" \
          '[ .[] | select(type == "object" and .type != "assistant" and .type != "result")
                 | tostring | select(contains($m)) ] | length' \
          "$EXECUTION_FILE" 2>/dev/null)"; then
      HITS="$(grep -o "$LIMIT_MSG" "$EXECUTION_FILE" | wc -l | tr -d ' ')"
    fi
    LAST_SHA="$SHA"
  fi
else
  echo "attempt ${ATTEMPT}: execution_file が無いため上限到達は数えません"
fi
case "$HITS" in ''|*[!0-9]*) HITS=0 ;; esac

OK=true
if [ "$OUTCOME" != "success" ]; then
  OK=false
fi
for f in $OUT_FILES; do
  if [ ! -s "$f" ]; then
    OK=false
    echo "attempt ${ATTEMPT}: 成果物が無いか空です: $f"
  fi
done

TOTAL_HITS=$(( TOTAL_HITS + HITS ))
ATTEMPTS=$(( ATTEMPTS + 1 ))

SHRINK=false
NEXT="$PROMPT_FILE"
if [ "$OK" != "true" ] && [ "$TOTAL_HITS" -gt 0 ]; then
  if [ -n "$PROMPT_FILE" ] && [ -s "$PROMPT_FILE" ]; then
    NEXT="${STATE_DIR}/${SLOT}_shrink_$(basename "$PROMPT_FILE")"
    { cat "$PROMPT_FILE"; printf '\n\n%s\n' "$SHRINK_BLOCK"; } > "$NEXT"
    SHRINK=true
    echo "attempt ${ATTEMPT}: 次の試行は縮小モードのプロンプトを使います: $NEXT"
  else
    echo "::warning::元のプロンプトが見つからないため縮小モード版を作れません: ${PROMPT_FILE}"
  fi
fi

{
  echo "TOTAL_HITS=${TOTAL_HITS}"
  echo "ATTEMPTS=${ATTEMPTS}"
  echo "LAST_SHA=${LAST_SHA}"
  echo "LAST_OK=${OK}"
  echo "LAST_OUTCOME=${OUTCOME}"
} > "$STATE"

CAUSE="$(make_cause "$OK" "$TOTAL_HITS" "$OUTCOME")"
echo "attempt ${ATTEMPT}: outcome=${OUTCOME} ok=${OK} limit_hits=${HITS} total_hits=${TOTAL_HITS}"
if [ "$HITS" -gt 0 ]; then
  echo "::warning title=出力上限到達（試行 ${ATTEMPT}）::1 応答の出力上限に ${HITS} 回達しました（成果物 ${OK}）"
fi
emit ok "$OK"
emit limit_hits "$HITS"
emit total_hits "$TOTAL_HITS"
emit shrink "$SHRINK"
emit next_prompt "$NEXT"
emit cause "$CAUSE"
exit 0
