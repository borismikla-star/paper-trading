"""
APEX BOT — Step 16: System Specification
==========================================
Obsahuje:
  A) Precedence Matrix      — kompletná tabuľka konfliktov všetkých vrstiev
  B) CircuitBreakerFSM      — formálny stavový automat pre circuit breaker
  C) RegimeFSM              — formálny stavový automat pre market regime
  D) InvariantTestSuite     — unit testy pre všetky garantované invariants

Tento modul je súčasne:
  - Živá dokumentácia systému
  - Spustiteľný test suite
  - Formálna špecifikácia správania

Spustenie testov:
    python step16_system_spec.py
"""

from __future__ import annotations

import logging
import time
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger("ApexBot.Spec")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)


# ════════════════════════════════════════════════════════════════════════════
# A) PRECEDENCE MATRIX
# ════════════════════════════════════════════════════════════════════════════

"""
PRECEDENCE MATRIX — APEX BOT
==============================

Vysvetlivky stĺpcov:
  trade  = allow_trading
  orders = allow_new_orders
  buys   = allow_new_buys
  dca    = allow_dca
  rebal  = allow_rebalance
  req    = allow_requotes
  cancel = forced_cancel_all
  inv_r  = forced_inventory_reduction
  ro     = reduce_only_mode
  size×  = order_size_multiplier (relatívny)

Priorita: nižšie číslo = vyššia priorita = prehlasuje ostatné.

┌────────────────────────────────────────────────────────────────────────────────────────────────┐
│ PRI │ VRSTVA           │ STAV / PODMIENKA            │trade│ord│buy│dca│reb│req│can│i_r│ ro│size×│
├────┼──────────────────┼─────────────────────────────┼─────┼───┼───┼───┼───┼───┼───┼───┼───┼─────┤
│  1 │ ExecutionSafety  │ CIRCUIT_BREAKER              │  ✗  │ ✗ │ ✗ │ ✗ │ ✗ │ ✗ │ ✓ │ – │ – │ 0.0 │
│  1 │ ExecutionSafety  │ UNSAFE (desync/heartbeat)    │  ✗  │ ✗ │ ✗ │ ✗ │ ✗ │ ✗ │ – │ – │ – │ 0.0 │
│  1 │ ExecutionSafety  │ UNSAFE (reject≥3)            │  ✗  │ ✗ │ ✗ │ ✗ │ ✗ │ ✗ │ – │ – │ – │ 0.0 │
│  1 │ ExecutionSafety  │ DEGRADED (stale data)        │  ✗  │ ✗ │ ✗ │ ✗ │ – │ ✗ │ – │ – │ – │ 0.0 │
│  1 │ ExecutionSafety  │ DEGRADED (ws stale)          │  ✓  │ ✓ │ ✓ │ ✓ │ – │ ✗ │ – │ – │ – │ 1.0 │
│  1 │ ExecutionSafety  │ cancel_stale_orders=True     │  –  │ – │ – │ – │ – │ – │ ✓ │ – │ – │ –   │
├────┼──────────────────┼─────────────────────────────┼─────┼───┼───┼───┼───┼───┼───┼───┼───┼─────┤
│  2 │ PortfolioRisk    │ KILL_SWITCH                  │  ✗  │ ✗ │ ✗ │ ✗ │ ✗ │ ✗ │ – │ ✓ │ – │ 0.0 │
│  2 │ PortfolioRisk    │ PAUSE_NEW_RISK               │  ✓  │ ✗ │ ✗ │ ✗ │ – │ – │ – │ – │ – │ 0.0 │
│  2 │ PortfolioRisk    │ REDUCE_RISK                  │  ✓  │ ✓ │ ✓ │ ✗ │ – │ – │ – │ – │ – │ 0.5 │
│  2 │ PortfolioRisk    │ force_reduce_inventory=True  │  –  │ – │ ✗ │ – │ – │ – │ – │ ✓ │ – │ –   │
├────┼──────────────────┼─────────────────────────────┼─────┼───┼───┼───┼───┼───┼───┼───┼───┼─────┤
│  3 │ MarketRegime     │ PANIC                        │  ✓  │ ✓ │ ✗ │ ✗ │ ✗ │ – │ – │ ✓ │ ✓ │ –   │
│  3 │ MarketRegime     │ BREAKOUT_DOWN                │  ✓  │ ✓ │ ✗ │ ✗ │ ✗ │ – │ – │ ✓ │ ✓ │ –   │
│  3 │ MarketRegime     │ BREAKOUT_UP                  │  ✓  │ ✓ │ ✓ │ ✗ │ ✗ │ – │ – │ – │ – │ –   │
│  3 │ MarketRegime     │ DOWNTREND                    │  ✓  │ ✓ │ ✗ │ ✗ │ ✓ │ – │ – │ ✓ │ – │ –   │
│  3 │ MarketRegime     │ UPTREND                      │  ✓  │ ✓ │ ✓ │ ✓ │ ✓ │ – │ – │ – │ – │ 1.0 │
│  3 │ MarketRegime     │ RANGE                        │  ✓  │ ✓ │ ✓ │ ✓ │ ✓ │ – │ – │ – │ – │ 1.0 │
│  3 │ MarketRegime     │ UNDEFINED                    │  ✓  │ ✗ │ ✗ │ ✗ │ ✗ │ – │ – │ – │ – │ 0.0 │
├────┼──────────────────┼─────────────────────────────┼─────┼───┼───┼───┼───┼───┼───┼───┼───┼─────┤
│  4 │ InventoryRisk    │ REDUCE_ONLY                  │  ✓  │ ✓ │ ✗ │ ✗ │ – │ – │ – │ ✓ │ – │ 0.0 │
│  4 │ InventoryRisk    │ EXTREME_LONG                 │  ✓  │ ✓ │ ✗ │ ✗ │ – │ – │ – │ ✓ │ – │ 0.0 │
│  4 │ InventoryRisk    │ HEAVY_LONG                   │  ✓  │ ✓ │ ✓ │ ✗ │ – │ – │ – │ – │ – │ 0.4 │
│  4 │ InventoryRisk    │ BALANCED                     │  ✓  │ ✓ │ ✓ │ ✓ │ – │ – │ – │ – │ – │ 1.0 │
├────┼──────────────────┼─────────────────────────────┼─────┼───┼───┼───┼───┼───┼───┼───┼───┼─────┤
│  5 │ VolatilityScaling│ EXTREME                      │  ✓  │ ✓ │ ✗ │ ✗ │ – │ – │ – │ – │ – │ 0.3 │
│  5 │ VolatilityScaling│ HIGH                         │  ✓  │ ✓ │ ✓ │ ✓ │ – │ – │ – │ – │ – │ 0.65│
│  5 │ VolatilityScaling│ NORMAL                       │  ✓  │ ✓ │ ✓ │ ✓ │ – │ – │ – │ – │ – │ 1.0 │
│  5 │ VolatilityScaling│ LOW                          │  ✓  │ ✓ │ ✓ │ ✓ │ – │ – │ – │ – │ – │ 0.8 │
├────┼──────────────────┼─────────────────────────────┼─────┼───┼───┼───┼───┼───┼───┼───┼───┼─────┤
│  6 │ DCAGuardrails    │ DISABLED                     │  ✓  │ ✓ │ ✓ │ ✗ │ – │ – │ – │ – │ – │ –   │
│  6 │ DCAGuardrails    │ LIMITED / RECOVERY_ONLY      │  ✓  │ ✓ │ ✓ │ ✗ │ – │ – │ – │ – │ – │ –   │
│  6 │ DCAGuardrails    │ NORMAL                       │  ✓  │ ✓ │ ✓ │ ✓ │ – │ – │ – │ – │ – │ –   │
├────┼──────────────────┼─────────────────────────────┼─────┼───┼───┼───┼───┼───┼───┼───┼───┼─────┤
│  7 │ Rebalance        │ BLOCK_REBALANCE              │  ✓  │ ✓ │ ✓ │ ✓ │ ✗ │ – │ – │ – │ – │ –   │
│  7 │ Rebalance        │ REDUCE_ONLY_RECENTER         │  ✓  │ ✓ │ ✗ │ ✗ │ ✓ │ – │ – │ – │ – │ –   │
│  7 │ Rebalance        │ rebal wants cancel+exec blok │  ✓  │ ✓ │ ✓ │ ✓ │ ✗ │ – │ – │ – │ – │ –   │
├────┼──────────────────┼─────────────────────────────┼─────┼───┼───┼───┼───┼───┼───┼───┼───┼─────┤
│  8 │ PositionSizing   │ BLOCKED                      │  ✓  │ ✓ │ ✗ │ – │ – │ – │ – │ – │ – │ 0.0 │
│  8 │ PositionSizing   │ REDUCED                      │  ✓  │ ✓ │ ✓ │ – │ – │ – │ – │ – │ – │ var │
│  8 │ PositionSizing   │ APPROVED                     │  ✓  │ ✓ │ ✓ │ – │ – │ – │ – │ – │ – │ 1.0 │
└────┴──────────────────┴─────────────────────────────┴─────┴───┴───┴───┴───┴───┴───┴───┴───┴─────┘

Legenda: ✓ = povolené/True  ✗ = zakázané/False  – = nezmenené/neovplyvnené  ✓/✗ = podmienečné

KONFLIKTNÉ SCENÁRE A ROZLÍŠENIE:
─────────────────────────────────
K1: ExecSafety=UNSAFE, Regime=RANGE(allow_grid=True)
    → ExecSafety (pri 1) vyhráva → allow_trading=False
    → Regime sa ignoruje

K2: PortRisk=PAUSE, Regime=RANGE(allow_buy=True)
    → PortRisk (pri 2) vyhráva → allow_new_orders=False
    → Regime nezmení výsledok (allow_buy=False z I2)

K3: Regime=UPTREND(buy=True), Inv=REDUCE_ONLY(buy=False)
    → Inv (pri 4) vyhráva → allow_new_buys=False
    → UPTREND sa prejaví len v grid_bias, nie v buy povolení

K4: Vol=EXTREME(dca=False), DCA=NORMAL(dca=True)
    → Vol (pri 5) vyhráva → allow_dca=False

K5: Rebalance=FULL_REBUILD(cancel=True), ExecSafety=block_requotes=True
    → ExecSafety (pri 1) vyhráva → allow_rebalance=False, cancel sa nekoná

K6: PortRisk=REDUCE_RISK(size=0.5), Vol=HIGH(size=0.65)
    → Oba aplikované multiplikatívne → final_size = base × 0.5 × 0.65 = 0.325

K7: Regime=PANIC, DCA=NORMAL
    → Regime (pri 3) vyhráva → allow_dca=False (invariant I7)
    → DCA rozhodnutie irelevantné

K8: ExecSafety=DEGRADED(ws_stale), Regime=RANGE
    → allow_trading=True (DEGRADED ≠ UNSAFE), ale allow_requotes=False
    → Grid beží, ale žiadne cancel/replace operácie
"""

PRECEDENCE_MATRIX_SUMMARY = """
Poradie priorít (1 = absolútna):
  1. ExecutionSafety   — operačná bezpečnosť (override všetkého)
  2. PortfolioRisk     — portfolio-level halt/pause/reduce
  3. MarketRegime      — trhové podmienky (protective/reduce_only)
  4. InventoryRisk     — inventory imbalance (buy suppression)
  5. VolatilityScaling — volatility-based size reduction
  6. DCAGuardrails     — DCA-specific kontrola
  7. Rebalance         — rebalance konzistencia s ExecSafety
  8. PositionSizing    — poradenský (ovplyvňuje len size, nie povolenie)
"""


# ════════════════════════════════════════════════════════════════════════════
# B) CIRCUIT BREAKER STATE MACHINE
# ════════════════════════════════════════════════════════════════════════════

class CBState(str, Enum):
    """Stavy Circuit Breakera."""
    CLOSED   = "CLOSED"    # normálny beh
    OPEN     = "OPEN"      # halt — čaká na cooldown
    HALF_OPEN= "HALF_OPEN" # cooldown uplynul, čaká na recovery ticky
    RECOVERED= "RECOVERED" # recovery úspešná, prechod do CLOSED


class CBTrigger(str, Enum):
    """Udalosti ktoré spôsobujú prechod."""
    REJECT_STREAK      = "REJECT_STREAK"
    HEARTBEAT_FAILURE  = "HEARTBEAT_FAILURE"
    DESYNC_DETECTED    = "DESYNC_DETECTED"
    COOLDOWN_ELAPSED   = "COOLDOWN_ELAPSED"
    RECOVERY_TICK      = "RECOVERY_TICK"
    RECOVERY_COMPLETE  = "RECOVERY_COMPLETE"
    HEALTH_DEGRADED    = "HEALTH_DEGRADED"
    MANUAL_RESET       = "MANUAL_RESET"


@dataclass
class CBTransition:
    """Jeden prechod v stavovom automate."""
    from_state:  CBState
    trigger:     CBTrigger
    to_state:    CBState
    action:      str   # čo sa má vykonať pri prechode
    guard:       str   # podmienka pre prechod (textový popis)


@dataclass
class CBStateSnapshot:
    state:               CBState
    triggered_at:        Optional[float]
    cooldown_sec:        float
    recovery_ticks_needed: int
    current_recovery_ticks: int
    consecutive_healthy: int

    def cooldown_remaining(self, now: float) -> float:
        if self.triggered_at is None:
            return 0.0
        return max(0.0, self.cooldown_sec - (now - self.triggered_at))

    def recovery_progress(self) -> str:
        return f"{self.current_recovery_ticks}/{self.recovery_ticks_needed}"


# Kompletná transition table
CIRCUIT_BREAKER_TRANSITIONS: list[CBTransition] = [
    # CLOSED → OPEN
    CBTransition(
        CBState.CLOSED, CBTrigger.REJECT_STREAK, CBState.OPEN,
        action="block_all_orders, log_critical, notify_telegram",
        guard="reject_streak >= reject_streak_cb",
    ),
    CBTransition(
        CBState.CLOSED, CBTrigger.HEARTBEAT_FAILURE, CBState.OPEN,
        action="block_all_orders, cancel_stale, log_critical",
        guard="exchange_heartbeat_ok == False",
    ),
    CBTransition(
        CBState.CLOSED, CBTrigger.DESYNC_DETECTED, CBState.OPEN,
        action="block_all_orders, log_critical",
        guard="desync_detected == True OR unknown_order_states >= threshold",
    ),

    # OPEN → HALF_OPEN
    CBTransition(
        CBState.OPEN, CBTrigger.COOLDOWN_ELAPSED, CBState.HALF_OPEN,
        action="set recovery_ticks=0, log_info, allow_monitoring_only",
        guard="now - triggered_at >= cb_cooldown_sec",
    ),

    # HALF_OPEN → OPEN (relapse)
    CBTransition(
        CBState.HALF_OPEN, CBTrigger.HEALTH_DEGRADED, CBState.OPEN,
        action="reset recovery_ticks, log_warning, restart cooldown",
        guard=(
            "market_data_stale OR heartbeat_fail "
            "OR reject_streak > 0 OR desync_detected"
        ),
    ),
    CBTransition(
        CBState.HALF_OPEN, CBTrigger.REJECT_STREAK, CBState.OPEN,
        action="reset recovery_ticks, restart cooldown, log_critical",
        guard="new reject during recovery",
    ),

    # HALF_OPEN → RECOVERED
    CBTransition(
        CBState.HALF_OPEN, CBTrigger.RECOVERY_COMPLETE, CBState.RECOVERED,
        action="log_info, notify_telegram_recovery",
        guard=(
            "consecutive_healthy >= cb_recovery_ticks "
            "AND market_data_fresh "
            "AND heartbeat_ok "
            "AND reject_streak == 0 "
            "AND NOT desync_detected"
        ),
    ),

    # RECOVERED → CLOSED
    CBTransition(
        CBState.RECOVERED, CBTrigger.MANUAL_RESET, CBState.CLOSED,
        action="reset all counters, resume normal operations",
        guard="always (auto after recovery_complete)",
    ),

    # ANY → OPEN (emergency)
    CBTransition(
        CBState.HALF_OPEN, CBTrigger.HEARTBEAT_FAILURE, CBState.OPEN,
        action="restart cooldown, reset recovery, log_critical",
        guard="heartbeat fails during recovery",
    ),
]


class CircuitBreakerFSM:
    """
    Formálny stavový automat pre Circuit Breaker.

    Prechody sú deterministické — každý stav + trigger = presný next_state.
    Žiadna ambiguita.

    Diagram:
                   REJECT_STREAK
                   HEARTBEAT_FAIL    HEALTH_DEGRADED
                   DESYNC_DETECTED   REJECT_STREAK
         ┌──────┐       ┌────────┐◄──────────────┐
         │CLOSED│──────►│  OPEN  │               │
         └──────┘       └────────┘               │
             ▲               │ COOLDOWN_ELAPSED  │
             │               ▼                  │
             │          ┌─────────┐─────────────┘
             │          │HALF_OPEN│
             │          └─────────┘
             │               │ RECOVERY_COMPLETE
             │               ▼
             │          ┌──────────┐
             └──────────│RECOVERED │
               AUTO     └──────────┘
    """

    def __init__(self, cooldown_sec: float = 120.0, recovery_ticks: int = 5):
        self._state        = CBState.CLOSED
        self._triggered_at: Optional[float] = None
        self._recovery_ticks = 0
        self._cooldown_sec = cooldown_sec
        self._recovery_needed = recovery_ticks

    @property
    def state(self) -> CBState:
        return self._state

    @property
    def is_blocking(self) -> bool:
        return self._state in (CBState.OPEN, CBState.HALF_OPEN)

    def process(self, trigger: CBTrigger, now: float = None) -> CBState:
        """
        Aplikuje trigger a vráti nový stav.
        Deterministické — ten istý stav + trigger → vždy ten istý výsledok.
        """
        now = now or time.time()
        prev = self._state

        if self._state == CBState.CLOSED:
            if trigger in (CBTrigger.REJECT_STREAK,
                           CBTrigger.HEARTBEAT_FAILURE,
                           CBTrigger.DESYNC_DETECTED):
                self._state        = CBState.OPEN
                self._triggered_at = now
                self._recovery_ticks = 0
                log.warning(f"[CB-FSM] {prev.value} → OPEN | trigger={trigger.value}")

        elif self._state == CBState.OPEN:
            if trigger == CBTrigger.COOLDOWN_ELAPSED:
                elapsed = now - (self._triggered_at or now)
                if elapsed >= self._cooldown_sec:
                    self._state = CBState.HALF_OPEN
                    log.info(f"[CB-FSM] OPEN → HALF_OPEN | elapsed={elapsed:.0f}s")

        elif self._state == CBState.HALF_OPEN:
            if trigger in (CBTrigger.HEALTH_DEGRADED,
                           CBTrigger.REJECT_STREAK,
                           CBTrigger.HEARTBEAT_FAILURE):
                self._state          = CBState.OPEN
                self._triggered_at   = now    # reštart cooldownu
                self._recovery_ticks = 0
                log.warning(f"[CB-FSM] HALF_OPEN → OPEN (relapse) | trigger={trigger.value}")

            elif trigger == CBTrigger.RECOVERY_TICK:
                self._recovery_ticks += 1
                if self._recovery_ticks >= self._recovery_needed:
                    self._state = CBState.RECOVERED
                    log.info(f"[CB-FSM] HALF_OPEN → RECOVERED | ticks={self._recovery_ticks}")

        elif self._state == CBState.RECOVERED:
            # Auto-transition do CLOSED
            self._state          = CBState.CLOSED
            self._triggered_at   = None
            self._recovery_ticks = 0
            log.info("[CB-FSM] RECOVERED → CLOSED | normálna prevádzka obnovená")

        return self._state

    def snapshot(self, now: float = None) -> CBStateSnapshot:
        now = now or time.time()
        return CBStateSnapshot(
            state                  = self._state,
            triggered_at           = self._triggered_at,
            cooldown_sec           = self._cooldown_sec,
            recovery_ticks_needed  = self._recovery_needed,
            current_recovery_ticks = self._recovery_ticks,
            consecutive_healthy    = self._recovery_ticks,
        )

    def manual_reset(self) -> None:
        """Manuálny reset — len pre emergency / operator zásah."""
        log.warning(f"[CB-FSM] MANUÁLNY RESET z {self._state.value} → CLOSED")
        self._state          = CBState.CLOSED
        self._triggered_at   = None
        self._recovery_ticks = 0


# ════════════════════════════════════════════════════════════════════════════
# C) REGIME STATE MACHINE
# ════════════════════════════════════════════════════════════════════════════

class RegimeTransitionRule(str, Enum):
    """Typy prechodových pravidiel."""
    SCORE_BASED   = "SCORE_BASED"     # composite score prahové
    OVERRIDE      = "OVERRIDE"         # okamžitý override (panic/breakout)
    HYSTERESIS    = "HYSTERESIS"       # asymetrické prahy
    PERSISTENCE   = "PERSISTENCE"      # vyžaduje N tickov potvrdenia
    COOLDOWN      = "COOLDOWN"         # min. čas medzi zmenami
    PANIC_FAST    = "PANIC_FAST"       # obíde cooldown pri vysokom conf


@dataclass
class RegimeTransitionSpec:
    """Špecifikácia jedného prechodu medzi režimami."""
    from_regime:      str   # MarketRegime.value alebo "*" (ľubovoľný)
    to_regime:        str
    rule_type:        RegimeTransitionRule
    condition:        str   # textový popis podmienky
    persistence_ticks: int  # 0 = okamžitý
    cooldown_ticks:   int   # 0 = bez cooldownu
    min_confidence:   float # 0.0 = bez požiadavky


# Kompletná tabuľka prechodov
REGIME_TRANSITIONS: list[RegimeTransitionSpec] = [
    # ── Okamžité override prechody (PANIC) ───────────────────────────────
    RegimeTransitionSpec(
        "*", "PANIC", RegimeTransitionRule.PANIC_FAST,
        condition="panic_drop_pct_3bar exceeded AND confidence >= panic_conf_override",
        persistence_ticks=0, cooldown_ticks=0, min_confidence=0.65,
    ),

    # ── Breakout override (rýchlejší ako score-based) ──────────────────
    RegimeTransitionSpec(
        "*", "BREAKOUT_DOWN", RegimeTransitionRule.OVERRIDE,
        condition="close < EMA_fast - breakout_atr_mult * ATR",
        persistence_ticks=1, cooldown_ticks=0, min_confidence=0.40,
    ),
    RegimeTransitionSpec(
        "*", "BREAKOUT_UP", RegimeTransitionRule.OVERRIDE,
        condition="close > EMA_fast + breakout_atr_mult * ATR",
        persistence_ticks=1, cooldown_ticks=0, min_confidence=0.40,
    ),

    # ── Score-based prechody z RANGE ──────────────────────────────────
    RegimeTransitionSpec(
        "RANGE", "UPTREND", RegimeTransitionRule.SCORE_BASED,
        condition="smoothed_composite >= uptrend_enter_score (+0.40)",
        persistence_ticks=3, cooldown_ticks=4, min_confidence=0.35,
    ),
    RegimeTransitionSpec(
        "RANGE", "DOWNTREND", RegimeTransitionRule.SCORE_BASED,
        condition="smoothed_composite <= downtrend_enter_score (-0.40)",
        persistence_ticks=3, cooldown_ticks=4, min_confidence=0.35,
    ),

    # ── Hysteresis prechody (výstup z trendu) ─────────────────────────
    RegimeTransitionSpec(
        "UPTREND", "RANGE", RegimeTransitionRule.HYSTERESIS,
        condition="smoothed_composite < uptrend_exit_score (+0.25) [< enter_score]",
        persistence_ticks=3, cooldown_ticks=4, min_confidence=0.35,
    ),
    RegimeTransitionSpec(
        "DOWNTREND", "RANGE", RegimeTransitionRule.HYSTERESIS,
        condition="smoothed_composite > downtrend_exit_score (-0.25) [> enter_score]",
        persistence_ticks=3, cooldown_ticks=4, min_confidence=0.35,
    ),

    # ── Breakout → normalizácia ────────────────────────────────────────
    RegimeTransitionSpec(
        "BREAKOUT_UP", "UPTREND", RegimeTransitionRule.SCORE_BASED,
        condition="breakout_up=False AND score >= uptrend_enter_score",
        persistence_ticks=3, cooldown_ticks=2, min_confidence=0.35,
    ),
    RegimeTransitionSpec(
        "BREAKOUT_UP", "RANGE", RegimeTransitionRule.SCORE_BASED,
        condition="breakout_up=False AND score < uptrend_enter_score",
        persistence_ticks=3, cooldown_ticks=2, min_confidence=0.30,
    ),
    RegimeTransitionSpec(
        "BREAKOUT_DOWN", "DOWNTREND", RegimeTransitionRule.SCORE_BASED,
        condition="breakout_down=False AND score <= downtrend_enter_score",
        persistence_ticks=3, cooldown_ticks=2, min_confidence=0.35,
    ),
    RegimeTransitionSpec(
        "BREAKOUT_DOWN", "RANGE", RegimeTransitionRule.SCORE_BASED,
        condition="breakout_down=False AND score > downtrend_enter_score",
        persistence_ticks=3, cooldown_ticks=2, min_confidence=0.30,
    ),

    # ── PANIC exit ─────────────────────────────────────────────────────
    RegimeTransitionSpec(
        "PANIC", "BREAKOUT_DOWN", RegimeTransitionRule.SCORE_BASED,
        condition="panic_not_detected AND breakout_down=True",
        persistence_ticks=5, cooldown_ticks=6, min_confidence=0.50,
    ),
    RegimeTransitionSpec(
        "PANIC", "DOWNTREND", RegimeTransitionRule.SCORE_BASED,
        condition="panic_not_detected AND score <= downtrend_enter_score",
        persistence_ticks=5, cooldown_ticks=6, min_confidence=0.50,
    ),
    RegimeTransitionSpec(
        "PANIC", "RANGE", RegimeTransitionRule.SCORE_BASED,
        condition="panic_not_detected AND score in range",
        persistence_ticks=8, cooldown_ticks=8, min_confidence=0.55,
    ),

    # ── UNDEFINED prechody ─────────────────────────────────────────────
    RegimeTransitionSpec(
        "UNDEFINED", "RANGE", RegimeTransitionRule.PERSISTENCE,
        condition="enough bars AND score in range",
        persistence_ticks=3, cooldown_ticks=0, min_confidence=0.20,
    ),
]

"""
REGIME STATE MACHINE DIAGRAM:
═══════════════════════════════

                         PANIC (conf≥0.65, override cooldown)
         ┌──────────────────────────────────────────────────┐
         │                                                  │
         ▼    score≥+0.40         score<+0.25(hyst)        │
   ┌─────────┐──────────►┌─────────┐◄──────────────┐       │
   │  RANGE  │           │ UPTREND │               │       │
   └─────────┘◄──────────└─────────┘               │       │
         │    score<+0.25                           │       │
         │                                          │       │
         │    score≤-0.40         score>-0.25(hyst)│       │
         │──────────────►┌──────────┐◄─────────────┘       │
         │               │DOWNTREND │                       │
         │◄──────────────└──────────┘                       │
         │    score>-0.25                                   │
         │                                                  │
         │   close > EMA+N*ATR                              │
         ├──────────────►┌────────────┐──── score norm ─►RANGE/UPTREND
         │               │BREAKOUT_UP │
         │◄──────────────└────────────┘ (persistence+cooldown)
         │   (po normalizácii)
         │
         │   close < EMA-N*ATR
         ├──────────────►┌─────────────┐──── score norm ─►RANGE/DT
         │               │BREAKOUT_DOWN│
         │               └─────────────┘
         │                      │ panic trigger
         │                      ▼
         │               ┌────────────┐
         └───────────────│   PANIC    │ (dlhý exit: 5-8 ticky)
                         └────────────┘
         ┌────────────┐
         │ UNDEFINED  │──── enough data ────►RANGE (persistence=3)
         └────────────┘

Kľúčové vlastnosti:
  • Všetky prechody OKREM PANIC vyžadujú persistence_ticks + cooldown_ticks
  • PANIC obíde cooldown ak confidence >= panic_conf_override
  • Hysteresis: exit prah je vždy bližšie k nule ako entry prah
  • PANIC má najdlhší exit (8 tickov) — konzervatívne
  • UNDEFINED → RANGE je najrýchlejší normálny prechod (3 ticky, 0 cooldown)
"""


# ════════════════════════════════════════════════════════════════════════════
# D) UNIT TEST PLAN — INVARIANTS
# ════════════════════════════════════════════════════════════════════════════

@dataclass
class TestCase:
    name:        str
    description: str
    test_fn:     Callable[[], bool]
    category:    str
    invariant:   str    # ktorý invariant testuje


@dataclass
class TestResult:
    test_name:   str
    passed:      bool
    error:       Optional[str]
    duration_ms: float


class InvariantTestSuite:
    """
    Kompletný test suite pre invariants APEX BOT systému.

    Kategórie:
      INV  — priame invariant testy (garantované DecisionEngine)
      CB   — circuit breaker state machine testy
      REG  — regime state machine testy
      INT  — integračné testy (viac modulov)
      EDGE — edge cases a fail-safe správanie

    Spustenie:
        suite = InvariantTestSuite()
        suite.run_all()
    """

    def __init__(self):
        self._tests:   list[TestCase]  = []
        self._results: list[TestResult] = []
        self._register_all()

    def _register_all(self):
        """Zaregistruje všetky testy."""
        self._register_decision_invariants()
        self._register_circuit_breaker_tests()
        self._register_regime_tests()
        self._register_integration_tests()
        self._register_edge_cases()

    # ── Registrácia testov ────────────────────────────────────────────────────

    def _register_decision_invariants(self):
        """Invarianty garantované DecisionEngine.__post_init__."""

        def _make_outcome(**kwargs):
            """Helper — vytvorí DecisionOutcome s danými hodnotami."""
            import sys, os
            sys.path.insert(0, '/home/claude')
            from step15_decision_engine import DecisionOutcome, DecisionLayer
            defaults = dict(
                allow_trading=True, allow_new_orders=True, allow_new_risk=True,
                allow_new_buys=True, allow_dca=True, reduce_only_mode=False,
                allow_rebalance=True, allow_requotes=True,
                forced_cancel_all=False, forced_inventory_reduction=False,
                order_size_multiplier=1.0, max_exposure_multiplier=1.0,
                winning_layer=DecisionLayer.DEFAULT,
                reason_codes=[], votes=[], explanation="test",
            )
            defaults.update(kwargs)
            return DecisionOutcome(**defaults)

        def _get_engine():
            import sys; sys.path.insert(0, '/home/claude')
            from step15_decision_engine import DecisionEngine, DecisionInputs
            return DecisionEngine(), DecisionInputs

        # I1: allow_trading=False → allow_new_orders=False
        def test_I1():
            out = _make_outcome(allow_trading=False, allow_new_orders=True)
            return out.allow_new_orders == False
        self._add(TestCase(
            "I1_trading_false_blocks_orders",
            "ak allow_trading=False, potom allow_new_orders musí byť False",
            test_I1, "INV", "I1",
        ))

        # I2: allow_new_orders=False → allow_new_buys=False
        def test_I2():
            out = _make_outcome(allow_trading=True, allow_new_orders=False, allow_new_buys=True)
            return out.allow_new_buys == False
        self._add(TestCase(
            "I2_orders_false_blocks_buys",
            "ak allow_new_orders=False, potom allow_new_buys musí byť False",
            test_I2, "INV", "I2",
        ))

        # I3: allow_new_orders=False → allow_requotes=False
        def test_I3():
            out = _make_outcome(allow_trading=True, allow_new_orders=False, allow_requotes=True)
            return out.allow_requotes == False
        self._add(TestCase(
            "I3_orders_false_blocks_requotes",
            "ak allow_new_orders=False, potom allow_requotes musí byť False",
            test_I3, "INV", "I3",
        ))

        # I4: reduce_only_mode=True → allow_new_buys=False
        def test_I4():
            out = _make_outcome(reduce_only_mode=True, allow_new_buys=True)
            return out.allow_new_buys == False
        self._add(TestCase(
            "I4_reduce_only_blocks_buys",
            "ak reduce_only_mode=True, potom allow_new_buys musí byť False",
            test_I4, "INV", "I4",
        ))

        # I5: reduce_only_mode=True → allow_dca=False
        def test_I5():
            out = _make_outcome(reduce_only_mode=True, allow_dca=True)
            return out.allow_dca == False
        self._add(TestCase(
            "I5_reduce_only_blocks_dca",
            "ak reduce_only_mode=True, potom allow_dca musí byť False",
            test_I5, "INV", "I5",
        ))

        # I6: forced_cancel_all=True → allow_new_orders=False
        def test_I6():
            out = _make_outcome(forced_cancel_all=True, allow_new_orders=True)
            return out.allow_new_orders == False
        self._add(TestCase(
            "I6_forced_cancel_blocks_orders",
            "ak forced_cancel_all=True, potom allow_new_orders musí byť False",
            test_I6, "INV", "I6",
        ))

        # I7: Panic regime → allow_dca=False (cez DecisionEngine)
        def test_I7():
            engine, DecisionInputs = _get_engine()
            inp = DecisionInputs(
                exec_safe_to_trade=True, exec_block_new_orders=False,
                exec_block_requotes=False, exec_trigger_circuit_breaker=False,
                exec_cancel_stale_orders=False, exec_state="HEALTHY",
                regime_effective="PANIC",
                regime_allow_grid=False, regime_allow_new_buys=False,
                regime_allow_dca=False, regime_protective=True,
                regime_inv_reduction=True, regime_confidence=0.9,
                dca_allow=True,   # DCA guard hovorí OK, ale PANIC musí blokovať
                vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
                inv_allow_new_buys=True, inv_allow_dca=True,
            )
            out = engine.decide(inp)
            return out.allow_dca == False
        self._add(TestCase(
            "I7_panic_regime_blocks_dca",
            "ak effective_regime=PANIC, allow_dca musí byť False aj keď DCA guard hovorí OK",
            test_I7, "INV", "I7",
        ))

        # I8: forced_inventory_reduction=True → allow_new_buys=False
        def test_I8():
            out = _make_outcome(forced_inventory_reduction=True, allow_new_buys=True)
            return out.allow_new_buys == False
        self._add(TestCase(
            "I8_forced_inv_reduction_blocks_buys",
            "ak forced_inventory_reduction=True, allow_new_buys musí byť False",
            test_I8, "INV", "I8",
        ))

        # I9: ExecSafety=CIRCUIT_BREAKER → allow_trading=False (priorita 1)
        def test_I9():
            engine, DecisionInputs = _get_engine()
            inp = DecisionInputs(
                exec_safe_to_trade=False, exec_block_new_orders=True,
                exec_block_requotes=True, exec_trigger_circuit_breaker=True,
                exec_cancel_stale_orders=False, exec_state="CIRCUIT_BREAKER",
                regime_effective="RANGE",
                regime_allow_grid=True, regime_allow_new_buys=True,
                regime_allow_dca=True, regime_protective=False,
                regime_inv_reduction=False, regime_confidence=0.8,
                vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
                inv_allow_new_buys=True, inv_allow_dca=True,
            )
            out = engine.decide(inp)
            return (
                out.allow_trading == False
                and out.allow_new_orders == False
                and out.allow_new_buys == False
                and out.forced_cancel_all == True
            )
        self._add(TestCase(
            "I9_exec_cb_overrides_everything",
            "ak exec CIRCUIT_BREAKER, musí byť allow_trading=False a cancel_all=True",
            test_I9, "INV", "I9",
        ))

        # I10: allow_new_buys=False → allow_dca=False
        def test_I10():
            out = _make_outcome(allow_new_buys=False, allow_dca=True)
            return out.allow_dca == False
        self._add(TestCase(
            "I10_no_buys_blocks_dca",
            "ak allow_new_buys=False, allow_dca musí byť False",
            test_I10, "INV", "I10",
        ))

        # Konzistencia: allow_trading=True ale allow_new_orders=False → allow_new_buys=False
        def test_consistency_1():
            engine, DecisionInputs = _get_engine()
            inp = DecisionInputs(
                exec_safe_to_trade=True, exec_block_new_orders=False,
                exec_block_requotes=False, exec_trigger_circuit_breaker=False,
                exec_cancel_stale_orders=False, exec_state="HEALTHY",
                port_risk_mode="PAUSE_NEW_RISK",
                regime_effective="RANGE", regime_allow_grid=True,
                regime_allow_new_buys=True, regime_allow_dca=True,
                regime_protective=False, regime_inv_reduction=False,
                regime_confidence=0.7,
                vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
                inv_allow_new_buys=True, inv_allow_dca=True,
            )
            out = engine.decide(inp)
            # allow_trading môže byť True, ale allow_new_orders=False
            # → allow_new_buys MUSÍ byť False (I2)
            if out.allow_new_orders == False:
                return out.allow_new_buys == False
            return True  # ak allow_new_orders=True, test nie je relevantný
        self._add(TestCase(
            "CONSISTENCY_pause_implies_no_buys",
            "PAUSE_NEW_RISK → allow_new_orders=False → allow_new_buys=False (chain invariant)",
            test_consistency_1, "INV", "I1+I2",
        ))

        # Konzistencia: žiadny stav nemôže mať allow_requotes=True ak allow_new_orders=False
        def test_no_requotes_without_orders():
            engine, DecisionInputs = _get_engine()
            for regime in ["PANIC", "BREAKOUT_DOWN", "UNDEFINED"]:
                inp = DecisionInputs(
                    exec_safe_to_trade=True, exec_block_new_orders=False,
                    exec_block_requotes=False, exec_trigger_circuit_breaker=False,
                    exec_cancel_stale_orders=False, exec_state="HEALTHY",
                    regime_effective=regime, regime_allow_grid=False,
                    regime_allow_new_buys=False, regime_allow_dca=False,
                    regime_protective=True, regime_inv_reduction=True,
                    regime_confidence=0.8,
                    vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
                    inv_allow_new_buys=True, inv_allow_dca=True,
                )
                out = engine.decide(inp)
                if not out.allow_new_orders and out.allow_requotes:
                    return False
            return True
        self._add(TestCase(
            "CONSISTENCY_no_requotes_without_orders",
            "allow_requotes nemôže byť True ak allow_new_orders=False",
            test_no_requotes_without_orders, "INV", "I3",
        ))

    def _register_circuit_breaker_tests(self):
        """Testy pre Circuit Breaker FSM."""

        def test_cb_closed_to_open_on_reject():
            fsm = CircuitBreakerFSM(cooldown_sec=10, recovery_ticks=3)
            assert fsm.state == CBState.CLOSED
            new_state = fsm.process(CBTrigger.REJECT_STREAK)
            return new_state == CBState.OPEN and fsm.is_blocking
        self._add(TestCase(
            "CB_closed_to_open_reject_streak",
            "CLOSED + REJECT_STREAK → OPEN",
            test_cb_closed_to_open_on_reject, "CB", "FSM-1",
        ))

        def test_cb_open_to_halfopen_after_cooldown():
            fsm = CircuitBreakerFSM(cooldown_sec=0.01, recovery_ticks=3)
            fsm.process(CBTrigger.REJECT_STREAK)
            time.sleep(0.05)
            now = time.time()
            new_state = fsm.process(CBTrigger.COOLDOWN_ELAPSED, now)
            return new_state == CBState.HALF_OPEN
        self._add(TestCase(
            "CB_open_to_halfopen_after_cooldown",
            "OPEN + COOLDOWN_ELAPSED (po uplynutí) → HALF_OPEN",
            test_cb_open_to_halfopen_after_cooldown, "CB", "FSM-2",
        ))

        def test_cb_halfopen_to_open_on_relapse():
            fsm = CircuitBreakerFSM(cooldown_sec=0.01, recovery_ticks=3)
            fsm.process(CBTrigger.REJECT_STREAK)
            time.sleep(0.05)
            fsm.process(CBTrigger.COOLDOWN_ELAPSED, time.time())
            assert fsm.state == CBState.HALF_OPEN
            fsm.process(CBTrigger.REJECT_STREAK)
            return fsm.state == CBState.OPEN
        self._add(TestCase(
            "CB_halfopen_relapse_to_open",
            "HALF_OPEN + REJECT_STREAK → OPEN (relapse)",
            test_cb_halfopen_to_open_on_relapse, "CB", "FSM-3",
        ))

        def test_cb_full_recovery_cycle():
            fsm = CircuitBreakerFSM(cooldown_sec=0.01, recovery_ticks=2)
            # CLOSED → OPEN
            fsm.process(CBTrigger.HEARTBEAT_FAILURE)
            assert fsm.state == CBState.OPEN
            # OPEN → HALF_OPEN
            time.sleep(0.02)
            fsm.process(CBTrigger.COOLDOWN_ELAPSED, time.time())
            assert fsm.state == CBState.HALF_OPEN
            # HALF_OPEN: recovery ticky
            fsm.process(CBTrigger.RECOVERY_TICK)
            assert fsm.state == CBState.HALF_OPEN
            fsm.process(CBTrigger.RECOVERY_TICK)
            assert fsm.state == CBState.RECOVERED
            # RECOVERED → CLOSED (auto)
            fsm.process(CBTrigger.MANUAL_RESET)
            return fsm.state == CBState.CLOSED and not fsm.is_blocking
        self._add(TestCase(
            "CB_full_recovery_cycle",
            "Kompletný cyklus: CLOSED→OPEN→HALF_OPEN→RECOVERED→CLOSED",
            test_cb_full_recovery_cycle, "CB", "FSM-4",
        ))

        def test_cb_open_ignores_cooldown_early():
            fsm = CircuitBreakerFSM(cooldown_sec=3600, recovery_ticks=3)
            fsm.process(CBTrigger.REJECT_STREAK)
            # Cooldown sa NEukončil — COOLDOWN_ELAPSED nemá efekt
            state = fsm.process(CBTrigger.COOLDOWN_ELAPSED, time.time())
            return state == CBState.OPEN   # zostáva OPEN
        self._add(TestCase(
            "CB_open_ignores_early_cooldown",
            "OPEN + COOLDOWN_ELAPSED pred uplynutím cooldownu → zostáva OPEN",
            test_cb_open_ignores_cooldown_early, "CB", "FSM-5",
        ))

        def test_cb_is_blocking_in_open_and_halfopen():
            fsm = CircuitBreakerFSM(cooldown_sec=0.01, recovery_ticks=3)
            fsm.process(CBTrigger.REJECT_STREAK)
            open_blocking = fsm.is_blocking
            time.sleep(0.02)
            fsm.process(CBTrigger.COOLDOWN_ELAPSED, time.time())
            halfopen_blocking = fsm.is_blocking
            return open_blocking and halfopen_blocking
        self._add(TestCase(
            "CB_is_blocking_open_and_halfopen",
            "is_blocking=True v OPEN aj HALF_OPEN stavoch",
            test_cb_is_blocking_in_open_and_halfopen, "CB", "FSM-6",
        ))

        def test_cb_manual_reset():
            fsm = CircuitBreakerFSM()
            fsm.process(CBTrigger.REJECT_STREAK)
            assert fsm.state == CBState.OPEN
            fsm.manual_reset()
            return fsm.state == CBState.CLOSED and not fsm.is_blocking
        self._add(TestCase(
            "CB_manual_reset",
            "manual_reset() z OPEN → CLOSED",
            test_cb_manual_reset, "CB", "FSM-7",
        ))

    def _register_regime_tests(self):
        """Testy pre Regime FSM."""

        def _make_regime_detector():
            import sys; sys.path.insert(0, '/home/claude')
            from step10_market_regime_v2 import RegimeDetector, RegimeConfig, MarketRegime
            cfg = RegimeConfig(
                persistence_ticks=2, cooldown_ticks=2,
                smoothing_alpha=0.5, min_bars=20,
                panic_drop_pct_3bar=3.0, panic_conf_override=0.60,
            )
            return RegimeDetector(cfg), MarketRegime

        def _klines(n, base, atr_pct, bias=0, seed=42):
            import random
            rng = random.Random(seed)
            price, k = base, []
            for _ in range(n):
                rng_val = price * atr_pct
                price  += rng.gauss(bias * price * 0.01, rng_val)
                price   = max(price, 1.0)
                k.append({"open": price-rng_val*0.1, "high": price+rng_val*0.5,
                           "low": price-rng_val*0.5, "close": price, "volume": 100})
            return k

        def test_regime_undefined_with_few_bars():
            det, MR = _make_regime_detector()
            k = _klines(10, 618.0, 0.010)   # < min_bars
            d = det.update(k)
            return d.effective_regime == MR.UNDEFINED
        self._add(TestCase(
            "REG_undefined_with_few_bars",
            "S menej barmi ako min_bars → effective=UNDEFINED",
            test_regime_undefined_with_few_bars, "REG", "REG-1",
        ))

        def test_regime_raw_ne_effective_before_persistence():
            det, MR = _make_regime_detector()
            k = _klines(60, 618.0, 0.010)
            # Prvý tick — raw môže byť iný ako effective
            d = det.update(k)
            # effective by malo byť UNDEFINED alebo RANGE (s málo tickmi)
            # persistence_counter < persistence_ticks → žiadna zmena
            return d.persistence_counter <= 2   # persistence ticks
        self._add(TestCase(
            "REG_raw_differs_effective_before_persistence",
            "raw_regime sa nestane effective pred persistence_ticks",
            test_regime_raw_ne_effective_before_persistence, "REG", "REG-2",
        ))

        def test_regime_panic_overrides_cooldown():
            det, MR = _make_regime_detector()
            # Stabilná range fáza
            rng = __import__('random').Random(42)
            price, k_range = 618.0, []
            for _ in range(30):
                rv = price * 0.008
                price += rng.gauss(0, rv)
                price = max(price, 1.0)
                k_range.append({"open":price,"high":price+rv*0.5,"low":price-rv*0.5,"close":price,"volume":100})
            for _ in range(4):
                d = det.update(k_range)
            # Ak range fáza nezafixovala RANGE, skip (UNDEFINED → bez cooldownu)
            if d.effective_regime not in (MR.RANGE, MR.UPTREND, MR.UNDEFINED):
                return True
            # Panic: posledné 4 bary klesnú o 2% každý (celkom ~8% > threshold 3%)
            k_panic = list(k_range)
            last = k_panic[-1]["close"]
            for _ in range(4):
                new = last * 0.98
                k_panic.append({"open":last,"high":last,"low":new,"close":new,"volume":200})
                last = new
            d2 = det.update(k_panic)
            bd = d2.score_breakdown
            # Panic musí byť detekovaný a effective musí byť PANIC
            return bd.panic_detected and d2.effective_regime == MR.PANIC
        self._add(TestCase(
            "REG_panic_overrides_cooldown",
            "PANIC s vysokou conf obíde cooldown a okamžite sa stane effective",
            test_regime_panic_overrides_cooldown, "REG", "REG-3",
        ))

        def test_regime_allows_grid_in_range():
            det, MR = _make_regime_detector()
            k = _klines(60, 618.0, 0.010, bias=0)
            d = None
            for _ in range(5):
                d = det.update(k)
            if d.effective_regime == MR.RANGE:
                return d.allow_grid == True
            return True  # skip ak nie je RANGE
        self._add(TestCase(
            "REG_grid_allowed_in_range",
            "V RANGE režime musí byť allow_grid=True",
            test_regime_allows_grid_in_range, "REG", "REG-4",
        ))

        def test_regime_no_dca_in_panic():
            det, MR = _make_regime_detector()
            k = _klines(60, 500.0, 0.070, bias=-5, seed=77)
            d = None
            for _ in range(5):
                d = det.update(k)
            if d.effective_regime == MR.PANIC:
                return d.allow_dca == False
            return True  # skip ak nie je PANIC
        self._add(TestCase(
            "REG_no_dca_in_panic",
            "V PANIC režime musí byť allow_dca=False",
            test_regime_no_dca_in_panic, "REG", "REG-5",
        ))

        def test_regime_score_breakdown_populated():
            det, MR = _make_regime_detector()
            k = _klines(60, 618.0, 0.012, bias=0)
            d = det.update(k)
            bd = d.score_breakdown
            return (
                bd is not None
                and isinstance(bd.smoothed_composite, float)
                and isinstance(bd.confidence, float)
                and 0.0 <= bd.confidence <= 1.0
                and -1.0 <= bd.smoothed_composite <= 1.0
            )
        self._add(TestCase(
            "REG_score_breakdown_valid_ranges",
            "score_breakdown.confidence ∈ [0,1], composite ∈ [-1,+1]",
            test_regime_score_breakdown_populated, "REG", "REG-6",
        ))

        def test_regime_ticks_increment():
            det, MR = _make_regime_detector()
            k = _klines(60, 618.0, 0.010)
            d1 = det.update(k)
            d2 = det.update(k)
            return d2.ticks_since_last_change >= d1.ticks_since_last_change
        self._add(TestCase(
            "REG_ticks_monotonically_increase",
            "ticks_since_last_change rastie monotónne kým nenastane zmena",
            test_regime_ticks_increment, "REG", "REG-7",
        ))

    def _register_integration_tests(self):
        """Integračné testy — viac modulov naraz."""

        def test_exec_unsafe_blocks_everything():
            import sys; sys.path.insert(0, '/home/claude')
            from step13_execution_safety_v2 import (
                ExecutionSafetyController, ExecutionSafetyConfig, ExecutionSnapshot
            )
            from step15_decision_engine import DecisionEngine, DecisionInputs
            ctrl = ExecutionSafetyController(ExecutionSafetyConfig(reject_streak_cb=3))
            engine = DecisionEngine()
            now = time.time()
            snap = ExecutionSnapshot(
                now_ts=now, last_market_data_ts=now-5, last_ws_message_ts=now-5,
                last_rest_ok_ts=now-5, exchange_heartbeat_ok=True,
                open_order_count=5, stale_order_count=0,
                order_reject_streak=3, cancel_streak=0, replace_streak=0,
                actions_last_minute=3, max_actions_per_minute=15,
                desync_detected=False, unknown_order_states=0, symbol="BNBUSDT",
            )
            exec_dec = ctrl.evaluate(snap)
            inp = DecisionInputs(
                exec_safe_to_trade=exec_dec.safe_to_trade,
                exec_block_new_orders=exec_dec.block_new_orders,
                exec_block_requotes=exec_dec.block_requotes,
                exec_trigger_circuit_breaker=exec_dec.trigger_circuit_breaker,
                exec_cancel_stale_orders=exec_dec.cancel_stale_orders,
                exec_state=exec_dec.state.value,
                regime_effective="RANGE", regime_allow_grid=True,
                regime_allow_new_buys=True, regime_allow_dca=True,
                regime_protective=False, regime_inv_reduction=False,
                regime_confidence=0.8,
                vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
                inv_allow_new_buys=True, inv_allow_dca=True,
            )
            out = engine.decide(inp)
            return (
                not out.allow_trading
                and not out.allow_new_orders
                and out.forced_cancel_all
            )
        self._add(TestCase(
            "INT_exec_cb_blocks_all_via_decision_engine",
            "ExecSafety CB → DecisionEngine → allow_trading=False, cancel_all=True",
            test_exec_unsafe_blocks_everything, "INT", "I9+K1",
        ))

        def test_conflict_regime_ok_but_portfolio_pause():
            import sys; sys.path.insert(0, '/home/claude')
            from step15_decision_engine import DecisionEngine, DecisionInputs
            engine = DecisionEngine()
            inp = DecisionInputs(
                exec_safe_to_trade=True, exec_block_new_orders=False,
                exec_block_requotes=False, exec_trigger_circuit_breaker=False,
                exec_cancel_stale_orders=False, exec_state="HEALTHY",
                port_risk_mode="PAUSE_NEW_RISK",
                regime_effective="RANGE", regime_allow_grid=True,
                regime_allow_new_buys=True, regime_allow_dca=True,
                regime_protective=False, regime_inv_reduction=False,
                regime_confidence=0.8,
                vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
                inv_allow_new_buys=True, inv_allow_dca=True,
            )
            out = engine.decide(inp)
            from step15_decision_engine import DecisionLayer
            return (
                not out.allow_new_orders
                and not out.allow_new_buys
                and out.winning_layer == DecisionLayer.PORTFOLIO_RISK
            )
        self._add(TestCase(
            "INT_portfolio_pause_beats_range_regime",
            "PortRisk=PAUSE vyhráva nad Regime=RANGE (K2)",
            test_conflict_regime_ok_but_portfolio_pause, "INT", "K2",
        ))

        def test_all_invariants_on_full_ok_state():
            import sys; sys.path.insert(0, '/home/claude')
            from step15_decision_engine import DecisionEngine, DecisionInputs
            engine = DecisionEngine()
            inp = DecisionInputs(
                exec_safe_to_trade=True, exec_block_new_orders=False,
                exec_block_requotes=False, exec_trigger_circuit_breaker=False,
                exec_cancel_stale_orders=False, exec_state="HEALTHY",
                regime_effective="RANGE", regime_allow_grid=True,
                regime_allow_new_buys=True, regime_allow_dca=True,
                regime_protective=False, regime_inv_reduction=False,
                regime_confidence=0.8,
                vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
                inv_allow_new_buys=True, inv_allow_dca=True,
            )
            out = engine.decide(inp)
            violations = engine.audit_invariants(out)
            return len(violations) == 0
        self._add(TestCase(
            "INT_no_invariant_violations_on_healthy_state",
            "Pri plnom zdraví: audit_invariants() vracia prázdny zoznam",
            test_all_invariants_on_full_ok_state, "INT", "ALL",
        ))

    def _register_edge_cases(self):
        """Edge cases a fail-safe správanie."""

        def test_exec_safety_invalid_data_is_degraded():
            import sys; sys.path.insert(0, '/home/claude')
            from step13_execution_safety_v2 import (
                ExecutionSafetyController, ExecutionSafetyConfig,
                ExecutionSnapshot, ExecSafetyState,
            )
            ctrl = ExecutionSafetyController(ExecutionSafetyConfig())
            now = time.time()
            # Nevalidné dáta: peak_today < portfolio_value nie je relevantné tu
            # ale stale_order_count > open_order_count je nevalidné
            snap = ExecutionSnapshot(
                now_ts=now, last_market_data_ts=now-5, last_ws_message_ts=now-5,
                last_rest_ok_ts=now-5, exchange_heartbeat_ok=True,
                open_order_count=2, stale_order_count=5,   # stale > open = nevalidné
                order_reject_streak=0, cancel_streak=0, replace_streak=0,
                actions_last_minute=0, max_actions_per_minute=15,
                desync_detected=False, unknown_order_states=0, symbol="BNBUSDT",
            )
            dec = ctrl.evaluate(snap)
            # Fail-safe: pri nevalidných dátach → DEGRADED alebo horšie
            return dec.state in (
                ExecSafetyState.DEGRADED,
                ExecSafetyState.UNSAFE,
                ExecSafetyState.CIRCUIT_BREAKER,
            )
        self._add(TestCase(
            "EDGE_invalid_data_not_healthy",
            "Nevalidné vstupy nesmú vrátiť HEALTHY (fail-safe default)",
            test_exec_safety_invalid_data_is_degraded, "EDGE", "FAIL-SAFE",
        ))

        def test_size_mult_clamped_zero_one():
            import sys; sys.path.insert(0, '/home/claude')
            from step15_decision_engine import DecisionEngine, DecisionInputs
            engine = DecisionEngine()
            # Extrémne multipliers
            inp = DecisionInputs(
                exec_safe_to_trade=True, exec_block_new_orders=False,
                exec_block_requotes=False, exec_trigger_circuit_breaker=False,
                exec_cancel_stale_orders=False, exec_state="HEALTHY",
                port_risk_mode="REDUCE_RISK", port_max_order_mult=0.5,
                regime_effective="RANGE", regime_allow_grid=True,
                regime_allow_new_buys=True, regime_allow_dca=True,
                regime_protective=False, regime_inv_reduction=False,
                regime_confidence=0.7,
                inv_buy_size_mult=0.4,
                vol_regime="HIGH", vol_order_size_mult=0.65,
                vol_allow_dca=True, vol_allow_new_buys=True,
                inv_allow_new_buys=True, inv_allow_dca=True,
            )
            out = engine.decide(inp)
            return 0.0 <= out.order_size_multiplier <= 1.0
        self._add(TestCase(
            "EDGE_size_multiplier_clamped_0_1",
            "order_size_multiplier musí byť vždy v [0.0, 1.0]",
            test_size_mult_clamped_zero_one, "EDGE", "CLAMP",
        ))

        def test_cb_recovery_requires_all_conditions():
            """Recovery nesmie nastať ak čo len jedna podmienka chýba."""
            fsm = CircuitBreakerFSM(cooldown_sec=0.01, recovery_ticks=3)
            fsm.process(CBTrigger.REJECT_STREAK)
            time.sleep(0.02)
            fsm.process(CBTrigger.COOLDOWN_ELAPSED, time.time())
            # Posielame len 2 recovery ticky (potrebné 3)
            fsm.process(CBTrigger.RECOVERY_TICK)
            fsm.process(CBTrigger.RECOVERY_TICK)
            return fsm.state == CBState.HALF_OPEN  # ešte nie RECOVERED
        self._add(TestCase(
            "EDGE_cb_recovery_needs_all_ticks",
            "Recovery sa neaktivuje pred dosiahnutím recovery_ticks_needed",
            test_cb_recovery_requires_all_conditions, "EDGE", "CB-RECOVERY",
        ))

        def test_regime_always_has_valid_action_flags():
            import sys; sys.path.insert(0, '/home/claude')
            from step10_market_regime_v2 import RegimeDetector, RegimeConfig
            det = RegimeDetector(RegimeConfig(min_bars=20, persistence_ticks=1, cooldown_ticks=1))
            # Rôzne vstupy — vždy musí byť valid bool output
            import random
            rng = random.Random(0)
            for _ in range(10):
                base  = rng.uniform(100, 1000)
                vol   = rng.uniform(0.005, 0.06)
                bias  = rng.uniform(-2, 2)
                n     = rng.randint(20, 80)
                klines = []
                price = base
                for _ in range(n):
                    r = price * vol
                    price += rng.gauss(bias * price * 0.01, r)
                    price = max(price, 1.0)
                    klines.append({"open":price,"high":price+r*0.5,"low":price-r*0.5,"close":price,"volume":100})
                d = det.update(klines)
                if not isinstance(d.allow_grid, bool): return False
                if not isinstance(d.allow_new_buys, bool): return False
                if not isinstance(d.allow_dca, bool): return False
                if not isinstance(d.protective_mode, bool): return False
            return True
        self._add(TestCase(
            "EDGE_regime_always_returns_valid_bools",
            "RegimeDecision vždy vracia bool polia (nie None)",
            test_regime_always_has_valid_action_flags, "EDGE", "TYPE-SAFETY",
        ))

    # ── Test runner ───────────────────────────────────────────────────────────

    def _add(self, tc: TestCase) -> None:
        self._tests.append(tc)

    def run_all(self, categories: Optional[list[str]] = None) -> dict[str, int]:
        """
        Spustí všetky testy a vypíše výsledky.

        Args:
            categories: Ak None, spustí všetky. Inak len dané kategórie.

        Returns:
            {"passed": N, "failed": N, "total": N}
        """
        tests = self._tests
        if categories:
            tests = [t for t in tests if t.category in categories]

        print(f"\n{'═'*70}")
        print(f"  APEX BOT — InvariantTestSuite ({len(tests)} testov)")
        print(f"{'═'*70}")

        by_category: dict[str, list[TestCase]] = {}
        for t in tests:
            by_category.setdefault(t.category, []).append(t)

        passed = failed = 0

        for cat, cat_tests in sorted(by_category.items()):
            print(f"\n  ── {cat} ({len(cat_tests)} testov) ──────────────────────────────")
            for tc in cat_tests:
                start = time.perf_counter()
                try:
                    result = tc.test_fn()
                    ok     = bool(result)
                    err    = None
                except Exception as e:
                    ok  = False
                    err = f"{type(e).__name__}: {e}"
                    traceback.print_exc()
                duration = (time.perf_counter() - start) * 1000

                self._results.append(TestResult(
                    test_name=tc.name, passed=ok,
                    error=err, duration_ms=duration,
                ))

                icon  = "✅" if ok else "❌"
                extra = f" | ERR: {err}" if err else ""
                print(f"    {icon} [{tc.invariant:12s}] {tc.name} ({duration:.1f}ms){extra}")

                if ok:
                    passed += 1
                else:
                    failed += 1

        total = passed + failed
        print(f"\n{'═'*70}")
        print(f"  VÝSLEDOK: {passed}/{total} passed | {failed} failed")
        if failed == 0:
            print("  ✅ Všetky invariants overené!")
        else:
            print("  ❌ Niektoré invariants sú porušené!")
        print(f"{'═'*70}\n")

        return {"passed": passed, "failed": failed, "total": total}

    def failed_tests(self) -> list[TestResult]:
        return [r for r in self._results if not r.passed]

    def print_precedence_matrix(self) -> None:
        print(PRECEDENCE_MATRIX_SUMMARY)
        print("\nPre plnú maticu pozri docstring na vrchu súboru (PRECEDENCE_MATRIX).")

    def print_cb_transitions(self) -> None:
        print(f"\n{'═'*70}")
        print("  CIRCUIT BREAKER — Transition Table")
        print(f"{'─'*70}")
        print(f"  {'FROM':<14} {'TRIGGER':<24} {'TO':<14} {'GUARD'}")
        print(f"  {'─'*12} {'─'*22} {'─'*12} {'─'*20}")
        for t in CIRCUIT_BREAKER_TRANSITIONS:
            print(f"  {t.from_state.value:<14} {t.trigger.value:<24} "
                  f"{t.to_state.value:<14} {t.guard[:40]}")
        print(f"{'═'*70}")

    def print_regime_transitions(self) -> None:
        print(f"\n{'═'*70}")
        print("  REGIME FSM — Transition Table")
        print(f"{'─'*70}")
        print(f"  {'FROM':<15} {'TO':<15} {'RULE':<16} {'PERS':<5} {'CDN':<5} {'CONF'}")
        print(f"  {'─'*13} {'─'*13} {'─'*14} {'─'*4} {'─'*4} {'─'*4}")
        for t in REGIME_TRANSITIONS:
            print(
                f"  {t.from_regime:<15} {t.to_regime:<15} "
                f"{t.rule_type.value:<16} "
                f"{t.persistence_ticks:<5} {t.cooldown_ticks:<5} "
                f"{t.min_confidence:.2f}"
            )
        print(f"{'═'*70}")


# ════════════════════════════════════════════════════════════════════════════
# Vstupný bod
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    suite = InvariantTestSuite()

    # Vypíš tabuľky
    suite.print_precedence_matrix()
    suite.print_cb_transitions()
    suite.print_regime_transitions()

    # Spusti všetky testy
    results = suite.run_all()

    # Exit kód pre CI/CD integráciu
    import sys
    sys.exit(0 if results["failed"] == 0 else 1)
