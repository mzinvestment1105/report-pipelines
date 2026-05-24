"""立花証券 e支店 API から銘柄発掘用ニュース（TDNet/EDINET AI 速報）を取得して raw に保存する。

旧 idea_generator.py（PMが「ゴミクズ」評価で停止中・TDNet 自前スキャン）を**立花の AI 要約データで代替**する。

取得対象：
- TDNet AI 適時開示要約（GNL=62199）：決算・人事・公開買付け等
- EDINET AI 大量保有報告（GNL=3105）：5%超保有・変更報告
- TDNet AI 自社株買い（GNL=62101）
- EDINET AI 有価証券届出書（GNL=61299）
- EDINET AI 臨時報告書（GNL=61499）
- 業績修正速報（GNL=6526）

出力: market/daily/{date}_ideas_raw.md（既存 idea_generator.py 出力と同名・置き換え可能）

使い方:
    python fetch_tachibana_ideas.py [--date YYYY-MM-DD] [--limit 2000]
"""
from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from lib.tachibana_client import TachibanaClient

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
MARKET_DIR = REPO_ROOT / "market" / "daily"

# 銘柄発掘で重要なジャンル（重要度順）
IDEA_GENRES = {
    "62101": "TDNet AI 自社株買い決議",
    "62199": "TDNet AI 適時開示",
    "3105": "EDINET AI 大量保有報告",
    "6526": "業績修正速報",
    "61299": "EDINET AI 有価証券届出書",
    "61499": "EDINET AI 臨時報告書",
    "6521": "QUICK レーティング更新",
    "6536": "QUICK 銘柄ラウンドアップ",
}

# 銘柄発掘でフィルタするキーワード（タイトルに含まれていたら優先）
HIGH_PRIORITY_KEYWORDS = [
    "上方修正", "下方修正", "業績予想",
    "公開買付", "TOB", "ＴＯＢ",
    "自社株", "自己株式取得", "自己株式消却",
    "増配", "配当予想",
    "業務提携", "資本業務提携", "戦略的提携",
    "M&A", "子会社化", "株式譲渡", "株式取得",
    "大量保有", "保有増加", "新規大株主",
    "新規事業", "新製品", "大型受注",
    "経営陣", "社長交代",
    "決算スコア",
]


def has_high_priority_keyword(title: str) -> list[str]:
    """高優先度キーワードがタイトルに含まれていれば全マッチを返す。"""
    return [k for k in HIGH_PRIORITY_KEYWORDS if k in title]


def extract_issue_codes(p_isl: str) -> list[str]:
    """p_ISL（'|' 区切り）から銘柄コードを抽出。"""
    if not p_isl:
        return []
    return [c.strip() for c in p_isl.split("|") if c.strip()]


def format_markdown(news_by_gnl: dict[str, list[dict]], target_date: str, total: int) -> str:
    lines: list[str] = []
    lines.append(f"# 銘柄発掘 raw（立花証券 AI 速報ベース）({target_date})")
    lines.append("")
    lines.append(f"- **取得日時**: {datetime.now().strftime('%Y-%m-%d %H:%M JST')}")
    lines.append(f"- **取得件数**: {total} 件")
    lines.append("- **データソース**: 立花証券 e支店 API（TDNet AI / EDINET AI / QUICK NQN）")
    lines.append("- **旧 idea_generator.py 置き換え**: 自前 TDNet スキャンを立花の QUICK AI 要約に置換")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 重要度順
    priority = ["62101", "6526", "3105", "62199", "61299", "61499", "6521", "6536"]
    for gnl in priority + sorted(set(news_by_gnl.keys()) - set(priority)):
        items = news_by_gnl.get(gnl, [])
        if not items:
            continue
        label = IDEA_GENRES.get(gnl, f"GNL={gnl}")
        lines.append(f"## {label}  ({len(items)} 件)")
        lines.append("")

        # 高優先度キーワード該当を先頭に
        high = []
        normal = []
        for n in items:
            kws = has_high_priority_keyword(n.get("_decoded_title", ""))
            if kws:
                n["_kws"] = kws
                high.append(n)
            else:
                normal.append(n)

        if high:
            lines.append(f"### ⭐ キーワード一致（{len(high)} 件）")
            lines.append("")
            for n in high:
                dt = f"{n['p_DT']} {n['p_TM']}"
                isl = n.get("p_ISL", "")
                isl_str = f" [銘柄: {isl}]" if isl else ""
                kw_str = f" [キーワード: {', '.join(n['_kws'])}]"
                lines.append(f"- **{dt}**{isl_str}{kw_str} {n['_decoded_title']}")
            lines.append("")

        if normal:
            lines.append(f"### その他（{len(normal)} 件）")
            lines.append("")
            for n in normal:
                dt = f"{n['p_DT']} {n['p_TM']}"
                isl = n.get("p_ISL", "")
                isl_str = f" [銘柄: {isl}]" if isl else ""
                lines.append(f"- **{dt}**{isl_str} {n['_decoded_title']}")
            lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--limit", type=int, default=2000, help="取得最大件数")
    args = parser.parse_args()

    print(f"[INFO] 立花証券 API 接続...")
    cli = TachibanaClient.from_env()
    cli.login()
    print(f"[OK] login")

    print(f"[INFO] 銘柄発掘向けニュース取得（最大 {args.limit} 件）...")
    all_news = cli.get_news_head(limit=args.limit)
    print(f"[INFO] 全 {len(all_news)} 件取得")

    # 銘柄発掘ジャンルのみフィルタ
    by_gnl: dict[str, list[dict]] = defaultdict(list)
    filtered = 0
    for n in all_news:
        gnl = n.get("p_GNL", "")
        if gnl in IDEA_GENRES:
            by_gnl[gnl].append(n)
            filtered += 1
    print(f"[INFO] 銘柄発掘関連 {filtered} 件抽出")

    md = format_markdown(by_gnl, args.date, filtered)
    out = MARKET_DIR / f"{args.date}_ideas_raw.md"
    out.write_text(md, encoding="utf-8")
    print(f"[OK] 保存完了: {out}")


if __name__ == "__main__":
    main()
