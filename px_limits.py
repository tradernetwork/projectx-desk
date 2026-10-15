"""Guard configuration and persisted day state. See docs/DECISIONS.md.

Every limit fails closed: a zero or unset value means "refuse to open", never "unlimited".
Env is read once at Limits.from_env(); tests build Limits directly.
"""
import json
import os
import pathlib
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from px_time import trade_date

HERE = pathlib.Path(__file__).parent
BRACKET_MODES = ("position", "oco")  # anything else (incl. unset) refuses opens


def _f(name, default):
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return 0.0  # a garbled limit must refuse, not default open


def _rail(name, default):
    """A behavioural rail: blank, garbled or negative falls back to the DEFAULT (never silently off).
    Only an explicit `0` turns the rail off."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        v = int(raw.strip())
    except ValueError:
        return default
    return v if v >= 0 else default


def _i(name, default):
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return 0


@dataclass(frozen=True)
class Limits:
    trading: bool = False                 # PX_TRADING=1 to allow place(); preview works regardless
    mcp_place: bool = False               # PX_MCP_PLACE=1 lets the MCP origin place (off in v1)
    account_id: Optional[int] = None      # PX_ACCOUNT_ID: the ONE account this desk may touch
    account_kind: str = "sim"             # PX_ACCOUNT_KIND sim|live
    allow_live: bool = False              # PX_ALLOW_LIVE=1 required to touch a live account
    bracket_mode: str = "unset"           # PX_BRACKET_MODE position|oco; unset refuses opens
    allowed_roots: tuple = ("MES", "MNQ", "MCL", "MGC")
    max_qty_order: int = 1
    max_qty_symbol: int = 2
    max_qty_total: int = 3
    max_orders_day: int = 10
    cooldown_after_loss_s: int = 300      # PX_COOLDOWN_AFTER_LOSS_S: no new opens for this long after a losing trade (0 = off)
    max_consecutive_losses: int = 3       # PX_MAX_CONSEC_LOSSES: locked for the session after this many losses in a row (0 = off)
    size_after_loss_lock: bool = True     # PX_SIZE_AFTER_LOSS_LOCK=0 to disable: after a loss, risk may not exceed the last trade's risk
    risk_base: str = "drawdown"          # PX_RISK_BASE: "drawdown" (1R = risk_pct of the live drawdown room, balance minus the MLL floor; the loss limit is the base, not the account size) or "remaining" (old: risk_pct of min(daily, drawdown) room)
    size_scalers: bool = True             # PX_SIZE_SCALERS=0 disables the ADX/ATR risk cuts (they only ever reduce)
    daily_loss_limit: float = 0.0         # dollars; 0 = refuse
    dd_buffer: float = 0.0                # dollars of trailing-drawdown room; 0 = refuse
    max_loss_frac: float = 0.25           # one trade may risk at most this share of remaining budget
    min_budget_multiple: float = 2.0      # refuse to open if remaining budget < this x max loss
    ticket_ttl_s: float = 120.0
    max_quote_age_s: float = 2.0
    max_hub_age_s: float = 30.0
    max_entry_dev_ticks: int = 20
    max_drift_ticks: int = 8              # place() refuses if the market moved this far from the staged price
    stop_ack_s: float = 5.0
    min_stop_ticks: int = 4
    max_stop_ticks: int = 200
    flat_by: Optional[str] = None         # PX_FLAT_BY "HH:MM" CT: auto-flatten everything at this time, and refuse opens 2 min before. Topstep's hard rule is 15:10 CT
    mll_floor: float = 0.0                # PX_MLL_FLOOR dollars: the firm's maximum-loss floor. Drawdown room = live balance - floor. 0 = use PX_DRAWDOWN_BUFFER
    no_new_after: Optional[str] = None    # "HH:MM" in America/Chicago; unset refuses opens
    intent_block_s: float = 60.0
    flatten_fallback: bool = False        # PX_FLATTEN_FALLBACK=1: after a failed closeContract, send a capped opposite market order. OFF: halt and tell a human
    halt_path: pathlib.Path = field(default_factory=lambda: HERE / "data" / "HALT")
    state_path: pathlib.Path = field(default_factory=lambda: HERE / "data" / "day_state.json")
    audit_path: pathlib.Path = field(default_factory=lambda: HERE / "data" / "audit.jsonl")
    intent_path: pathlib.Path = field(default_factory=lambda: HERE / "data" / "intents.json")

    @classmethod
    def from_env(cls):
        aid = os.getenv("PX_ACCOUNT_ID", "").strip()
        roots = tuple(r.strip().upper() for r in os.getenv("PX_ALLOWED_ROOTS", "MES,MNQ,MCL,MGC").split(",") if r.strip())
        d = {}
        if os.getenv("PX_HALT_FILE"):
            d["halt_path"] = pathlib.Path(os.environ["PX_HALT_FILE"])
        if os.getenv("PX_STATE_PATH"):
            d["state_path"] = pathlib.Path(os.environ["PX_STATE_PATH"])
        if os.getenv("PX_INTENT_PATH"):
            d["intent_path"] = pathlib.Path(os.environ["PX_INTENT_PATH"])
        if os.getenv("PX_AUDIT_PATH"):
            d["audit_path"] = pathlib.Path(os.environ["PX_AUDIT_PATH"])
        return cls(
            trading=os.getenv("PX_TRADING", "0") == "1",
            mcp_place=os.getenv("PX_MCP_PLACE", "0") == "1",
            account_id=int(aid) if aid.isdigit() else None,
            account_kind=os.getenv("PX_ACCOUNT_KIND", "sim").lower(),
            allow_live=os.getenv("PX_ALLOW_LIVE", "0") == "1",
            bracket_mode=os.getenv("PX_BRACKET_MODE", "unset").lower(),
            allowed_roots=roots,
            max_qty_order=_i("PX_MAX_QTY_ORDER", 1),
            max_qty_symbol=_i("PX_MAX_QTY_SYMBOL", 2),
            max_qty_total=_i("PX_MAX_QTY_TOTAL", 3),
            max_orders_day=_i("PX_MAX_ORDERS_DAY", 10),
            risk_base="remaining" if os.getenv("PX_RISK_BASE", "drawdown").strip().lower() == "remaining" else "drawdown",
            size_scalers=os.getenv("PX_SIZE_SCALERS", "1") != "0",
            cooldown_after_loss_s=_rail("PX_COOLDOWN_AFTER_LOSS_S", 300),
            max_consecutive_losses=_rail("PX_MAX_CONSEC_LOSSES", 3),
            size_after_loss_lock=os.getenv("PX_SIZE_AFTER_LOSS_LOCK", "1") != "0",
            flatten_fallback=os.getenv("PX_FLATTEN_FALLBACK", "0") == "1",
            daily_loss_limit=_f("PX_DAILY_LOSS_LIMIT", 0),
            dd_buffer=_f("PX_DRAWDOWN_BUFFER", 0),
            no_new_after=os.getenv("PX_NO_NEW_AFTER") or None,
            flat_by=os.getenv("PX_FLAT_BY") or None,
            mll_floor=_f("PX_MLL_FLOOR", 0),
            **d,
        )


def atomic_write_json(path: pathlib.Path, obj) -> None:
    """Write JSON so a crash leaves either the old file or the new one, never a torn file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class DayState:
    """Order count per CT trading day, persisted so a restart cannot reset the daily cap.

    A missing file is a fresh day. A CORRUPT file is unknown state: `ok` goes False and the guard
    refuses to open rather than assume zero orders.
    """

    def __init__(self, path: pathlib.Path):
        self.path, self.ok, self.date, self.orders, self.last_risk = path, True, None, 0, None
        self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text())
            self.date, self.orders = raw["date"], int(raw["orders"])
            lr = raw.get("last_risk")
            self.last_risk = float(lr) if isinstance(lr, (int, float)) else None
        except FileNotFoundError:
            pass
        except (ValueError, KeyError, TypeError, OSError):
            self.ok = False  # corrupt: do not guess

    def orders_today(self, now: Optional[datetime] = None) -> int:
        today = trade_date(now or datetime.now(timezone.utc)).isoformat()
        return self.orders if self.date == today else 0

    def last_risk_today(self, now: Optional[datetime] = None) -> Optional[float]:
        """Dollar risk of the most recent order placed today (survives a restart), or None."""
        today = trade_date(now or datetime.now(timezone.utc)).isoformat()
        return self.last_risk if self.date == today else None

    def count_order(self, now: Optional[datetime] = None, risk: Optional[float] = None) -> int:
        today = trade_date(now or datetime.now(timezone.utc)).isoformat()
        if self.date != today:
            self.date, self.orders, self.last_risk = today, 0, None
        self.orders += 1
        if isinstance(risk, (int, float)) and risk > 0:
            self.last_risk = float(risk)
        atomic_write_json(self.path, {"date": self.date, "orders": self.orders, "last_risk": self.last_risk})
        return self.orders
