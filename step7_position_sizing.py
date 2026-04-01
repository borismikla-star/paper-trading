"""
APEX BOT — Step 7: Position Sizing
====================================
Risk-based position sizing framework pre grid trading.

Zodpovednosti:
  - Určiť veľkosť každého grid orderu v USDT
  - Adaptovať size na základe volatility, drawdownu, exposure
  - Blokovať ordery ak nie je dostatok risk budgetu
  - Osobitná logika pre DCA ordery

Integrácia:
  - Volá ho GridEngine pred každým place_grid()
  - Volá ho OrderManager pred DCA príkazom
  - Vstup: SizingContext (aktuálny stav portfólia + trhu)
  - Výstup: SizingDecision (schválená veľkosť alebo blokácia)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Protocol

log = logging.getLogger("ApexBot.Sizing")


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class SizingMode(str, Enum):
    FIXED_NOTIONAL    = "FIXED_NOTIONAL"     # fixná suma v USDT
    PCT_OF_EQUITY     = "PCT_OF_EQUITY"      # % z celkovej hodnoty portfólia
    RISK_BUDGET       = "RISK_BUDGET"        # max strata per order / risk budget
    VOLATILITY_ADJ    = "VOLATILITY_ADJ"     # ATR-based škálovanie
    DRAWDOWN_AWARE    = "DRAWDOWN_AWARE"     # redukcia pri drawdowne


class OrderIntent(str, Enum):
    GRID_BUY  = "GRID_BUY"
    GRID_SELL = "GRID_SELL"
    DCA       = "DCA"
    REBALANCE = "REBALANCE"


class SizingVerdict(str, Enum):
    APPROVED  = "APPROVED"    # order môže byť zadaný
    REDUCED   = "REDUCED"     # size bol znížený, ale order je možný
    BLOCKED   = "BLOCKED"     # order je zakázaný


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SizingConfig:
    """
    Všetky konfiguračné parametre position sizingu.

    Konzervat. default nastavenia:
      - mode = VOLATILITY_ADJ
      - base_order_usdt = 15.0
      - max_order_usdt = 50.0
      - max_symbol_exposure_pct = 0.60  (max 60% portfólia v jednom symbole)
      - max_grid_allocation_pct = 0.40  (max 40% v otvorených grid orderoch)
      - drawdown_reduce_start = 0.05    (pri 5% drawdowne začni redukovať)
      - drawdown_block_at = 0.15        (pri 15% drawdowne blokuj nové ordery)
    """
    # Základný sizing
    mode:                    SizingMode = SizingMode.VOLATILITY_ADJ
    base_order_usdt:         float = 15.0
    min_order_usdt:          float = 5.0       # Binance min notional + buffer
    max_order_usdt:          float = 50.0

    # Equity-based sizing
    equity_pct_per_order:    float = 0.015     # 1.5% portfólia per order

    # Risk budget sizing
    risk_per_order_pct:      float = 0.005     # 0.5% portfólia ako max strata per order
    stop_loss_estimate_pct:  float = 0.08      # odhadovaný SL pre výpočet risk budgetu

    # Exposure limity
    max_symbol_exposure_pct: float = 0.60      # max % portfólia v jednom symbole
    max_grid_allocation_pct: float = 0.40      # max % portfólia v otvorených orderoch
    max_single_order_pct:    float = 0.05      # max % portfólia v jednom orderi

    # Drawdown ochrana
    drawdown_reduce_start:   float = 0.05      # pri tomto DD začni škálovať dole
    drawdown_block_at:       float = 0.15      # pri tomto DD blokuj nové ordery
    drawdown_min_multiplier: float = 0.30      # minimálny size multiplier pri DD

    # Volatility adjustments
    atr_low_multiplier:      float = 0.80      # pri nízkej vol znížiť size
    atr_high_multiplier:     float = 0.60      # pri vysokej vol znížiť size ešte viac
    atr_extreme_multiplier:  float = 0.30      # pri extrémnej vol drasticky znížiť
    atr_low_threshold:       float = 0.005     # ATR% < 0.5% = low vol
    atr_high_threshold:      float = 0.020     # ATR% > 2.0% = high vol
    atr_extreme_threshold:   float = 0.040     # ATR% > 4.0% = extreme vol

    # DCA špecifické
    dca_size_multiplier:     float = 0.80      # DCA order je menší ako grid order
    dca_block_on_drawdown:   float = 0.10      # blokuj DCA pri 10% drawdowne
    dca_max_budget_pct:      float = 0.10      # max 10% portfólia pre DCA

    # Binance minimá
    binance_min_notional:    float = 5.0
    binance_min_qty:         float = 0.01


# ─────────────────────────────────────────────────────────────────────────────
# Context — aktuálny stav portfólia a trhu
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SizingContext:
    """
    Snapshot aktuálneho stavu potrebný pre sizing rozhodnutie.
    Caller je zodpovedný za správne naplnenie polí.
    """
    # Portfólio stav
    portfolio_value:     float          # celková hodnota (cash + inventory)
    cash_available:      float          # voľný USDT
    peak_portfolio:      float          # najvyššia hodnota portfólia (pre drawdown)
    open_grid_exposure:  float          # USDT viazané v otvorených grid orderoch
    symbol_exposure:     float          # USDT viazané v inventore daného symbolu

    # Grid kontext
    intent:              OrderIntent
    grid_levels:         int            # celkový počet grid úrovní
    current_price:       float

    # Trhový kontext
    atr_pct:             Optional[float] = None   # ATR ako % z ceny
    atr_avg_pct:         Optional[float] = None   # dlhodobý priemer ATR%

    # DCA kontext
    dca_spent_usdt:      float = 0.0
    last_dca_at:         Optional[float] = None   # timestamp posledného DCA

    # Metadata
    symbol:              str = ""

    @property
    def drawdown(self) -> float:
        """Aktuálny drawdown od peaku (0.0 – 1.0)."""
        if self.peak_portfolio <= 0:
            return 0.0
        return max(0.0, (self.peak_portfolio - self.portfolio_value) / self.peak_portfolio)

    @property
    def total_exposure_pct(self) -> float:
        """Celková expozícia ako % portfólia."""
        if self.portfolio_value <= 0:
            return 0.0
        return (self.open_grid_exposure + self.symbol_exposure) / self.portfolio_value


# ─────────────────────────────────────────────────────────────────────────────
# Decision — výstup sizing modulu
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SizingDecision:
    verdict:         SizingVerdict
    order_usdt:      float              # schválená veľkosť v USDT
    raw_usdt:        float              # pôvodná veľkosť pred redukciami
    multiplier:      float              # súčin všetkých aplikovaných multiplikátorov
    reasons:         list[str] = field(default_factory=list)
    blocked_reason:  Optional[str] = None

    @property
    def is_executable(self) -> bool:
        return self.verdict != SizingVerdict.BLOCKED

    def log_summary(self, logger: logging.Logger) -> None:
        tag = {
            SizingVerdict.APPROVED: "✅",
            SizingVerdict.REDUCED:  "⚠️",
            SizingVerdict.BLOCKED:  "🚫",
        }[self.verdict]
        msg = (
            f"{tag} Sizing {self.verdict.value}: "
            f"{self.raw_usdt:.2f} → {self.order_usdt:.2f} USDT "
            f"(mult={self.multiplier:.3f})"
        )
        if self.reasons:
            msg += f" | {'; '.join(self.reasons)}"
        logger.info(msg)


# ─────────────────────────────────────────────────────────────────────────────
# Sizing Strategy Protocol
# ─────────────────────────────────────────────────────────────────────────────

class SizingStrategy(Protocol):
    """Interface pre všetky sizing stratégie."""
    def compute_base_size(self, ctx: SizingContext, cfg: SizingConfig) -> float:
        """Vráti základnú veľkosť orderu v USDT pred risk filtrami."""
        ...


# ─────────────────────────────────────────────────────────────────────────────
# Konkrétne sizing stratégie
# ─────────────────────────────────────────────────────────────────────────────

class FixedNotionalStrategy:
    def compute_base_size(self, ctx: SizingContext, cfg: SizingConfig) -> float:
        return cfg.base_order_usdt


class PctOfEquityStrategy:
    def compute_base_size(self, ctx: SizingContext, cfg: SizingConfig) -> float:
        return ctx.portfolio_value * cfg.equity_pct_per_order


class RiskBudgetStrategy:
    """
    Size = (portfolio * risk_per_order_pct) / stop_loss_estimate_pct

    Príklad: portfólio=10000, risk=0.5%, SL=8%
    → size = (10000 * 0.005) / 0.08 = 625 USDT (pred ďalšími limitmi)
    """
    def compute_base_size(self, ctx: SizingContext, cfg: SizingConfig) -> float:
        if cfg.stop_loss_estimate_pct <= 0:
            return cfg.base_order_usdt
        return (ctx.portfolio_value * cfg.risk_per_order_pct) / cfg.stop_loss_estimate_pct


class VolatilityAdjustedStrategy:
    """
    Začni s PCT_OF_EQUITY a aplikuj ATR multiplier.
    Vyššia vol → menší order (zachovaj konštantný risk v USDT).
    """
    def compute_base_size(self, ctx: SizingContext, cfg: SizingConfig) -> float:
        base = ctx.portfolio_value * cfg.equity_pct_per_order
        if ctx.atr_pct is None:
            return base
        if ctx.atr_pct >= cfg.atr_extreme_threshold:
            return base * cfg.atr_extreme_multiplier
        if ctx.atr_pct >= cfg.atr_high_threshold:
            # Lineárna interpolácia medzi high a extreme
            t = (ctx.atr_pct - cfg.atr_high_threshold) / (
                cfg.atr_extreme_threshold - cfg.atr_high_threshold
            )
            return base * (cfg.atr_high_multiplier * (1 - t) + cfg.atr_extreme_multiplier * t)
        if ctx.atr_pct <= cfg.atr_low_threshold:
            return base * cfg.atr_low_multiplier
        return base


class DrawdownAwareStrategy:
    """
    Wrappuje inú stratégiu a aplikuje drawdown redukciu.
    """
    def __init__(self, inner: SizingStrategy):
        self._inner = inner

    def compute_base_size(self, ctx: SizingContext, cfg: SizingConfig) -> float:
        base = self._inner.compute_base_size(ctx, cfg)
        dd   = ctx.drawdown
        if dd <= cfg.drawdown_reduce_start:
            return base
        # Lineárna redukcia: drawdown_reduce_start → drawdown_block_at
        span = cfg.drawdown_block_at - cfg.drawdown_reduce_start
        if span <= 0:
            return base * cfg.drawdown_min_multiplier
        progress  = min(1.0, (dd - cfg.drawdown_reduce_start) / span)
        mult      = 1.0 - progress * (1.0 - cfg.drawdown_min_multiplier)
        return base * mult


def _build_strategy(cfg: SizingConfig) -> SizingStrategy:
    base_map: dict[SizingMode, SizingStrategy] = {
        SizingMode.FIXED_NOTIONAL: FixedNotionalStrategy(),
        SizingMode.PCT_OF_EQUITY:  PctOfEquityStrategy(),
        SizingMode.RISK_BUDGET:    RiskBudgetStrategy(),
        SizingMode.VOLATILITY_ADJ: VolatilityAdjustedStrategy(),
        SizingMode.DRAWDOWN_AWARE: DrawdownAwareStrategy(VolatilityAdjustedStrategy()),
    }
    strategy = base_map.get(cfg.mode, VolatilityAdjustedStrategy())
    # DRAWDOWN_AWARE je vždy obalená vrstva bez ohľadu na mode
    if cfg.mode != SizingMode.DRAWDOWN_AWARE:
        strategy = DrawdownAwareStrategy(strategy)
    return strategy


# ─────────────────────────────────────────────────────────────────────────────
# PositionSizer — hlavná trieda
# ─────────────────────────────────────────────────────────────────────────────

class PositionSizer:
    """
    Produkčný position sizer pre APEX BOT.

    Rozhodovací pipeline (v poradí):
      1. Hard blokácie (drawdown limit, exposure limit, cash)
      2. Base size výpočet (podľa zvoleného mode)
      3. Risk filtre (max single order, max symbol exposure)
      4. DCA-špecifická logika
      5. Binance minimum check
      6. Finálny clamp [min_order, max_order]

    Príklad použitia:
        sizer = PositionSizer(cfg)
        decision = sizer.size(ctx)
        if decision.is_executable:
            executor.buy(price, precision.qty_from_usdt(decision.order_usdt, price))
    """

    def __init__(self, cfg: SizingConfig):
        self.cfg      = cfg
        self._strategy = _build_strategy(cfg)

    def size(self, ctx: SizingContext) -> SizingDecision:
        reasons: list[str] = []

        # ── 1. Hard blokácie ────────────────────────────────────────────────
        block = self._check_hard_blocks(ctx, reasons)
        if block:
            return SizingDecision(
                verdict=SizingVerdict.BLOCKED,
                order_usdt=0.0,
                raw_usdt=0.0,
                multiplier=0.0,
                reasons=reasons,
                blocked_reason=block,
            )

        # ── 2. Base size ────────────────────────────────────────────────────
        raw = self._strategy.compute_base_size(ctx, self.cfg)

        # ── 3. DCA modifikácia ───────────────────────────────────────────────
        if ctx.intent == OrderIntent.DCA:
            dca_block = self._check_dca_block(ctx, reasons)
            if dca_block:
                return SizingDecision(
                    verdict=SizingVerdict.BLOCKED,
                    order_usdt=0.0, raw_usdt=raw, multiplier=0.0,
                    reasons=reasons, blocked_reason=dca_block,
                )
            raw *= self.cfg.dca_size_multiplier
            reasons.append(f"DCA mult={self.cfg.dca_size_multiplier:.2f}")

        # ── 4. Risk filtre — znižujú size, neblokujú ────────────────────────
        adjusted, mult_total = self._apply_risk_filters(raw, ctx, reasons)

        # ── 5. Binance minimum ───────────────────────────────────────────────
        if adjusted < self.cfg.binance_min_notional:
            return SizingDecision(
                verdict=SizingVerdict.BLOCKED,
                order_usdt=0.0, raw_usdt=raw, multiplier=mult_total,
                reasons=reasons,
                blocked_reason=f"Pod Binance min notional: {adjusted:.2f} < {self.cfg.binance_min_notional}",
            )

        if adjusted < self.cfg.min_order_usdt:
            return SizingDecision(
                verdict=SizingVerdict.BLOCKED,
                order_usdt=0.0, raw_usdt=raw, multiplier=mult_total,
                reasons=reasons,
                blocked_reason=f"Pod min_order_usdt: {adjusted:.2f} < {self.cfg.min_order_usdt}",
            )

        # ── 6. Clamp na max ──────────────────────────────────────────────────
        final  = min(adjusted, self.cfg.max_order_usdt)
        if final < adjusted:
            reasons.append(f"Clamped na max {self.cfg.max_order_usdt:.0f} USDT")

        verdict = SizingVerdict.APPROVED if final >= raw * 0.95 else SizingVerdict.REDUCED

        decision = SizingDecision(
            verdict=verdict,
            order_usdt=round(final, 4),
            raw_usdt=round(raw, 4),
            multiplier=round(mult_total * (final / adjusted if adjusted > 0 else 1), 4),
            reasons=reasons,
        )
        decision.log_summary(log)
        return decision

    # ── Interné metódy ────────────────────────────────────────────────────────

    def _check_hard_blocks(self, ctx: SizingContext, reasons: list[str]) -> Optional[str]:
        dd = ctx.drawdown
        if dd >= self.cfg.drawdown_block_at:
            return f"Drawdown {dd:.1%} ≥ blokačný limit {self.cfg.drawdown_block_at:.1%}"

        if ctx.cash_available < self.cfg.binance_min_notional:
            return f"Nedostatok cashu: {ctx.cash_available:.2f} USDT"

        sym_exp_pct = ctx.symbol_exposure / ctx.portfolio_value if ctx.portfolio_value > 0 else 0
        if sym_exp_pct >= self.cfg.max_symbol_exposure_pct:
            return (
                f"Max symbol exposure dosiahnutá: "
                f"{sym_exp_pct:.1%} ≥ {self.cfg.max_symbol_exposure_pct:.1%}"
            )

        grid_alloc_pct = ctx.open_grid_exposure / ctx.portfolio_value if ctx.portfolio_value > 0 else 0
        if grid_alloc_pct >= self.cfg.max_grid_allocation_pct:
            return (
                f"Max grid allocation dosiahnutá: "
                f"{grid_alloc_pct:.1%} ≥ {self.cfg.max_grid_allocation_pct:.1%}"
            )

        return None

    def _check_dca_block(self, ctx: SizingContext, reasons: list[str]) -> Optional[str]:
        dd = ctx.drawdown
        if dd >= self.cfg.dca_block_on_drawdown:
            return f"DCA blokovaný: drawdown {dd:.1%} ≥ {self.cfg.dca_block_on_drawdown:.1%}"

        dca_budget = ctx.portfolio_value * self.cfg.dca_max_budget_pct
        if ctx.dca_spent_usdt >= dca_budget:
            return f"DCA budget vyčerpaný: {ctx.dca_spent_usdt:.2f} / {dca_budget:.2f} USDT"

        # Blokuj DCA ak je ATR extrémny (silný downtrend)
        if ctx.atr_pct is not None and ctx.atr_pct >= self.cfg.atr_extreme_threshold:
            return f"DCA blokovaný: extrémna volatilita ATR={ctx.atr_pct:.3%}"

        return None

    def _apply_risk_filters(
        self, base: float, ctx: SizingContext, reasons: list[str]
    ) -> tuple[float, float]:
        size       = base
        multiplier = 1.0

        # Max single order % portfólia
        max_by_pct = ctx.portfolio_value * self.cfg.max_single_order_pct
        if size > max_by_pct:
            m    = max_by_pct / size
            size = max_by_pct
            multiplier *= m
            reasons.append(f"Max single order pct ({self.cfg.max_single_order_pct:.1%}): →{size:.2f}")

        # Nepoužij viac než dostupný cash
        if size > ctx.cash_available:
            m    = ctx.cash_available / size
            size = ctx.cash_available
            multiplier *= m
            reasons.append(f"Cash limit: →{size:.2f}")

        return size, multiplier

    def compute_grid_allocation(self, ctx: SizingContext) -> dict[str, float]:
        """
        Vypočíta optimálne rozdelenie kapitálu pre celý grid.
        Vracia odporúčané USDT per level a celkovú expozíciu.
        """
        decision    = self.size(ctx)
        if not decision.is_executable:
            return {"per_level": 0.0, "total": 0.0, "blocked": True}

        per_level   = decision.order_usdt
        total       = per_level * ctx.grid_levels
        max_allowed = ctx.portfolio_value * self.cfg.max_grid_allocation_pct

        if total > max_allowed:
            per_level = max_allowed / ctx.grid_levels
            total     = max_allowed

        return {
            "per_level": round(per_level, 4),
            "total":     round(total, 4),
            "blocked":   False,
            "levels":    ctx.grid_levels,
        }
