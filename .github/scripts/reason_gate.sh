#!/usr/bin/env bash
#
# reason_gate.sh - 動意（日次・週次）の「なぜ動いた」を ETL の事実ファイルと機械照合する理由ゲートの
# workflow 側の手順（2026-10-04 新設・PM 承認 dev/drafts/2026-10-04_dev_mover_reason_fix_plan.md §3-D）。
#
# なぜ必要か:
#   2026-10-02 週次号の 7256 で、最大変動日（10/2）と無関係な 9/28 の開示（影響軽微・場中開示後に
#   高値更新なし）を冒頭の理由に置いた誤記が配信された。bi/pipelines/lib/reason_check.py が誌面と
#   {date}_movers_facts.json（make_mover_report.py が raw と同じ場所へ出す）を照合して不合格を出す。
#   不合格の扱い（PM 判断事項 3）: 生成枠で不合格銘柄の「なぜ動いた」だけを 1 回書き直させ、
#   それでも不合格なら merge-and-send で facts だけの 1 文へ機械差し替えして配信する（絶対配信原則）。
#
# モード（env MODE）:
#   check     生成枠（generate-market）の試行の後。不合格なら修正試行用のプロンプトと facts の抜粋を作る。
#             出力: status（no_md / no_facts / pass / fail / error）・need_fix・fix_prompt・failed_codes・fail_count
#   recheck   修正試行の後。修正が他の行を壊していれば元に戻し、再検査する。
#             出力: status（pass / fail / error / no_facts）・failed_codes・fail_count・restored
#   fallback  merge-and-send の件数ゲートの前。不合格銘柄を facts だけの 1 文へ差し替える。
#             出力: status（no_facts / pass / replaced / error）・replaced_codes・replaced_count・after_fail_count
#
# 入力（env）:
#   MD           検査対象の誌面 md（必須）
#   FACTS        {date}_movers_facts.json（無い・空なら検査を飛ばして no_facts を返す）
#   ALLOW_FIX    check: 'true' のときだけ修正試行を要求する（時間枠が足りない時は 'false'）
#   RULE_FILE    check: 書式の正本のプロンプト（report-pipelines/prompts/mover-weekly.md 等）
#   LEG          ログ・作業ディレクトリ名に使う枠名（growth 等。既定 main）
#
# 失敗しても step を落とさない（常に exit 0）。配信は止めない（絶対配信原則 _cr §36）。
#
set -uo pipefail

MODE="${MODE:-check}"
MD="${MD:-}"
FACTS="${FACTS:-}"
ALLOW_FIX="${ALLOW_FIX:-false}"
RULE_FILE="${RULE_FILE:-}"
LEG="${LEG:-main}"

WS="${GITHUB_WORKSPACE:-$(pwd)}"
# 修正試行の Claude が Read する抜粋はワークスペース内に置く（作業ディレクトリ外の読込を避ける）。
WORK="${WS}/reason_gate_work/${LEG}"
mkdir -p "$WORK"
CHECKER="${WS}/private-repo/bi/pipelines/lib/reason_check.py"

emit() {
  echo "$1=$2"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    echo "$1=$2" >> "$GITHUB_OUTPUT"
  fi
}

json_get() {
  # $1=json ファイル $2=キー（failed_codes は , 区切り・数値はそのまま）
  python3 - "$1" "$2" <<'PY' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    print(""); sys.exit(0)
v = d.get(sys.argv[2], "")
print(",".join(map(str, v)) if isinstance(v, list) else v)
PY
}

if [ -z "$MD" ] || [ ! -s "$MD" ]; then
  echo "理由ゲート（${MODE}・${LEG}）: 誌面が無いため検査しません: ${MD}"
  emit status no_md
  emit need_fix false
  emit replaced_count 0
  exit 0
fi
if [ -z "$FACTS" ] || [ ! -s "$FACTS" ]; then
  echo "::warning title=理由ゲート未実施::事実ファイル（${FACTS##*/}）が無いため「なぜ動いた」の照合を飛ばします（動意 ETL の facts 出力失敗）。配信は続けます"
  emit status no_facts
  emit need_fix false
  emit replaced_count 0
  exit 0
fi

case "$MODE" in
  check)
    python3 "$CHECKER" --file "$MD" --facts "$FACTS" --json-out "$WORK/check1.json"
    ec=$?
    if [ "$ec" -eq 0 ]; then
      emit status pass
      emit need_fix false
      emit fail_count 0
      exit 0
    fi
    if [ "$ec" -ne 4 ]; then
      echo "::warning title=理由ゲート実行失敗::reason_check.py が exit ${ec} で終了しました（検査を飛ばして続行）"
      emit status error
      emit need_fix false
      exit 0
    fi
    CODES="$(json_get "$WORK/check1.json" failed_codes)"
    NFAIL="$(json_get "$WORK/check1.json" fail_count)"
    emit status fail
    emit failed_codes "$CODES"
    emit fail_count "${NFAIL:-0}"
    if [ "$ALLOW_FIX" != "true" ]; then
      echo "::warning title=理由ゲート不合格::${LEG}: 不合格 ${NFAIL} 件（${CODES}）。時間枠が足りないため修正試行は行わず、merge-and-send の機械差し替えに任せます"
      emit need_fix false
      exit 0
    fi
    # 修正前の誌面を退避（修正試行が他の行を壊した時に戻す）
    cp "$MD" "$WORK/before_fix.md"
    # facts のうち不合格銘柄だけの抜粋と、不合格の一覧を作る（Claude に渡すのはこの 2 つだけ）
    python3 - "$FACTS" "$WORK/check1.json" "$WORK/facts_failed.json" "$WORK/failures.md" <<'PY'
import json, sys
facts = json.load(open(sys.argv[1], encoding="utf-8"))
res = json.load(open(sys.argv[2], encoding="utf-8"))
codes = res.get("failed_codes", [])
sub = {k: v for k, v in facts.items() if k != "stocks"}
sub["stocks"] = {c: facts.get("stocks", {}).get(c) for c in codes if c in facts.get("stocks", {})}
json.dump(sub, open(sys.argv[3], "w", encoding="utf-8"), ensure_ascii=False, indent=1)
lines = []
for f in res.get("failures", []):
    if f.get("severity") != "fail":
        continue
    lines.append(f"- 見出し行 L{f.get('line')}・{f.get('code')} {f.get('name')}・検査({f.get('check')}): {f.get('message')}")
open(sys.argv[4], "w", encoding="utf-8").write("\n".join(lines) + "\n")
PY
    PROMPT="${RUNNER_TEMP:-/tmp}/reason_fix_prompt_${LEG}.md"
    {
      cat <<EOF
あなたは動意銘柄レポートの校正担当である。誌面 \`${MD}\` のうち、事実照合ゲートで不合格になった銘柄の「**なぜ動いた**」の行だけを書き直す。

## 対象（不合格の一覧）
EOF
      cat "$WORK/failures.md"
      cat <<EOF

## 使ってよい材料
- \`${WORK}/facts_failed.json\` = 不合格銘柄だけの事実ファイル（ETL が出力した値）。最初に Read する。各銘柄の \`max_move_day\`（最大変動日）・\`week_days\` / \`month3_days\` の各日の騰落率・開示（\`disclosures\`・公表時刻・\`minor_impact\`（影響軽微）・\`intraday\`（場中開示）・\`post_new_extreme\`（開示後の高値／安値更新））・報道（\`news\`）・関連銘柄の同日騰落（\`related\`）・四季報（\`shikiho\`）。
- 書式の正本は \`${WS}/${RULE_FILE}\` の「理由は値動きの日付と材料の時刻を突き合わせて特定する」の項。Grep でその項を探し、その項だけを Read する（ファイル全体は読まない）。raw は読まない。

## 書き直しの規律（要点）
1. 冒頭に最大変動日を \`{M/D}（{曜}）{＋/▼}{X.X}%：{その日の材料}\` の形で書く。騰落率は facts の値を小数第 1 位で書く。
2. 理由に使える材料は、最大変動日（または 2σ の日）に割り当てられた開示・報道だけ。別の日の材料・過去の開示を最大変動日の理由に流用しない。
3. 影響軽微の開示・場中開示の後に高値（下落日は安値）を更新していない開示は理由にしない。
4. 材料が無い日は \`材料は確認できず。\` と書き、facts にある事実（関連銘柄の同日騰落率など）だけを添える。理由を創作しない（思惑・期待・警戒・懸念・失望・連想・観測・利益確定・買い戻し・需給 などの語で理由を作らない）。
5. 推測語（可能性が高い・と思われる・と考えられる・だろう・のはず・と推測される・公算・とみるのが自然・とみられる・見込み）を地の文で使わない。報道の推量は \`{媒体名}は「{原文}」と書いている\` の形でだけ引用してよい。
6. facts と元の誌面に無い数値・固有名詞を足さない。1〜3 文に収める。

## 作業の制約
- Edit で、対象銘柄の見出し（\`### N位 {コード} …\`）の下にある \`**なぜ動いた**：\` の行だけを置き換える。行頭の \`**なぜ動いた**：\` は残す。
- 見出し・「何の会社」・他の銘柄・その他の行は 1 文字も変えない。Write でファイル全体を書き直さない。
- 同じ銘柄が複数の見出しに出る場合は、一覧の見出し行（L 番号）の下の行だけを直す。
- 全件を直したら終了する。説明文の出力は要らない。
EOF
    } > "$PROMPT"
    emit need_fix true
    emit fix_prompt "$PROMPT"
    echo "理由ゲート（${LEG}）: 不合格 ${NFAIL} 件（${CODES}）→ 修正試行を 1 回行います（プロンプト: ${PROMPT}）"
    exit 0
    ;;

  recheck)
    RESTORED=false
    BK="$WORK/before_fix.md"
    if [ -s "$BK" ]; then
      # 修正試行が誌面を壊していないか（見出し・何の会社の行数が同じか）を確認し、壊れていれば戻す
      H_BK="$(grep -c '^### ' "$BK" || true)"; H_NOW="$(grep -c '^### ' "$MD" || true)"
      W_BK="$(grep -c '^\*\*何の会社\*\*' "$BK" || true)"; W_NOW="$(grep -c '^\*\*何の会社\*\*' "$MD" || true)"
      R_BK="$(grep -c '^\*\*なぜ動いた\*\*' "$BK" || true)"; R_NOW="$(grep -c '^\*\*なぜ動いた\*\*' "$MD" || true)"
      if [ "$H_BK" != "$H_NOW" ] || [ "$W_BK" != "$W_NOW" ] || [ "$R_BK" != "$R_NOW" ]; then
        echo "::warning title=修正試行の取り消し::${LEG}: 修正試行で見出し・行の数が変わったため修正前の誌面へ戻します（見出し ${H_BK}→${H_NOW}・何の会社 ${W_BK}→${W_NOW}・なぜ動いた ${R_BK}→${R_NOW}）"
        cp "$BK" "$MD"
        RESTORED=true
      fi
    fi
    emit restored "$RESTORED"
    python3 "$CHECKER" --file "$MD" --facts "$FACTS" --json-out "$WORK/check2.json"
    ec=$?
    if [ "$ec" -eq 0 ]; then
      echo "理由ゲート（${LEG}）: 修正試行の後は不合格なし"
      emit status pass
      emit fail_count 0
    elif [ "$ec" -eq 4 ]; then
      CODES="$(json_get "$WORK/check2.json" failed_codes)"
      NFAIL="$(json_get "$WORK/check2.json" fail_count)"
      echo "::warning title=理由ゲート不合格（修正試行後）::${LEG}: 不合格 ${NFAIL} 件（${CODES}）。merge-and-send で事実だけの 1 文へ機械差し替えします"
      emit status fail
      emit failed_codes "$CODES"
      emit fail_count "${NFAIL:-0}"
    else
      echo "::warning title=理由ゲート実行失敗::reason_check.py が exit ${ec} で終了しました（再検査を飛ばして続行）"
      emit status error
    fi
    exit 0
    ;;

  fallback)
    OUT="$WORK/reasonfixed.md"
    LOG="$WORK/fallback.log"
    python3 "$CHECKER" --file "$MD" --facts "$FACTS" --fallback-fix --out "$OUT" --json-out "$WORK/fallback.json" 2>&1 | tee "$LOG"
    ec=${PIPESTATUS[0]}
    if [ "$ec" -ne 0 ] && [ "$ec" -ne 4 ]; then
      echo "::warning title=理由ゲート実行失敗::reason_check.py --fallback-fix が exit ${ec} で終了しました（差し替えなしで配信を続けます）"
      emit status error
      emit replaced_count 0
      exit 0
    fi
    CODES="$(grep -m1 '^REPLACED=' "$LOG" | sed 's/^REPLACED=//' | tr -d '\r')"
    AFTER="$(json_get "$WORK/fallback.json" after_fix_fail_count)"
    if [ -n "$CODES" ]; then
      if [ -s "$OUT" ]; then
        cp "$OUT" "$MD"
        N="$(echo "$CODES" | tr ',' '\n' | grep -c .)"
        # 通知用は「・」区切り
        emit replaced_codes "$(echo "$CODES" | sed 's/,/・/g')"
        emit replaced_count "$N"
        emit after_fail_count "${AFTER:-0}"
        emit status replaced
        echo "理由ゲート: ${N} 銘柄（${CODES}）の「なぜ動いた」を事実だけの 1 文へ差し替えました（差し替え後の不合格 ${AFTER:-?} 件）"
      else
        echo "::warning title=理由ゲート差し替え失敗::差し替え後の md が空のため差し替えずに配信を続けます"
        emit status error
        emit replaced_count 0
      fi
    else
      emit status pass
      emit replaced_count 0
      emit after_fail_count "${AFTER:-0}"
      echo "理由ゲート: 差し替え対象なし"
    fi
    exit 0
    ;;

  *)
    echo "::warning::reason_gate.sh: 未知の MODE=${MODE}"
    exit 0
    ;;
esac
