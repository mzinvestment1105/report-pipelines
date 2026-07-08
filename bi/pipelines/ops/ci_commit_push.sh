#!/usr/bin/env bash
# ci_commit_push.sh — GHA から Private リポ（mizuki-fund）へ生成物を commit & push する共通ヘルパー。
#
# 2026-07-07 新設（rebase conflict 恒久対策）:
# 旧方式「commit → fetch → rebase + checkout --theirs ループ」は、履歴書き換え・並行 push 時に
# add/add 大量衝突 → rebase --continue 途中失敗 → index.lock 残留、という構造的弱点があった
# （実測: mover_weekly run で rebase 失敗 → push 不能）。
# 本ヘルパーは rebase を廃止し fresh-base 方式で置き換える:
#   (1) 指定パス配下で「この job が作った差分」（追加/変更/削除ファイル）を検出し一時 dir へ退避
#   (2) git fetch && git reset --hard origin/master（常に最新 origin を土台にする）
#   (3) 退避した差分だけを新しい土台へ再適用（他レーンが並行 push したファイルは消さない）
#   (4) git add（-f 指定時は gitignore 対象の未追跡ファイルも走査・強制追加）→ commit
#   (5) push（reject = 並行 push なら (2)〜(4) を最大 3 回やり直し）
#
# 使い方（cwd = private repo のルート・スクリプト本体は repo 外 = Public checkout から実行する。
# repo 内にコピーすると (2) の reset --hard が実行中の自分自身を書き換えるため禁止）:
#   bash ../report-pipelines/bi/pipelines/ops/ci_commit_push.sh -m "data: macro report 2026-07-07" market/daily
#   bash ../report-pipelines/bi/pipelines/ops/ci_commit_push.sh -m "data: financial_history 2026-07-07" -f bi/outputs/financial_history_master.parquet
set -u

MSG=""
FORCE_ADD=0
MAX_ATTEMPTS=3

usage() {
  echo "usage: ci_commit_push.sh -m <commit message> [-f] <path> [path ...]" >&2
  exit 2
}

while getopts "m:f" opt; do
  case "$opt" in
    m) MSG="$OPTARG" ;;
    f) FORCE_ADD=1 ;;
    *) usage ;;
  esac
done
shift $((OPTIND - 1))

[ -z "$MSG" ] && usage
[ "$#" -eq 0 ] && usage

git config user.name "github-actions[bot]"
git config user.email "github-actions[bot]@users.noreply.github.com"

# ---- (1) この job が作った差分（clone 時点の HEAD 比較・未追跡ファイル含む）を検出 ----
STATUS_OPTS=(--porcelain=v1 -z -uall)
if [ "$FORCE_ADD" -eq 1 ]; then
  # gitignore 対象の未追跡ファイル（例: bi/outputs/*.parquet の新規作成）も対象にする
  STATUS_OPTS+=(--ignored=matching)
fi

CHANGED=()   # 追加 or 変更されたファイル（退避 → 再適用）
DELETED=()   # 削除されたファイル（rotate によるアーカイブ移動元など・新土台でも削除を再適用）
while IFS= read -r -d '' entry; do
  [ -z "$entry" ] && continue
  st="${entry:0:2}"
  path="${entry:3}"
  case "$st" in
    *D*) DELETED+=("$path") ;;
    *)   CHANGED+=("$path") ;;
  esac
done < <(git status "${STATUS_OPTS[@]}" -- "$@")

if [ "${#CHANGED[@]}" -eq 0 ] && [ "${#DELETED[@]}" -eq 0 ]; then
  echo "[ci_commit_push] 指定パス配下に差分なし・commit しません"
  exit 0
fi
echo "[ci_commit_push] 差分検出: changed=${#CHANGED[@]} deleted=${#DELETED[@]}"

# ---- 退避（repo 外の一時ディレクトリへ） ----
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
for f in ${CHANGED[@]+"${CHANGED[@]}"}; do
  mkdir -p "$TMP/$(dirname "$f")"
  cp -p "$f" "$TMP/$f"
done

attempt=1
while [ "$attempt" -le "$MAX_ATTEMPTS" ]; do
  echo "[ci_commit_push] attempt ${attempt}/${MAX_ATTEMPTS}"

  # ---- (2) 途中失敗の残骸を除去し、最新 origin/master を土台にする ----
  git rebase --abort 2>/dev/null || true
  rm -f .git/index.lock 2>/dev/null || true
  git fetch origin master
  git reset --hard origin/master

  # ---- (3) 差分の再適用 ----
  for f in ${CHANGED[@]+"${CHANGED[@]}"}; do
    mkdir -p "$(dirname "$f")"
    cp -p "$TMP/$f" "$f"
  done
  for f in ${DELETED[@]+"${DELETED[@]}"}; do
    rm -f -- "$f"
  done

  # ---- (4) ステージ（-A で削除も反映）→ commit ----
  for f in ${CHANGED[@]+"${CHANGED[@]}"}; do
    if [ "$FORCE_ADD" -eq 1 ]; then
      git add -f -- "$f" || echo "[ci_commit_push] WARN: add -f 失敗: $f"
    else
      git add -- "$f" || echo "[ci_commit_push] WARN: add 失敗（gitignore 対象?）: $f"
    fi
  done
  for f in ${DELETED[@]+"${DELETED[@]}"}; do
    git add -A -- "$f" 2>/dev/null || true
  done

  if git diff --staged --quiet; then
    echo "[ci_commit_push] 新しい origin/master に対して差分なし・commit しません"
    exit 0
  fi

  git commit -m "$MSG"

  # ---- (5) push（reject なら fresh-base からやり直し） ----
  if git push origin HEAD:master; then
    echo "[ci_commit_push] push 完了"
    exit 0
  fi

  echo "[ci_commit_push] push reject（並行 push とみられる）・やり直します"
  attempt=$((attempt + 1))
  sleep $((attempt * 5))
done

echo "[ci_commit_push] ERROR: ${MAX_ATTEMPTS} 回試行しても push できませんでした" >&2
exit 1
