#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock-report 誌面フォーマット構成ファイルの版凍結 / 復元ユーティリティ。

構成5ファイル（＋補助2ファイル）を版フォルダへ凍結し、必要時に正本へ復元する。
削除は一切行わない（退避はコピー、正本の変化は復元による上書きのみ）。

使い方:
    python bi/pipelines/freeze_stock_report_format.py --list
    python bi/pipelines/freeze_stock_report_format.py --version v7 --note "変更要旨"
    python bi/pipelines/freeze_stock_report_format.py --restore v5
"""

from __future__ import annotations

import argparse
import datetime as _dt
import shutil
import sys
from pathlib import Path

# Windows の既定コンソール（cp932）だと日本語出力が文字化けするため UTF-8 へ再設定する。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, OSError):
        pass

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

# リポジトリルート = このスクリプト（bi/pipelines/）から2階層上
REPO_ROOT = Path(__file__).resolve().parents[2]

VERSIONS_DIR = REPO_ROOT / "dev" / "skills_frozen" / "stock_report_versions"
VERSIONS_MD = VERSIONS_DIR / "VERSIONS.md"
TRASH_DIR = REPO_ROOT / ".trash"

# 正本パス（リポジトリルート相対） -> 版フォルダ内のファイル名
CORE_FILES: dict[str, str] = {
    "agents/stock_analyst.md": "stock_analyst.md",
    ".claude/commands/stock-report.md": "stock-report.md",
    "bi/pipelines/gate_stock_report.py": "gate_stock_report.py",
    "bi/pipelines/lib/md_to_pdf.py": "md_to_pdf.py",
    "bi/pipelines/lib/table_rules.py": "table_rules.py",
}

# 補助ファイル（存在すれば対象に含める・必須ではない）
AUX_FILES: dict[str, str] = {
    "bi/pipelines/lib/report_skeleton.py": "report_skeleton.py",
    "agents/stock_analyst_examples.md": "stock_analyst_examples.md",
}

ALL_FILES: dict[str, str] = {**CORE_FILES, **AUX_FILES}

VERSIONS_MD_HEADER = "| 版 | 日付 | コミット | 変更要旨 | 凍結フォルダ |"


# ---------------------------------------------------------------------------
# 共通ヘルパ
# ---------------------------------------------------------------------------


def _today() -> str:
    """ローカル日付を YYYY-MM-DD で返す。"""
    return _dt.date.today().strftime("%Y-%m-%d")


def _err(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def _normalize_version(version: str) -> str:
    """'6' / 'V6' / 'v6' を 'v6' に正規化する。"""
    v = version.strip()
    if not v:
        raise ValueError("版名が空です")
    if v[0] in ("v", "V"):
        v = "v" + v[1:]
    else:
        v = "v" + v
    return v


def _find_version_dirs(version: str) -> list[Path]:
    """vN_* に一致する版フォルダを列挙する（vN と vN1 の誤マッチを避けるため接尾辞を検査）。"""
    if not VERSIONS_DIR.exists():
        return []
    prefix = f"{version}_"
    return sorted(
        p for p in VERSIONS_DIR.iterdir() if p.is_dir() and p.name.startswith(prefix)
    )


def _copy(src: Path, dst: Path) -> None:
    """バイナリコピー（改行コード・タイムスタンプを保持）。"""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# --list
# ---------------------------------------------------------------------------


def cmd_list() -> int:
    if not VERSIONS_DIR.exists():
        _err(f"版フォルダの親ディレクトリが存在しません: {VERSIONS_DIR}")
        return 1

    dirs = sorted(p for p in VERSIONS_DIR.iterdir() if p.is_dir())
    if not dirs:
        print(f"版フォルダはまだありません: {VERSIONS_DIR}")
        return 0

    print(f"凍結版一覧（{VERSIONS_DIR}）")
    print(f"{'版':<6} {'日付':<12} {'ファイル数':>8}  フォルダ名")
    print("-" * 60)
    for d in dirs:
        name = d.name
        if "_" in name:
            ver, _, date = name.partition("_")
        else:
            ver, date = name, "-"
        n_files = sum(1 for p in d.iterdir() if p.is_file())
        print(f"{ver:<6} {date:<12} {n_files:>8}  {name}")
    print("-" * 60)
    print(f"合計 {len(dirs)} 版")
    return 0


# ---------------------------------------------------------------------------
# --version / --note （凍結）
# ---------------------------------------------------------------------------


def _append_versions_md(version: str, today: str, note: str, folder_name: str) -> None:
    """VERSIONS.md の表の最終行の後ろへ1行追記する。存在しなければ警告のみ。"""
    if not VERSIONS_MD.exists():
        print(
            f"[WARN] {VERSIONS_MD} が存在しないため版一覧への追記をスキップしました。"
            f"\n       追記すべき行: | {version} | {today} | 作業ツリー | {note} | "
            f"`{folder_name}/` |"
        )
        return

    text = VERSIONS_MD.read_text(encoding="utf-8")
    lines = text.split("\n")

    new_row = f"| {version} | {today} | 作業ツリー | {note} | `{folder_name}/` |"

    # 表の行（| で始まる行）のうち最後のものを探し、その直後へ挿入する
    last_table_idx = -1
    for i, line in enumerate(lines):
        if line.lstrip().startswith("|"):
            last_table_idx = i

    if last_table_idx == -1:
        # 表が見つからない場合は末尾へ追記（ヘッダ付き）
        print("[WARN] VERSIONS.md に表が見つかりませんでした。末尾に表を新設して追記します。")
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append(VERSIONS_MD_HEADER)
        lines.append("|---|---|---|---|---|")
        lines.append(new_row)
        lines.append("")
    else:
        lines.insert(last_table_idx + 1, new_row)

    VERSIONS_MD.write_text("\n".join(lines), encoding="utf-8")
    print(f"[OK] VERSIONS.md へ追記しました: {new_row}")


def cmd_freeze(version: str, note: str) -> int:
    today = _today()
    folder_name = f"{version}_{today}"
    dest_dir = VERSIONS_DIR / folder_name

    # 既存フォルダがあれば上書きせず中止（同名日付フォルダ・同一版の別日付フォルダの両方を確認）
    if dest_dir.exists():
        _err(f"凍結先フォルダが既に存在します（上書きしません）: {dest_dir}")
        return 1

    existing = _find_version_dirs(version)
    if existing:
        _err(
            f"版 {version} は既に凍結済みです（上書きしません）: "
            + ", ".join(p.name for p in existing)
        )
        return 1

    # 必須ファイルの存在確認（1つでも欠けたら中止）
    missing_core = [
        rel for rel in CORE_FILES if not (REPO_ROOT / rel).is_file()
    ]
    if missing_core:
        _err("構成5ファイルのうち以下が見つかりません（中止）:")
        for rel in missing_core:
            _err(f"  - {rel}")
        return 1

    VERSIONS_DIR.mkdir(parents=True, exist_ok=True)
    dest_dir.mkdir(parents=False, exist_ok=False)

    copied: list[str] = []
    skipped: list[str] = []
    for rel, fname in ALL_FILES.items():
        src = REPO_ROOT / rel
        if not src.is_file():
            skipped.append(rel)
            continue
        _copy(src, dest_dir / fname)
        copied.append(f"{rel} -> {fname}")

    print(f"[OK] 凍結しました: {dest_dir}")
    print(f"  日付: {today} / 版: {version} / 要旨: {note}")
    print(f"  コピーしたファイル ({len(copied)}件):")
    for line in copied:
        print(f"    - {line}")
    if skipped:
        print(f"  未存在のためスキップした補助ファイル ({len(skipped)}件):")
        for rel in skipped:
            print(f"    - {rel}")

    _append_versions_md(version, today, note, folder_name)
    return 0


# ---------------------------------------------------------------------------
# --restore （復元）
# ---------------------------------------------------------------------------


def cmd_restore(version: str) -> int:
    dirs = _find_version_dirs(version)
    if not dirs:
        _err(f"版 {version} に対応するフォルダが見つかりません: {VERSIONS_DIR}/{version}_*")
        return 1
    if len(dirs) > 1:
        _err(
            f"版 {version} に該当するフォルダが複数あります（中止）: "
            + ", ".join(p.name for p in dirs)
        )
        return 1

    src_dir = dirs[0]
    today = _today()
    backup_dir = TRASH_DIR / f"{today}_stock_report_format_before_restore"

    # 1) 現在の正本を .trash へ退避（コピー・削除しない）
    backup_dir.mkdir(parents=True, exist_ok=True)
    backed_up: list[str] = []
    not_present: list[str] = []
    for rel, fname in ALL_FILES.items():
        cur = REPO_ROOT / rel
        if not cur.is_file():
            not_present.append(rel)
            continue
        _copy(cur, backup_dir / fname)
        backed_up.append(rel)

    # 2) 版フォルダから正本へ上書きコピー（版フォルダに無いファイルは触らない）
    restored: list[str] = []
    untouched: list[str] = []
    for rel, fname in ALL_FILES.items():
        src = src_dir / fname
        if not src.is_file():
            untouched.append(rel)
            continue
        _copy(src, REPO_ROOT / rel)
        restored.append(rel)

    if not restored:
        _err(f"版フォルダに復元対象のファイルがありません: {src_dir}")
        return 1

    # 3) 結果表示
    print(f"[OK] 復元しました: {src_dir.name} -> 正本")
    print("")
    print(f"■ 退避（コピー）先: {backup_dir}")
    for rel in backed_up:
        print(f"    - {rel}")
    if not_present:
        print("  （正本が存在せず退避しなかったファイル）")
        for rel in not_present:
            print(f"    - {rel}")
    print("")
    print(f"■ 上書きしたファイル ({len(restored)}件):")
    for rel in restored:
        print(f"    - {rel}")
    if untouched:
        print(f"■ 版フォルダに無いため触っていないファイル ({len(untouched)}件):")
        for rel in untouched:
            print(f"    - {rel}")
    print("")
    print("■ 復元後の手順")
    print("  1) 動作確認:")
    print("       python bi/pipelines/lib/report_skeleton.py")
    print("       python bi/pipelines/gate_stock_report.py --help")
    print("  2) Public リポ C:/Users/mizuk/report-pipelines の同一パス")
    print("     （bi/pipelines 配下のみ）へ同期すること。")
    return 0


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="stock-report 誌面フォーマット構成ファイルの版凍結 / 復元（削除は一切しない）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        metavar="vN",
        help="凍結する版名（例: v7）。--note と併用する。",
    )
    parser.add_argument(
        "--note",
        metavar="要旨",
        help="VERSIONS.md へ記録する変更要旨（--version と併用）。",
    )
    parser.add_argument(
        "--restore",
        metavar="vN",
        help="指定版のファイルを正本へ復元する（実行前に .trash へ退避コピー）。",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="凍結済み版フォルダの一覧（版名・日付・ファイル数）を表示する。",
    )

    args = parser.parse_args(argv)

    modes = [bool(args.version), bool(args.restore), bool(args.list)]
    if sum(modes) == 0:
        parser.print_help()
        return 1
    if sum(modes) > 1:
        _err("--version / --restore / --list は同時に指定できません。")
        return 1

    if args.list:
        return cmd_list()

    if args.restore:
        try:
            version = _normalize_version(args.restore)
        except ValueError as e:
            _err(str(e))
            return 1
        return cmd_restore(version)

    # 凍結
    if not args.note:
        _err("--version には --note \"変更要旨\" が必須です。")
        return 1
    try:
        version = _normalize_version(args.version)
    except ValueError as e:
        _err(str(e))
        return 1
    return cmd_freeze(version, args.note.strip())


if __name__ == "__main__":
    sys.exit(main())
