"""
APEX BOT — Step 12: Inventory Risk Management
===============================================
Riadi inventory risk pri grid tradingu.
Grid stratégia prirodzene akumuluje inventory — tento modul kontroluje,
aby sa bot neprepol do nechcenej directional long pozície.

Inventory stavy:
  BALANCED      → inventory v cieľovom pásme
  HEAVY_LONG    → nad hornou hranicou pásma
  EXTREME_LONG  → výrazne nad limitom
  REDUCE_ONLY   → aktívna redukcia (kombinácia stavu + trhu)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("ApexBot.InvRisk")


class InventoryState(str, Enum):
    BALANCED      = "BALANCED"
    HEAVY_LONG    = "HEAVY_LONG"
    EXTREME_LONG  = "EXTREME_LONG"
    REDUCE_ONLY   = "REDUCE_ONLY"


@dataclass
class InventoryConfig:
    """
    Cieľové pásmo inventory pre BNB/USDT spot grid.

    target_inventory_pct: ideálne % portfólia v base assete (coin)
    upper_band_pct:       horná hranica pred redukciou
    hard_limit_pct:       absolútna horná hranica → EXTREME
    lower_band_pct:       spodná hranica (príliš málo coinu)

    Príklad pre 10k USDT portfólio:
      target = 20%  → 2000 USDT v BNB
      upper  = 35%  → HEAVY_LONG nad touto hranicou
      hard   = 50%  → EXTREME nad touto hranicou
    """
    target_inventory_pct: float = 0.20
    upper_band_pct:       float = 0.35
    hard_limit_pct:       float = 0.50
    lower_band_pct:       float = 0.05

    # Multipliere na veľkosť BUY orderov pri nadmernej inventory
    heavy_long_buy_mult:    float = 0.40   # len 40% normálnej veľkosti
    extreme_long_buy_mult:  float = 0.0    # blokuj BUY
    reduce_only_buy_mult:   float = 0.0

    # Sell aggressiveness (1.0 = normálny grid, >1.0 = viac/skôr predávaj)
    heavy_long_sell_mult:   float = 1.3
    extreme_long_sell_mult: float = 1.6
    reduce_only_sell_mult:  float = 2.0

    # Podmienky pre REDUCE_ONLY mode (kombinácia stavu + trhu)
    reduce_on_downtrend:    bool  = True
    reduce_on_high_vol:     bool  = True
    reduce_on_extreme_long: bool  = True

    # Rebalance bias: pri HEAVY_LONG posuň grid center nadol
    bias_shift_pct_heavy:   float = -0.3  # -0.3% shift grid base price
    bias_shift_pct_extreme: float = -0.8


@dataclass
class InventorySnapshot:
    """Aktuálny stav inventory."""
    coin_qty:          float
    coin_market_value: float      # coin_qty × current_price
    portfolio_value:   float
    avg_buy_price:     float
    current_price:     float
    unrealized_pnl:    float
    market_regime:     str        # z step10
    volatility_regime: str        # z step8
    recent_buy_count:  int        # počet BUY fillov za posledných N barov
    recent_sell_count: int

    @property
    def inventory_pct(self) -> float:
        if self.portfolio_value <= 0:
            return 0.0
        return self.coin_market_value / self.portfolio_value

    @property
    def fill_imbalance(self) -> float:
        """Kladné = viac BUY fillov ako SELL (akumulácia)."""
        total = self.recent_buy_count + self.recent_sell_count
        if total == 0:
            return 0.0
        return (self.recent_buy_count - self.recent_sell_count) / total

    def validate(self) -> list[str]:
        errs = []
        if self.portfolio_value <= 0:
            errs.append("portfolio_value <= 0")
        if self.coin_qty < 0:
            errs.append("coin_qty < 0")
        if self.current_price <= 0:
            errs.append("current_price <= 0")
        return errs


@dataclass
class InventoryDecision:
    inventory_state:             InventoryState
    buy_size_multiplier:         float
    sell_aggressiveness_mult:    float
    allow_new_buys:              bool
    allow_dca:                   bool
    force_inventory_reduction:   bool
    rebalance_bias_shift:        float    # % posun grid base price
    target_inventory_pct:        float
    current_inventory_pct:       float
    reason_codes:                list[str] = field(default_factory=list)

    def log_summary(self, logger: logging.Logger) -> None:
        icon = {
            "BALANCED":     "✅",
            "HEAVY_LONG":   "⚠️",
            "EXTREME_LONG": "🔴",
            "REDUCE_ONLY":  "🛑",
        }[self.inventory_state.value]
        logger.info(
            f"{icon} Inventory {self.inventory_state.value} "
            f"({self.current_inventory_pct:.1%} / target {self.target_inventory_pct:.1%}) | "
            f"buy_mult={self.buy_size_multiplier:.2f} "
            f"sell_mult={self.sell_aggressiveness_mult:.2f} | "
            f"{'; '.join(self.reason_codes)}"
        )


class InventoryRiskManager:
    """
    Riadi inventory risk a vracia priame akčné odporúčania.

    Integrácia:
        inv_decision = inv_manager.evaluate(snap)
        # V GridEngine:
        effective_buy_size = base_size * inv_decision.buy_size_multiplier
        if not inv_decision.allow_new_buys:
            skip_buy_orders()
        # V AdvancedRebalance:
        bias_shift = inv_decision.rebalance_bias_shift
    """

    def __init__(self, cfg: Optional[InventoryConfig] = None):
        self.cfg = cfg or InventoryConfig()

    def evaluate(self, snap: InventorySnapshot) -> InventoryDecision:
        cfg = self.cfg
        errs = snap.validate()
        if errs:
            log.error(f"[InvRisk] Nevalidné dáta: {errs} → REDUCE_ONLY")
            return self._make(
                InventoryState.REDUCE_ONLY, cfg,
                snap.inventory_pct, [f"DATA_INVALID: {e}" for e in errs]
            )

        inv_pct = snap.inventory_pct
        codes: list[str] = []

        # ── Klasifikácia inventory stavu ──────────────────────────────────
        if inv_pct >= cfg.hard_limit_pct:
            base_state = InventoryState.EXTREME_LONG
            codes.append(f"EXTREME: {inv_pct:.1%} ≥ {cfg.hard_limit_pct:.1%}")
        elif inv_pct >= cfg.upper_band_pct:
            base_state = InventoryState.HEAVY_LONG
            codes.append(f"HEAVY: {inv_pct:.1%} ≥ {cfg.upper_band_pct:.1%}")
        else:
            base_state = InventoryState.BALANCED

        # ── Eskalácia do REDUCE_ONLY ──────────────────────────────────────
        final_state = base_state
        if base_state in (InventoryState.EXTREME_LONG, InventoryState.HEAVY_LONG):
            downtrend  = snap.market_regime in ("DOWNTREND", "BREAKOUT_DOWN", "PANIC")
            high_vol   = snap.volatility_regime in ("HIGH", "EXTREME")
            if (
                (cfg.reduce_on_extreme_long and base_state == InventoryState.EXTREME_LONG) or
                (cfg.reduce_on_downtrend and downtrend) or
                (cfg.reduce_on_high_vol and high_vol and base_state == InventoryState.EXTREME_LONG)
            ):
                final_state = InventoryState.REDUCE_ONLY
                codes.append(
                    f"→REDUCE_ONLY: "
                    f"extreme={base_state == InventoryState.EXTREME_LONG} "
                    f"down={downtrend} highvol={high_vol}"
                )

        # Varovanie pri fill imbalance (akumulácia bez predaja)
        if snap.fill_imbalance > 0.6 and base_state != InventoryState.BALANCED:
            codes.append(f"FILL_IMBALANCE: {snap.fill_imbalance:.2f}")

        decision = self._make(final_state, cfg, inv_pct, codes)
        decision.log_summary(log)
        return decision

    def _make(
        self,
        state:   InventoryState,
        cfg:     InventoryConfig,
        inv_pct: float,
        codes:   list[str],
    ) -> InventoryDecision:
        if state == InventoryState.BALANCED:
            buy_mult  = 1.0
            sell_mult = 1.0
            allow_buy = True
            allow_dca = True
            force_red = False
            bias      = 0.0
        elif state == InventoryState.HEAVY_LONG:
            buy_mult  = cfg.heavy_long_buy_mult
            sell_mult = cfg.heavy_long_sell_mult
            allow_buy = buy_mult > 0
            allow_dca = False
            force_red = False
            bias      = cfg.bias_shift_pct_heavy
        elif state == InventoryState.EXTREME_LONG:
            buy_mult  = cfg.extreme_long_buy_mult
            sell_mult = cfg.extreme_long_sell_mult
            allow_buy = False
            allow_dca = False
            force_red = True
            bias      = cfg.bias_shift_pct_extreme
        else:  # REDUCE_ONLY
            buy_mult  = 0.0
            sell_mult = cfg.reduce_only_sell_mult
            allow_buy = False
            allow_dca = False
            force_red = True
            bias      = cfg.bias_shift_pct_extreme

        return InventoryDecision(
            inventory_state            = state,
            buy_size_multiplier        = buy_mult,
            sell_aggressiveness_mult   = sell_mult,
            allow_new_buys             = allow_buy,
            allow_dca                  = allow_dca,
            force_inventory_reduction  = force_red,
            rebalance_bias_shift       = bias,
            target_inventory_pct       = cfg.target_inventory_pct,
            current_inventory_pct      = inv_pct,
            reason_codes               = codes,
        )
