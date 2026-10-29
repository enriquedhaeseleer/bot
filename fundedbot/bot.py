"""Main trading loop: BTC only, several strategy legs sharing the one BTC position on the exchange.

Mirrors research/sim.py (run_fast with no_hedge=True):
  * every 15m candle close: trail the stops of open legs, then let legs whose candle just closed enter;
  * each leg has its own stop / target; the bot closes that leg's size when its level is crossed;
  * the exchange holds one stop-loss for the whole position at the size-weighted average of the leg
    stops (attached to every entry, then kept up to date through the position's exit-orders endpoint),
    so a crash of this program can never lose more than all leg stops together;
  * no leg may open against the current position (the exchange nets one position per market).
Challenge stage: CHALLENGE_LEGS with challenge_risk. Funded: FUNDED_LEGS with funded_risk, and at
+payout_target everything is closed so you can request the payout on the website.
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path

import pandas as pd

from . import proc
from .api import ApiError, Client
from .bitunix import client_for
from .config import Settings
from .marketdata import closed_candles
from .risk import assess, size_trade
from .strategy import latest

log = logging.getLogger("fundedbot")
NY = "America/New_York"
BASE = "15m"


def td(tf: str) -> pd.Timedelta:
    return pd.Timedelta(tf.replace("m", "min"))


def ny_day() -> str:
    return str(pd.Timestamp.now(tz=NY).date())


def now_ms() -> int:
    return int(time.time() * 1000)


class State:
    """Persisted between restarts so we never re-trade a candle or lose track of the legs."""

    def __init__(self, path: Path):
        self.path = path
        d = json.loads(path.read_text()) if path.exists() else {}
        self.legs: dict[str, dict] = d.get("legs", {})
        self.last_bar_ms: int = d.get("last_bar_ms", 0)
        self.protect_id: str | None = d.get("protect_id")
        self.protect_px: float = d.get("protect_px", 0.0)
        self.protect_size: float = d.get("protect_size", 0.0)
        self.day: str = d.get("day", ny_day())
        self.trades_today: int = d.get("trades_today", 0)
        self.locked_day: str | None = d.get("locked_day")
        self.last_trade_ms: int | None = d.get("last_trade_ms")
        self.payout_reached: bool = d.get("payout_reached", False)
        self.notified: dict[str, str] = d.get("notified", {})
        self.done: bool = d.get("done", False)
        self.account_id: str | None = d.get("account_id")
        self.first_equity: float | None = d.get("first_equity")  # equity at the bot's first tick on this account
        self.seen: dict[str, int] = d.get("seen", {})  # leg -> open time (ms) of the last candle evaluated

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__ | {"path": None}, indent=1, default=str))
        tmp.replace(self.path)


class Bot:
    def __init__(self, cfg: Settings, client: Client | None = None):
        self.cfg = cfg
        self.api = client or client_for(cfg)
        self.state = State(cfg.state_file)
        self.account_id = cfg.account_id or self._pick_account()
        if self.state.account_id not in (None, self.account_id):
            log.info("Ander account dan in %s: de bot begint met een schone status.", cfg.state_file.name)
            cfg.state_file.unlink(missing_ok=True)
            self.state = State(cfg.state_file)
        self.state.account_id = self.account_id
        mk = [m for m in self.api.markets() if m["provider"] == cfg.provider and m["coin"] == cfg.symbol]
        if not mk:
            raise SystemExit(f"Market {cfg.provider}|{cfg.symbol} niet gevonden.")
        self.market = mk[0]
        self.mid = self.market["market_id"]
        self.step = float(self.market["size_step"])
        self.policy: dict | None = None
        self.policy_at = 0.0
        self.unknown_position = False
        self.legs_by_name = {lg.name: lg for lg in cfg.challenge_legs + cfg.funded_legs}
        if self.state.last_trade_ms is None:
            self.state.last_trade_ms = self._last_trade_from_api()
        env = "LIVE" if cfg.live else "SANDBOX"
        log.info("Bot gestart [%s%s] account=%s market=%s", env, " DRY-RUN" if cfg.dry_run else "",
                 self.account_id, self.mid)

    def _pick_account(self) -> str:
        accs = [a for a in self.api.accounts() if a["status"] == "active"]
        if not accs:
            raise SystemExit("Geen actief account gevonden voor deze key.")
        if len(accs) > 1:
            names = ", ".join(f"{a['id']} ({a['name']}, {a['starting_balance']:.0f})" for a in accs)
            raise SystemExit(f"Meerdere actieve accounts: {names}. Zet FP_ACCOUNT_ID in .env.")
        return accs[0]["id"]

    def _last_trade_from_api(self) -> int:
        try:
            times = [int(p["opened_at"]) for p in self.api.positions(self.account_id)]
            times += [int(p.get("closed_at") or p["opened_at"]) for p in self.api.closed_positions(self.account_id, 5)]
        except (ApiError, KeyError, AttributeError):
            times = []
        return max(times) if times else now_ms()

    def _once(self, key: str, every: str | None = None) -> bool:
        """True the first time `key` is seen today (for log lines that would otherwise repeat)."""
        tag = every or ny_day()
        if self.state.notified.get(key) == tag:
            return False
        self.state.notified[key] = tag
        return True

    # ------------------------------------------------------------------ loop
    @property
    def mode(self) -> str:
        if self.cfg.dry_run:
            return "dry-run"
        if self.cfg.venue == "bitunix":
            return "eigen"
        return "live" if self.cfg.live else "sandbox"

    def run(self):
        """Tick until the challenge is done or bot.stop appears next to the state file."""
        folder = self.cfg.state_file.resolve().parent
        other = proc.read_pid(folder)
        if other and other.get("pid") != os.getpid() and proc.pid_alive(other.get("pid")):
            log.error("Er draait al een bot (pid %s, %s). Stop die eerst.", other["pid"], other.get("mode"))
            raise SystemExit(0)  # not a crash: a service manager must not keep retrying
        stop_flag = folder / "bot.stop"
        stop_flag.unlink(missing_ok=True)
        proc.write_pid(folder, self.mode, self.cfg.state_file)
        proc.keep_awake(True)
        log.info("Bot draait (%s). Hij houdt de pc wakker en stopt pas als je hem stopt.", self.mode)
        try:
            while not self.state.done:
                if stop_flag.exists():
                    stop_flag.unlink(missing_ok=True)
                    log.info("Stopverzoek ontvangen. Open posities blijven staan met hun stop op de server.")
                    break
                try:
                    self.tick()
                except ApiError as e:
                    log.error("API-fout: %s", e)
                    if e.status in (401, 403):
                        raise SystemExit(f"Authenticatie/toegang geweigerd: {e}")
                except Exception:
                    log.exception("onverwachte fout in tick")
                time.sleep(self.cfg.poll_seconds)
        finally:
            proc.keep_awake(False)
            proc.clear_pid(folder)
        log.info("Bot gestopt.")

    def tick(self):
        st, rp = self.state, self.cfg.risk
        today = ny_day()
        if st.day != today:
            log.info("Nieuwe handelsdag (NY): %s", today)
            st.day, st.trades_today = today, 0

        account = self.api.account(self.account_id)
        if account.get("status") != "active":
            log.warning("Account status is '%s'. De bot stopt. Start hem opnieuw voor je volgende account.",
                        account.get("status"))
            st.done = True
            st.save()
            return
        if time.time() - self.policy_at > 300:
            self.policy = self.api.trading_policy(self.account_id)
            self.policy_at = time.time()

        pos = self._position()
        if not self.cfg.dry_run:  # in a dry run the legs are virtual and the exchange stays flat
            self._reconcile(pos)
        funded = account.get("stage") == "funded"
        legs_cfg = self.cfg.funded_legs if funded else self.cfg.challenge_legs
        risk = rp.funded_risk if funded else rp.challenge_risk
        rs = assess(account, self.policy, len(st.legs), self._open_risk(), risk, rp,
                    st.trades_today, st.locked_day == today)
        if st.first_equity is None and rs.equity is not None:
            st.first_equity = rs.equity
        if self.unknown_position:
            rs.blockers.append("onbekende BTC-positie (niet van de bot)")

        if rs.must_flatten:
            log.warning("GUARD: equity %.2f <= guard %.2f -> alles sluiten en pauze tot middernacht NY",
                        rs.equity, rs.guard_level)
            self._flatten()
            st.locked_day = today
            st.save()
            return

        if rs.target_reached:
            log.info("CHALLENGE GEHAALD (equity %.2f). Posities sluiten; de bot stopt. "
                     "Start hem opnieuw zodra je funded account actief is.", rs.equity)
            self._flatten()
            st.done = True
            st.save()
            return

        if funded and (rs.payout_ready or st.payout_reached):
            if not st.payout_reached:
                self._flatten()
                st.payout_reached = True
                log.info("UITBETALING: +%.0f%% gehaald (equity %.2f). Alles is gesloten. Vraag nu op de website "
                         "een uitbetaling aan van alles boven $%.0f. De bot opent op dit account niets meer.",
                         rp.payout_target * 100, rs.equity, rs.start)
            elif self._once("payout"):
                log.info("Herinnering: vraag je uitbetaling aan op de website. Deze bot handelt niet meer op dit account.")
            st.save()
            return

        if rs.stuck and self._once("stuck"):
            log.warning("Te dicht bij de max-loss floor om verder te traden (equity %.2f).", rs.equity)

        if st.legs:
            self._manage_exits()
        if self._bar_due():
            self._on_bar(rs, legs_cfg)
        self._keepalive(rs, funded)
        st.save()

    # ------------------------------------------------------------------ state of the exchange
    def _position(self) -> dict | None:
        for p in self.api.positions(self.account_id):
            if p.get("status") == "open" and p.get("market_id") == self.mid and float(p.get("size", 0)) > 0:
                return p
        return None

    def _signed(self, pos: dict | None) -> float:
        if pos is None:
            return 0.0
        return float(pos["size"]) * (1 if pos["side"] == "long" else -1)

    def _open_risk(self) -> float:
        return sum(L["size"] * abs(L["entry"] - L["stop"]) for L in self.state.legs.values())

    def _reconcile(self, pos: dict | None):
        """Make our legs agree with what the exchange holds (a protective stop may have fired)."""
        st = self.state
        server = self._signed(pos)
        ours = sum(L["side"] * L["size"] for L in st.legs.values())
        self.unknown_position = server != 0 and not st.legs
        if abs(server - ours) <= self.step / 2:
            return
        if server == 0:
            if st.legs:
                log.warning("BTC-positie is gesloten door de server (stop of sluiting): legs %s gewist",
                            list(st.legs))
                st.last_trade_ms = now_ms()
            st.legs.clear()
            self._cancel_protection()
            return
        if not st.legs:
            if self._once("unknown"):
                log.warning("Er staat een BTC-positie open die de bot niet kent (%+.4f). "
                            "Ik open niets tot die gesloten is.", server)
            return
        # partly reduced (e.g. resized stop): trim the newest legs to what the exchange holds
        log.warning("Positie %.4f wijkt af van legs %.4f: legs worden bijgesteld", server, ours)
        same = [k for k, L in st.legs.items() if (L["side"] > 0) == (server > 0)]
        for k in list(st.legs):
            if k not in same:
                st.legs.pop(k)
        left = abs(server)
        for k in sorted(same, key=lambda k: st.legs[k]["opened_ms"]):
            take = min(st.legs[k]["size"], left)
            if take <= self.step / 2:
                st.legs.pop(k)
            else:
                st.legs[k]["size"] = take
            left -= take
        self._update_protection()

    # ------------------------------------------------------------------ exits
    def _price(self) -> float:
        q = self.api.quote(self.mid, "buy", float(self.market["min_size"]))
        return float(q.get("mid") or q.get("estimated_fill_price"))

    def _close_leg(self, name: str, why: str, px: float) -> bool:
        """Close one leg's size of the shared position. True when the leg is gone."""
        L = self.state.legs[name]
        pos = None if self.cfg.dry_run else self._position()
        size = L["size"] if pos is None else min(L["size"], abs(self._signed(pos)))
        log.info("%s: %s @ ~%.1f -> %.4f BTC sluiten%s", name, why, px, size,
                 "  [DRY-RUN]" if self.cfg.dry_run else "")
        if self.cfg.dry_run:
            self.state.legs.pop(name)
            return True
        if pos is None:
            return False  # nothing left on the exchange; _reconcile clears the legs
        try:
            o = self.api.close_position(pos["id"], expected_price=px, size=size,
                                        client_order_id=f"x-{name}-{L['opened_ms']}")
            o = self.api.wait_order(o["id"])
        except ApiError as e:
            log.error("%s: sluiten mislukt: %s", name, e)
            return False
        if o["status"] != "filled":
            log.warning("%s: sluitorder status %s %s", name, o["status"], o.get("reject_reason", ""))
            return False
        pnl = L["side"] * size * (float(o.get("fill_price", px)) - L["entry"])
        log.info("%s: gesloten @ %s, resultaat ~$%.2f (voor fees)", name, o.get("fill_price"), pnl)
        self.state.legs.pop(name)
        self.state.last_trade_ms = now_ms()
        return True

    def _after_closes(self):
        if not self.cfg.dry_run:
            self._reconcile(self._position())
        self._update_protection()

    def _manage_exits(self):
        """Close a leg's size when its own stop or target is crossed."""
        px = self._price()
        changed = False
        for name, L in list(self.state.legs.items()):
            s = L["side"]
            hit_stop = px <= L["stop"] if s > 0 else px >= L["stop"]
            hit_tp = L.get("tp") is not None and (px >= L["tp"] if s > 0 else px <= L["tp"])
            if hit_stop or hit_tp:
                changed |= self._close_leg(name, "stop geraakt" if hit_stop else "target geraakt", px)
        if changed:
            self._after_closes()

    # ------------------------------------------------------------------ bar close
    def _bar_due(self) -> bool:
        now = pd.Timestamp.now(tz="UTC")
        boundary = now.floor(td(BASE))
        if (now - boundary).total_seconds() < 20:
            return False
        return self.state.last_bar_ms < int((boundary - td(BASE)).timestamp() * 1000)

    def _on_bar(self, rs, legs_cfg):
        st = self.state
        now = pd.Timestamp.now(tz="UTC")
        boundary = now.floor(td(BASE))
        bar_open = boundary - td(BASE)
        tfs = sorted({BASE} | {lg.tf for lg in legs_cfg} | {self.legs_by_name[k].tf for k in st.legs})
        frames = closed_candles(self.cfg.provider, self.cfg.symbol, tfs)
        f15 = frames.get(BASE)
        if f15 is None or f15.empty or f15.index[-1] < bar_open:
            if now - boundary > pd.Timedelta(minutes=5):
                log.warning("15m-candle van %s kwam niet binnen; deze candle wordt overgeslagen", bar_open)
                st.last_bar_ms = int(bar_open.timestamp() * 1000)
            return
        st.last_bar_ms = int(bar_open.timestamp() * 1000)
        close = float(f15.close.iloc[-1])

        # 1) strategy exits (e.g. a Supertrend flip), then trailing stops of the legs that were already
        #    open, in the simulator's order
        closed = False
        for name in list(st.legs):
            lg = self.legs_by_name[name]
            if self._leg_closed_now(frames[lg.tf], lg, boundary):
                sig = latest(frames[lg.tf], lg)
                if sig is not None and sig.exit:
                    closed |= self._close_leg(name, "exit-signaal van de strategie", close)
        if closed:
            self._after_closes()
        for name, L in st.legs.items():
            lg = self.legs_by_name[name]
            if not L.get("trail"):
                continue
            sig = latest(frames[lg.tf], lg)
            if sig is None or sig.trail_dist is None:
                continue
            new = close - L["side"] * sig.trail_dist
            better = new > L["stop"] if L["side"] > 0 else new < L["stop"]
            if better:
                log.info("%s: trailing stop %.1f -> %.1f", name, L["stop"], new)
                L["stop"] = new

        # 2) entries of legs whose candle closed at this boundary (or closed while the bot was not running)
        for lg in legs_cfg:
            if self._leg_closed_now(frames[lg.tf], lg, boundary):
                st.seen[lg.name] = int(frames[lg.tf].index[-1].timestamp() * 1000)
        if rs.can_open:
            self._entries(rs, legs_cfg, frames, boundary, close)
        elif any(self._leg_closed_now(frames[lg.tf], lg, boundary) for lg in legs_cfg) and rs.blockers:
            log.info("geen nieuwe trades: %s", "; ".join(rs.blockers))

        # 3) the exchange-side protective stop follows the legs
        self._update_protection()
        if boundary.minute == 0:  # hourly sign of life in the log
            log.info("Hartslag: BTC %.0f | equity %s | open legs %d%s", close,
                     "?" if rs.equity is None else f"{rs.equity:,.2f}", len(st.legs),
                     "" if rs.can_open else " | geen nieuwe trades: " + "; ".join(rs.blockers))

    @staticmethod
    def _leg_closed_now(df: pd.DataFrame, lg, boundary: pd.Timestamp) -> bool:
        return not df.empty and df.index[-1] + td(lg.tf) == boundary

    def _missed(self, df: pd.DataFrame, lg, boundary: pd.Timestamp) -> bool:
        """The leg's last candle closed while the bot was not evaluating it (down, restarting, a deploy, a
        failed start) and the next candle of that timeframe has not closed yet: its signal may still count."""
        if df.empty:
            return False
        return (self.state.seen.get(lg.name, 0) < int(df.index[-1].timestamp() * 1000)
                and boundary < df.index[-1] + 2 * td(lg.tf))

    def _entries(self, rs, legs_cfg, frames, boundary, close):
        st, rp = self.state, self.cfg.risk
        halted = ((self.policy or {}).get("trading_halt") or {}).get("categories") or []
        for lg in legs_cfg:
            if lg.name in st.legs:
                continue
            df = frames[lg.tf]
            on_time = self._leg_closed_now(df, lg, boundary)
            if not on_time and not self._missed(df, lg, boundary):
                continue
            st.seen[lg.name] = int(df.index[-1].timestamp() * 1000)
            sig = latest(df, lg)
            if sig is None or sig.side == 0:
                continue
            side = "buy" if sig.side > 0 else "sell"
            if not on_time:
                # a missed signal is taken late only while the price is less than one stop distance away
                at = (sig.candle_time + td(lg.tf)).strftime("%d-%m %H:%M UTC")
                drift = sig.side * (close - sig.price)
                if not -sig.stop_dist < drift < sig.stop_dist:
                    log.info("%s: gemist signaal %s van %s overgeslagen: de prijs is al %+.0f verder",
                             lg.name, side, at, drift)
                    continue
                log.info("%s: signaal %s van %s gemist (bot draaide toen niet); wordt alsnog genomen",
                         lg.name, side, at)
            if len(st.legs) >= len(legs_cfg):
                break
            if any(L["side"] == -sig.side for L in st.legs.values()):
                log.info("%s: signaal %s overgeslagen: er staat een tegengestelde positie open", lg.name, side)
                continue
            open_notional = sum(L["size"] for L in st.legs.values()) * close
            sz = size_trade(rs, rp, self.market, close, sig.stop_dist, open_notional, len(st.legs),
                            len(legs_cfg), self.policy, halted)
            if sz.size <= 0:
                log.info("%s: signaal %s overgeslagen: %s", lg.name, side, sz.reason)
                continue
            coid = f"{lg.name}-{int(sig.candle_time.timestamp())}" + ("" if on_time else "-late")
            # the stop travels with the order: the weighted average of all legs including this one; it covers
            # the whole resulting position, so it replaces the stop of the legs already open
            planned = list(st.legs.values()) + [{"size": sz.size, "stop": close - sig.side * sig.stop_dist}]
            avg_stop = self._round_px(sum(L["size"] * L["stop"] for L in planned) / sum(L["size"] for L in planned))
            body = {"client_order_id": coid, "type": "market", "account_id": self.account_id,
                    "market_id": self.mid, "side": side, "size": sz.size, "expected_price": close,
                    "leverage": sz.leverage, "margin_mode": "cross", "stop_loss_price": avg_stop}
            if st.legs:
                body["replace_overlapping_exits"] = True
            log.info("%s: %s %.4f BTC @ ~%.1f  stop %.1f  %s  risico $%.2f%s", lg.name, side.upper(), sz.size,
                     close, close - sig.side * sig.stop_dist,
                     f"target {close + sig.side * sig.tp_dist:.1f}" if sig.tp_dist else "trailing",
                     sz.risk_usd, "  [DRY-RUN]" if self.cfg.dry_run else "")
            if self.cfg.dry_run:
                order = {"status": "filled", "fill_price": close, "filled_size": sz.size}  # virtual leg
            else:
                try:
                    order = self.api.place_order(body, idem=coid)
                    order = self.api.wait_order(order["id"])
                except ApiError as e:
                    log.error("%s: order geweigerd: %s %s", lg.name, e, e.body.get("error", {}).get("details", ""))
                    continue
            if order["status"] != "filled":
                log.warning("%s: order status %s %s", lg.name, order["status"], order.get("reject_reason", ""))
                continue
            fill = float(order.get("fill_price") or close)
            size = float(order.get("filled_size") or sz.size)
            st.legs[lg.name] = {
                "side": sig.side, "size": size, "entry": fill,
                "stop": fill - sig.side * sig.stop_dist,
                "tp": None if sig.tp_dist is None else fill + sig.side * sig.tp_dist,
                "trail": sig.trail_dist is not None, "opened_ms": now_ms()}
            st.trades_today += 1
            st.last_trade_ms = now_ms()
            rs.open_risk += size * sig.stop_dist
            if order.get("stop_loss_order_id"):
                st.protect_id = order["stop_loss_order_id"]
            st.protect_px, st.protect_size = avg_stop, sum(L["size"] for L in st.legs.values())
            log.info("%s: gevuld %.4f @ %.1f (fee %s), stop op de server @ %.1f", lg.name, size, fill,
                     order.get("fee"), avg_stop)
            # protect right away, not only at the end of the bar
            if not self._update_protection():
                return

    # ------------------------------------------------------------------ exchange-side protection
    def _round_px(self, px: float) -> float:
        return round(px, int(self.market.get("price_decimals", 1)))

    def _cancel_protection(self):
        """Forget the stop: a reduce-only exit is cancelled by the exchange once the position is flat."""
        st = self.state
        st.protect_id, st.protect_px, st.protect_size = None, 0.0, 0.0

    def _exits(self) -> list[dict]:
        return [o for o in self.api.working_orders(self.account_id)
                if o.get("market_id") == self.mid and o.get("reduce_only")]

    @staticmethod
    def _is_sl(o: dict) -> bool:
        return o.get("bracket_role") == "stop_loss" or o.get("type") in ("stop_market", "stop_limit")

    def _update_protection(self) -> bool:
        """One reduce-only stop for the whole position at the size-weighted average leg stop.

        Returns False (after closing everything) if the position could not be protected."""
        st = self.state
        if not st.legs:
            self._cancel_protection()
            return True
        size = sum(L["size"] for L in st.legs.values())
        avg = self._round_px(sum(L["size"] * L["stop"] for L in st.legs.values()) / size)
        if self.cfg.dry_run:
            st.protect_px, st.protect_size = avg, size
            return True
        try:
            pos = self._position()
            if pos is None:
                return True  # nothing to protect; _reconcile clears the legs
            exits = self._exits()
            sl = [o for o in exits if self._is_sl(o)]
            if (len(sl) == 1 and math.isclose(float(sl[0].get("trigger_price") or 0), avg)
                    and abs(float(sl[0]["size"]) - float(pos["size"])) < self.step / 2):
                st.protect_id, st.protect_px, st.protect_size = sl[0]["id"], avg, size
                return True
            pos_size = float(pos["size"])
            if sl:
                ops = [{"kind": "modify", "order_id": sl[0]["id"], "price": avg, "size": pos_size}]
                ops += [{"kind": "cancel", "order_id": o["id"]} for o in sl[1:]]
            else:
                ops = [{"kind": "place", "group": "sl", "price": avg, "size": pos_size, "execution_type": "market"}]
            expected = [{"order_id": o["id"], "price": float(o.get("trigger_price") or o.get("limit_price") or 0),
                         "size": float(o["size"])} for o in exits]
            self.api.update_exits(pos["id"], {"expected_position_size": pos_size, "expected_orders": expected,
                                              "operations": ops})
            new_sl = [o for o in self._exits() if self._is_sl(o)]
        except ApiError as e:
            log.error("KAN POSITIE NIET BESCHERMEN (%s %s): alles wordt gesloten",
                      e, e.body.get("error", {}).get("details", ""))
            self._flatten()
            return False
        st.protect_id = new_sl[0]["id"] if new_sl else None
        st.protect_px, st.protect_size = avg, size
        log.info("beschermende stop op de server: %.4f BTC @ %.1f", size, avg)
        return True

    # ------------------------------------------------------------------ inactivity rule
    def _keepalive(self, rs, funded: bool):
        """The platform breaches accounts without an executed trade for 30 (evaluation) or 7 (funded)
        days. If the strategy has been quiet that long, do the smallest possible trade and close it."""
        st, rp = self.state, self.cfg.risk
        days = rp.keepalive_days_funded if funded else rp.keepalive_days_eval
        if st.legs or self.unknown_position or not rs.can_open:
            return
        if now_ms() - (st.last_trade_ms or now_ms()) < days * 86_400_000:
            return
        px = self._price()
        step = self.step
        size = max(float(self.market["min_size"]),
                   math.ceil(float(self.market.get("min_notional", 0)) / px / step) * step)
        size = round(size, int(self.market.get("size_decimals", 8)))
        log.info("Inactiviteitsregel: al %.1f dagen geen trade. Kleinste trade (%.4f BTC) openen en sluiten.%s",
                 (now_ms() - st.last_trade_ms) / 86_400_000, size, "  [DRY-RUN]" if self.cfg.dry_run else "")
        if self.cfg.dry_run:
            st.last_trade_ms = now_ms()
            return
        coid = f"keepalive-{now_ms()}"
        try:
            o = self.api.wait_order(self.api.place_order(
                {"client_order_id": coid, "type": "market", "account_id": self.account_id, "market_id": self.mid,
                 "side": "buy", "size": size, "expected_price": px, "leverage": 1, "margin_mode": "cross"},
                idem=coid)["id"])
        except ApiError as e:
            log.error("keep-alive trade mislukt: %s", e)
            return
        if o["status"] == "filled":
            st.last_trade_ms = now_ms()
            pos = self._position()
            if pos is not None:
                try:
                    self.api.wait_order(self.api.close_position(pos["id"], expected_price=px,
                                                                client_order_id=f"{coid}-x")["id"])
                except ApiError as e:
                    log.error("keep-alive positie sluiten mislukt (%s): alles wordt gesloten", e)
                    self._flatten()

    def _flatten(self):
        st = self.state
        if self.cfg.dry_run:
            log.warning("[DRY-RUN] zou nu alles sluiten")
            st.legs.clear()
            return
        try:
            self.api.cancel_all(self.account_id)
        except ApiError as e:
            log.error("cancel-all faalde: %s", e)
        self.api.close_all(self.account_id)
        if st.legs:
            st.last_trade_ms = now_ms()
        st.legs.clear()
        st.protect_id, st.protect_px, st.protect_size = None, 0.0, 0.0
