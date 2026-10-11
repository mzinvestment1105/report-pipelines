#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""経済イベント日程台帳 (market/calendar/economic_events.yml) の更新スクリプト。

背景
----
マクロレポートの経済カレンダーは Finnhub から取得する実装だったが、無料プランでは
経済カレンダー API が使えず実取得 0 件だった。GHA では WebSearch / WebFetch が 404 で
機能せず、記憶ベースの日付記載は禁止のため、レポート生成側は日程を知る手段が無かった。

本スクリプトは公式サイト（FRB / 日銀 / 内閣府 / 米労働統計局 BLS / 米国勢調査局 Census）から
確定日程をスクレイプし、台帳 YAML を更新する。**ローカル実行前提**（GHA からは実行しない）。requests + BeautifulSoup のみを
使い、Claude の WebFetch ツールには依存しない。

使い方
------
    python bi/pipelines/fetch_economic_calendar.py --dry-run
    python bi/pipelines/fetch_economic_calendar.py --source boj
    python bi/pipelines/fetch_economic_calendar.py --source bls
    python bi/pipelines/fetch_economic_calendar.py --source all

更新頻度の目安
--------------
- FOMC / 日銀 金融政策決定会合: 年次固定日程のため年 1 回（前年末〜年初）
- 内閣府 GDP (QE): 四半期ごと
- 米労働統計局 BLS（米CPI・米雇用統計・米PPI）/ 米国勢調査局 Census（米小売売上高）:
  月 1 回程度（実行日から US_HORIZON_DAYS 日先までの発表日を取得する）

方針
----
- 既存台帳を読み、**新規イベントのみ追記する**。既存エントリ（過去日を含む）を消さない。
  既に同じ id が存在する場合、日付・時刻が変わっていれば更新し、そうでなければ何もしない。
- 米国の発表時刻は米東部時間で公表されるため、zoneinfo（America/New_York → Asia/Tokyo）で
  日本時間へ機械的に換算して台帳に入れる（夏時間の切り替えも zoneinfo に任せ、手で補正しない）。
- 取得失敗時は明確にエラーを出す。**推測での補完を一切しない**（取れなかった項目は書かない）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError as exc:  # pragma: no cover
    print(f"[FATAL] 依存パッケージが不足しています: {exc}", file=sys.stderr)
    print("        pip install requests beautifulsoup4 ruamel.yaml pyyaml", file=sys.stderr)
    raise SystemExit(2)

import yaml
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- 定数

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = REPO_ROOT / "market" / "calendar" / "economic_events.yml"

URL_FOMC = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
URL_BOJ_MPM = "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
URL_CAO_QE = "https://www.esri.cao.go.jp/jp/sna/kouhyou/kouhyou_top.html"
# 米労働統計局 BLS の月別リリースカレンダー（日付セル id="dMMDD" に指標名・対象月・発表時刻が並ぶ）
URL_BLS_MONTH = "https://www.bls.gov/schedule/{year}/{month:02d}_sched.htm"
# 米国勢調査局 Census の年別 経済指標公表スケジュール（一覧表）。年別 URL は公開済みの年だけ有効で、
# 翌年分は年なしの URL に載る（2026-10-11 実測: 年なし URL の見出しが 2027 年版）。見出しの年で照合する。
URL_CENSUS_LIST = "https://www.census.gov/economic-indicators/calendar-listview-{year}.html"
URL_CENSUS_LIST_CURRENT = "https://www.census.gov/economic-indicators/calendar-listview.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}
# BLS は一般ブラウザ相当の User-Agent を 403 で拒否し、取得目的を名乗る User-Agent は受け付ける
# （2026-10-11 実測）。そのため BLS だけ専用ヘッダーを使う。
BLS_HEADERS = {
    "User-Agent": "economic-calendar-fetcher/1.0 (personal research script)",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 30

# 米国指標は実行日から何日先までの発表日を台帳へ入れるか（約半年）
US_HORIZON_DAYS = 183
TZ_ET = ZoneInfo("America/New_York")
TZ_JST = ZoneInfo("Asia/Tokyo")

MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


class FetchError(RuntimeError):
    """一次情報の取得・解析に失敗したことを表す。推測補完はしない。"""


def _get(url: str, headers: dict | None = None) -> BeautifulSoup:
    resp = requests.get(url, headers=headers or HEADERS, timeout=TIMEOUT)
    if resp.status_code != 200:
        raise FetchError(f"HTTP {resp.status_code}: {url}")
    resp.encoding = resp.apparent_encoding or resp.encoding
    return BeautifulSoup(resp.text, "html.parser")


def _iso(y: int, m: int, d: int) -> str:
    return dt.date(y, m, d).isoformat()


# ---------------------------------------------------------------- FOMC

def fetch_fomc(target_years: list[int]) -> list[dict]:
    """FRB の FOMC カレンダーページから開催日程を取得する。

    ページ構造: 年ごとに `<div class="panel panel-default">` があり、パネル見出しに
    "2026 FOMC Meetings"、その中に月名 (`.fomc-meeting__month`) と日 (`.fomc-meeting__date`)
    のペアが並ぶ。SEP 公表回は日付欄に "*" が付く。
    """
    soup = _get(URL_FOMC)
    events: list[dict] = []

    for panel in soup.select("div.panel"):
        heading = panel.find(["h4", "h3", "div"], class_=re.compile("panel-heading|panel-title"))
        head_txt = heading.get_text(" ", strip=True) if heading else panel.get_text(" ", strip=True)[:60]
        ym = re.search(r"(20\d{2})", head_txt)
        if not ym:
            continue
        year = int(ym.group(1))
        if year not in target_years:
            continue

        rows = panel.select("div.fomc-meeting")
        for row in rows:
            mon_el = row.select_one(".fomc-meeting__month")
            day_el = row.select_one(".fomc-meeting__date")
            if not mon_el or not day_el:
                continue
            mon_txt = mon_el.get_text(" ", strip=True).lower()
            day_txt = day_el.get_text(" ", strip=True)

            # 月をまたぐ回 ("April/May") は開始月・終了月が別
            mon_parts = [p.strip() for p in re.split(r"[/\-]", mon_txt) if p.strip()]
            mon_start = MONTHS_EN.get(mon_parts[0])
            mon_end = MONTHS_EN.get(mon_parts[-1], mon_start)
            if mon_start is None:
                continue

            is_sep = "*" in day_txt
            nums = re.findall(r"\d+", day_txt)
            if not nums:
                continue
            d_start = int(nums[0])
            d_end = int(nums[-1]) if len(nums) > 1 else d_start

            y_start = year
            y_end = year + 1 if mon_end < mon_start else year
            try:
                start = _iso(y_start, mon_start, d_start)
                end = _iso(y_end, mon_end, d_end)
            except ValueError:
                continue

            note = "アメリカの中央銀行が政策金利を決める会合"
            if is_sep:
                note += "。今回は先行きの金利見通し（SEP）も同時に公表する回"
            events.append({
                "id": f"fomc_{y_start}_{mon_start:02d}",
                "name": "米FOMC",
                "start_date": start,
                "end_date": end,
                "time": None,
                "importance": 3,
                "note": note,
                "source_url": URL_FOMC,
                "source": "fomc",
            })

    if not events:
        raise FetchError("FOMC 日程を 1 件も抽出できませんでした（ページ構造の変更の可能性）")
    return events


# ---------------------------------------------------------------- 日銀

_JP_MD = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")


def fetch_boj(target_years: list[int]) -> list[dict]:
    """日銀の金融政策決定会合スケジュールページから開催日程を取得する。

    ページ構造: 「表 YYYY年」を caption に持つ表が年ごとにあり、1 列目が
    「1月22日（木）・23日（金）」形式の開催日、2 列目が展望レポートの公表日（無ければ "-"）。
    """
    soup = _get(URL_BOJ_MPM)
    events: list[dict] = []

    for table in soup.find_all("table"):
        cap = table.find("caption")
        cap_txt = cap.get_text(" ", strip=True) if cap else ""
        ym = re.search(r"(20\d{2})\s*年", cap_txt)
        if not ym:
            continue
        year = int(ym.group(1))
        if year not in target_years:
            continue

        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            date_txt = cells[0].get_text(" ", strip=True)
            outlook_txt = cells[1].get_text(" ", strip=True)

            md = _JP_MD.findall(date_txt)
            if not md:
                # 「1月22日（木）・23日（金）」の 2 日目は「23日」だけの場合がある
                m0 = re.search(r"(\d{1,2})\s*月", date_txt)
                days = re.findall(r"(\d{1,2})\s*日", date_txt)
                if not m0 or not days:
                    continue
                mon = int(m0.group(1))
                md = [(str(mon), days[0])] + [(str(mon), d) for d in days[1:]]

            mon_s, day_s = int(md[0][0]), int(md[0][1])
            mon_e, day_e = int(md[-1][0]), int(md[-1][1])
            # 「1月22日（木）・23日（金）」は 2 日目の月表記が省略されるため補完
            days_all = re.findall(r"(\d{1,2})\s*日", date_txt)
            if len(md) == 1 and len(days_all) > 1:
                day_e = int(days_all[-1])
                mon_e = mon_s
                if day_e < day_s:  # 月をまたぐ
                    mon_e = mon_s + 1
            y_e = year + 1 if mon_e < mon_s else year
            try:
                start = _iso(year, mon_s, day_s)
                end = _iso(y_e, mon_e, day_e)
            except ValueError:
                continue

            has_outlook = bool(re.search(r"\d{1,2}\s*月", outlook_txt))
            if has_outlook:
                name = "日銀 金融政策決定会合（展望レポート公表回）"
                note = ("日本の中央銀行が政策金利を決める会合。今回は物価と景気の先行き見通しを"
                        "まとめた資料（展望レポート）も同日に公表する")
            else:
                name = "日銀 金融政策決定会合"
                note = "日本の中央銀行が政策金利や国債の買い入れ方針を決める会合。最終日の午後に結果を発表する"

            events.append({
                "id": f"boj_mpm_{year}_{mon_s:02d}",
                "name": name,
                "start_date": start,
                "end_date": end,
                "time": None,
                "importance": 3,
                "note": note,
                "source_url": URL_BOJ_MPM,
                "source": "boj",
            })

    if not events:
        raise FetchError("日銀 金融政策決定会合の日程を 1 件も抽出できませんでした（ページ構造の変更の可能性）")
    return events


# ---------------------------------------------------------------- 内閣府 GDP

_CAO_QUARTER = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*-\s*(\d{1,2})\s*月期\s*[（(]\s*([12])\s*次速報")
_CAO_DATE = re.compile(r"(20\d{2})\s*[（(]?[^)）]*[)）]?\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_CAO_TIME = re.compile(r"(\d{1,2})\s*時\s*(\d{1,2})\s*分")


def fetch_cao(target_years: list[int]) -> list[dict]:
    """内閣府 経済社会総合研究所の公表予定ページから四半期別 GDP 速報の日程を取得する。

    ページ構造: 「事項 / 公表予定日 / 公表時刻」の 3 列表。事項欄が
    「2026年4-6月期（2次速報）」形式、公表予定日が「2026（令和8）年9月8日（火）」形式。
    """
    soup = _get(URL_CAO_QE)
    events: list[dict] = []

    for table in soup.find_all("table"):
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if len(cells) < 2:
                continue
            item = cells[0].get_text(" ", strip=True)
            qm = _CAO_QUARTER.search(item)
            if not qm:
                continue
            q_year, q_from, q_to, kind = int(qm.group(1)), int(qm.group(2)), int(qm.group(3)), qm.group(4)

            date_txt = cells[1].get_text(" ", strip=True)
            dm = _CAO_DATE.search(date_txt)
            if not dm:
                # 「2026年9月頃」等、日付が確定していない行は入れない（推測補完の禁止）
                continue
            y, mo, d = int(dm.group(1)), int(dm.group(2)), int(dm.group(3))
            if y not in target_years and y - 1 not in target_years:
                continue

            time_str = None
            if len(cells) >= 3:
                tm = _CAO_TIME.search(cells[2].get_text(" ", strip=True))
                if tm:
                    time_str = f"{int(tm.group(1)):02d}:{int(tm.group(2)):02d}"

            kind_label = "1次速報" if kind == "1" else "2次速報"
            q_label = f"{q_from}-{q_to}月期"
            if kind == "1":
                note = f"国内で生み出された儲けの合計が{q_from}〜{q_to}月にどれだけ伸びたかの最初の集計値"
            else:
                note = f"{q_from}〜{q_to}月のGDPの改定値。速報の数字を新しい資料で修正したもの"

            q_idx = {1: 1, 4: 2, 7: 3, 10: 4}.get(q_from, 0)
            events.append({
                "id": f"cao_gdp_{q_year}q{q_idx}_{'first' if kind == '1' else 'second'}",
                "name": f"{q_label}GDP {kind_label}",
                "start_date": _iso(y, mo, d),
                "end_date": _iso(y, mo, d),
                "time": time_str,
                "importance": 3,
                "note": note,
                "source_url": URL_CAO_QE,
                "source": "cao",
            })

    if not events:
        raise FetchError("内閣府 GDP 公表予定日を 1 件も抽出できませんでした（ページ構造の変更の可能性）")
    return events


# ---------------------------------------------------------------- 米国指標（BLS / Census）共通

_MONTH_YEAR = re.compile(r"(" + "|".join(m.capitalize() for m in MONTHS_EN) + r")\s+(20\d{2})")
_ET_TIME = re.compile(r"(\d{1,2}):(\d{2})\s*([AP])\.?\s*M", re.IGNORECASE)


def _us_window() -> tuple[dt.date, dt.date]:
    """米国指標の取得対象期間（実行日〜US_HORIZON_DAYS 日先）。"""
    today = dt.date.today()
    return today, today + dt.timedelta(days=US_HORIZON_DAYS)


def _parse_ref_month(text: str) -> tuple[int, int] | None:
    """"September 2026" → (2026, 9)。見つからなければ None。"""
    m = _MONTH_YEAR.search(text)
    if not m:
        return None
    return int(m.group(2)), MONTHS_EN[m.group(1).lower()]


def _et_to_jst(day: dt.date, time_text: str) -> tuple[str, str] | None:
    """米東部時間の「08:30 AM」を日本時間の (YYYY-MM-DD, HH:MM) に換算する。夏時間は zoneinfo 任せ。"""
    m = _ET_TIME.search(time_text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if ampm == "P" and hour != 12:
        hour += 12
    if ampm == "A" and hour == 12:
        hour = 0
    et = dt.datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ_ET)
    jst = et.astimezone(TZ_JST)
    return jst.date().isoformat(), jst.strftime("%H:%M")


def _month_iter(start: dt.date, end: dt.date):
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


# ---------------------------------------------------------------- 米労働統計局 BLS

# 公式カレンダー上の指標名 → (id 用キー, 和名, 重要度, 注釈)。重要度は _cr §23 の星基準。
_BLS_TARGETS = {
    "Consumer Price Index": (
        "cpi", "米CPI", 3,
        "アメリカで暮らしに使うモノやサービスの値段が、1年前と比べてどれだけ上がったかを示す数字",
    ),
    "Employment Situation": (
        "nfp", "米雇用統計", 3,
        "アメリカで会社に雇われて働く人が前の月よりどれだけ増えたかと、仕事を探している人の割合（失業率）を示す数字",
    ),
    "Producer Price Index": (
        "ppi", "米PPI", 2,
        "アメリカの企業どうしが売り買いするモノやサービスの値段がどれだけ動いたかを示す数字",
    ),
}


def fetch_bls(target_years: list[int]) -> list[dict]:  # noqa: ARG001 - 期間は実行日基準
    """米労働統計局 BLS の月別リリースカレンダーから CPI・雇用統計・PPI の発表日を取得する。

    ページ構造: 月ごとのカレンダー表で、日付セル `<td id="d1014">` の中に
    `<p><strong>Consumer Price Index</strong>September 2026<br>08:30 AM</p>` 形式で
    指標名・対象月・発表時刻（米東部時間）が並ぶ。前月末・翌月初のセルも同じ表に含まれる。
    """
    today, horizon = _us_window()
    events: dict[str, dict] = {}
    page_errors: list[str] = []

    for year, month in _month_iter(today, horizon):
        url = URL_BLS_MONTH.format(year=year, month=month)
        try:
            soup = _get(url, headers=BLS_HEADERS)
        except Exception as exc:  # noqa: BLE001 - 月単位の失敗は明示して続行
            page_errors.append(f"{year}-{month:02d}: {exc}")
            continue

        for td in soup.find_all("td", id=re.compile(r"^d\d{4}$")):
            cell_m, cell_d = int(td["id"][1:3]), int(td["id"][3:5])
            cell_y = year
            if month == 1 and cell_m == 12:
                cell_y = year - 1
            elif month == 12 and cell_m == 1:
                cell_y = year + 1
            try:
                release = dt.date(cell_y, cell_m, cell_d)
            except ValueError:
                continue
            if not (today <= release <= horizon):
                continue

            for p in td.find_all("p"):
                strong = p.find("strong")
                if not strong:
                    continue
                spec = _BLS_TARGETS.get(strong.get_text(" ", strip=True))
                if not spec:
                    continue
                rest = p.get_text("\n", strip=True).split("\n")[1:]
                rest_txt = " ".join(rest)
                ref = _parse_ref_month(rest_txt)
                jst = _et_to_jst(release, rest_txt)
                if not ref or not jst:
                    # 対象月・時刻が読めない行は入れない（推測補完の禁止）
                    continue
                key, label, importance, note = spec
                ref_y, ref_m = ref
                eid = f"bls_{key}_{ref_y}_{ref_m:02d}"
                events[eid] = {
                    "id": eid,
                    "name": f"{label}（{ref_m}月分）",
                    "start_date": jst[0],
                    "end_date": jst[0],
                    "time": jst[1],
                    "importance": importance,
                    "note": note,
                    "source_url": url,
                    "source": "bls",
                }

    for err in page_errors:
        print(f"[WARN] bls: 月別カレンダーを取得できませんでした {err}", file=sys.stderr)
    if not events:
        raise FetchError("BLS の CPI・雇用統計・PPI の発表日を 1 件も抽出できませんでした"
                         "（ページ構造の変更または取得拒否の可能性）")
    return list(events.values())


# ---------------------------------------------------------------- 米国勢調査局 Census

_CENSUS_RETAIL = "Advance Monthly Sales for Retail and Food Services"
_CENSUS_DATE = re.compile(r"([A-Za-z]+)\s+(\d{1,2}),\s*(20\d{2})")
_CENSUS_HEADING = re.compile(r"(20\d{2})\s+Economic Indicator Release Schedule")


def _census_page_for(year: int) -> tuple[str, BeautifulSoup]:
    """指定年の公表スケジュール一覧を返す。見出しの年が一致するページだけを採用する。"""
    tried: list[str] = []
    for url in (URL_CENSUS_LIST.format(year=year), URL_CENSUS_LIST_CURRENT):
        try:
            soup = _get(url)
        except FetchError as exc:
            tried.append(str(exc))
            continue
        heads = [h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])]
        if any((m := _CENSUS_HEADING.search(h)) and int(m.group(1)) == year for h in heads):
            return url, soup
        tried.append(f"{url} に {year} 年版の見出しがありません")
    raise FetchError(" / ".join(tried))


def fetch_census(target_years: list[int]) -> list[dict]:  # noqa: ARG001 - 期間は実行日基準
    """米国勢調査局 Census の年別 経済指標公表スケジュールから小売売上高（速報）の発表日を取得する。

    ページ構造: 「指標名 / 公表日 / 公表時刻 / 対象期間」の一覧表。指標名が
    "Advance Monthly Sales for Retail and Food Services"、公表日が "October 15, 2026"、
    公表時刻が "8:30 AM"（米東部時間）、対象期間が "September 2026" 形式。
    """
    today, horizon = _us_window()
    events: dict[str, dict] = {}
    page_errors: list[str] = []

    for year in sorted({today.year, horizon.year}):
        try:
            url, soup = _census_page_for(year)
        except Exception as exc:  # noqa: BLE001 - 年単位の失敗は明示して続行
            page_errors.append(f"{year}: {exc}")
            continue

        for tr in soup.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all("td")]
            if len(cells) < 4 or cells[0] != _CENSUS_RETAIL:
                continue
            dm = _CENSUS_DATE.search(cells[1])
            if not dm or dm.group(1).lower() not in MONTHS_EN:
                continue
            try:
                release = dt.date(int(dm.group(3)), MONTHS_EN[dm.group(1).lower()], int(dm.group(2)))
            except ValueError:
                continue
            if not (today <= release <= horizon):
                continue
            ref = _parse_ref_month(cells[3])
            jst = _et_to_jst(release, cells[2])
            if not ref or not jst:
                continue
            ref_y, ref_m = ref
            eid = f"census_retail_{ref_y}_{ref_m:02d}"
            events[eid] = {
                "id": eid,
                "name": f"米小売売上高（{ref_m}月分）",
                "start_date": jst[0],
                "end_date": jst[0],
                "time": jst[1],
                "importance": 2,
                "note": "アメリカのお店やネット通販で、1か月にどれだけ物が売れたかを示す数字（速報値）",
                "source_url": url,
                "source": "census",
            }

    for err in page_errors:
        print(f"[WARN] census: 公表スケジュールを取得できませんでした {err}", file=sys.stderr)
    if not events:
        raise FetchError("Census の小売売上高の発表日を 1 件も抽出できませんでした"
                         "（ページ構造の変更または取得拒否の可能性）")
    return list(events.values())


# ---------------------------------------------------------------- 台帳 I/O

def load_ledger() -> dict:
    if not LEDGER_PATH.exists():
        return {"meta": {"timezone": "JST"}, "events": []}
    with LEDGER_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    data.setdefault("meta", {"timezone": "JST"})
    data.setdefault("events", [])
    return data


def merge_events(existing: list[dict], incoming: list[dict], today: str) -> tuple[list[dict], list[dict], list[dict]]:
    """既存を消さずに追記・更新する。戻り値 = (マージ後, 新規, 更新)。"""
    by_id = {e.get("id"): e for e in existing if e.get("id")}
    added, updated = [], []

    for ev in incoming:
        ev = dict(ev)
        ev["updated"] = today
        eid = ev["id"]
        if eid not in by_id:
            existing.append(ev)
            by_id[eid] = ev
            added.append(ev)
            continue
        cur = by_id[eid]
        # YAML から date 型で読まれる場合があるため文字列に正規化してから比較する
        def _norm(v):
            return v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v
        changed = any(_norm(cur.get(k)) != _norm(ev.get(k))
                      for k in ("start_date", "end_date", "time", "name", "importance"))
        if changed:
            before = {k: cur.get(k) for k in ("start_date", "end_date", "time")}
            cur.update(ev)
            updated.append({"id": eid, "before": before,
                            "after": {k: ev.get(k) for k in ("start_date", "end_date", "time")}})

    existing.sort(key=lambda e: (str(e.get("start_date")), str(e.get("id"))))
    return existing, added, updated


# 台帳の見出しグループ（id の接頭辞 → 見出し）。上から順に評価し、最初に一致したグループへ入れる。
_LEDGER_GROUPS = [
    ("fomc_", "FOMC（米連邦公開市場委員会）"),
    ("boj_mpm_", "日銀 金融政策決定会合"),
    ("boj_", "日銀 その他"),
    ("cao_", "内閣府 四半期別GDP速報（QE）"),
    ("bls_", "米労働統計局（BLS）: 米CPI・米雇用統計・米PPI"),
    ("census_", "米国勢調査局（Census）: 米小売売上高"),
]
_FIELD_ORDER = ["id", "name", "start_date", "end_date", "time", "importance",
                "note", "source_url", "source", "updated"]
# 日付・時刻は文字列としてダブルクォートで保存する（date 型で読まれると再読込時の比較がぶれるため）
_QUOTED_KEYS = {"start_date", "end_date", "time", "updated"}


def _stringify(obj):
    if isinstance(obj, dict):
        return {k: _stringify(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify(v) for v in obj]
    if isinstance(obj, (dt.date, dt.datetime)):
        return obj.isoformat()
    return obj


def _scalar(key: str, value) -> str:
    """1 つの値を YAML スカラー表記にする。読み戻して同じ値になる最も素直な書き方を選ぶ。"""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if key not in _QUOTED_KEYS and "\n" not in text:
        try:
            if yaml.safe_load(f"k: {text}") == {"k": text}:
                return text
        except yaml.YAMLError:
            pass
    return json.dumps(text, ensure_ascii=False)


def _group_label(eid: str) -> str:
    for prefix, label in _LEDGER_GROUPS:
        if eid.startswith(prefix):
            return label
    return "その他"


def _render_body(data: dict) -> str:
    """meta と events を書き出す。events は発表主体ごとの見出しコメント付きで、各見出し内は日付順。"""
    lines = ["meta:"]
    for k, v in data.get("meta", {}).items():
        lines.append(f"  {k}: {_scalar(k, v)}")
    lines.append("")
    lines.append("events:")

    order = [label for _, label in _LEDGER_GROUPS] + ["その他"]
    grouped: dict[str, list[dict]] = {label: [] for label in order}
    for ev in data.get("events", []):
        grouped[_group_label(str(ev.get("id", "")))].append(ev)

    for label in order:
        evs = sorted(grouped[label], key=lambda e: (str(e.get("start_date")), str(e.get("id"))))
        if not evs:
            continue
        sep = "" if label.endswith("）") else " "  # 既存台帳の見出し表記（全角括弧の後は空白なし）に合わせる
        lines.append(f"  # ---- {label}{sep}----")
        for ev in evs:
            keys = [k for k in _FIELD_ORDER if k in ev] + [k for k in ev if k not in _FIELD_ORDER]
            for i, k in enumerate(keys):
                prefix = "  - " if i == 0 else "    "
                lines.append(f"{prefix}{k}: {_scalar(k, ev[k])}")
    return "\n".join(lines) + "\n"


def save_ledger(data: dict) -> None:
    """台帳を書き戻す。先頭のスキーマ説明コメントは維持し、書き出し後の読み戻しで内容一致を検証する。"""
    header_lines: list[str] = []
    if LEDGER_PATH.exists():
        with LEDGER_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    header_lines.append(line.rstrip("\n"))
                else:
                    break

    data = _stringify(data)
    body = _render_body(data)

    # 書き出した YAML を読み戻し、1 件でも値が食い違えば書き込まずに止める（既存行の消失・改変の防止）
    reread = yaml.safe_load(body) or {}
    want = sorted(data.get("events", []), key=lambda e: str(e.get("id")))
    got = sorted(_stringify(reread.get("events", [])), key=lambda e: str(e.get("id")))
    if want != got or _stringify(reread.get("meta")) != data.get("meta"):
        raise FetchError("台帳の書き出し結果を読み戻すと内容が一致しません。台帳は書き換えていません。")

    with LEDGER_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        if header_lines:
            fh.write("\n".join(header_lines).rstrip() + "\n\n")
        fh.write(body)


# ---------------------------------------------------------------- CLI

FETCHERS = {"fomc": fetch_fomc, "boj": fetch_boj, "cao": fetch_cao,
            "bls": fetch_bls, "census": fetch_census}


def main() -> int:
    ap = argparse.ArgumentParser(description="経済イベント日程台帳を公式サイトから更新する（ローカル実行専用）")
    ap.add_argument("--dry-run", action="store_true", help="取得結果を表示するだけで台帳を書き換えない")
    ap.add_argument("--source", choices=[*FETCHERS, "all"], default="all", help="取得元（既定: all）")
    args = ap.parse_args()

    today = dt.date.today()
    today_s = today.isoformat()
    target_years = [today.year, today.year + 1]
    sources = list(FETCHERS) if args.source == "all" else [args.source]

    fetched: list[dict] = []
    failures: list[str] = []
    for name in sources:
        try:
            got = FETCHERS[name](target_years)
            print(f"[OK]   {name}: {len(got)} 件取得")
            for ev in sorted(got, key=lambda e: e["start_date"]):
                span = ev["start_date"] if ev["start_date"] == ev["end_date"] else f"{ev['start_date']}〜{ev['end_date']}"
                print(f"         {span} {ev['time'] or '--:--'}  {ev['name']}")
            fetched.extend(got)
        except Exception as exc:  # noqa: BLE001 - 失敗は握りつぶさず明示する
            print(f"[FAIL] {name}: {exc}", file=sys.stderr)
            failures.append(name)

    if not fetched:
        print("[FATAL] 1 件も取得できませんでした。台帳は変更しません（推測での補完はしない）。", file=sys.stderr)
        return 1

    data = load_ledger()
    merged, added, updated = merge_events(list(data.get("events", [])), fetched, today_s)

    print(f"\n新規 {len(added)} 件 / 更新 {len(updated)} 件 / 台帳合計 {len(merged)} 件")
    for ev in added:
        print(f"  + {ev['start_date']} {ev['name']}")
    for u in updated:
        print(f"  ~ {u['id']}: {u['before']} -> {u['after']}")

    if args.dry_run:
        print("\n--dry-run のため台帳は書き換えていません。")
        return 0 if not failures else 1

    if not added and not updated:
        print("変更なし。台帳は書き換えていません。")
        return 0 if not failures else 1

    data["events"] = merged
    data.setdefault("meta", {})["updated"] = today_s
    save_ledger(data)
    print(f"\n書き込み完了: {LEDGER_PATH}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
