"""X（旧 Twitter）先行フォロー（アウトリーチ）候補の抽出と Console 実行スクリプト生成 ETL。

用途:
    運用アカウント（既定 @noctra__ai）を「フォローしていない」金融・AI 系アカウントを、
    取得済みの候補プール（金融キーワード投稿の RT 者一覧・バズ投稿の投稿者一覧）から抽出し、
    先行フォロー＋「相手のプロフィール最上段の投稿（固定があれば固定・無ければ最新）」への
    いいねを行うブラウザ Console 用スクリプト（.js）・候補 CSV・PM 向けレポート（.md）を出力する。

    フォローバック（相手が先にフォローしてくれている相手）は本スクリプトの対象外で、
    make_followback_candidates.py が担当する。両者は互いに相手を除外する。

    読み取り専用。X へのアクセス・書き込みは一切行わない（取得済み JSON を読むだけ）。
    実際のフォロー・いいねは、生成された .js を PM がブラウザの Console に貼って実行する。
    JS 本体は make_followback_candidates.py のエンジンを import して流用し、
    関係判定（フォロバ→先行フォロー）といいね対象（最新→最上段）だけを差し替える。

候補プール（対象日以前・直近 POOL_DAYS 日以内のファイル）:
    1. bi/outputs/x_posts/retweeters_*.json … 金融キーワード投稿を RT した人（fetch_x_retweeters.py）
    2. bi/outputs/x_posts/buzz_*.json       … 国内バズ投稿の投稿者（fetch_x_buzz.py の top_domestic のみ）

除外基準（make_followback_candidates.py と同一の機械基準 + 先行フォロー固有の基準）:
    A. 既にこちらをフォローしている（＝フォローバック側で扱う）
    B. 既にこちらがフォローしている
    C. 過去に自分がフォローしていて following から消えた相手（一度リム済み・反復禁止）
    D. friends_count >= 3000 / ff_ratio >= 2.0 / statuses_count <= 100 / bio 相互フォロー系 / 鍵 / influencers
    E. followers_count >= --max-followers（既定 600・PM 指示 2026-09-04）
    E2. friends_count >= 300、フォロワーがフォロー数の 1.1 倍を超える相手、
        またはフォロー数が取れていない相手（PM 指示 2026-09-04）
    E3. 名前・bio・投稿本文に日本語（ひらがな・カタカナ・漢字）が無い相手（海外アカウント）
    E4. 直近 14 日以内の活動（RT 者なら RT した投稿の投稿日時＝RT はそれ以降、バズ投稿者なら投稿日時）が
        確認できない相手（動いていないアカウント。PM 指示 2026-09-04）
    F. bio（bio が無いバズ投稿者はバズった投稿本文）に金融・AI キーワードが無い
       … URL を除去し、英数字キーワードは単語境界で判定する（短縮 URL 内の ai 等に反応しない）
    G. 過去の outreach_candidates_*.csv で既に対象（verdict = outreach）になった相手

    除外分も CSV には verdict = exclude として全件残す（水増しも黙殺もしない）。

使い方:
    python make_outreach_candidates.py --date 2026-09-04
    python make_outreach_candidates.py --date 2026-09-04 --dry-run
    python make_outreach_candidates.py --max-candidates 50 --max-followers 20000 --account noctra__ai
    python make_outreach_candidates.py --no-like

出力（--dry-run 時は一切出力しない）:
    bi/outputs/x_posts/outreach_candidates_{date}.csv … プール全件の判定結果
    bi/outputs/x_posts/outreach_console_{date}.js     … 実行スクリプト（0 件なら生成しない）
    research/sns/{date}_outreach.md                   … PM 向けレポート

既存ファイルは上書きしない（同名があれば中止して PM に知らせる）。
"""
from __future__ import annotations

import argparse
import csv
import glob
import os
import re
import sys
from datetime import datetime, timedelta
from string import Template

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_followback_candidates as fb  # noqa: E402

X_POSTS_DIR = fb.X_POSTS_DIR
RESEARCH_SNS_DIR = fb.RESEARCH_SNS_DIR
MAX_PER_RUN = fb.MAX_PER_RUN
COOLDOWN_MIN = fb.COOLDOWN_MIN

POOL_DAYS = 60            # 対象日からこの日数以内の候補プールだけを使う
DEFAULT_MAX_FOLLOWERS = 600   # PM 指示 2026-09-04: フォロワー 600 以上はフォローしない
MAX_FRIENDS_OUTREACH = 300    # PM 指示 2026-09-04: フォロー数 300 以上はフォローしない
DEFAULT_MAX_CANDIDATES = 30   # PM 指示 2026-09-04: 1 日の先行フォローは 30 件まで
                              # （実測で 39 件目まで成功し 40 件目に code=88 のレート制限へ到達したため）
MAX_INACTIVE_DAYS = 14       # PM 指示 2026-09-04: 直近 2 週間に RT か投稿の実績が無い相手（動いていない）はフォローしない
MIN_FF_RATIO = 1.0 / 1.1      # PM 指示 2026-09-04: フォロワーがフォロー数の 1.1 倍を超える相手は除外。
                              # ff_ratio = フォロー数 ÷ フォロワー数 なので、この値（約 0.909）未満が除外。
                              # 承認欲求の強い層を外し、フォロー数の方が多い相手を優先する。
JP_RE = re.compile(r"[぀-ヿ一-鿿]")  # ひらがな・カタカナ・漢字

# bio に含まれていたら金融・AI 系とみなすキーワード（小文字化して部分一致）
NICHE_KEYWORDS = {
    "金融": [
        "株", "投資", "投資家", "トレード", "トレーダー", "デイトレ", "スイング", "nisa", "積立",
        "配当", "高配当", "日経", "決算", "証券", "fx", "為替", "先物", "資産運用", "資産形成",
        "個別株", "米国株", "日本株", "インデックス", "オルカン", "s&p", "fire", "億り人",
        "相場", "銘柄", "チャート", "テクニカル", "ファンダ", "マクロ", "金利", "債券",
        "investor", "investing", "trader", "trading", "stocks", "equities", "markets",
        "finance", "hedge fund", "portfolio", "dividend", "etf", "macro", "options",
    ],
    "AI": [
        "ai", "人工知能", "生成ai", "llm", "chatgpt", "claude", "gemini", "openai", "anthropic",
        "機械学習", "深層学習", "ディープラーニング", "プロンプト", "エージェント", "自動化",
        "machine learning", "deep learning", "artificial intelligence", "agents", "prompt",
        "cursor", "copilot", "gpt", "rag", "自然言語", "データサイエンス", "data science",
    ],
}

CSV_FIELDS = fb.CSV_FIELDS[:-1] + ["source", "niche", "active_at", "bio_head"]

DATE_RE = fb.DATE_RE


# ---------------------------------------------------------------- 候補プール


def parse_x_date(v):
    """X の日時文字列（"Wed Sep 02 04:14:58 +0000 2026" または ISO 8601）→ JST の date。取れなければ None。"""
    if not v:
        return None
    for fmt in ("%a %b %d %H:%M:%S %z %Y",):
        try:
            return datetime.strptime(str(v), fmt).astimezone(fb.JST).date()
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=fb.JST)
        return dt.astimezone(fb.JST).date()
    except ValueError:
        return None


def dated_files(pattern: str, target_date: str) -> list:
    """パターンに合う JSON を (日付, パス) で列挙し、対象日以前・POOL_DAYS 日以内に絞る。"""
    lower = (fb.parse_date(target_date) - timedelta(days=POOL_DAYS)).strftime("%Y-%m-%d")
    out = []
    for p in sorted(glob.glob(os.path.join(X_POSTS_DIR, pattern))):
        m = DATE_RE.search(os.path.basename(p))
        if not m:
            continue
        d = m.group(1)
        if lower <= d <= target_date:
            out.append((d, p))
    return out


def load_pool(target_date: str):
    """RT 者一覧とバズ投稿者を screen_name（小文字）→ レコードで統合する。

    同じ相手が複数ファイルに現れた場合は数値項目の揃っている方（RT 者側）を優先し、
    source には出典ファイル名を全て残す。
    """
    pool = {}
    used_files = []

    for d, p in dated_files("retweeters_*.json", target_date):
        doc = fb.load_snapshot(p)
        if not doc:
            continue
        used_files.append(("retweeters", d, p, len(doc.get("accounts") or [])))
        post_dates = {}
        for sp in doc.get("source_posts") or []:
            dt = parse_x_date(sp.get("created_at"))
            if sp.get("id") and dt:
                post_dates[str(sp["id"])] = dt
        for u in doc.get("accounts") or []:
            sn = str(u.get("screen_name") or "").strip()
            if not sn:
                continue
            key = sn.lower()
            rec = pool.get(key)
            if rec is None:
                rec = {"user": dict(u), "sources": [], "pool_kind": "retweeters"}
                pool[key] = rec
            rec["sources"].append(os.path.basename(p))
            for pid in u.get("rt_post_ids") or []:
                dt = post_dates.get(str(pid))
                if dt and (rec.get("active_at") is None or dt > rec["active_at"]):
                    rec["active_at"] = dt
            if rec["pool_kind"] != "retweeters":
                rec["user"].update({k: v for k, v in u.items() if v not in (None, "")})
                rec["pool_kind"] = "retweeters"

    for d, p in dated_files("buzz_*.json", target_date):
        doc = fb.load_snapshot(p)
        if not doc:
            continue
        n = 0
        for list_key in ("top_domestic",):
            for post in doc.get(list_key) or []:
                sn = str(post.get("screen_name") or "").strip()
                if not sn:
                    continue
                n += 1
                key = sn.lower()
                rec = pool.get(key)
                if rec is None:
                    rec = {
                        "user": {
                            "screen_name": sn,
                            "name": post.get("display_name") or "",
                            "followers_count": post.get("followers"),
                            "profile_bio": "",
                        },
                        "sources": [],
                        "pool_kind": "buzz",
                    }
                    pool[key] = rec
                src = os.path.basename(p)
                if src not in rec["sources"]:
                    rec["sources"].append(src)
                text = str(post.get("text") or "").strip()
                if text:
                    rec.setdefault("texts", []).append(text)
                dt = parse_x_date(post.get("created_at"))
                if dt and (rec.get("active_at") is None or dt > rec["active_at"]):
                    rec["active_at"] = dt
        used_files.append(("buzz", d, p, n))

    return pool, used_files


def load_prior_outreach_targets(target_date: str) -> dict:
    """過去の outreach_candidates_*.csv で verdict = outreach だった相手 → 日付。"""
    out = {}
    for p in sorted(glob.glob(os.path.join(X_POSTS_DIR, "outreach_candidates_*.csv"))):
        m = DATE_RE.search(os.path.basename(p))
        if not m or m.group(1) >= target_date:
            continue
        try:
            with open(p, "r", encoding="utf-8-sig", newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("verdict") == "outreach" and row.get("screen_name"):
                        out.setdefault(row["screen_name"].lower(), m.group(1))
        except OSError:
            continue
    return out


# ---------------------------------------------------------------- 判定


URL_RE = re.compile(r"https?://\S+|t\.co/\S+")
ASCII_KW_RE = re.compile(r"^[a-z0-9&. ]+$")


def niche_of(text: str):
    """文面が金融・AI のどちらに当たるかを「分類（一致語）」の形で返す。該当なしは None。

    URL は判定前に取り除く（t.co の短縮 URL に含まれる ai 等への誤反応を防ぐ）。
    英数字のみのキーワードは前後が英数字でない位置にある時だけ一致とみなす
    （"ai" が "aicXKmov" や "mail" に反応しないようにする）。日本語は部分一致。
    """
    t = URL_RE.sub(" ", (text or "").lower())
    if not t.strip():
        return None
    hits = []
    for label, kws in NICHE_KEYWORDS.items():
        for kw in kws:
            if ASCII_KW_RE.match(kw):
                if re.search(r"(?<![a-z0-9])" + re.escape(kw) + r"(?![a-z0-9])", t):
                    hits.append("%s（%s）" % (label, kw))
                    break
            elif kw in t:
                hits.append("%s（%s）" % (label, kw))
                break
    return "・".join(hits) if hits else None


def judge_outreach(rec: dict, sn: str, followers_users: dict, following_users: dict,
                   exclusions: set, unfollowed_history: dict, prior: dict, max_followers: int,
                   target_date: str = None):
    """1 件の候補について (除外理由 or None, niche 表記) を返す。判定順は docstring の A→G。"""
    u = rec["user"]
    if sn in followers_users:
        return "既にフォローされている（フォロバ側）", ""
    if sn in following_users:
        return "既にフォロー中", ""
    if sn in unfollowed_history:
        return "過去にリム済み", ""
    reason = fb.judge(u, sn, exclusions, unfollowed_history)
    if reason:
        return reason, ""
    followers = fb.to_int(u.get("followers_count"))
    if followers is not None and followers >= max_followers:
        return "フォロワー%d以上" % max_followers, ""
    friends = fb.to_int(u.get("friends_count"))
    ratio = fb.to_float(u.get("ff_ratio"))
    if ratio is None and friends is not None and followers:
        ratio = friends / float(followers)
    if friends is None or ratio is None:
        return "フォロー数不明（判定不能）", ""
    if friends >= MAX_FRIENDS_OUTREACH:
        return "フォロー数%d以上" % MAX_FRIENDS_OUTREACH, ""
    if ratio < MIN_FF_RATIO:
        return "フォロワーがフォロー数の1.1倍超", ""
    jp_text = " ".join([str(u.get("name") or ""), str(u.get("profile_bio") or "")] + list(rec.get("texts") or []))
    if not JP_RE.search(jp_text):
        return "日本語なし（海外アカウント）", ""
    active = rec.get("active_at")
    if active is None:
        return "直近の活動を確認できず", ""
    if (fb.parse_date(target_date) - active).days > MAX_INACTIVE_DAYS:
        return "直近%d日の活動なし（最終確認 %s）" % (MAX_INACTIVE_DAYS, active.isoformat()), ""
    niche = niche_of(u.get("profile_bio"))
    if not niche and rec.get("texts"):
        # bio が取れていないバズ投稿者は、バズった投稿本文で判定する
        n2 = niche_of(" ".join(rec["texts"]))
        if n2:
            niche = "投稿本文: " + n2
    if not niche:
        return "bio・投稿本文に金融・AI 語なし", ""
    if sn in prior:
        return "前回対象済み（%s）" % prior[sn], niche
    return None, niche


def sort_by_followback_likelihood(rows: list) -> list:
    """フォロー返しの見込み順。FF比（フォロー数÷フォロワー数）の高い順、同率はフォロワー数の多い順。"""
    return sorted(rows, key=lambda r: (-(fb.to_float(r["user"].get("ff_ratio")) or 0.0),
                                       -(fb.to_int(r["user"].get("followers_count")) or 0)))


def reason_counts(rows: list) -> list:
    order = [
        "既にフォローされている（フォロバ側）",
        "既にフォロー中",
        "過去にリム済み",
        "フォロー数3000以上",
        "FF比2.0以上",
        "投稿数100以下",
        "bio相互フォロー系",
        "鍵アカウント",
        "influencers除外",
    ]
    c = {}
    for r in rows:
        c[r["exclude_reason"]] = c.get(r["exclude_reason"], 0) + 1
    out = [(k, c[k]) for k in order if k in c]
    out += [(k, v) for k, v in sorted(c.items()) if k not in order]
    return out


# ---------------------------------------------------------------- 出力


def display_bio(r: dict) -> str:
    """レポート・CSV の bio 欄。bio が無いバズ投稿者は「[投稿] 本文冒頭」を出す。"""
    bio = r["user"].get("profile_bio") or ""
    if not bio.strip() and r.get("texts"):
        bio = "[投稿] " + r["texts"][0]
    return fb.bio_head(bio)


def write_csv(path: str, candidates: list, excluded: list) -> None:
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for r in sort_by_followback_likelihood(candidates) + fb.sort_excluded(excluded):
            u = r["user"]
            w.writerow(
                {
                    "screen_name": u.get("screen_name", ""),
                    "rest_id": u.get("rest_id", ""),
                    "name": u.get("name", ""),
                    "followers_count": u.get("followers_count", ""),
                    "friends_count": u.get("friends_count", ""),
                    "ff_ratio": u.get("ff_ratio", ""),
                    "statuses_count": u.get("statuses_count", ""),
                    "verdict": r["verdict"],
                    "exclude_reason": r["exclude_reason"],
                    "source": ";".join(r["sources"]),
                    "niche": r.get("niche", ""),
                    "active_at": r["active_at"].isoformat() if r.get("active_at") else "",
                    "bio_head": display_bio(r),
                }
            )


def _replace_once(text: str, old: str, new: str, label: str) -> str:
    """エンジン文字列の差し替え。ちょうど 1 箇所に一致しなければ中止する（黙って壊さない）。"""
    n = text.count(old)
    if n != 1:
        fb.fail(
            "JS エンジンの差し替え点「%s」が %d 箇所見つかりました（1 箇所の想定）。"
            "make_followback_candidates.py の JS が変わっています。本スクリプトの差し替え定義を更新してください。"
            % (label, n)
        )
    return text.replace(old, new)


def patched_js_body() -> str:
    """フォローバック用エンジンを先行フォロー用に差し替える。"""
    body = fb.JS_BODY
    body = _replace_once(
        body,
        "  // ---- 対象アカウント（未フォローバック・フォロワー数降順）------------------\n",
        "  // ---- 対象アカウント（先行フォロー候補・フォロワー数降順）------------------\n",
        "対象コメント",
    )
    body = _replace_once(
        body,
        '        if (conns.indexOf("followed_by") === -1) {\n'
        "          // 相手がフォローを外している＝フォローバックの前提が崩れている\n"
        '          relCache["notfollowedby:" + key] = "1";\n'
        "        }\n",
        '        if (conns.indexOf("followed_by") !== -1) {\n'
        "          // 相手が既にこちらをフォローしている＝フォローバック側のスクリプトで扱う\n"
        '          relCache["followedby:" + key] = "1";\n'
        "        }\n",
        "関係判定",
    )
    body = _replace_once(
        body,
        '    if (relCache["notfollowedby:" + key] === "1") {\n'
        '      return { ok: true, skipped: true, status: 0, note: "相手がフォローを外していた" };\n'
        "    }\n",
        '    if (relCache["followedby:" + key] === "1") {\n'
        '      return { ok: true, skipped: true, status: 0, note: "相手が既にフォロー中（フォロバ側で扱う）" };\n'
        "    }\n",
        "スキップ判定",
    )
    body = _replace_once(
        body,
        '    const goneN = batch.filter((t) => relCache["notfollowedby:" + t.sn.toLowerCase()] === "1").length;\n',
        '    const goneN = batch.filter((t) => relCache["followedby:" + t.sn.toLowerCase()] === "1").length;\n',
        "事前確認集計",
    )
    body = _replace_once(
        body,
        '      "[事前確認] 既にフォロー済み " + alreadyN + " 件・相手がフォローを外していた " + goneN +\n',
        '      "[事前確認] 既にフォロー済み " + alreadyN + " 件・相手が既にフォロー中（フォロバ側） " + goneN +\n',
        "事前確認表示",
    )
    # --- クールダウン（バッチ間 30 分待機）の撤廃（PM 指示 2026-09-04）-------
    # 1 回 10 件の上限は残し、終わったらそのまま貼り直せるようにする。
    # 開始前のゲート（前回実行からの経過時間チェック）を丸ごと取り除く。
    body = _replace_once(
        body,
        "  // 前回実行からの経過時間チェック（連続実行による自動化検知を避けるため）\n"
        "  const nowMs = Date.now();\n"
        "  const lastRun = loadLastRun();\n"
        "  if (lastRun > 0 && window.__fbForce !== true) {\n"
        "    const elapsedMin = (nowMs - lastRun) / 60000;\n"
        "    if (elapsedMin < COOLDOWN_MIN) {\n"
        "      const restMin = Math.ceil(COOLDOWN_MIN - elapsedMin);\n"
        "      console.warn(\n"
        '        "[中止] 前回の実行から " + Math.floor(elapsedMin) + " 分しか経っていません。" +\n'
        '          "バッチ間は最低 " + COOLDOWN_MIN + " 分空けてください（あと約 " + restMin + " 分）。"\n'
        "      );\n"
        "      console.warn(\n"
        '        "どうしても今すぐ続行したい場合のみ、Console に  window.__fbForce = true  " +\n'
        '          "と入力して Enter を押してから、このファイルをもう一度貼り付けてください。"\n'
        "      );\n"
        "      return;\n"
        "    }\n"
        "  }\n",
        "  // クールダウンなし（PM 指示 2026-09-04）。1 回の上限のみ残し、\n"
        "  // 終わったらそのまま貼り直せば続きを即座に処理する。\n",
        "クールダウン判定",
    )
    # 終了時の案内も「待機不要」へ差し替える
    body = _replace_once(
        body,
        "    console.log(\n"
        '      "【クールダウン】次のバッチまで最低 " + COOLDOWN_MIN +\n'
        '        " 分空けてください。残り " + remaining.length + " 件を終えるには、あと " +\n'
        '        Math.ceil(remaining.length / MAX_PER_RUN) + " 回の貼り直しが必要です。"\n'
        "    );\n"
        "    console.log(\n"
        '      "  " + COOLDOWN_MIN + " 分未満で貼り直した場合、本スクリプトは開始前に警告して中止します。"\n'
        "    );\n",
        "    console.log(\n"
        '      "残り " + remaining.length + " 件を終えるには、あと " +\n'
        '        Math.ceil(remaining.length / MAX_PER_RUN) +\n'
        '        " 回の貼り直しが必要です（待機不要・そのまま貼り直せます）。"\n'
        "    );\n",
        "終了時クールダウン案内",
    )
    # --- レート制限（code=88 / HTTP 429）で即時全停止（PM 指示 2026-09-04）-----
    # 実測: 39 件目まで成功し 40 件目で code=88 が出た（2026-09-04）。
    # 連続失敗 3 件を待たずに 1 件目の制限で止め、成功件数を明示する。
    body = _replace_once(
        body,
        "    if (json && Array.isArray(json.errors) && json.errors.length > 0) {\n"
        "      const e0 = json.errors[0];\n"
        "      return {\n"
        "        ok: false,\n"
        '        reason: "API エラー code=" + e0.code + " " + e0.message,\n'
        "        status: res.status,\n"
        "      };\n"
        "    }\n"
        "    if (!res.ok) {\n"
        '      return { ok: false, reason: "HTTP " + res.status, status: res.status };\n'
        "    }\n",
        "    if (json && Array.isArray(json.errors) && json.errors.length > 0) {\n"
        "      const e0 = json.errors[0];\n"
        "      // code=88 (Rate limit exceeded) / HTTP 429 は X 側の回数制限。\n"
        "      // 続けても全て失敗し検知を強めるだけなので、その場で全停止する。\n"
        "      const limited =\n"
        "        e0.code === 88 || res.status === 429 || /rate limit/i.test(String(e0.message || \"\"));\n"
        "      return {\n"
        "        ok: false,\n"
        "        rateLimited: limited,\n"
        '        reason: "API エラー code=" + e0.code + " " + e0.message,\n'
        "        status: res.status,\n"
        "      };\n"
        "    }\n"
        "    if (!res.ok) {\n"
        "      return {\n"
        "        ok: false,\n"
        "        rateLimited: res.status === 429,\n"
        '        reason: "HTTP " + res.status,\n'
        "        status: res.status,\n"
        "      };\n"
        "    }\n",
        "レート制限の検知",
    )
    body = _replace_once(
        body,
        "    if (consecutiveFail >= MAX_CONSECUTIVE_FAIL) {\n",
        "    // レート制限に当たったら連続失敗の判定を待たずに即停止する\n"
        "    if (r.rateLimited === true) {\n"
        "      console.warn(\n"
        '        "[全停止] X のフォロー回数制限に達しました（' + "code=88 / HTTP 429" + '）。" +\n'
        '          "この実行では " + okCount + " 件フォローできました。時間を空けてから貼り直してください。"\n'
        "      );\n"
        '      stoppedReason = "レート制限（この実行での成功 " + okCount + " 件）";\n'
        "      break;\n"
        "    }\n"
        "\n"
        "    if (consecutiveFail >= MAX_CONSECUTIVE_FAIL) {\n",
        "レート制限での即時停止",
    )
    for old, new in (
        ('"fb_done_$key"', '"ob_done_$key"'),
        ('"fb_lastrun_$key"', '"ob_lastrun_$key"'),
        ('"fb_rel_$key"', '"ob_rel_$key"'),
        ('x_posts/followback_result_" + RUN_DATE', 'x_posts/outreach_result_" + RUN_DATE'),
    ):
        body = _replace_once(body, old, new, old)
    return body


def patched_like_engine() -> str:
    """いいね対象を「固定ツイートを除いた最新」→「最上段（固定があれば固定・無ければ最新）」へ。"""
    eng = fb.JS_LIKE_ENGINE
    eng = _replace_once(
        eng,
        "  // GraphQL UserTweets から「固定・RT・リプライを除いた最新の本人ツイート」を選ぶ\n",
        "  // GraphQL UserTweets から「プロフィール最上段の投稿」を選ぶ。\n"
        "  // 固定ツイートがあればそれ（本人の投稿なら RT・リプライでも可）、無ければ\n"
        "  // RT・リプライを除いた最新の本人ツイート。\n",
        "いいね対象コメント",
    )
    eng = _replace_once(
        eng,
        '      // 固定ツイート・プロモーション枠は対象外\n'
        '      if (eid.indexOf("promoted-") === 0 || eid.indexOf("pinned-") === 0) continue;\n'
        '      if (/(^|-)pinned/i.test(eid) || /promoted/i.test(eid)) continue;\n'
        "      const content = entry && entry.content;\n"
        "      const sc = content && (content.socialContext ||\n"
        "        (content.itemContent && content.itemContent.socialContext));\n"
        "      if (sc) {\n"
        "        const scText = JSON.stringify(sc).toLowerCase();\n"
        '        if (scText.indexOf("pinned") !== -1 || scText.indexOf("固定") !== -1) continue;\n'
        "      }\n"
        "      const res = tweetResultOf(entry);\n"
        "      if (!res) continue;\n"
        "      const legacy = res.legacy;\n"
        "      if (!legacy) continue;\n",
        '      // プロモーション枠は対象外\n'
        '      if (eid.indexOf("promoted-") === 0 || /promoted/i.test(eid)) continue;\n'
        "      const content = entry && entry.content;\n"
        "      const sc = content && (content.socialContext ||\n"
        "        (content.itemContent && content.itemContent.socialContext));\n"
        "      let isPinned = eid.indexOf(\"pinned-\") === 0 || /(^|-)pinned/i.test(eid);\n"
        "      if (!isPinned && sc) {\n"
        "        const scText = JSON.stringify(sc).toLowerCase();\n"
        '        if (scText.indexOf("pinned") !== -1 || scText.indexOf("固定") !== -1) isPinned = true;\n'
        "      }\n"
        "      const res = tweetResultOf(entry);\n"
        "      if (!res) continue;\n"
        "      const legacy = res.legacy;\n"
        "      if (!legacy) continue;\n"
        "      if (isPinned) {\n"
        "        // 固定ツイート＝プロフィール最上段。見つかった時点でこれを採用する\n"
        "        const pinnedAuthor =\n"
        "          (res.core && res.core.user_results && res.core.user_results.result) || null;\n"
        "        const pinnedAuthorId = String(\n"
        '          (pinnedAuthor && pinnedAuthor.rest_id) || legacy.user_id_str || ""\n'
        "        );\n"
        "        if (userId && pinnedAuthorId && pinnedAuthorId !== String(userId)) continue;\n"
        '        const pinnedId = String(res.rest_id || legacy.id_str || "");\n'
        "        if (pinnedId) return pinnedId;\n"
        "        continue;\n"
        "      }\n",
        "固定ツイート優先",
    )
    return eng


def build_js(account: str, target_date: str, targets: list, pool_n: int, excluded_n: int,
             do_like: bool, max_followers: int) -> str:
    key_suffix = target_date.replace("-", "_")
    n = len(targets)
    runs = (n + MAX_PER_RUN - 1) // MAX_PER_RUN
    per_person_sec_avg = 17 if do_like else 14
    batch_n = min(n, MAX_PER_RUN)
    run_min = max(1, int(round(batch_n * per_person_sec_avg / 60)))

    rows_js = "\n".join(
        "    { sn: %s, id: %s },"
        % (fb.js_str(r["user"].get("screen_name")), fb.js_str(str(r["user"].get("rest_id") or "")))
        for r in targets
    )

    if do_like:
        title = " * ノクトラ（@%s）先行フォロー＋最上段投稿いいね実行スクリプト  %s 版（対象 %d 件）\n" % (
            account, target_date, n)
        action = (
            " * 【このスクリプトが行うこと】\n"
            " *   対象1人につき次の2つを行います。\n"
            " *     1. フォロー（/i/api/1.1/friendships/create.json）\n"
            " *     2. その相手のプロフィール最上段の投稿1件へのいいね（GraphQL FavoriteTweet）\n"
            " *   いいねの対象は「固定ツイートがあれば固定ツイート、無ければ RT・リプライを除いた最新の本人投稿」です。\n"
            " *   投稿を取得できなかった相手・いいねが失敗した相手も、フォローは実行します。\n"
            " *\n"
        )
        safety_like = (
            " *   - いいねの失敗は連続失敗カウントに含めません（フォローの失敗のみ数えます）\n"
            " *   - いいねが 5 件連続で失敗した場合は「いいね機能のみ」を自動で無効化し、\n"
            " *     以降はフォローだけを続行します（Console に警告を出します）\n"
        )
    else:
        title = " * ノクトラ（@%s）先行フォロー実行スクリプト  %s 版（対象 %d 件・いいねなし）\n" % (
            account, target_date, n)
        action = (
            " * 【このスクリプトが行うこと】\n"
            " *   対象1人につきフォロー（/i/api/1.1/friendships/create.json）のみを行います（--no-like 版）。\n"
            " *\n"
        )
        safety_like = ""

    header = (
        "/* ==========================================================================\n"
        + title
        + " *\n"
        + action
        + " * 【使い方 3 行】\n"
        " *   1. ブラウザで https://x.com/home を開き、ノクトラ本人でログインした状態にする\n"
        " *   2. F12 キー（または右クリック → 検証）を押して「Console」タブを開く\n"
        " *   3. このファイルの中身を全文コピーして Console に貼り付け、Enter を押す\n"
        " *\n"
        " * 【停止方法】\n"
        " *   Console に  window.__fbStop = true  と入力して Enter（次の1件に進む前に停止します）。\n"
        " *   または、その X のタブを閉じる／ページを再読み込みすれば即座に止まります。\n"
        " *\n"
        " * 【今回の対象】\n"
        " *   %s 時点の候補プール（金融キーワード投稿の RT 者・バズ投稿の投稿者）%d 件のうち、\n"
        " *   除外基準（既にフォロー関係あり・過去にリム済み・フォロー数 3000 以上・FF比 2.0 以上・\n"
        " *   投稿数 100 以下・bio の相互フォロー狙い・鍵・別枠の情報源・フォロワー %d 以上・フォロー数 300 以上・フォロワーがフォロー数の 1.1 倍超・海外・\n"
        " *   bio/投稿本文に金融/AI 語なし・前回対象済み）に触れた %d 件を外し、残る %d 件が対象です。\n"
        " *   並び順は FF比（フォロー数÷フォロワー数）の高い順＝フォロー返しの見込み順です。\n"
        " *\n"
        " * 【1回の上限と貼り直し】\n"
        " *   1回の実行上限は %d 件（MAX_PER_RUN）です。%s。\n"
        " *   処理済みの相手は localStorage（キー %s）に記録されるため、貼り直すたびに続きから進みます。\n"
        " *\n"
        " * 【所要時間の目安】\n"
        " *   1回（最大 %d 件）でおよそ %d 分です。全 %d 件ではおよそ %d 分です。\n"
        " *\n"
        " * 【貼り直しの間隔】\n"
        " *   待機は不要です。1回（%d 件）終わったら、そのまま同じファイルをもう一度貼り付ければ\n"
        " *   続きの %d 件を処理します（クールダウンは PM 指示 2026-09-04 により撤廃）。\n"
        " *\n"
        " * 【安全設計】\n"
        " *   - 1件ごとに 8〜20 秒のランダム待機・1回最大 %d 件・フォロー失敗 3 件連続で全停止\n"
        % (
            target_date, pool_n, max_followers, excluded_n, n,
            MAX_PER_RUN, fb.runs_phrase(n), "ob_done_" + key_suffix,
            MAX_PER_RUN, run_min, n, runs * run_min,
            MAX_PER_RUN, MAX_PER_RUN,
            MAX_PER_RUN,
        )
        + safety_like
        + " *   - 実行直前に friendships/lookup.json で最新の関係を問い合わせ、\n"
        " *     既にフォロー済みの相手・既にこちらをフォローしてくれている相手（フォロバ側で扱う）はスキップ\n"
        " *\n"
        " * 【方式】\n"
        " *   X の内部 API へ POST します。認証は現在ログイン中の cookie（ct0）をブラウザ内で読むだけで、\n"
        " *   外部への送信は一切ありません。DOM 操作方式への自動切替は /followers ページ上でのみ働くため、\n"
        " *   本スクリプトでは実質 API 方式のみです（401/403/404 が続く場合は時間を空けてください）。\n"
        " * ========================================================================== */\n"
    )

    body = Template(patched_js_body()).substitute(
        rows=rows_js,
        mpr=str(MAX_PER_RUN),
        cooldown=str(COOLDOWN_MIN),
        key=key_suffix,
        date=target_date,
        dolike="true" if do_like else "false",
        likeengine=patched_like_engine() if do_like else "",
    )
    return header + body


def build_md(account: str, target_date: str, used_files: list, pool_n: int, followers_n: int,
             following_n: int, candidates: list, targets: list, excluded: list, js_path,
             csv_path: str, do_like: bool, max_followers: int) -> str:
    L = []
    a = L.append
    a("# ノクトラ X（@%s）先行フォロー候補（金融・AI）  %s" % (account, target_date))
    a("")
    a("## データ基準日と件数サマリー")
    a("")
    a("| 項目 | 件数 | 取得元 |")
    a("|---|---|---|")
    for kind, d, p, n in used_files:
        label = "RT 者一覧" if kind == "retweeters" else "バズ投稿者"
        a("| 候補プール（%s） | %d | %s（%s） |" % (label, n, fb.rel_repo(p), d))
    a("| 候補プール（重複除去後） | %d | 上記の統合 |" % pool_n)
    a("| フォロワー（followers） | %d | 除外用（既にフォローされている相手） |" % followers_n)
    a("| フォロー中（following） | %d | 除外用（既にフォロー中の相手） |" % following_n)
    a("| 除外 | %d | 下記の判定基準 |" % len(excluded))
    a("| **本日の先行フォロー対象** | **%d** | 除外後 |" % len(targets))
    for reason, cnt in reason_counts(excluded):
        a("| 　除外内訳: %s | %d | 同上 |" % (reason, cnt))
    a("")
    a("## 先行フォロー対象")
    a("")
    if targets:
        a("除外基準に触れなかった %d 件を FF比（フォロー数÷フォロワー数）の高い順＝フォロー返しの見込み順で並べています。" % len(candidates)
          + ("本日の対象は上位 %d 件です。" % len(targets) if len(targets) < len(candidates) else ""))
        a("")
        a("| screen_name | 確認リンク | name | followers | following | FF比 | 投稿数 | 分類 | 活動確認日 | 出典 | bio |")
        a("|---|---|---|---|---|---|---|---|---|---|---|")
        for r in targets:
            u = r["user"]
            a("| %s | https://x.com/%s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                u.get("screen_name", ""), u.get("screen_name", ""), fb.esc_pipe(u.get("name", "")),
                u.get("followers_count", ""), u.get("friends_count", ""), u.get("ff_ratio", ""), u.get("statuses_count", ""),
                r.get("niche", ""), r["active_at"].isoformat() if r.get("active_at") else "",
                fb.esc_pipe(";".join(r["sources"])),
                fb.esc_pipe(display_bio(r)),
            ))
        a("")
        a("`%s` を生成済みです（対象 %d 件・%s）。" % (
            fb.rel_repo(js_path), len(targets),
            "フォロー + 最上段投稿へのいいね" if do_like else "フォローのみ"))
    else:
        a("対象 0 件のため、実行スクリプトは生成していません。")
    a("")
    a("## 実行手順")
    a("")
    a("1. ブラウザで https://x.com/home を開き、@%s でログインした状態にします。" % account)
    a("2. F12 で Console を開き、`%s` の中身を全文貼り付けて Enter を押します。" % fb.rel_repo(js_path) if targets else "2. （対象 0 件）")
    a("3. 1回の実行上限は %d 件です。待機は不要で、終わったらそのまま貼り直せば続きを処理します。" % MAX_PER_RUN)
    a("4. 実行結果の 1 行 JSON を `bi/outputs/x_posts/outreach_result_%s.json` へ保存します。" % target_date)
    a("")
    a("## 除外した相手と理由")
    a("")
    a("候補プール %d 件のうち %d 件を除外しました。" % (pool_n, len(excluded)))
    a("")
    a("| screen_name | 除外理由 | followers | following | 投稿数 | 出典 |")
    a("|---|---|---|---|---|---|")
    for r in fb.sort_excluded(excluded):
        u = r["user"]
        a("| %s | %s | %s | %s | %s | %s |" % (
            u.get("screen_name", ""), r["exclude_reason"], u.get("followers_count", ""),
            u.get("friends_count", ""), u.get("statuses_count", ""), fb.esc_pipe(";".join(r["sources"]))))
    a("")
    a("## 判定基準")
    a("")
    a("- A. 既にこちらをフォローしている相手 → フォローバック側（make_followback_candidates.py）で扱うため除外")
    a("- B. 既にこちらがフォローしている相手 → 除外")
    a("- C. 過去に自分がフォローしていて following から消えた相手 → 反復になるため除外")
    a("- D. フォロー数 %d 以上・FF比 %s 以上・投稿数 %d 以下・bio の相互フォロー系語・鍵・x_influencers.yaml 記載 → 除外" % (
        fb.MAX_FRIENDS_COUNT, fb.MAX_FF_RATIO, fb.MIN_STATUSES_COUNT))
    a("- E. フォロワー %d 以上 → 除外（--max-followers で変更可）" % max_followers)
    a("- E2. フォロー数 %d 以上、フォロワーがフォロー数の 1.1 倍を超える相手、またはフォロー数が取れていない相手 → 除外" % MAX_FRIENDS_OUTREACH)
    a("- E3. 名前・bio・投稿本文に日本語が無い相手（海外アカウント） → 除外")
    a("- E4. 直近 %d 日以内の活動（RT した投稿の投稿日以降に RT、または投稿）が確認できない相手 → 除外" % MAX_INACTIVE_DAYS)
    a("- F. bio（bio が無いバズ投稿者は投稿本文）に金融・AI のキーワードが無い → 除外。URL は除いて判定し、英数字の語は単語単位で一致させる")
    a("- G. 過去の outreach_candidates_*.csv で既に対象になった相手 → 除外")
    a("")
    a("全件の判定結果: `%s`" % fb.rel_repo(csv_path))
    a("")
    return "\n".join(L)


# ---------------------------------------------------------------- main


def main() -> int:
    p = argparse.ArgumentParser(description="X の先行フォロー候補抽出と Console 実行スクリプト生成（読み取り専用）")
    p.add_argument("--date", default=None, help="対象日 YYYY-MM-DD（省略時は JST の当日）")
    p.add_argument("--account", default="noctra__ai", help="対象アカウントの screen_name（既定 noctra__ai）")
    p.add_argument("--dry-run", action="store_true", help="ファイルを出力せず件数だけ表示する")
    p.add_argument("--no-like", action="store_true", help="生成する JS でいいねを行わない")
    p.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES,
                   help="1 日の先行フォロー上限（既定 %d・PM 指示 2026-09-04）" % DEFAULT_MAX_CANDIDATES)
    p.add_argument("--max-followers", type=int, default=DEFAULT_MAX_FOLLOWERS,
                   help="この値以上のフォロワー数は除外（既定 %d）" % DEFAULT_MAX_FOLLOWERS)
    args = p.parse_args()

    target_date = args.date or fb.jst_today()
    try:
        fb.parse_date(target_date)
    except ValueError:
        fb.fail("--date は YYYY-MM-DD 形式で指定してください（受領値: %s）" % target_date)
    if args.max_candidates < 1:
        fb.fail("--max-candidates は 1 以上で指定してください")
    account = args.account

    # --- 入力 ---------------------------------------------------------------
    following_list, following_path, _ff, _acct = fb.find_current_following(account, target_date)
    followers_list, followers_path, _fw, followers_used_date, followers_is_fallback = fb.find_followers(
        account, target_date)
    following_users = fb.users_by_sn(following_list)
    followers_users = fb.users_by_sn(followers_list)

    following_snaps, _followers_snaps = fb.collect_snapshots(account, target_date)
    following_snaps = [s for s in following_snaps if s[0] != target_date]
    following_snaps.append((target_date, following_users, following_path))
    following_snaps.sort(key=lambda s: s[0])
    unfollowed_history = fb.build_unfollowed_history(following_snaps, following_users)
    exclusions = fb.load_influencer_exclusions()
    prior = load_prior_outreach_targets(target_date)

    pool, used_files = load_pool(target_date)
    if not pool:
        fb.fail("候補プールが空です（%s 以前 %d 日以内の retweeters_*.json / buzz_*.json が見つかりません）。"
                "python bi/pipelines/fetch_x_retweeters.py または fetch_x_buzz.py で取得してから再実行してください。"
                % (target_date, POOL_DAYS))

    # --- 判定 ---------------------------------------------------------------
    candidates, excluded = [], []
    for sn in sorted(pool):
        rec = pool[sn]
        reason, niche = judge_outreach(rec, sn, followers_users, following_users, exclusions,
                                       unfollowed_history, prior, args.max_followers, target_date)
        row = {"user": rec["user"], "sources": rec["sources"], "niche": niche, "texts": rec.get("texts") or [],
               "active_at": rec.get("active_at")}
        if reason:
            row.update(verdict="exclude", exclude_reason=reason)
            excluded.append(row)
        else:
            row.update(verdict="outreach", exclude_reason="")
            candidates.append(row)
    targets = sort_by_followback_likelihood(candidates)[: args.max_candidates]
    capped_out = len(candidates) - len(targets)

    csv_path = os.path.join(X_POSTS_DIR, "outreach_candidates_%s.csv" % target_date)
    js_path = os.path.join(X_POSTS_DIR, "outreach_console_%s.js" % target_date)
    md_path = os.path.join(RESEARCH_SNS_DIR, "%s_outreach.md" % target_date)
    do_like = not args.no_like

    # --- 集計表示 -----------------------------------------------------------
    print("対象アカウント : @%s" % account)
    print("対象日         : %s（対象上限 %d 件・フォロワー上限 %d）" % (target_date, args.max_candidates, args.max_followers))
    for kind, d, p_, n in used_files:
        print("候補プール     : %-10s %4d 件  %s" % (kind, n, fb.rel_repo(p_)))
    print("候補プール統合 : %d 件" % len(pool))
    print("followers      : %d 件  %s%s" % (len(followers_users), fb.rel_repo(followers_path),
                                           ("  ※%s の当日分が無いため %s を代用" % (target_date, followers_used_date)) if followers_is_fallback else ""))
    print("following      : %d 件  %s" % (len(following_users), fb.rel_repo(following_path)))
    print("  候補（outreach）: %d 件" % len(candidates))
    print("  除外（exclude） : %d 件" % len(excluded))
    for reason, cnt in reason_counts(excluded):
        print("    - %s: %d 件" % (reason, cnt))
    print("  本日の対象: %d 件%s" % (len(targets), ("（上限により %d 件を次回送り）" % capped_out) if capped_out else ""))
    if targets:
        runs = (len(targets) + MAX_PER_RUN - 1) // MAX_PER_RUN
        print("    1回 %d 件・貼り直し %d 回（待機なし）・JS の動作: %s" % (
            MAX_PER_RUN, runs, "フォロー + 最上段投稿へのいいね" if do_like else "フォローのみ"))

    if args.dry_run:
        print()
        print("[dry-run] ファイルは出力していません。出力予定は次の通りです。")
        print("  CSV : %s" % fb.rel_repo(csv_path))
        print("  JS  : %s" % (fb.rel_repo(js_path) if targets else "（対象 0 件のため生成しません）"))
        print("  MD  : %s" % fb.rel_repo(md_path))
        return 0

    existing = [q for q in [csv_path, md_path] if os.path.exists(q)]
    if targets and os.path.exists(js_path):
        existing.append(js_path)
    if existing:
        fb.fail("出力先に既存ファイルがあります。上書きしないため中止しました。\n  " + "\n  ".join(existing))

    write_csv(csv_path, candidates, excluded)
    if targets:
        with open(js_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(build_js(account, target_date, targets, len(pool), len(excluded), do_like, args.max_followers))
    with open(md_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(build_md(account, target_date, used_files, len(pool), len(followers_users), len(following_users),
                         candidates, targets, excluded, js_path, csv_path, do_like, args.max_followers))
    print()
    print("出力:")
    print("  CSV : %s" % fb.rel_repo(csv_path))
    print("  JS  : %s" % (fb.rel_repo(js_path) if targets else "（対象 0 件のため生成しません）"))
    print("  MD  : %s" % fb.rel_repo(md_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
