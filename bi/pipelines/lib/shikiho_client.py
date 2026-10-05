"""
四季報オンライン（東洋経済）の無料 API から「掲載号・四季報予想純利益・会社予想との乖離」を取る
=================================================================================

動意レポート「なぜ動いた」改修（2026-10-04 PM 承認・計画書 B-1〜B-3）。

取得元（いずれもログイン不要・HTTP 200 を 2026-10-04 に確認済み）:
  - https://api-shikiho.toyokeizai.net/stocks/v1/stocks/{code}/latest
      shimen_pub_date（掲載号の発売日）・year / season_name / shikiho_name（号名）・
      fyp1_per（四季報予想ベースの今期 PER）・number_of_outstanding_stocks_adj（調整後株数）・
      shimen_results の会社予想行（「会27.3予」）・max_modified_date_tk（四季報予想の最終更新日）
  - https://api-shikiho.toyokeizai.net/stocks/v1/stocks/{code}/headers
      current_price / price_updated_time（PER の基準株価）・planned_disclosure_date_text（次回決算予定日）
  - https://str.toyokeizai.net/magazine/shikiho/
      「YYYY年N集◯号」（YYYY年M月D日発売）」の本文から最新号の発売日を照合する

四季報予想純利益（百万円）＝ 株価 ÷ fyp1_per × 調整後株数 ÷ 1e6。
乖離率（%）＝（四季報予想 ÷ 会社予想 − 1）× 100。会社予想が 0 以下・取得不能なら乖離率は出さない。

取れない項目（有料・マスク）: 四季報の売上・営業利益・経常利益予想／見出し記号／前号比。
非公開 API のため、応答形式が変わって取れない場合は error に理由を入れて返す（呼び出し側が
「四季報: 取得失敗（理由）」行を出す）。推定値で埋めない。
"""

from __future__ import annotations

import re
import time
import unicodedata
from collections import Counter
from datetime import date

import requests

API_BASE = "https://api-shikiho.toyokeizai.net/stocks/v1/stocks/{code}/{ep}"
STORE_URL = "https://str.toyokeizai.net/magazine/shikiho/"
REQUEST_SLEEP = 0.5   # 1 リクエストごとの待機（秒）
TIMEOUT = 20

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Referer": "https://shikiho.toyokeizai.net/",
    "Origin": "https://shikiho.toyokeizai.net",
}

_STORE_RE = re.compile(
    r"(\d{4})年\s*(\d+)集\s*(\S{1,3}号)」?\s*[（(]\s*(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日発売"
)


def _get_json(url: str) -> tuple[dict | None, str]:
    """GET して JSON を返す。失敗時は (None, 理由)。1 リクエストごとに REQUEST_SLEEP 待つ。"""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=TIMEOUT)
    except Exception as e:  # 通信例外
        time.sleep(REQUEST_SLEEP)
        return None, f"通信例外 {type(e).__name__}"
    time.sleep(REQUEST_SLEEP)
    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"
    try:
        d = r.json()
    except Exception:
        return None, "JSON 解析失敗"
    if not isinstance(d, dict):
        return None, "応答形式の変化（dict でない）"
    st = d.get("status") or {}
    if str(st.get("code", "1000")) != "1000" or str(d.get("is_exist", "1")) != "1":
        return None, f"銘柄データなし（status={st.get('code')} is_exist={d.get('is_exist')}）"
    return d, ""


def _num(s) -> float | None:
    """'4,000' / '-313' / 'ー' 等を数値へ。数値でなければ None。"""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = unicodedata.normalize("NFKC", str(s)).replace(",", "").strip()
    t = t.replace("△", "-").replace("▲", "-")
    try:
        return float(t)
    except ValueError:
        return None


def _ymd(s) -> str:
    """'20260916' → '2026-09-16'。形式外は空文字。"""
    t = str(s or "").strip()
    if re.fullmatch(r"\d{8}", t):
        return f"{t[:4]}-{t[4:6]}-{t[6:]}"
    return ""


def _company_forecast(results: list, fy_end: str) -> tuple[float | None, str, str]:
    """shimen_results から会社予想行（「会YY.M予」）の純利益と開示日を返す。

    fy_end は fyp1_fiscal_year_end（'2027-03'）。一致する年度の行を優先し、無ければ最初の会社予想行。
    Returns: (純利益 百万円 or None, 行ラベル, 開示日 'YYYY-MM-DD' or '')
    """
    if not isinstance(results, list) or not results:
        return None, "", ""
    head = results[0] if isinstance(results[0], list) else []
    try:
        ni_idx = head.index("純利益")
    except ValueError:
        ni_idx = 4
    want = ""
    m = re.fullmatch(r"(\d{4})-(\d{2})", str(fy_end or ""))
    if m:
        want = f"{m.group(1)[2:]}.{int(m.group(2))}予"
    rows = [r for r in results[1:] if isinstance(r, list) and r and str(r[0]).startswith("会")]
    if not rows:
        return None, "", ""
    pick = next((r for r in rows if want and str(r[0]).endswith(want)), rows[0])
    val = _num(pick[ni_idx]) if len(pick) > ni_idx else None
    disc = ""
    md = re.search(r"\((\d{2})\.(\d{1,2})\.(\d{1,2})\)", str(pick[-1]))
    if md:
        disc = f"20{md.group(1)}-{int(md.group(2)):02d}-{int(md.group(3)):02d}"
    return val, str(pick[0]), disc


def fetch_shikiho(code4: str) -> dict:
    """1 銘柄分の四季報情報を返す。

    Returns（成功時）:
      {"ok": True, "issue": "2026年4集 秋号", "release_date": "2026-09-16",
       "forecast_updated": "2026-09-03", "per": 2.4658, "price": 468.0,
       "price_time": "2026/10/02 15:30:00", "shares_adj": 39511728.0,
       "shikiho_net_income_mn": 7499.4, "company_net_income_mn": 4000.0,
       "company_forecast_label": "会27.3予", "company_forecast_disclosed": "2026-05-15",
       "fiscal_year_end": "2027-03", "gap_pct": 87.49, "next_disclosure": "2026/11/16", "error": ""}
    失敗時: {"ok": False, "error": "理由"}（他のキーは取れた分だけ入る）
    """
    out: dict = {"ok": False, "error": ""}
    latest, err = _get_json(API_BASE.format(code=code4, ep="latest"))
    if latest is None:
        out["error"] = f"latest {err}"
        return out
    headers, herr = _get_json(API_BASE.format(code=code4, ep="headers"))

    year = str(latest.get("year") or "").strip()
    season = unicodedata.normalize("NFKC", str(latest.get("season_name") or "")).strip()
    name = str(latest.get("shikiho_name") or "").strip()
    out["issue"] = f"{year}年{season} {name}".strip() if year else ""
    out["release_date"] = _ymd(latest.get("shimen_pub_date"))
    out["forecast_updated"] = _ymd(latest.get("max_modified_date_tk"))
    out["fiscal_year_end"] = str(latest.get("fyp1_fiscal_year_end") or "")
    per = _num(latest.get("fyp1_per"))
    shares = _num(latest.get("number_of_outstanding_stocks_adj"))
    out["per"] = per
    out["shares_adj"] = shares

    price = None
    if headers is not None:
        price = _num(headers.get("current_price"))
        out["price_time"] = str(headers.get("price_updated_time") or "")
        out["next_disclosure"] = str(headers.get("planned_disclosure_date_text") or "")
    out["price"] = price

    shk = None
    if per and per > 0 and shares and price:
        shk = price / per * shares / 1e6
    out["shikiho_net_income_mn"] = shk
    comp, label, disc = _company_forecast(latest.get("shimen_results"), out["fiscal_year_end"])
    out["company_net_income_mn"] = comp
    out["company_forecast_label"] = label
    out["company_forecast_disclosed"] = disc
    out["gap_pct"] = ((shk / comp - 1.0) * 100.0) if (shk is not None and comp and comp > 0) else None

    reasons = []
    if headers is None:
        reasons.append(f"headers {herr}（逆算の基準株価なし）")
    if per is None:
        reasons.append("四季報予想 PER なし（赤字予想または未掲載）")
    elif per <= 0:
        reasons.append("四季報予想 PER が 0 以下")
    if shares is None:
        reasons.append("調整後株数なし")
    if comp is None:
        reasons.append("会社予想の純利益なし")
    out["notes"] = reasons
    out["ok"] = bool(out["release_date"])
    if not out["ok"]:
        out["error"] = "掲載号の発売日（shimen_pub_date）が空"
    return out


def fetch_store_release() -> tuple[str, str, str]:
    """東洋経済 STORE の四季報ページから最新号の (号名, 発売日 'YYYY-MM-DD', エラー理由) を返す。"""
    try:
        r = requests.get(STORE_URL, headers={"User-Agent": _HEADERS["User-Agent"]}, timeout=TIMEOUT)
    except Exception as e:
        time.sleep(REQUEST_SLEEP)
        return "", "", f"通信例外 {type(e).__name__}"
    time.sleep(REQUEST_SLEEP)
    if r.status_code != 200:
        return "", "", f"HTTP {r.status_code}"
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = r.apparent_encoding or "utf-8"
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", r.text))
    found = []
    for m in _STORE_RE.finditer(text):
        try:
            d = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        except ValueError:
            continue
        found.append((d, f"{m.group(1)}年{m.group(2)}集 {m.group(3)}"))
    if not found:
        return "", "", "発売日の記載を本文から抽出できず（ページ構成の変化）"
    d, issue = max(found)
    return issue, d.isoformat(), ""


def fetch_shikiho_batch(codes: list[str], log=print) -> dict:
    """複数銘柄の四季報情報と、最新号の発売日（最頻値・STORE 照合）を返す。

    Returns:
      {"stocks": {code: fetch_shikiho(...)},
       "release_mode": "YYYY-MM-DD" or "", "release_mode_issue": "...",
       "store_issue": "...", "store_release": "YYYY-MM-DD" or "",
       "store_check": "一致" / "不一致（…）" / "照合不能（…）",
       "errors": ["code: 理由", ...]}
    """
    stocks: dict = {}
    errors: list[str] = []
    for i, c in enumerate(codes, 1):
        res = fetch_shikiho(c)
        stocks[c] = res
        if not res.get("ok"):
            errors.append(f"{c}: {res.get('error')}")
    rel = Counter(v.get("release_date") for v in stocks.values() if v.get("release_date"))
    iss = Counter(v.get("issue") for v in stocks.values() if v.get("issue"))
    mode = rel.most_common(1)[0][0] if rel else ""
    mode_issue = iss.most_common(1)[0][0] if iss else ""
    s_issue, s_rel, s_err = fetch_store_release()
    if s_err:
        check = f"照合不能（東洋経済 STORE: {s_err}）"
    elif not mode:
        check = "照合不能（API の発売日が全銘柄で取得できず）"
    elif s_rel == mode:
        check = "一致"
    else:
        check = f"不一致（API 最頻値 {mode}・STORE {s_rel}）"
    if check != "一致":
        errors.append(f"発売日照合: {check}")
    log(f"  四季報: {len(codes)}銘柄・取得 {sum(1 for v in stocks.values() if v.get('ok'))}・"
        f"最新号 {mode_issue} 発売 {mode}・STORE 照合 {check}")
    return {"stocks": stocks, "release_mode": mode, "release_mode_issue": mode_issue,
            "store_issue": s_issue, "store_release": s_rel, "store_check": check,
            "errors": errors}
