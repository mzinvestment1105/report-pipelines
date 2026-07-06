# -*- coding: utf-8 -*-
"""market/daily/ 配下レポートフォルダの archive ローテーション。

CLAUDE.md データ鮮度ルール:
  「sector 以外のレポートフォルダは 5 件上限で超過分を archive/ へ移動」
を自動化する。対象フォルダ: macro / movers / theme / ideas / largecap / scout
（sector は上限なしのため対象外）。

動作:
  - 各フォルダ直下の *.md のうち、ファイル名に YYYY-MM-DD を含むものだけを対象
    （README.md 等の日付なしファイルは対象外・触らない）
  - 日付降順（同日はファイル名降順）に並べ、新しい 5 件を残す
  - 超過分を同フォルダ内 archive/（無ければ作成）へ shutil.move で退避
  - 削除は一切しない（移動のみ）。移動先に同名ファイルが既にあればスキップして警告

raw ファイルのローテーション（2026-07-06 追加）:
  - market/daily/ 直下の `{YYYY-MM-DD}_{source}_raw.md`（例: 2026-07-06_macro_raw.md）
    のうち、ファイル名日付が実行日の 7 日より古いものを
    market/daily/archive/raw/（無ければ作成）へ shutil.move で退避
    （7 日はマクロ夕刊・週次レポートが直近 raw を参照する余地を考慮した保守値）
  - README.md 等の日付なしファイル・サブフォルダは対象外・触らない
  - 削除は一切しない（移動のみ）。移動先に同名ファイルが既にあればスキップして警告

リポルート解決（GHA の private-repo チェックアウト内でも動く）:
  1. 環境変数 MIZUKI_FUND_ROOT があればそれを使う
  2. なければスクリプト位置から 3 階層上（{root}/bi/pipelines/ops/本ファイル）

使い方:
  python bi/pipelines/ops/rotate_report_archives.py            # 本実行
  python bi/pipelines/ops/rotate_report_archives.py --dry-run  # 移動せず表示のみ
"""

import argparse
import datetime
import os
import re
import shutil
import sys
from pathlib import Path

KEEP_COUNT = 5
TARGET_FOLDERS = ["macro", "movers", "theme", "ideas", "largecap", "scout"]
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")

# market/daily/ 直下の raw ファイル（{YYYY-MM-DD}_{source}_raw.md）ローテーション設定
RAW_KEEP_DAYS = 7
RAW_NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_.+_raw\.md$")


def resolve_repo_root() -> Path:
    env_root = os.environ.get("MIZUKI_FUND_ROOT", "").strip()
    if env_root:
        root = Path(env_root).resolve()
    else:
        # {root}/bi/pipelines/ops/rotate_report_archives.py → parents[3] = {root}
        root = Path(__file__).resolve().parents[3]
    return root


def rotate_folder(folder: Path, dry_run: bool) -> int:
    """1 フォルダをローテーションし、移動（予定）件数を返す。"""
    if not folder.is_dir():
        print(f"[SKIP] フォルダなし: {folder}")
        return 0

    dated_files = []
    for f in sorted(folder.glob("*.md")):
        if not f.is_file():
            continue
        m = DATE_RE.search(f.name)
        if not m:
            continue  # 日付なしファイル（README.md 等）は対象外
        dated_files.append((m.group(1), f.name, f))

    # 日付降順・同日はファイル名降順（新しい 5 件を先頭に）
    dated_files.sort(key=lambda t: (t[0], t[1]), reverse=True)

    excess = dated_files[KEEP_COUNT:]
    if not excess:
        print(f"[OK]   {folder.name}: {len(dated_files)} 件 <= {KEEP_COUNT} 件・移動なし")
        return 0

    archive_dir = folder / "archive"
    moved = 0
    for _, _, src in excess:
        dst = archive_dir / src.name
        if dst.exists():
            print(f"[WARN] 移動先に同名ファイルあり・スキップ（削除はしない）: {dst}")
            continue
        if dry_run:
            print(f"[DRY]  move {src} -> {dst}")
        else:
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            print(f"[MOVE] {src} -> {dst}")
        moved += 1

    print(
        f"[OK]   {folder.name}: 全 {len(dated_files)} 件中 新しい {KEEP_COUNT} 件を残し "
        f"{moved} 件を archive/ へ{'移動予定 (dry-run)' if dry_run else '移動'}"
    )
    return moved


def rotate_raw_files(daily: Path, dry_run: bool) -> int:
    """market/daily/ 直下の raw ファイルをローテーションし、移動（予定）件数を返す。

    ファイル名日付が実行日の RAW_KEEP_DAYS 日より古い `*_raw.md` を
    market/daily/archive/raw/ へ退避する（削除は一切しない）。
    """
    cutoff = datetime.date.today() - datetime.timedelta(days=RAW_KEEP_DAYS)

    old_files = []
    for f in sorted(daily.glob("*_raw.md")):
        if not f.is_file():
            continue
        m = RAW_NAME_RE.match(f.name)
        if not m:
            continue  # 日付なしファイル（README.md 等）は対象外
        try:
            file_date = datetime.date.fromisoformat(m.group(1))
        except ValueError:
            continue  # 不正な日付（例: 2026-13-99）は触らない
        if file_date < cutoff:
            old_files.append(f)

    if not old_files:
        print(f"[OK]   raw: {cutoff} より古い raw ファイルなし・移動なし")
        return 0

    archive_dir = daily / "archive" / "raw"
    moved = 0
    for src in old_files:
        dst = archive_dir / src.name
        if dst.exists():
            print(f"[WARN] 移動先に同名ファイルあり・スキップ（削除はしない）: {dst}")
            continue
        if dry_run:
            print(f"[DRY]  move {src} -> {dst}")
        else:
            archive_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            print(f"[MOVE] {src} -> {dst}")
        moved += 1

    print(
        f"[OK]   raw: {cutoff} より古い {len(old_files)} 件中 "
        f"{moved} 件を archive/raw/ へ{'移動予定 (dry-run)' if dry_run else '移動'}"
    )
    return moved


def main() -> int:
    parser = argparse.ArgumentParser(description="market/daily/ 配下レポートの archive ローテーション")
    parser.add_argument("--dry-run", action="store_true", help="移動せず対象の表示のみ")
    args = parser.parse_args()

    root = resolve_repo_root()
    daily = root / "market" / "daily"
    if not daily.is_dir():
        print(f"[ERROR] market/daily が見つかりません: {daily}")
        print("        MIZUKI_FUND_ROOT 環境変数でリポルートを指定してください")
        return 1

    print(f"repo root: {root}")
    print(f"mode     : {'dry-run（移動なし）' if args.dry_run else '本実行'}")

    total = 0
    for name in TARGET_FOLDERS:
        total += rotate_folder(daily / name, args.dry_run)
    total += rotate_raw_files(daily, args.dry_run)

    print(f"合計: {total} 件{'（dry-run・実際には移動していない）' if args.dry_run else ' 移動'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
