"""
マクロレポート自動生成スクリプト

前日との差分を検出し、新着記事があれば後続の Claude 分析用プロンプトを生成する。
新着なしの場合は exit code 2 を返す（CI での skip 判定に使う）。

使い方:
  python generate_macro_report.py             # 今日のレポートを生成
  python generate_macro_report.py --date 2026-04-05  # 日付指定
  python generate_macro_report.py --force     # 新着なしでも強制生成

exit codes:
  0  正常生成
  1  エラー（ファイル未存在等）
  2  新着記事なし（skip）
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yfinance as yf
from dotenv import load_dotenv

REPO_ROOT  = Path(__file__).resolve().parents[2]
MARKET_DIR = REPO_ROOT / "market" / "daily"
MACRO_DIR  = MARKET_DIR / "macro"
AGENTS_DIR = REPO_ROOT / "agents"
_ENV_PATH  = Path(__file__).resolve().parent / ".env"
JST = timezone(timedelta(hours=9))

EXIT_OK   = 0
EXIT_ERR  = 1
EXIT_SKIP = 2

# yfinance ティッカー
SNAPSHOT_TICKERS = {
    "日経平均":         "^N225",
    "日経225先物(夜間)": "NIY=F",
    "S&P500":          "^GSPC",
    "ドル円":           "USDJPY=X",
    "金(Gold)":        "GC=F",
    "BTC":             "BTC-USD",
    "米10年債":         "^TNX",
    "VIX":             "^VIX",
}


def _is_stale(path: Path, target_date: date, max_age_minutes: int) -> bool:
    """
    入力ファイルの鮮度判定。
    - ファイル未存在: stale
    - mtime の日付が target_date と不一致: stale
    - target_date が今日の場合、mtime が max_age_minutes を超える: stale
    """
    if not path.exists():
        return True
    mtime_jst = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).astimezone(JST)
    if mtime_jst.date() != target_date:
        return True
    today_jst = datetime.now(JST).date()
    if target_date == today_jst:
        age_min = (datetime.now(JST) - mtime_jst).total_seconds() / 60
        if age_min > max_age_minutes:
            return True
    return False


def _refresh_inputs(target_date: date) -> None:
    """
    当日分の raw 入力を再取得する。
    """
    script_dir = Path(__file__).resolve().parent
    print("[INFO] 入力データを更新します（fetch_rss.py / fetch_finnhub.py）")
    subprocess.run([sys.executable, "fetch_rss.py"], cwd=script_dir, check=True)
    subprocess.run(
        [sys.executable, "fetch_finnhub.py", "--date", target_date.strftime("%Y-%m-%d")],
        cwd=script_dir,
        check=True,
    )


# ---------------------------------------------------------------------------
# 市況スナップショット
# ---------------------------------------------------------------------------

def get_market_snapshot() -> str:
    lines = ["| 指標 | 水準 | 前日比 | 備考 |", "|------|------|--------|------|"]
    for name, ticker in SNAPSHOT_TICKERS.items():
        try:
            info = yf.Ticker(ticker).fast_info
            close = info.last_price
            prev  = info.previous_close
            if close is not None and prev is not None and prev != 0:
                chg = close - prev
                pct = chg / prev * 100
                comment = ""
                if name == "VIX":
                    if close >= 30:
                        comment = "⚠️ 恐怖ゾーン"
                    elif close <= 15:
                        comment = "楽観ゾーン"
                lines.append(f"| {name} | {close:,.2f} | {chg:+,.2f} ({pct:+.2f}%) | {comment} |")
            else:
                lines.append(f"| {name} | 取得不可 | ─ | ─ |")
        except Exception as e:
            lines.append(f"| {name} | 取得不可 | ─ | {e} |")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 新着記事の検出
# ---------------------------------------------------------------------------

def extract_urls(content: str) -> set[str]:
    """時系列インデックスセクションからURLを抽出"""
    urls: set[str] = set()
    in_timeline = False
    for line in content.splitlines():
        if "時系列インデックス" in line:
            in_timeline = True
        elif in_timeline and line.startswith("## ") and "時系列" not in line:
            break
        elif in_timeline:
            for url in re.findall(r"https?://[^\s\)\]]+", line):
                urls.add(url)
    return urls


def count_new_articles(today: str, yesterday: str | None) -> int:
    today_urls = extract_urls(today)
    if not yesterday:
        return len(today_urls)
    return len(today_urls - extract_urls(yesterday))


# ---------------------------------------------------------------------------
# プロンプト構築
# ---------------------------------------------------------------------------

def build_prompt(
    today_raw: str,
    yesterday_report: str | None,
    snapshot: str,
    target_date: str,
    finnhub_raw: str | None = None,
) -> str:
    agent_spec = (AGENTS_DIR / "macro_analyst.md").read_text(encoding="utf-8")

    delta_section = ""
    if yesterday_report:
        # 前日レポートの冒頭2500字を差分コンテキストとして渡す
        preview = yesterday_report[:2500].rstrip()
        delta_section = f"""
---
## 前日レポート（差分参照用）
以下は前日（{target_date}の前日）のレポート冒頭です。
**今日のレポートでは「前日から変わった点・新しい動き」を重点的に書いてください。**
変化がないトピックは1〜2行で簡潔にまとめ、新規・変化ありのトピックを深堀りしてください。

{preview}
---
"""

    finnhub_section = ""
    if finnhub_raw:
        finnhub_section = f"""
---
## グローバルニュース・経済カレンダー（Finnhub）
以下は Reuters/Bloomberg 等のグローバルニュースと今後の経済指標カレンダーです。
日本語のニュースと組み合わせて、マクロ環境を総合的に分析してください。
英語のニュース見出し・要約は内容を理解した上で日本語で分析に反映してください。

{finnhub_raw}
---
"""

    return f"""\
あなたは Mizuki Fund のマクロ経済アナリストです。
以下の情報をもとに本日（{target_date}）のマクロレポートを生成してください。

## エージェント仕様（必ず遵守）
{agent_spec}

## 本日の市況スナップショット（yfinance 取得）
{snapshot}
{delta_section}{finnhub_section}
## 本日のニュース生データ（{target_date}_news_raw.md 全文）
{today_raw}

---
上記情報をもとに agents/macro_analyst.md の仕様に従い、
`{target_date}.md` として出力するレポートを日本語で生成してください。
マークダウン形式で出力し、コードブロックで囲まないこと。

## 出力ルール

- Deep Research 候補セクションは出力しない（2026-05-19 PM 確定・マクロレポート廃止）
- agents/macro_analyst.md の「レポート構成」セクションに従い、市況スナップショット → 重要テーマ → 逆張りシグナル → 総合見通しの順で書く
- 専門用語は中学生レベルの注釈をつける
"""


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(_ENV_PATH)

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--force", action="store_true", help="新着なしでも強制生成")
    parser.add_argument("--snapshot-only", action="store_true", help="市況スナップショットだけ取得して表示")
    parser.add_argument("--no-ensure-fresh", action="store_true", help="入力ファイルの鮮度チェックと自動更新を行わない")
    parser.add_argument("--fresh-max-minutes", type=int, default=180, help="target_date が今日の場合の鮮度しきい値（分）")
    args = parser.parse_args()
    target_date_str: str = args.date
    target_date_obj = date.fromisoformat(target_date_str)

    # スナップショットのみモード
    if args.snapshot_only:
        print("市況データ取得中 (yfinance)...")
        print(get_market_snapshot())
        sys.exit(EXIT_OK)

    # news_raw.md を読み込む
    raw_path = MARKET_DIR / f"{target_date_str}_news_raw.md"
    finnhub_path = MARKET_DIR / f"{target_date_str}_finnhub_raw.md"

    ensure_fresh = not args.no_ensure_fresh
    if ensure_fresh:
        if target_date_obj == datetime.now(JST).date():
            if _is_stale(raw_path, target_date_obj, args.fresh_max_minutes) or _is_stale(finnhub_path, target_date_obj, args.fresh_max_minutes):
                try:
                    _refresh_inputs(target_date_obj)
                except subprocess.CalledProcessError as e:
                    print(f"[ERROR] 入力データの更新に失敗しました: {e}", file=sys.stderr)
                    sys.exit(EXIT_ERR)
        else:
            # 過去日付は自動再取得不可（fetch_rss.py は当日ファイル生成のため）
            if not raw_path.exists():
                print(f"[ERROR] {raw_path.name} が存在しません。過去日付は手動でファイルを用意してください。", file=sys.stderr)
                sys.exit(EXIT_ERR)

    if not raw_path.exists():
        print(f"[ERROR] {raw_path.name} が存在しません。fetch_rss.py を先に実行してください。", file=sys.stderr)
        sys.exit(EXIT_ERR)

    today_raw = raw_path.read_text(encoding="utf-8")

    # 前日ファイルを読み込む
    yesterday_str = (target_date_obj - timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_raw_path    = MARKET_DIR / f"{yesterday_str}_news_raw.md"
    yesterday_report_path = MACRO_DIR / f"{yesterday_str}.md"

    yesterday_raw    = yesterday_raw_path.read_text(encoding="utf-8")    if yesterday_raw_path.exists()    else None
    yesterday_report = yesterday_report_path.read_text(encoding="utf-8") if yesterday_report_path.exists() else None

    # 新着記事数チェック
    new_count = count_new_articles(today_raw, yesterday_raw)
    print(f"新着記事数: {new_count}")

    if new_count == 0 and not args.force:
        print("[SKIP] 新着記事なし")
        sys.exit(EXIT_SKIP)

    # Finnhub raw データを読み込む（任意・存在しなくてもスキップ）
    finnhub_raw: str | None = None
    if finnhub_path.exists():
        finnhub_raw = finnhub_path.read_text(encoding="utf-8")
        print(f"Finnhub データあり: {finnhub_path.name} ({len(finnhub_raw):,} 文字)")
    else:
        print(f"Finnhub データなし（{finnhub_path.name}）- fetch_finnhub.py を先に実行するとグローバルニュースが追加されます")

    # 市況スナップショット取得
    print("市況データ取得中 (yfinance)...")
    snapshot = get_market_snapshot()

    prompt = build_prompt(today_raw, yesterday_report, snapshot, target_date_str, finnhub_raw)

    # rawファイルに保存（後続の Claude 分析用）
    raw_output_path = MARKET_DIR / f"{target_date_str}_macro_raw.md"
    raw_output_path.write_text(prompt, encoding="utf-8")
    print(f"[OK] raw保存完了: {raw_output_path.name}")


if __name__ == "__main__":
    main()
