"""立花証券 e支店 API から個別銘柄レポート用の追加コンテキストを取得して raw に追記する。

既存 `research/stocks/{code}_{date}_data.md` の末尾に「## 立花証券 e支店 API 追加情報」セクションを追記する。
- TDNet AI 適時開示要約（該当銘柄のみ）
- EDINET AI 大量保有報告（該当銘柄のみ）
- 自社株買い決議・有価証券届出書・臨時報告書（該当銘柄のみ）
- 信用残・証金残・逆日歩スナップ
- 銘柄詳細情報

使い方:
    python fetch_tachibana_stock_context.py --code 6501 [--date YYYY-MM-DD] [--news-limit 200]
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from lib.tachibana_client import GNL_LABEL, TachibanaClient

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
STOCKS_DIR = REPO_ROOT / "research" / "stocks"

# 個別銘柄レポートに使う立花カテゴリ
STOCK_GENRES = {
    "62199": "TDNet AI 適時開示",
    "62101": "TDNet AI 自社株買い",
    "3105": "EDINET AI 大量保有報告",
    "61299": "EDINET AI 有価証券届出書",
    "61499": "EDINET AI 臨時報告書",
    "3001": "QUICK 個別銘柄解説",
    "6526": "業績修正",
    "6521": "QUICK レーティング更新",
}


def fetch_stock_news(cli: TachibanaClient, code: str, limit: int) -> list[dict]:
    """指定銘柄に関連するニュースを取得。p_IS でフィルタ。"""
    return cli.get_news_head(limit=limit, issue_code=code)


def format_news_section(news: list[dict], code: str) -> str:
    """銘柄関連ニュースをジャンル別に Markdown 化。"""
    if not news:
        return f"(銘柄 {code} 関連ニュースなし)"

    by_gnl: dict[str, list[dict]] = defaultdict(list)
    for n in news:
        gnl = n.get("p_GNL", "")
        by_gnl[gnl].append(n)

    lines: list[str] = [f"### 銘柄 {code} 関連ニュース（{len(news)} 件・直近順）", ""]
    # 重要度順
    priority = ["62199", "3105", "62101", "61299", "61499", "3001", "6526", "6521"]
    for gnl in priority + sorted(set(by_gnl.keys()) - set(priority)):
        items = by_gnl.get(gnl, [])
        if not items:
            continue
        label = STOCK_GENRES.get(gnl, GNL_LABEL.get(gnl, f"GNL={gnl}"))
        lines.append(f"#### {label}  ({len(items)} 件)")
        lines.append("")
        for n in items:
            dt = f"{n['p_DT']} {n['p_TM']}"
            lines.append(f"- **{dt}** {n['_decoded_title']}")
        lines.append("")
    return "\n".join(lines)


def format_credit_section(cli: TachibanaClient, code: str) -> str:
    """銘柄の信用残・証金残・逆日歩を取得して Markdown 化。"""
    margin = cli.get_credit_margin([code])
    sf = cli.get_securities_finance([code])
    hibu = cli.get_short_borrowing_cost([code])

    m = margin[0] if margin else {}
    s = sf[0] if sf else {}
    h = hibu[0] if hibu else {}

    lines = ["### 需給スナップショット（立花証券 e支店 API・最新時点）", ""]

    if m:
        lines.append("#### 信用残（一般 / 制度 / 合算）")
        lines.append("")
        lines.append(f"- **信用残日付**: {m.get('pMBD', '-')}")
        lines.append(f"- **信用残買残**: 一般 {m.get('pMBB3', '-')} / 制度 {m.get('pMBB6', '-')} / 合算 **{m.get('pMBBQ', '-')}**")
        lines.append(f"- **信用残売残**: 一般 {m.get('pMBS3', '-')} / 制度 {m.get('pMBS6', '-')} / 合算 **{m.get('pMBSQ', '-')}**")
        lines.append(f"- **信用倍率**: 一般 {m.get('pMBR3', '-')} / 制度 {m.get('pMBR6', '-')} / 合算 **{m.get('pMBRQ', '-')}**")
        lines.append(f"- **買残前週比（合算）**: {m.get('pMBNQ', '-')}")
        lines.append(f"- **売残前週比（合算）**: {m.get('pMBCQ', '-')}")
        lines.append("")

    if s:
        lines.append("#### 証金残（証券金融会社の貸借残・速報/確報）")
        lines.append("")
        status = "確報" if s.get("pSFKS") == "2" else "速報"
        lines.append(f"- **証金更新日**: {s.get('pSFD', '-')} （{status}）")
        lines.append(f"- **証金融資残**: {s.get('pSFF6', '-')} （前日比 {s.get('pSFG6', '-')}）")
        lines.append(f"- **証金貸株残**: {s.get('pSFS6', '-')} （前日比 {s.get('pSSG6', '-')}）")
        lines.append(f"- **証金差引残**: {s.get('pSFN6', '-')} （前日比 {s.get('pSFC6', '-')}）")
        lines.append(f"- **貸借倍率**: {s.get('pSFR6', '-')}")
        lines.append(f"- **回転日数**: {s.get('pSFD6', '-')}")
        lines.append("")

    if h:
        hibu_val = h.get("pBWRQ", "") or "なし（株不足ではない）"
        lines.append("#### 逆日歩")
        lines.append("")
        lines.append(f"- **逆日歩**: {hibu_val}")
        lines.append("")

    return "\n".join(lines) if lines else "(立花証券から需給データ取得できず)"


def append_to_data_file(data_path: Path, news_section: str, credit_section: str, code: str) -> None:
    """既存の deep_dive data ファイル末尾に立花セクションを追記。重複追記回避。"""
    if not data_path.exists():
        # 新規作成
        existing = f"# 銘柄 {code} 立花証券データ\n\n"
    else:
        existing = data_path.read_text(encoding="utf-8")
    marker = "\n---\n\n## 立花証券 e支店 API 追加情報"
    if marker in existing:
        existing = existing.split(marker)[0].rstrip() + "\n"
    appended = f"""{existing}

---

## 立花証券 e支店 API 追加情報

> 取得日時: {datetime.now().strftime('%Y-%m-%d %H:%M JST')}
> データソース: 立花証券 e支店 API デモ環境

{news_section}

{credit_section}
"""
    data_path.write_text(appended, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--code", required=True, help="銘柄コード（4桁・例: 6501）")
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--news-limit", type=int, default=200)
    parser.add_argument("--output", default=None, help="出力先パス（デフォルトは research/stocks/{code}_{date}_tachibana.md）")
    args = parser.parse_args()

    print(f"[INFO] 立花証券 API 接続...")
    cli = TachibanaClient.from_env()
    cli.login()
    print(f"[OK] login")

    print(f"[INFO] 銘柄 {args.code} 関連ニュース取得（最大 {args.news_limit} 件）...")
    news = fetch_stock_news(cli, args.code, args.news_limit)
    news_section = format_news_section(news, args.code)

    print(f"[INFO] 信用残・証金残・逆日歩取得...")
    credit_section = format_credit_section(cli, args.code)

    if args.output:
        out_path = Path(args.output)
    else:
        # 個別銘柄レポート向けの専用ファイルとして書き出し（既存の deep_dive raw とは別）
        out_path = STOCKS_DIR / args.code / f"{args.date}_tachibana_raw.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] {out_path} に書き込み...")
    append_to_data_file(out_path, news_section, credit_section, args.code)
    print(f"[OK] 完了: {out_path}")


if __name__ == "__main__":
    main()
