"""Thin REST client for the MyFundedPerps v1 API.

Retries 429/500/503 and network errors with exponential backoff + jitter and
reuses the same Idempotency-Key, as the docs require.
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from urllib.parse import quote

import requests

log = logging.getLogger(__name__)
RETRY_STATUS = {429, 500, 503}


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str, body: dict | None = None):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.body = body or {}


class Client:
    def __init__(self, base_url: str, api_key: str, timeout: float = 15, max_retries: int = 5):
        self.base = base_url.rstrip("/")
        self.s = requests.Session()
        self.s.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        self.timeout = timeout
        self.max_retries = max_retries

    def _req(self, method: str, path: str, *, params=None, json=None, idem: str | None = None):
        headers = {"Idempotency-Key": idem} if idem else {}
        for attempt in range(self.max_retries + 1):
            try:
                r = self.s.request(method, self.base + path, params=params, json=json,
                                   headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise
                wait = min(30, 2 ** attempt) + random.random()
                log.warning("netwerkfout %s %s (%s), retry over %.1fs", method, path, e, wait)
                time.sleep(wait)
                continue
            if r.status_code in RETRY_STATUS and attempt < self.max_retries:
                wait = float(r.headers.get("Retry-After") or min(30, 2 ** attempt)) + random.random()
                log.warning("%s op %s %s, retry over %.1fs", r.status_code, method, path, wait)
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                    err = body.get("error", {})
                except ValueError:
                    body, err = {}, {}
                raise ApiError(r.status_code, err.get("code", "unknown"), err.get("message", r.text[:200]), body)
            return r.json() if r.content else {}
        raise RuntimeError("unreachable")

    # ---- reads
    def accounts(self) -> list[dict]:
        return self._req("GET", "/v1/accounts")["data"]

    def account(self, account_id: str) -> dict:
        return self._req("GET", f"/v1/accounts/{account_id}")["data"]

    def trading_policy(self, account_id: str) -> dict:
        return self._req("GET", f"/v1/accounts/{account_id}/trading-policy")["data"]

    def markets(self) -> list[dict]:
        return self._req("GET", "/v1/markets")["data"]

    def positions(self, account_id: str) -> list[dict]:
        return self._req("GET", "/v1/positions", params={"account_id": account_id})["data"]

    def closed_positions(self, account_id: str, limit: int = 50) -> list[dict]:
        """Most recent closed positions, following cursors until `limit` are collected."""
        out, cursor = [], None
        while len(out) < limit:
            params = {"account_id": account_id, "status": "closed", "limit": min(100, limit - len(out))}
            if cursor:
                params["cursor"] = cursor
            page = self._req("GET", "/v1/positions", params=params)
            out += page["data"]
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor:
                break
        return out

    def order(self, order_id: str) -> dict:
        return self._req("GET", f"/v1/orders/{order_id}")["data"]

    def quote(self, market_id: str, side: str, size: float) -> dict:
        return self._req("GET", f"/v1/markets/{quote(market_id, safe='')}/quote",
                         params={"side": side, "size": size})["data"]

    # ---- writes
    def place_order(self, body: dict, idem: str | None = None) -> dict:
        return self._req("POST", "/v1/orders", json=body, idem=idem or str(uuid.uuid4()))["data"]

    def close_position(self, position_id: str, size: float | None = None,
                       client_order_id: str | None = None) -> dict:
        """Reduce-only market close; omit size for the full position."""
        body: dict = {"type": "market"}
        if size is not None:
            body["size"] = size
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._req("POST", f"/v1/positions/{position_id}/close", json=body,
                         idem=client_order_id or str(uuid.uuid4()))["data"]

    def cancel_order(self, order_id: str) -> dict:
        return self._req("DELETE", f"/v1/orders/{order_id}")["data"]

    def modify_order(self, order_id: str, price: float, size: float | None = None) -> dict:
        body: dict = {"price": price}
        if size is not None:
            body["size"] = size
        return self._req("PATCH", f"/v1/orders/{order_id}", json=body)["data"]

    def close_all(self, account_id: str) -> dict:
        return self._req("POST", f"/v1/accounts/{account_id}/close-all-positions", json={},
                         idem=str(uuid.uuid4()))

    def cancel_all(self, account_id: str) -> dict:
        return self._req("POST", f"/v1/accounts/{account_id}/cancel-all-orders", json={},
                         idem=str(uuid.uuid4()))

    def wait_order(self, order_id: str, timeout: float = 20) -> dict:
        """Poll a pending order until it settles (filled / rejected / resting / ...)."""
        deadline = time.time() + timeout
        delay = 0.5
        while True:
            o = self.order(order_id)
            if o["status"] != "pending" or time.time() > deadline:
                return o
            time.sleep(delay)
            delay = min(delay * 1.6, 3)
