"""Start the bot:      python -m fundedbot [--dry-run] [--live]
Show its trades:    python -m fundedbot trades [--n 50] [--live]
List your accounts: python -m fundedbot accounts [--live]   (the ID to put in FP_ACCOUNT_ID)
Dashboard:          python -m fundedbot dashboard [--live] [--port 8787] [--host 0.0.0.0] [--autostart]
State, log and pid files go to FP_DATA_DIR (default: the current folder)."""
import argparse
import logging
import os
import sys
import time
from pathlib import Path

from .api import Client
from .bot import Bot
from .config import data_dir, load_settings


def _ts(ms):
    import pandas as pd
    return pd.to_datetime(ms, unit="ms", utc=True).tz_convert("Europe/Brussels").strftime("%Y-%m-%d %H:%M")


def show_accounts(cfg):
    """Every account this key can see, with the website number (FP-...) next to the API id."""
    api = Client(cfg.base_url, cfg.api_key)
    accs = api.accounts()
    print(f"{'nummer (website)':<18} {'naam':<22} {'fase':<11} {'status':<9} {'start':>10} {'balans':>12}  id voor FP_ACCOUNT_ID")
    for a in accs:
        mark = "  <- in .env" if a["id"] == cfg.account_id else ""
        print(f"{str(a.get('account_number', '')):<18} {a.get('name', ''):<22} {a.get('stage', ''):<11} "
              f"{a.get('status', ''):<9} {a.get('starting_balance', 0):>10,.0f} {a.get('balance', 0):>12,.2f}  {a['id']}{mark}")
    if cfg.account_id and not any(a["id"] == cfg.account_id for a in accs):
        print("\nLet op: FP_ACCOUNT_ID in .env hoort bij geen van deze accounts.")
    if not cfg.live:
        print("\n(Sandbox-key: alleen sandbox-accounts. Je echte accounts zie je met een fp_live_ key en --live.)")


def show_trades(cfg, n: int):
    api = Client(cfg.base_url, cfg.api_key)
    account_id = cfg.account_id
    if not account_id:
        accs = api.accounts()
        if len(accs) != 1:
            raise SystemExit("Meerdere accounts: zet FP_ACCOUNT_ID in .env.")
        account_id = accs[0]["id"]
    acc = api.account(account_id)
    risk = acc.get("risk") or {}
    print(f"Account {acc['name']} ({acc['stage']}, {acc['status']})  start ${acc['starting_balance']:,.2f}  "
          f"balance ${acc['balance']:,.2f}  equity ${risk.get('equity') or 0:,.2f}")

    open_ = [p for p in api.positions(account_id) if p["status"] == "open"]
    print(f"\nOpen posities ({len(open_)}):")
    for p in open_:
        print(f"  {_ts(p['opened_at'])}  {p['symbol']:<5} {p['side']:<5} {p['size']} @ {p['entry_price']}")

    closed = api.closed_positions(account_id, n)
    print(f"\nGesloten trades (laatste {len(closed)}):")
    print(f"  {'geopend':<16} {'gesloten':<16} {'coin':<5} {'kant':<5} {'entry':>12} {'exit':>12} "
          f"{'reden':<11} {'P&L':>9} {'kosten':>8}")
    total = wins = 0.0
    for p in closed:
        pnl = p.get("realized_pnl") or 0.0
        costs = (p.get("fees") or 0.0) + (p.get("funding") or 0.0)
        net = pnl - costs if p.get("realized_pnl") is not None else 0.0
        total += net
        wins += net > 0
        print(f"  {_ts(p['opened_at']):<16} {_ts(p['closed_at']) if p.get('closed_at') else '':<16} "
              f"{p['symbol']:<5} {p['side']:<5} {p['entry_price']:>12.6g} {p.get('exit_price', 0):>12.6g} "
              f"{p.get('close_reason', ''):<11} {net:>9.2f} {costs:>8.2f}")
    if closed:
        print(f"\nTotaal netto ${total:,.2f}, winrate {wins / len(closed):.0%}")


def main():
    ap = argparse.ArgumentParser(prog="fundedbot")
    ap.add_argument("command", nargs="?", default="run", choices=["run", "trades", "accounts", "dashboard"])
    ap.add_argument("--dry-run", action="store_true", help="alles berekenen en loggen, geen orders plaatsen")
    ap.add_argument("--live", action="store_true", default=os.environ.get("FP_LIVE") == "1",
                    help="live account (fp_live_ key) in plaats van sandbox; op Railway: variabele FP_LIVE=1")
    ap.add_argument("--env", default=".env")
    ap.add_argument("--n", type=int, default=50, help="aantal gesloten trades (trades-commando)")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8787)), help="poort van het dashboard")
    ap.add_argument("--host", default="127.0.0.1", help="adres van het dashboard (0.0.0.0 alleen met DASHBOARD_PASSWORD)")
    ap.add_argument("--autostart", action="store_true", default=os.environ.get("FP_AUTOSTART") == "1",
                    help="dashboard start de bot zelf en herstart hem na een crash (tenzij gestopt)")
    ap.add_argument("--state", default="state.json", help="statusbestand van de bot")
    args = ap.parse_args()

    cfg = load_settings(live=args.live, dry_run=args.dry_run, env_file=args.env)
    if args.command == "trades":
        show_trades(cfg, args.n)
        return
    if args.command == "accounts":
        show_accounts(cfg)
        return
    if args.command == "dashboard":
        from .dashboard import serve
        serve(cfg, args.port, args.host, args.autostart)
        return
    cfg.state_file = Path(args.state) if Path(args.state).is_absolute() else data_dir() / args.state
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(data_dir() / "bot.log", encoding="utf-8")],
    )
    log = logging.getLogger("fundedbot")
    while True:  # a network hiccup at start-up must not end the bot
        try:
            Bot(cfg).run()
            break
        except (SystemExit, KeyboardInterrupt):
            raise
        except Exception:
            log.exception("De bot kon niet starten of viel uit; nieuwe poging over 30 seconden")
            time.sleep(30)


if __name__ == "__main__":
    main()
