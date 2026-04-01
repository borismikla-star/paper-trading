"""
APEX BOT — Step 10: Market Regime Detection (v2)
==================================================
Rozšírenie o confidence, persistence, cooldown a hysteresis.

Kľúčové vylepšenia oproti v1:
  - raw_regime  vs  effective_regime (oddelené)
  - Confidence score [0, 1] pre každú klasifikáciu
  - Persistence: nový režim sa stane efektívnym až po N tickoch
  - Hysteresis: vstupné/výstupné prahy sú asymetrické
  - Cooldown: min. tickov medzi zmenami (okrem PANIC override)
  - ScoreBreakdown: plná transparentnosť skóre pre audit

Odporúčané defaulty pre BNB/USDT 1h:
  persistence_ticks    = 3    — nový režim potvrdený po 3 hodinách
  cooldown_ticks       = 4    — zmena len každé 4 hodiny
  smoothing_alpha      = 0.20 — pomalá EWM reakcia
  min_confidence_enter = 0.40 — nový režim len ak conf ≥ 40%
  panic_conf_override  = 0.70 — PANIC obíde cooldown pri conf ≥ 70%
"""

from __future__ import annotations

import logging
import math
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("ApexBot.Regime")


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class MarketRegime(str, Enum):
    RANGE          = "RANGE"
    UPTREND        = "UPTREND"
    DOWNTREND      = "DOWNTREND"
    BREAKOUT_UP    = "BREAKOUT_UP"
    BREAKOUT_DOWN  = "BREAKOUT_DOWN"
    PANIC          = "PANIC"
    UNDEFINED      = "UNDEFINED"


class GridBias(str, Enum):
    NEUTRAL           = "NEUTRAL"
    DEFENSIVE_LONG    = "DEFENSIVE_LONG"
    REDUCE_INVENTORY  = "REDUCE_INVENTORY"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeConfig:
    # Indikátory
    ema_fast_period:       int   = 20
    ema_slow_period:       int   = 50
    adx_period:            int   = 14
    atr_period:            int   = 14
    lookback_swing:        int   = 20
    momentum_period:       int   = 10
    min_bars:              int   = 55

    # Breakout / panic thresholds
    breakout_atr_mult:     float = 2.5
    panic_drop_pct_3bar:   float = 4.0   # pokles o X% za 3 bary = PANIC

    # Scoring váhy (relatívne, suma nemusí = 1)
    w_ema_slope:           float = 0.25
    w_adx:                 float = 0.20
    w_hh_ll:               float = 0.20
    w_atr_pct:             float = 0.15
    w_momentum:            float = 0.20

    # Klasifikačné prahy (pre composite_score [-1, +1])
    uptrend_enter_score:   float = +0.40   # vstup do UPTREND
    uptrend_exit_score:    float = +0.25   # výstup z UPTREND (hysteresis gap = 0.15)
    downtrend_enter_score: float = -0.40
    downtrend_exit_score:  float = -0.25

    # Confidence
    min_confidence_enter:  float = 0.35   # min. conf pre recognition nového režimu
    panic_conf_override:   float = 0.65   # PANIC obíde cooldown nad touto hodnotou

    # Persistence — počet tickov kedy raw == new PRED zmenou effective
    persistence_ticks:     int   = 3

    # Cooldown — min. tickov medzi zmenami effective regime
    cooldown_ticks:        int   = 4

    # Score smoothing (EWM)
    smoothing_alpha:       float = 0.20


# ─────────────────────────────────────────────────────────────────────────────
# Score Breakdown (pre audit a explainability)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScoreBreakdown:
    ema_slope_pct:     float
    ema_slope_score:   float
    adx_proxy:         float
    adx_score:         float
    hh_ll_raw:         float
    hh_ll_score:       float
    atr_pct:           float
    atr_score:         float
    momentum_pct:      float
    momentum_score:    float
    raw_composite:     float
    smoothed_composite: float
    confidence:        float
    breakout_up:       bool
    breakout_down:     bool
    panic_detected:    bool


# ─────────────────────────────────────────────────────────────────────────────
# Decision
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RegimeDecision:
    """
    Výstup RegimeDetector.

    raw_regime:       okamžitá klasifikácia (môže flipovať)
    effective_regime: stabilizovaný režim (persistence + cooldown)
    regime_changed:   ak effective_regime sa zmenil tento tick
    change_allowed:   či zmena bola povolená (cooldown, persistence)
    confidence:       0–1 sila effective_regime klasifikácie
    ticks_in_current_regime:  počet tickov v efektívnom režime
    ticks_since_last_change:  ticky od poslednej zmeny effective
    persistence_counter:      koľko tickov raw == kandidát (pred zmenou)
    """
    raw_regime:                MarketRegime
    effective_regime:          MarketRegime
    regime_changed:            bool
    change_allowed:            bool
    confidence:                float
    ticks_in_current_regime:   int
    ticks_since_last_change:   int
    persistence_counter:       int
    score_breakdown:           ScoreBreakdown

    # Akčné odporúčania (z effective_regime)
    allow_grid:                bool
    allow_new_buys:            bool
    allow_dca:                 bool
    protective_mode:           bool
    inventory_reduction_mode:  bool
    grid_bias:                 GridBias

    def summary(self) -> str:
        r = self.effective_regime.value
        c = self.confidence
        changed = f" ←{self.raw_regime.value}" if self.regime_changed else ""
        ticks   = self.ticks_in_current_regime
        pending = f" [cand:{self.persistence_counter}]" if self.persistence_counter > 0 else ""
        return (
            f"[REGIME] {r}{changed}{pending} "
            f"conf={c:.2f} ticks={ticks} "
            f"score={self.score_breakdown.smoothed_composite:+.3f} | "
            f"grid={'✓' if self.allow_grid else '✗'} "
            f"buy={'✓' if self.allow_new_buys else '✗'} "
            f"dca={'✓' if self.allow_dca else '✗'} "
            f"prot={'⚠' if self.protective_mode else '—'}"
        )


# Statická tabuľka obchodných pravidiel
_REGIME_RULES: dict[MarketRegime, dict] = {
    MarketRegime.RANGE: dict(
        allow_grid=True, allow_new_buys=True, allow_dca=True,
        protective_mode=False, inventory_reduction_mode=False,
        grid_bias=GridBias.NEUTRAL,
    ),
    MarketRegime.UPTREND: dict(
        allow_grid=True, allow_new_buys=True, allow_dca=True,
        protective_mode=False, inventory_reduction_mode=False,
        grid_bias=GridBias.DEFENSIVE_LONG,
    ),
    MarketRegime.DOWNTREND: dict(
        allow_grid=True, allow_new_buys=False, allow_dca=False,
        protective_mode=False, inventory_reduction_mode=True,
        grid_bias=GridBias.REDUCE_INVENTORY,
    ),
    MarketRegime.BREAKOUT_UP: dict(
        allow_grid=False, allow_new_buys=True, allow_dca=False,
        protective_mode=False, inventory_reduction_mode=False,
        grid_bias=GridBias.NEUTRAL,
    ),
    MarketRegime.BREAKOUT_DOWN: dict(
        allow_grid=False, allow_new_buys=False, allow_dca=False,
        protective_mode=True, inventory_reduction_mode=True,
        grid_bias=GridBias.REDUCE_INVENTORY,
    ),
    MarketRegime.PANIC: dict(
        allow_grid=False, allow_new_buys=False, allow_dca=False,
        protective_mode=True, inventory_reduction_mode=True,
        grid_bias=GridBias.REDUCE_INVENTORY,
    ),
    MarketRegime.UNDEFINED: dict(
        allow_grid=False, allow_new_buys=False, allow_dca=False,
        protective_mode=True, inventory_reduction_mode=False,
        grid_bias=GridBias.NEUTRAL,
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Indicator helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ema(closes: list[float], period: int) -> float:
    if len(closes) < period:
        return closes[-1] if closes else 0.0
    k   = 2.0 / (period + 1)
    val = sum(closes[:period]) / period
    for c in closes[period:]:
        val = c * k + val * (1 - k)
    return val


def _atr(klines: list[dict], period: int) -> float:
    if len(klines) < 2:
        return 0.0
    trs = [
        max(klines[i]["high"] - klines[i]["low"],
            abs(klines[i]["high"] - klines[i-1]["close"]),
            abs(klines[i]["low"]  - klines[i-1]["close"]))
        for i in range(1, len(klines))
    ]
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else 0.0
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _adx_proxy(klines: list[dict], period: int = 14) -> float:
    """0–1 normalizovaný ADX proxy (sila trendu bez smeru)."""
    if len(klines) < period + 2:
        return 0.0
    closes = [k["close"] for k in klines]
    ema_s  = _ema(closes, period)
    ema_f  = _ema(closes[-max(period//2, 5):], max(period//2, 5))
    atr_v  = _atr(klines[-period-1:], period)
    if atr_v <= 0 or ema_s <= 0:
        return 0.0
    diff_pct  = abs(ema_f - ema_s) / ema_s
    atr_pct   = atr_v / ema_s
    normalized = 1 - 1 / (1 + diff_pct / max(atr_pct * 1.5, 0.0001))
    return min(1.0, max(0.0, normalized))


def _hh_ll_score(klines: list[dict], lookback: int) -> float:
    window = klines[-lookback:]
    if len(window) < 4:
        return 0.0
    highs  = [k["high"]  for k in window]
    lows   = [k["low"]   for k in window]
    n      = len(highs)
    hh = sum(1 for i in range(1, n) if highs[i] > highs[i-1])
    ll = sum(1 for i in range(1, n) if lows[i]  < lows[i-1])
    hl = n - 1 - ll
    lh = n - 1 - hh
    up_score   = (hh + hl) / (2 * (n - 1))
    down_score = (ll + lh) / (2 * (n - 1))
    return up_score - down_score


def _atr_percentile(current_atr_pct: float, history: deque) -> float:
    if len(history) < 5:
        return 0.5
    return sum(1 for h in history if h <= current_atr_pct) / len(history)


# ─────────────────────────────────────────────────────────────────────────────
# RegimeDetector v2
# ─────────────────────────────────────────────────────────────────────────────

class RegimeDetector:
    """
    Produkčný market regime detektor s persistence, cooldown, hysteresis.

    Stavový model:
      _effective_regime  — aktuálne platný režim
      _candidate_regime  — navrhovaný nový režim (musí persistovať)
      _candidate_ticks   — počet tickov kde raw == kandidát
      _ticks_since_change — tickov od poslednej zmeny effective

    Zmena effective_regime nastane len ak:
      1. raw_regime == _candidate_regime po N persistenc tickoch
      2. confidence ≥ min_confidence_enter
      3. ticks_since_change ≥ cooldown_ticks
      VÝNIMKA: PANIC s conf ≥ panic_conf_override obíde body 2+3

    Použitie:
        detector = RegimeDetector(cfg)
        decision = detector.update(klines)
        # Vždy použi decision.effective_regime pre trading logiku
    """

    def __init__(self, cfg: Optional[RegimeConfig] = None):
        self.cfg                    = cfg or RegimeConfig()
        self._effective_regime:     MarketRegime = MarketRegime.UNDEFINED
        self._candidate_regime:     Optional[MarketRegime] = None
        self._candidate_ticks:      int   = 0
        self._ticks_since_change:   int   = 999
        self._ticks_in_regime:      int   = 0
        self._smoothed_score:       float = 0.0
        self._atr_pct_history:      deque[float] = deque(maxlen=200)
        self._score_history:        deque[float] = deque(maxlen=50)

    @property
    def effective_regime(self) -> MarketRegime:
        return self._effective_regime

    def update(self, klines: list[dict]) -> RegimeDecision:
        """Hlavná metóda — zavolaj každý tick."""
        cfg = self.cfg

        if len(klines) < cfg.min_bars:
            return self._make_decision(
                raw=MarketRegime.UNDEFINED,
                effective=MarketRegime.UNDEFINED,
                changed=False,
                change_allowed=False,
                breakdown=self._empty_breakdown(),
            )

        breakdown = self._compute_breakdown(klines)
        raw       = self._classify_raw(breakdown)

        # Update smoothed score history
        self._score_history.append(breakdown.smoothed_composite)

        # Panic override check
        is_panic           = raw == MarketRegime.PANIC
        panic_high_conf    = is_panic and breakdown.confidence >= cfg.panic_conf_override

        # Persistence tracking
        if raw == self._candidate_regime:
            self._candidate_ticks += 1
        else:
            self._candidate_regime = raw
            self._candidate_ticks  = 1

        # Zmena effective_regime?
        changed       = False
        change_allowed = False
        cooldown_ok   = self._ticks_since_change >= cfg.cooldown_ticks
        persistence_ok= self._candidate_ticks >= cfg.persistence_ticks
        confidence_ok = breakdown.confidence >= cfg.min_confidence_enter

        can_change = (cooldown_ok and persistence_ok and confidence_ok) or panic_high_conf

        if can_change and self._candidate_regime != self._effective_regime:
            old = self._effective_regime
            self._effective_regime   = self._candidate_regime
            self._ticks_since_change = 0
            self._ticks_in_regime    = 0
            self._candidate_ticks    = 0
            changed       = True
            change_allowed = True
            log.info(
                f"[REGIME] {old.value} → {self._effective_regime.value} "
                f"conf={breakdown.confidence:.2f} "
                f"score={breakdown.smoothed_composite:+.3f} "
                f"panic_override={panic_high_conf}"
            )

        self._ticks_since_change += 1
        self._ticks_in_regime    += 1

        return self._make_decision(
            raw=raw,
            effective=self._effective_regime,
            changed=changed,
            change_allowed=change_allowed,
            breakdown=breakdown,
        )

    # ── Výpočet breakdown ─────────────────────────────────────────────────────

    def _compute_breakdown(self, klines: list[dict]) -> ScoreBreakdown:
        cfg    = self.cfg
        closes = [k["close"] for k in klines]
        close  = closes[-1]

        # EMA slope
        ema_f      = _ema(closes, cfg.ema_fast_period)
        prev_window = closes[:-5] if len(closes) > cfg.ema_fast_period + 5 else closes[:-1]
        ema_f_prev = _ema(prev_window, cfg.ema_fast_period) if len(prev_window) >= cfg.ema_fast_period else ema_f
        ema_slope_pct = (ema_f - ema_f_prev) / ema_f_prev * 100 if ema_f_prev > 0 else 0.0
        ema_slope_score = max(-1.0, min(1.0, ema_slope_pct / 0.5))

        # ADX proxy (smer: ema_fast vs ema_slow)
        ema_s    = _ema(closes, cfg.ema_slow_period)
        adx_raw  = _adx_proxy(klines, cfg.adx_period)
        adx_dir  = 1.0 if ema_f > ema_s else -1.0
        adx_score = adx_raw * adx_dir

        # HH/LL
        hh_ll_raw   = _hh_ll_score(klines, cfg.lookback_swing)
        hh_ll_score = hh_ll_raw  # priamo [-1, +1]

        # ATR
        atr_v    = _atr(klines, cfg.atr_period)
        atr_pct  = atr_v / close if close > 0 else 0.0
        self._atr_pct_history.append(atr_pct)
        atr_perc = _atr_percentile(atr_pct, self._atr_pct_history)
        atr_score = 0.5 - atr_perc  # -0.5 (high vol) až +0.5 (low vol)

        # Momentum
        mp       = min(cfg.momentum_period, len(closes) - 1)
        mom_pct  = (close - closes[-mp-1]) / closes[-mp-1] * 100 if closes[-mp-1] > 0 else 0.0
        mom_score = max(-1.0, min(1.0, mom_pct / 3.0))

        # Composite
        raw_composite = (
            cfg.w_ema_slope * ema_slope_score +
            cfg.w_adx       * adx_score       +
            cfg.w_hh_ll     * hh_ll_score     +
            cfg.w_atr_pct   * atr_score       +
            cfg.w_momentum  * mom_score
        )

        # EWM smoothing
        alpha = cfg.smoothing_alpha
        self._smoothed_score = alpha * raw_composite + (1 - alpha) * self._smoothed_score

        # Breakout detekcia
        upper = ema_f + cfg.breakout_atr_mult * atr_v
        lower = ema_f - cfg.breakout_atr_mult * atr_v
        breakout_up   = close > upper
        breakout_down = close < lower

        # Panic: rýchly pokles za posledné 3 bary
        panic = False
        if len(closes) >= 4:
            drop = (closes[-4] - close) / closes[-4] * 100 if closes[-4] > 0 else 0
            panic = drop > cfg.panic_drop_pct_3bar

        # Confidence z vzdialenosti od prahu
        s      = self._smoothed_score
        thresh = cfg.uptrend_enter_score
        dist   = abs(abs(s) - thresh)
        conf   = min(1.0, max(0.0, dist / max(0.01, 1.0 - thresh)))

        # Panic confidence je priamo z hĺbky poklesu
        if panic:
            drop_val = (closes[-4] - close) / closes[-4] * 100 if closes[-4] > 0 else 0
            conf = min(1.0, drop_val / cfg.panic_drop_pct_3bar)

        return ScoreBreakdown(
            ema_slope_pct=ema_slope_pct, ema_slope_score=ema_slope_score,
            adx_proxy=adx_raw, adx_score=adx_score,
            hh_ll_raw=hh_ll_raw, hh_ll_score=hh_ll_score,
            atr_pct=atr_pct, atr_score=atr_score,
            momentum_pct=mom_pct, momentum_score=mom_score,
            raw_composite=raw_composite,
            smoothed_composite=self._smoothed_score,
            confidence=round(conf, 4),
            breakout_up=breakout_up, breakout_down=breakout_down,
            panic_detected=panic,
        )

    # ── Raw klasifikácia (hysteresis) ─────────────────────────────────────────

    def _classify_raw(self, bd: ScoreBreakdown) -> MarketRegime:
        """
        Hysteresis: vstupný prah ≠ výstupný prah.
        Aktuálny effective_regime ovplyvňuje výstupné prahy.
        """
        cfg = self.cfg

        # Hard override (panic > breakout > score)
        if bd.panic_detected:
            return MarketRegime.PANIC
        if bd.breakout_down:
            return MarketRegime.BREAKOUT_DOWN
        if bd.breakout_up:
            return MarketRegime.BREAKOUT_UP

        s       = bd.smoothed_composite
        current = self._effective_regime

        # Hysteresis pre UPTREND
        if current == MarketRegime.UPTREND:
            return MarketRegime.UPTREND if s >= cfg.uptrend_exit_score else MarketRegime.RANGE
        if current == MarketRegime.DOWNTREND:
            return MarketRegime.DOWNTREND if s <= cfg.downtrend_exit_score else MarketRegime.RANGE

        # Z RANGE: vyššie prahy pre vstup
        if s >= cfg.uptrend_enter_score:
            return MarketRegime.UPTREND
        if s <= cfg.downtrend_enter_score:
            return MarketRegime.DOWNTREND
        return MarketRegime.RANGE

    # ── Make Decision ─────────────────────────────────────────────────────────

    def _make_decision(
        self,
        raw:           MarketRegime,
        effective:     MarketRegime,
        changed:       bool,
        change_allowed: bool,
        breakdown:     ScoreBreakdown,
    ) -> RegimeDecision:
        rules = _REGIME_RULES[effective]
        return RegimeDecision(
            raw_regime               = raw,
            effective_regime         = effective,
            regime_changed           = changed,
            change_allowed           = change_allowed,
            confidence               = breakdown.confidence,
            ticks_in_current_regime  = self._ticks_in_regime,
            ticks_since_last_change  = self._ticks_since_change,
            persistence_counter      = self._candidate_ticks,
            score_breakdown          = breakdown,
            **rules,
        )

    def _empty_breakdown(self) -> ScoreBreakdown:
        return ScoreBreakdown(
            ema_slope_pct=0, ema_slope_score=0,
            adx_proxy=0, adx_score=0,
            hh_ll_raw=0, hh_ll_score=0,
            atr_pct=0, atr_score=0,
            momentum_pct=0, momentum_score=0,
            raw_composite=0, smoothed_composite=0,
            confidence=0,
            breakout_up=False, breakout_down=False, panic_detected=False,
        )
