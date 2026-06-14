"""立花証券 e支店 API から動意銘柄レポート用の追加コンテキストを取得して raw に追記する。

既存 `{date}_movers_raw.md` の末尾に「## 立花証券 e支店 API 追加情報」セクションを追記する。
- AI 市況: ストップ高 / 新高値 / 新安値 / 売買代金上位 / 寄付後上昇率/下落率
- QUICK NQN 個別銘柄解説: 動意理由が明示された解説記事（最重要）
- 動意銘柄の信用残・逆日歩スナップ

使い方:
    python fetch_tachibana_mover_context.py [--date YYYY-MM-DD] [--news-limit 500]
"""
from __future__ import annotations

import argparse
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from lib.tachibana_client import GNL_LABEL, TachibanaClient

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
MARKET_DIR = REPO_ROOT / "market" / "daily"

# 動意レポートに使う立花カテゴリ
MOVER_GENRES = {
    "3001": "QUICK 個別銘柄解説（動意理由）",
    "60030": "AI 市況・材料発生",
    "60090": "AI 市況・ストップ高",
    "60100": "AI 市況・新高値",
    "60101": "AI 市況・新安値",
    "60110": "AI 市況・値上がり率",
    "60120": "AI 市況・値下がり率",
    "60130": "AI 市況・売買代金上位",
    "60140": "AI 市況・寄付後上昇率",
    "60141": "AI 市況・寄付後下落率",
    "6536": "QUICK 銘柄ラウンドアップ",
}

ISSUE_CODE_PATTERN = re.compile(r"^### (\d{4}[A-Z]?) ")


def extract_issue_codes(raw_md: str) -> list[str]:
    """movers_raw.md から銘柄コードを抽出（行頭 `### 6501 銘柄名 [プライム]` 形式）。"""
    codes: list[str] = []
    seen: set[str] = set()
    for line in raw_md.splitlines():
        m = ISSUE_CODE_PATTERN.match(line)
        if m:
            c = m.group(1)
            if c not in seen:
                seen.add(c)
                codes.append(c)
    return codes


def format_news_section(cli: TachibanaClient, limit: int) -> str:
    """動意レポート関連カテゴリのニュースを抽出して Markdown 化。"""
    all_news = cli.get_news_head(limit=limit)
    by_gnl: dict[str, list[dict]] = defaultdict(list)
    for n in all_news:
        gnl = n.get("p_GNL", "")
        if gnl in MOVER_GENRES:
            by_gnl[gnl].append(n)

    lines: list[str] = []
    # 動意理由系を先頭（重要度順）
    priority_order = ["3001", "60090", "60100", "60101", "60030", "60130", "60110", "60120", "60140", "60141", "6536"]
    for gnl in priority_order:
        items = by_gnl.get(gnl, [])
        if not items:
            continue
        label = MOVER_GENRES[gnl]
        lines.append(f"### {label}  ({len(items)} 件)")
        lines.append("")
        for n in items:
            dt = f"{n['p_DT']} {n['p_TM']}"
            isl = n.get("p_ISL", "")
            isl_str = f" [銘柄: {isl}]" if isl else ""
            lines.append(f"- **{dt}**{isl_str} {n['_decoded_title']}")
        lines.append("")
    return "\n".join(lines)


def format_credit_section(cli: TachibanaClient, codes: list[str]) -> str:
    """動意銘柄の信用残・逆日歩を一括取得して Markdown 化。120 銘柄ごとに分割。"""
    if not codes:
        return "(動意銘柄リストが空のためスキップ)"
    lines: list[str] = []
    margin_all: list[dict] = []
    hibu_all: list[dict] = []
    for i in range(0, len(codes), 120):
        chunk = codes[i:i+120]
        margin_all.extend(cli.get_credit_margin(chunk))
        hibu_all.extend(cli.get_short_borrowing_cost(chunk))

    margin_by_code = {m.get("sIssueCode", ""): m for m in margin_all}
    hibu_by_code = {h.get("sIssueCode", ""): h for h in hibu_all}

    lines.append(f"### 信用残・逆日歩スナップショット  （{len(codes)} 銘柄）")
    lines.append("")
    lines.append("| 銘柄 | 信用残買残(合算) | 売残(合算) | 前週比買残 | 逆日歩 | 更新日 |")
    lines.append("|---|---|---|---|---|---|")
    for code in codes:
        m = margin_by_code.get(code, {})
        h = hibu_by_code.get(code, {})
        bbq = m.get("pMBBQ", "-")
        bsq = m.get("pMBSQ", "-")
        bnq = m.get("pMBNQ", "-")
        hibu = h.get("pBWRQ", "") or "なし"
        d = m.get("pMBD", "-")
        lines.append(f"| {code} | {bbq} | {bsq} | {bnq} | {hibu} | {d} |")
    return "\n".join(lines)


def append_tachibana_section(raw_path: Path, news_section: str, credit_section: str) -> None:
    """既存の movers_raw.md の末尾に立花セクションを追記。重複追記を避けるため既存セクションは削除して上書き。"""
    existing = raw_path.read_text(encoding="utf-8")
    # 既存の立花セクションを削除（再実行対応）
    marker = "\n---\n\n## 立花証券 e支店 API 追加情報"
    if marker in existing:
        existing = existing.split(marker)[0].rstrip() + "\n"

    appended = f"""{existing}

---

## 立花証券 e支店 API 追加情報

> 取得日時: {datetime.now().strftime('%Y-%m-%d %H:%M JST')}
> データソース: 立花証券 e支店 API デモ環境（QUICK NQN / TDNet AI / AI 市況 / 信用残・逆日歩）

### 動意関連ニュース（QUICK NQN 個別銘柄解説 + AI 市況）

{news_section}

### 動意銘柄の需給スナップショット

{credit_section}
"""
    raw_path.write_text(appended, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--news-limit", type=int, default=500)
    args = parser.parse_args()

    raw_path = MARKET_DIR / f"{args.date}_movers_raw.md"
    if not raw_path.exists():
        print(f"[ERROR] {raw_path} が存在しません。make_mover_report.py を先に実行してください。", file=sys.stderr)
        sys.exit(1)

    print(f"[INFO] {raw_path.name} 読込中...")
    raw_md = raw_path.read_text(encoding="utf-8")
    codes = extract_issue_codes(raw_md)
    print(f"[INFO] 動意銘柄 {len(codes)} 件抽出")

    print("[INFO] 立花証券 API 接続...")
    cli = TachibanaClient.from_env()
    cli.login()
    print("[OK] login")

    print(f"[INFO] 動意関連ニュース取得（最大 {args.news_limit} 件）...")
    news_section = format_news_section(cli, args.news_limit)

    print(f"[INFO] 信用残・逆日歩取得（{len(codes)} 銘柄）...")
    credit_section = format_credit_section(cli, codes)

    print(f"[INFO] {raw_path.name} に立花セクション追記...")
    append_tachibana_section(raw_path, news_section, credit_section)
    print(f"[OK] 完了: {raw_path}")


if __name__ == "__main__":
    main()
