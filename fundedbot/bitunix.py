"""Bitunix futures (own money) behind the same interface as the MyFundedPerps client, so the bot, the risk
manager and the dashboard run unchanged.

Differences that are translated here:
  * Bitunix holds one stop for the whole position (a "position TP/SL order": it closes the position at
    market, at whatever size the position has then). The bot's exit-orders calls place / move that stop.
  * An entry with stop_loss_price is a plain market order; the position stop is set right after the fill
    (in wait_order). If that fails the bot's own protection step retries and closes everything on failure.
  * There are no prop-firm floors: the account reports no daily or max-loss floor, and its starting balance
    is the current equity, so a trade risks a fixed share of what is in the account now.

Signing (https://www.bitunix.com/api-docs/futures/common/sign.html):
    digest = SHA256(nonce + timestamp + api_key + sorted query "k1v1k2v2" + compact JSON body)
    sign   = SHA256(digest + secret)
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import string
import time

import requests

from .api import ApiError, Client

log = logging.getLogger(__name__)
BASE = "https://fapi.bitunix.com"
SYMBOL = "BTCUSDT"
COIN = "USDT"
STOP_TYPE = "MARK_PRICE"  # mark price: a stop is not set off by a single wick on Bitunix' own book


def client_for(cfg):
    """The exchange client for these settings."""
    if getattr(cfg, "venue", "mfp") == "bitunix":
        return BitunixClient(cfg.api_key, cfg.api_secret)
    return Client(cfg.base_url, cfg.api_key)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def sign(api_key: str, secret: str, nonce: str, ts: str, query: dict, body: str) -> str:
    q = "".join(f"{k}{query[k]}" for k in sorted(query))
    return _sha(_sha(nonce + ts + api_key + q + body) + secret)


class BitunixClient:
    def __init__(self, api_key: str, api_secret: str, timeout: float = 15, max_retries: int = 4):
        self.key, self.secret = api_key, api_secret
        self.timeout, self.max_retries = timeout, max_retries
        self.s = requests.Session()
        self._mode: str | None = None       # ONE_WAY / HEDGE
        self._pending_sl: dict[str, float] = {}
        self._setup_done = False

    # ------------------------------------------------------------------ transport
    def _req(self, method: str, path: str, params: dict | None = None, body: dict | None = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        raw = json.dumps(body, separators=(",", ":")) if body is not None else ""
        for attempt in range(self.max_retries + 1):
            nonce = "".join(random.choices(string.ascii_letters + string.digits, k=32))
            ts = str(int(time.time() * 1000))
            headers = {"api-key": self.key, "nonce": nonce, "timestamp": ts, "language": "en-US",
                       "sign": sign(self.key, self.secret, nonce, ts, params, raw),
                       "Content-Type": "application/json"}
            try:
                r = self.s.request(method, BASE + path, params=params or None, data=raw or None,
                                   headers=headers, timeout=self.timeout)
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise
                log.warning("netwerkfout %s %s (%s), opnieuw", method, path, e)
                time.sleep(min(20, 2 ** attempt))
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt < self.max_retries:
                time.sleep(min(20, 2 ** attempt))
                continue
            try:
                j = r.json()
            except ValueError:
                raise ApiError(r.status_code, "bad_response", r.text[:200])
            if r.status_code >= 400 or j.get("code") not in (0, "0"):
                raise ApiError(r.status_code if r.status_code >= 400 else 400, str(j.get("code")),
                               str(j.get("msg") or j)[:300], {"error": {"details": j}})
            return j.get("data")
        raise RuntimeError("unreachable")

    def _get(self, path, **params):
        return self._req("GET", path, params=params)

    def _post(self, path, body):
        return self._req("POST", path, body=body)

    # ------------------------------------------------------------------ setup
    def mode(self) -> str:
        if self._mode is None:
            self._mode = (self._get("/api/v1/futures/account/position_mode") or {}).get("positionMode", "ONE_WAY")
        return self._mode

    def setup(self, leverage: int = 5):
        """Cross margin and a fixed leverage once; the risk manager sizes every trade itself."""
        if self._setup_done:
            return
        for path, body in (("/api/v1/futures/account/change_margin_mode", {"marginMode": "CROSS"}),
                           ("/api/v1/futures/account/change_leverage", {"leverage": leverage})):
            try:
                self._post(path, {"symbol": SYMBOL, "marginCoin": COIN, **body})
            except ApiError as e:  # e.g. not allowed while a position is open; the current setting stays
                log.info("Bitunix %s niet aangepast: %s", path.rsplit("/", 1)[-1], e)
        self._setup_done = True

    # ------------------------------------------------------------------ reads (MyFundedPerps shapes)
    def _equity(self) -> tuple[float, float]:
        d = self._get("/api/v1/futures/account", marginCoin=COIN)
        a = d[0] if isinstance(d, list) else d
        f = lambda k: float(a.get(k) or 0)  # noqa: E731
        balance = f("available") + f("frozen") + f("margin")
        return balance, balance + f("crossUnrealizedPNL") + f("isolationUnrealizedPNL")

    def account(self, account_id=None) -> dict:
        balance, equity = self._equity()
        return {"id": "bitunix", "name": "Bitunix BTC", "account_number": "Bitunix", "stage": "own",
                "status": "active", "starting_balance": equity, "balance": balance,
                "risk": {"equity": equity, "marks_complete": True, "daily_loss_floor": None,
                         "max_drawdown_floor": None, "remaining_profit_equity": None,
                         "requirements": {"daily_loss_pct": 0, "max_drawdown_pct": 0, "profit_target_pct": 0}}}

    def accounts(self) -> list[dict]:
        return [self.account()]

    def trading_policy(self, account_id=None) -> dict:
        pair = self._pair()
        closed = pair.get("symbolStatus", "OPEN") != "OPEN" or pair.get("isApiSupported") is False
        return {"trading_halt": {"platform": False, "categories": []}, "limits": {},
                "opening_exposure_restricted": closed}

    def _pair(self) -> dict:
        d = self._get("/api/v1/futures/market/trading_pairs", symbols=SYMBOL)
        return next(p for p in d if p["symbol"] == SYMBOL)

    def markets(self) -> list[dict]:
        p = self._pair()
        dec = int(p.get("basePrecision", 4))
        return [{"market_id": SYMBOL, "provider": "binance", "coin": SYMBOL, "min_size": float(p["minTradeVolume"]),
                 "size_step": 10 ** -dec, "size_decimals": dec, "price_decimals": int(p.get("quotePrecision", 1)),
                 "max_leverage": float(p.get("maxLeverage", 100)), "min_notional": 0}]

    def quote(self, market_id, side, size) -> dict:
        t = self._get("/api/v1/futures/market/tickers", symbols=SYMBOL)
        px = float(next(x for x in t if x["symbol"] == SYMBOL)["lastPrice"])
        return {"mid": px, "estimated_fill_price": px}

    def positions(self, account_id=None) -> list[dict]:
        out = []
        for p in self._get("/api/v1/futures/position/get_pending_positions", symbol=SYMBOL) or []:
            if float(p.get("qty") or 0) > 0:
                out.append({"id": str(p["positionId"]), "market_id": SYMBOL, "status": "open",
                            "side": p["side"].lower(), "size": float(p["qty"]), "symbol": "BTC",
                            "entry_price": float(p.get("avgOpenPrice") or 0), "opened_at": int(p.get("ctime") or 0)})
        return out

    def closed_positions(self, account_id=None, n: int = 5) -> list[dict]:
        d = self._get("/api/v1/futures/position/get_history_positions", symbol=SYMBOL, limit=min(int(n), 100)) or {}
        out = []
        for p in d.get("positionList", []) if isinstance(d, dict) else d:
            out.append({"opened_at": int(p.get("ctime") or 0), "closed_at": int(p.get("mtime") or 0),
                        "side": str(p.get("side", "")).lower(), "size": float(p.get("maxQty") or 0), "symbol": "BTC",
                        "entry_price": float(p.get("entryPrice") or 0), "exit_price": float(p.get("closePrice") or 0),
                        "realized_pnl": float(p.get("realizedPNL") or 0), "fees": float(p.get("fee") or 0),
                        "funding": -float(p.get("funding") or 0), "close_reason": ""})
        return out

    def working_orders(self, account_id=None) -> list[dict]:
        """The position stop as an exit order of the whole position (its size follows the position)."""
        pos = {p["id"]: p["size"] for p in self.positions()}
        out = []
        for o in self._get("/api/v1/futures/tpsl/get_pending_orders", symbol=SYMBOL, limit=100) or []:
            if o.get("slPrice"):
                out.append({"id": str(o["id"]), "type": "stop_market", "bracket_role": "stop_loss",
                            "reduce_only": True, "market_id": SYMBOL, "trigger_price": float(o["slPrice"]),
                            "size": float(o.get("slQty") or pos.get(str(o.get("positionId")), 0)),
                            "position_id": str(o.get("positionId"))})
        return out

    # ------------------------------------------------------------------ orders
    def place_order(self, body: dict, idem: str | None = None) -> dict:
        """A market entry; the stop in the body is put on the position once the order has filled."""
        self.setup(int(body.get("leverage") or 5))
        req = {"symbol": SYMBOL, "qty": f"{float(body['size']):.8f}".rstrip("0").rstrip("."),
               "side": body["side"].upper(), "orderType": "MARKET"}
        if self.mode() == "HEDGE":
            req["tradeSide"] = "OPEN"
        if body.get("client_order_id"):
            req["clientId"] = body["client_order_id"][:32]
        oid = str(self._post("/api/v1/futures/trade/place_order", req)["orderId"])
        if body.get("stop_loss_price"):
            self._pending_sl[oid] = float(body["stop_loss_price"])
        return {"id": oid, "status": "pending"}

    def close_position(self, position_id: str, expected_price: float | None = None, size: float | None = None,
                       client_order_id: str | None = None) -> dict:
        pos = next((p for p in self.positions() if p["id"] == str(position_id)), None)
        if pos is None:
            raise ApiError(404, "position_not_found", "position not found")
        if size is None or size >= pos["size"] - 1e-12:
            self._post("/api/v1/futures/trade/flash_close_position", {"positionId": str(position_id)})
            return {"id": f"flash:{position_id}", "status": "pending"}
        req = {"symbol": SYMBOL, "qty": f"{float(size):.8f}".rstrip("0").rstrip("."), "orderType": "MARKET"}
        if self.mode() == "HEDGE":  # Bitunix: in hedge mode the side names the position being closed
            req |= {"side": "BUY" if pos["side"] == "long" else "SELL", "tradeSide": "CLOSE",
                    "positionId": str(position_id)}
        else:
            req |= {"side": "SELL" if pos["side"] == "long" else "BUY", "reduceOnly": True}
        if client_order_id:
            req["clientId"] = client_order_id[:32]
        return {"id": str(self._post("/api/v1/futures/trade/place_order", req)["orderId"]), "status": "pending"}

    def wait_order(self, order_id: str, timeout: float = 20.0) -> dict:
        t0 = time.time()
        if str(order_id).startswith("flash:"):
            pid = order_id.split(":", 1)[1]
            while time.time() - t0 < timeout:
                if not any(p["id"] == pid for p in self.positions()):
                    return {"id": order_id, "status": "filled"}
                time.sleep(0.5)
            return {"id": order_id, "status": "pending"}
        o = {}
        while time.time() - t0 < timeout:
            o = self._get("/api/v1/futures/trade/get_order_detail", orderId=order_id) or {}
            if o.get("status") in ("FILLED", "CANCELED"):
                break
            time.sleep(0.4)
        if o.get("status") != "FILLED":
            return {"id": order_id, "status": "canceled" if o.get("status") == "CANCELED" else "pending"}
        trades = (self._get("/api/v1/futures/trade/get_history_trades", symbol=SYMBOL, orderId=order_id, limit=100)
                  or {}).get("tradeList", [])
        qty = sum(float(t["qty"]) for t in trades) or float(o.get("tradeQty") or o.get("qty") or 0)
        px = (sum(float(t["qty"]) * float(t["price"]) for t in trades) / qty) if trades and qty else None
        out = {"id": order_id, "status": "filled", "filled_size": qty, "fill_price": px,
               "fee": sum(float(t.get("fee") or 0) for t in trades) or float(o.get("fee") or 0)}
        sl = self._pending_sl.pop(str(order_id), None)
        if sl is not None:
            try:
                out["stop_loss_order_id"] = self._set_position_stop(sl)
            except ApiError as e:  # the bot's protection step tries again and closes everything if it can't
                log.error("stop op de Bitunix-positie zetten mislukt: %s", e)
        return out

    def _set_position_stop(self, price: float) -> str | None:
        pos = self.positions()
        if not pos:
            return None
        pid = pos[0]["id"]
        cur = [o for o in self.working_orders() if o["position_id"] == pid]
        body = {"symbol": SYMBOL, "positionId": pid, "slPrice": f"{price:.1f}", "slStopType": STOP_TYPE}
        if cur:
            self._post("/api/v1/futures/tpsl/position/modify_order", body)
            return cur[0]["id"]
        return str(self._post("/api/v1/futures/tpsl/position/place_order", body)["orderId"])

    def update_exits(self, position_id: str, body: dict) -> dict:
        """The bot's exit-order operations on the one position stop: modify / place move it, cancel removes it."""
        for op in body.get("operations", []):
            if op["kind"] == "cancel":
                self._post("/api/v1/futures/tpsl/cancel_order", {"symbol": SYMBOL, "orderId": str(op["order_id"])})
            else:  # modify or place: the position has one stop, at this price
                req = {"symbol": SYMBOL, "positionId": str(position_id), "slPrice": f"{float(op['price']):.1f}",
                       "slStopType": STOP_TYPE}
                path = "modify_order" if op["kind"] == "modify" else "place_order"
                self._post(f"/api/v1/futures/tpsl/position/{path}", req)
        return {"id": position_id}

    def cancel_all(self, account_id=None):
        self._post("/api/v1/futures/trade/cancel_all_orders", {"symbol": SYMBOL})
        for o in self.working_orders():
            try:
                self._post("/api/v1/futures/tpsl/cancel_order", {"symbol": SYMBOL, "orderId": o["id"]})
            except ApiError:
                pass

    def close_all(self, account_id=None):
        self._post("/api/v1/futures/trade/close_all_position", {"symbol": SYMBOL})
