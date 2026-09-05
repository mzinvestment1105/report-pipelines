"""ノクトラ X アカウントの「検索除外（サーチバン）」状態とインプレッションを日次で自動測定・記録する。

背景（research/sns/x_algorithm/searchban_action_plan.md 手1・手2）:
    2026-09-05 時点でノクトラ（@noctra__ai）の投稿は「投稿本文の完全一致フレーズ」では
    一般検索に出ないが、同じフレーズに `from:noctra__ai` を付けると出る。つまり索引には
    存在するが一般検索の結果集合から除外されている。この状態がいつ解除されるかを毎日
    自動で検知し、あわせてインプレッション推移を欠測なく記録するのが本スクリプトの目的。

測定する項目:
    1. 除外判定（対照実験の自動化）
         自投稿 N 件から特徴的な日本語フレーズを自動抽出し、
           (a) フレーズ単独検索  (b) フレーズ + from:noctra__ai
         (a)=0 かつ (b)>=1 → 除外 / 両方ヒット → 正常 / それ以外 → 判定不能
    2. 対照群
         他アカウントの投稿でも同じ (a)/(b) 判定を行い、検索基盤自体が
         正常に動いていることを確認する。対照が取れないと 1 の判定は無効。
    3. from: 検索の状態（最新タブ・話題タブそれぞれの返却件数）
    4. インプレッション（自投稿直近30件の view_count 等の明細）
    5. アカウント状態（フォロワー/フォロー/投稿数・凍結/ロック/センシティブ等のフラグ）
    6. フォロー中タイムライン（Following タブ）への出現測定
         → 「検索除外だけか、ホーム配信も絞られているか」の切り分けデータ

認証:
    bi/pipelines/fetch_x_posts.py と同一のバーナー垢 Cookie 方式（Playwright + cookie 注入）。
    .env の X_BURNER_AUTH_TOKEN / X_BURNER_CT0 を使う（GHA では Secrets から .env を作る）。
    ノクトラ本垢の認証情報は絶対に使わない。

使い方:
    python check_x_searchban.py                      # 通常の日次実行
    python check_x_searchban.py --headed             # ブラウザを実画面で開いてデバッグ
    python check_x_searchban.py --phrases 3          # 判定に使うフレーズ数を変える
    python check_x_searchban.py --backfill           # 欠測期間の遡及取得
    python check_x_searchban.py --skip-timeline      # フォロー中 TL 測定を省く

出力:
    bi/outputs/x_posts/searchban_log.csv                 1実行1行の追記（既存行は書き換えない）
    bi/outputs/x_posts/x_impressions_{YYYY-MM-DD}.csv    投稿別の明細
    bi/outputs/x_posts/searchban_raw_{YYYY-MM-DD}.json   判定の生データ（デバッグ用）

エラー方針:
    Cookie 失効 / レート制限(429) / GraphQL 仕様変更 を区別して status 列へ記録する。
    黙って失敗しない。判定できない場合は verdict="判定不能" とし、
    「正常」「除外」のどちらとも記録しない。
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv
from playwright.async_api import async_playwright

# fetch_x_posts.py の実装を再利用する（車輪の再発明をしない）。
# GraphQL の抽出ロジック・cookie 注入・stealth 適用は同一実装を共有し、
# X 側の仕様変更時に修正箇所が1つで済むようにする。
sys.path.insert(0, str(Path(__file__).resolve().parent))
from fetch_x_posts import (  # noqa: E402
    _extract_tweets_from_graphql,
    _is_tweet_graphql_url,
    _new_context,
)

JST = timezone(timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(Path(__file__).resolve().parent / ".env")

OUT_DIR = REPO_ROOT / "bi" / "outputs" / "x_posts"
LOG_CSV = OUT_DIR / "searchban_log.csv"

TARGET_HANDLE = "noctra__ai"

# 対照群に使う他アカウント。検索基盤そのものが動いていることの確認用。
# 「一般公開・日本語・日常的に投稿がある」ことだけが条件で、内容は判定に使わない。
CONTROL_HANDLES = ("nikkei", "bloombergjapan", "ReutersJapan")

LOG_HEADER = [
    "measured_at",          # 実行時刻（JST・ISO8601）
    "date",                 # 対象日（JST）
    "status",               # ok / cookie_expired / rate_limited / graphql_changed / error
    "verdict",              # 除外 / 正常 / 判定不能
    "verdict_reason",       # 判定根拠の要約
    "phrases_tested",       # 判定に使ったフレーズ数
    "phrases_bare_hit",     # フレーズ単独検索で自投稿がヒットしたフレーズ数
    "phrases_from_hit",     # フレーズ+from: で自投稿がヒットしたフレーズ数
    "control_verdict",      # 対照群の判定（正常 / 除外 / 判定不能）
    "control_detail",       # 対照群の内訳
    "from_live_count",      # from:{handle} 最新タブの返却件数
    "from_top_count",       # from:{handle} 話題タブの返却件数
    "timeline_status",      # 出現 / 非出現 / 測定不可
    "timeline_detail",      # フォロー中 TL 測定の詳細
    "impressions_n",        # インプレッションを取れた投稿数
    "impressions_median",   # インプレッション中央値
    "impressions_mean",     # インプレッション平均
    "followers_count",
    "friends_count",
    "statuses_count",
    "protected",            # 鍵アカ
    "suspended",            # 凍結
    "possibly_sensitive",   # センシティブ判定
    "error_detail",
]

IMP_HEADER = [
    "date",                 # 明細を書いた日（JST）
    "post_id",
    "created_at",           # 投稿日時（X の原文表記）
    "created_at_jst",       # JST 変換後
    "text_head30",          # 本文冒頭30字
    "impressions",
    "likes",
    "retweets",
    "replies",
    "bookmarks",
    "url",
]


# ---------------------------------------------------------------- ユーティリティ

def _now() -> datetime:
    return datetime.now(JST)


def _today() -> str:
    return _now().strftime("%Y-%m-%d")


def _parse_x_time(s: str | None) -> datetime | None:
    """X の created_at（'Wed Sep 03 12:34:56 +0000 2026'）を JST の datetime にする。"""
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return dt.astimezone(JST)
    except Exception:
        return None


def _as_int(v) -> int | None:
    """view_count 等を int にする。X は数値を文字列で返すことがある。

    数値化できない場合は None を返し、集計から除外する（0 として扱わない。
    0 扱いにすると「インプレッション0の投稿」と区別できなくなるため）。
    """
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None


async def _sleep(low: float = 4.0, high: float = 9.0) -> None:
    """機械的アクセスパターンを避けるためのランダム待機。"""
    await asyncio.sleep(random.uniform(low, high))


# ---------------------------------------------------------------- フレーズ抽出

# 完全一致検索に使うフレーズは「記号・数字を含まない連続した日本語」に限る。
# 記号（#・@・％・→ 等）や数字は X 側の検索正規化で挙動が変わり、
# 「除外されているから0件」なのか「クエリが悪いから0件」なのか区別できなくなるため。
_JP_RUN = re.compile(r"[ぁ-んァ-ヶ一-龥ー]{10,40}")

# 定型句だけのフレーズは他アカウントもヒットし得るため判定材料にしない。
_TOO_COMMON = re.compile(r"^(ありがとうございます|おはようございます|よろしくお願いします)")


def extract_phrases(text: str | None, min_len: int = 10, max_len: int = 20) -> list[str]:
    """本文から完全一致検索に使える特徴的な日本語フレーズを抽出する。

    条件: 記号・数字・英字を含まない連続した日本語 10〜20 文字。
    長い連続日本語からは先頭 max_len 文字を切り出して使う。
    """
    if not text:
        return []
    # URL・メンション・ハッシュタグを先に落とす（本文の一部として検索すると必ず外れる）
    cleaned = re.sub(r"https?://\S+", " ", text)
    cleaned = re.sub(r"[@#][\w_ぁ-んァ-ヶ一-龥]+", " ", cleaned)

    out: list[str] = []
    for m in _JP_RUN.finditer(cleaned):
        run = m.group(0)
        phrase = run[:max_len]
        if len(phrase) < min_len:
            continue
        if _TOO_COMMON.match(phrase):
            continue
        out.append(phrase)
    return out


# ---------------------------------------------------------------- 検索実行

class SearchError(Exception):
    """検索そのものが成立しなかった（＝本当の0件と区別すべき）失敗。"""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind  # cookie_expired / rate_limited / graphql_changed / error
        self.detail = detail


async def run_search(context, query: str, tab: str = "live", scrolls: int = 2) -> dict:
    """1回の検索を実行し、取得ツイートと通信の状態を返す。

    「0件」には2種類ある（検索基盤が返した本当の0件 / 認証切れ等で結果が来ていない）。
    両者を取り違えると除外判定が丸ごと誤るため、GraphQL レスポンスを1本も観測
    できなかった場合と HTTP 401/403/429 を観測した場合は例外にして 0 件と区別する。
    """
    page = await context.new_page()
    tweets: list[dict] = []
    seen: set[str] = set()
    graphql_seen = 0
    http_errors: list[str] = []

    async def on_response(response):
        nonlocal graphql_seen
        url = response.url
        if not _is_tweet_graphql_url(url):
            return
        status = response.status
        if status >= 400:
            http_errors.append(f"HTTP {status}")
            return
        graphql_seen += 1
        try:
            data = await response.json()
        except Exception:
            return
        for t in _extract_tweets_from_graphql(data):
            tid = t.get("id")
            if tid and tid not in seen:
                seen.add(tid)
                tweets.append(t)

    page.on("response", on_response)
    url = f"https://x.com/search?q={quote_plus(query)}&src=typed_query&f={tab}"
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(5)
        for _ in range(scrolls):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(2.5)
    except Exception as e:
        raise SearchError("error", f"navigate 失敗: {e}") from e
    finally:
        if not page.is_closed():
            await page.close()

    if any("429" in e for e in http_errors):
        raise SearchError("rate_limited", f"429 を検知しました（query={query[:30]}）")
    if any(("401" in e or "403" in e) for e in http_errors):
        raise SearchError("cookie_expired", f"401/403 を検知しました（query={query[:30]}）")
    if graphql_seen == 0:
        # GraphQL が1本も観測できない = 検索が実行されていない。
        # 「0件」として記録すると除外の誤検知になるため必ず区別する。
        raise SearchError(
            "graphql_changed",
            f"GraphQL レスポンスを1件も観測できませんでした（query={query[:30]}）",
        )

    return {"tweets": tweets, "graphql_responses": graphql_seen, "http_errors": http_errors}


def _count_by_author(tweets: list[dict], handle: str) -> int:
    h = handle.lower()
    return sum(1 for t in tweets if (t.get("user_screen_name") or "").lower() == h)


async def judge_exclusion(context, handle: str, posts: list[dict], n_phrases: int) -> dict:
    """対照実験（フレーズ単独 vs フレーズ+from:）を自動実行して除外を判定する。"""
    tested: list[dict] = []
    for post in posts:
        if len(tested) >= n_phrases:
            break
        phrases = extract_phrases(post.get("text"))
        if not phrases:
            continue
        phrase = phrases[0]

        entry: dict = {"post_id": post.get("id"), "phrase_len": len(phrase)}
        try:
            bare = await run_search(context, f'"{phrase}"', tab="live", scrolls=1)
            entry["bare_total"] = len(bare["tweets"])
            entry["bare_self"] = _count_by_author(bare["tweets"], handle)
        except SearchError as e:
            entry["bare_error"] = f"{e.kind}: {e.detail}"
        await _sleep()

        try:
            withfrom = await run_search(
                context, f'"{phrase}" from:{handle}', tab="live", scrolls=1
            )
            entry["from_total"] = len(withfrom["tweets"])
            entry["from_self"] = _count_by_author(withfrom["tweets"], handle)
        except SearchError as e:
            entry["from_error"] = f"{e.kind}: {e.detail}"
        await _sleep()

        tested.append(entry)
        print(f"      phrase#{len(tested)}: bare_self={entry.get('bare_self')} "
              f"from_self={entry.get('from_self')}")

    # 判定: フレーズ単独で自投稿が0件 かつ from: 付きで1件以上 → そのフレーズは「除外」
    excluded = 0
    normal = 0
    unknown = 0
    for e in tested:
        if "bare_error" in e or "from_error" in e:
            unknown += 1
        elif e.get("from_self", 0) >= 1 and e.get("bare_self", 0) == 0:
            excluded += 1
        elif e.get("from_self", 0) >= 1 and e.get("bare_self", 0) >= 1:
            normal += 1
        else:
            # from: 付きでも出ない = 索引にすら無い or 検索不調。除外とは断定しない。
            unknown += 1

    return {
        "tested": tested,
        "excluded": excluded,
        "normal": normal,
        "unknown": unknown,
        "bare_hit": sum(1 for e in tested if e.get("bare_self", 0) >= 1),
        "from_hit": sum(1 for e in tested if e.get("from_self", 0) >= 1),
    }


async def judge_control(context, handles: tuple[str, ...]) -> dict:
    """対照群。他アカウントで同じ (a)/(b) 判定を行い検索基盤の正常性を確認する。

    対照が「正常」でなければ、ノクトラ側の 0 件は検索基盤の不調が原因かもしれず、
    除外判定そのものが無効になる。よって本測定は必須とする。
    """
    details: list[dict] = []
    verdict = "判定不能"
    for handle in handles:
        entry: dict = {"handle": handle}
        try:
            # 対照アカウントの最新投稿を from: 検索で取り、その本文からフレーズを作る。
            src = await run_search(context, f"from:{handle}", tab="live", scrolls=1)
            own = [t for t in src["tweets"]
                   if (t.get("user_screen_name") or "").lower() == handle.lower()]
            phrase = None
            for t in own:
                ph = extract_phrases(t.get("text"))
                if ph:
                    phrase = ph[0]
                    break
            if not phrase:
                entry["error"] = "抽出できる日本語フレーズがありませんでした"
                details.append(entry)
                continue
            entry["phrase_len"] = len(phrase)
            await _sleep()

            bare = await run_search(context, f'"{phrase}"', tab="live", scrolls=1)
            entry["bare_self"] = _count_by_author(bare["tweets"], handle)
            entry["bare_total"] = len(bare["tweets"])
            await _sleep()

            wf = await run_search(context, f'"{phrase}" from:{handle}', tab="live", scrolls=1)
            entry["from_self"] = _count_by_author(wf["tweets"], handle)
            await _sleep()

            if entry["bare_self"] >= 1 and entry["from_self"] >= 1:
                entry["verdict"] = "正常"
            elif entry["from_self"] >= 1 and entry["bare_self"] == 0:
                entry["verdict"] = "除外"
            else:
                entry["verdict"] = "判定不能"
        except SearchError as e:
            entry["error"] = f"{e.kind}: {e.detail}"
        details.append(entry)
        print(f"      control @{handle}: {entry.get('verdict') or entry.get('error')}")

        if entry.get("verdict") == "正常":
            # 対照が1つでも「正常」なら検索基盤は動いている。これ以上叩かない。
            verdict = "正常"
            break

    if verdict != "正常":
        vs = [d.get("verdict") for d in details if d.get("verdict")]
        if vs and all(v == "除外" for v in vs):
            verdict = "除外"
    return {"verdict": verdict, "details": details}


# ---------------------------------------------------------------- タイムライン

def _find_user_node(node):
    """GraphQL のレスポンスから User ノードを1つ探す。"""
    if isinstance(node, dict):
        if node.get("__typename") in ("User", "UserUnavailable"):
            return node
        if "rest_id" in node and ("legacy" in node or "core" in node):
            return node
        for v in node.values():
            r = _find_user_node(v)
            if r:
                return r
    elif isinstance(node, list):
        for v in node:
            r = _find_user_node(v)
            if r:
                return r
    return None


async def fetch_profile(context, handle: str) -> dict | None:
    """プロフィールページを開いて UserByScreenName の User ノードを取る。"""
    page = await context.new_page()
    captured: list[dict] = []

    async def on_response(response):
        if "UserByScreenName" not in response.url and "UserByRestId" not in response.url:
            return
        try:
            captured.append(await response.json())
        except Exception:
            return

    page.on("response", on_response)
    try:
        await page.goto(f"https://x.com/{handle}", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(6)
    except Exception:
        return None
    finally:
        if not page.is_closed():
            await page.close()

    for payload in captured:
        u = _find_user_node(payload)
        if u:
            return u
    return None


def _extract_following_flag(profile: dict | None) -> bool | None:
    """「バーナー垢がこのユーザーをフォローしているか」の真偽値を取り出す。

    2026-09-05 実測: X は現在このフラグを relationship_perspectives.following に置いており、
    旧来の legacy.following は空（legacy 自体が空 dict）。
    注意: relationship_counts.following は「そのユーザーのフォロー数（整数）」であり
    フォロー関係のフラグではない。ここを取り違えると常に True 相当になるため、
    bool 型であることを必ず確認してから採用する。
    """
    if not isinstance(profile, dict):
        return None
    perspectives = profile.get("relationship_perspectives")
    if isinstance(perspectives, dict) and isinstance(perspectives.get("following"), bool):
        return perspectives["following"]
    legacy = profile.get("legacy")
    if isinstance(legacy, dict) and isinstance(legacy.get("following"), bool):
        return legacy["following"]
    rel = profile.get("relationship")
    if isinstance(rel, dict) and isinstance(rel.get("following"), bool):
        return rel["following"]
    return None


async def check_following_timeline(context, handle: str, profile: dict | None,
                                   recent_ids: set[str]) -> dict:
    """バーナー垢のフォロー中タイムライン（Following タブ）へ対象が出るかを測定する。

    重要: バーナー垢が対象をフォローしていない場合は「測定不可」と記録し、
    こちらから勝手にフォローはしない（X のフォロー関連規約に触れる操作を無断で行わない）。
    """
    following_flag = _extract_following_flag(profile)

    if profile is None:
        return {"status": "測定不可",
                "detail": "プロフィールを取得できずフォロー関係を確認できませんでした"}
    if following_flag is None:
        return {"status": "測定不可",
                "detail": "フォロー関係のフラグを取得できませんでした（GraphQL 仕様変更の可能性）"}
    if not following_flag:
        return {
            "status": "測定不可",
            "detail": (
                f"バーナー垢が @{handle} をフォローしていないためフォロー中 TL を測定できません。"
                "測定にはバーナー垢からのフォローが必要です"
                "（本スクリプトは自動フォローを行いません）"
            ),
        }

    page = await context.new_page()
    tl: list[dict] = []
    seen: set[str] = set()

    async def on_tl(response):
        # 2026-09-05 実測: 「フォロー中」タブの実体は HomeLatestTimeline であり、
        # 「おすすめ」タブは HomeTimeline。ここを GraphQL 全般や HomeTimeline 込みに
        # すると、直前に開いたプロフィールページのレスポンスや「おすすめ」の
        # 推薦投稿まで混ざり、フォロー配信されていないのに「TL に出現」と
        # 誤カウントされる（実測でこの誤判定が発生した）。
        # フォロー配信の有無を測るという目的上、HomeLatestTimeline のみを採る。
        if "HomeLatestTimeline" not in response.url:
            return
        try:
            data = await response.json()
        except Exception:
            return
        for t in _extract_tweets_from_graphql(data):
            tid = t.get("id")
            if tid and tid not in seen:
                seen.add(tid)
                tl.append(t)

    page.on("response", on_tl)
    try:
        await page.goto("https://x.com/home", wait_until="domcontentloaded", timeout=60000)
        await asyncio.sleep(4)
        # Following タブへ切替（見つからない場合は「おすすめ」のまま測らず測定不可にする）。
        # UI 言語はバーナー垢の設定次第で英語/日本語のどちらもあり得る。
        # また X はタブ一覧（ScrollSnap-List）を複数描画するため、
        # locator をそのまま click すると strict mode 違反で失敗する。必ず .first を使う。
        try:
            tab = page.get_by_role(
                "tab", name=re.compile(r"フォロー中|Following")
            ).first
            await tab.click(timeout=20000)
        except Exception as e:
            return {"status": "測定不可",
                    "detail": f"フォロー中タブを特定できませんでした（UI 変更の可能性）: {e}"}
        # タブ切替は URL を変えないため、HomeLatestTimeline のレスポンスが
        # 実際に届いたかどうかだけが「フォロー中タブに切り替わった」ことの根拠になる。
        # 届かないまま0件を「非出現」と記録すると誤判定になるので、下で件数を確認する。
        await asyncio.sleep(5)
        for _ in range(12):
            await page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            await asyncio.sleep(2.5)
    except Exception as e:
        return {"status": "測定不可", "detail": f"タイムライン取得に失敗: {e}"}
    finally:
        if not page.is_closed():
            await page.close()

    if not tl:
        # フォロー中タイムラインのレスポンスを1件も受け取れていない。
        # これを「非出現」と記録すると、配信が絞られていると誤読するため必ず区別する。
        return {
            "status": "測定不可",
            "detail": ("フォロー中タイムライン（HomeLatestTimeline）のレスポンスを"
                       "受け取れませんでした（タブ切替に失敗した可能性）"),
            "timeline_len": 0,
        }

    positions = [
        i + 1 for i, t in enumerate(tl)
        if (t.get("user_screen_name") or "").lower() == handle.lower()
    ]
    hit_recent = [t.get("id") for t in tl if t.get("id") in recent_ids]

    if positions:
        return {
            "status": "出現",
            "detail": (
                f"フォロー中 TL {len(tl)} 件中 @{handle} の投稿が {len(positions)} 件出現"
                f"（最上位 {min(positions)} 番目・直近投稿との一致 {len(hit_recent)} 件）"
            ),
            "timeline_len": len(tl),
            "positions": positions,
        }
    return {
        "status": "非出現",
        "detail": (
            f"フォロー中 TL {len(tl)} 件を取得しましたが @{handle} の投稿は0件でした"
            "（ホーム配信の絞り込みの可能性／単に新規投稿が古い可能性の両方があります）"
        ),
        "timeline_len": len(tl),
        "positions": [],
    }


# ---------------------------------------------------------------- 自投稿取得

async def fetch_self_posts(context, handle: str, count: int = 30) -> tuple[list[dict], dict]:
    """自投稿を from: 検索経由で取得する（最新タブ・話題タブの件数も記録）。

    プロフィールタイムライン直読みは 2026-08-14 の GraphQL オペレーション名変更で
    無言 0 件になった経緯があるため、実証済みの from: 検索経由を主経路にする。
    """
    meta: dict = {}
    posts: list[dict] = []
    try:
        live = await run_search(context, f"from:{handle}", tab="live", scrolls=8)
        own = [t for t in live["tweets"]
               if (t.get("user_screen_name") or "").lower() == handle.lower()]
        meta["from_live_count"] = len(own)
        posts = own
    except SearchError as e:
        meta["from_live_count"] = None
        meta["from_live_error"] = f"{e.kind}: {e.detail}"
    await _sleep()

    try:
        top = await run_search(context, f"from:{handle}", tab="top", scrolls=8)
        own_top = [t for t in top["tweets"]
                   if (t.get("user_screen_name") or "").lower() == handle.lower()]
        meta["from_top_count"] = len(own_top)
        # 最新タブで足りない分を話題タブで補い、取得の網を広げる
        have = {p.get("id") for p in posts}
        for t in own_top:
            if t.get("id") not in have:
                posts.append(t)
                have.add(t.get("id"))
    except SearchError as e:
        meta["from_top_count"] = None
        meta["from_top_error"] = f"{e.kind}: {e.detail}"

    def _key(t):
        dt = _parse_x_time(t.get("created_at"))
        return dt or datetime(1970, 1, 1, tzinfo=JST)

    posts.sort(key=_key, reverse=True)
    return posts, meta


async def fetch_backfill_posts(context, handle: str, since: str, until: str) -> list[dict]:
    """欠測期間の遡及取得。since/until を週次に刻んで from: 検索を回す。

    X の検索は since:/until: 演算子を受け付けるため、期間を細かく切ることで
    1クエリあたりの取得上限に阻まれずに古い投稿まで到達できる。
    """
    start = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=JST)
    end = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=JST)
    collected: dict[str, dict] = {}
    errors: list[str] = []
    cur = start
    while cur < end:
        nxt = min(cur + timedelta(days=7), end)
        q = f"from:{handle} since:{cur:%Y-%m-%d} until:{nxt:%Y-%m-%d}"
        try:
            res = await run_search(context, q, tab="live", scrolls=5)
            for t in res["tweets"]:
                if (t.get("user_screen_name") or "").lower() == handle.lower() and t.get("id"):
                    collected[t["id"]] = t
            print(f"[backfill] {cur:%m/%d}-{nxt:%m/%d}: 累計 {len(collected)} 件")
        except SearchError as e:
            errors.append(f"{cur:%m/%d}-{nxt:%m/%d}: {e.kind}")
            print(f"[backfill] {cur:%m/%d}-{nxt:%m/%d}: 失敗 {e.kind}")
        await _sleep()
        cur = nxt
    if errors:
        print(f"[backfill] 失敗した期間: {', '.join(errors)}")
    return list(collected.values())


# ---------------------------------------------------------------- プロフィール整形

def flatten_profile(user: dict | None) -> dict:
    """UserByScreenName の User ノードからアカウント状態のフラグを取り出す。

    キー配置は X 側で新旧2系統あるため両方にフォールバックする
    （fetch_x_profile.py の flatten_user と同じ考え方）。
    """
    if not isinstance(user, dict):
        return {}
    legacy = user.get("legacy") or {}
    core = user.get("core") or {}
    rel = user.get("relationship_counts") or {}
    tc = user.get("tweet_counts") or {}
    priv = user.get("privacy") or {}

    def pick(*vals):
        for v in vals:
            if v is not None:
                return v
        return None

    return {
        "screen_name": pick(legacy.get("screen_name"), core.get("screen_name")),
        "followers_count": pick(legacy.get("followers_count"), rel.get("followers")),
        "friends_count": pick(legacy.get("friends_count"), rel.get("following")),
        "statuses_count": pick(legacy.get("statuses_count"), tc.get("tweets")),
        "created_at": pick(legacy.get("created_at"), core.get("created_at")),
        "protected": pick(legacy.get("protected"), priv.get("protected")),
        # 凍結・センシティブ判定。2026-09-05 実測では legacy が空 dict になっており、
        # possibly_sensitive は User ノードの最上位に移動している。
        "suspended": (user.get("__typename") == "UserUnavailable") or legacy.get("suspended"),
        "possibly_sensitive": pick(user.get("possibly_sensitive"),
                                   legacy.get("possibly_sensitive")),
        "is_blue_verified": user.get("is_blue_verified"),
        # 「バーナー垢がこのユーザーをフォローしているか」。
        # relationship_counts.following（フォロー数の整数）とは別物なので専用関数で取る。
        "following": _extract_following_flag(user),
    }


# ---------------------------------------------------------------- CSV 出力

def append_row(path: Path, header: list[str], row: dict) -> None:
    """1行追記。既存があればヘッダを書かず、既存行には一切触れない。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists() and path.stat().st_size > 0
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerow(row)


def append_impressions(posts: list[dict], date_str: str, out_path: Path) -> int:
    """投稿別明細を追記。既に同じ post_id の行がある場合は重複させない。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if out_path.exists() and out_path.stat().st_size > 0:
        with out_path.open(encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                if r.get("post_id"):
                    existing.add(r["post_id"])

    rows = []
    for p in posts:
        pid = p.get("id")
        if not pid or pid in existing:
            continue
        dt = _parse_x_time(p.get("created_at"))
        text = (p.get("text") or "").replace("\n", " ")
        rows.append({
            "date": date_str,
            "post_id": pid,
            "created_at": p.get("created_at"),
            "created_at_jst": dt.strftime("%Y-%m-%d %H:%M") if dt else "",
            "text_head30": text[:30],
            "impressions": p.get("view_count"),
            "likes": p.get("like_count"),
            "retweets": p.get("retweet_count"),
            "replies": p.get("reply_count"),
            "bookmarks": p.get("bookmark_count"),
            "url": p.get("url"),
        })

    if not rows:
        return 0
    exists = out_path.exists() and out_path.stat().st_size > 0
    with out_path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=IMP_HEADER, extrasaction="ignore")
        if not exists:
            w.writeheader()
        w.writerows(rows)
    return len(rows)


# ---------------------------------------------------------------- メイン

async def main_async() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--handle", default=TARGET_HANDLE)
    ap.add_argument("--phrases", type=int, default=5, help="除外判定に使う自投稿の件数")
    ap.add_argument("--count", type=int, default=30, help="インプレッションを取る自投稿件数")
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--skip-timeline", action="store_true", help="フォロー中 TL 測定を省く")
    ap.add_argument("--skip-control", action="store_true", help="対照群測定を省く（非推奨）")
    ap.add_argument("--backfill", action="store_true", help="欠測期間の遡及取得を行う")
    ap.add_argument("--backfill-since", default="2026-08-14")
    ap.add_argument("--backfill-until", default="")
    args = ap.parse_args()

    date_str = _today()
    handle = args.handle
    row: dict = {
        "measured_at": _now().isoformat(timespec="seconds"),
        "date": date_str,
        "status": "ok",
        "verdict": "判定不能",
        "verdict_reason": "",
        "control_verdict": "判定不能",
        "timeline_status": "測定不可",
        "timeline_detail": "",
        "error_detail": "",
    }
    raw: dict = {"measured_at": row["measured_at"], "handle": handle}
    posts: list[dict] = []

    async with async_playwright() as p:
        browser, context = await _new_context(p, args.headed)
        try:
            # ---- 自投稿とインプレッションの取得 + from: 件数
            print(f"[1/5] 自投稿の取得（from:{handle}）")
            posts, meta = await fetch_self_posts(context, handle, args.count)
            row["from_live_count"] = meta.get("from_live_count")
            row["from_top_count"] = meta.get("from_top_count")
            raw["self_meta"] = meta
            if meta.get("from_live_error") or meta.get("from_top_error"):
                err = meta.get("from_live_error") or meta.get("from_top_error")
                row["status"] = str(err).split(":")[0]
                row["error_detail"] = str(err)
            print(f"      取得 {len(posts)} 件 / live={row['from_live_count']} "
                  f"top={row['from_top_count']}")

            # ---- 遡及取得
            if args.backfill:
                until = args.backfill_until or date_str
                print(f"[backfill] {args.backfill_since} 〜 {until}")
                bf = await fetch_backfill_posts(context, handle, args.backfill_since, until)
                have = {q.get("id") for q in posts}
                added = 0
                for t in bf:
                    if t.get("id") not in have:
                        posts.append(t)
                        have.add(t.get("id"))
                        added += 1
                print(f"[backfill] 遡及で {len(bf)} 件取得（新規 {added} 件・合計 {len(posts)} 件）")

            # X の views.count は文字列（例 "1234"）で返るため、
            # isinstance(..., int) だけで判定すると全件が捨てられ 0 件になる。必ず数値化する。
            views = [v for v in (_as_int(q.get("view_count")) for q in posts) if v is not None]
            row["impressions_n"] = len(views)
            row["impressions_median"] = round(statistics.median(views), 1) if views else ""
            row["impressions_mean"] = round(statistics.mean(views), 1) if views else ""

            # ---- 除外判定
            if posts:
                print(f"[2/5] 除外判定（対照実験・最大 {args.phrases} フレーズ）")
                jr = await judge_exclusion(context, handle, posts, args.phrases)
                raw["exclusion"] = jr
                row["phrases_tested"] = len(jr["tested"])
                row["phrases_bare_hit"] = jr["bare_hit"]
                row["phrases_from_hit"] = jr["from_hit"]
                if jr["excluded"] > 0 and jr["normal"] == 0:
                    row["verdict"] = "除外"
                    row["verdict_reason"] = (
                        f"フレーズ単独0件かつfrom:付きヒットが{jr['excluded']}/{len(jr['tested'])}件"
                    )
                elif jr["normal"] > 0 and jr["excluded"] == 0:
                    row["verdict"] = "正常"
                    row["verdict_reason"] = f"両検索でヒット {jr['normal']}/{len(jr['tested'])}件"
                elif jr["excluded"] > 0 and jr["normal"] > 0:
                    row["verdict"] = "判定不能"
                    row["verdict_reason"] = (
                        f"除外{jr['excluded']}件・正常{jr['normal']}件が混在（部分的な除外の可能性）"
                    )
                else:
                    row["verdict"] = "判定不能"
                    row["verdict_reason"] = f"有効な判定が取れず（unknown={jr['unknown']}）"
                print(f"      判定: {row['verdict']} / {row['verdict_reason']}")
            else:
                row["verdict_reason"] = "自投稿を取得できず判定材料なし"

            # ---- 対照群
            if not args.skip_control:
                print("[3/5] 対照群（検索基盤の正常性確認）")
                cr = await judge_control(context, CONTROL_HANDLES)
                raw["control"] = cr
                row["control_verdict"] = cr["verdict"]
                row["control_detail"] = json.dumps(cr["details"], ensure_ascii=False)
                print(f"      対照群: {cr['verdict']}")
                if cr["verdict"] != "正常" and row["verdict"] == "除外":
                    # 対照が正常でなければノクトラの 0 件は検索基盤の不調かもしれない。
                    row["verdict"] = "判定不能"
                    row["verdict_reason"] += "／対照群が正常でないため除外と断定不可"

            # ---- アカウント状態 ＋ フォロー中 TL
            print("[4/5] アカウント状態とフォロー中タイムライン")
            profile_node = await fetch_profile(context, handle)
            prof = flatten_profile(profile_node)
            raw["profile"] = prof
            for k in ("followers_count", "friends_count", "statuses_count",
                      "protected", "suspended", "possibly_sensitive"):
                row[k] = prof.get(k)
            print(f"      followers={prof.get('followers_count')} "
                  f"friends={prof.get('friends_count')} statuses={prof.get('statuses_count')} "
                  f"suspended={prof.get('suspended')} protected={prof.get('protected')}")

            if not args.skip_timeline:
                recent_ids = {q.get("id") for q in posts[:10] if q.get("id")}
                tlr = await check_following_timeline(context, handle, profile_node, recent_ids)
                raw["timeline"] = tlr
                row["timeline_status"] = tlr["status"]
                row["timeline_detail"] = tlr["detail"]
                print(f"      TL: {tlr['status']} / {tlr['detail'][:80]}")

            # ---- 出力
            print("[5/5] 出力")
            imp_path = OUT_DIR / f"x_impressions_{date_str}.csv"
            n_new = append_impressions(posts, date_str, imp_path)
            print(f"      明細 {n_new} 行を追記: {imp_path.name}")

            raw_path = OUT_DIR / f"searchban_raw_{date_str}.json"
            raw_path.parent.mkdir(parents=True, exist_ok=True)
            raw_path.write_text(
                json.dumps(raw, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
        except SearchError as e:
            row["status"] = e.kind
            row["error_detail"] = e.detail
            row["verdict"] = "判定不能"
            row["verdict_reason"] = f"測定中断: {e.kind}"
            print(f"ERROR ({e.kind}): {e.detail}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001
            row["status"] = "error"
            row["error_detail"] = f"{type(e).__name__}: {e}"
            row["verdict"] = "判定不能"
            print(f"ERROR: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            await context.close()
            await browser.close()

    append_row(LOG_CSV, LOG_HEADER, row)
    print(f"[log] {LOG_CSV.name} へ1行追記: status={row['status']} verdict={row['verdict']}")
    return 0 if row["status"] == "ok" else 1


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    raise SystemExit(main())
