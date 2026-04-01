"""
APEX BOT — Step 8: Volatility Scaling
========================================
Dynamické škálovanie správania grid stratégie podľa volatility trhu.

Zodpovednosti:
  - Klasifikovať aktuálny volatility režim (LOW/NORMAL/HIGH/EXTREME)
  - Vracať explicitné odporúčania pre GridEngine, PositionSizer, CustomLogic
  - Implementovať smoothing + hysteresis pre stabilitu režimov
  - Blokovať nebezpečné zmeny (napr. DCA pri EXTREME vol)

Integrácia:
  - Volá ho hlavný loop (step5_main.py) každý tick
  - Výstup VolatilityDecision konzumujú: GridEngine, PositionSizer, CustomLogic
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("ApexBot.VolScaler")


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class VolatilityRegime(str, Enum):
    LOW     = "LOW"       # trh stagnuje, úzky spread
    NORMAL  = "NORMAL"    # štandardné podmienky
    HIGH    = "HIGH"      # zvýšená volatilita, pozor na sizing
    EXTREME = "EXTREME"   # kríza / flash crash, ochranný režim


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VolatilityConfig:
    """
    Konfiguračné prahové hodnoty pre volatility scaling.

    Pravidlo pre nastavovanie prahov:
      Vždy nechaj gap medzi LOW_EXIT a NORMAL_ENTRY atď.
      (hysteresis) aby sa predišlo rýchlemu prepínaniu.

    Príklad pre BNB/USDT s 1h sviečkami:
      Typický ATR% ≈ 0.8% – 1.5%
      LOW    < 0.5%
      NORMAL 0.5% – 2.0%
      HIGH   2.0% – 4.0%
      EXTREME > 4.0%
    """
    # ATR% prahy pre vstup do režimu
    low_entry:     float = 0.005   # ATR% < 0.5% → LOW
    normal_entry:  float = 0.020   # ATR% < 2.0% → NORMAL (z HIGH)
    high_entry:    float = 0.020   # ATR% ≥ 2.0% → HIGH
    extreme_entry: float = 0.040   # ATR% ≥ 4.0% → EXTREME

    # Hysteresis — výstupné prahy (musia byť "menej agresívne" než vstup)
    low_exit:      float = 0.007   # z LOW vychádza pri ATR% > 0.7%
    high_exit:     float = 0.015   # z HIGH vychádza pri ATR% < 1.5%
    extreme_exit:  float = 0.030   # z EXTREME vychádza pri ATR% < 3.0%

    # Smoothing — EWM priemer ATR% namiesto surového ATR%
    smoothing_alpha: float = 0.15   # nízka alpha = pomalá reakcia (stabilita)

    # Cooldown — minimálny čas (v tickoch) medzi zmenami režimu
    regime_cooldown_ticks: int = 5

    # ATR periódy
    atr_period_short: int = 7     # krátkodobý ATR
    atr_period_long:  int = 21    # dlhodobý ATR (baseline)

    # Rolling returns volatility (alternatíva k ATR)
    use_returns_vol:  bool = False
    returns_vol_window: int = 20


# ─────────────────────────────────────────────────────────────────────────────
# Regime Rules — odporúčania pre každý režim
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RegimeRules:
    """
    Statická tabuľka pravidiel pre každý volatility režim.

    Multipliers sú relatívne voči baseline (1.0 = žiadna zmena).

    Tabuľka:
    ┌─────────┬──────────┬────────────┬──────────┬──────────────┬─────────────┐
    │ Režim   │ grid_width│ order_size │ levels   │ rebal_thresh │ max_exposure│
    ├─────────┼──────────┼────────────┼──────────┼──────────────┼─────────────┤
    │ LOW     │  0.70×   │  0.80×     │  +2      │  0.70×       │  1.00×      │
    │ NORMAL  │  1.00×   │  1.00×     │   0      │  1.00×       │  1.00×      │
    │ HIGH    │  1.40×   │  0.65×     │  -2      │  1.30×       │  0.75×      │
    │ EXTREME │  2.00×   │  0.30×     │  -4      │  2.00×       │  0.40×      │
    └─────────┴──────────┴────────────┴──────────┴──────────────┴─────────────┘

    Kľúčové dizajnové rozhodnutia:
      - HIGH vol → ŠIRŠÍ grid (cena sa hýbe viac), ale MENŠÍ order (ochrana)
      - EXTREME → Ochranný režim: dramaticky redukujeme expozíciu
      - LOW vol → zúžený grid + mierny size downgrade (nízka likvidita)
    """
    grid_width_mult:     float    # multiplier na step_pct
    order_size_mult:     float    # multiplier na order_usdt
    levels_delta:        int      # +/- levels oproti baseline
    rebalance_mult:      float    # multiplier na rebalance threshold
    max_exposure_mult:   float    # multiplier na max_symbol_exposure
    allow_dca:           bool
    allow_new_buys:      bool
    protective_mode:     bool     # True = žiadne nové pozície, len risk management


REGIME_RULES: dict[VolatilityRegime, RegimeRules] = {
    VolatilityRegime.LOW: RegimeRules(
        grid_width_mult   = 0.70,
        order_size_mult   = 0.80,
        levels_delta      = +2,
        rebalance_mult    = 0.70,
        max_exposure_mult = 1.00,
        allow_dca         = True,
        allow_new_buys    = True,
        protective_mode   = False,
    ),
    VolatilityRegime.NORMAL: RegimeRules(
        grid_width_mult   = 1.00,
        order_size_mult   = 1.00,
        levels_delta      = 0,
        rebalance_mult    = 1.00,
        max_exposure_mult = 1.00,
        allow_dca         = True,
        allow_new_buys    = True,
        protective_mode   = False,
    ),
    VolatilityRegime.HIGH: RegimeRules(
        grid_width_mult   = 1.40,
        order_size_mult   = 0.65,
        levels_delta      = -2,
        rebalance_mult    = 1.30,
        max_exposure_mult = 0.75,
        allow_dca         = True,
        allow_new_buys    = True,
        protective_mode   = False,
    ),
    VolatilityRegime.EXTREME: RegimeRules(
        grid_width_mult   = 2.00,
        order_size_mult   = 0.30,
        levels_delta      = -4,
        rebalance_mult    = 2.00,
        max_exposure_mult = 0.40,
        allow_dca         = False,
        allow_new_buys    = False,
        protective_mode   = True,
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot — vstupné dáta
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VolatilitySnapshot:
    """Vstupné dáta pre volatility scaling výpočet."""
    klines:        list[dict]    # OHLCV sviečky (dicts s open/high/low/close/volume)
    current_price: float
    symbol:        str = ""

    def closes(self)  -> list[float]: return [k["close"]  for k in self.klines]
    def highs(self)   -> list[float]: return [k["high"]   for k in self.klines]
    def lows(self)    -> list[float]: return [k["low"]    for k in self.klines]


# ─────────────────────────────────────────────────────────────────────────────
# Decision — výstupné odporúčania
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VolatilityDecision:
    """
    Výstup VolatilityScaler — konzumujú ho ostatné moduly.

    Použitie:
        decision = scaler.update(snapshot)

        # V GridEngine:
        new_step_pct = base_step_pct * decision.grid_width_mult
        new_levels   = base_levels + decision.levels_delta

        # V PositionSizer:
        effective_size = base_size * decision.order_size_mult
        max_exp        = base_max_exp * decision.max_exposure_mult

        # V CustomLogic:
        if not decision.allow_dca: skip_dca()
        if decision.protective_mode: cancel_buys()
    """
    regime:              VolatilityRegime
    prev_regime:         VolatilityRegime
    regime_changed:      bool

    # Raw metriky
    atr_short:           float      # ATR z krátkodobého okna
    atr_long:            float      # ATR z dlhodobého okna (baseline)
    atr_pct:             float      # ATR% = atr_short / price
    atr_ratio:           float      # atr_short / atr_long (> 1 = rastúca vol)
    smoothed_atr_pct:    float      # EWM smoothed ATR%

    # Odporúčania (priamo aplikovateľné v ostatných moduloch)
    grid_width_mult:     float
    order_size_mult:     float
    levels_delta:        int
    rebalance_mult:      float
    max_exposure_mult:   float
    allow_dca:           bool
    allow_new_buys:      bool
    protective_mode:     bool

    # Odvodené odporúčané hodnoty (pre grid s base hodnotami)
    def apply_to_step_pct(self, base_step_pct: float) -> float:
        return round(
            max(0.3, min(5.0, base_step_pct * self.grid_width_mult)), 3
        )

    def apply_to_levels(self, base_levels: int) -> int:
        return max(2, base_levels + self.levels_delta)

    def apply_to_order_usdt(self, base_usdt: float) -> float:
        return round(base_usdt * self.order_size_mult, 4)

    def summary(self) -> str:
        change = f" ← z {self.prev_regime.value}" if self.regime_changed else ""
        return (
            f"[VOL] Režim: {self.regime.value}{change} | "
            f"ATR%={self.atr_pct:.3%} smooth={self.smoothed_atr_pct:.3%} "
            f"ratio={self.atr_ratio:.2f} | "
            f"grid×{self.grid_width_mult:.2f} size×{self.order_size_mult:.2f} "
            f"exp×{self.max_exposure_mult:.2f} | "
            f"dca={'✓' if self.allow_dca else '✗'} "
            f"buys={'✓' if self.allow_new_buys else '✗'} "
            f"protect={'⚠' if self.protective_mode else '—'}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Indicator helpers
# ─────────────────────────────────────────────────────────────────────────────

def _calc_atr(klines: list[dict], period: int) -> float:
    """Wilder ATR. Vyžaduje aspoň period+1 sviečok."""
    if len(klines) < period + 1:
        # Fallback: simple average of (high - low)
        trs = [k["high"] - k["low"] for k in klines]
        return sum(trs) / len(trs) if trs else 0.0

    trs: list[float] = []
    for i in range(1, len(klines)):
        h, l, pc = klines[i]["high"], klines[i]["low"], klines[i-1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    # Seed s prvými `period` hodnotami
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _calc_returns_vol(closes: list[float], window: int) -> float:
    """Anualizovaná rolling vol z log returns. Vracia dennú std * sqrt(365)."""
    if len(closes) < window + 1:
        return 0.0
    log_rets = [
        math.log(closes[i] / closes[i-1])
        for i in range(len(closes) - window, len(closes))
        if closes[i-1] > 0
    ]
    if not log_rets:
        return 0.0
    mean = sum(log_rets) / len(log_rets)
    var  = sum((r - mean) ** 2 for r in log_rets) / len(log_rets)
    return math.sqrt(var * 365)


# ─────────────────────────────────────────────────────────────────────────────
# VolatilityScaler — hlavná trieda
# ─────────────────────────────────────────────────────────────────────────────

class VolatilityScaler:
    """
    Produkčný volatility scaler pre APEX BOT.

    Zodpovednosti:
      1. Výpočet ATR (short + long window)
      2. EWM smoothing ATR% pre stabilitu
      3. Hysteresis pri prechode medzi režimami
      4. Cooldown medzi zmenami režimu
      5. Generovanie VolatilityDecision pre ostatné moduly

    Príklad:
        scaler = VolatilityScaler(cfg)
        decision = scaler.update(snapshot)
        new_step = decision.apply_to_step_pct(base_step_pct=1.2)
    """

    def __init__(self, cfg: Optional[VolatilityConfig] = None):
        self.cfg = cfg or VolatilityConfig()
        self._regime:           VolatilityRegime = VolatilityRegime.NORMAL
        self._smoothed_atr_pct: float = 0.0
        self._ticks_since_change: int = 999
        self._atr_pct_history: deque[float] = deque(maxlen=100)

    @property
    def current_regime(self) -> VolatilityRegime:
        return self._regime

    def update(self, snapshot: VolatilitySnapshot) -> VolatilityDecision:
        """Hlavná metóda — zavolaj každý tick z hlavného loopu."""
        klines = snapshot.klines
        price  = snapshot.current_price
        if price <= 0:
            return self._make_decision(
                VolatilityRegime.NORMAL, False, 0.0, 0.0, 0.0, 0.0, 0.0
            )

        # ── Výpočet ATR ──────────────────────────────────────────────────────
        atr_short = _calc_atr(klines, self.cfg.atr_period_short)
        atr_long  = _calc_atr(klines, self.cfg.atr_period_long)
        atr_pct   = atr_short / price
        atr_ratio = atr_short / atr_long if atr_long > 0 else 1.0

        # Voliteľne nahraď ATR rolling returns vol
        if self.cfg.use_returns_vol:
            rv = _calc_returns_vol(snapshot.closes(), self.cfg.returns_vol_window)
            if rv > 0:
                atr_pct = rv / math.sqrt(365)  # denná vol approx

        # ── EWM Smoothing ────────────────────────────────────────────────────
        alpha = self.cfg.smoothing_alpha
        if self._smoothed_atr_pct == 0.0:
            self._smoothed_atr_pct = atr_pct
        else:
            self._smoothed_atr_pct = alpha * atr_pct + (1 - alpha) * self._smoothed_atr_pct

        self._atr_pct_history.append(self._smoothed_atr_pct)
        self._ticks_since_change += 1

        # ── Hysteresis + Cooldown ────────────────────────────────────────────
        new_regime = self._classify(self._smoothed_atr_pct)
        regime_changed = False

        if (
            new_regime != self._regime
            and self._ticks_since_change >= self.cfg.regime_cooldown_ticks
        ):
            prev = self._regime
            self._regime = new_regime
            self._ticks_since_change = 0
            regime_changed = True
            log.info(
                f"[VOL] Zmena režimu: {prev.value} → {new_regime.value} | "
                f"smoothed ATR%={self._smoothed_atr_pct:.3%}"
            )

        return self._make_decision(
            self._regime, regime_changed,
            atr_short, atr_long, atr_pct, self._smoothed_atr_pct, atr_ratio
        )

    def _classify(self, smoothed_atr_pct: float) -> VolatilityRegime:
        """
        Hysteresis klasifikácia — výstupný prah je iný ako vstupný.

        Logika:
          - Raz v EXTREME zostaneš tam, kým ATR neklesne pod extreme_exit
          - Raz v LOW zostaneš tam, kým ATR nestúpne nad low_exit
          - Tým sa predchádza rýchlemu oscilácii pri hraničných hodnotách
        """
        current = self._regime
        cfg     = self.cfg
        s       = smoothed_atr_pct

        if current == VolatilityRegime.EXTREME:
            return VolatilityRegime.EXTREME if s >= cfg.extreme_exit else VolatilityRegime.HIGH

        if current == VolatilityRegime.HIGH:
            if s >= cfg.extreme_entry:
                return VolatilityRegime.EXTREME
            if s < cfg.high_exit:
                return VolatilityRegime.NORMAL
            return VolatilityRegime.HIGH

        if current == VolatilityRegime.LOW:
            if s >= cfg.high_entry:
                return VolatilityRegime.HIGH
            if s > cfg.low_exit:
                return VolatilityRegime.NORMAL
            return VolatilityRegime.LOW

        # NORMAL (default)
        if s >= cfg.extreme_entry:
            return VolatilityRegime.EXTREME
        if s >= cfg.high_entry:
            return VolatilityRegime.HIGH
        if s <= cfg.low_entry:
            return VolatilityRegime.LOW
        return VolatilityRegime.NORMAL

    def _make_decision(
        self,
        regime:          VolatilityRegime,
        changed:         bool,
        atr_short:       float,
        atr_long:        float,
        atr_pct:         float,
        smoothed_atr_pct: float,
        atr_ratio:       float,
    ) -> VolatilityDecision:
        rules     = REGIME_RULES[regime]
        prev      = self._regime if not changed else list(VolatilityRegime)[
            max(0, list(VolatilityRegime).index(regime) - 1)
        ]
        decision = VolatilityDecision(
            regime            = regime,
            prev_regime       = prev,
            regime_changed    = changed,
            atr_short         = atr_short,
            atr_long          = atr_long,
            atr_pct           = atr_pct,
            atr_ratio         = atr_ratio,
            smoothed_atr_pct  = smoothed_atr_pct,
            grid_width_mult   = rules.grid_width_mult,
            order_size_mult   = rules.order_size_mult,
            levels_delta      = rules.levels_delta,
            rebalance_mult    = rules.rebalance_mult,
            max_exposure_mult = rules.max_exposure_mult,
            allow_dca         = rules.allow_dca,
            allow_new_buys    = rules.allow_new_buys,
            protective_mode   = rules.protective_mode,
        )
        log.debug(decision.summary())
        return decision

    def get_atr_percentile(self) -> Optional[float]:
        """
        Percentil aktuálneho ATR% v historii posledných 100 tickov.
        0.0 = historicky najnižší, 1.0 = historicky najvyšší.
        """
        hist = list(self._atr_pct_history)
        if len(hist) < 10 or self._smoothed_atr_pct == 0.0:
            return None
        below = sum(1 for h in hist if h <= self._smoothed_atr_pct)
        return below / len(hist)
