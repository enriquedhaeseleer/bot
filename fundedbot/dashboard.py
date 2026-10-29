"""Local dashboard: python -m fundedbot dashboard  ->  http://127.0.0.1:8787

Shows the account, how much of the daily / max loss is used, the bot's legs and server stop, current
signals, recent trades and the bot log, and can start / stop the bot or close everything.
It only listens on this computer; the API key never leaves the Python process. The bot runs as its own
process (bot.pid / bot.stop, see proc.py), so it keeps running when the dashboard is closed.
With a second account configured (config.profiles), each account has its own bot and the page switches
between them; every request names the account with ?acct=live|sandbox.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import proc
from .api import ApiError, Client
from .bitunix import client_for
from .bot import ny_day
from .config import Settings


def mode_label(cfg: Settings) -> str:
    return "EIGEN" if cfg.venue == "bitunix" else ("LIVE" if cfg.live else "SANDBOX")
from .marketdata import closed_candles
from .risk import _frac, assess
from .strategy import latest, trigger

CODE = Path(__file__).resolve().parents[1]
PAGE = Path(__file__).with_name("dashboard.html")


class Dashboard:
    def __init__(self, cfg: Settings, client: Client | None = None, root: Path | None = None):
        self.cfg = cfg
        self.api = client or client_for(cfg)
        self.root = root or cfg.state_file.resolve().parent  # state, log and pid files
        self._sig_cache: tuple[float, dict] = (0.0, {})
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ data
    def _running(self) -> dict | None:
        info = proc.read_pid(self.root)
        return info if info and proc.pid_alive(info.get("pid")) else None

    def _state(self) -> dict:
        info = self._running() or {}
        p = self.root / info.get("state", "state.json")
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return {}

    def _bot_status(self) -> dict:
        info = self._running()
        heartbeat = None
        if info:
            sp = self.root / info.get("state", "state.json")
            heartbeat = time.time() - sp.stat().st_mtime if sp.exists() else None
        return {"running": bool(info), "mode": (info or {}).get("mode", ""), "pid": (info or {}).get("pid"),
                "started": (info or {}).get("started"), "heartbeat_s": heartbeat}

    def overview(self) -> dict:
        cfg, rp = self.cfg, self.cfg.risk
        acc = self.api.account(cfg.account_id)
        pol = self.api.trading_policy(cfg.account_id)
        pos = [p for p in self.api.positions(cfg.account_id) if p.get("status") == "open"]
        risk = acc.get("risk") or {}
        req = risk.get("requirements") or {}
        start = float(acc["starting_balance"])
        eq = risk.get("equity")
        dll = _frac(req.get("daily_loss_pct"), 0.03)
        mdd = _frac(req.get("max_drawdown_pct"), 0.06)
        tgt = _frac(req.get("profit_target_pct"), 0.10)
        st = self._state()
        legs = st.get("legs", {})
        funded = acc.get("stage") == "funded"
        leg_risk = rp.funded_risk if funded else rp.challenge_risk
        open_risk = sum(L["size"] * abs(L["entry"] - L["stop"]) for L in legs.values())
        rs = assess(acc, pol, len(legs), open_risk, leg_risk, rp, st.get("trades_today", 0),
                    st.get("locked_day") == ny_day())
        day_floor = risk.get("daily_loss_floor")
        day_base = float(day_floor) + dll * start if day_floor is not None else None
        stage_legs = cfg.funded_legs if funded else cfg.challenge_legs
        # every trade on the account (the bot's and by hand) opened since midnight New York
        midnight = int(pd.Timestamp.now(tz="America/New_York").normalize().timestamp() * 1000)
        closed = self.api.closed_positions(cfg.account_id, 50)
        opened = [int(p.get("opened_at") or 0) for p in closed + pos]
        moments = opened + [int(p.get("closed_at") or 0) for p in closed]
        return {
            "mode": mode_label(cfg),
            "first_equity": st.get("first_equity"),
            "account_trades": {"today": sum(t >= midnight for t in opened), "last_ms": max(moments) if moments else None},
            "account": {k: acc.get(k) for k in ("name", "account_number", "stage", "status", "starting_balance", "balance")}
            | {"id": cfg.account_id, "equity": eq},
            "rules": {"daily_loss_pct": dll, "max_drawdown_pct": mdd, "profit_target_pct": tgt},
            "levels": {"day_base": day_base, "daily_floor": day_floor, "dd_floor": risk.get("max_drawdown_floor"),
                       "guard": rs.guard_level if rs.guard_level > -1e18 else None,
                       "target_equity": start * (1 + tgt) if not funded else start * (1 + rp.payout_target),
                       "remaining_profit": risk.get("remaining_profit_equity")},
            "today_pnl": (eq - day_base) if (eq is not None and day_base is not None) else None,
            "can_open": rs.can_open, "blockers": rs.blockers, "stuck": rs.stuck,
            "positions": [{k: p.get(k) for k in ("id", "symbol", "side", "size", "entry_price", "opened_at")} for p in pos],
            "bot_state": {"legs": legs, "protect_px": st.get("protect_px"), "protect_size": st.get("protect_size"),
                          "locked_day": st.get("locked_day"), "payout_reached": st.get("payout_reached", False),
                          "done": st.get("done", False), "last_trade_ms": st.get("last_trade_ms"),
                          "last_bar_ms": st.get("last_bar_ms"), "trades_today": st.get("trades_today", 0)},
            "config": {"stage_legs": [{"name": lg.name, "kind": lg.kind, "tf": lg.tf} for lg in stage_legs],
                       "leg_risk": leg_risk, "daily_guard": rp.daily_guard, "payout_target": rp.payout_target},
            "bot": self._bot_status(),
            "policy": {k: pol.get(k) for k in ("payout_pending", "manual_trading_blocked", "opening_exposure_restricted",
                                                 "restriction")} | {"halt": (pol.get("trading_halt") or {}).get("platform")},
            "time": int(time.time() * 1000),
        }

    def trades(self, n: int = 30) -> list[dict]:
        out = []
        for p in self.api.closed_positions(self.cfg.account_id, n):
            pnl = p.get("realized_pnl")
            costs = (p.get("fees") or 0.0) + (p.get("funding") or 0.0)
            out.append({"opened_at": p.get("opened_at"), "closed_at": p.get("closed_at"), "side": p.get("side"),
                        "size": p.get("size"), "entry": p.get("entry_price"), "exit": p.get("exit_price"),
                        "reason": p.get("close_reason"), "pnl": None if pnl is None else pnl - costs, "costs": costs})
        return out

    def signals(self) -> dict:
        t, cached = self._sig_cache
        if time.time() - t < 60:
            return cached
        legs = list(self.cfg.challenge_legs) + [lg for lg in self.cfg.funded_legs if lg not in self.cfg.challenge_legs]
        frames = closed_candles(self.cfg.provider, self.cfg.symbol, sorted({"15m"} | {lg.tf for lg in legs}))
        f15 = frames["15m"]
        out = {"price": float(f15.close.iloc[-1]) if not f15.empty else None, "legs": []}
        for lg in legs:
            df = frames[lg.tf]
            s = latest(df, lg)
            stage = "challenge" if lg in self.cfg.challenge_legs else "funded"
            if lg in self.cfg.challenge_legs and lg in self.cfg.funded_legs:
                stage = "beide"
            tf = pd.Timedelta(lg.tf.replace("m", "min"))
            forming, next_close = None, None
            if not df.empty:
                next_close = df.index[-1] + 2 * tf  # the candle forming now closes then
                cur = f15[f15.index >= df.index[-1] + tf]
                if len(cur):
                    forming = {"open": float(cur.open.iloc[0]), "high": float(cur.high.max()), "low": float(cur.low.min())}
            out["legs"].append({"name": lg.name, "tf": lg.tf, "stage": stage,
                                "candle": str(s.candle_time) if s else None, "side": s.side if s else None,
                                "stop_dist": s.stop_dist if s else None, "trigger": trigger(df, lg, forming),
                                "next_close": str(next_close) if next_close is not None else None})
        self._sig_cache = (time.time(), out)
        return out

    def log_tail(self, n: int = 150) -> list[str]:
        log = self.root / "bot.log"
        if not log.exists():
            return []
        with log.open(encoding="utf-8", errors="replace") as f:
            return f.readlines()[-n:]

    # ------------------------------------------------------------------ actions
    def start_bot(self, dry: bool) -> dict:
        """Start the bot as its own process: it keeps running when the dashboard or terminal closes."""
        with self._lock:
            info = self._running()
            if info:
                return {"ok": False, "message": f"De bot draait al ({info.get('mode')})."}
            (self.root / "bot.disabled").unlink(missing_ok=True)  # a server reboot may start it again
            args = [sys.executable, "-m", "fundedbot"]
            if dry:
                args += ["--dry-run", "--state", "state_dryrun.json"]
            console = open(self.root / "bot_console.log", "ab")
            # the bot reads its account from these; with several accounts each bot gets its own
            env = dict(os.environ, FP_DATA_DIR=str(self.root))
            if self.cfg.venue == "bitunix":
                env["FP_PROFILE"] = "eigen"  # Bitunix keys come from FP_BITUNIX_API_KEY / _SECRET
            else:
                if self.cfg.live:
                    args.append("--live")
                env.update(FP_PROFILE="", FP_API_KEY=self.cfg.api_key, FP_ACCOUNT_ID=self.cfg.account_id or "",
                           FP_LIVE="1" if self.cfg.live else "0")
            kw = dict(cwd=CODE, env=env, stdout=console, stderr=console, stdin=subprocess.DEVNULL, close_fds=True)
            if sys.platform == "win32":
                detached = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
                try:  # also leave a job object (a terminal or app that kills its children on exit)
                    subprocess.Popen(args, creationflags=detached | 0x01000000, **kw)
                except OSError:
                    subprocess.Popen(args, creationflags=detached, **kw)
            else:
                subprocess.Popen(args, start_new_session=True, **kw)
            console.close()
            for _ in range(30):  # wait until it has registered itself
                time.sleep(0.5)
                if self._running():
                    mode = "dry-run" if dry else mode_label(self.cfg).lower()
                    return {"ok": True, "message": f"Bot gestart ({mode}). Hij blijft draaien als je het dashboard sluit."}
            return {"ok": False, "message": "De bot startte niet. Kijk in bot_console.log of het logboek."}

    def stop_bot(self) -> dict:
        with self._lock:
            info = self._running()
            if not info:
                return {"ok": False, "message": "De bot draait niet."}
            (self.root / "bot.stop").touch()
            (self.root / "bot.disabled").touch()  # on a server: do not start it again after a reboot
            for _ in range(60):  # it checks the flag every poll (10 s)
                time.sleep(0.5)
                if not proc.pid_alive(info["pid"]):
                    return {"ok": True, "message": "Bot gestopt. Open posities blijven staan met hun stop op de server."}
            proc.kill(info["pid"])
            (self.root / "bot.pid").unlink(missing_ok=True)
            return {"ok": True, "message": "Bot geforceerd gestopt. Open posities blijven staan met hun stop op de server."}

    def watch_once(self) -> str | None:
        """Start the bot unless it runs, was stopped on purpose (bot.disabled) or finished the challenge."""
        if (self.root / "bot.disabled").exists() or self._state().get("done") or self._running():
            return None
        return self.start_bot(dry=False)["message"]

    def close_all(self) -> dict:
        stopped = self.stop_bot()["ok"]
        try:
            self.api.cancel_all(self.cfg.account_id)
        except ApiError as e:
            return {"ok": False, "message": f"Orders annuleren mislukt: {e}"}
        try:
            self.api.close_all(self.cfg.account_id)
        except ApiError as e:
            return {"ok": False, "message": f"Sluiten mislukt: {e}"}
        return {"ok": True, "message": "Alle orders geannuleerd en alle posities gesloten."
                + (" De bot is gestopt." if stopped else "")}


def _password() -> str:
    return os.environ.get("DASHBOARD_PASSWORD", "")


def authorized(header: str | None, password: str) -> bool:
    """HTTP Basic auth: any user name, the password from DASHBOARD_PASSWORD."""
    if not password:
        return True
    if not header or not header.startswith("Basic "):
        return False
    try:
        _, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except ValueError:
        return False
    return hmac.compare_digest(given.encode(), password.encode())


def watch_all(dashes: dict[str, Dashboard], every: float = 60.0):
    """Keep every account's bot running (Railway, server), also after a crash or a restart of the machine."""
    while True:
        for name, dash in dashes.items():
            try:
                msg = dash.watch_once()
                if msg:
                    print(time.strftime("%Y-%m-%d %H:%M:%S"), f"watchdog ({name}):", msg, flush=True)
            except Exception as e:  # never let the watchdog die
                print(f"watchdog fout ({name}):", e, flush=True)
        time.sleep(every)


def serve(cfgs: Settings | dict[str, Settings], port: int = 8787, host: str = "127.0.0.1", autostart: bool = False):
    password = _password()
    if host not in ("127.0.0.1", "localhost") and not password:
        raise SystemExit("Het dashboard openzetten voor internet kan alleen met een wachtwoord: zet DASHBOARD_PASSWORD.")
    if isinstance(cfgs, Settings):
        cfgs = {"live" if cfgs.live else "sandbox": cfgs}
    dashes = {name: Dashboard(cfg) for name, cfg in cfgs.items()}
    first = next(iter(dashes))
    if autostart:
        threading.Thread(target=watch_all, args=(dashes,), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the console quiet
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, default=str).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _auth(self) -> bool:
            if authorized(self.headers.get("Authorization"), password):
                return True
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="FundedBot", charset="UTF-8"')
            self.end_headers()
            return False

        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/health":  # for the platform's health check, tells nothing about the account
                return self._json({"ok": True})
            if not self._auth():
                return
            dash = dashes.get(parse_qs(u.query).get("acct", [first])[0])
            try:
                if u.path == "/":
                    body = PAGE.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Cache-Control", "no-store")  # after a deploy the browser gets the new page
                    self.end_headers()
                    self.wfile.write(body)
                elif u.path == "/api/profiles":
                    self._json([{"name": n, "mode": mode_label(d.cfg)} for n, d in dashes.items()])
                elif dash is None:
                    self._json({"error": "onbekend account"}, 404)
                elif u.path == "/api/overview":
                    self._json(dash.overview())
                elif u.path == "/api/trades":
                    self._json(dash.trades(int(parse_qs(u.query).get("n", ["30"])[0])))
                elif u.path == "/api/signals":
                    self._json(dash.signals())
                elif u.path == "/api/log":
                    self._json(dash.log_tail())
                else:
                    self._json({"error": "niet gevonden"}, 404)
            except ApiError as e:
                self._json({"error": f"API: {e}"}, 502)
            except Exception as e:  # show the problem in the page instead of a dead request
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_POST(self):
            if not self._auth():
                return
            # only accept requests from the page itself
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._json({"ok": False, "message": "Geweigerd."}, 403)
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
            u = urlparse(self.path).path
            dash = dashes.get(data.get("acct") or first)
            if dash is None:
                return self._json({"ok": False, "message": "Onbekend account."}, 404)
            if u == "/api/bot/start":
                self._json(dash.start_bot(bool(data.get("dry"))))
            elif u == "/api/bot/stop":
                self._json(dash.stop_bot())
            elif u == "/api/close-all":
                if data.get("confirm") != "SLUIT":
                    return self._json({"ok": False, "message": "Typ SLUIT om te bevestigen."}, 400)
                self._json(dash.close_all())
            else:
                self._json({"error": "niet gevonden"}, 404)

    srv = ThreadingHTTPServer((host, port), Handler)
    where = f"http://127.0.0.1:{port}" if host in ("127.0.0.1", "localhost") else f"poort {port} (met wachtwoord)"
    accounts = ", ".join(f"{n}: account {d.cfg.account_id}, data in {d.root}" for n, d in dashes.items())
    print(f"Dashboard: {where}   ({accounts}{', bots starten automatisch' if autostart else ''})", flush=True)
    print("Dashboard sluiten: Ctrl+C. De bot blijft dan gewoon doorlopen; stop hem met de knop 'Stoppen'.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
