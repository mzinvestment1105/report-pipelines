"""立花証券 e支店 API クライアント（共通ライブラリ）。

認証フロー：
1. ユーザID + 暗証番号 + 公開鍵を立花証券画面に事前登録
2. 認証ID（sAuthId）を発行・取得
3. ログイン要求で仮想URL を受信（PMの公開鍵で暗号化済）
4. 秘密鍵で OAEP-SHA256 復号化 → 仮想URL を当日中使い回し

使い方:
    from lib.tachibana_client import TachibanaClient
    cli = TachibanaClient.from_env()
    cli.login()
    news = cli.get_news_head(limit=500)
    margin = cli.get_credit_margin(["6501", "7203"])
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

# 日本時間（夏時間が無いため +9 時間固定。tzdata に依存しない）
_JST = timezone(timedelta(hours=9))

# API の版（2026-10-08 修正）。設定値（GHA secret・.env）の版が古くても、コード側でこの版へ揃える。
# v4r9 は 2026-09-28 以降ログインが HTTP 404・本文 0 バイト。立花証券公式サンプルの版は v4r10。
API_VERSION = "v4r10"
_API_VERSION_RE = re.compile(r"e_api_v\d+r\d+")


class TachibanaApiError(RuntimeError):
    """立花 API の失敗。文言に要求 URL・認証 ID・仮想 URL を含めない（ログへそのまま出せる）。"""


def normalize_api_base(api_base: str) -> str:
    """設定値の base URL の版（パスの `e_api_v{N}r{M}`）を API_VERSION に揃える。版の部分が無ければそのまま返す。"""
    base = (api_base or "").strip().rstrip("/")
    return _API_VERSION_RE.sub(f"e_api_{API_VERSION}", base)


def _now_str() -> str:
    """要求時刻（p_sd_date）。実行機の時計ではなく日本時間で作る。

    GHA ランナーは UTC のため datetime.now() では 9 時間古い時刻になり、
    サーバーが errno=8（exceed time limit）で拒否する（2026-09-10〜09-25 の GHA ログで確認）。
    """
    n = datetime.now(_JST)
    return n.strftime("%Y.%m.%d-%H:%M:%S.") + f"{n.microsecond // 1000:03d}"


def _get_json(url: str, timeout: int, what: str) -> dict[str, Any]:
    """GET して Shift-JIS の JSON を返す。HTTP 状態・JSON 解析・p_errno を検査し、失敗理由を例外の文言に残す。

    url は認証 ID・仮想 URL を含むため例外の文言へ入れない（通信例外も型名だけにし、元の例外は連結しない）。
    """
    try:
        r = requests.get(url, timeout=timeout)
    except requests.RequestException as e:
        raise TachibanaApiError(f"{what} failed: 通信エラー（{type(e).__name__}）") from None
    if r.status_code != 200:
        raise TachibanaApiError(
            f"{what} failed: HTTP {r.status_code}（本文 {len(r.content)} バイト。API の版・URL を確認）")
    r.encoding = "shift_jis"
    try:
        data = json.loads(r.text)
    except ValueError:
        raise TachibanaApiError(
            f"{what} failed: 応答が JSON ではない（HTTP {r.status_code}・本文 {len(r.content)} バイト）") from None
    if not isinstance(data, dict):
        raise TachibanaApiError(f"{what} failed: 応答の形が想定外（{type(data).__name__}）")
    errno = data.get("p_errno")
    if errno is not None and str(errno) != "0":
        raise TachibanaApiError(f"{what} failed: errno={errno} err={data.get('p_err')}")
    return data


def _decode_headline(b64: str) -> str:
    """ニュースヘッドライン文字列を復号（Base64 → URL エンコード → Shift-JIS）。"""
    try:
        url_str = base64.b64decode(b64).decode("ascii")
        return urllib.parse.unquote(url_str, encoding="cp932")  # 公式サンプルと同じ cp932（Shift-JIS の上位互換）
    except Exception as e:
        return f"[decode error: {e}]"


def _news_of_day(master_url: str, ymd: str, p_no: str) -> list[dict[str, Any]]:
    """ニュース問合取得（v4r10 の CLMMfdsGetNews）。指定日（YYYYMMDD）の全件を返す（本文 p_TX 付き）。

    応答の各要素には日付が無いため p_DT を補う。
    """
    payload = {
        "p_no": p_no,
        "p_sd_date": _now_str(),
        "sCLMID": "CLMMfdsGetNews",
        "sJsonOfmt": "4",
        "p_DT": ymd,
    }
    url = f"{master_url}?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
    data = _get_json(url, 60, "news")
    items = data.get("aCLMMfdsNews") or []
    for n in items:
        n.setdefault("p_DT", ymd)
    return items


def fetch_news(
    master_url: str,
    next_no,
    limit: int = 100,
    offset: int = 0,
    category: str | None = None,
    issue_code: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    max_days: int = 7,
) -> list[dict[str, Any]]:
    """ニュースを新しい順に返す（各要素に p_DT と復号済みの見出し `_decoded_title` を付ける）。

    2026-10-08: v4r10 で旧 CLMMfdsGetNewsHead（ページング・期間指定）は errno=-1（引数エラー）となり、
    日付指定で 1 日分を一括で返す CLMMfdsGetNews（立花証券公式サンプルの方式）に替わったため、日ごとに取得して手元で絞り込む。

    引数:
        master_url: ログインで得た仮想 URL（sUrlMaster）
        next_no: 要求番号 p_no を返す関数（呼ぶたびに増える）
        limit / offset: 新しい順に並べた後の取得件数・開始位置
        category: p_CGL（カテゴリ）に含まれるものだけ
        issue_code: p_ISL（関連銘柄）に含まれるものだけ
        date_from / date_to: YYYYMMDD。片方だけなら 1 日分（最大 31 日分）。
            どちらも無い時は日本時間の当日から遡り、limit 件に達するか max_days 日分を取得するまで続ける（旧 API の「直近 N 件」に相当）。
    """
    if date_from or date_to:
        start = datetime.strptime(date_from or date_to, "%Y%m%d").date()
        end = datetime.strptime(date_to or date_from, "%Y%m%d").date()
        if end < start:
            start, end = end, start
        n_days = min((end - start).days + 1, 31)
    else:
        end = datetime.now(_JST).date()
        n_days = max(1, max_days)
    out: list[dict[str, Any]] = []
    for k in range(n_days):
        ymd = (end - timedelta(days=k)).strftime("%Y%m%d")
        items = _news_of_day(master_url, ymd, str(next_no()))
        items.sort(key=lambda n: (str(n.get("p_TM") or ""), str(n.get("p_ID") or "")), reverse=True)
        for n in items:
            if category and category not in str(n.get("p_CGL") or "").split("|"):
                continue
            if issue_code and issue_code not in str(n.get("p_ISL") or "").split("|"):
                continue
            out.append(n)
        if len(out) >= offset + limit:
            break
    out = out[offset:offset + limit]
    for n in out:
        n["_decoded_title"] = _decode_headline(n.get("p_HDL", ""))
    return out


@dataclass
class TachibanaClient:
    """立花証券 e支店 API ライトクライアント。"""

    auth_id: str
    private_key_path: Path
    api_base: str
    _private_key: Any = field(default=None, init=False, repr=False)
    _request_url: str | None = field(default=None, init=False)
    _master_url: str | None = field(default=None, init=False)
    _price_url: str | None = field(default=None, init=False)
    _request_no: int = field(default=1, init=False)

    @classmethod
    def from_env(cls) -> "TachibanaClient":
        """環境変数 TACHIBANA_DEMO_* または TACHIBANA_PROD_* から構築。

        秘密鍵の供給方法は 2 通り（GHA Secrets / ローカル .env のどちらでも動作）：
        1. TACHIBANA_*_PRIVATE_KEY_PEM: PEM 文字列を直接環境変数で渡す（GHA Secrets 向け）
        2. TACHIBANA_*_PRIVATE_KEY_PATH: PEM ファイルパス（ローカル開発向け）
        """
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        for prefix in ("TACHIBANA_PROD_", "TACHIBANA_DEMO_"):
            auth_id = os.getenv(f"{prefix}AUTH_ID")
            api_base = os.getenv(f"{prefix}API_BASE")
            key_pem = os.getenv(f"{prefix}PRIVATE_KEY_PEM")
            key_path = os.getenv(f"{prefix}PRIVATE_KEY_PATH")
            if auth_id and api_base and (key_pem or key_path):
                repo_root = Path(__file__).resolve().parent.parent.parent.parent
                inst = cls.__new__(cls)
                inst.auth_id = auth_id
                inst.api_base = normalize_api_base(api_base)
                inst._request_url = None
                inst._master_url = None
                inst._price_url = None
                inst._request_no = 1
                if key_pem:
                    # PEM 文字列を直接読込（GHA Secrets 経由）
                    inst.private_key_path = Path("__from_env_pem__")
                    inst._private_key = serialization.load_pem_private_key(key_pem.encode("utf-8"), password=None)
                else:
                    key_abs = repo_root / key_path if not Path(key_path).is_absolute() else Path(key_path)
                    inst.private_key_path = key_abs
                    with open(key_abs, "rb") as f:
                        inst._private_key = serialization.load_pem_private_key(f.read(), password=None)
                return inst
        raise RuntimeError(
            "立花証券認証情報が設定されていません。"
            "TACHIBANA_DEMO_AUTH_ID + TACHIBANA_DEMO_API_BASE + "
            "(TACHIBANA_DEMO_PRIVATE_KEY_PEM または TACHIBANA_DEMO_PRIVATE_KEY_PATH) を設定してください。"
        )

    def __post_init__(self) -> None:
        self.api_base = normalize_api_base(self.api_base)
        # from_env 経由（__new__ ベース）の場合は _private_key 設定済でスキップ
        if self._private_key is not None:
            return
        with open(self.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _next_no(self) -> str:
        self._request_no += 1
        return str(self._request_no)

    def login(self) -> dict[str, Any]:
        """ログイン → 仮想URL を取得・復号化してインスタンス変数に保存。

        HTTP 状態・応答の結果コード（p_errno・sResultCode）を検査し、失敗時は
        TachibanaApiError（文言 `login failed: ...`・URL と認証 ID を含まない）を送出する。
        """
        payload = {
            "p_no": "1",
            "p_sd_date": _now_str(),
            "sCLMID": "CLMAuthLoginRequest",
            "sJsonOfmt": "4",
            "sAuthId": self.auth_id,
        }
        url = f"{self.api_base}/auth/?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
        resp = _get_json(url, 20, "login")
        if resp.get("p_errno") != "0" or resp.get("sResultCode") != "0":
            raise TachibanaApiError(
                f"login failed: errno={resp.get('p_errno')} err={resp.get('p_err')} "
                f"result={resp.get('sResultCode')} text={resp.get('sResultText')}")
        oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        try:
            self._request_url = self._private_key.decrypt(base64.b64decode(resp["sUrlRequest"]), oaep).decode("utf-8")
            self._master_url = self._private_key.decrypt(base64.b64decode(resp["sUrlMaster"]), oaep).decode("utf-8")
            self._price_url = self._private_key.decrypt(base64.b64decode(resp["sUrlPrice"]), oaep).decode("utf-8")
        except Exception as e:  # 鍵の不一致・応答の欠け
            raise TachibanaApiError(f"login failed: 仮想 URL の復号に失敗（{type(e).__name__}）") from None
        return resp

    def _ensure_logged_in(self) -> None:
        if not self._master_url:
            self.login()

    def get_news_head(
        self,
        limit: int = 100,
        offset: int = 0,
        category: str | None = None,
        issue_code: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        max_days: int = 7,
    ) -> list[dict[str, Any]]:
        """ニュースを新しい順に返す（中身は fetch_news。引数の意味も同じ）。"""
        self._ensure_logged_in()
        return fetch_news(self._master_url, self._next_no, limit=limit, offset=offset,
                          category=category, issue_code=issue_code,
                          date_from=date_from, date_to=date_to, max_days=max_days)

    def get_news_body(self, news_id: str) -> str:
        """ニュース本文取得（ヘッダーの p_ID で指定）。Shift-JIS デコード済テキストを返す。"""
        self._ensure_logged_in()
        payload = {
            "p_no": self._next_no(),
            "p_sd_date": _now_str(),
            "sCLMID": "CLMMfdsGetNewsBody",
            "sJsonOfmt": "4",
            "p_ID": news_id,
        }
        url = f"{self._master_url}?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
        data = _get_json(url, 30, "news_body")
        body_b64 = data.get("p_BODY", "")
        if not body_b64:
            return ""
        return _decode_headline(body_b64)

    def get_credit_margin(self, issue_codes: list[str]) -> list[dict[str, Any]]:
        """信用残情報を一括取得。最大120銘柄。"""
        self._ensure_logged_in()
        if len(issue_codes) > 120:
            issue_codes = issue_codes[:120]
        payload = {
            "p_no": self._next_no(),
            "p_sd_date": _now_str(),
            "sCLMID": "CLMMfdsGetShinyouZan",
            "sJsonOfmt": "4",
            "sTargetIssueCode": ",".join(issue_codes),
        }
        url = f"{self._master_url}?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
        data = _get_json(url, 30, "credit_margin")
        return data.get("aCLMMfdsShinyouZan", [])

    def get_securities_finance(self, issue_codes: list[str]) -> list[dict[str, Any]]:
        """証金残情報を一括取得。最大120銘柄。"""
        self._ensure_logged_in()
        if len(issue_codes) > 120:
            issue_codes = issue_codes[:120]
        payload = {
            "p_no": self._next_no(),
            "p_sd_date": _now_str(),
            "sCLMID": "CLMMfdsGetSyoukinZan",
            "sJsonOfmt": "4",
            "sTargetIssueCode": ",".join(issue_codes),
        }
        url = f"{self._master_url}?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
        data = _get_json(url, 30, "securities_finance")
        return data.get("aCLMMfdsSyoukinZan", [])

    def get_short_borrowing_cost(self, issue_codes: list[str]) -> list[dict[str, Any]]:
        """逆日歩情報を一括取得。最大120銘柄。"""
        self._ensure_logged_in()
        if len(issue_codes) > 120:
            issue_codes = issue_codes[:120]
        payload = {
            "p_no": self._next_no(),
            "p_sd_date": _now_str(),
            "sCLMID": "CLMMfdsGetHibuInfo",
            "sJsonOfmt": "4",
            "sTargetIssueCode": ",".join(issue_codes),
        }
        url = f"{self._master_url}?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
        data = _get_json(url, 30, "short_borrowing_cost")
        return data.get("aCLMMfdsHibuInfo", [])

    def get_issue_detail(self, issue_codes: list[str]) -> list[dict[str, Any]]:
        """銘柄詳細情報を一括取得。最大120銘柄。"""
        self._ensure_logged_in()
        if len(issue_codes) > 120:
            issue_codes = issue_codes[:120]
        payload = {
            "p_no": self._next_no(),
            "p_sd_date": _now_str(),
            "sCLMID": "CLMMfdsGetIssueDetail",
            "sJsonOfmt": "4",
            "sTargetIssueCode": ",".join(issue_codes),
        }
        url = f"{self._master_url}?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
        data = _get_json(url, 30, "issue_detail")
        return data.get("aCLMMfdsIssueDetail", [])


# カテゴリ・ジャンルラベル（人間可読・レポート整形用）
CGL_LABEL: dict[str, str] = {
    "100": "QUICK NQN（市況速報）",
    "110": "AI 市況（ボード）",
    "120": "QUICK ニュース",
    "129": "TDNet/EDINET AI 速報",
}

GNL_LABEL: dict[str, str] = {
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
