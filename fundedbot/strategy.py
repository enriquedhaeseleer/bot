"""Leg signals on closed candles: the same formulas as research/strategies.py and research/fast_ideas.py.

Each function returns full-length arrays (entry +1/-1/0, stop distance, take-profit distance,
trailing distance, exit flag); the bot only reads the last closed candle. tests/test_strategy_parity.py checks
bar for bar that a 1000-candle window gives the same signals as the backtest.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .config import Leg

MIN_BARS = {"ema_cross": 600, "keltner": 100, "donchian": 150, "level_break": 400, "supertrend": 200,
            "smc_structure": 200, "wide_bar": 50}


@dataclass
class LegSignal:
    side: int                # +1 long, -1 short, 0 none
    candle_time: pd.Timestamp
    price: float             # close of the signal candle
    stop_dist: float
    tp_dist: float | None    # None: no fixed target
    trail_dist: float | None  # None: no trailing stop
    exit: bool = False       # strategy wants this leg closed at this candle's close


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _trend(df, filt):
    """+1 where only longs are allowed, -1 only shorts, 0 both (filt=0 disables)."""
    if not filt:
        return pd.Series(0, index=df.index)
    return np.sign(df.close - ema(df.close, filt))


def ema_cross(df, fast=50, slow=200, filt=200, atr_n=14, sl=2.5, rr=2.5):
    ef, es, eF, a = ema(df.close, fast), ema(df.close, slow), ema(df.close, filt), atr(df, atr_n)
    up = (ef > es) & (ef.shift() <= es.shift()) & (df.close > eF)
    dn = (ef < es) & (ef.shift() >= es.shift()) & (df.close < eF)
    return up.astype(int) - dn.astype(int), sl * a, rr * sl * a, None, None


def keltner(df, n=20, k=2.0, sl=1.5, trail=3.0, filt=0, atr_n=14):
    m = ema(df.close, n)
    a = atr(df, atr_n)
    up = (df.close > m + k * a) & (df.close.shift() <= (m + k * a).shift())
    dn = (df.close < m - k * a) & (df.close.shift() >= (m - k * a).shift())
    tr = _trend(df, filt)
    entry = (up & (tr >= 0)).astype(int) - (dn & (tr <= 0)).astype(int)
    return entry, sl * a, None, trail * a, None


def supertrend(df, n=10, mult=3.0, sl=2.0, trail=4.0, filt=0, atr_n=14):
    """Enter on a Supertrend flip, trailing stop, and close the leg on the next flip."""
    a_st = atr(df, n).to_numpy()
    hl2 = ((df.high + df.low) / 2).to_numpy()
    c = df.close.to_numpy()
    up = hl2 - mult * a_st
    dn = hl2 + mult * a_st
    T = len(c)
    trend = np.ones(T)
    fu, fd = up.copy(), dn.copy()
    for t in range(1, T):
        fu[t] = max(up[t], fu[t - 1]) if c[t - 1] > fu[t - 1] else up[t]
        fd[t] = min(dn[t], fd[t - 1]) if c[t - 1] < fd[t - 1] else dn[t]
        trend[t] = 1 if c[t] > fd[t - 1] else (-1 if c[t] < fu[t - 1] else trend[t - 1])
    flip = np.r_[False, trend[1:] != trend[:-1]]
    tr = _trend(df, filt).to_numpy()
    entry = np.where(flip & ((tr == 0) | (tr == trend)), trend, 0)
    a = atr(df, atr_n)
    return entry, sl * a, None, trail * a, flip


def donchian(df, n=48, sl=2.0, trail=3.0, filt=0, atr_n=14):
    hi = df.high.rolling(n).max().shift()
    lo = df.low.rolling(n).min().shift()
    a = atr(df, atr_n)
    up = (df.close > hi) & (df.close.shift() <= hi.shift())
    dn = (df.close < lo) & (df.close.shift() >= lo.shift())
    if filt:  # only with the trend: above / below the EMA
        e = ema(df.close, filt)
        up &= df.close > e
        dn &= df.close < e
    return up.astype(int) - dn.astype(int), sl * a, None, trail * a, None


def _session_ids(index: pd.DatetimeIndex, kind: str) -> np.ndarray:
    if kind == "D":
        return np.asarray((index.floor("D") - pd.Timestamp("1970-01-01", tz="UTC")).days)
    if kind == "W":  # weeks start Monday 00:00 UTC
        return np.asarray((index.floor("D") - pd.Timestamp("1970-01-05", tz="UTC")).days // 7)
    raise ValueError(kind)


def level_break(df, kind="W", sl=1.5, rr=2.0, atr_n=14):
    """First close beyond the prior week's (or day's) high/low within the current session."""
    a = atr(df, atr_n)
    sess = _session_ids(df.index, kind)
    g = df.groupby(sess)
    ph = pd.Series(sess, index=df.index).map(g.high.max().shift()).to_numpy()
    pl = pd.Series(sess, index=df.index).map(g.low.min().shift()).to_numpy()
    c = df.close.to_numpy()
    above = pd.Series(c > ph, index=df.index)
    below = pd.Series(c < pl, index=df.index)
    prev = lambda x: x.groupby(sess).shift().fillna(False).astype(bool).groupby(sess).cummax()  # noqa: E731
    first_up = above & ~prev(above)
    first_dn = below & ~prev(below)
    entry = pd.Series(first_up.to_numpy().astype(int) - first_dn.to_numpy().astype(int), index=df.index)
    return entry, sl * a, rr * sl * a, None, None


def _pivots(df, L):
    """Pivot high/low of bar t-L, known at bar t (L bars on both sides)."""
    h, lo = df.high.to_numpy(), df.low.to_numpy()
    n = len(df)
    hmax = pd.Series(h).rolling(2 * L + 1).max().to_numpy()
    lmin = pd.Series(lo).rolling(2 * L + 1).min().to_numpy()
    ph = np.full(n, np.nan)
    pl = np.full(n, np.nan)
    for t in range(2 * L, n):
        if h[t - L] == hmax[t]:
            ph[t] = h[t - L]
        if lo[t - L] == lmin[t]:
            pl[t] = lo[t - L]
    return ph, pl


def smc_structure(df, L=10, mode="both", sl=1.5, trail=3.0, atr_n=14):
    """Market structure (Smart Money Concepts style): trade the first close beyond the last confirmed swing
    high/low. With the current structure trend that is a BOS, against it a CHoCH; mode picks which to trade."""
    ph, pl = _pivots(df, L)
    c = df.close.to_numpy()
    n = len(c)
    entry = np.zeros(n, int)
    hi = lo = np.nan
    hi_live = lo_live = False
    trend = 0
    for t in range(n):
        if not np.isnan(ph[t]):
            hi, hi_live = ph[t], True
        if not np.isnan(pl[t]):
            lo, lo_live = pl[t], True
        if hi_live and c[t] > hi:
            if mode in ("both", "choch" if trend == -1 else "bos"):
                entry[t] = 1
            trend, hi_live = 1, False
        elif lo_live and c[t] < lo:
            if mode in ("both", "choch" if trend == 1 else "bos"):
                entry[t] = -1
            trend, lo_live = -1, False
    a = atr(df, atr_n)
    return entry, sl * a, None, trail * a, None


def wide_bar(df, k=2.0, close_frac=0.25, sl=2.0, trail=3.0, atr_n=14):
    """A candle wider than k * ATR (of the candles before it) that closes near its high (low): trade that way."""
    rng = df.high - df.low
    big = rng > k * atr(df, atr_n).shift()
    up = big & (df.high - df.close <= close_frac * rng) & (df.close > df.open)
    dn = big & (df.close - df.low <= close_frac * rng) & (df.close < df.open)
    a = atr(df, atr_n)
    return up.astype(int) - dn.astype(int), sl * a, None, trail * a, None


KINDS = {"ema_cross": ema_cross, "keltner": keltner, "donchian": donchian, "level_break": level_break,
         "supertrend": supertrend, "smc_structure": smc_structure, "wide_bar": wide_bar}


def leg_series(df: pd.DataFrame, lg: Leg):
    """Full arrays (entry, stop, tp, trail, exit) for a leg on its own candles."""
    entry, stop, tp, trail, exit_ = KINDS[lg.kind](df, **lg.p)
    as_arr = lambda x: None if x is None else np.asarray(x, dtype=float)  # noqa: E731
    ex = np.zeros(len(df), bool) if exit_ is None else np.asarray(exit_, dtype=bool)
    return np.asarray(entry, dtype=int), as_arr(stop), as_arr(tp), as_arr(trail), ex


def latest(df: pd.DataFrame, lg: Leg) -> LegSignal | None:
    """Signal and trailing distance on the LAST closed candle (None with too little history)."""
    if len(df) < MIN_BARS[lg.kind]:
        return None
    entry, stop, tp, trail, ex = leg_series(df, lg)
    stop_d = float(stop[-1])
    if not np.isfinite(stop_d) or stop_d <= 0:
        return None
    return LegSignal(int(entry[-1]), df.index[-1], float(df.close.iloc[-1]), stop_d,
                     None if tp is None else float(tp[-1]), None if trail is None else float(trail[-1]),
                     bool(ex[-1]))
