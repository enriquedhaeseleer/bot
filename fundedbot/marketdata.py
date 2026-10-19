"""Closed candles from the public MyFundedPerps market-data stream (no API key)."""
from __future__ import annotations

import asyncio
import json

import pandas as pd
import websockets

from .config import MARKET_STREAM_URL


async def _history(provider: str, symbol: str, intervals: list[str], limit: int) -> dict[str, list]:
    out: dict[str, list] = {}
    errors: dict[str, str] = {}
    async with websockets.connect(MARKET_STREAM_URL, open_timeout=15, max_size=2**24) as ws:
        # the server allows at most 4 concurrent history requests per connection
        for i in range(0, len(intervals), 4):
            ids = {}
            for k, iv in enumerate(intervals[i:i + 4]):
                rid = i + k + 1
                ids[rid] = iv
                await ws.send(json.dumps({"op": "req", "id": rid, "method": "candles.history",
                                          "payload": {"provider": provider, "symbol": symbol,
                                                      "interval": iv, "limit": limit}}))
            while ids:
                f = json.loads(await asyncio.wait_for(ws.recv(), 30))
                rid = f.get("id")
                if rid not in ids:
                    continue  # heartbeats or other frames
                iv = ids.pop(rid)
                if f["op"] == "res":
                    out[iv] = f["result"]
                else:
                    errors[iv] = str(f.get("error"))
    for iv, e in errors.items():
        out.setdefault(iv, [])
        out[f"_error_{iv}"] = e  # type: ignore[assignment]
    return out


def _frame(rows: list) -> pd.DataFrame:
    rows = [r for r in rows if r.get("isFinal", True)]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df.index = pd.to_datetime(df["openTime"], unit="ms", utc=True)
    df = df[["open", "high", "low", "close", "volume"]].sort_index()
    return df[~df.index.duplicated(keep="last")]


def resample(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    return df.resample(tf.replace("m", "min")).agg(agg).dropna()


def closed_candles(provider: str, symbol: str, intervals: list[str], limit: int = 1000) -> dict[str, pd.DataFrame]:
    """Closed candles per interval. A 2h or 4h series the stream cannot deliver is built from 1h candles."""
    multi = [iv for iv in intervals if iv in ("2h", "4h")]
    want = list(dict.fromkeys(intervals + (["1h"] if multi else [])))
    raw = asyncio.run(_history(provider, symbol, want, limit))
    frames = {iv: _frame(raw.get(iv, [])) for iv in want}
    for iv in multi:
        if frames[iv].empty and not frames["1h"].empty:
            h = frames["1h"]
            full = resample(h, iv)
            # only keep candles whose 1h candles are all closed
            last_ok = h.index[-1] + pd.Timedelta("1h")
            frames[iv] = full[full.index + pd.Timedelta(iv) <= last_ok]
    return {iv: frames[iv] for iv in intervals}
