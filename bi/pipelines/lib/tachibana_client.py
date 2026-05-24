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
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _now_str() -> str:
    n = datetime.now()
    return n.strftime("%Y.%m.%d-%H:%M:%S.") + f"{n.microsecond // 1000:03d}"


def _decode_headline(b64: str) -> str:
    """ニュースヘッドライン文字列を復号（Base64 → URL エンコード → Shift-JIS）。"""
    try:
        url_str = base64.b64decode(b64).decode("ascii")
        return urllib.parse.unquote(url_str, encoding="shift_jis")
    except Exception as e:
        return f"[decode error: {e}]"


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
        """環境変数 TACHIBANA_DEMO_* または TACHIBANA_PROD_* から構築。"""
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parent.parent / ".env")
        # デモ優先・本番が設定されていればそちらを使う
        for prefix in ("TACHIBANA_PROD_", "TACHIBANA_DEMO_"):
            auth_id = os.getenv(f"{prefix}AUTH_ID")
            key_path = os.getenv(f"{prefix}PRIVATE_KEY_PATH")
            api_base = os.getenv(f"{prefix}API_BASE")
            if auth_id and key_path and api_base:
                repo_root = Path(__file__).resolve().parent.parent.parent.parent
                key_abs = repo_root / key_path if not Path(key_path).is_absolute() else Path(key_path)
                return cls(auth_id=auth_id, private_key_path=key_abs, api_base=api_base)
        raise RuntimeError("立花証券認証情報が .env に設定されていません（TACHIBANA_DEMO_* または TACHIBANA_PROD_*）")

    def __post_init__(self) -> None:
        with open(self.private_key_path, "rb") as f:
            self._private_key = serialization.load_pem_private_key(f.read(), password=None)

    def _next_no(self) -> str:
        self._request_no += 1
        return str(self._request_no)

    def login(self) -> dict[str, Any]:
        """ログイン → 仮想URL を取得・復号化してインスタンス変数に保存。"""
        payload = {
            "p_no": "1",
            "p_sd_date": _now_str(),
            "sCLMID": "CLMAuthLoginRequest",
            "sJsonOfmt": "4",
            "sAuthId": self.auth_id,
        }
        url = f"{self.api_base}/auth/?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
        r = requests.get(url, timeout=20)
        r.encoding = "shift_jis"
        resp = json.loads(r.text)
        if resp.get("p_errno") != "0" or resp.get("sResultCode") != "0":
            raise RuntimeError(f"login failed: errno={resp.get('p_errno')} err={resp.get('p_err')} text={resp.get('sResultText')}")
        oaep = padding.OAEP(mgf=padding.MGF1(hashes.SHA256()), algorithm=hashes.SHA256(), label=None)
        self._request_url = self._private_key.decrypt(base64.b64decode(resp["sUrlRequest"]), oaep).decode("utf-8")
        self._master_url = self._private_key.decrypt(base64.b64decode(resp["sUrlMaster"]), oaep).decode("utf-8")
        self._price_url = self._private_key.decrypt(base64.b64decode(resp["sUrlPrice"]), oaep).decode("utf-8")
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
    ) -> list[dict[str, Any]]:
        """ニュースヘッダー取得。100件/ページの上限あり・limit > 100 ならページング。

        引数:
            limit: 取得最大件数（複数ページに渡る）
            offset: 開始オフセット
            category: p_CG（カテゴリコード絞り込み）
            issue_code: p_IS（銘柄コード絞り込み）
            date_from / date_to: YYYYMMDD 形式
        """
        self._ensure_logged_in()
        out: list[dict[str, Any]] = []
        page_size = 100
        cur = offset
        remaining = limit
        while remaining > 0:
            take = min(page_size, remaining)
            payload: dict[str, str] = {
                "p_no": self._next_no(),
                "p_sd_date": _now_str(),
                "sCLMID": "CLMMfdsGetNewsHead",
                "sJsonOfmt": "4",
                "p_REC_OFST": str(cur),
                "p_REC_LIMT": str(take),
            }
            if category:
                payload["p_CG"] = category
            if issue_code:
                payload["p_IS"] = issue_code
            if date_from:
                payload["p_DT_FROM"] = date_from
            if date_to:
                payload["p_DT_TO"] = date_to
            url = f"{self._master_url}?{urllib.parse.quote(json.dumps(payload, ensure_ascii=False))}"
            r = requests.get(url, timeout=30)
            r.encoding = "shift_jis"
            data = json.loads(r.text)
            items = data.get("aCLMMfdsNewsHead", [])
            if not items:
                break
            for n in items:
                n["_decoded_title"] = _decode_headline(n.get("p_HDL", ""))
            out.extend(items)
            if len(items) < take:
                break
            cur += take
            remaining -= take
        return out

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
        r = requests.get(url, timeout=30)
        r.encoding = "shift_jis"
        data = json.loads(r.text)
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
        r = requests.get(url, timeout=30)
        r.encoding = "shift_jis"
        data = json.loads(r.text)
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
        r = requests.get(url, timeout=30)
        r.encoding = "shift_jis"
        data = json.loads(r.text)
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
        r = requests.get(url, timeout=30)
        r.encoding = "shift_jis"
        data = json.loads(r.text)
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
        r = requests.get(url, timeout=30)
        r.encoding = "shift_jis"
        data = json.loads(r.text)
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
