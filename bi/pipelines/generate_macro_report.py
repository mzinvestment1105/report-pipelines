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
    "米VIX":           "^VIX",
    "日経VI":          "^N225VI",
}

def _yahoo_chart_pairs(ticker: str) -> list[tuple]:
    """Yahoo Finance chart API を直叩きして (date, close) のリスト（昇順）を取得する。失敗時は空リスト。

    PM 2026-06-23: yfinance は内部の crumb/cookie 認証が Yahoo にブロックされやすく、
    日経平均(^N225) 等が古い終値で止まる事象が頻発する。chart API はその認証フローを介さず
    ブラウザ相当の UA で素直に JSON を返すため、yfinance が当日値を返せない時の二次ソースとして用いる。
    対象シンボルは yfinance と同一（^N225 / NIY=F / USDJPY=X 等）。日経VI(^N225VI) は Yahoo 側に
    存在せず 404 になるため空リストが返る（その場合は yfinance のみが情報源）。
    """
    import json as _json
    import urllib.parse as _up
    import urllib.request as _ur

    sym = _up.quote(ticker, safe="")
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=14d&interval=1d"
    try:
        req = _ur.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                )
            },
        )
        with _ur.urlopen(req, timeout=20) as resp:
            data = _json.loads(resp.read().decode("utf-8", "replace"))
        res = data["chart"]["result"][0]
        ts = res["timestamp"]
        closes = res["indicators"]["quote"][0]["close"]
        pairs = [
            (datetime.fromtimestamp(t, tz=timezone.utc).date(), float(c))
            for t, c in zip(ts, closes)
            if c is not None
        ]
        pairs.sort(key=lambda p: p[0])
        return pairs
    except Exception:
        return []


def _yf_pairs(ticker: str) -> list[tuple]:
    """yfinance の 14 日履歴から (date, close) のリスト（昇順）を取得する。失敗時は空リスト。"""
    try:
        hist = yf.Ticker(ticker).history(period="14d", auto_adjust=False)
        if hist is None or hist.empty:
            return []
        hist = hist.dropna(subset=["Close"])
        return [(idx.date(), float(c)) for idx, c in zip(hist.index, hist["Close"])]
    except Exception:
        return []


def _extract_close_prev(pairs: list[tuple], target) -> tuple | None:
    """(date, close) 昇順リストから「当日終値・前日終値・取得日」を返す。
    target 指定時は target 以下の最新営業日を当日とする（届かなければ全期間でフォールバック）。"""
    if target is not None:
        filtered = [p for p in pairs if p[0] <= target]
        if len(filtered) >= 2:
            pairs = filtered
    if len(pairs) < 2:
        return None
    close = float(pairs[-1][1])
    prev = float(pairs[-2][1])
    latest = pairs[-1][0]
    if prev == 0:
        return None
    return close, prev, latest


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

def get_market_snapshot(target_date: str | None = None) -> str:
    """yfinance から市況スナップショットを取得する。

    fast_info は実行時刻依存（東京市場閉鎖中は前日値・米国市場開場中は前日終値）で
    レポート対象日と取得日がズレる重大事故が発生したため、history() ベースに改修。

    target_date が指定された場合: その日付以下の最新確定終値を「当日」とする
    指定なしの場合: 最新確定終値を使う

    各行に「取得日（YYYY-MM-DD）」を必ず備考欄に明記し、target_date と乖離がある
    場合は ⚠️ で警告表示する。
    """
    from datetime import date as date_cls
    lines = ["| 指標 | 水準 | 前日比 | 取得日 / 備考 |", "|------|------|--------|------|"]

    target: date_cls | None = None
    if target_date:
        try:
            target = date_cls.fromisoformat(target_date)
        except ValueError:
            target = None

    for name, ticker in SNAPSHOT_TICKERS.items():
        # 1) yfinance（一次ソース・14 日分で週末/休場を吸収）
        snap = _extract_close_prev(_yf_pairs(ticker), target)
        source = "yfinance"

        # 2) Yahoo chart API フォールバック: yfinance が失敗、または target 指定時に当日値へ届いて
        #    いない場合のみ。取得日が yfinance と同等以上に新しい時だけ採用する（古い値での後退を防ぐ）。
        need_fallback = snap is None or (target is not None and snap[2] < target)
        if need_fallback:
            fb = _extract_close_prev(_yahoo_chart_pairs(ticker), target)
            if fb is not None and (snap is None or fb[2] >= snap[2]):
                snap = fb
                source = "yahoo_chart"

        if snap is None:
            lines.append(f"| {name} | 取得不可 | ─ | 全ソース取得失敗 |")
            continue

        close, prev, latest_date = snap
        chg = close - prev
        pct = chg / prev * 100

        comment_parts: list[str] = [f"close={latest_date.isoformat()}", f"src={source}"]
        if target is not None and latest_date != target:
            # 取得日と対象日が乖離している場合は警告マーク
            comment_parts.append(f"⚠️ target={target.isoformat()} と乖離")
        if name == "米VIX":
            if close >= 30:
                comment_parts.append("⚠️ 恐怖ゾーン")
            elif close <= 15:
                comment_parts.append("楽観ゾーン")

        lines.append(
            f"| {name} | {close:,.2f} | {chg:+,.2f} ({pct:+.2f}%) | {' / '.join(comment_parts)} |"
        )
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
    tachibana_raw: str | None = None,
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

    tachibana_section = ""
    if tachibana_raw:
        tachibana_section = f"""
---
## 立花証券 e支店 API ニュース速報（QUICK NQN / TDNet AI / AI 市況）
以下は立花証券 e支店 API 経由で取得した日本株専門ニュース速報です。完全日本語・既に整理済。
- **QUICK NQN**: 東証セッション速報・米国株市況・為替時系列・日経先物・日本株 ADR・QUICK レーティング更新・業績修正・銘柄ラウンドアップ
- **AI 市況（ボード）**: 寄り前注文予想・材料発生・ストップ高/新高値/新安値・売買代金上位・寄付後上昇率/下落率
- **TDNet/EDINET AI 速報**: 個別銘柄の決算/人事/公開買付け/自社株買い/大量保有報告/有価証券届出書 を AI 要約済
このセクションは**マクロレポートの市況スナップショット・重要テーマ・構造的リスク**の分析素材として活用してください。
- マクロ環境分析には QUICK NQN セクション（セッション速報・為替・米国株・先物）を最優先で参照
- 個別銘柄の動意分析は TDNet AI 速報・QUICK 個別銘柄解説を参照

{tachibana_raw}
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

## 出力ルール（重大品質違反は再生成）

- Deep Research 候補セクションは出力しない（2026-05-19 PM 確定・マクロレポート廃止）
- agents/macro_analyst.md の「レポート構成」セクションに従い、市況スナップショット → 重要テーマ → 逆張りシグナル → 総合見通しの順で書く

### 重要ルール（PM 2026-05-20 明示指示・違反は重大品質違反）

1. **英語原文の転記は完全禁止**：Reuters・Bloomberg・WSJ・Barron's・CNBC 等の英語見出し・本文・引用句を **1 文字も貼らない**。「Bonds Bury Stocks」「Asian stocks extend losing streak」「Trump says X」等の英語フレーズは禁止。英語ニュースは内容を理解した上で**完全に日本語で書き直す**。媒体名は日本語表記推奨（Reuters → ロイター・Barron's → バロンズ）。**英語固有名詞単独（Trump・FRB・FOMC・Nvidia）は OK、文・節として英語を残すのは NG**

2. **時刻表記は JST（日本時間）統一**：全イベント・全ニュースの時刻は **JST に統一**する。米国時間で言及する場合は**必ず JST を主体にして米国時間を括弧で補足**（例：「**5/22 06:00 JST 頃**（米国時間 5/21 引け後）：Nvidia 決算」）。「5/21 米国引け後」のような JST 換算なしの単独表記は**禁止**。標準換算：米市場引け 16:00 EDT = JST 翌朝 05:00、FOMC 14:00 EDT = JST 翌朝 03:00、米経済指標 08:30 EDT = JST 21:30

3. **専門用語に中学生レベルの注釈必須**：略語・業界用語・固有サービス名・経済指標名は**初出時に括弧で平易な説明を付ける**。「**初めて見た人が読める**」を基準とする。例：「**量子ドットレーザー**（ナノサイズの半導体結晶で発光する光通信用レーザー素子）」「**Philly Fed**（フィラデルフィア連銀の製造業景況感指数・米景気の先行指標）」。金融基本用語（PER・PBR・ROE 等）は注釈不要

4. **見出しは「事実 + 因果」で完結**：★★☆・▼▼☆ 等のスコアの後の見出しを「テーマ名」だけで終わらせず、「**何が起きて → なぜ重要か**」が一目で分かる形にする
"""


# ---------------------------------------------------------------------------
# メイン
# ---------------------------------------------------------------------------

def main() -> None:
    # Windows cp932 環境でも絵文字を含む出力が落ちないよう UTF-8 強制
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

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
        print(f"市況データ取得中 (yfinance) target_date={target_date_str}...")
        print(get_market_snapshot(target_date_str))
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

    # 立花証券 e支店 API ニュース raw データを読み込む（任意・存在しなくてもスキップ）
    tachibana_path = MARKET_DIR / f"{target_date_str}_tachibana_news_raw.md"
    tachibana_raw: str | None = None
    if tachibana_path.exists():
        tachibana_raw = tachibana_path.read_text(encoding="utf-8")
        print(f"立花証券 e支店ニュースあり: {tachibana_path.name} ({len(tachibana_raw):,} 文字)")
    else:
        print(f"立花証券 e支店ニュースなし（{tachibana_path.name}）- fetch_tachibana_news.py を先に実行すると QUICK/TDNet AI 速報が追加されます")

    # 市況スナップショット取得（target_date を渡して取得日の整合性を保証）
    print(f"市況データ取得中 (yfinance) target_date={target_date_str}...")
    snapshot = get_market_snapshot(target_date_str)

    prompt = build_prompt(today_raw, yesterday_report, snapshot, target_date_str, finnhub_raw, tachibana_raw)

    # rawファイルに保存（後続の Claude 分析用）
    raw_output_path = MARKET_DIR / f"{target_date_str}_macro_raw.md"
    raw_output_path.write_text(prompt, encoding="utf-8")
    print(f"[OK] raw保存完了: {raw_output_path.name}")


if __name__ == "__main__":
    main()
