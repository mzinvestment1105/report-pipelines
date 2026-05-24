"""立花証券 e支店 API からニュースを取得して Markdown に保存する。

使い方:
    python fetch_tachibana_news.py [--date YYYY-MM-DD] [--limit 500]

出力:
    market/daily/{date}_tachibana_news_raw.md

仕様:
- 認証 I/F (CLMAuthLoginRequest) でログイン → 仮想 URL を秘密鍵で復号
- マスタ機能の CLMMfdsGetNewsHead で最大 500 件取得（100件 × 5ページ）
- ヘッドラインは Shift-JIS の URL エンコード文字列を Base64 化したもの → 復号
- カテゴリ別（マクロ系 / 個別銘柄 AI 速報 / AI 市況）に整形して保存

環境変数（bi/pipelines/.env）:
- TACHIBANA_DEMO_AUTH_ID
- TACHIBANA_DEMO_PRIVATE_KEY_PATH
- TACHIBANA_DEMO_API_BASE
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import urllib.parse
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent.parent
MARKET_DIR = REPO_ROOT / "market" / "daily"
ENV_PATH = BASE_DIR / ".env"

# カテゴリ・ジャンルの人間可読ラベル（2026-05-23 実証データから）
CGL_LABEL = {
    "100": "QUICK NQN（市況速報）",
    "110": "AI 市況（ボード）",
    "120": "QUICK ニュース",
    "129": "TDNet/EDINET AI 速報",
}

GNL_LABEL = {
    "3001": "QUICK 個別銘柄解説",
    "3007": "為替",
    "3009": "東証セッション速報",
    "3052": "米国株市況",
    "3105": "EDINET AI 大量保有報告",
    "6508": "日本株 ADR",
    "6512": "日経先物",
    "6521": "QUICK レーティング更新",
    "6526": "業績修正",
    "6536": "QUICK 銘柄ラウンドアップ",
    "60010": "AI 市況・寄り前注文予想",
    "60030": "AI 市況・材料発生",
    "60090": "AI 市況・ストップ高",
    "60100": "AI 市況・新高値",
    "60101": "AI 市況・新安値",
    "60110": "AI 市況・値上がり率",
    "60120": "AI 市況・値下がり率",
    "60130": "AI 市況・売買代金上位",
    "60140": "AI 市況・寄付後上昇率",
    "60141": "AI 市況・寄付後下落率",
    "61299": "EDINET AI 有価証券届出書",
    "61499": "EDINET AI 臨時報告書",
    "62101": "TDNet AI 自社株買い",
    "62199": "TDNet AI 適時開示",
}


def now_str() -> str:
    n = datetime.now()
    return n.strftime("%Y.%m.%d-%H:%M:%S.") + f"{n.microsecond // 1000:03d}"


def login(api_base: str, auth_id: str, private_key) -> dict[str, str]:
    """ログインして仮想URLを取得・復号して返す。"""
    payload = {
        "p_no": "1",
        "p_sd_date": now_str(),
        "sCLMID": "CLMAuthLoginRequest",
        "sJsonOfmt": "4",
        "sAuthId": auth_id,
    }
    url = f"{api_base}/auth/?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
    r = requests.get(url, timeout=20)
    r.encoding = "shift_jis"
    resp = json.loads(r.text)
    if resp.get("p_errno") != "0" or resp.get("sResultCode") != "0":
        raise RuntimeError(f"login failed: {resp.get('p_err')} / {resp.get('sResultText')}")
    oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
    return {
        "request": private_key.decrypt(base64.b64decode(resp["sUrlRequest"]), oaep).decode("utf-8"),
        "master": private_key.decrypt(base64.b64decode(resp["sUrlMaster"]), oaep).decode("utf-8"),
        "price": private_key.decrypt(base64.b64decode(resp["sUrlPrice"]), oaep).decode("utf-8"),
    }


def fetch_news_page(master_url: str, offset: int, limit: int, p_no: int) -> dict:
    payload = {
        "p_no": str(p_no),
        "p_sd_date": now_str(),
        "sCLMID": "CLMMfdsGetNewsHead",
        "sJsonOfmt": "4",
        "p_REC_OFST": str(offset),
        "p_REC_LIMT": str(limit),
    }
    url = f"{master_url}?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
    r = requests.get(url, timeout=30)
    r.encoding = "shift_jis"
    return json.loads(r.text)


def decode_headline(b64: str) -> str:
    try:
        url_str = base64.b64decode(b64).decode("ascii")
        return urllib.parse.unquote(url_str, encoding="shift_jis")
    except Exception as e:
        return f"[decode error: {e}]"


def fetch_all_news(master_url: str, total_limit: int) -> list[dict]:
    """直近 total_limit 件のニュースを取得。100件/ページで複数回叩く。"""
    all_news: list[dict] = []
    page_size = 100
    for ofst in range(0, total_limit, page_size):
        d = fetch_news_page(master_url, ofst, page_size, p_no=ofst + 10)
        items = d.get("aCLMMfdsNewsHead", [])
        if not items:
            break
        all_news.extend(items)
    return all_news


def format_markdown(news: list[dict], target_date: str) -> str:
    """カテゴリ別にニュースを整形した Markdown を返す。"""
    lines: list[str] = []
    lines.append(f"# 立花証券 e支店 API ニュース raw ({target_date})")
    lines.append("")
    lines.append(f"- **取得日時**: {datetime.now().strftime('%Y-%m-%d %H:%M JST')}")
    lines.append(f"- **取得件数**: {len(news)} 件")
    lines.append("- **データソース**: 立花証券 e支店 API デモ環境 (QUICK NQN / TDNet AI / AI 市況)")
    lines.append("")
    lines.append("カテゴリ別に整理。レポート生成時に該当セクションを Claude が読み込む。")
    lines.append("")
    lines.append("---")
    lines.append("")

    # CGL × GNL でグルーピング
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for n in news:
        key = (n.get("p_CGL", ""), n.get("p_GNL", ""))
        grouped[key].append(n)

    # CGL ごとに整理（マクロ系 100 → AI市況 110 → TDNet 129 の順）
    cgl_order = ["100", "110", "129", "120"]
    seen_cgl: set[str] = set()

    for cgl in cgl_order + sorted(set(k[0] for k in grouped.keys()) - set(cgl_order)):
        cgl_keys = [k for k in grouped.keys() if k[0] == cgl]
        if not cgl_keys or cgl in seen_cgl:
            continue
        seen_cgl.add(cgl)
        cgl_label = CGL_LABEL.get(cgl, f"CGL={cgl}")
        cgl_total = sum(len(grouped[k]) for k in cgl_keys)
        lines.append(f"## {cgl_label}  ({cgl_total} 件)")
        lines.append("")

        # GNL を件数降順
        for key in sorted(cgl_keys, key=lambda k: -len(grouped[k])):
            _, gnl = key
            items = grouped[key]
            gnl_label = GNL_LABEL.get(gnl, f"GNL={gnl}")
            lines.append(f"### {gnl_label}  ({len(items)} 件)")
            lines.append("")
            for n in items:
                title = decode_headline(n["p_HDL"])
                dt = f"{n['p_DT']} {n['p_TM']}"
                isl = n.get("p_ISL", "")
                isl_str = f" [銘柄: {isl}]" if isl else ""
                lines.append(f"- **{dt}**{isl_str} {title}")
            lines.append("")

    return "\n".join(lines)


def main() -> None:
    load_dotenv(ENV_PATH)
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    parser.add_argument("--limit", type=int, default=500, help="取得最大件数（100単位）")
    args = parser.parse_args()

    auth_id = os.getenv("TACHIBANA_DEMO_AUTH_ID")
    key_path = os.getenv("TACHIBANA_DEMO_PRIVATE_KEY_PATH")
    api_base = os.getenv("TACHIBANA_DEMO_API_BASE")
    if not all([auth_id, key_path, api_base]):
        print("[ERROR] .env に TACHIBANA_DEMO_AUTH_ID / TACHIBANA_DEMO_PRIVATE_KEY_PATH / TACHIBANA_DEMO_API_BASE が必要", file=sys.stderr)
        sys.exit(1)

    key_abs = (REPO_ROOT / key_path) if not Path(key_path).is_absolute() else Path(key_path)
    with open(key_abs, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    print(f"[INFO] login to {api_base} ...")
    urls = login(api_base, auth_id, private_key)
    print(f"[OK] 仮想URL 取得済")

    print(f"[INFO] 最大 {args.limit} 件のニュース取得中 ...")
    news = fetch_all_news(urls["master"], args.limit)
    print(f"[OK] {len(news)} 件取得")

    md = format_markdown(news, args.date)
    out = MARKET_DIR / f"{args.date}_tachibana_news_raw.md"
    out.write_text(md, encoding="utf-8")
    print(f"[OK] 保存完了: {out}")


if __name__ == "__main__":
    main()
