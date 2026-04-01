"""
APEX BOT — Step 15: Central Decision Engine
=============================================
Zjednocuje rozhodnutia zo všetkých ochranných vrstiev.
Jeden deterministický precedence model — žiadne roztrúsené if podmienky.

Precedence poradie (1 = najvyššia priorita):
  1. ExecutionSafety     — operačná bezpečnosť
  2. PortfolioRisk       — portfolio-level risk
  3. MarketRegime        — trhové podmienky
  4. InventoryRisk       — inventory stav
  5. VolatilityScaling   — volatility regime
  6. DCAGuardrails       — DCA-specific kontrola
  7. RebalanceDecision   — rebalance logika
  8. PositionSizing      — sizing (poradenský, nie blokujúci)

Garantované invariants:
  I1.  allow_trading=False → allow_new_orders=False
  I2.  allow_new_orders=False → allow_new_buys=False
  I3.  allow_new_orders=False → allow_requotes=False
  I4.  reduce_only_mode=True → allow_new_buys=False
  I5.  reduce_only_mode=True → allow_dca=False
  I6.  forced_cancel_all=True → allow_new_orders=False (tento tick)
  I7.  effective_regime=PANIC → allow_dca=False
  I8.  forced_inventory_reduction=True → allow_new_buys=False
  I9.  safe_to_trade=False (ExecSafety) → allow_trading=False (absolútna priorita)
  I10. allow_new_buys=False → allow_dca=False
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("ApexBot.DecisionEngine")


# ─────────────────────────────────────────────────────────────────────────────
# Reason codes (pre auditovateľnosť)
# ─────────────────────────────────────────────────────────────────────────────

class DecisionLayer(str, Enum):
    EXECUTION_SAFETY  = "EXECUTION_SAFETY"
    PORTFOLIO_RISK    = "PORTFOLIO_RISK"
    MARKET_REGIME     = "MARKET_REGIME"
    INVENTORY_RISK    = "INVENTORY_RISK"
    VOLATILITY        = "VOLATILITY"
    DCA_GUARDRAILS    = "DCA_GUARDRAILS"
    REBALANCE         = "REBALANCE"
    POSITION_SIZING   = "POSITION_SIZING"
    INVARIANT_ENFORCE = "INVARIANT_ENFORCE"
    DEFAULT           = "DEFAULT"


@dataclass
class LayerVote:
    """Hlas jednej vrstvy — pre audit trail."""
    layer:          DecisionLayer
    voted_allow:    bool          # True = vrstva hovorí "povolené"
    voted_block:    bool          # True = vrstva hovorí "blokuj"
    reason:         str
    override_used:  bool = False  # True = táto vrstva prehlasovala inú


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DecisionEngineConfig:
    """
    Minimálna konfigurácia — väčšina logiky je deterministická.
    """
    # Ak je execution safety DEGRADED (nie UNSAFE), povoliť obchodovanie?
    allow_trading_in_degraded: bool = True

    # Ak volatility = EXTREME, blokovať nové buy (okrem iných pravidiel)?
    block_buys_on_extreme_vol: bool = True

    # Logovať každý tick?
    log_every_tick: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Inputs — vstupy z jednotlivých modulov
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DecisionInputs:
    """
    Agregované vstupy zo všetkých vrstiev.
    Voliteľné polia = vrstva nemusí byť prítomná.
    Ak chýba kritická vrstva, engine použije konzervatívny default.

    Povinné:
      exec_safety    — vždy musí byť prítomné
    Odporúčané:
      portfolio_risk, regime, inventory, volatility
    Voliteľné:
      dca, rebalance, sizing
    """
    # Povinné
    exec_safe_to_trade:          bool
    exec_block_new_orders:       bool
    exec_block_requotes:         bool
    exec_trigger_circuit_breaker: bool
    exec_cancel_stale_orders:    bool
    exec_state:                  str    # ExecSafetyState.value

    # Portfolio risk (default = konzervatívny)
    port_risk_mode:              str    = "NORMAL"   # PortfolioRiskMode.value
    port_allowed_new_orders:     bool   = True
    port_allowed_new_buys:       bool   = True
    port_allowed_dca:            bool   = True
    port_max_order_mult:         float  = 1.0
    port_force_reduce_inventory: bool   = False
    port_trading_halt:           bool   = False

    # Market regime (default = konzervatívny UNDEFINED)
    regime_effective:            str    = "UNDEFINED"
    regime_allow_grid:           bool   = False
    regime_allow_new_buys:       bool   = False
    regime_allow_dca:            bool   = False
    regime_protective:           bool   = True
    regime_inv_reduction:        bool   = False
    regime_confidence:           float  = 0.0

    # Inventory risk
    inv_state:                   str    = "BALANCED"
    inv_allow_new_buys:          bool   = True
    inv_allow_dca:               bool   = True
    inv_force_reduction:         bool   = False
    inv_buy_size_mult:           float  = 1.0

    # Volatility
    vol_regime:                  str    = "NORMAL"
    vol_allow_dca:               bool   = True
    vol_allow_new_buys:          bool   = True
    vol_order_size_mult:         float  = 1.0
    vol_max_exposure_mult:       float  = 1.0

    # DCA Guardrails (voliteľné)
    dca_allow:                   Optional[bool]  = None
    dca_approved_usdt:           float           = 0.0
    dca_state:                   str             = "NORMAL"

    # Rebalance (voliteľné)
    rebalance_type:              Optional[str]   = None   # RebalanceType.value
    rebalance_cancel_orders:     bool            = False

    # Position sizing (poradenský)
    sizing_verdict:              Optional[str]   = None   # SizingVerdict.value
    sizing_order_usdt:           float           = 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Outcome — výstup DecisionEngine
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DecisionOutcome:
    """
    Finálne rozhodnutie — single gate pre celý trading loop.

    winning_layer:  vrstva, ktorá determinovala výsledok
    votes:          kompletný audit trail
    explanation:    ľudsky čitateľné vysvetlenie
    """
    allow_trading:              bool
    allow_new_orders:           bool
    allow_new_risk:             bool    # allow_new_orders AND nie reduce_only
    allow_new_buys:             bool
    allow_dca:                  bool
    reduce_only_mode:           bool
    allow_rebalance:            bool
    allow_requotes:             bool
    forced_cancel_all:          bool
    forced_inventory_reduction: bool
    order_size_multiplier:      float   # kombinovaný size mult
    max_exposure_multiplier:    float
    winning_layer:              DecisionLayer
    reason_codes:               list[str]
    votes:                      list[LayerVote]
    explanation:                str

    def __post_init__(self):
        """Enforcement všetkých invariants — poradie je kritické."""
        # I1: trading → orders
        if not self.allow_trading:
            object.__setattr__(self, "allow_new_orders", False)
        # I4 + I5: reduce_only → buys + dca
        if self.reduce_only_mode:
            object.__setattr__(self, "allow_new_buys", False)
            object.__setattr__(self, "allow_dca", False)
        # I6: forced_cancel → orders + buys + dca
        if self.forced_cancel_all:
            object.__setattr__(self, "allow_new_orders", False)
            object.__setattr__(self, "allow_new_buys", False)
            object.__setattr__(self, "allow_dca", False)
        # I8: forced_inv_reduction → buys
        if self.forced_inventory_reduction:
            object.__setattr__(self, "allow_new_buys", False)
        # I2: orders → buys (po všetkých orders-level enforcement)
        if not self.allow_new_orders:
            object.__setattr__(self, "allow_new_buys", False)
        # I10: buys → dca (po všetkých buy-level enforcement)
        if not self.allow_new_buys:
            object.__setattr__(self, "allow_dca", False)
        # I3: orders → requotes (POSLEDNÉ — po I6 ktorý môže nastaviť orders=False)
        if not self.allow_new_orders:
            object.__setattr__(self, "allow_requotes", False)
        # allow_new_risk
        if not self.allow_new_orders or self.reduce_only_mode:
            object.__setattr__(self, "allow_new_risk", False)

    def is_fully_blocked(self) -> bool:
        return not self.allow_trading

    def log_summary(self, logger: logging.Logger) -> None:
        icon = "✅" if self.allow_trading else ("⚠️" if self.allow_new_orders else "🛑")
        logger.info(
            f"{icon} Decision [{self.winning_layer.value}] | "
            f"trade={self.allow_trading} orders={self.allow_new_orders} "
            f"buys={self.allow_new_buys} dca={self.allow_dca} "
            f"reduce_only={self.reduce_only_mode} "
            f"cancel_all={self.forced_cancel_all} | "
            f"size×{self.order_size_multiplier:.2f} | "
            f"{'; '.join(self.reason_codes[:3])}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# DecisionEngine — hlavná trieda
# ─────────────────────────────────────────────────────────────────────────────

class DecisionEngine:
    """
    Centrálny decision engine pre APEX BOT.

    Rozhodovací pipeline (deterministický, vrstvený):

    Tier 1 — HARD BLOCKS (circuit breaker, trading halt):
      Ak ANY z týchto = True → allow_trading=False, všetko blokované

    Tier 2 — EXECUTION SAFETY GATE:
      Ak exec_safe_to_trade=False → allow_trading=False

    Tier 3 — PORTFOLIO RISK GATE:
      KILL_SWITCH → allow_trading=False
      PAUSE_NEW_RISK → allow_new_orders=False
      REDUCE_RISK → znížiť size mult, zakázať DCA

    Tier 4 — MARKET REGIME GATE:
      PANIC / BREAKOUT_DOWN → protective mode
      DOWNTREND → zakázať buy/DCA

    Tier 5 — INVENTORY GATE:
      REDUCE_ONLY / EXTREME_LONG → zakázať buy, force reduce

    Tier 6 — VOLATILITY GATE:
      EXTREME → zakázať buy (ak cfg.block_buys_on_extreme_vol)

    Tier 7 — DCA GATE:
      Ak dca guard hovorí nie → allow_dca=False

    Tier 8 — REBALANCE + CONSISTENCY:
      Ak rebalance chce cancel_orders ale exec blokuje requotes
      → BLOCK_REBALANCE

    Tier 9 — INVARIANT ENFORCEMENT (v __post_init__)

    Príklad:
        engine  = DecisionEngine(cfg)
        outcome = engine.decide(inputs)
        if outcome.forced_cancel_all:
            cancel_all_open_orders()
        if not outcome.allow_trading:
            return  # preskočiť tick
    """

    def __init__(self, cfg: Optional[DecisionEngineConfig] = None):
        self.cfg = cfg or DecisionEngineConfig()

    def decide(self, inp: DecisionInputs) -> DecisionOutcome:
        """Hlavná metóda — vráti deterministický DecisionOutcome."""
        cfg   = self.cfg
        votes: list[LayerVote] = []
        codes: list[str]       = []

        # Pracovné premenné (mutable počas pipeline)
        allow_trading       = True
        allow_new_orders    = True
        allow_new_buys      = True
        allow_dca           = True
        reduce_only         = False
        allow_rebalance     = True
        allow_requotes      = True
        forced_cancel_all   = False
        forced_inv_reduce   = False
        order_size_mult     = 1.0
        max_exp_mult        = 1.0
        winning_layer       = DecisionLayer.DEFAULT

        def block(
            layer:   DecisionLayer,
            reason:  str,
            *,
            trade:   bool = False,
            orders:  bool = False,
            buys:    bool = False,
            dca:     bool = False,
            rebal:   bool = False,
            cancel:  bool = False,
            inv_red: bool = False,
            red_only:bool = False,
            req:     bool = False,
        ):
            nonlocal allow_trading, allow_new_orders, allow_new_buys
            nonlocal allow_dca, allow_rebalance, forced_cancel_all
            nonlocal forced_inv_reduce, reduce_only, allow_requotes, winning_layer

            did_block = trade or orders or buys or dca or rebal or cancel or inv_red or red_only or req
            if trade:
                allow_trading    = False
            if orders or (not trade and not allow_trading):
                allow_new_orders = False
            if buys or red_only:
                allow_new_buys   = False
            if dca or red_only:
                allow_dca        = False
            if rebal:
                allow_rebalance  = False
            if cancel:
                forced_cancel_all = True
            if inv_red:
                forced_inv_reduce = True
            if red_only:
                reduce_only       = True
            if req:
                allow_requotes    = False

            votes.append(LayerVote(
                layer=layer, voted_allow=not did_block,
                voted_block=did_block, reason=reason,
                override_used=(did_block and winning_layer != DecisionLayer.DEFAULT),
            ))
            if did_block:
                winning_layer = layer
                codes.append(f"{layer.value}:{reason}")

        # ── Tier 1: Hard blocks ────────────────────────────────────────────

        if inp.exec_trigger_circuit_breaker or inp.port_trading_halt:
            reason = "CIRCUIT_BREAKER" if inp.exec_trigger_circuit_breaker else "PORTFOLIO_HALT"
            block(DecisionLayer.EXECUTION_SAFETY, reason,
                  trade=True, orders=True, buys=True, dca=True,
                  rebal=True, cancel=True, req=True)

        if inp.exec_cancel_stale_orders:
            block(DecisionLayer.EXECUTION_SAFETY, "CANCEL_STALE_ORDERS", cancel=True)

        # ── Tier 2: Execution Safety gate ─────────────────────────────────

        if not inp.exec_safe_to_trade and allow_trading:
            degraded = inp.exec_state == "DEGRADED"
            if degraded and cfg.allow_trading_in_degraded:
                # DEGRADED: povolíme obchodovanie ale nie requotes
                block(DecisionLayer.EXECUTION_SAFETY, "EXEC_DEGRADED_NO_REQUOTES", req=True)
            else:
                block(DecisionLayer.EXECUTION_SAFETY, f"EXEC_{inp.exec_state}",
                      trade=True, orders=True, buys=True, dca=True, req=True)

        if inp.exec_block_new_orders and allow_new_orders:
            block(DecisionLayer.EXECUTION_SAFETY, "EXEC_BLOCK_ORDERS",
                  orders=True, buys=True, dca=True, req=True)

        if inp.exec_block_requotes and allow_requotes:
            block(DecisionLayer.EXECUTION_SAFETY, "EXEC_BLOCK_REQUOTES", req=True)

        # ── Tier 3: Portfolio Risk gate ────────────────────────────────────

        if inp.port_risk_mode == "KILL_SWITCH" and allow_trading:
            block(DecisionLayer.PORTFOLIO_RISK, "PORTFOLIO_KILL",
                  trade=True, orders=True, buys=True, dca=True, inv_red=True)

        elif inp.port_risk_mode == "PAUSE_NEW_RISK" and allow_new_orders:
            block(DecisionLayer.PORTFOLIO_RISK, "PORTFOLIO_PAUSE",
                  orders=True, buys=True, dca=True)

        elif inp.port_risk_mode == "REDUCE_RISK":
            block(DecisionLayer.PORTFOLIO_RISK, "PORTFOLIO_REDUCE", dca=True)
            order_size_mult *= inp.port_max_order_mult
            codes.append(f"PORTFOLIO_RISK:size_mult={order_size_mult:.2f}")

        if inp.port_force_reduce_inventory and not forced_inv_reduce:
            block(DecisionLayer.PORTFOLIO_RISK, "PORT_FORCE_INV_REDUCE",
                  buys=True, inv_red=True)

        # ── Tier 4: Market Regime gate ─────────────────────────────────────

        regime = inp.regime_effective
        if regime in ("PANIC", "BREAKOUT_DOWN") and allow_trading:
            block(DecisionLayer.MARKET_REGIME, f"REGIME_{regime}",
                  buys=True, dca=True, red_only=True, inv_red=True)

        elif regime == "DOWNTREND" and allow_new_buys:
            block(DecisionLayer.MARKET_REGIME, "REGIME_DOWNTREND",
                  buys=True, dca=True, inv_red=True)

        elif regime == "UNDEFINED" and allow_trading:
            block(DecisionLayer.MARKET_REGIME, "REGIME_UNDEFINED",
                  orders=True, buys=True, dca=True)

        if not inp.regime_allow_grid and allow_rebalance:
            block(DecisionLayer.MARKET_REGIME, "REGIME_NO_GRID", rebal=True)

        if regime == "PANIC" and allow_dca:
            block(DecisionLayer.MARKET_REGIME, "PANIC_NO_DCA", dca=True)  # I7

        # ── Tier 5: Inventory gate ─────────────────────────────────────────

        if not inp.inv_allow_new_buys and allow_new_buys:
            block(DecisionLayer.INVENTORY_RISK, f"INV_{inp.inv_state}_NO_BUY", buys=True)

        if not inp.inv_allow_dca and allow_dca:
            block(DecisionLayer.INVENTORY_RISK, "INV_NO_DCA", dca=True)

        if inp.inv_force_reduction and not forced_inv_reduce:
            block(DecisionLayer.INVENTORY_RISK, "INV_FORCE_REDUCE",
                  buys=True, inv_red=True)

        # Kombinuj size multipliers
        order_size_mult  *= inp.inv_buy_size_mult
        max_exp_mult     *= inp.vol_max_exposure_mult

        # ── Tier 6: Volatility gate ────────────────────────────────────────

        if inp.vol_regime == "EXTREME":
            if cfg.block_buys_on_extreme_vol and allow_new_buys:
                block(DecisionLayer.VOLATILITY, "VOL_EXTREME_NO_BUY", buys=True)
            if not inp.vol_allow_dca and allow_dca:
                block(DecisionLayer.VOLATILITY, "VOL_EXTREME_NO_DCA", dca=True)

        order_size_mult *= inp.vol_order_size_mult

        # ── Tier 7: DCA gate ───────────────────────────────────────────────

        if inp.dca_allow is False and allow_dca:
            block(DecisionLayer.DCA_GUARDRAILS, f"DCA_{inp.dca_state}", dca=True)

        # ── Tier 8: Rebalance konzistencia ─────────────────────────────────

        if inp.rebalance_cancel_orders and inp.exec_block_requotes:
            block(DecisionLayer.REBALANCE, "REBAL_BLOCKED_BY_EXEC", rebal=True)
            codes.append("REBALANCE:blocked_by_exec_safety")

        # ── Zostavenie výsledku ────────────────────────────────────────────

        if not votes:
            votes.append(LayerVote(
                layer=DecisionLayer.DEFAULT, voted_allow=True,
                voted_block=False, reason="ALL_LAYERS_PASS",
            ))
            codes.append("ALL_OK")
            winning_layer = DecisionLayer.DEFAULT

        explanation = self._build_explanation(votes, winning_layer, inp)

        outcome = DecisionOutcome(
            allow_trading              = allow_trading,
            allow_new_orders           = allow_new_orders,
            allow_new_risk             = allow_new_orders and not reduce_only,
            allow_new_buys             = allow_new_buys,
            allow_dca                  = allow_dca,
            reduce_only_mode           = reduce_only,
            allow_rebalance            = allow_rebalance,
            allow_requotes             = allow_requotes,
            forced_cancel_all          = forced_cancel_all,
            forced_inventory_reduction = forced_inv_reduce,
            order_size_multiplier      = round(max(0.0, min(1.0, order_size_mult)), 4),
            max_exposure_multiplier    = round(max(0.0, min(1.0, max_exp_mult)), 4),
            winning_layer              = winning_layer,
            reason_codes               = codes,
            votes                      = votes,
            explanation                = explanation,
        )

        if cfg.log_every_tick:
            outcome.log_summary(log)

        return outcome

    def _build_explanation(
        self,
        votes:         list[LayerVote],
        winning_layer: DecisionLayer,
        inp:           DecisionInputs,
    ) -> str:
        blocking = [v for v in votes if v.voted_block]
        if not blocking:
            return (
                f"Všetky vrstvy povolili obchodovanie. "
                f"Regime: {inp.regime_effective}, Vol: {inp.vol_regime}"
            )
        primary = blocking[0]
        others  = [v.layer.value for v in blocking[1:]]
        out     = (
            f"Blokujúca vrstva: {primary.layer.value} ({primary.reason}). "
        )
        if others:
            out += f"Ďalšie obmedzenia: {', '.join(others)}. "
        out += (
            f"Regime: {inp.regime_effective} (conf={inp.regime_confidence:.2f}), "
            f"ExecState: {inp.exec_state}, "
            f"PortRisk: {inp.port_risk_mode}"
        )
        return out

    def audit_invariants(self, outcome: DecisionOutcome) -> list[str]:
        """
        Overí všetky invariants — vráti zoznam porušení.
        Prázdny zoznam = všetko OK.
        """
        violations: list[str] = []

        if not outcome.allow_trading and outcome.allow_new_orders:
            violations.append("I1: allow_trading=False ale allow_new_orders=True")
        if not outcome.allow_new_orders and outcome.allow_new_buys:
            violations.append("I2: allow_new_orders=False ale allow_new_buys=True")
        if not outcome.allow_new_orders and outcome.allow_requotes:
            violations.append("I3: allow_new_orders=False ale allow_requotes=True")
        if outcome.reduce_only_mode and outcome.allow_new_buys:
            violations.append("I4: reduce_only_mode=True ale allow_new_buys=True")
        if outcome.reduce_only_mode and outcome.allow_dca:
            violations.append("I5: reduce_only_mode=True ale allow_dca=True")
        if outcome.forced_cancel_all and outcome.allow_new_orders:
            violations.append("I6: forced_cancel_all=True ale allow_new_orders=True")
        if not outcome.allow_new_buys and outcome.allow_dca:
            violations.append("I10: allow_new_buys=False ale allow_dca=True")

        return violations
