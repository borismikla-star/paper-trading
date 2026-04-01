"""
APEX BOT — Step 2: Grid Engine
===============================
Vypočíta grid úrovne, sleduje ich stav, detekuje plnenia.

Závisí od: step1_core.py
"""

import math
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional
from datetime import datetime

log = logging.getLogger("ApexBot.Grid")


# ── Typy ────────────────────────────────────────────────────────────────────

class Side(str, Enum):
    BUY  = "BUY"
    SELL = "SELL"

class OrderStatus(str, Enum):
    PENDING = "PENDING"   # vypočítaná, ešte nezadaná
    OPEN    = "OPEN"      # zadaná na burzu
    FILLED  = "FILLED"    # vyplnená
    CANCELLED = "CANCELLED"


@dataclass
class GridLevel:
    """Jedna úroveň gridu = jeden limitný príkaz."""
    index:      int
    side:       Side
    price:      float
    quantity:   float
    status:     OrderStatus = OrderStatus.PENDING
    order_id:   Optional[str] = None
    filled_at:  Optional[datetime] = None
    profit:     float = 0.0

    def __str__(self):
        return (
            f"[{self.index:+3d}] {self.side.value:4s} "
            f"@ {self.price:>10.4f} | qty {self.quantity:.4f} | {self.status.value}"
        )


@dataclass
class GridState:
    """Celkový stav gridu — všetky úrovne + štatistiky."""
    pair:           str
    base_price:     float
    step_pct:       float
    levels:         int
    order_amount:   float           # USDT per order
    symbol_info:    dict = field(default_factory=dict)

    grid:           list[GridLevel] = field(default_factory=list)
    total_profit:   float = 0.0
    filled_count:   int   = 0
    created_at:     datetime = field(default_factory=datetime.now)
    last_rebalance: Optional[datetime] = None


# ── Pomocné funkcie ──────────────────────────────────────────────────────────

def round_step(value: float, step: float) -> float:
    """Zaokrúhli hodnotu na krok burzy (step size)."""
    if step <= 0:
        return value
    precision = max(0, round(-math.log10(step)))
    return round(round(value / step) * step, precision)


def round_price(value: float, tick: float) -> float:
    """Zaokrúhli cenu na tick size burzy."""
    return round_step(value, tick)


# ── Grid Engine ──────────────────────────────────────────────────────────────

class GridEngine:
    """
    Vypočíta a spravuje grid úrovne pre daný pár.

    Štruktúra gridu (príklad, 3 úrovne, krok 1.2%):

        index +3  → SELL @ base * 1.036
        index +2  → SELL @ base * 1.024
        index +1  → SELL @ base * 1.012
        ─────────── BASE PRICE ───────────
        index -1  → BUY  @ base * 0.988
        index -2  → BUY  @ base * 0.976
        index -3  → BUY  @ base * 0.964
    """

    def __init__(self, symbol_info: dict):
        self.symbol_info = symbol_info
        self.step_size   = symbol_info.get("step_size", 0.01)
        self.tick_size   = symbol_info.get("tick_size", 0.01)
        self.min_qty     = symbol_info.get("min_qty", 0.01)
        self.min_notional = symbol_info.get("min_notional", 10.0)

    # ── Výpočet gridu ────────────────────────────────────────────────────────

    def build(
        self,
        pair: str,
        base_price: float,
        levels: int,
        step_pct: float,
        order_amount_usdt: float,
    ) -> GridState:
        """
        Vytvorí nový GridState so všetkými úrovňami.

        Args:
            pair:              Symbol páru (napr. BNBUSDT)
            base_price:        Aktuálna trhová cena
            levels:            Počet úrovní na každú stranu (napr. 8 → 8 BUY + 8 SELL)
            step_pct:          Percentuálny krok medzi úrovňami (napr. 1.2)
            order_amount_usdt: Hodnota jedného príkazu v USDT
        """
        state = GridState(
            pair=pair,
            base_price=base_price,
            step_pct=step_pct,
            levels=levels,
            order_amount=order_amount_usdt,
            symbol_info=self.symbol_info,
        )

        step_mult = step_pct / 100.0
        grid_levels = []

        for i in range(1, levels + 1):
            # ── BUY úroveň (pod aktuálnou cenou) ───────────────────────────
            buy_price = round_price(base_price * (1 - i * step_mult), self.tick_size)
            buy_qty   = round_step(order_amount_usdt / buy_price, self.step_size)

            if buy_qty >= self.min_qty and buy_qty * buy_price >= self.min_notional:
                grid_levels.append(GridLevel(
                    index    = -i,
                    side     = Side.BUY,
                    price    = buy_price,
                    quantity = buy_qty,
                ))

            # ── SELL úroveň (nad aktuálnou cenou) ──────────────────────────
            sell_price = round_price(base_price * (1 + i * step_mult), self.tick_size)
            sell_qty   = round_step(order_amount_usdt / sell_price, self.step_size)

            if sell_qty >= self.min_qty and sell_qty * sell_price >= self.min_notional:
                grid_levels.append(GridLevel(
                    index    = +i,
                    side     = Side.SELL,
                    price    = sell_price,
                    quantity = sell_qty,
                ))

        # Zoraď: od najvyššej ceny po najnižšiu
        state.grid = sorted(grid_levels, key=lambda x: x.price, reverse=True)
        log.info(
            f"Grid vytvorený: {pair} @ {base_price:.4f} | "
            f"{len([l for l in state.grid if l.side == Side.BUY])} BUY + "
            f"{len([l for l in state.grid if l.side == Side.SELL])} SELL úrovní"
        )
        return state

    # ── Rebalancovanie ───────────────────────────────────────────────────────

    def needs_rebalance(self, state: GridState, current_price: float) -> bool:
        """
        Skontroluje či cena nevyšla mimo grid rozsah.
        Ak áno, treba grid prestaviť.
        """
        drift_pct = abs(current_price - state.base_price) / state.base_price * 100
        max_drift  = state.step_pct * state.levels * 0.6   # 60% rozsahu
        if drift_pct > max_drift:
            log.warning(
                f"Rebalance potrebný: cena driftovala {drift_pct:.1f}% "
                f"(limit {max_drift:.1f}%)"
            )
            return True
        return False

    def rebalance(self, state: GridState, new_price: float) -> GridState:
        """Prestavia grid okolo novej ceny. Zachová štatistiky."""
        log.info(f"Rebalancujem grid: {state.base_price:.4f} → {new_price:.4f}")
        new_state = self.build(
            pair              = state.pair,
            base_price        = new_price,
            levels            = state.levels,
            step_pct          = state.step_pct,
            order_amount_usdt = state.order_amount,
        )
        # Prenesie historické štatistiky
        new_state.total_profit   = state.total_profit
        new_state.filled_count   = state.filled_count
        new_state.created_at     = state.created_at
        new_state.last_rebalance = datetime.now()
        return new_state

    # ── Detekcia plnení ──────────────────────────────────────────────────────

    def check_fills(self, state: GridState, current_price: float) -> list[GridLevel]:
        """
        Simuluje plnenie príkazov podľa aktuálnej ceny.
        (V produkcii nahradíme WebSocket eventmi z Binance.)

        BUY  sa plní ak cena klesne ≤ price úrovne
        SELL sa plní ak cena stúpne ≥ price úrovne
        """
        newly_filled = []
        for level in state.grid:
            if level.status != OrderStatus.OPEN:
                continue

            filled = (
                level.side == Side.BUY  and current_price <= level.price or
                level.side == Side.SELL and current_price >= level.price
            )

            if filled:
                level.status    = OrderStatus.FILLED
                level.filled_at = datetime.now()
                level.profit    = self._calc_profit(level, state)
                state.total_profit += level.profit
                state.filled_count += 1
                newly_filled.append(level)
                log.info(
                    f"✅ FILLED {level.side.value} @ {level.price:.4f} | "
                    f"profit: {level.profit:+.4f} USDT | "
                    f"celkom: {state.total_profit:+.4f} USDT"
                )

        return newly_filled

    def _calc_profit(self, level: GridLevel, state: GridState) -> float:
        """
        Odhaduje zisk z vyplnenej úrovne.
        BUY:  zisk = (predaj na susednej SELL - nákup) * qty - poplatky
        SELL: zisk = (predaj - nákup na susednej BUY) * qty - poplatky
        """
        step_value = level.price * (state.step_pct / 100)
        gross = step_value * level.quantity
        fee   = level.price * level.quantity * 0.001  # 0.1% taker fee
        return round(gross - fee * 2, 6)

    # ── Výpis stavu ─────────────────────────────────────────────────────────

    def print_state(self, state: GridState, current_price: float):
        """Vypíše prehľadnú tabuľku gridu do logu."""
        log.info("─" * 62)
        log.info(f"  GRID: {state.pair} | Base: {state.base_price:.4f} | Live: {current_price:.4f}")
        log.info(f"  Zisk: {state.total_profit:+.4f} USDT | Vyplnené: {state.filled_count}")
        log.info("─" * 62)
        for level in state.grid:
            marker = " ◄ LIVE" if abs(level.price - current_price) / current_price < state.step_pct / 200 else ""
            log.info(f"  {level}{marker}")
        log.info("─" * 62)


# ── Test / Demo ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # Simulované symbol_info (v reáli príde z step1_core.py)
    symbol_info = {
        "symbol":       "BNBUSDT",
        "min_qty":      0.01,
        "max_qty":      9000000.0,
        "step_size":    0.01,
        "min_notional": 5.0,
        "tick_size":    0.01,
    }

    engine = GridEngine(symbol_info)

    # 1. Vytvor grid
    BASE_PRICE = 618.42
    state = engine.build(
        pair              = "BNBUSDT",
        base_price        = BASE_PRICE,
        levels            = 6,
        step_pct          = 1.2,
        order_amount_usdt = 15.0,
    )

    # Nastav všetky ako OPEN (v reáli to urobí Order Manager)
    for lvl in state.grid:
        lvl.status = OrderStatus.OPEN

    # 2. Vypíš grid
    engine.print_state(state, BASE_PRICE)

    # 3. Simuluj pohyb ceny a plnenia
    import time
    test_prices = [618.42, 615.10, 610.88, 607.20, 614.50, 622.80, 629.60]
    log.info("\n📈 Simulácia pohybu ceny...")

    for price in test_prices:
        log.info(f"\n  → Cena: {price:.2f}")
        filled = engine.check_fills(state, price)
        if filled:
            for f in filled:
                log.info(f"     💰 {f.side.value} filled @ {f.price:.4f} | profit {f.profit:+.4f} USDT")

        if engine.needs_rebalance(state, price):
            state = engine.rebalance(state, price)
            for lvl in state.grid:
                lvl.status = OrderStatus.OPEN

        time.sleep(0.3)

    log.info(f"\n📊 Výsledok simulácie:")
    log.info(f"   Vyplnené príkazy: {state.filled_count}")
    log.info(f"   Celkový zisk:     {state.total_profit:+.4f} USDT")
    log.info("\n✅ Krok 2 hotový. Pokračuj na Krok 3 — Custom logika (RSI, DCA, Stop-loss).")
