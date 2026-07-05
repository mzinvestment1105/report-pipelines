"""
カバレッジ銘柄リスト生成ETL。

カバレッジ銘柄の定義（feedback_coverage_definition.md）：
- セクターマップ22ファイル（research/sectors/01-22_*.md）の「キープレイヤー」テーブル銘柄
- portfolio/watchlist.md の銘柄
- portfolio/positions.md の保有銘柄

出力:
  research/earnings/coverage_stocks.csv (code, name, sector, source)
  research/earnings/coverage_stocks.parquet
"""

from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent.parent
SECTORS_DIR = ROOT / "research/sectors"
WATCHLIST_MD = ROOT / "portfolio/watchlist.md"
POSITIONS_MD = ROOT / "portfolio/positions.md"
OUT_CSV = ROOT / "research/earnings/coverage_stocks.csv"
OUT_PARQUET = ROOT / "research/earnings/coverage_stocks.parquet"

SECTOR_MAP_PATTERN = re.compile(r"^\d{2}_[a-z_]+\.md$")
KEYPLAYER_HEADER = re.compile(r"^##\s*キープレイヤー", re.MULTILINE)
TABLE_ROW = re.compile(
    r"^\|\s*(\d+)\s*\|\s*([0-9]{3,4}[A-Z]?)\s*\|\s*([^\|]+?)\s*\|",
    re.MULTILINE,
)
WATCHLIST_HEADING = re.compile(r"^##\s*([0-9]{3,4}[A-Z]?)\s+(.+)$", re.MULTILINE)
POSITION_ROW = re.compile(r"^\|\s*([0-9]{3,4}[A-Z]?)\s*\|\s*([^\|]+?)\s*\|", re.MULTILINE)


def extract_from_sector(md_path: Path) -> list[dict]:
    text = md_path.read_text(encoding="utf-8")
    m = KEYPLAYER_HEADER.search(text)
    if not m:
        return []
    after = text[m.end():]
    end_match = re.search(r"^---\s*$", after, re.MULTILINE)
    table_block = after[: end_match.start()] if end_match else after

    sector_key = md_path.stem
    records: list[dict] = []
    for row_match in TABLE_ROW.finditer(table_block):
        rank, code, name = row_match.groups()
        records.append({
            "code": code.strip(),
            "name": name.strip(),
            "sector": sector_key,
            "source": "sector_map",
        })
    return records


def extract_from_watchlist(md_path: Path) -> list[dict]:
    text = md_path.read_text(encoding="utf-8")
    records: list[dict] = []
    for m in WATCHLIST_HEADING.finditer(text):
        code, name = m.groups()
        records.append({
            "code": code.strip(),
            "name": name.strip(),
            "sector": "watchlist",
            "source": "watchlist",
        })
    return records


def extract_from_positions(md_path: Path) -> list[dict]:
    text = md_path.read_text(encoding="utf-8")
    in_section = False
    records: list[dict] = []
    for line in text.splitlines():
        if "## 保有ポジション" in line:
            in_section = True
            continue
        if in_section and line.startswith("## "):
            break
        if not in_section:
            continue
        m = POSITION_ROW.match(line)
        if not m:
            continue
        code, name = m.groups()
        if code in ("ティッカー", "-----"):
            continue
        records.append({
            "code": code.strip(),
            "name": name.strip(),
            "sector": "holding",
            "source": "positions",
        })
    return records


def main() -> None:
    sector_files = sorted(
        f for f in SECTORS_DIR.iterdir()
        if f.is_file() and SECTOR_MAP_PATTERN.match(f.name)
    )
    print(f"セクターマップ: {len(sector_files)} ファイル")

    all_records: list[dict] = []
    for md in sector_files:
        recs = extract_from_sector(md)
        print(f"  {md.name}: {len(recs)} 銘柄")
        all_records.extend(recs)

    wl_records = extract_from_watchlist(WATCHLIST_MD)
    print(f"watchlist: {len(wl_records)} 銘柄")
    all_records.extend(wl_records)

    pos_records = extract_from_positions(POSITIONS_MD)
    print(f"positions: {len(pos_records)} 銘柄")
    all_records.extend(pos_records)

    df = pd.DataFrame(all_records)
    df["code"] = df["code"].astype(str)

    agg = (
        df.groupby("code")
        .agg(
            name=("name", "first"),
            sectors=("sector", lambda x: ",".join(sorted(set(x)))),
            sources=("source", lambda x: ",".join(sorted(set(x)))),
        )
        .reset_index()
    )

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    agg.to_csv(OUT_CSV, encoding="utf-8", index=False)
    agg.to_parquet(OUT_PARQUET, index=False)
    print()
    print(f"ユニーク銘柄数: {len(agg)}")
    print(f"保存: {OUT_CSV}")
    print(f"保存: {OUT_PARQUET}")


if __name__ == "__main__":
    main()
