# -*- coding: utf-8 -*-
"""レポートの Discord 送信が「PC で読む」リンク付きの中央関数だけを通ることの再発検知テスト。

【背景（PM 2026-10-10 指示）】
全レポートはブラウザで読むリンク付きでしか Discord へ送らない。同日、業界レポート 2 本が
send_report_pdf_discord.py の内部関数を直接呼ぶ使い捨てスクリプトで送られ、リンクが付かなかった。
送信は send_report_pdf_discord.send_report_pdf() に一本化し、本テストはリポ内に
「中央の送信関数以外で Discord webhook へファイルを添付して送るコード」が増えていないかを検査する。

【検査の中身】
  1. git 管理下のコード（.py・.yml・.yaml・.sh・.ps1・.js）とスキル定義（.claude/commands/*.md）を
     走査し、Discord への添付送信の印（files[N]・payload_json・curl -F 等）を持つファイルを集める。
  2. 中央の送信関数のファイル・レポート以外の添付（理由付きの許可リスト）・封印済み
     （先頭で raise して何も実行しない）・承認待ち（理由付き）以外が 1 件でもあれば失敗する。
  3. 承認待ちリストの項目が直ったのにリストに残っていれば失敗する（直したら外す）。
  4. レポート本文をテキストで送っていた旧スクリプトが封印されたままであることを確かめる。

【実行方法】
    python -m pytest bi/pipelines/tests/test_discord_send_guard.py -q
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

# 中央の送信関数（send_report_pdf）を持つファイル。ここだけが Discord へレポート PDF を添付送信してよい。
CENTRAL = "bi/pipelines/send_report_pdf_discord.py"

# レポート以外の添付送信（理由を必ず書く）。レポートを送る経路をここへ足してはいけない。
NON_REPORT_ALLOWED = {
    "bi/pipelines/build_report_audio.py": "レポートの読み上げ音声（mp3）。PDF ではなく閲覧リンクの対象外",
    "bi/pipelines/send_post_bank_discord.py": "SNS 投稿ストック（md）。レポートではない",
    ".github/workflows/x_outreach_daily.yml": "X 先行フォロー候補の作業リスト PDF＋実行用スクリプト。レポートではない",
    ".github/workflows/x_unfollow_daily.yml": "X フォロー整理の作業リスト PDF＋実行用スクリプト。レポートではない",
}

# リンク無しでレポートを送るが、ルールファイルのため PM の個別承認待ちで未修正のもの（理由を必ず書く）。
KNOWN_PENDING = {
    ".claude/commands/dlab-report.md": "D-Lab レポート PDF をスキル内のコードで直接送っている。"
    "send_report_pdf_discord.py --kind pdf への置き換えはスキル定義の変更（PM 承認）が必要",
}

# レポート本文をテキストで送っていた旧スクリプト。封印（先頭で raise）されたままであること。
LEGACY_REPORT_TEXT_SENDERS = [
    "bi/pipelines/send_report_discord.py",
    "bi/pipelines/send_macro_discord.py",
    "bi/pipelines/send_sector_discord.py",
    "bi/pipelines/send_mover_discord.py",
    "bi/pipelines/send_ideas_discord.py",
    "bi/pipelines/send_earnings_discord.py",
    "dev/prototype/themes/pipelines/send_growth_reverse_discord.py",
    "dev/prototype/themes/pipelines/send_growth_reverse_v2_discord.py",
    "dev/prototype/themes/pipelines/send_theme_rotation_discord.py",
]

_CODE_SUFFIXES = {".py", ".yml", ".yaml", ".sh", ".ps1", ".js", ".mjs"}

# Discord webhook への添付送信の印。
_ATTACH_MARK = re.compile(
    r"""["']files\[\d+\]["']"""         # Discord の multipart キー "files[0]" 等（list の files[0] は除く）
    r"""|payload_json"""                # 添付送信時の本文 JSON
    r"""|curl\b[^\n]*\s(?:-F|--form)\s"""  # curl のフォーム送信
    r"""|\bFormData\b"""                # JS のフォーム送信
    r"""|-Form\b|-InFile\b"""           # PowerShell のフォーム／ファイル送信
    r"""|\b_post_pdf\b""",              # 中央ファイルの低レベル送信関数を外から直接呼ぶ使い捨てコード
)
# .post( の引数に files= がある送信（Notion 等への送信と区別するため webhook の語も併せて見る）。
_FILES_POST = re.compile(r"\.post\(\s*[^()]*?\bfiles\s*=", re.S)
_WEBHOOK = re.compile(r"webhook", re.IGNORECASE)


def _tracked_files() -> list[str]:
    out = subprocess.run(
        ["git", "-c", "core.quotepath=off", "ls-files", "-z"],
        cwd=REPO, capture_output=True, check=True,
    ).stdout.decode("utf-8")
    return [p for p in out.split("\0") if p]


def _is_scan_target(rel: str) -> bool:
    if rel.startswith("bi/pipelines/tests/"):
        return False  # テスト自身は検査語を文字列として含む
    if rel.startswith(".claude/commands/") and rel.endswith(".md"):
        return True  # スキル定義は手順内のコードがそのまま実行される
    return Path(rel).suffix.lower() in _CODE_SUFFIXES


def _sends_discord_attachment(text: str) -> bool:
    if _ATTACH_MARK.search(text):
        return True
    return bool(_FILES_POST.search(text) and _WEBHOOK.search(text))


def _is_sealed_python(path: Path) -> bool:
    """docstring・コメント・from __future__ の直後の最初の文が raise なら封印済み（何も実行しない）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return False
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(getattr(node, "value", None), ast.Constant) \
                and isinstance(node.value.value, str):
            continue  # docstring
        if isinstance(node, ast.ImportFrom) and node.module == "__future__":
            continue
        return isinstance(node, ast.Raise)
    return False


def _attachment_senders() -> list[str]:
    found = []
    for rel in _tracked_files():
        if not _is_scan_target(rel):
            continue
        path = REPO / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if _sends_discord_attachment(text):
            found.append(rel)
    return found


def test_only_central_function_sends_report_attachments():
    """中央の送信関数・許可リスト・封印済み・承認待ち以外に、Discord への添付送信コードが無いこと。"""
    offenders = []
    for rel in _attachment_senders():
        if rel == CENTRAL or rel in NON_REPORT_ALLOWED or rel in KNOWN_PENDING:
            continue
        if rel.endswith(".py") and _is_sealed_python(REPO / rel):
            continue
        offenders.append(rel)
    assert offenders == [], (
        "Discord へファイルを添付して送るコードが中央の送信関数以外にある。"
        "レポートは send_report_pdf_discord.send_report_pdf（CLI なら --kind pdf 等）経由で"
        "「PC で読む」リンク付きでしか送らないこと: " + ", ".join(offenders)
    )


def test_known_pending_entries_are_still_needed():
    """承認待ちの項目が直ったら KNOWN_PENDING から外す（古い例外を残さない）。"""
    stale = [
        rel for rel in KNOWN_PENDING
        if (REPO / rel).is_file()
        and not _sends_discord_attachment((REPO / rel).read_text(encoding="utf-8"))
    ]
    assert stale == [], "直った項目を KNOWN_PENDING から外すこと: " + ", ".join(stale)


def test_central_file_has_link_enforcing_sender():
    """中央ファイルに send_report_pdf があり、低レベル送信がリンク検査を通ること。"""
    text = (REPO / CENTRAL).read_text(encoding="utf-8")
    assert "def send_report_pdf(" in text
    assert "_require_inline_link(pdf_path, content, view_url)" in text
    assert "def _with_inline_view_link(" not in text  # リンク任意の旧関数を復活させない


@pytest.mark.parametrize("rel", LEGACY_REPORT_TEXT_SENDERS)
def test_legacy_report_text_senders_stay_sealed(rel):
    """レポート本文をテキストで送っていた旧スクリプトは封印（先頭で raise）されたままであること。"""
    path = REPO / rel
    if not path.is_file():
        pytest.skip(f"このリポには無い: {rel}")
    assert _is_sealed_python(path), f"封印が外れている（先頭の raise が無い）: {rel}"
