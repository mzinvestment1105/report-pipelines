"""動意日次のテーマ欄（本日の動意母集団の `材料` 列・理由素材）の材料の並びと当日の個別解説。

2026-10-05 PM 承認（テーマ欄改修 A）。
  - 旧: 直近 30 日の TDNet 見出し 最大 3 件 → 銘柄別 Yahoo ニュース 最大 3 件 の順で、
        母集団表は先頭 2 件だけを表示していた。当日の材料より 9 月の開示が先に並び、
        当日の値動きの理由が表示枠から押し出されていた（2026-10-05 のセキュリティ関連 6 銘柄）。
  - 新: 夜間 PTS（make_pts_mover_report._pts_reason_material_for）と同じ当日優先にする。
        当日の開示 → 当日の個別解説（立花 QUICK） → 当日のニュース → 過去の開示 → 過去のニュース
        → 引け後（当日 15:30 以降）の開示・ニュース → 市況記事（市況まとめ・銘柄名の列挙の見出し）
  - 「当日」の窓は lib/move_days.assign_day と同じ「前営業日 15:30〜当日 15:30」（_cr §2-C）。
  - 市況記事は取得段階では落とさない（make_mover_report._is_market_wide_news とは別）。
    その銘柄が動いた理由を書いていない見出しのため、並びの最後へ回すだけにする。

当日の個別解説は立花証券 e支店 API（QUICK 個別銘柄解説・ジャンル 3001）の当日分を
yahoo_data[code]["quick"] へ入れて使う（attach_quick_commentary）。認証情報が無い・取得に
失敗した日は何もしない（材料の並びは当日の開示 → 当日のニュース → … のまま）。
"""
from __future__ import annotations

import re
from datetime import date, datetime, time as dtime

from lib.move_days import has_time, parse_dt

CLOSE = dtime(15, 30)          # 東証の大引け（lib/move_days.CLOSE_MIN と同じ境界）
QUICK_GENRE = "3001"           # 立花 API のジャンル: QUICK 個別銘柄解説（動意理由）

# 各区分の件数上限（従来の TDNet 3 件・ニュース 3 件と同じ規模に保つ）
MAX_TODAY_DISC = 3
MAX_QUICK = 2
MAX_TODAY_NEWS = 3
MAX_PAST_DISC = 3
MAX_PAST_NEWS = 3
MAX_AFTER = 2
MAX_ROUNDUP = 2

# 市況まとめ・銘柄名の列挙だけの見出し（2026-10-05 の材料蓄積で実測した形）。
# 例: 「東証グロース（前引け）＝売り買い拮抗、…がS高」「…／グロース市況」「【今週の注目トピック(2)】…」
#     「【本日の材料と銘柄】…」「今朝の注目ニュース！…」「【杉村富生の短期相場観測】…」
#     「＜特別気配＞ …が買い気配」「話題株ピックアップ【夕刊】…」「新興市場銘柄ダイジェスト:…」
#     「日経平均は１４００円程度高、…」「前場に注目すべき3つのポイント～…」「…／オープニングコメント」
#     「注目銘柄ダイジェスト（前場）:…」「TOB・MBO(公開買付)銘柄一覧（…）」
_ROUNDUP_RE = re.compile(
    r"^東証(プライム|スタンダード|グロース|グロ－ス)?[^＝]{0,6}（(前引け|大引け)）"
    r"|市況$|^【今週の注目トピック|^【本日の材料と銘柄】|^今朝の注目ニュース"
    r"|^【杉村富生|^＜特別気配＞|^話題株ピックアップ|^新興市場銘柄ダイジェスト"
    r"|^日経平均|注目すべき\d+つのポイント|オープニングコメント$|^注目銘柄ダイジェスト|銘柄一覧"
)


def is_roundup(title: str) -> bool:
    """市況まとめ・銘柄名の列挙だけの見出しなら True。"""
    return bool(_ROUNDUP_RE.search(str(title or "").strip()))


def _md_hm(dt: datetime) -> str:
    return f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"


def classify(dt: datetime | None, d: date | None, today: date, prev: date | None) -> str:
    """公表日時を 'today'（前営業日 15:30〜当日 15:30）/ 'after'（当日 15:30 以降）/ 'past' に分ける。

    時刻が無く日付だけ分かる場合は、当日の日付なら 'today'、それより後なら 'after'。
    日付も分からないものは 'past'（当日の材料と確認できないため）。
    """
    end = datetime.combine(today, CLOSE)
    if dt is not None:
        if dt >= end:
            return "after"
        start = datetime.combine(prev, CLOSE) if prev is not None else datetime.combine(today, dtime(0, 0))
        return "today" if dt >= start else "past"
    if d is not None:
        if d > today:
            return "after"
        return "today" if d == today else "past"
    return "past"


def order_daily_materials(
    code4: str,
    tdnet_data: dict,
    yahoo_data: dict,
    today: date,
    prev: date | None,
    asof: datetime | None = None,
    news_when=None,
) -> list[str]:
    """テーマ欄の材料テキストを当日優先の順で返す（出典は raw の銘柄ブロックと同一・新たな取得はしない）。

    Args:
        news_when: Yahoo ニュース 1 件 -> (表示ラベル, 配信日時 or None, 日付 or None)。
            make_mover_report._news_when を渡す。
        asof: 検証用。これより後に公表されたものは入れない（TDNet は取得段階で除外済み）。
    """
    today_disc: list[str] = []
    after: list[str] = []
    past_disc: list[str] = []
    for e in (tdnet_data.get(code4, {}) or {}).get("entries", []) or []:
        title = str(e.get("title") or "").strip()
        if not title:
            continue
        pub = str(e.get("published") or "")
        dt = parse_dt(pub)
        timed = dt is not None and has_time(pub)
        kind = classify(dt if timed else None, dt.date() if dt is not None else None, today, prev)
        if kind == "today":
            label = _md_hm(dt) if timed else f"{dt.month}/{dt.day}"
            today_disc.append(f"当日開示 {label}　{title}")
        elif kind == "after":
            label = _md_hm(dt) if timed else f"{dt.month}/{dt.day}"
            after.append(f"引け後開示 {label}　{title}")
        else:
            past_disc.append(f"TDNet {pub[:10]}　{title}")

    y = yahoo_data.get(code4, {}) or {}
    quick: list[str] = []
    for q in y.get("quick") or []:
        dt = q.get("dt")
        title = str(q.get("title") or "").strip()
        if not title or not isinstance(dt, datetime):
            continue
        if asof is not None and dt > asof:
            continue
        quick.append(f"当日解説（QUICK） {_md_hm(dt)}　{title}")

    today_news: list[str] = []
    past_news: list[str] = []
    roundup: list[str] = []
    for n in y.get("news") or []:
        title = str(n.get("title") or "").strip()
        if not title:
            continue
        lab, dt, d = ("", None, None)
        if news_when is not None:
            try:
                lab, dt, d = news_when(n)
            except Exception:
                lab, dt, d = ("", None, None)
        # 一覧の表記が「HH:MM」（当日配信）の記事は、日付と時刻から配信日時を組み立てて窓を判定する
        hm = re.fullmatch(r"(\d{1,2}):(\d{2})", str(n.get("date") or "").strip())
        if dt is None and d is not None and hm:
            dt = datetime.combine(d, dtime(int(hm.group(1)), int(hm.group(2))))
        if asof is not None and ((dt is not None and dt > asof)
                                 or (dt is None and d is not None and d > asof.date())):
            continue
        media = str(n.get("media") or "").strip()
        when = f"{lab} {media}".strip() if lab else media
        if is_roundup(title):
            roundup.append(f"市況記事 {when}　{title}" if when else f"市況記事　{title}")
            continue
        kind = classify(dt, d, today, prev)
        if kind == "today":
            today_news.append(f"当日ニュース {when}　{title}" if when else f"当日ニュース　{title}")
        elif kind == "after":
            after.append(f"引け後ニュース {when}　{title}" if when else f"引け後ニュース　{title}")
        else:
            past_news.append(f"ニュース {lab}　{title}" if lab else f"ニュース　{title}")

    return (
        today_disc[:MAX_TODAY_DISC]
        + quick[:MAX_QUICK]
        + today_news[:MAX_TODAY_NEWS]
        + past_disc[:MAX_PAST_DISC]
        + past_news[:MAX_PAST_NEWS]
        + after[:MAX_AFTER]
        + roundup[:MAX_ROUNDUP]
    )


# ---------------------------------------------------------------------------
# 当日の個別解説（立花証券 e支店 API・QUICK 個別銘柄解説）
# ---------------------------------------------------------------------------

def _quick_dt(n: dict) -> datetime | None:
    d = str(n.get("p_DT") or "").strip()
    t = str(n.get("p_TM") or "").strip()
    try:
        return datetime.strptime(d + (t[:4] if len(t) >= 4 else "0000"), "%Y%m%d%H%M")
    except ValueError:
        return None


def _clean_quick_title(title: str) -> str:
    t = re.sub(r"^<[A-Z]+>", "", str(title or "")).strip()
    return t.lstrip("◇◆■□●").strip()


def _safe_error(e: Exception) -> str:
    """ログ用の失敗理由。通信例外の文言は要求 URL（認証 ID を含む）を含み得るため型名だけにする。"""
    msg = str(e)
    if isinstance(e, RuntimeError) and ("login failed" in msg or "認証情報" in msg):
        return f"{type(e).__name__}: {msg[:160]}"
    return type(e).__name__


def fetch_quick_commentary(
    target: date,
    asof: datetime | None = None,
    limit: int = 2000,
    client=None,
) -> tuple[dict[str, list[dict]], str]:
    """対象日の QUICK 個別銘柄解説を銘柄コード別に返す。戻り値は ({code: [{dt, title}]}, 失敗理由)。"""
    try:
        if client is None:
            from lib.tachibana_client import TachibanaClient
            client = TachibanaClient.from_env()
        client.login()
        ymd = target.strftime("%Y%m%d")
        news = client.get_news_head(limit=limit, date_from=ymd, date_to=ymd)
    except Exception as e:  # 取得失敗で raw 生成を止めない（_cr §36）
        return {}, _safe_error(e)

    out: dict[str, list[dict]] = {}
    for n in news or []:
        if str(n.get("p_GNL") or "") != QUICK_GENRE:
            continue
        dt = _quick_dt(n)
        if dt is None or dt.date() != target:
            continue
        if asof is not None and dt > asof:
            continue
        title = _clean_quick_title(n.get("_decoded_title", ""))
        if not title:
            continue
        for code in str(n.get("p_ISL") or "").split("|"):
            code = code.strip().upper()
            if code:
                out.setdefault(code, []).append({"dt": dt, "title": title})
    for code in out:
        out[code].sort(key=lambda x: x["dt"])
    return out, ""


def attach_quick_commentary(
    yahoo_data: dict,
    target: date,
    asof: datetime | None = None,
    fetcher=fetch_quick_commentary,
) -> str:
    """当日の QUICK 個別解説を yahoo_data[code]["quick"] へ入れ、ログ 1 行を返す。

    yahoo_data に無い銘柄（材料の取得対象外）には入れない。取得できなければ何もしない。
    """
    by_code, err = fetcher(target, asof)
    if err:
        return f"当日の個別解説（立花 QUICK）: 取得できず（{err}）"
    hit = 0
    for code, items in by_code.items():
        if code in yahoo_data and isinstance(yahoo_data[code], dict):
            yahoo_data[code]["quick"] = items
            hit += 1
    total = sum(len(v) for v in by_code.values())
    return f"当日の個別解説（立花 QUICK）: {total} 件（{len(by_code)} 銘柄）のうち材料の取得対象 {hit} 銘柄へ付与"
