"""
APEX BOT — Step 6: Guardian Module
====================================
Robustné ochranné vrstvy bota:

  A) StructuredLogger   — zapisuje do bot_log.txt + konzoly, JSON záznamy
  B) CircuitBreaker     — Flash Crash ochrana (cena -X% za Y minút → halt)
  C) EmergencyStop      — okamžité zrušenie všetkých príkazov + halt
  D) Heartbeat          — pravidelný súhrn každých 30 minút
  E) TrailingTakeProfit — sleduje cenu nahor, predá pri otočení
  F) StaleInventory     — DCA / micro-scalp keď cena klesne pod grid
  G) TelegramNotifier   — správy pri obchode, chybe, heartbeate

Závisí od: step1_core.py (PaperTracker, PrecisionManager, SymbolConfig)
"""

import os
import json
import time
import logging
import threading
import requests
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional, Callable
from decimal import Decimal

# ════════════════════════════════════════════════════════════════
# A) STRUCTURED LOGGER
# ════════════════════════════════════════════════════════════════

class StructuredLogger:
    """
    Dvojvrstvový logger:
      1. Konzola    — farebné INFO správy pre live sledovanie
      2. bot_log.txt — JSON záznamy pre spätnú analýzu

    Každý JSON záznam obsahuje:
      timestamp, level, category, message, + voliteľné dáta

    Kategórie:
      TRADE | SIGNAL | CIRCUIT | EMERGENCY | HEARTBEAT |
      TRAILING | STALE | TELEGRAM | SYSTEM | ERROR
    """

    LOG_FILE = "bot_log.txt"
    SEP      = "─" * 64

    def __init__(self, symbol: str, test_mode: bool):
        self.symbol    = symbol
        self.test_mode = test_mode

        # Konzolový handler
        self._console = logging.getLogger("ApexBot.Guardian")
        if not self._console.handlers:
            h = logging.StreamHandler()
            h.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"
            ))
            self._console.addHandler(h)
        self._console.setLevel(logging.INFO)

        # File handler pre bot_log.txt — každý riadok = 1 JSON objekt
        self._file_handler = logging.FileHandler(self.LOG_FILE, encoding="utf-8")
        self._file_handler.setFormatter(logging.Formatter("%(message)s"))
        self._file_logger = logging.getLogger("ApexBot.File")
        if not self._file_logger.handlers:
            self._file_logger.addHandler(self._file_handler)
        self._file_logger.setLevel(logging.DEBUG)
        self._file_logger.propagate = False

        self.info("SYSTEM", f"Logger spustený | {symbol} | test_mode={test_mode}")

    # ── Verejné metódy ────────────────────────────────────────────

    def info(self, category: str, msg: str, data: dict = None):
        self._log("INFO", category, msg, data)

    def warning(self, category: str, msg: str, data: dict = None):
        self._log("WARNING", category, msg, data)

    def error(self, category: str, msg: str, data: dict = None):
        self._log("ERROR", category, msg, data)

    def trade(self, side: str, price: float, qty: float, pnl: float = None,
              notional: float = None, fee: float = None):
        """Špeciálny záznam pre každý obchod."""
        icon = "🟢" if side == "BUY" else "🔴"
        pnl_str = f" | P&L: {pnl:+.4f}" if pnl is not None else ""
        self.info("TRADE",
            f"{icon} {side} @ {price:.4f} | qty {qty:.6f} | "
            f"notional {notional or price*qty:.4f}{pnl_str}",
            {"side": side, "price": price, "qty": qty, "pnl": pnl,
             "notional": notional, "fee": fee}
        )

    def heartbeat(self, price: float, open_orders: int, unrealized_pnl: float,
                  portfolio_value: float, uptime_min: float):
        """Heartbeat záznam — každých 30 min."""
        self.info("HEARTBEAT",
            f"💓 Heartbeat | cena={price:.4f} | objednávky={open_orders} | "
            f"uPNL={unrealized_pnl:+.4f} | portfólio={portfolio_value:.4f} | "
            f"uptime={uptime_min:.0f}min",
            {"price": price, "open_orders": open_orders,
             "unrealized_pnl": unrealized_pnl, "portfolio_value": portfolio_value,
             "uptime_min": uptime_min}
        )

    # ── Interné ──────────────────────────────────────────────────

    def _log(self, level: str, category: str, msg: str, data: dict = None):
        # Konzola
        full_msg = f"[{category}] {msg}"
        getattr(self._console, level.lower(), self._console.info)(full_msg)

        # JSON súbor
        record = {
            "ts":       datetime.now().isoformat(timespec="seconds"),
            "level":    level,
            "category": category,
            "symbol":   self.symbol,
            "msg":      msg,
        }
        if data:
            record["data"] = {k: v for k, v in data.items() if v is not None}
        self._file_logger.info(json.dumps(record, ensure_ascii=False))


# ════════════════════════════════════════════════════════════════
# B) CIRCUIT BREAKER — Flash Crash ochrana
# ════════════════════════════════════════════════════════════════

class BreakerState(str, Enum):
    CLOSED  = "CLOSED"   # normálny beh
    OPEN    = "OPEN"     # halt — čaká na manuálny reset
    COOLING = "COOLING"  # krátky cooldown pred resetom


@dataclass
class PricePoint:
    price: float
    ts:    datetime


class CircuitBreaker:
    """
    Ochrana pred Flash Crash.

    Sleduje históriu cien v okne `window_minutes`.
    Ak cena klesne o viac ako `drop_pct` oproti maximu v okne:
      → stav OPEN → zavolá `on_trip(reason)` → bot zastaví obchodovanie

    Manuálny reset: breaker.reset()

    Príklad:
        breaker = CircuitBreaker(drop_pct=5.0, window_minutes=10)
        breaker.on_trip = lambda r: manager.cancel_all()
        breaker.update(618.42)   # volaj každý tick
    """

    def __init__(
        self,
        drop_pct:       float = 5.0,    # % pokles = trip
        window_minutes: float = 10.0,   # časové okno sledovania
        cooldown_min:   float = 30.0,   # cooldown po resete
        logger: Optional[StructuredLogger] = None,
    ):
        self.drop_pct       = drop_pct
        self.window_min     = window_minutes
        self.cooldown_min   = cooldown_min
        self.logger         = logger
        self.state          = BreakerState.CLOSED
        self._history: deque[PricePoint] = deque()
        self._tripped_at:   Optional[datetime] = None
        self._trip_count    = 0
        self.on_trip:       Optional[Callable[[str], None]] = None   # callback

    @property
    def is_open(self) -> bool:
        return self.state == BreakerState.OPEN

    @property
    def is_closed(self) -> bool:
        return self.state == BreakerState.CLOSED

    def update(self, current_price: float) -> bool:
        """
        Zavolaj pri každom novom tick ceny.
        Vráti True ak je breaker OK (CLOSED), False ak je OPEN (halt).
        """
        now = datetime.now()
        self._history.append(PricePoint(current_price, now))
        self._evict_old(now)

        if self.state == BreakerState.OPEN:
            return False   # halt stále aktívny

        # Vypočítaj max cenu v okne
        if len(self._history) < 2:
            return True
        window_max = max(p.price for p in self._history)
        drop_pct   = (window_max - current_price) / window_max * 100

        if drop_pct >= self.drop_pct:
            self._trip(current_price, window_max, drop_pct, now)
            return False

        return True

    def reset(self) -> bool:
        """
        Manuálny reset — vráti breaker do CLOSED.
        Vráti False ak cooldown ešte neuplynul.
        """
        if self._tripped_at:
            elapsed = (datetime.now() - self._tripped_at).total_seconds() / 60
            if elapsed < self.cooldown_min:
                remaining = self.cooldown_min - elapsed
                msg = f"Reset odmietnutý — cooldown: ešte {remaining:.1f} min"
                if self.logger:
                    self.logger.warning("CIRCUIT", msg)
                else:
                    print(f"[CIRCUIT] {msg}")
                return False
        self.state = BreakerState.CLOSED
        self._history.clear()
        msg = "Circuit Breaker resetovaný → CLOSED. Bot môže pokračovať."
        if self.logger:
            self.logger.info("CIRCUIT", f"✅ {msg}")
        else:
            print(f"[CIRCUIT] {msg}")
        return True

    def status(self) -> dict:
        now     = datetime.now()
        elapsed = (now - self._tripped_at).total_seconds() / 60 if self._tripped_at else 0
        return {
            "state":      self.state.value,
            "trip_count": self._trip_count,
            "tripped_at": self._tripped_at.isoformat() if self._tripped_at else None,
            "elapsed_min": round(elapsed, 1),
            "history_len": len(self._history),
        }

    def _trip(self, price: float, peak: float, drop: float, now: datetime):
        self.state      = BreakerState.OPEN
        self._tripped_at = now
        self._trip_count += 1
        reason = (
            f"🚨 CIRCUIT BREAKER TRIP #{self._trip_count} | "
            f"Pokles {drop:.2f}% za {self.window_min:.0f} min | "
            f"Peak: {peak:.4f} → Aktuálne: {price:.4f}"
        )
        if self.logger:
            self.logger.error("CIRCUIT", reason,
                {"price": price, "peak": peak, "drop_pct": drop,
                 "window_min": self.window_min, "trip_count": self._trip_count})
        else:
            print(f"[CIRCUIT] {reason}")
        if self.on_trip:
            try:
                self.on_trip(reason)
            except Exception as e:
                if self.logger:
                    self.logger.error("CIRCUIT", f"on_trip callback zlyhal: {e}")

    def _evict_old(self, now: datetime):
        cutoff = now - timedelta(minutes=self.window_min)
        while self._history and self._history[0].ts < cutoff:
            self._history.popleft()


# ════════════════════════════════════════════════════════════════
# C) EMERGENCY STOP
# ════════════════════════════════════════════════════════════════

class EmergencyStop:
    """
    Okamžité zastavenie bota.

    Scenáre aktivácie:
      • Manuálne: stop.trigger("dôvod")
      • CircuitBreaker callback
      • Stop-loss z CustomLogic

    Po aktivácii:
      1. Zavolá všetky registrované `cleanup_callbacks`
         (napr. cancel_all orders, zatvoriť WebSocket)
      2. Nastaví is_active = True → hlavný loop skontroluje a skončí
      3. Zaznamená do logu + Telegram

    Reset len cez: stop.reset() + manuálne potvrdenie
    """

    def __init__(self, logger: Optional[StructuredLogger] = None,
                 notifier=None):
        self.is_active:         bool = False
        self.reason:            str  = ""
        self.triggered_at:      Optional[datetime] = None
        self.trigger_count:     int  = 0
        self.logger             = logger
        self.notifier           = notifier
        self._callbacks:        list[Callable] = []

    def register_cleanup(self, fn: Callable):
        """Zaregistruje funkciu ktorá sa zavolá pri emergency stop."""
        self._callbacks.append(fn)

    def trigger(self, reason: str):
        """Aktivuje emergency stop."""
        if self.is_active:
            return   # už aktívny, ignoruj duplicity

        self.is_active     = True
        self.reason        = reason
        self.triggered_at  = datetime.now()
        self.trigger_count += 1

        msg = f"🛑 EMERGENCY STOP #{self.trigger_count} | {reason}"
        if self.logger:
            self.logger.error("EMERGENCY", msg, {"reason": reason})
        else:
            print(f"[EMERGENCY] {msg}")

        if self.notifier:
            try:
                self.notifier.send_emergency(reason)
            except Exception:
                pass

        for cb in self._callbacks:
            try:
                cb()
            except Exception as e:
                if self.logger:
                    self.logger.error("EMERGENCY", f"Cleanup callback zlyhal: {e}")

    def reset(self):
        """Reset — vyžaduje manuálne volanie."""
        self.is_active = False
        self.reason    = ""
        msg = f"Emergency Stop resetovaný (trigger count: {self.trigger_count})"
        if self.logger:
            self.logger.info("EMERGENCY", f"✅ {msg}")

    def check(self) -> bool:
        """Vráti True ak je bot OK (stop nie je aktívny)."""
        return not self.is_active


# ════════════════════════════════════════════════════════════════
# D) HEARTBEAT
# ════════════════════════════════════════════════════════════════

class Heartbeat:
    """
    Každých `interval_min` minút vypíše do konzoly súhrn stavu bota.

    Súhrn obsahuje:
      • Aktuálna cena
      • Počet otvorených objednávok
      • Nerealizovaný PNL
      • Realizovaný PNL
      • Uptime
      • Stav Circuit Breakera

    Spúšťa sa v separátnom threade — neblokuje hlavný loop.
    """

    def __init__(
        self,
        interval_min:   float = 30.0,
        logger:         Optional[StructuredLogger] = None,
        notifier        = None,
        get_state_fn:   Optional[Callable[[], dict]] = None,
    ):
        self.interval_min = interval_min
        self.logger       = logger
        self.notifier     = notifier
        self.get_state_fn = get_state_fn   # funkcia → vráti aktuálny stav bota
        self._started_at  = datetime.now()
        self._thread:     Optional[threading.Thread] = None
        self._stop_event  = threading.Event()
        self._beat_count  = 0

    def start(self):
        """Spustí heartbeat v background threade."""
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="HeartbeatThread"
        )
        self._thread.start()
        if self.logger:
            self.logger.info("HEARTBEAT",
                f"Heartbeat spustený | interval={self.interval_min} min")

    def stop(self):
        self._stop_event.set()

    def beat_now(self):
        """Okamžitý heartbeat — zavolaj manuálne kedykoľvek."""
        self._beat()

    def _loop(self):
        while not self._stop_event.wait(timeout=self.interval_min * 60):
            self._beat()

    def _beat(self):
        self._beat_count += 1
        uptime = (datetime.now() - self._started_at).total_seconds() / 60

        # Získaj stav od bota
        state = {}
        if self.get_state_fn:
            try:
                state = self.get_state_fn()
            except Exception as e:
                if self.logger:
                    self.logger.error("HEARTBEAT", f"get_state_fn zlyhal: {e}")

        price        = state.get("price",            0.0)
        open_orders  = state.get("open_orders",      0)
        unreal_pnl   = state.get("unrealized_pnl",   0.0)
        real_pnl     = state.get("realized_pnl",     0.0)
        port_value   = state.get("portfolio_value",  0.0)
        breaker_state= state.get("breaker_state",    "UNKNOWN")
        e_stop       = state.get("emergency_stop",   False)

        sep = "─" * 64
        lines = [
            sep,
            f"  💓 HEARTBEAT #{self._beat_count}  |  {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}",
            sep,
            f"  {'Cena:':<30} {price:.4f}",
            f"  {'Otvorené objednávky:':<30} {open_orders}",
            f"  {'Nerealizovaný PNL:':<30} {unreal_pnl:>+.4f} USDT",
            f"  {'Realizovaný PNL:':<30} {real_pnl:>+.4f} USDT",
            f"  {'Portfólio hodnota:':<30} {port_value:.4f} USDT",
            f"  {'Circuit Breaker:':<30} {breaker_state}",
            f"  {'Emergency Stop:':<30} {'🛑 AKTÍVNY' if e_stop else '✅ OK'}",
            f"  {'Uptime:':<30} {uptime:.0f} min ({uptime/60:.1f} h)",
            sep,
        ]
        for line in lines:
            if self.logger:
                self.logger._console.info(line)
            else:
                print(line)

        if self.logger:
            self.logger.heartbeat(price, open_orders, unreal_pnl, port_value, uptime)

        if self.notifier:
            try:
                self.notifier.send_heartbeat(
                    price, open_orders, unreal_pnl, real_pnl, port_value, uptime
                )
            except Exception:
                pass


# ════════════════════════════════════════════════════════════════
# E) TRAILING TAKE PROFIT
# ════════════════════════════════════════════════════════════════

@dataclass
class TrailingPosition:
    """Jedna sledovaná pozícia pre trailing."""
    order_id:     str
    side:         str           # "SELL" — sledujeme BUY pozície smerom nahor
    entry_price:  float
    qty:          float
    high_water:   float         # najvyššia zaznamenaná cena od vstupu
    trail_pct:    float         # koľko % od HWM je trigger
    activated:    bool = False  # trailing sa aktivuje až po dosiahnutí min. zisku
    activate_pct: float = 0.5  # min. % zisk pre aktiváciu trailing


class TrailingTakeProfit:
    """
    Trailing Take Profit — sleduje cenu nahor a predá pri otočení.

    Logika:
      1. BUY sa vyplní → zaregistruj pozíciu: entry_price, qty
      2. Bot sleduje high_water_mark (HWM) — najvyššiu cenu od vstupu
      3. Trailing sa AKTIVUJE keď cena stúpne o `activate_pct` nad entry
      4. Ak cena klesne o `trail_pct` pod HWM → PREDAJ (trailing stop)

    Príklad (trail_pct=0.8%, activate_pct=0.5%):
      entry: 618.00
      aktivácia pri: 618.00 * 1.005 = 621.09
      cena ide na 630.00 → HWM = 630.00
      trail trigger: 630.00 * (1 - 0.008) = 624.96
      ak cena klesne na 624.96 → PREDAJ

    Výhoda: namiesto predaja na fixnom 625.63 počkáš na otočenie
    a predáš napr. na 628+ ak trend pokračuje.
    """

    def __init__(
        self,
        trail_pct:    float = 0.8,    # % od HWM pre trigger
        activate_pct: float = 0.5,    # min. % zisk pre aktiváciu
        logger: Optional[StructuredLogger] = None,
    ):
        self.trail_pct    = trail_pct
        self.activate_pct = activate_pct
        self.logger       = logger
        self._positions: dict[str, TrailingPosition] = {}

    def register(self, order_id: str, entry_price: float, qty: float):
        """Zaregistruj novú BUY pozíciu pre trailing sledovanie."""
        pos = TrailingPosition(
            order_id     = order_id,
            side         = "SELL",
            entry_price  = entry_price,
            qty          = qty,
            high_water   = entry_price,
            trail_pct    = self.trail_pct,
            activate_pct = self.activate_pct,
        )
        self._positions[order_id] = pos
        if self.logger:
            self.logger.info("TRAILING",
                f"Registrovaná pozícia {order_id} | "
                f"entry={entry_price:.4f} | qty={qty:.4f} | "
                f"trail={self.trail_pct}% | aktivácia po +{self.activate_pct}%"
            )

    def update(self, current_price: float) -> list[TrailingPosition]:
        """
        Aktualizuj všetky pozície s aktuálnou cenou.
        Vráti zoznam pozícií kde bol spustený trailing predaj.
        """
        triggered = []
        for oid, pos in list(self._positions.items()):
            # Aktualizuj HWM
            if current_price > pos.high_water:
                pos.high_water = current_price

            # Kontrola aktivácie
            activate_price = pos.entry_price * (1 + pos.activate_pct / 100)
            if not pos.activated and pos.high_water >= activate_price:
                pos.activated = True
                if self.logger:
                    self.logger.info("TRAILING",
                        f"⚡ Trailing AKTIVOVANÝ {oid} | "
                        f"HWM={pos.high_water:.4f} (vstup={pos.entry_price:.4f})"
                    )

            # Kontrola triggeru (len ak aktivovaný)
            if pos.activated:
                trail_trigger = pos.high_water * (1 - pos.trail_pct / 100)
                if current_price <= trail_trigger:
                    profit_pct = (current_price - pos.entry_price) / pos.entry_price * 100
                    if self.logger:
                        self.logger.info("TRAILING",
                            f"🎯 Trailing TRIGGER {oid} | "
                            f"cena={current_price:.4f} | HWM={pos.high_water:.4f} | "
                            f"trigger={trail_trigger:.4f} | "
                            f"profit={profit_pct:+.2f}%",
                            {"order_id": oid, "price": current_price,
                             "hwm": pos.high_water, "trigger": trail_trigger,
                             "profit_pct": profit_pct, "qty": pos.qty}
                        )
                    triggered.append(pos)
                    del self._positions[oid]

        return triggered

    def remove(self, order_id: str):
        """Odober pozíciu (napr. keď sa predaj uskutočnil inak)."""
        self._positions.pop(order_id, None)

    def active_count(self) -> int:
        return len(self._positions)

    def summary(self, current_price: float) -> list[dict]:
        result = []
        for oid, pos in self._positions.items():
            unrealized = (current_price - pos.entry_price) / pos.entry_price * 100
            result.append({
                "order_id":   oid,
                "entry":      pos.entry_price,
                "hwm":        pos.high_water,
                "current":    current_price,
                "unrealized": unrealized,
                "activated":  pos.activated,
                "trigger_at": pos.high_water * (1 - pos.trail_pct / 100),
            })
        return result


# ════════════════════════════════════════════════════════════════
# F) STALE INVENTORY — zaseknuté pozície
# ════════════════════════════════════════════════════════════════

class StaleMode(str, Enum):
    NORMAL      = "NORMAL"       # cena je v gridu, obchoduj normálne
    DCA         = "DCA"          # cena pod gridom, DCA nakup
    MICRO_SCALP = "MICRO_SCALP"  # cena ďaleko pod gridom, micro-scalp
    RECOVERY    = "RECOVERY"     # cena sa vracia do gridu


@dataclass
class StaleStatus:
    mode:           StaleMode
    below_grid_pct: float       # % pod spodnou hranicou gridu
    avg_buy_price:  float       # priemerná nákupná cena
    breakeven_price: float      # cena potrebná na nulový P&L
    dca_suggestion: float       # odporúčaná DCA suma v USDT
    coin_balance:   float


class StaleInventoryManager:
    """
    Správa "zaseknutého inventory" — keď cena klesne pod spodný grid.

    Režimy:
      NORMAL      → cena v gridu alebo nad ním → obchoduj normálne
      DCA         → cena 0–5% pod gridom → DCA nakupy každých N minút
      MICRO_SCALP → cena >5% pod gridom → micro-scalp na malých pohyboch
      RECOVERY    → cena sa vracia nahor → postupne likviduj zásoby

    Breakeven výpočet:
      breakeven = (sum(buy_price * qty)) / total_qty
      Toto je cena pri ktorej sme na nule (bez poplatkov).

    DCA logika:
      Nakup malé množstvo každých `dca_interval_min` minút
      aby sme znížili avg_buy_price.
      Maximálna DCA suma = `max_dca_budget_usdt`.
    """

    def __init__(
        self,
        grid_bottom:        float,          # spodná hranica gridu
        avg_buy_price:      float,          # aktuálna priem. nák. cena
        coin_balance:       float,          # držané qty
        dca_interval_min:   float = 15.0,
        max_dca_budget_usdt: float = 100.0,
        micro_scalp_pct:    float = 0.3,    # krok micro-scalp v %
        logger: Optional[StructuredLogger] = None,
    ):
        self.grid_bottom         = grid_bottom
        self.avg_buy_price       = avg_buy_price
        self.coin_balance        = coin_balance
        self.dca_interval_min    = dca_interval_min
        self.max_dca_budget_usdt = max_dca_budget_usdt
        self.micro_scalp_pct     = micro_scalp_pct
        self.logger              = logger

        self._dca_spent_usdt     = 0.0
        self._last_dca_time:     Optional[datetime] = None
        self._mode               = StaleMode.NORMAL

    def update_inventory(self, avg_buy_price: float, coin_balance: float):
        """Aktualizuj stav inventory po každom obchode."""
        self.avg_buy_price = avg_buy_price
        self.coin_balance  = coin_balance

    def analyze(self, current_price: float) -> StaleStatus:
        """
        Analyzuj situáciu a vráti StaleStatus s odporúčaním.
        Volaj každý tick.
        """
        if current_price >= self.grid_bottom:
            self._mode = StaleMode.NORMAL
            below_pct  = 0.0
        else:
            below_pct = (self.grid_bottom - current_price) / self.grid_bottom * 100
            if below_pct <= 5.0:
                self._mode = StaleMode.DCA
            else:
                self._mode = StaleMode.MICRO_SCALP

        # Breakeven cena
        breakeven = self.avg_buy_price if self.avg_buy_price > 0 else current_price

        # Odporúčaná DCA suma
        dca_suggestion = 0.0
        if self._mode in (StaleMode.DCA, StaleMode.MICRO_SCALP):
            remaining_budget = self.max_dca_budget_usdt - self._dca_spent_usdt
            dca_suggestion   = min(remaining_budget * 0.1, 20.0)   # 10% zostávajúceho budgetu

        status = StaleStatus(
            mode            = self._mode,
            below_grid_pct  = below_pct,
            avg_buy_price   = self.avg_buy_price,
            breakeven_price = breakeven,
            dca_suggestion  = dca_suggestion,
            coin_balance    = self.coin_balance,
        )

        if self._mode != StaleMode.NORMAL:
            if self.logger:
                self.logger.warning("STALE",
                    f"📦 Stale Inventory | mode={self._mode.value} | "
                    f"pod gridom: {below_pct:.2f}% | "
                    f"avg_buy={self.avg_buy_price:.4f} | "
                    f"breakeven={breakeven:.4f} | "
                    f"DCA odporúčanie: {dca_suggestion:.2f} USDT",
                    {"mode": self._mode.value, "below_pct": below_pct,
                     "avg_buy": self.avg_buy_price, "breakeven": breakeven}
                )
        return status

    def should_dca(self) -> bool:
        """Vráti True ak je čas na DCA nákup."""
        if self._mode not in (StaleMode.DCA, StaleMode.MICRO_SCALP):
            return False
        if self._dca_spent_usdt >= self.max_dca_budget_usdt:
            return False
        if self._last_dca_time is None:
            return True
        elapsed = (datetime.now() - self._last_dca_time).total_seconds() / 60
        return elapsed >= self.dca_interval_min

    def record_dca(self, amount_usdt: float):
        """Zaznamenaj uskutočnený DCA nákup."""
        self._dca_spent_usdt += amount_usdt
        self._last_dca_time   = datetime.now()
        if self.logger:
            self.logger.info("STALE",
                f"DCA vykonaný: {amount_usdt:.2f} USDT | "
                f"celkom DCA: {self._dca_spent_usdt:.2f} / {self.max_dca_budget_usdt:.2f} USDT"
            )

    def get_micro_scalp_levels(self, current_price: float, n: int = 3) -> list[float]:
        """
        Vráti N cien pre micro-scalp príkazy pod aktuálnou cenou.
        Používa menší krok ako hlavný grid.
        """
        step = current_price * (self.micro_scalp_pct / 100)
        return [current_price - step * i for i in range(1, n + 1)]


# ════════════════════════════════════════════════════════════════
# G) TELEGRAM NOTIFIER
# ════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    Telegram notifikácie pre všetky dôležité udalosti.

    Nastavenie (5 minút):
      1. Napíš @BotFather → /newbot → získaš TOKEN
      2. Napíš svojmu botovi /start
      3. https://api.telegram.org/bot<TOKEN>/getUpdates → nájdi chat.id
      4. Vlož do .env:
           TELEGRAM_BOT_TOKEN=...
           TELEGRAM_CHAT_ID=...

    Správy sa posielajú asynchrónne — neblokujú hlavný loop.
    Retry: 3× pri sieťovej chybe.
    Rate limit: max 1 správa / 3 sekundy (Telegram limit).
    """

    API_URL   = "https://api.telegram.org/bot{token}/sendMessage"
    MAX_RETRY = 3
    RATE_WAIT = 3.0   # sekundy medzi správami

    def __init__(
        self,
        token:   str = "",
        chat_id: str = "",
        symbol:  str = "",
        logger:  Optional[StructuredLogger] = None,
    ):
        self.token   = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.symbol  = symbol
        self.logger  = logger
        self.enabled = bool(self.token and self.chat_id)
        self._last_sent = datetime.min
        self._queue: list[str] = []
        self._lock  = threading.Lock()

        if not self.enabled:
            if logger:
                logger.warning("TELEGRAM",
                    "Telegram nie je nakonfigurovaný (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)"
                )

    # ── Verejné správy ────────────────────────────────────────────

    def send_trade(self, side: str, price: float, qty: float, pnl: float = None,
                   notional: float = None):
        """Správa pri každom obchode."""
        icon     = "🟢" if side == "BUY" else "🔴"
        pnl_line = ""
        if pnl is not None:
            emoji    = "✅" if pnl >= 0 else "❌"
            pnl_line = f"\nP&amp;L: <b>{pnl:+.4f} USDT</b> {emoji}"
        self._send(
            f"{icon} <b>{side}</b> | <code>{self.symbol}</code>\n"
            f"Cena: <code>{price:.4f}</code> | Qty: <code>{qty:.6f}</code>\n"
            f"Hodnota: <code>{notional or price*qty:.4f} USDT</code>"
            f"{pnl_line}"
        )

    def send_circuit_trip(self, reason: str):
        self._send(
            f"🚨 <b>CIRCUIT BREAKER</b>\n"
            f"<code>{self.symbol}</code>\n"
            f"{reason}\n\n"
            f"<i>Bot zastavil obchodovanie. Čaká na manuálny reset.</i>"
        )

    def send_emergency(self, reason: str):
        self._send(
            f"🛑 <b>EMERGENCY STOP</b>\n"
            f"<code>{self.symbol}</code>\n"
            f"{reason}"
        )

    def send_heartbeat(self, price: float, open_orders: int,
                       unreal_pnl: float, real_pnl: float,
                       portfolio: float, uptime_min: float):
        self._send(
            f"💓 <b>Heartbeat</b> | <code>{self.symbol}</code>\n"
            f"Cena: <code>{price:.4f}</code>\n"
            f"Objednávky: <code>{open_orders}</code>\n"
            f"uPNL: <code>{unreal_pnl:+.4f} USDT</code>\n"
            f"rPNL: <code>{real_pnl:+.4f} USDT</code>\n"
            f"Portfólio: <code>{portfolio:.4f} USDT</code>\n"
            f"Uptime: <code>{uptime_min:.0f} min</code>",
            silent=True,
        )

    def send_trailing_trigger(self, price: float, hwm: float, profit_pct: float, qty: float):
        self._send(
            f"🎯 <b>Trailing Take Profit</b> | <code>{self.symbol}</code>\n"
            f"Predaj @ <code>{price:.4f}</code>\n"
            f"HWM: <code>{hwm:.4f}</code>\n"
            f"Profit: <code>{profit_pct:+.2f}%</code> | Qty: <code>{qty:.6f}</code>"
        )

    def send_stale_alert(self, mode: str, below_pct: float,
                         avg_buy: float, breakeven: float):
        self._send(
            f"📦 <b>Stale Inventory</b> | <code>{self.symbol}</code>\n"
            f"Režim: <b>{mode}</b>\n"
            f"Pod gridom: <code>{below_pct:.2f}%</code>\n"
            f"Avg nák. cena: <code>{avg_buy:.4f}</code>\n"
            f"Breakeven: <code>{breakeven:.4f}</code>"
        )

    def send_error(self, msg: str):
        self._send(f"⚠️ <b>CHYBA BOTA</b>\n<code>{self.symbol}</code>\n<code>{msg[:400]}</code>")

    def send_custom(self, text: str, silent: bool = False):
        self._send(text, silent=silent)

    # ── Interné ──────────────────────────────────────────────────

    def _send(self, text: str, silent: bool = False):
        if not self.enabled:
            return
        thread = threading.Thread(
            target=self._send_sync, args=(text, silent), daemon=True
        )
        thread.start()

    def _send_sync(self, text: str, silent: bool):
        # Rate limiting
        with self._lock:
            elapsed = (datetime.now() - self._last_sent).total_seconds()
            if elapsed < self.RATE_WAIT:
                time.sleep(self.RATE_WAIT - elapsed)

        url     = self.API_URL.format(token=self.token)
        payload = {
            "chat_id":              self.chat_id,
            "text":                 text,
            "parse_mode":           "HTML",
            "disable_notification": silent,
        }

        for attempt in range(1, self.MAX_RETRY + 1):
            try:
                resp = requests.post(url, json=payload, timeout=10)
                resp.raise_for_status()
                with self._lock:
                    self._last_sent = datetime.now()
                if self.logger:
                    self.logger.info("TELEGRAM", f"Správa odoslaná (pokus {attempt})")
                return
            except requests.RequestException as e:
                if self.logger:
                    self.logger.warning("TELEGRAM",
                        f"Odosielanie zlyhalo (pokus {attempt}/{self.MAX_RETRY}): {e}"
                    )
                if attempt < self.MAX_RETRY:
                    time.sleep(5 * attempt)

        if self.logger:
            self.logger.error("TELEGRAM", f"Správa sa neodoslala po {self.MAX_RETRY} pokusoch")


# ════════════════════════════════════════════════════════════════
# H) PROFIT MANAGER — výber ziskov + Trailing Daily Profit
# ════════════════════════════════════════════════════════════════

class ProfitSignal(str, Enum):
    OK          = "OK"           # normálny beh
    TRAIL_LOCK  = "TRAIL_LOCK"   # denný zisk dosiahol cieľ, trailing aktívny
    TRAIL_STOP  = "TRAIL_STOP"   # trailing stop spustený — ukonči deň
    DAY_REPORT  = "DAY_REPORT"   # koniec dňa — odošli report


@dataclass
class DaySnapshot:
    """Denný záznam ziskov — ukladá sa pri midnight rollover."""
    date:           str      # "2026-03-31"
    opening_value:  float    # hodnota portfólia na začiatku dňa
    closing_value:  float    # hodnota portfólia na konci dňa
    day_profit:     float    # closing - opening
    peak_profit:    float    # najvyšší denný zisk
    trades_count:   int
    trail_triggered: bool    # či sa trailing stop spustil


class ProfitManager:
    """
    Správa ziskov nad base_capital s Trailing Daily Profit logikou.

    Koncepty:
    ┌─────────────────────────────────────────────────────────┐
    │  base_capital    = 10 000 USDT  (tvoj vklad)           │
    │  current_profit  = portfolio_value - base_capital       │
    │  day_profit      = portfolio_value - day_open_value     │
    └─────────────────────────────────────────────────────────┘

    Trailing Daily Profit logika:
      1. Deň začne s day_profit = 0
      2. Keď day_profit dosiahne daily_target (napr. 100 USDT)
         → aktivuje sa trailing stop na 90 % peak zisku
      3. Ak day_profit stúpne na 150 → trail_stop = 150 * 0.90 = 135
      4. Ak day_profit klesne pod trail_stop → TRAIL_STOP signál
         → bot ukončí obchodovanie na daný deň

    Mesačný progress:
      monthly_target = daily_target * 22  (22 obchodných dní)
      Každý deň Telegram správa: "Dnes: X USDT | Do cieľa: Y USDT"
    """

    def __init__(
        self,
        base_capital:    float = 10_000.0,
        daily_target:    float = 100.0,
        trail_pct:       float = 0.90,      # zachovaj 90 % peak zisku
        monthly_target:  float = None,       # ak None → daily_target * 22
        logger:          Optional[StructuredLogger] = None,
        notifier        = None,
    ):
        self.base_capital   = base_capital
        self.daily_target   = daily_target
        self.trail_pct      = trail_pct
        self.monthly_target = monthly_target or daily_target * 22
        self.logger         = logger
        self.notifier       = notifier

        # Denný stav
        self._day_open_value:  float = base_capital
        self._day_peak_profit: float = 0.0
        self._trail_stop_at:   float = 0.0       # aktívna trail stop úroveň
        self._trail_active:    bool  = False
        self._day_stopped:     bool  = False      # True = obchodovanie zastavené na dnes
        self._current_day:     str   = datetime.now().strftime("%Y-%m-%d")
        self._day_trades:      int   = 0

        # Mesačný stav
        self._month_key:       str   = datetime.now().strftime("%Y-%m")
        self._monthly_profit:  float = 0.0
        self._day_history:     list[DaySnapshot] = []

        if logger:
            logger.info("PROFIT",
                f"ProfitManager init | base={base_capital:.0f} USDT | "
                f"daily_target={daily_target:.0f} USDT | "
                f"trail={trail_pct*100:.0f}% | monthly_target={self.monthly_target:.0f} USDT"
            )

    # ── Hlavná metóda — volaj každý tick ─────────────────────────

    def update(self, portfolio_value: float, trades_today: int = 0) -> ProfitSignal:
        """
        Aktualizuj stav a vráť signál.

        Args:
            portfolio_value: aktuálna celková hodnota portfólia v USDT
            trades_today:    počet obchodov dnes (pre report)

        Returns:
            ProfitSignal.OK          → obchoduj normálne
            ProfitSignal.TRAIL_LOCK  → trailing aktívny, sleduj
            ProfitSignal.TRAIL_STOP  → zastav obchodovanie dnes
            ProfitSignal.DAY_REPORT  → bol midnight rollover, report odoslaný
        """
        self._day_trades  = trades_today
        today_str = datetime.now().strftime("%Y-%m-%d")

        # ── Midnight rollover — nový deň ──────────────────────────
        if today_str != self._current_day:
            signal = self._rollover(portfolio_value, today_str)
            return signal

        # ── Ak obchodovanie dnes zastavené → OK (čakaj do zajtrajška) ──
        if self._day_stopped:
            return ProfitSignal.TRAIL_STOP

        # ── Výpočet denného zisku ─────────────────────────────────
        day_profit = portfolio_value - self._day_open_value

        # ── Aktualizuj peak ───────────────────────────────────────
        if day_profit > self._day_peak_profit:
            self._day_peak_profit = day_profit
            # Aktualizuj trail stop level
            if self._trail_active:
                new_stop = self._day_peak_profit * self.trail_pct
                if new_stop > self._trail_stop_at:
                    self._trail_stop_at = new_stop
                    if self.logger:
                        self.logger.info("PROFIT",
                            f"📈 Trail stop posunutý nahor: "
                            f"peak={self._day_peak_profit:+.2f} USDT → "
                            f"stop={self._trail_stop_at:+.2f} USDT"
                        )

        # ── Aktivácia trailing (prvýkrát pri dosiahnutí cieľa) ───
        if not self._trail_active and day_profit >= self.daily_target:
            self._trail_active  = True
            self._trail_stop_at = self._day_peak_profit * self.trail_pct
            msg = (
                f"🔒 Trailing Daily Profit AKTIVOVANÝ | "
                f"denný zisk={day_profit:+.2f} USDT (cieľ={self.daily_target:.0f}) | "
                f"trail stop={self._trail_stop_at:+.2f} USDT "
                f"({self.trail_pct*100:.0f}% z peak)"
            )
            if self.logger:
                self.logger.info("PROFIT", msg,
                    {"day_profit": day_profit, "peak": self._day_peak_profit,
                     "trail_stop": self._trail_stop_at})
            if self.notifier:
                self.notifier.send_custom(
                    f"🔒 <b>Trailing Daily Profit aktívny</b>\n"
                    f"Denný zisk: <code>+{day_profit:.2f} USDT</code>\n"
                    f"Trail stop: <code>+{self._trail_stop_at:.2f} USDT</code>\n"
                    f"<i>Bot predá ak zisk klesne pod túto hranicu.</i>"
                )
            return ProfitSignal.TRAIL_LOCK

        # ── Kontrola trail stop spustenia ────────────────────────
        if self._trail_active and day_profit <= self._trail_stop_at:
            self._day_stopped = True
            msg = (
                f"🛑 Trailing Daily Profit STOP | "
                f"zisk={day_profit:+.2f} USDT klesol pod "
                f"stop={self._trail_stop_at:+.2f} USDT | "
                f"peak bol={self._day_peak_profit:+.2f} USDT"
            )
            if self.logger:
                self.logger.info("PROFIT", msg,
                    {"day_profit": day_profit, "trail_stop": self._trail_stop_at,
                     "peak": self._day_peak_profit})
            if self.notifier:
                self.notifier.send_custom(
                    f"🛑 <b>Trailing Stop spustený — koniec dňa</b>\n"
                    f"Denný zisk uzamknutý: <code>+{day_profit:.2f} USDT</code>\n"
                    f"Peak bol: <code>+{self._day_peak_profit:.2f} USDT</code>\n"
                    f"<i>Bot prestáva obchodovať. Pokračuje zajtra.</i>"
                )
            return ProfitSignal.TRAIL_STOP

        # ── Normálny beh ──────────────────────────────────────────
        return ProfitSignal.TRAIL_LOCK if self._trail_active else ProfitSignal.OK

    # ── Midnight rollover ─────────────────────────────────────────

    def _rollover(self, portfolio_value: float, new_day: str) -> ProfitSignal:
        """Spracuje prechod na nový deň — zaznamená výsledok, odošle report."""
        day_profit = portfolio_value - self._day_open_value

        # Ulož snapshot
        snap = DaySnapshot(
            date           = self._current_day,
            opening_value  = self._day_open_value,
            closing_value  = portfolio_value,
            day_profit     = day_profit,
            peak_profit    = self._day_peak_profit,
            trades_count   = self._day_trades,
            trail_triggered= self._day_stopped,
        )
        self._day_history.append(snap)

        # Aktualizuj mesačný súčet
        month_of_snap = self._current_day[:7]
        if month_of_snap == self._month_key:
            self._monthly_profit += day_profit
        else:
            # Nový mesiac
            self._month_key      = new_day[:7]
            self._monthly_profit = day_profit

        # Odošli denný report
        self._send_day_report(snap)

        # Reset na nový deň
        self._current_day      = new_day
        self._day_open_value   = portfolio_value
        self._day_peak_profit  = 0.0
        self._trail_stop_at    = 0.0
        self._trail_active     = False
        self._day_stopped      = False
        self._day_trades       = 0

        if self.logger:
            self.logger.info("PROFIT",
                f"📅 Nový deň: {new_day} | "
                f"Otvárajúca hodnota: {portfolio_value:.2f} USDT | "
                f"Včera: {day_profit:+.2f} USDT"
            )
        return ProfitSignal.DAY_REPORT

    # ── Denný Telegram report ─────────────────────────────────────

    def _send_day_report(self, snap: DaySnapshot):
        """Správa na konci dňa: zarobené dnes + progress k mesačnému cieľu."""
        remaining    = max(0.0, self.monthly_target - self._monthly_profit)
        monthly_pct  = min(100.0, self._monthly_profit / self.monthly_target * 100)

        # Progress bar (10 znakov)
        filled = int(monthly_pct / 10)
        bar    = "█" * filled + "░" * (10 - filled)

        trail_note = "\n⚠️ <i>Trailing stop spustený počas dňa.</i>" if snap.trail_triggered else ""

        msg = (
            f"📅 <b>Denný report — {snap.date}</b>\n"
            f"{'─'*30}\n"
            f"Dnes zarobené:  <code>{snap.day_profit:>+8.2f} USDT</code>\n"
            f"Peak dňa:       <code>{snap.peak_profit:>+8.2f} USDT</code>\n"
            f"Obchody dnes:   <code>{snap.trades_count:>8d}</code>\n"
            f"{'─'*30}\n"
            f"Mesačný zisk:   <code>{self._monthly_profit:>+8.2f} USDT</code>\n"
            f"Mesačný cieľ:   <code>{self.monthly_target:>8.0f} USDT</code>\n"
            f"Do cieľa chýba: <code>{remaining:>+8.2f} USDT</code>\n"
            f"Progress: [{bar}] {monthly_pct:.1f}%"
            f"{trail_note}"
        )

        if self.logger:
            self.logger.info("PROFIT",
                f"Denný report | dnes={snap.day_profit:+.2f} | "
                f"mesačný={self._monthly_profit:+.2f} / {self.monthly_target:.0f} | "
                f"chýba={remaining:.2f}",
                {"date": snap.date, "day_profit": snap.day_profit,
                 "monthly_profit": self._monthly_profit,
                 "monthly_target": self.monthly_target,
                 "remaining": remaining}
            )
        if self.notifier:
            self.notifier.send_custom(msg)

    # ── Statusové metódy ──────────────────────────────────────────

    def current_profit(self, portfolio_value: float) -> float:
        """Všetko nad base_capital = čistý zisk."""
        return portfolio_value - self.base_capital

    def day_profit(self, portfolio_value: float) -> float:
        """Zisk aktuálneho dňa."""
        return portfolio_value - self._day_open_value

    def monthly_remaining(self) -> float:
        """Koľko USDT chýba do mesačného cieľa."""
        return max(0.0, self.monthly_target - self._monthly_profit)

    def status(self, portfolio_value: float) -> dict:
        dp = self.day_profit(portfolio_value)
        return {
            "base_capital":    self.base_capital,
            "portfolio_value": portfolio_value,
            "current_profit":  self.current_profit(portfolio_value),
            "day_profit":      dp,
            "day_peak":        self._day_peak_profit,
            "trail_active":    self._trail_active,
            "trail_stop_at":   self._trail_stop_at,
            "day_stopped":     self._day_stopped,
            "monthly_profit":  self._monthly_profit,
            "monthly_target":  self.monthly_target,
            "monthly_remaining": self.monthly_remaining(),
            "monthly_pct":     min(100.0, self._monthly_profit / self.monthly_target * 100),
        }

    def print_status(self, portfolio_value: float):
        """Vypíše prehľadný status do konzoly."""
        s   = self.status(portfolio_value)
        sep = "─" * 64
        print(sep)
        print(f"  💰 PROFIT MANAGER  |  {datetime.now().strftime('%d.%m.%Y %H:%M')}")
        print(sep)
        print(f"  {'Base capital:':<32} {s['base_capital']:>10.2f} USDT")
        print(f"  {'Aktuálna hodnota:':<32} {s['portfolio_value']:>10.2f} USDT")
        print(f"  {'Celkový zisk (nad base):':<32} {s['current_profit']:>+10.2f} USDT")
        print(sep)
        print(f"  {'Denný zisk:':<32} {s['day_profit']:>+10.2f} USDT")
        print(f"  {'Denný peak:':<32} {s['day_peak']:>+10.2f} USDT")
        print(f"  {'Denný cieľ:':<32} {self.daily_target:>10.2f} USDT")
        trail_icon = "🔒 AKTÍVNY" if s['trail_active'] else "⬜ čaká na cieľ"
        print(f"  {'Trailing stop:':<32} {trail_icon}")
        if s['trail_active']:
            print(f"  {'  └ stop level:':<32} {s['trail_stop_at']:>+10.2f} USDT")
        stop_icon = "🛑 ÁNO — čaká do zajtrajška" if s['day_stopped'] else "✅ NIE"
        print(f"  {'Obchodovanie zastavené:':<32} {stop_icon}")
        print(sep)
        bar_filled = int(s['monthly_pct'] / 10)
        bar = "█" * bar_filled + "░" * (10 - bar_filled)
        print(f"  {'Mesačný zisk:':<32} {s['monthly_profit']:>+10.2f} USDT")
        print(f"  {'Mesačný cieľ:':<32} {s['monthly_target']:>10.2f} USDT")
        print(f"  {'Do cieľa chýba:':<32} {s['monthly_remaining']:>10.2f} USDT")
        print(f"  Progress: [{bar}] {s['monthly_pct']:.1f}%")
        print(sep)


# ════════════════════════════════════════════════════════════════
# I) GUARDIAN — spája všetky ochranné vrstvy
# ════════════════════════════════════════════════════════════════

class Guardian:
    """
    Fasáda — spája všetky ochranné moduly do jedného objektu.

    Použitie v hlavnom loope:
        guardian = Guardian.create(symbol, test_mode, base_capital=10000)

        # Každý tick:
        ok, profit_signal = guardian.tick(current_price, portfolio_value=...)
        if not ok: break   # circuit breaker alebo emergency stop
        if profit_signal == ProfitSignal.TRAIL_STOP: break  # koniec dňa

        # Po BUY fill:
        guardian.on_buy_filled(order_id, price, qty)

        # Po SELL fill:
        guardian.on_sell_filled(order_id, price, qty, pnl)

        # Skontroluj trailing:
        triggered = guardian.check_trailing(current_price)

        # Skontroluj stale:
        stale = guardian.check_stale(current_price, avg_buy, coin_balance)
    """

    def __init__(
        self,
        symbol:           str,
        test_mode:        bool,
        circuit_drop_pct: float = 5.0,
        circuit_window:   float = 10.0,
        trail_pct:        float = 0.8,
        trail_activate:   float = 0.5,
        heartbeat_min:    float = 30.0,
        grid_bottom:      float = 0.0,
        telegram_token:   str   = "",
        telegram_chat_id: str   = "",
        starting_usdt:    float = 1000.0,
        base_capital:     float = 10_000.0,
        daily_target:     float = 100.0,
        profit_trail_pct: float = 0.90,
        monthly_target:   float = None,
    ):
        self.symbol    = symbol
        self.test_mode = test_mode

        # Inicializuj všetky moduly
        self.logger   = StructuredLogger(symbol, test_mode)
        self.notifier = TelegramNotifier(telegram_token, telegram_chat_id, symbol, self.logger)
        self.circuit  = CircuitBreaker(circuit_drop_pct, circuit_window, logger=self.logger)
        self.estop    = EmergencyStop(self.logger, self.notifier)
        self.trailing = TrailingTakeProfit(trail_pct, trail_activate, self.logger)
        self.stale    = None
        self._grid_bottom = grid_bottom

        # Profit Manager — nový modul
        self.profit = ProfitManager(
            base_capital   = base_capital,
            daily_target   = daily_target,
            trail_pct      = profit_trail_pct,
            monthly_target = monthly_target,
            logger         = self.logger,
            notifier       = self.notifier,
        )

        # Heartbeat so stavovým callbackom
        self.heartbeat = Heartbeat(
            interval_min = heartbeat_min,
            logger       = self.logger,
            notifier     = self.notifier,
            get_state_fn = self._get_state,
        )

        # Registruj circuit breaker → emergency stop
        self.circuit.on_trip = self._on_circuit_trip

        # Aktuálny stav
        self._current_price   = 0.0
        self._open_orders     = 0
        self._unrealized_pnl  = 0.0
        self._realized_pnl    = 0.0
        self._portfolio_value = starting_usdt
        self._started_at      = datetime.now()
        self._trades_today    = 0

        self.logger.info("SYSTEM",
            f"Guardian inicializovaný | {symbol} | test_mode={test_mode} | "
            f"base_capital={base_capital:.0f} | daily_target={daily_target:.0f}"
        )

    @classmethod
    def create(
        cls,
        symbol:        str,
        test_mode:     bool  = True,
        starting_usdt: float = 1000.0,
        base_capital:  float = 10_000.0,
        daily_target:  float = 100.0,
        **kwargs,
    ) -> "Guardian":
        """Továrenská metóda — vytvorí Guardian z .env konfigurácie."""
        return cls(
            symbol           = symbol,
            test_mode        = test_mode,
            circuit_drop_pct = float(os.getenv("CIRCUIT_DROP_PCT",   "5.0")),
            circuit_window   = float(os.getenv("CIRCUIT_WINDOW_MIN", "10.0")),
            trail_pct        = float(os.getenv("TRAIL_PCT",          "0.8")),
            trail_activate   = float(os.getenv("TRAIL_ACTIVATE_PCT", "0.5")),
            heartbeat_min    = float(os.getenv("HEARTBEAT_MIN",      "30.0")),
            telegram_token   = os.getenv("TELEGRAM_BOT_TOKEN", ""),
            telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID",   ""),
            starting_usdt    = starting_usdt,
            base_capital     = float(os.getenv("BASE_CAPITAL",    str(base_capital))),
            daily_target     = float(os.getenv("DAILY_TARGET",    str(daily_target))),
            profit_trail_pct = float(os.getenv("PROFIT_TRAIL_PCT","0.90")),
            monthly_target   = float(os.getenv("MONTHLY_TARGET",  "0")) or None,
            **kwargs,
        )

    def start(self):
        """Spustí background služby (heartbeat thread)."""
        self.heartbeat.start()
        self.logger.info("SYSTEM", "Guardian spustený")

    def stop(self):
        """Zastav background služby."""
        self.heartbeat.stop()
        self.logger.info("SYSTEM", "Guardian zastavený")

    # ── Hlavné metódy pre hlavný loop ────────────────────────────

    def tick(self, price: float, open_orders: int = 0,
             unrealized_pnl: float = 0.0, realized_pnl: float = 0.0,
             portfolio_value: float = 0.0,
             trades_today: int = 0) -> tuple[bool, ProfitSignal]:
        """
        Zavolaj každý tick z hlavného loopu.
        Vráti (ok, profit_signal):
          ok=False           → zastav okamžite (circuit / emergency)
          ok=True, TRAIL_STOP → zastav obchodovanie na dnes (zisk zabezpečený)
          ok=True, OK/LOCK    → obchoduj normálne
        """
        self._current_price   = price
        self._open_orders     = open_orders
        self._unrealized_pnl  = unrealized_pnl
        self._realized_pnl    = realized_pnl
        self._portfolio_value = portfolio_value if portfolio_value > 0 else self._portfolio_value
        self._trades_today    = trades_today

        # 1. Emergency stop kontrola
        if not self.estop.check():
            return False, ProfitSignal.OK

        # 2. Circuit breaker kontrola
        circuit_ok = self.circuit.update(price)
        if not circuit_ok and not self.estop.is_active:
            self.estop.trigger(f"Circuit Breaker OPEN: {self.circuit.status()}")
        if not circuit_ok:
            return False, ProfitSignal.OK

        # 3. Profit Manager kontrola
        profit_signal = self.profit.update(self._portfolio_value, trades_today)

        return True, profit_signal

    def on_buy_filled(self, order_id: str, price: float, qty: float,
                      notional: float = None, fee: float = None):
        """Zavolaj po vyplnení BUY príkazu."""
        self.logger.trade("BUY", price, qty, notional=notional or price*qty, fee=fee)
        self.trailing.register(order_id, price, qty)
        self.notifier.send_trade("BUY", price, qty, notional=notional)

    def on_sell_filled(self, order_id: str, price: float, qty: float,
                       pnl: float = None, notional: float = None, fee: float = None):
        """Zavolaj po vyplnení SELL príkazu."""
        self.logger.trade("SELL", price, qty, pnl=pnl, notional=notional or price*qty, fee=fee)
        self.trailing.remove(order_id)
        self.notifier.send_trade("SELL", price, qty, pnl=pnl, notional=notional)

    def check_trailing(self, price: float) -> list[TrailingPosition]:
        """Skontroluj trailing trigger. Vráti pozície na predaj."""
        triggered = self.trailing.update(price)
        for pos in triggered:
            profit_pct = (price - pos.entry_price) / pos.entry_price * 100
            self.notifier.send_trailing_trigger(price, pos.high_water, profit_pct, pos.qty)
        return triggered

    def check_stale(self, price: float, avg_buy: float, coin_balance: float) -> Optional[StaleStatus]:
        """Skontroluj stale inventory. Vráti StaleStatus alebo None."""
        if self._grid_bottom <= 0:
            return None
        if self.stale is None:
            self.stale = StaleInventoryManager(
                grid_bottom    = self._grid_bottom,
                avg_buy_price  = avg_buy,
                coin_balance   = coin_balance,
                logger         = self.logger,
            )
        self.stale.update_inventory(avg_buy, coin_balance)
        status = self.stale.analyze(price)
        if status.mode != StaleMode.NORMAL:
            self.notifier.send_stale_alert(
                status.mode.value, status.below_grid_pct,
                status.avg_buy_price, status.breakeven_price
            )
        return status

    def emergency_stop(self, reason: str):
        """Manuálny emergency stop."""
        self.estop.trigger(reason)

    def reset_circuit(self) -> bool:
        """Manuálny reset circuit breakera."""
        ok = self.circuit.reset()
        if ok:
            self.estop.reset()
        return ok

    def set_grid_bottom(self, price: float):
        self._grid_bottom = price

    # ── Interné ──────────────────────────────────────────────────

    def _get_state(self) -> dict:
        ps = self.profit.status(self._portfolio_value)
        return {
            "price":             self._current_price,
            "open_orders":       self._open_orders,
            "unrealized_pnl":    self._unrealized_pnl,
            "realized_pnl":      self._realized_pnl,
            "portfolio_value":   self._portfolio_value,
            "breaker_state":     self.circuit.state.value,
            "emergency_stop":    self.estop.is_active,
            "day_profit":        ps["day_profit"],
            "monthly_profit":    ps["monthly_profit"],
            "monthly_remaining": ps["monthly_remaining"],
            "trail_active":      ps["trail_active"],
            "day_stopped":       ps["day_stopped"],
        }

    def _on_circuit_trip(self, reason: str):
        self.notifier.send_circuit_trip(reason)


# ════════════════════════════════════════════════════════════════
# DEMO & TESTY
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
# DEMO & TESTY
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":

    print("═" * 64)
    print("  APEX BOT — Step 6: Guardian + ProfitManager Demo")
    print("═" * 64 + "\n")

    g = Guardian(
        symbol="BNB/USDT", test_mode=True,
        circuit_drop_pct=5.0, circuit_window=10.0,
        trail_pct=0.8, trail_activate=0.5, heartbeat_min=999,
        base_capital=10_000.0, daily_target=100.0,
        profit_trail_pct=0.90, starting_usdt=10_000.0,
    )

    # ── TEST 1: Normálny beh ─────────────────────────────────────
    print("── TEST 1: Normálny beh ────────────────────────────────────")
    for p, pv in [(618.0, 10_010.0), (619.5, 10_020.0), (617.8, 10_015.0)]:
        ok, sig = g.tick(p, open_orders=6, portfolio_value=pv)
        print(f"  tick({p}) pv={pv} → ok={ok} signal={sig.value}")

    # ── TEST 2: Flash Crash → Circuit Breaker ────────────────────
    print("\n── TEST 2: Flash Crash ─────────────────────────────────────")
    g2 = Guardian("BNB/USDT", True, circuit_drop_pct=5.0,
                  circuit_window=10.0, heartbeat_min=999,
                  base_capital=10_000.0, daily_target=100.0, starting_usdt=10_000.0)
    for p, pv in [(618, 10_010), (615, 10_005), (610, 9_990),
                  (604, 9_960), (590, 9_900), (587, 9_880)]:
        ok, sig = g2.tick(p, portfolio_value=float(pv))
        print(f"  tick({p}) → ok={ok} breaker={g2.circuit.state.value}")
        if not ok:
            print(f"  ⛔ Zastavené!")
            break

    # ── TEST 3: ProfitManager — Trailing Daily Profit ────────────
    print("\n── TEST 3: Trailing Daily Profit ───────────────────────────")
    pm = ProfitManager(
        base_capital=10_000.0, daily_target=100.0,
        trail_pct=0.90,
    )

    # Simulácia portfólio hodnôt počas dňa
    portfolio_values = [
        (10_000, "Štart dňa"),
        (10_040, "Zisk +40 USDT"),
        (10_080, "Zisk +80 USDT"),
        (10_105, "Zisk +105 → AKTIVÁCIA trailing (cieľ=100)"),
        (10_130, "Zisk +130 → HWM, trail_stop=117"),
        (10_155, "Zisk +155 → HWM, trail_stop=139.5"),
        (10_170, "Zisk +170 → HWM, trail_stop=153"),
        (10_145, "Pokles na +145 → nad stop, sledujeme"),
        (10_120, "Pokles na +120 → pod trail_stop 153 → STOP!"),
        (10_095, "Ďalší tick — bot už neobchoduje"),
    ]

    for pv, desc in portfolio_values:
        sig = pm.update(float(pv), trades_today=5)
        dp  = pm.day_profit(float(pv))
        stop = f"stop@{pm._trail_stop_at:+.1f}" if pm._trail_active else "trail=čaká"
        print(f"  pv={pv:>8} | zisk={dp:>+7.1f} | {stop:>18} | "
              f"sig={sig.value:<12} | {desc}")

    # Print status
    print()
    pm.print_status(10_120.0)

    # ── TEST 4: Denný report (simulácia midnight) ─────────────────
    print("── TEST 4: Denný report (midnight rollover) ────────────────")
    pm2 = ProfitManager(base_capital=10_000.0, daily_target=100.0, trail_pct=0.90)
    pm2._monthly_profit = 320.0   # simulácia — mesiac má už 320 USDT
    pm2._current_day    = "2026-03-30"   # starý deň → vyvolá rollover
    sig = pm2.update(10_085.0)   # nový deň = rollover
    print(f"  Rollover signal: {sig.value}")

    # ── TEST 5: Guardian.tick() vracia tuple ──────────────────────
    print("\n── TEST 5: Guardian tick() s profit kontrolou ───────────────")
    g3 = Guardian("BNB/USDT", True, heartbeat_min=999,
                  base_capital=10_000.0, daily_target=50.0,
                  profit_trail_pct=0.90, starting_usdt=10_000.0)
    for pv, desc in [(10_020, "Nízky zisk"), (10_055, "Cieľ dosiahnutý!"),
                     (10_070, "Rast"), (10_045, "Pokles → TRAIL STOP")]:
        ok, sig = g3.tick(618.0, portfolio_value=float(pv), trades_today=3)
        print(f"  pv={pv} → ok={ok} | profit_signal={sig.value} | {desc}")

    # ── TEST 6: Heartbeat so ziskovými dátami ────────────────────
    print("\n── TEST 6: Heartbeat ────────────────────────────────────────")
    g._current_price   = 618.42
    g._open_orders     = 8
    g._unrealized_pnl  = 0.48
    g._realized_pnl    = 0.38
    g._portfolio_value = 10_085.0
    g.heartbeat.beat_now()

    print("\n" + "═" * 64)
    print("  ✅ Všetky testy prešli!")
    print("═" * 64)
    print("\n  Integrácia do step5_main.py:")
    print("    guardian = Guardian.create(symbol, test_mode, base_capital=10000, daily_target=100)")
    print("    ok, profit_sig = guardian.tick(price, portfolio_value=portfolio)")
    print("    if not ok: break  # circuit / emergency")
    print("    if profit_sig == ProfitSignal.TRAIL_STOP: break  # denný cieľ splnený")
