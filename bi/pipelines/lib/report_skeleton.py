"""個別銘柄レポートの誌面骨格の単一定義（PM 2026-09-07 承認）。

正本は [agents/stock_analyst.md](../../../agents/stock_analyst.md) の
「## 誌面骨格（全セクション共通・絶対遵守・PM 2026-09-07 承認）」節である。
本モジュールはその節を**パースして**セクション見出し・小見出しホワイトリスト・
固定表のヘッダを取り出す。ゲート側へ値をハードコードしないことで、
正本を直せば検査が追随する（二重管理の防止）。

正本の骨格節が読めない場合は空の Skeleton を返し、呼び出し側が検査を
スキップできるようにする（フェイルオープン。ゲートを壊してレポート配信を
止めるより、検査を落とす方が §36 絶対配信原則に沿う）。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
STOCK_ANALYST_MD = REPO_ROOT / "agents" / "stock_analyst.md"

SKELETON_HEADING = "## 誌面骨格"

# 骨格節の中の各表を見分けるためのリード文（正本の太字ラベル）
_LEAD_H2 = "**セクション見出し（"
_LEAD_H3 = "**小見出しの許可一覧（"
_LEAD_ELEM = "**要素種類の固定**"
_LEAD_TABLE = "**固定表の列名・列順（"
_LEAD_ADDRESS = "**住所の確定（"
_LEAD_DECOR = "**装飾**"

_BACKTICK = re.compile(r"`([^`]+)`")


@dataclass
class Skeleton:
    """骨格の機械可読表現。空（loaded=False）なら検査をスキップする。"""

    loaded: bool = False
    # `## ...` の文言（正本の順序どおり）
    h2_order: list[str] = field(default_factory=list)
    # 条件付きセクションの `## ...` 文言
    h2_conditional: set[str] = field(default_factory=set)
    # 許可される `### ...` の文言（全セクション横断の集合）
    h3_whitelist: set[str] = field(default_factory=set)
    # `## 文言` -> その配下で許可される `### 文言` のリスト（順序つき）
    h3_by_section: dict[str, list[str]] = field(default_factory=dict)
    # 小見出しを禁止したセクションの `## 文言`
    h3_forbidden_sections: set[str] = field(default_factory=set)
    # 固定表のヘッダ署名（`列 / 列 / 列` を正規化したもの）の集合
    table_headers: set[str] = field(default_factory=set)

    @property
    def h2_required(self) -> list[str]:
        return [h for h in self.h2_order if h not in self.h2_conditional]


def _skeleton_block(text: str) -> str | None:
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(SKELETON_HEADING):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    return "\n".join(lines[start:end])


def _rows(block: str, lead: str, next_leads: tuple[str, ...]) -> list[list[str]]:
    """lead 行の直後から次のリードまでの表の本文行をセルのリストで返す。"""
    lines = block.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.startswith(lead):
            start = i
            break
    if start is None:
        return []
    out: list[list[str]] = []
    for line in lines[start + 1:]:
        s = line.strip()
        if any(s.startswith(nl) for nl in next_leads):
            break
        if not s.startswith("|"):
            continue
        if re.match(r"^\|[\s:\-|]+\|$", s):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        out.append(cells)
    return out


def _first_backtick(cell: str) -> str | None:
    m = _BACKTICK.search(cell)
    return m.group(1).strip() if m else None


def _all_backticks(cell: str) -> list[str]:
    return [m.strip() for m in _BACKTICK.findall(cell)]


def normalize_header(cells: list[str]) -> str:
    """表のヘッダ行を比較可能な署名へ正規化する。"""
    return " / ".join(re.sub(r"\s+", "", c) for c in cells)


def _normalize_spec_header(spec: str) -> str:
    """骨格の `列 / 列 / 列` 記法を署名へ正規化する。"""
    parts = [p.strip() for p in spec.split("/")]
    return " / ".join(re.sub(r"\s+", "", p) for p in parts)


def load(path: Path | None = None) -> Skeleton:
    md_path = path or STOCK_ANALYST_MD
    if not md_path.exists():
        return Skeleton()
    block = _skeleton_block(md_path.read_text(encoding="utf-8"))
    if not block:
        return Skeleton()

    sk = Skeleton(loaded=True)

    # --- セクション見出し（`## ...`・順序・条件付き）---------------------
    for cells in _rows(block, _LEAD_H2, (_LEAD_H3, _LEAD_ELEM, _LEAD_TABLE)):
        if len(cells) < 3:
            continue
        head = _first_backtick(cells[1])
        if not head or not head.startswith("## "):
            continue
        title = head[3:].strip()
        if title in sk.h2_order:
            continue
        sk.h2_order.append(title)
        if "条件付き" in cells[2]:
            sk.h2_conditional.add(title)

    # --- 小見出しの許可一覧（`### ...`）----------------------------------
    for cells in _rows(block, _LEAD_H3, (_LEAD_ELEM, _LEAD_TABLE, _LEAD_ADDRESS)):
        if len(cells) < 3:
            continue
        section = re.sub(r"\s+", "", cells[0])
        if section in ("セクション", "§", ""):
            continue  # 表のヘッダ行
        heads = [h[4:].strip() for h in _all_backticks(cells[1]) if h.startswith("### ")]
        if "小見出し禁止" in cells[2] or not heads:
            sk.h3_forbidden_sections.add(section)
            sk.h3_by_section[section] = []
            continue
        sk.h3_by_section[section] = heads
        sk.h3_whitelist.update(heads)

    # --- 固定表の列名・列順 ----------------------------------------------
    for cells in _rows(block, _LEAD_TABLE, (_LEAD_ADDRESS, _LEAD_DECOR)):
        if len(cells) < 4:
            continue
        spec = _first_backtick(cells[3])
        if not spec or "/" not in spec:
            continue
        sk.table_headers.add(_normalize_spec_header(spec))

    return sk


def section_key(h2_title: str) -> str:
    """`7. 大株主・資本異動` -> `7`。番号なしの節は文言そのものを返す。"""
    m = re.match(r"^([0-9]+(?:-[A-Z])?)\.", h2_title.strip())
    return m.group(1) if m else re.sub(r"\s+", "", h2_title.strip())


if __name__ == "__main__":  # 手動確認用
    sk = load()
    print("loaded:", sk.loaded)
    print("h2:", len(sk.h2_order), sk.h2_order)
    print("conditional:", sk.h2_conditional)
    print("h3 whitelist:", len(sk.h3_whitelist))
    for k, v in sk.h3_by_section.items():
        print("  ", k, "->", v)
    print("tables:", len(sk.table_headers))
    for h in sorted(sk.table_headers):
        print("  ", h)
