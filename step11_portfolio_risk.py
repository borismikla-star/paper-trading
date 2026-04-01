"""
APEX BOT — Step 11: Portfolio Risk Engine
==========================================
Risk kontrola na úrovni celého portfólia.
Fail-safe: pri nevalidných dátach → PAUSE_NEW_RISK, nie NORMAL.

Risk režimy (v poradí závažnosti):
  NORMAL          → štandardný beh
  REDUCE_RISK     → menšie ordery, obmedzené DCA
  PAUSE_NEW_RISK  → žiadne nové pozície, držíme existujúce
  KILL_SWITCH     → okamžité zastavenie, redukcia inventory
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("ApexBot.PortRisk")


class PortfolioRiskMode(str, Enum):
    NORMAL         = "NORMAL"
    REDUCE_RISK    = "REDUCE_RISK"
    PAUSE_NEW_RISK = "PAUSE_NEW_RISK"
    KILL_SWITCH    = "KILL_SWITCH"


@dataclass
class PortfolioRiskConfig:
    """
    Konzervatívne defaulty pre BNB/USDT spot, 5k–20k USDT účet.
    """
    # Daily drawdown prahy
    daily_dd_reduce_pct:     float = 0.03   # 3% DD dnes → REDUCE
    daily_dd_pause_pct:      float = 0.05   # 5% DD dnes → PAUSE
    daily_dd_kill_pct:       float = 0.08   # 8% DD dnes → KILL

    # Rolling (7-dňový) drawdown
    rolling_dd_reduce_pct:   float = 0.07
    rolling_dd_pause_pct:    float = 0.12
    rolling_dd_kill_pct:     float = 0.18

    # Stop-loss udalosti
    sl_per_day_pause:        int   = 2      # 2+ SL za deň → PAUSE
    sl_per_week_kill:        int   = 5

    # DCA frekvencia
    dca_per_day_warn:        int   = 3
    dca_per_day_block:       int   = 5

    # Koncentrácia (jeden symbol / celé portfólio)
    max_concentration_pct:   float = 0.65   # > 65% v jednom symbole → reduce buys

    # Expozícia
    max_total_exposure_pct:  float = 0.70   # > 70% portfólia v otvorených pozíciách

    # Kombinovaný trigger (regime + DD)
    kill_on_panic_with_dd:   float = 0.05   # PANIC regime + 5% DD → KILL


@dataclass
class PortfolioRiskSnapshot:
    """Aktuálny stav portfólia pre risk engine."""
    portfolio_value:      float
    peak_today:           float
    peak_rolling:         float          # peak za posledných N dní
    realized_pnl_today:   float
    unrealized_pnl:       float
    total_exposure:       float          # USDT v otvorených pozíciách
    max_symbol_exposure:  float          # najväčšia expozícia jedného symbolu
    stop_loss_today:      int
    stop_loss_this_week:  int
    dca_today:            int
    market_regime:        str            # MarketRegime.value
    volatility_regime:    str            # VolatilityRegime.value

    def _safe_dd(self, peak: float) -> float:
        if peak <= 0 or self.portfolio_value <= 0:
            return 0.0
        return max(0.0, (peak - self.portfolio_value) / peak)

    @property
    def daily_dd(self) -> float:
        return self._safe_dd(self.peak_today)

    @property
    def rolling_dd(self) -> float:
        return self._safe_dd(self.peak_rolling)

    @property
    def concentration_pct(self) -> float:
        if self.portfolio_value <= 0:
            return 0.0
        return self.max_symbol_exposure / self.portfolio_value

    @property
    def exposure_pct(self) -> float:
        if self.portfolio_value <= 0:
            return 1.0
        return self.total_exposure / self.portfolio_value

    def validate(self) -> list[str]:
        """Vráti zoznam varovaní o nevalidných dátach."""
        warnings = []
        if self.portfolio_value <= 0:
            warnings.append("portfolio_value <= 0")
        if self.peak_today < self.portfolio_value:
            warnings.append("peak_today < portfolio_value (nekonzistentné)")
        if self.total_exposure < 0:
            warnings.append("total_exposure < 0")
        return warnings


@dataclass
class PortfolioRiskDecision:
    mode:                    PortfolioRiskMode
    allowed_new_orders:      bool
    allowed_new_buys:        bool
    allowed_dca:             bool
    max_exposure_multiplier: float    # 0–1, škáluje max exposure limit
    max_order_size_mult:     float    # 0–1, škáluje veľkosť orderu
    force_reduce_inventory:  bool
    trading_halt:            bool
    reason_codes:            list[str] = field(default_factory=list)

    def log_summary(self, logger: logging.Logger) -> None:
        icon = {"NORMAL": "✅", "REDUCE_RISK": "⚠️",
                "PAUSE_NEW_RISK": "🚫", "KILL_SWITCH": "🛑"}[self.mode.value]
        logger.info(
            f"{icon} PortRisk {self.mode.value} | "
            f"orders={'✓' if self.allowed_new_orders else '✗'} "
            f"buys={'✓' if self.allowed_new_buys else '✗'} "
            f"dca={'✓' if self.allowed_dca else '✗'} "
            f"halt={'YES' if self.trading_halt else 'no'} | "
            f"{'; '.join(self.reason_codes)}"
        )


# Statická tabuľka pre každý mode
_MODE_DEFAULTS: dict[PortfolioRiskMode, dict] = {
    PortfolioRiskMode.NORMAL: dict(
        allowed_new_orders=True, allowed_new_buys=True, allowed_dca=True,
        max_exposure_multiplier=1.0, max_order_size_mult=1.0,
        force_reduce_inventory=False, trading_halt=False,
    ),
    PortfolioRiskMode.REDUCE_RISK: dict(
        allowed_new_orders=True, allowed_new_buys=True, allowed_dca=False,
        max_exposure_multiplier=0.6, max_order_size_mult=0.5,
        force_reduce_inventory=False, trading_halt=False,
    ),
    PortfolioRiskMode.PAUSE_NEW_RISK: dict(
        allowed_new_orders=False, allowed_new_buys=False, allowed_dca=False,
        max_exposure_multiplier=0.3, max_order_size_mult=0.0,
        force_reduce_inventory=False, trading_halt=False,
    ),
    PortfolioRiskMode.KILL_SWITCH: dict(
        allowed_new_orders=False, allowed_new_buys=False, allowed_dca=False,
        max_exposure_multiplier=0.0, max_order_size_mult=0.0,
        force_reduce_inventory=True, trading_halt=True,
    ),
}


class PortfolioRiskEngine:
    """
    Risk engine na úrovni portfólia.

    Pravidlá sú vyhodnocované v poradí od najzávažnejšieho.
    Prvé triggnuté pravidlo určuje výsledný mode.
    Fail-safe: nevalidné vstupy → PAUSE_NEW_RISK.

    Príklad integrácie v hlavnom loope:
        risk_decision = risk_engine.evaluate(snapshot)
        if risk_decision.trading_halt:
            guardian.emergency_stop("PortfolioRiskEngine: KILL_SWITCH")
        if not risk_decision.allowed_new_buys:
            skip_buy_grid()
    """

    def __init__(self, cfg: Optional[PortfolioRiskConfig] = None):
        self.cfg = cfg or PortfolioRiskConfig()

    def evaluate(self, snap: PortfolioRiskSnapshot) -> PortfolioRiskDecision:
        """Vyhodnotí risk a vráti rozhodnutie."""
        # Fail-safe pri nevalidných dátach
        warnings = snap.validate()
        if warnings:
            log.error(f"[PortRisk] Nevalidné dáta: {warnings} → PAUSE_NEW_RISK")
            return self._decision(
                PortfolioRiskMode.PAUSE_NEW_RISK,
                [f"DATA_INVALID: {w}" for w in warnings]
            )

        cfg    = self.cfg
        codes: list[str] = []
        mode   = PortfolioRiskMode.NORMAL

        # ── KILL_SWITCH triggers ───────────────────────────────────────────
        if snap.daily_dd >= cfg.daily_dd_kill_pct:
            mode = PortfolioRiskMode.KILL_SWITCH
            codes.append(f"DAILY_DD_KILL: {snap.daily_dd:.2%}")

        elif snap.rolling_dd >= cfg.rolling_dd_kill_pct:
            mode = PortfolioRiskMode.KILL_SWITCH
            codes.append(f"ROLLING_DD_KILL: {snap.rolling_dd:.2%}")

        elif snap.stop_loss_this_week >= cfg.sl_per_week_kill:
            mode = PortfolioRiskMode.KILL_SWITCH
            codes.append(f"SL_WEEK_KILL: {snap.stop_loss_this_week}")

        elif (
            snap.market_regime == "PANIC"
            and snap.daily_dd >= cfg.kill_on_panic_with_dd
        ):
            mode = PortfolioRiskMode.KILL_SWITCH
            codes.append(f"PANIC_WITH_DD: {snap.daily_dd:.2%}")

        # ── PAUSE_NEW_RISK triggers ────────────────────────────────────────
        elif snap.daily_dd >= cfg.daily_dd_pause_pct:
            mode = PortfolioRiskMode.PAUSE_NEW_RISK
            codes.append(f"DAILY_DD_PAUSE: {snap.daily_dd:.2%}")

        elif snap.rolling_dd >= cfg.rolling_dd_pause_pct:
            mode = PortfolioRiskMode.PAUSE_NEW_RISK
            codes.append(f"ROLLING_DD_PAUSE: {snap.rolling_dd:.2%}")

        elif snap.stop_loss_today >= cfg.sl_per_day_pause:
            mode = PortfolioRiskMode.PAUSE_NEW_RISK
            codes.append(f"SL_DAY_PAUSE: {snap.stop_loss_today}")

        elif snap.exposure_pct >= cfg.max_total_exposure_pct:
            mode = PortfolioRiskMode.PAUSE_NEW_RISK
            codes.append(f"EXPOSURE_PAUSE: {snap.exposure_pct:.2%}")

        elif snap.dca_today >= cfg.dca_per_day_block:
            mode = PortfolioRiskMode.PAUSE_NEW_RISK
            codes.append(f"DCA_BLOCK: {snap.dca_today}")

        # ── REDUCE_RISK triggers ───────────────────────────────────────────
        elif snap.daily_dd >= cfg.daily_dd_reduce_pct:
            mode = PortfolioRiskMode.REDUCE_RISK
            codes.append(f"DAILY_DD_REDUCE: {snap.daily_dd:.2%}")

        elif snap.rolling_dd >= cfg.rolling_dd_reduce_pct:
            mode = PortfolioRiskMode.REDUCE_RISK
            codes.append(f"ROLLING_DD_REDUCE: {snap.rolling_dd:.2%}")

        elif snap.concentration_pct >= cfg.max_concentration_pct:
            mode = PortfolioRiskMode.REDUCE_RISK
            codes.append(f"CONCENTRATION: {snap.concentration_pct:.2%}")

        elif snap.dca_today >= cfg.dca_per_day_warn:
            mode = PortfolioRiskMode.REDUCE_RISK
            codes.append(f"DCA_WARN: {snap.dca_today}")

        elif snap.volatility_regime == "EXTREME":
            mode = PortfolioRiskMode.REDUCE_RISK
            codes.append("VOL_EXTREME")

        if not codes:
            codes.append("ALL_OK")

        decision = self._decision(mode, codes)
        decision.log_summary(log)
        return decision

    def _decision(self, mode: PortfolioRiskMode, codes: list[str]) -> PortfolioRiskDecision:
        return PortfolioRiskDecision(mode=mode, reason_codes=codes, **_MODE_DEFAULTS[mode])
