"""Bot settings. Secrets come from the environment (or a .env file), never from code."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SANDBOX_URL = "https://sandbox.myfundedperpetuals.com"
LIVE_URL = "https://developers.myfundedperpetuals.com"
MARKET_STREAM_URL = "wss://api-stream.myfundedperpetuals.com/v1/market-data"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@dataclass(frozen=True)
class Leg:
    """One independent strategy slot. Several legs share the one BTC position on the exchange."""
    name: str
    kind: str      # ema_cross | keltner | donchian | level_break | supertrend | smc_structure | wide_bar
    tf: str        # candle timeframe: 15m | 1h | 2h | 4h
    params: tuple  # (key, value) pairs, hashable

    @property
    def p(self) -> dict:
        return dict(self.params)


def leg(name, strategy, tf, /, **params) -> Leg:
    return Leg(name, strategy, tf, tuple(sorted(params.items())))


# Challenge: only legs that beat random entry moments per trade in both periods (research/edge_scan.py,
# edge_combo.py): ~51-54% per challenge in ~3-4 weeks. Funded: wide-range candle 4h alone, a leg with real
# per-trade edge in both periods (research/edge_scan3.py); after the challenge it pays out in ~78-91% of funded
# accounts in ~3-4 months (research/funded_like_cycles.py). One payout at +5%.
# Order = entry priority.
CHALLENGE_LEGS = (
    leg("keltner4h_trend", "keltner", "4h", n=20, k=2.5, sl=1.5, trail=3.0, filt=200),
    leg("keltner2h", "keltner", "2h", n=20, k=2.5, sl=1.5, trail=5.0),
    leg("donch4h_trend", "donchian", "4h", n=24, sl=2.0, trail=3.0, filt=200),
)
FUNDED_LEGS = (
    leg("wide4h", "wide_bar", "4h", k=2.0, close_frac=0.25, sl=2.0, trail=3.0),
)


@dataclass
class RiskParams:
    challenge_risk: float = 0.012   # fraction of starting balance lost if a leg's stop is hit
    funded_risk: float = 0.008
    daily_guard: float = 0.6        # flatten when 60% of the daily allowance is used (40% stays free)
    dd_guard: float = 0.9           # flatten when this share of the max-loss allowance is used
    max_total_leverage: float = 5.0  # total notional / starting balance
    order_leverage: float = 5.0     # leverage sent per order (capped by the market's max)
    max_trades_per_day: int = 12
    min_risk_fraction: float = 0.3  # skip trades shrunk below this share of the normal risk
    payout_target: float = 0.05     # funded: flatten and request a payout at +5%
    keepalive_days_eval: float = 25.0   # platform inactivity rule: 30 days in evaluation
    keepalive_days_funded: float = 6.0  # platform inactivity rule: 7 days when funded


@dataclass
class Settings:
    api_key: str
    base_url: str
    live: bool
    account_id: str | None
    symbol: str = "BTCUSDT"
    provider: str = "binance"
    dry_run: bool = False
    poll_seconds: int = 10
    state_file: Path = Path("state.json")
    challenge_legs: tuple = CHALLENGE_LEGS
    funded_legs: tuple = FUNDED_LEGS
    risk: RiskParams = field(default_factory=RiskParams)


def data_dir() -> Path:
    """Where state, log and pid files live: FP_DATA_DIR (e.g. a Railway volume) or the current folder."""
    d = Path(os.environ.get("FP_DATA_DIR") or ".").resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_settings(live: bool = False, dry_run: bool = False, env_file: str = ".env") -> Settings:
    _load_dotenv(Path(env_file))
    key = os.environ.get("FP_API_KEY", "").strip()
    if not key:
        raise SystemExit("FP_API_KEY ontbreekt. Zet hem in .env (zie .env.example) of als variabele (Railway).")
    if key.startswith("fp_live_") and not live:
        raise SystemExit("Dit is een LIVE key. Start bewust met --live als je echt live wilt traden.")
    if key.startswith("fp_test_") and live:
        raise SystemExit("--live gevraagd maar dit is een sandbox key (fp_test_).")
    return Settings(
        api_key=key,
        base_url=LIVE_URL if live else SANDBOX_URL,
        live=live,
        account_id=os.environ.get("FP_ACCOUNT_ID") or None,
        dry_run=dry_run,
        state_file=data_dir() / "state.json",
    )
