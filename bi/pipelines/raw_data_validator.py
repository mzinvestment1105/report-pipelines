"""
生データファイルの取得品質チェック。
送信スクリプトが Discord に送る前にデータ不足・古いデータを検知して中止する。

チェック観点:
  1. ファイル存在確認
  2. ファイル内「生成日時」またはファイル名の日付が target_date と一致すること
  3. データ件数・取得成功率の検証
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

JST = timezone(timedelta(hours=9))

# Yahoo ニュース・掲示板の取得成功率の最低閾値（これを下回ると中止）
YAHOO_MIN_SUCCESS_RATE = 0.2

# sector_raw のデータ基準日の最大許容遅延（週次データなので7日まで許容）
SECTOR_DATA_MAX_LAG_DAYS = 7


# ---------------------------------------------------------------------------
# 共通: ヘッダー内の生成日時を読む
# ---------------------------------------------------------------------------

def _read_generated_date(text: str) -> date | None:
    """ファイル内の日付フィールドを読んで返す。なければ None。
    対応フィールド: 生成日時 / 収集日
    """
    for pattern in [
        r"\*\*生成日時\*\*[^\d]*(\d{4}-\d{2}-\d{2})",
        r"\*\*収集日\*\*[^\d]*(\d{4}-\d{2}-\d{2})",
    ]:
        m = re.search(pattern, text)
        if m:
            return date.fromisoformat(m.group(1))
    return None


def _check_generated_date(path: Path, text: str, target_date: str) -> None:
    """ファイル内の生成日時が target_date と一致することを確認する。"""
    gen_date = _read_generated_date(text)
    if gen_date is None:
        # ヘッダーに生成日時がない場合はファイル名の日付で代用
        m = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
        if m:
            gen_date = date.fromisoformat(m.group(1))
    if gen_date is None:
        print(f"[WARN] {path.name} の生成日時を確認できませんでした。")
        return
    expected = date.fromisoformat(target_date)
    if gen_date != expected:
        raise SystemExit(
            f"[ERROR] 送信を中止しました。\n"
            f"  {path.name} の生成日時が {gen_date} です（期待: {expected}）。\n"
            f"  古いファイルが残っている可能性があります。ETLを再実行してください。"
        )


# ---------------------------------------------------------------------------
# マクロ: news_raw
# ---------------------------------------------------------------------------

def check_news_raw(path: Path, target_date: str | None = None) -> None:
    """
    news_raw.md を検証する。
    - ファイルが存在しない → 中止
    - 生成日時が target_date と不一致 → 中止
    - タイムラインテーブル行（| 202）が 0 件 → 中止
    """
    if not path.exists():
        raise SystemExit(
            f"[ERROR] 生データが存在しません: {path}\n"
            f"  generate_macro_report.py（または fetch_rss.py）を先に実行してください。"
        )
    text = path.read_text(encoding="utf-8")
    if target_date:
        _check_generated_date(path, text, target_date)

    article_lines = [l for l in text.splitlines() if l.startswith("| 202")]
    if len(article_lines) == 0:
        raise SystemExit(
            f"[ERROR] 送信を中止しました。\n"
            f"  {path.name} の記事件数が 0 件です。RSS取得に失敗している可能性があります。\n"
            f"  fetch_rss.py を再実行してから再送してください。"
        )
    print(f"[OK] news_raw チェック通過: {len(article_lines)} 件")


# ---------------------------------------------------------------------------
# 動意銘柄: movers_raw
# ---------------------------------------------------------------------------

def check_movers_raw(path: Path, target_date: str | None = None) -> None:
    """
    movers_raw.md を検証する。
    - ファイルが存在しない → 中止
    - 生成日時が target_date と不一致 → 中止
    - 銘柄エントリ（### XXXX）が 0 件 → 中止
    - Yahoo ニュース取得成功率が YAHOO_MIN_SUCCESS_RATE 未満 → 中止
    - Yahoo 掲示板取得成功率が YAHOO_MIN_SUCCESS_RATE 未満 → 中止
    """
    if not path.exists():
        raise SystemExit(
            f"[ERROR] 生データが存在しません: {path}\n"
            f"  make_mover_report.py を先に実行してください。"
        )
    text = path.read_text(encoding="utf-8")
    if target_date:
        _check_generated_date(path, text, target_date)

    entries = re.findall(r"^### \d{4}", text, re.MULTILINE)
    total = len(entries)
    if total == 0:
        raise SystemExit(
            f"[ERROR] 送信を中止しました。\n"
            f"  {path.name} の銘柄エントリが 0 件です。ETL取得に失敗している可能性があります。\n"
            f"  make_mover_report.py を再実行してから再送してください。"
        )

    # みんかぶニュース成功率（Yahoo Finance Japan 廃止に伴い minkabu に移行）
    news_ok = len(re.findall(r"\*\*みんかぶニュース（\d+件）", text))
    news_rate = news_ok / total if total > 0 else 0.0
    if news_rate < YAHOO_MIN_SUCCESS_RATE:
        raise SystemExit(
            f"[ERROR] 送信を中止しました。\n"
            f"  {path.name} のみんかぶニュース取得成功率が低すぎます: "
            f"{news_ok}/{total} 銘柄 ({news_rate:.0%}) / 閾値 {YAHOO_MIN_SUCCESS_RATE:.0%}\n"
            f"  みんかぶのスクレイピングに失敗している可能性があります。\n"
            f"  make_mover_report.py を再実行してから再送してください。"
        )

    print(
        f"[OK] movers_raw チェック通過: {total} 銘柄 / "
        f"みんかぶニュース {news_ok}/{total} ({news_rate:.0%})"
    )


# ---------------------------------------------------------------------------
# セクター: sector_raw
# ---------------------------------------------------------------------------

def check_sector_raw(path: Path, target_date: str | None = None) -> None:
    """
    sector_raw.md を検証する。
    - ファイルが存在しない → 中止
    - 生成日時が target_date と不一致 → 中止
    - データ基準日が target_date から SECTOR_DATA_MAX_LAG_DAYS 超 → 中止
    - セクションエントリ（### N.）が 0 件 → 中止
    """
    if not path.exists():
        raise SystemExit(
            f"[ERROR] 生データが存在しません: {path}\n"
            f"  make_sector_raw.py を先に実行してください。"
        )
    text = path.read_text(encoding="utf-8")
    if target_date:
        _check_generated_date(path, text, target_date)

        # データ基準日チェック（週次データのため一定の遅れは許容）
        m = re.search(r"\*\*データ基準日\*\*[^\d]*(\d{4}-\d{2}-\d{2})", text)
        if m:
            data_date = date.fromisoformat(m.group(1))
            expected = date.fromisoformat(target_date)
            lag = (expected - data_date).days
            if lag > SECTOR_DATA_MAX_LAG_DAYS:
                raise SystemExit(
                    f"[ERROR] 送信を中止しました。\n"
                    f"  {path.name} のデータ基準日が {data_date} です（{lag}日前）。\n"
                    f"  許容遅延は {SECTOR_DATA_MAX_LAG_DAYS} 日以内です。ETLを再実行してください。"
                )

    # セクター行数チェック（パフォーマンス表の行 or セクター見出し）
    entries = re.findall(r"^### \d+\.", text, re.MULTILINE)
    if len(entries) == 0:
        # make_sector_raw.py の出力形式（### セクター名）でも許容
        entries = re.findall(r"^### .+", text, re.MULTILINE)
    if len(entries) == 0:
        raise SystemExit(
            f"[ERROR] 送信を中止しました。\n"
            f"  {path.name} のセクションエントリが 0 件です。ETL取得に失敗している可能性があります。\n"
            f"  make_sector_raw.py を再実行してから再送してください。"
        )
    print(f"[OK] sector_raw チェック通過: {len(entries)} エントリ")


# ---------------------------------------------------------------------------
# アイデア: ideas_raw
# ---------------------------------------------------------------------------

def check_ideas_raw(path: Path, target_date: str | None = None) -> None:
    """
    ideas_raw.md を検証する。
    - ファイルが存在しない → 中止
    - ファイル名の日付が target_date と不一致 → 中止（ヘッダーに生成日時なし）
    - 候補銘柄エントリ（### N.）が 0 件 → 中止
    """
    if not path.exists():
        raise SystemExit(
            f"[ERROR] 生データが存在しません: {path}\n"
            f"  アイデアETLを先に実行してください。"
        )
    if target_date:
        # ideas_rawはヘッダーに生成日時フィールドがないのでファイル名で判断
        m = re.match(r"(\d{4}-\d{2}-\d{2})", path.name)
        if m:
            file_date = date.fromisoformat(m.group(1))
            expected = date.fromisoformat(target_date)
            if file_date != expected:
                raise SystemExit(
                    f"[ERROR] 送信を中止しました。\n"
                    f"  {path.name} の日付が {file_date} です（期待: {expected}）。\n"
                    f"  古いファイルが残っている可能性があります。ETLを再実行してください。"
                )

    text = path.read_text(encoding="utf-8")
    entries = re.findall(r"^### \d+\.", text, re.MULTILINE)
    if len(entries) == 0:
        raise SystemExit(
            f"[ERROR] 送信を中止しました。\n"
            f"  {path.name} の候補銘柄エントリが 0 件です。ETL取得に失敗している可能性があります。\n"
            f"  アイデアETLを再実行してから再送してください。"
        )
    print(f"[OK] ideas_raw チェック通過: {len(entries)} 銘柄")


# ---------------------------------------------------------------------------
# 個別銘柄: deep_dive data
# ---------------------------------------------------------------------------

def check_deep_dive_data(path: Path, target_date: str | None = None) -> None:
    """
    deep_dive の生データファイルを検証する。
    - ファイルが存在しない → 中止
    - 生成日時が target_date と不一致 → 中止
    - Yahoo ニュースと掲示板が両方 0 件 → 中止（片方だけなら WARN）
    """
    if not path.exists():
        raise SystemExit(
            f"[ERROR] 生データが存在しません: {path}\n"
            f"  deep_dive.py --code {{コード}} を先に実行してください。"
        )
    text = path.read_text(encoding="utf-8")
    if target_date:
        _check_generated_date(path, text, target_date)

    news_section = re.search(r"## Yahoo Finance ニュース\n(.*?)(?=\n---|\Z)", text, re.DOTALL)
    news_count = len([
        l for l in (news_section.group(1).splitlines() if news_section else [])
        if l.strip().startswith("-")
    ])

    bbs_section = re.search(r"## Yahoo掲示板.*?\n(.*?)(?=\n---|\Z)", text, re.DOTALL)
    bbs_count = len([
        l for l in (bbs_section.group(1).splitlines() if bbs_section else [])
        if l.strip().startswith(">")
    ])

    if news_count == 0 and bbs_count == 0:
        raise SystemExit(
            f"[ERROR] 送信を中止しました。\n"
            f"  {path.name} の Yahoo ニュースと掲示板が両方 0 件です。\n"
            f"  Yahoo Finance のスクレイピングに失敗している可能性があります。\n"
            f"  deep_dive.py --code {{コード}} を再実行してください。"
        )
    if news_count == 0:
        print(f"[WARN] Yahoo ニュース 0 件（掲示板 {bbs_count} 件あり）。ニュース取得に失敗した可能性があります。")
    if bbs_count == 0:
        print(f"[WARN] 掲示板 0 件（ニュース {news_count} 件あり）。掲示板取得に失敗した可能性があります。")

    print(f"[OK] deep_dive_data チェック通過: ニュース {news_count} 件 / 掲示板 {bbs_count} 件")
