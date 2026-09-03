#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""経済イベント日程台帳 (market/calendar/economic_events.yml) の更新スクリプト。

背景
----
マクロレポートの経済カレンダーは Finnhub から取得する実装だったが、無料プランでは
経済カレンダー API が使えず実取得 0 件だった。GHA では WebSearch / WebFetch が 404 で
機能せず、記憶ベースの日付記載は禁止のため、レポート生成側は日程を知る手段が無かった。

本スクリプトは公式サイト（FRB / 日銀 / 内閣府）から確定日程をスクレイプし、台帳 YAML を
更新する。**ローカル実行前提**（GHA からは実行しない）。requests + BeautifulSoup のみを
使い、Claude の WebFetch ツールには依存しない。

使い方
------
    python bi/pipelines/fetch_economic_calendar.py --dry-run
    python bi/pipelines/fetch_economic_calendar.py --source boj
    python bi/pipelines/fetch_economic_calendar.py --source all

更新頻度の目安
--------------
- FOMC / 日銀 金融政策決定会合: 年次固定日程のため年 1 回（前年末〜年初）
- 内閣府 GDP (QE): 四半期ごと

方針
----
- 既存台帳を読み、**新規イベントのみ追記する**。既存エントリ（過去日を含む）を消さない。
  既に同じ id が存在する場合、日付・時刻が変わっていれば更新し、そうでなければ何もしない。
- 取得失敗時は明確にエラーを出す。**推測での補完を一切しない**（取れなかった項目は書かない）。
"""

from __future__ import annotations

import argparse
import datetime as dt
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

# ---------------------------------------------------------------- 定数

REPO_ROOT = Path(__file__).resolve().parents[2]
LEDGER_PATH = REPO_ROOT / "market" / "calendar" / "economic_events.yml"

URL_FOMC = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
URL_BOJ_MPM = "https://www.boj.or.jp/mopo/mpmsche_minu/index.htm"
URL_CAO_QE = "https://www.esri.cao.go.jp/jp/sna/kouhyou/kouhyou_top.html"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}
TIMEOUT = 30

MONTHS_EN = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


class FetchError(RuntimeError):
    """一次情報の取得・解析に失敗したことを表す。推測補完はしない。"""


def _get(url: str) -> BeautifulSoup:
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
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


def save_ledger(data: dict) -> None:
    """台帳を書き戻す。先頭のスキーマ説明コメントは維持する。"""
    header_lines: list[str] = []
    if LEDGER_PATH.exists():
        with LEDGER_PATH.open(encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("#") or not line.strip():
                    header_lines.append(line.rstrip("\n"))
                else:
                    break
    # 日付は文字列で保存する（date 型で書かれると再読込時の比較がぶれるため）
    def _stringify(obj):
        if isinstance(obj, dict):
            return {k: _stringify(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_stringify(v) for v in obj]
        if isinstance(obj, (dt.date, dt.datetime)):
            return obj.isoformat()
        return obj

    body = yaml.safe_dump(_stringify(data), allow_unicode=True, sort_keys=False,
                          default_flow_style=False, width=200)
    with LEDGER_PATH.open("w", encoding="utf-8", newline="\n") as fh:
        if header_lines:
            fh.write("\n".join(header_lines).rstrip() + "\n\n")
        fh.write(body)


# ---------------------------------------------------------------- CLI

FETCHERS = {"fomc": fetch_fomc, "boj": fetch_boj, "cao": fetch_cao}


def main() -> int:
    ap = argparse.ArgumentParser(description="経済イベント日程台帳を公式サイトから更新する（ローカル実行専用）")
    ap.add_argument("--dry-run", action="store_true", help="取得結果を表示するだけで台帳を書き換えない")
    ap.add_argument("--source", choices=["boj", "fomc", "cao", "all"], default="all", help="取得元（既定: all）")
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
