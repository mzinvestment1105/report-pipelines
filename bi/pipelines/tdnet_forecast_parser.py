"""
TDNet 決算短信 PDF から幅付き業績予想（レンジ形式）を取得するフォールバック。

JQuants /fins/summary が幅付き予想のため NULL を返す銘柄向け（例: 3994 マネーフォワード）。
TDNet Atom から最新の 決算短信 PDF を取得し、レンジ中央値を円建てで返す。

返値キー（STATEMENT_NUMERIC_COLS と同名）:
  NetSales_NextYear_Forecast
  OperatingProfit_NextYear_Forecast
  Profit_NextYear_Forecast

使い方:
  from tdnet_forecast_parser import fetch_tdnet_range_forecasts
  result = fetch_tdnet_range_forecasts("3994")
  # → {"NetSales_NextYear_Forecast": 55475000000, ...}
  # 幅付き予想が見つからない場合は {} を返す

環境変数:
  TDNET_FORECAST_FALLBACK=0  で無効化（デフォルト: 1 = 有効）
"""

from __future__ import annotations

import os
import re
import time
from io import BytesIO, StringIO
from xml.etree import ElementTree as ET

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------

_TDNET_ATOM_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list/{code}.atom"
_NS = {"a": "http://purl.org/atom/ns#"}
_REQUEST_TIMEOUT = 20

# 決算短信タイトルにマッチするパターン（業績予想修正も含む）
_KESSANTAN_TITLE_RE = re.compile(
    r"決算短信|業績予想の?修正",
    re.IGNORECASE,
)

# 幅付き予想の数値レンジパターン
# 例: 53,400〜57,550  /  △2,500〜500  /  △3,700〜△700
#
# ポイント: pdfminer が列境界を消して "14.349,742" のように結合する。
#   ・(?<![.\d]) : 直前が数字または小数点の場合はマッチしない
#     → "14.349,742" の "3" 以降にある "349,742" を除外
#   ・\d{1,3}(?:,\d{3})* : 3桁区切りを強制し "57,5506" のような誤延長を防ぐ
_NEG_PREFIX = r"(?:[△▲]\s*)?"
# "49,742～52,505" の `,742` の `7` から始まる誤マッチを防ぐためカンマも lookbehind に含める
_NUM_PART = rf"(?<![,.\d]){_NEG_PREFIX}\d{{1,3}}(?:,\d{{3}})*"
_RANGE_RE = re.compile(
    rf"({_NUM_PART})\s*[〜～]\s*({_NUM_PART})"
)

# 業績予想セクションの開始キーワード
_FORECAST_SECTION_RE = re.compile(r"(?:連結|単体)?業績予想")

# EPS（1株当たり）の除外閾値: 他レンジの最大絶対値の 1/200 未満なら EPS と判断
_EPS_RATIO_THRESHOLD = 1 / 200

# 単位（百万円 / 千円）の検出
_UNIT_MILLION_RE = re.compile(r"単位[：:（(]\s*百万円")
_UNIT_THOUSAND_RE = re.compile(r"単位[：:（(]\s*千円")


# ---------------------------------------------------------------------------
# ユーティリティ
# ---------------------------------------------------------------------------

def _is_enabled() -> bool:
    return os.environ.get("TDNET_FORECAST_FALLBACK", "1").strip() not in ("0", "false", "no", "off")


def _parse_jp_number(s: str) -> float:
    """△/▲ 付き日本語数値文字列を float へ変換。失敗時は NaN。"""
    s = s.strip().replace(",", "").replace("\u3000", "").replace(" ", "")
    negative = s.startswith(("△", "▲"))
    s = s.lstrip("△▲").strip()
    try:
        v = float(s)
        return -v if negative else v
    except ValueError:
        return float("nan")


def _midpoint(a_str: str, b_str: str) -> float | None:
    """2値の中央値を返す。どちらかが NaN なら None。"""
    a = _parse_jp_number(a_str)
    b = _parse_jp_number(b_str)
    if pd.isna(a) or pd.isna(b):
        return None
    return (a + b) / 2.0


# ---------------------------------------------------------------------------
# PDF テキスト抽出
# ---------------------------------------------------------------------------

def _extract_pdf_text(pdf_bytes: bytes, max_chars: int = 4000) -> str:
    """pdfminer で PDF バイト列から先頭テキストを抽出。失敗時は空文字。"""
    try:
        from pdfminer.high_level import extract_text_to_fp
        from pdfminer.layout import LAParams
    except ImportError:
        return ""
    try:
        out = StringIO()
        extract_text_to_fp(
            BytesIO(pdf_bytes),
            out,
            laparams=LAParams(line_margin=0.4),
            output_type="text",
            codec=None,
        )
        text = out.getvalue()
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return text[:max_chars]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# TDNet Atom → 最新 決算短信 PDF URL
# ---------------------------------------------------------------------------

def _fetch_atom_entries(code4: str) -> list[dict]:
    """TDNet Atom を取得し開示リストを返す。"""
    url = _TDNET_ATOM_URL.format(code=code4)
    resp = requests.get(url, timeout=_REQUEST_TIMEOUT)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    entries = []
    for entry in root.findall("a:entry", _NS):
        def _t(tag: str) -> str:
            el = entry.find(tag, _NS)
            return el.text.strip() if el is not None and el.text else ""
        link_el = entry.find("a:link[@rel='alternate']", _NS)
        link_href = link_el.get("href", "") if link_el is not None else ""
        pdf_url = ""
        if "rd.php?" in link_href:
            pdf_url = link_href.split("rd.php?", 1)[1]
        elif link_href.lower().endswith(".pdf"):
            pdf_url = link_href
        raw_title = _t("a:title")
        title = raw_title.split(":", 1)[1].strip() if ":" in raw_title else raw_title
        entries.append({
            "title": title,
            "published": _t("a:issued") or _t("a:created") or "",
            "pdf_url": pdf_url,
        })
    return entries


def _find_latest_kessantan_pdf_url(entries: list[dict]) -> str:
    """開示リストから最新の決算短信 PDF URL を返す。"""
    for entry in entries:  # Atom は新着順
        if _KESSANTAN_TITLE_RE.search(entry.get("title", "")) and entry.get("pdf_url"):
            return entry["pdf_url"]
    return ""


# ---------------------------------------------------------------------------
# 業績予想レンジ抽出
# ---------------------------------------------------------------------------

def _extract_ranges_from_text(text: str) -> dict[str, float]:
    """
    決算短信テキストから幅付き業績予想レンジを抽出し「円」単位の dict を返す。
    キー: sales / op / np

    ロジック:
    1. 業績予想セクション内の全レンジを位置情報付きで収集
    2. EPS（株単位・極小値）を閾値フィルタで除外
    3. 売上高 = 最大絶対値レンジ
    4. 営業利益 = 文書上で売上高より後に現れる最初のレンジ
    5. 純利益  = 文書上で最後のレンジ
       日本基準 [op, 経常, np] → 最後 = np ✓
       IFRS     [op, np]       → 最後 = np ✓
    """
    # 業績予想セクション
    section_match = _FORECAST_SECTION_RE.search(text)
    search_text = text[section_match.start():] if section_match else text

    # 単位確認（先頭 1000 字）
    unit_multiplier = 1_000_000  # 決算短信デフォルト: 百万円
    if _UNIT_THOUSAND_RE.search(text[:1000]):
        unit_multiplier = 1_000

    # 位置付き全レンジを収集
    positioned: list[tuple[int, float]] = []  # (pos, midpoint)
    for m in _RANGE_RE.finditer(search_text):
        mid = _midpoint(m.group(1), m.group(2))
        if mid is not None:
            positioned.append((m.start(), mid))

    if not positioned:
        return {}

    # EPS フィルタ: 最大絶対値の 1/200 未満 → 1株当たり指標とみなして除外
    max_abs = max(abs(p[1]) for p in positioned)
    threshold = max_abs * _EPS_RATIO_THRESHOLD if max_abs > 0 else 0
    significant = [(pos, mid) for pos, mid in positioned if abs(mid) >= threshold]

    if not significant:
        return {}

    # 売上高 = 最大絶対値（文書内位置は問わない）
    sales_pos, sales_mid = max(significant, key=lambda x: abs(x[1]))

    # 売上高より後に出てくるレンジ = 利益候補
    profit_candidates = [(pos, mid) for pos, mid in significant if pos > sales_pos]

    result: dict[str, float] = {"sales": sales_mid}
    if profit_candidates:
        result["op"] = profit_candidates[0][1]   # 最初 = 営業利益
        result["np"] = profit_candidates[-1][1]  # 最後 = 純利益

    # 単位変換（百万円 or 千円 → 円）
    return {k: v * unit_multiplier for k, v in result.items()}


# ---------------------------------------------------------------------------
# 公開 API
# ---------------------------------------------------------------------------

def fetch_tdnet_range_forecasts(
    code4: str,
    sleep_seconds: float = 1.0,
) -> dict[str, float]:
    """
    TDNet 決算短信 PDF から幅付き業績予想（レンジ）を取得し、
    STATEMENT_NUMERIC_COLS 準拠の dict を返す（値は円建て）。

    幅付き予想が見つからない場合・エラー時は {} を返す。
    TDNET_FORECAST_FALLBACK=0 の場合も {} を返す。

    返値キー:
      NetSales_NextYear_Forecast
      OperatingProfit_NextYear_Forecast
      Profit_NextYear_Forecast
    """
    if not _is_enabled():
        return {}

    try:
        entries = _fetch_atom_entries(code4)
        if sleep_seconds > 0:
            time.sleep(sleep_seconds)
        pdf_url = _find_latest_kessantan_pdf_url(entries)
        if not pdf_url:
            return {}

        resp = requests.get(pdf_url, timeout=_REQUEST_TIMEOUT)
        resp.raise_for_status()
        if sleep_seconds > 0:
            time.sleep(sleep_seconds * 0.5)

        text = _extract_pdf_text(resp.content)
        if not text:
            return {}

        raw = _extract_ranges_from_text(text)
        if not raw:
            return {}

        mapping = {
            "sales": "NetSales_NextYear_Forecast",
            "op": "OperatingProfit_NextYear_Forecast",
            "np": "Profit_NextYear_Forecast",
        }
        return {mapping[k]: v for k, v in raw.items() if k in mapping}

    except Exception:
        return {}


# ---------------------------------------------------------------------------
# CLI 検証用
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="TDNet 幅付き予想パーサー 検証")
    parser.add_argument("--code", required=True, help="銘柄コード（例: 3994）")
    parser.add_argument("--debug", action="store_true", help="PDF テキストを表示")
    args = parser.parse_args()

    code = args.code.strip()
    print(f"[TDNet 幅付き予想パーサー] code={code}")

    try:
        entries = _fetch_atom_entries(code)
        pdf_url = _find_latest_kessantan_pdf_url(entries)
        if not pdf_url:
            print("  決算短信 PDF が見つかりませんでした")
        else:
            print(f"  PDF URL: {pdf_url}")
            resp = requests.get(pdf_url, timeout=_REQUEST_TIMEOUT)
            text = _extract_pdf_text(resp.content)
            if args.debug:
                print("\n--- PDF テキスト（先頭 4000 字）---")
                print(text[:4000])
                print("---")
            raw = _extract_ranges_from_text(text)
            print(f"\n  抽出レンジ（百万円換算前）: {raw}")
            result = fetch_tdnet_range_forecasts(code, sleep_seconds=0)
            print(f"\n  最終結果（円建て）:")
            for k, v in result.items():
                print(f"    {k}: {v:,.0f} 円 ({v/1e8:.1f}億円)")
            if not result:
                print("    幅付き予想なし / 取得失敗")
    except Exception as e:
        print(f"  エラー: {e}")
