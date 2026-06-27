"""
EDINET DB クライアント（MCPプロトコル・キーローテーション対応）

EDINET DB は REST API を持たず、MCP over HTTP でのみアクセス可能。
環境変数 EDINETDB_API_KEYS にカンマ区切りでキーを列挙する。
呼び出しごとにランダムにキーを選択してレート制限を分散する。
（各キー 100コール/日・3,000コール/月）

使い方:
    from edinetdb_client import EdinetDBClient
    client = EdinetDBClient()
    company = client.get_company("E02174")
    financials = client.get_financials("E02174", years=5)
"""

from __future__ import annotations

import json
import os
import random
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

MCP_URL = "https://edinetdb.jp/mcp"
TIMEOUT  = 20
SLEEP    = 1.0  # リクエスト間スリープ（秒）


class EdinetDBClient:
    def __init__(self) -> None:
        raw = os.environ.get("EDINETDB_API_KEYS", "").strip()
        if not raw:
            raise ValueError("EDINETDB_API_KEYS が未設定です。.env に追記してください。")
        self._keys = [k.strip() for k in raw.split(",") if k.strip()]
        if not self._keys:
            raise ValueError("EDINETDB_API_KEYS にキーが1つも設定されていません。")
        self._call_id = 0
        # プロセス起動ごとに開始キーをランダム化（毎回 keys[0] 固定だと
        # そのキーが上限到達時に即 429 で死ぬため）
        self._key_idx = random.randint(0, len(self._keys) - 1)

    def _pick_key(self) -> str:
        key = self._keys[self._key_idx % len(self._keys)]
        self._key_idx += 1
        return key

    def _call(self, tool_name: str, arguments: dict | None = None) -> dict | list:
        """MCP over HTTP でツールを呼び出す。result の JSON を返す。
        429 (Too Many Requests)・5xx・ネットワークエラーは次のキーへ全キー試行までリトライする。
        4xx クライアントエラー (400/401/403/404 等) は即座に raise する。"""
        self._call_id += 1
        payload = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
            "id": self._call_id,
        }
        last_err: Exception | None = None
        for attempt in range(len(self._keys)):
            key = self._pick_key()
            headers = {
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
            }
            time.sleep(SLEEP)
            try:
                resp = requests.post(MCP_URL, headers=headers, json=payload, timeout=TIMEOUT)
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                # ネットワーク断・読み取りタイムアウトは次のキーで再試行
                last_err = e
                continue
            if resp.status_code == 429:
                last_err = requests.exceptions.HTTPError(
                    f"429 on key #{attempt + 1}/{len(self._keys)}", response=resp
                )
                continue
            if resp.status_code >= 500:
                # サーバ側エラーは一過性のことが多いため次のキーで再試行
                last_err = requests.exceptions.HTTPError(
                    f"{resp.status_code} on key #{attempt + 1}/{len(self._keys)}", response=resp
                )
                continue
            # 4xx クライアントエラー (認証・不正リクエスト等) は再試行せず即 raise
            resp.raise_for_status()
            rpc = resp.json()
            if "error" in rpc:
                raise RuntimeError(f"EDINET DB MCP error: {rpc['error']}")
            content = rpc.get("result", {}).get("content", [])
            if not content:
                return {}
            first = content[0]
            if not isinstance(first, dict) or "text" not in first:
                raise RuntimeError(
                    f"EDINET DB: 予期しない MCP レスポンス形式 (content[0]={first!r})"
                )
            try:
                return json.loads(first["text"])
            except (json.JSONDecodeError, TypeError) as e:
                raise RuntimeError(
                    f"EDINET DB: MCP レスポンスの JSON パースに失敗: {type(e).__name__}: {e}"
                ) from e
        raise RuntimeError(
            f"EDINET DB: 全{len(self._keys)}キーが 429／5xx／ネットワークエラー。上限到達の可能性あり"
        ) from last_err

    # ------------------------------------------------------------------ #
    #  公開メソッド
    # ------------------------------------------------------------------ #

    def search_companies(self, query: str, limit: int = 5) -> list[dict]:
        """企業名・証券コードで検索。"""
        data = self._call("search_companies", {"query": query, "limit": limit})
        return data.get("companies", []) if isinstance(data, dict) else data

    def get_company(self, edinet_code: str) -> dict:
        """企業基本情報 + 最新財務サマリー + TDNet決算短信。"""
        return self._call("get_company", {"edinet_code": edinet_code})

    def get_financials(self, edinet_code: str, years: int = 5) -> list[dict]:
        """最大10年分の財務時系列データ。list[dict] を返す。"""
        data = self._call("get_financials", {"edinet_code": edinet_code, "years": years})
        if isinstance(data, list):
            return data
        return data.get("data", data.get("financials", []))

    def get_text_blocks(self, edinet_code: str) -> dict:
        """有価証券報告書の定性情報（事業概要・リスク・MD&A等）。"""
        data = self._call("get_text_blocks", {"edinet_code": edinet_code})
        return data if isinstance(data, dict) else {}

    def get_earnings(self, edinet_code: str, limit: int = 8) -> list[dict]:
        """直近の決算短信（TDNet）。"""
        data = self._call("get_earnings", {"edinet_code": edinet_code, "limit": limit})
        if isinstance(data, list):
            return data
        return data.get("earnings", data.get("data", []))

    def get_shareholders(self, edinet_code: str) -> dict:
        """大量保有報告書（5%超の大株主）。"""
        data = self._call("get_shareholders", {"edinet_code": edinet_code})
        return data if isinstance(data, dict) else {}

    def get_analysis(self, edinet_code: str) -> dict:
        """AI分析（健全性スコア・業界比較）。"""
        data = self._call("get_analysis", {"edinet_code": edinet_code})
        return data if isinstance(data, dict) else {}

    def screen_companies(self, **kwargs) -> list[dict]:
        """定量スクリーニング。"""
        data = self._call("screen_companies", kwargs)
        return data.get("companies", []) if isinstance(data, dict) else data

    def code_to_edinet(self, code4: str) -> str | None:
        """証券コード4桁 → EDINETコード変換。見つからなければ None。
        secCode は 5桁（末尾0付き）で返ることがあるため、両方の形で比較する。
        部分一致で同じ4桁を含む他社（例：2160 検索で 22160/42160/92160 等）が
        混在し得るため、limit は広めに取る。"""
        results = self.search_companies(code4, limit=20)
        for c in results:
            sec = str(c.get("secCode", ""))
            sec_4 = sec[:4] if len(sec) == 5 else sec
            if sec == code4 or sec_4 == code4:
                return c.get("edinetCode")
        return None
