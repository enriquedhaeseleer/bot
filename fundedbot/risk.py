"""Risk manager: the same guards that were simulated in research/sim.py.

Pure functions over API snapshots, so they are easy to unit-test. All loss
limits are read from the server's own floors (daily_loss_floor,
max_drawdown_floor); our guards sit *inside* those floors with a buffer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .config import RiskParams

# Plan defaults used only when the API does not report a percentage.
DEFAULT_DLL = 0.03
DEFAULT_MAX_DD = 0.06


def _frac(pct, default):
    """API percentages may come as 3 or 0.03; normalise to a fraction."""
    if pct is None:
        return default
    pct = float(pct)
    if pct == 0:
        return 0.0  # a zero percentage disables that rule
    return pct / 100 if pct > 1 else pct


@dataclass
class RiskState:
    equity: float | None
    start: float
    guard_level: float          # flatten when equity falls to this level
    headroom: float             # equity - guard_level
    open_risk: float            # sum of stop-loss risk of open legs
    risk_per_trade: float       # fraction of start risked per leg in the current stage
    must_flatten: bool
    stuck: bool                 # too close to the max-loss floor to keep trading
    target_reached: bool
    blockers: list[str] = field(default_factory=list)  # reasons not to open trades
    payout_ready: bool = False

    @property
    def can_open(self) -> bool:
        return not self.blockers and not self.must_flatten and not self.stuck and not self.target_reached


def assess(account: dict, policy: dict | None, n_positions: int, open_risk: float, risk_per_trade: float,
           rp: RiskParams, trades_today: int, locked_today: bool) -> RiskState:
    """Evaluate the account against our guards.

    n_positions: open strategy legs (or exchange positions); open_risk: sum over legs of
    size * |entry - stop|, the same (conservative) measure the simulator uses.
    """
    start = float(account["starting_balance"])
    risk = account.get("risk") or {}
    eq = risk.get("equity")
    req = risk.get("requirements") or {}
    dll = _frac(req.get("daily_loss_pct"), DEFAULT_DLL)
    max_dd = _frac(req.get("max_drawdown_pct"), DEFAULT_MAX_DD)
    blockers: list[str] = []

    if account.get("status") != "active":
        blockers.append(f"account status {account.get('status')}")
    if eq is None or not risk.get("marks_complete", False):
        blockers.append("risk snapshot incompleet (geen verse marks)")

    guards = []
    if risk.get("daily_loss_floor") is not None and dll > 0:
        guards.append(float(risk["daily_loss_floor"]) + (1 - rp.daily_guard) * dll * start)
    if risk.get("max_drawdown_floor") is not None:
        guards.append(float(risk["max_drawdown_floor"]) + (1 - rp.dd_guard) * max_dd * start)
    guard_level = max(guards) if guards else -math.inf
    dd_guard_level = guards[-1] if risk.get("max_drawdown_floor") is not None else -math.inf

    risk_usd = risk_per_trade * start

    headroom = (eq - guard_level) if eq is not None else -math.inf
    must_flatten = n_positions > 0 and eq is not None and eq <= guard_level
    stuck = (n_positions == 0 and eq is not None
             and (eq - dd_guard_level) / 2 < rp.min_risk_fraction * risk_usd)

    stage = account.get("stage")
    rpe = risk.get("remaining_profit_equity")
    target_reached = stage == "evaluation" and rpe is not None and rpe <= 0

    if locked_today:
        blockers.append("daily guard geraakt, pauze tot middernacht New York")
    if trades_today >= rp.max_trades_per_day:
        blockers.append(f"max {rp.max_trades_per_day} trades per dag bereikt")
    if policy:
        for flag in ("manual_trading_blocked", "payout_pending", "opening_exposure_restricted",
                     "copy_follower_locked", "copy_scope_blocked"):
            if policy.get(flag):
                blockers.append(flag)
        if policy.get("restriction"):
            blockers.append(f"restrictie: {policy['restriction'].get('restriction')}")
        halt = policy.get("trading_halt") or {}
        if halt.get("platform"):
            blockers.append("platform trading halt")

    # funded: our payout policy flattens at +payout_target and asks for a withdrawal
    payout_ready = stage == "funded" and eq is not None and eq >= start * (1 + rp.payout_target)

    return RiskState(eq, start, guard_level, headroom, open_risk, risk_per_trade, must_flatten, stuck,
                     target_reached, blockers, payout_ready)


@dataclass
class Sizing:
    size: float
    risk_usd: float
    leverage: float
    reason: str = ""


def size_trade(rs: RiskState, rp: RiskParams, market: dict, price: float, stop_dist: float,
               open_notional: float, n_positions: int, max_positions: int, policy: dict | None,
               halted_categories: list[str] | None = None) -> Sizing:
    """Position size for a new trade, or size 0 with the reason it was skipped."""
    def skip(why):
        return Sizing(0.0, 0.0, 0.0, why)

    if not rs.can_open:
        return skip("; ".join(rs.blockers) or "guard actief")
    if market.get("trading_enabled") is False or market.get("reduce_only"):
        return skip("market niet open voor nieuwe posities")
    if halted_categories and market.get("category") in halted_categories:
        return skip(f"categorie {market.get('category')} gehalt")
    if n_positions >= max_positions:
        return skip("max posities")
    if stop_dist <= 0 or price <= 0:
        return skip("ongeldige stop")

    base_risk = rs.risk_per_trade * rs.start
    risk_eff = min(base_risk, (rs.headroom - rs.open_risk) / 2)
    if risk_eff < rp.min_risk_fraction * base_risk:
        return skip("te weinig ruimte tot de guard")

    size = risk_eff / stop_dist
    max_notional = rp.max_total_leverage * rs.start - open_notional
    lim = (policy or {}).get("limits") or {}
    if lim.get("max_position_value_usd"):
        max_notional = min(max_notional, float(lim["max_position_value_usd"]))
    if size * price > max_notional:
        size = max(0.0, max_notional / price)

    step = float(market["size_step"])
    size = math.floor(size / step + 1e-9) * step
    size = round(size, int(market.get("size_decimals", 8)))
    min_notional = max(float(market.get("min_notional", 0)), float(lim.get("min_order_notional_usd") or 0))
    if size < float(market["min_size"]) or size * price < min_notional:
        return skip("positie te klein voor market-minimum")

    leverage = min(rp.order_leverage, float(market.get("max_leverage", rp.order_leverage)))
    return Sizing(size, size * stop_dist, leverage)
