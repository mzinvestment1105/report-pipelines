"""
マクロレポート自動生成スクリプト

前日との差分を検出し、新着記事があれば Claude API で sonnet_macro.md を生成する。
新着なしの場合は exit code 2 を返す（CI での skip 判定に使う）。

使い方:
  python generate_macro_report.py             # 今日のレポートを生成
  python generate_macro_report.py --date 2026-04-05  # 日付指定
  python generate_macro_report.py --force     # 新着なしでも強制生成

exit codes:
  0  正常生成
  1  エラー（API失敗・ファイル未存在等）
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


def _latest_deep_research_date(macro_dir: Path) -> date | None:
    """
    market/daily/macro/ の *_deep_research.md から最新日付を返す。
    ファイル名先頭の YYYY-MM-DD を日付として解釈する。
    """
    dates: list[date] = []
    for p in macro_dir.glob("*_deep_research.md"):
        name = p.name
        if len(name) >= 10:
            try:
                dates.append(date.fromisoformat(name[:10]))
            except ValueError:
                continue
    return max(dates) if dates else None


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
    deep_research: str | None = None,
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

    deep_research_section = ""
    if deep_research:
        deep_research_section = f"""
---
## Deep Research 定性分析（外部入力）
以下は Perplexity 等の Deep Research による詳細調査結果です。
定量データ・一次情報を積極的に活用し、レポートの各テーマセクションに反映してください。

{deep_research}
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
{deep_research_section}
---
上記情報をもとに agents/macro_analyst.md の仕様に従い、
`{target_date}_sonnet_macro.md` として出力するレポートを日本語で生成してください。
マークダウン形式で出力し、コードブロックで囲まないこと。

## ⚠️ 必須出力ルール（絶対に省略禁止）

レポートの**最後**に、必ず以下のフォーマットで「Deep Research 候補」セクションを出力すること。
このセクションは**省略不可・「なし」の場合もその旨を明記**すること。
候補が思いつかない場合でも「Deep Research 候補なし（本日は全テーマ解像度十分）」と書くこと。

```
## 📌 Deep Research 候補

- [ ] 〇〇について（理由: △△が不明確なため）
- [ ] 〇〇について（理由: △△の影響度を定量化したい）
```

上記フォーマットを守り、「このレポートで重要だが解像度が足りない」「掘り下げると投資判断が変わりうる」論点を3〜5件リストアップすること。
"""


def build_deep_research_prompt(
    today_raw: str,
    target_date: str,
    finnhub_raw: str | None = None,
) -> str:
    """
    外部調査用の Deep Research プロンプトを生成する。
    """
    finnhub_section = finnhub_raw[:8000] if finnhub_raw else "（Finnhubデータなし）"
    return f"""# マクロ Deep Research プロンプト ({target_date})

以下の生データをもとに、{target_date} 時点の日本株マクロ判断に直結する深掘りを実施してください。

## 重点論点（必須）
1. 要人発言（日本総理・米国大統領・FRB・日銀）で市場に効く新情報
2. 直近1週間の地政学・原油・金利変動の因果整理
3. 今後1〜2週間の日本株セクター別インパクト（強弱）
4. 反証シナリオ（強気/弱気の崩れる条件）

## 出力要件
- 日本語
- 事実/根拠/含意を分けて記述
- 数値は可能な限り前回比・予想比を付記
- 末尾に「追加確認が必要な論点」を3件以上

## 当日ニュース生データ（抜粋）
{today_raw[:12000]}

## Finnhubデータ（抜粋）
{finnhub_section}
"""


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    load_dotenv(_ENV_PATH)

    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--force", action="store_true", help="新着なしでも強制生成")
    parser.add_argument("--snapshot-only", action="store_true", help="市況スナップショットだけ取得して表示（API不要）")
    parser.add_argument("--no-ensure-fresh", action="store_true", help="入力ファイルの鮮度チェックと自動更新を行わない")
    parser.add_argument("--fresh-max-minutes", type=int, default=180, help="target_date が今日の場合の鮮度しきい値（分）")
    parser.add_argument("--deep-research-max-days", type=int, default=7, help="Deep Research を任意扱いできる最大経過日数")
    parser.add_argument("--allow-stale-deep-research", action="store_true", help="Deep Research 7日ルールを一時的に無効化")
    args = parser.parse_args()
    target_date_str: str = args.date
    target_date_obj = date.fromisoformat(target_date_str)

    # スナップショットのみモード（Claude Code手動生成時に使う）
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

    # Deep Research 7日ルール:
    # 最新の Deep Research が一定日数より古い場合は、当日分 Deep Research を必須化する。
    latest_dr_date = _latest_deep_research_date(MACRO_DIR)
    needs_fresh_deep = (
        latest_dr_date is None
        or (target_date_obj - latest_dr_date).days > args.deep_research_max_days
    )
    if needs_fresh_deep and not args.allow_stale_deep_research:
        required_dr_path = MACRO_DIR / f"{target_date_str}_deep_research.md"
        if not required_dr_path.exists():
            latest_text = latest_dr_date.isoformat() if latest_dr_date else "なし"
            print(
                "[ERROR] Deep Research が古いため当日分が必須です。\n"
                f"  最新Deep Research日付: {latest_text}\n"
                f"  必須ファイル: {required_dr_path.name}\n"
                "  対応: 当日分 Deep Research を実行して保存後、再実行してください。",
                file=sys.stderr,
            )
            sys.exit(EXIT_ERR)

    # Deep Research データを読み込む（当日ファイル優先）
    deep_research_path = MACRO_DIR / f"{target_date_str}_deep_research.md"
    deep_research_prompt_path = MACRO_DIR / f"{target_date_str}_deep_research_prompt.md"
    deep_research: str | None = None
    if deep_research_path.exists():
        deep_research = deep_research_path.read_text(encoding="utf-8")
        print(f"Deep Research データあり: {deep_research_path.name} ({len(deep_research):,} 文字)")
    else:
        print(f"Deep Research なし({deep_research_path.name}) -- Perplexity 結果をこのパスに保存すると自動統合されます")

    # 市況スナップショット取得
    print("市況データ取得中 (yfinance)...")
    snapshot = get_market_snapshot()

    # Deep Research プロンプトを必ず発行
    dr_prompt = build_deep_research_prompt(today_raw, target_date_str, finnhub_raw)
    deep_research_prompt_path.write_text(dr_prompt, encoding="utf-8")
    print(f"[OK] Deep Researchプロンプト保存: {deep_research_prompt_path.name}")

    prompt = build_prompt(today_raw, yesterday_report, snapshot, target_date_str, finnhub_raw, deep_research)

    # rawファイルに保存（Claude Code インタラクティブ生成用）
    raw_output_path = MARKET_DIR / f"{target_date_str}_macro_raw.md"
    raw_output_path.write_text(prompt, encoding="utf-8")
    print(f"[OK] raw保存完了: {raw_output_path.name}")
    print(f"   -> Claude Code にマクロレポート生成を依頼してください。")


if __name__ == "__main__":
    main()
