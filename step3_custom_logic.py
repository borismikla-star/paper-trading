"""
APEX BOT — Step 3: Custom Logika
==================================
RSI filter · DCA trigger · Stop-loss · Momentum

Závisí od: step1_core.py, step2_grid_engine.py
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from datetime import datetime, timedelta

log = logging.getLogger("ApexBot.Logic")


# ── Typy signálov ────────────────────────────────────────────────────────────

class Signal(str, Enum):
    NEUTRAL   = "NEUTRAL"    # obchoduj normálne
    PAUSE     = "PAUSE"      # pozastav nové príkazy
    DCA       = "DCA"        # pridaj extra nákup
    WIDEN     = "WIDEN"      # rozšír grid (momentum)
    TIGHTEN   = "TIGHTEN"    # zúž grid (nízka volatilita)
    STOP_LOSS = "STOP_LOSS"  # zavri všetko, zastav bota


@dataclass
class MarketSnapshot:
    """Vstupné dáta pre analýzu."""
    price:        float
    klines:       list[dict]   # OHLCV sviečky z Binance
    portfolio_value:   float   # aktuálna hodnota portfólia v USDT
    portfolio_start:   float   # počiatočná hodnota portfólia
    last_dca_time: Optional[datetime] = None


@dataclass
class LogicResult:
    """Výstup analýzy — čo má bot urobiť."""
    signal:       Signal
    reason:       str
    rsi:          Optional[float] = None
    atr:          Optional[float] = None
    momentum:     Optional[float] = None
    dca_amount:   Optional[float] = None
    grid_multiplier: float = 1.0   # 1.0 = normálny grid, >1 = širší, <1 = užší


# ── RSI Indikátor ────────────────────────────────────────────────────────────

class RSIIndicator:
    """
    Relative Strength Index — meria či je trh prekúpený alebo prepredaný.
    RSI > 75 → prekúpený  (nepridávaj BUY príkazy)
    RSI < 25 → prepredaný (silný signál na nákup)
    """

    def __init__(self, period: int = 14):
        self.period = period

    def calculate(self, klines: list[dict]) -> Optional[float]:
        closes = [k["close"] for k in klines]
        if len(closes) < self.period + 1:
            log.warning(f"RSI: nedostatok dát ({len(closes)} sviečok, potrebujem {self.period + 1})")
            return None

        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))

        # Priemer posledných `period` hodnôt
        avg_gain = sum(gains[-self.period:]) / self.period
        avg_loss = sum(losses[-self.period:]) / self.period

        if avg_loss == 0:
            return 100.0

        rs  = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return round(rsi, 2)


# ── ATR Indikátor (volatilita) ───────────────────────────────────────────────

class ATRIndicator:
    """
    Average True Range — meria volatilitu trhu.
    Vysoký ATR → rozšír grid (cena sa hýbe viac)
    Nízky ATR  → zúž grid (cena stojí)
    """

    def __init__(self, period: int = 14):
        self.period = period

    def calculate(self, klines: list[dict]) -> Optional[float]:
        if len(klines) < self.period + 1:
            return None

        true_ranges = []
        for i in range(1, len(klines)):
            high  = klines[i]["high"]
            low   = klines[i]["low"]
            prev_close = klines[i - 1]["close"]
            tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
            true_ranges.append(tr)

        atr = sum(true_ranges[-self.period:]) / self.period
        return round(atr, 4)

    def as_pct(self, atr: float, price: float) -> float:
        """ATR vyjadrený ako % z ceny — lepšie porovnateľný medzi pármi."""
        return round((atr / price) * 100, 3)


# ── Momentum ─────────────────────────────────────────────────────────────────

class MomentumIndicator:
    """
    Jednoduchý cenový momentum — porovná aktuálnu cenu s priemerom N sviečok.
    Silný momentum → rozšír grid, bot zarobí viac na pohybe.
    """

    def __init__(self, period: int = 10):
        self.period = period

    def calculate(self, klines: list[dict], current_price: float) -> Optional[float]:
        if len(klines) < self.period:
            return None
        avg = sum(k["close"] for k in klines[-self.period:]) / self.period
        momentum = (current_price - avg) / avg * 100
        return round(momentum, 3)


# ── Hlavná logika bota ───────────────────────────────────────────────────────

class CustomLogic:
    """
    Kombinuje všetky indikátory a vráti jeden jasný signál pre Grid Engine.

    Pravidlá (konfigurovateľné cez .env):
    ┌──────────────────────────────────────────────────────────┐
    │ RSI > 75        → PAUSE  (nepridávaj BUY)               │
    │ RSI < 25        → DCA    (extra nákup, silný signál)     │
    │ Strata > 8%     → STOP_LOSS                              │
    │ Pokles > 2%     → DCA    (priemer dole, Dollar Cost Avg) │
    │ ATR% > 1.5x avg → WIDEN  (rozšír grid o 30%)            │
    │ ATR% < 0.5x avg → TIGHTEN (zúž grid o 20%)              │
    │ Momentum > +2%  → WIDEN  (trend hore, väčší rozsah)     │
    │ Momentum < -2%  → PAUSE  (trend dole, opatrnosť)        │
    └──────────────────────────────────────────────────────────┘
    """

    def __init__(
        self,
        rsi_overbought:   float = 75.0,
        rsi_oversold:     float = 25.0,
        stop_loss_pct:    float = 8.0,
        dca_trigger_pct:  float = 2.0,
        dca_amount_usdt:  float = 20.0,
        dca_cooldown_min: int   = 60,
        momentum_period:  int   = 10,
        atr_period:       int   = 14,
        rsi_period:       int   = 14,
    ):
        self.rsi_overbought   = rsi_overbought
        self.rsi_oversold     = rsi_oversold
        self.stop_loss_pct    = stop_loss_pct
        self.dca_trigger_pct  = dca_trigger_pct
        self.dca_amount_usdt  = dca_amount_usdt
        self.dca_cooldown_min = dca_cooldown_min

        self.rsi      = RSIIndicator(rsi_period)
        self.atr      = ATRIndicator(atr_period)
        self.momentum = MomentumIndicator(momentum_period)

        self._atr_history: list[float] = []   # na výpočet priemerného ATR

    # ── Hlavná metóda ────────────────────────────────────────────────────────

    def analyze(self, snap: MarketSnapshot) -> LogicResult:
        """
        Analyzuje trh a vráti LogicResult so signálom.
        Volaj každých ~60 sekúnd z hlavného loopu.
        """
        klines = snap.klines
        price  = snap.price

        # ── Výpočet indikátorov ──────────────────────────────────────────────
        rsi_val      = self.rsi.calculate(klines)
        atr_val      = self.atr.calculate(klines)
        atr_pct      = self.atr.as_pct(atr_val, price) if atr_val else None
        momentum_val = self.momentum.calculate(klines, price)

        log.info(
            f"Indikátory → RSI: {rsi_val} | "
            f"ATR%: {atr_pct} | Momentum: {momentum_val}%"
        )

        # ── ATR história (na detekciu zmeny volatility) ──────────────────────
        if atr_pct:
            self._atr_history.append(atr_pct)
            if len(self._atr_history) > 20:
                self._atr_history.pop(0)
        avg_atr = sum(self._atr_history) / len(self._atr_history) if self._atr_history else atr_pct or 1.0

        # ── 1. STOP-LOSS — najvyššia priorita ───────────────────────────────
        loss_pct = (snap.portfolio_value - snap.portfolio_start) / snap.portfolio_start * 100
        if loss_pct <= -self.stop_loss_pct:
            return LogicResult(
                signal  = Signal.STOP_LOSS,
                reason  = f"Strata portfólia {loss_pct:.2f}% prekročila limit -{self.stop_loss_pct}%",
                rsi     = rsi_val,
                atr     = atr_pct,
                momentum = momentum_val,
            )

        # ── 2. RSI prekúpenosť — zastav nové BUY príkazy ────────────────────
        if rsi_val and rsi_val > self.rsi_overbought:
            return LogicResult(
                signal  = Signal.PAUSE,
                reason  = f"RSI {rsi_val:.1f} > {self.rsi_overbought} — trh prekúpený, pozastavujem BUY",
                rsi     = rsi_val,
                atr     = atr_pct,
                momentum = momentum_val,
            )

        # ── 3. DCA — RSI prepredaný ──────────────────────────────────────────
        if rsi_val and rsi_val < self.rsi_oversold:
            if self._dca_allowed(snap.last_dca_time):
                return LogicResult(
                    signal     = Signal.DCA,
                    reason     = f"RSI {rsi_val:.1f} < {self.rsi_oversold} — trh prepredaný, extra nákup",
                    rsi        = rsi_val,
                    atr        = atr_pct,
                    momentum   = momentum_val,
                    dca_amount = self.dca_amount_usdt,
                )

        # ── 4. DCA — cenový pokles ───────────────────────────────────────────
        price_drop = self._price_drop_pct(klines, price)
        if price_drop and price_drop >= self.dca_trigger_pct:
            if self._dca_allowed(snap.last_dca_time):
                return LogicResult(
                    signal     = Signal.DCA,
                    reason     = f"Cena klesla o {price_drop:.2f}% — DCA trigger aktivovaný",
                    rsi        = rsi_val,
                    atr        = atr_pct,
                    momentum   = momentum_val,
                    dca_amount = self.dca_amount_usdt,
                )

        # ── 5. Momentum — silný trend nahor ─────────────────────────────────
        if momentum_val and momentum_val > 2.0:
            grid_mult = min(1.5, 1.0 + momentum_val / 10)
            return LogicResult(
                signal           = Signal.WIDEN,
                reason           = f"Momentum +{momentum_val:.2f}% — rozširujem grid (x{grid_mult:.2f})",
                rsi              = rsi_val,
                atr              = atr_pct,
                momentum         = momentum_val,
                grid_multiplier  = grid_mult,
            )

        # ── 6. Momentum — silný trend nadol ─────────────────────────────────
        if momentum_val and momentum_val < -2.0:
            return LogicResult(
                signal   = Signal.PAUSE,
                reason   = f"Momentum {momentum_val:.2f}% — klesajúci trend, pozastavujem",
                rsi      = rsi_val,
                atr      = atr_pct,
                momentum = momentum_val,
            )

        # ── 7. Vysoká volatilita → rozšír grid ──────────────────────────────
        if atr_pct and atr_pct > avg_atr * 1.5:
            return LogicResult(
                signal          = Signal.WIDEN,
                reason          = f"ATR {atr_pct:.3f}% >> priemer {avg_atr:.3f}% — vysoká volatilita",
                rsi             = rsi_val,
                atr             = atr_pct,
                momentum        = momentum_val,
                grid_multiplier = 1.3,
            )

        # ── 8. Nízka volatilita → zúž grid ──────────────────────────────────
        if atr_pct and atr_pct < avg_atr * 0.5:
            return LogicResult(
                signal          = Signal.TIGHTEN,
                reason          = f"ATR {atr_pct:.3f}% << priemer {avg_atr:.3f}% — nízka volatilita",
                rsi             = rsi_val,
                atr             = atr_pct,
                momentum        = momentum_val,
                grid_multiplier = 0.8,
            )

        # ── Predvolené: obchoduj normálne ────────────────────────────────────
        return LogicResult(
            signal   = Signal.NEUTRAL,
            reason   = "Podmienky v norme — grid beží normálne",
            rsi      = rsi_val,
            atr      = atr_pct,
            momentum = momentum_val,
        )

    # ── Pomocné metódy ───────────────────────────────────────────────────────

    def _dca_allowed(self, last_dca_time: Optional[datetime]) -> bool:
        """Skontroluje cooldown medzi DCA príkazmi."""
        if last_dca_time is None:
            return True
        elapsed = (datetime.now() - last_dca_time).total_seconds() / 60
        if elapsed < self.dca_cooldown_min:
            log.info(f"DCA cooldown: ešte {self.dca_cooldown_min - elapsed:.0f} min")
            return False
        return True

    def _price_drop_pct(self, klines: list[dict], current_price: float, lookback: int = 3) -> Optional[float]:
        """Vypočíta % pokles ceny oproti posledným N sviečkam."""
        if len(klines) < lookback:
            return None
        recent_high = max(k["high"] for k in klines[-lookback:])
        if recent_high <= 0:
            return None
        return round((recent_high - current_price) / recent_high * 100, 3)


# ── Test / Demo ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    import random

    def make_klines(base: float, n: int = 50, trend: float = 0) -> list[dict]:
        """Generuje testovacie sviečky."""
        klines, price = [], base
        for _ in range(n):
            change = (random.gauss(trend, 1.0) / 100) * price
            price += change
            klines.append({
                "open":   price - change,
                "high":   price + abs(random.gauss(0, 0.5) / 100 * price),
                "low":    price - abs(random.gauss(0, 0.5) / 100 * price),
                "close":  price,
                "volume": random.uniform(100, 500),
            })
        return klines

    logic = CustomLogic(
        rsi_overbought=75, rsi_oversold=25,
        stop_loss_pct=8, dca_trigger_pct=2,
        dca_amount_usdt=20, dca_cooldown_min=60,
    )

    scenarios = [
        ("Normálny trh",       618.0,  1200.0, 1200.0, 0.0),
        ("Prekúpený trh",      640.0,  1200.0, 1200.0, +0.5),
        ("Prepredaný trh",     590.0,  1200.0, 1200.0, -0.5),
        ("Silný momentum",     650.0,  1200.0, 1200.0, +0.8),
        ("Klesajúci trend",    600.0,  1200.0, 1200.0, -0.8),
        ("Stop-loss trigger",  570.0,  1104.0, 1200.0, -0.3),
    ]

    log.info("=" * 60)
    log.info("APEX BOT — Krok 3: Custom Logika — Simulácia scenárov")
    log.info("=" * 60)

    for name, price, port_val, port_start, trend in scenarios:
        klines = make_klines(price * 0.95, n=50, trend=trend)
        snap = MarketSnapshot(
            price            = price,
            klines           = klines,
            portfolio_value  = port_val,
            portfolio_start  = port_start,
            last_dca_time    = None,
        )
        result = logic.analyze(snap)
        log.info(
            f"\n📊 Scenár: {name}\n"
            f"   Signál:   {result.signal.value}\n"
            f"   Dôvod:    {result.reason}\n"
            f"   RSI: {result.rsi} | ATR%: {result.atr} | Mom: {result.momentum}%\n"
            f"   Grid mult: x{result.grid_multiplier}"
        )

    log.info("\n✅ Krok 3 hotový. Pokračuj na Krok 4 — Order Manager.")
