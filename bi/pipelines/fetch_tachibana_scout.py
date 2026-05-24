"""立花証券 e支店 API から Scout Radar 用ニュース（寄り前注文予想・材料発生・売買代金上位）を取得する。

Scout Radar の核となる「目で見るに値する候補」を立花の AI 市況から抽出：
- 寄り前注文予想（GNL=60010）：朝の寄り付き前に動意候補をリストアップ
- 材料発生（GNL=60030）：リアルタイム値動きで「今動いている」銘柄
- ストップ高 / 新高値 / 新安値（GNL=60090 / 60100 / 60101）
- 売買代金上位（GNL=60130）
- QUICK レーティング更新（GNL=6521）：アナリスト評価変化

Scout Radar 本体（[dev/scout_radar_design.md](../../dev/scout_radar_design.md)）が PMの初期ルール確定待ちのため、
本スクリプトはまず「raw データ取得」のみを担当。Scout Radar 完成後に統合する。

出力: market/daily/{date}_scout_raw.md

使い方:
    python fetch_tachibana_scout.py [--date YYYY-MM-DD] [--limit 1000]
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from lib.tachibana_client import TachibanaClient

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
MARKET_DIR = REPO_ROOT / "market" / "daily"

# Scout Radar で使う立花カテゴリ
SCOUT_GENRES = {
    "60010": "🔮 寄り前注文予想（値上がり / 値下がり / 売買急増）",
    "60030": "📡 材料発生（リアルタイム値動き）",
    "60090": "🚀 ストップ高一覧",
    "60100": "📈 新高値一覧",
    "60101": "📉 新安値一覧",
    "60130": "💰 売買代金上位",
    "60140": "⬆ 寄付後上昇率",
    "60141": "⬇ 寄付後下落率",
    "60110": "値上がり率上位",
    "60120": "値下がり率上位",
    "6521": "🏷 QUICK レーティング更新",
    "3001": "📰 QUICK 個別銘柄解説（動意理由）",
}


def extract_issue_codes_from_isl(p_isl: str) -> list[str]:
    """p_ISL（'|' 区切り）から銘柄コードを抽出。"""
    if not p_isl:
        return []
    return [c.strip() for c in p_isl.split("|") if c.strip()]


def count_mentioned_codes(news_list: list[dict]) -> Counter:
    """ニュース全体で言及された銘柄コードの頻度を集計（Scout Radar の候補スコアの基礎）。"""
    counter: Counter = Counter()
    for n in news_list:
        for c in extract_issue_codes_from_isl(n.get("p_ISL", "")):
            counter[c] += 1
    return counter


def format_markdown(news_by_gnl: dict[str, list[dict]], top_codes: list[tuple[str, int]], target_date: str, total: int) -> str:
    lines: list[str] = []
    lines.append(f"# Scout Radar raw（立花証券 AI 市況ベース）({target_date})")
    lines.append("")
    lines.append(f"- **取得日時**: {datetime.now().strftime('%Y-%m-%d %H:%M JST')}")
    lines.append(f"- **取得件数**: {total} 件")
    lines.append("- **データソース**: 立花証券 e支店 API（QUICK AI 市況）")
    lines.append("")
    lines.append("---")
    lines.append("")

    # 言及銘柄頻度ランキング（Scout Radar 候補抽出の素材）
    lines.append("## 🏆 言及銘柄頻度ランキング（Scout 候補抽出素材）")
    lines.append("")
    lines.append("立花 AI 市況で複数のセクションに登場する銘柄ほど「今動いている」可能性が高い。Scout Radar の候補スコア基礎データ。")
    lines.append("")
    lines.append("| 順位 | 銘柄コード | 言及件数 |")
    lines.append("|---|---|---|")
    for i, (code, n) in enumerate(top_codes[:30], 1):
        lines.append(f"| {i} | {code} | {n} |")
    lines.append("")

    # ジャンル別詳細
    priority = ["60010", "60030", "60090", "60100", "60101", "60130", "60140", "60141", "60110", "60120", "6521", "3001"]
    for gnl in priority:
        items = news_by_gnl.get(gnl, [])
        if not items:
            continue
        label = SCOUT_GENRES[gnl]
        lines.append(f"## {label}  ({len(items)} 件)")
        lines.append("")
        for n in items:
            dt = f"{n['p_DT']} {n['p_TM']}"
            isl = n.get("p_ISL", "")
            isl_str = f" [銘柄: {isl}]" if isl else ""
            lines.append(f"- **{dt}**{isl_str} {n['_decoded_title']}")
        lines.append("")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--limit", type=int, default=1000, help="取得最大件数")
    args = parser.parse_args()

    print(f"[INFO] 立花証券 API 接続...")
    cli = TachibanaClient.from_env()
    cli.login()
    print(f"[OK] login")

    print(f"[INFO] Scout Radar 向けニュース取得（最大 {args.limit} 件）...")
    all_news = cli.get_news_head(limit=args.limit)

    # Scout ジャンルのみフィルタ
    by_gnl: dict[str, list[dict]] = defaultdict(list)
    filtered: list[dict] = []
    for n in all_news:
        gnl = n.get("p_GNL", "")
        if gnl in SCOUT_GENRES:
            by_gnl[gnl].append(n)
            filtered.append(n)
    print(f"[INFO] Scout 関連 {len(filtered)} 件抽出")

    # 銘柄言及頻度
    code_count = count_mentioned_codes(filtered)
    top_codes = code_count.most_common(30)
    print(f"[INFO] ユニーク銘柄 {len(code_count)} 件・上位30 {top_codes[:5]}")

    md = format_markdown(by_gnl, top_codes, args.date, len(filtered))
    out = MARKET_DIR / f"{args.date}_scout_raw.md"
    out.write_text(md, encoding="utf-8")
    print(f"[OK] 保存完了: {out}")


if __name__ == "__main__":
    main()
