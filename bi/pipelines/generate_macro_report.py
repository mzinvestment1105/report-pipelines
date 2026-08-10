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

# 最新終値の取得は全レポート共通の lib/snapshot_utils に集約（stale 事故を一元的に防ぐ）
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.snapshot_utils import get_latest_close, is_stale_close  # noqa: E402

EXIT_OK   = 0
EXIT_ERR  = 1
EXIT_SKIP = 2

# yfinance ティッカー
SNAPSHOT_TICKERS = {
    "日経平均":         "^N225",
    "日経先物":         "NIY=F",
    "S&P500":          "^GSPC",
    "ドル円":           "USDJPY=X",
    "金(Gold)":        "GC=F",
    "BTC":             "BTC-USD",
    "米10年債":         "^TNX",
    "米VIX":            "^VIX",
    "日経VI":           "^N225VI",
}

# 小数2桁を維持する銘柄（為替・金利）。それ以外（指数・株価指数・コモディティ・暗号資産）は
# 整数表示（PM 2026-06-27 指示：指数の小数は不要・読みづらいだけ）。前日比は「円整数 / %小数1桁」、
# 区切りはスラッシュで統一する。
_DECIMAL_NAMES = {"ドル円", "米10年債", "米VIX", "日経VI"}

# 市場別サマリー（PM 2026-07-07 指示）で使う J-Quants v2 指数コード
# （公式 spec jpx-jquants.com/ja/spec/idx-bars-daily/indexcodes・プラン制限なし）。
#   0500 = 東証プライム市場指数 / 0501 = 東証スタンダード市場指数 / 0070 = 東証グロース市場250指数
# グロースは市場代表として一般に引用される「グロース市場250指数」（旧マザーズ指数の後継）を採用
# （0502=グロース市場指数は使わない。0075 は REIT 指数のため使用不可）。
SEGMENT_INDEX_CODES = {
    "プライム": "0500",
    "スタンダード": "0501",
    "グロース": "0070",
}

# 市場区分（MarketCodeName）と ETF/REIT 除外の結合元。screening_master は生成時点で
# 個別株のみを収録（ETF・REIT・上場投信は除外済み）のため、これと結合することで
# 市場別サマリーのブレッドス集計が自然に個別株のみに絞られる。
SCREENING_MASTER_PATH = REPO_ROOT / "bi" / "outputs" / "screening_master.parquet"

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
    """市況スナップショットを取得する（取得は lib/snapshot_utils.get_latest_close に集約）。

    各銘柄について「Yahoo chart 日足 + meta.regularMarketPrice(遅延しない最新セッション確定値)
    + yfinance(backup)」の中から最も新しい確定終値を採用する。日足配列の最終要素しか見ず
    古い終値で止まる旧実装の事故（PM 2026-06-27・日経平均の最高値誤掲）を構造的に防ぐ。

    target_date 指定時は target_date 以下の最新営業日を「当日」とする。各行に取得日を明記し、
    陳腐化は営業日ベース（jpholiday で祝日除外）で判定する。朝刊が前営業日を出す 1 営業日の
    ズレは許容し、それを超えて営業日が飛んでいる場合のみ警告する（カレンダー日数では判定しない）。
    """
    from datetime import date as date_cls
    lines = ["| 指標 | 水準 | 前日比 | 取得日 / 備考 |", "|------|------|--------|------|"]

    target: date_cls | None = None
    if target_date:
        try:
            target = date_cls.fromisoformat(target_date)
        except ValueError:
            target = None

    quotes = {nm: get_latest_close(tk, target) for nm, tk in SNAPSHOT_TICKERS.items()}
    cash = quotes.get("日経平均")
    for name, q in quotes.items():
        if q is None or q.close is None:
            lines.append(f"| {name} | 取得不可 | ─ | 全ソース取得失敗 |")
            continue

        decimal = name in _DECIMAL_NAMES
        # 先物の前日比（夜間セッション自前基準）は現物の前日比（前営業日終値基準）と基準日が
        # 異なり並べると矛盾に見えるため、月曜寄りに直結する「現物比」を表示する（PM 2026-06-27）。
        if name == "日経先物" and cash is not None and cash.close:
            basis = q.close - cash.close
            bpct = (q.close / cash.close - 1) * 100
            chg_txt = f"{basis:+,.0f} / {bpct:+.1f}%（現物比）"
        elif q.prev in (None, 0) or q.change is None:
            chg_txt = "─"
        elif decimal:
            chg_txt = f"{q.change:+,.2f} / {q.pct:+.2f}%"      # 為替・金利: 小数2桁維持
        else:
            chg_txt = f"{q.change:+,.0f} / {q.pct:+.1f}%"      # 指数等: 円整数・%小数1桁

        comment_parts: list[str] = [f"close={q.date.isoformat()}", f"src={q.source}"]
        if q.market_state == "REGULAR":
            comment_parts.append("場中速報")
        # 営業日ベースで陳腐化判定（カレンダー日数では判定しない）。Yahoo+CNBC のクロスソースで
        # 最新営業日の実値を取得済のため通常は発火しないが、両系統が落ちた最終保険として明示する。
        if target is not None and is_stale_close(q.date, target):
            comment_parts.append(f"⚠️ 営業日基準で陳腐化（最新営業日の値が未取得・{q.date.isoformat()}）")
        level_txt = f"{q.close:,.2f}" if decimal else f"{q.close:,.0f}"
        lines.append(
            f"| {name} | {level_txt} | {chg_txt} | {' / '.join(comment_parts)} |"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 市場別サマリー（プライム / スタンダード / グロース・PM 2026-07-07 指示）
# ---------------------------------------------------------------------------

def get_market_segment_summary(target_date: str | None = None) -> str | None:
    """市場別サマリー（3市場の指数前日比 + ブレッドス）の md ブロックを返す。

    データソースは J-Quants v2（キーは JQUANTS_API_KEY 環境変数・GHA でも動く HTTP）:
    - 指数: /indices/bars/daily の 0500/0501/0070（SEGMENT_INDEX_CODES 参照）
    - ブレッドス: /equities/bars/daily の対象日・前営業日 2 回取得を、screening_master の
      MarketCodeName（市場区分）と Code 先頭4桁で結合して市場別に集計。
      ETF・REIT・上場投信は screening_master 側で除外済みのため結合により自然に個別株のみになる。
    - 対象日 = target_date 以下の最新営業日（朝刊=前営業日・夕刊=当日）。営業日の解決は
      /markets/calendar ベースの recent_trading_days_v2 を再利用する。
    - 取得行の Date が対象日と一致しない場合は採用しない（古い日付での黙ったフォールバック禁止）。
      データが揃わない市場は行ごと省略し、「取得失敗」等のフォールバック表記は書かない（§25 流儀）。
    - 全体が取得できない場合は None を返して stderr に警告する（レポート生成自体は止めない）。
    """
    import statistics
    from datetime import date as date_cls

    try:
        import jquantsapi
        import pandas as pd

        from jq_client_utils import fetch_paginated_v2, recent_trading_days_v2

        api_key = os.environ.get("JQUANTS_API_KEY", "").strip()
        if not api_key:
            print("[WARN] 市場別サマリー: JQUANTS_API_KEY 未設定のためブロックを省略します", file=sys.stderr)
            return None

        end: date_cls | None = None
        if target_date:
            try:
                end = date_cls.fromisoformat(target_date)
            except ValueError:
                end = None
        if end is None:
            end = datetime.now(JST).date()

        client = jquantsapi.ClientV2(api_key=api_key)
        days = recent_trading_days_v2(client, 2, end=end)  # [対象日, 前営業日]
        if len(days) < 2:
            print("[WARN] 市場別サマリー: 営業日2日分を解決できずブロックを省略します", file=sys.stderr)
            return None
        day_t, day_p = days[0], days[1]

        def _index_closes(d: date_cls) -> dict[str, float]:
            """d 日の指数終値 {指数コード: 終値}。Date 不一致の行は採用しない。"""
            rows = fetch_paginated_v2(
                client, "/indices/bars/daily", params={"date": d.strftime("%Y-%m-%d")}
            )
            out: dict[str, float] = {}
            for r in rows:
                if str(r.get("Date", ""))[:10] != d.isoformat():
                    continue
                code = str(r.get("Code", ""))
                close = r.get("C")  # v2 のフィールド名は C（Close ではない）
                if code in SEGMENT_INDEX_CODES.values() and close is not None:
                    out[code] = float(close)
            return out

        def _equity_closes(d: date_cls) -> dict[str, float]:
            """d 日の全銘柄終値 {Code先頭4桁: 終値}。Date 不一致の行は採用しない。"""
            rows = fetch_paginated_v2(
                client, "/equities/bars/daily", params={"date": d.strftime("%Y-%m-%d")}
            )
            out: dict[str, float] = {}
            for r in rows:
                if str(r.get("Date", ""))[:10] != d.isoformat():
                    continue
                close = r.get("C")
                if close is None:
                    continue
                # v2 の Code は5桁（例 13010・130A0）。screening_master は4桁のため先頭4桁で結合。
                # 普通株の5桁目は常に "0"。優先株・種類株（例 75505 ゼンショー優先株）は5桁目が
                # 0 以外のためスキップし、普通株の終値を種類株が上書きする衝突を防ぐ（2026-07-07 検証で6社の実害を確認）。
                code5 = str(r.get("Code", ""))
                if not code5.endswith("0"):
                    continue
                out[code5[:4]] = float(close)
            return out

        idx_t = _index_closes(day_t)
        idx_p = _index_closes(day_p)
        eq_t = _equity_closes(day_t)
        eq_p = _equity_closes(day_p)
        if not eq_t or not eq_p:
            print("[WARN] 市場別サマリー: 株価日足が取得できずブロックを省略します", file=sys.stderr)
            return None

        # ETF/REIT 除外と市場区分の結合元（モジュール定数 SCREENING_MASTER_PATH のコメント参照）
        sm = pd.read_parquet(SCREENING_MASTER_PATH, columns=["Code", "MarketCodeName"])

        rows_out: list[str] = []
        for mkt, idx_code in SEGMENT_INDEX_CODES.items():
            codes = sm.loc[sm["MarketCodeName"] == mkt, "Code"].astype(str)
            changes: list[float] = []
            for c4 in codes:
                t_close, p_close = eq_t.get(c4), eq_p.get(c4)
                if t_close is not None and p_close:
                    changes.append((t_close / p_close - 1.0) * 100.0)
            it, ip = idx_t.get(idx_code), idx_p.get(idx_code)
            if not changes or it is None or ip is None or ip == 0:
                # データが揃わない市場は行ごと省略（フォールバック表記を書かない・§25 流儀）
                print(f"[WARN] 市場別サマリー: {mkt} のデータ欠落のため行を省略します", file=sys.stderr)
                continue
            ups = [x for x in changes if x > 0]
            downs = [x for x in changes if x < 0]
            flat = len(changes) - len(ups) - len(downs)
            # 中央値が定義できない側（全銘柄が同方向の日）は既存スナップショットの「─」流儀
            med_up = f"{statistics.median(ups):+.2f}%" if ups else "─"
            med_dn = f"{statistics.median(downs):+.2f}%" if downs else "─"
            # 市場指数は TOPIX 型（700〜2,100 pt 水準）のため公式引用どおり小数2桁を維持。
            # 前日比は §21-A のスラッシュ区切り流儀「±pt / ±%」。
            chg_txt = f"{it - ip:+,.2f}pt / {(it / ip - 1.0) * 100.0:+.2f}%"
            rows_out.append(
                f"| {mkt} | {it:,.2f} | {chg_txt} | {len(ups)} | {len(downs)} | {flat} "
                f"| {med_up} | {med_dn} |"
            )

        if not rows_out:
            print("[WARN] 市場別サマリー: 全市場でデータが揃わずブロックを省略します", file=sys.stderr)
            return None

        return "\n".join(
            [
                f"### 市場別サマリー（{day_t.month}/{day_t.day} 終値）",
                "",
                "| 市場 | 指数 | 前日比 | 値上がり | 値下がり | 変わらず | 上昇側中央値 | 下落側中央値 |",
                "|------|------|--------|--------|--------|--------|--------------|--------------|",
                *rows_out,
            ]
        )
    except Exception as e:  # noqa: BLE001 — 補助ブロックの失敗でレポート全体を止めない（既存流儀）
        print(f"[WARN] 市場別サマリー取得失敗（ブロック省略）: {type(e).__name__}: {e}", file=sys.stderr)
        return None


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
    segment_summary: str | None = None,
) -> str:
    agent_spec = (AGENTS_DIR / "macro_analyst.md").read_text(encoding="utf-8")

    # 市場別サマリー（取得失敗時は None → ブロックごと省略・注記も書かない）
    segment_section = f"\n{segment_summary}\n" if segment_summary else ""

    delta_section = ""
    if yesterday_report:
        # 前日レポートの冒頭2500字を差分コンテキストとして渡す。
        # ただし前日の H1 タイトル行（先頭 `# ...`）は除外する。
        # 巨大な羅列型タイトルをそのまま手本として渡すと、当日タイトルが
        # 同じ体裁を模倣し続けてローデータ羅列が自己増殖するため（PM 2026-07-15）。
        yr_body = yesterday_report.lstrip()
        if yr_body.startswith("# "):
            nl = yr_body.find("\n")
            yr_body = yr_body[nl + 1 :].lstrip() if nl != -1 else ""
        preview = yr_body[:2500].rstrip()
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
あなたは当ファンドのマクロ経済アナリストです。
以下の情報をもとに本日（{target_date}）のマクロレポートを生成してください。

## エージェント仕様（必ず遵守）
{agent_spec}

## 本日の市況スナップショット（yfinance 取得）
{snapshot}
{segment_section}{delta_section}{finnhub_section}
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

5. **同一材料の重複説明を絶対禁止（構造で防ぐ）**：1 材料（個別ニュース・企業・指標。例 Apple 値上げ・Oracle・ホルムズ海峡）の詳細説明（何が起きたか／なぜ重要か）は、最も適切な **1 セクションだけ**で行う。具体的に守る：
   - (a) **冒頭リードは 3 文以内**。当日の結論・方向感のみを書き、個別材料の経緯や理由を説明しない（材料はテーマ番号で 1 語触れる程度）。
   - (b) **市況スナップショット表の備考列は数値の意味のみ**（例「close=6/26・現物比 +0.5%」）。材料の物語（「Apple 値上げで値嵩株に売り集中」等）を備考に書かない。
   - (c) **先物セクションは数値関係（現物比・基準）のみ**。材料（米テック・中東等）をここで再説明しない。
   - (d) 各材料はいずれか **1 テーマでのみ**説明し、他セクションでは 1 行ポインタ（「→ テーマ◯参照」）か結論だけにする。同じ固有名詞が 4 セクション以上に説明として登場する状態を作らない。

6. **市場別サマリー表の転記（PM 2026-07-07 指示）**：上の市況スナップショットに「### 市場別サマリー（M/D 終値）」ブロックがある場合、市況スナップショット表の直後に**見出し（対象日含む）・数値とも一字一句そのまま転記**する（§21-A 準用）。再計算・丸め直し・行/列の追加・削除・並べ替え・コメント列の付加を禁止し、raw にある行だけを転記する。ブロックが無い日は市場別サマリーを書かず、取得失敗等の注記も書かない。
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
        print(f"市場別サマリー取得中 (J-Quants v2) target_date={target_date_str}...")
        segment_summary = get_market_segment_summary(target_date_str)
        if segment_summary:
            print(segment_summary)
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

    # 市場別サマリー取得（J-Quants v2・取得失敗時は None → raw からブロックごと省略）
    print(f"市場別サマリー取得中 (J-Quants v2) target_date={target_date_str}...")
    segment_summary = get_market_segment_summary(target_date_str)

    prompt = build_prompt(
        today_raw, yesterday_report, snapshot, target_date_str, finnhub_raw, tachibana_raw, segment_summary
    )

    # rawファイルに保存（後続の Claude 分析用）
    raw_output_path = MARKET_DIR / f"{target_date_str}_macro_raw.md"
    raw_output_path.write_text(prompt, encoding="utf-8")
    print(f"[OK] raw保存完了: {raw_output_path.name}")


if __name__ == "__main__":
    main()
