"""
APEX BOT — Step 13: Execution Safety (v2 — produkčná verzia)
==============================================================
Chráni live trading systém pred operačnými a exekučnými zlyhaniami.

Stavy (v poradí závažnosti):
  HEALTHY          → plný beh
  DEGRADED         → čiastočné problémy, obchodovanie povolené s obmedzeniami
  UNSAFE           → blokuj nové ordery, len cancel/reduce
  CIRCUIT_BREAKER  → úplný halt, čakaj na cooldown + health ticks

Fail-safe konvencia:
  Nevalidné / chýbajúce vstupy → DEGRADED (nie HEALTHY).
  Ambiguita → vždy konzervatívnejší stav.

Recovery požiadavky (musí byť splnené SÚČASNE):
  1. market data fresh
  2. exchange heartbeat OK
  3. reject streak = 0
  4. desync nie je prítomný
  5. N zdravých tickov v rade
  6. cooldown uplynul
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("ApexBot.ExecSafety")


# ─────────────────────────────────────────────────────────────────────────────
# Enumerations
# ─────────────────────────────────────────────────────────────────────────────

class ExecSafetyState(str, Enum):
    HEALTHY         = "HEALTHY"
    DEGRADED        = "DEGRADED"
    UNSAFE          = "UNSAFE"
    CIRCUIT_BREAKER = "CIRCUIT_BREAKER"


class ExecReasonCode(str, Enum):
    ALL_OK                   = "ALL_OK"
    MARKET_DATA_STALE        = "MARKET_DATA_STALE"
    WS_STREAM_STALE          = "WS_STREAM_STALE"
    HEARTBEAT_FAILURE        = "HEARTBEAT_FAILURE"
    REST_DEGRADED            = "REST_DEGRADED"
    REJECT_STREAK            = "REJECT_STREAK"
    CANCEL_STREAK            = "CANCEL_STREAK"
    REPLACE_STREAK           = "REPLACE_STREAK"
    ACTION_BUDGET_EXCEEDED   = "ACTION_BUDGET_EXCEEDED"
    STALE_OPEN_ORDERS        = "STALE_OPEN_ORDERS"
    DESYNC_DETECTED          = "DESYNC_DETECTED"
    UNKNOWN_ORDER_STATES     = "UNKNOWN_ORDER_STATES"
    CIRCUIT_BREAKER_COOLDOWN = "CIRCUIT_BREAKER_COOLDOWN"
    RECOVERY_IN_PROGRESS     = "RECOVERY_IN_PROGRESS"
    DATA_VALIDATION_FAILED   = "DATA_VALIDATION_FAILED"


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionSafetyConfig:
    """
    Konzervatívne defaulty pre Binance Spot / BNB/USDT.

    Odporúčania:
      market_data_stale_sec = 30    — Binance WebSocket update každú sekundu
      ws_stale_sec          = 15    — WebSocket stale po 15s bez správy
      heartbeat_stale_sec   = 60    — ping/pong timeout
      rest_stale_sec        = 120   — REST fallback stale
      reject_streak_unsafe  = 3     — 3 rejecty za sebou = UNSAFE
      reject_streak_cb      = 5     — 5 rejectov = CIRCUIT_BREAKER
      cancel_streak_warn    = 5     — opakované cancely = degraded
      max_actions_per_min   = 15    — rate limit ochrana
      stale_order_age_sec   = 300   — order > 5 min bez fill = stale
      max_stale_orders      = 3     — nad toto = cancel
      desync_→ UNSAFE       = vždy
      cb_cooldown_sec       = 120   — 2 min cooldown
      cb_recovery_ticks     = 5     — 5 zdravých tickov pred recovery
    """
    # Stale thresholds
    market_data_stale_sec:   float = 30.0
    ws_stale_sec:            float = 15.0
    heartbeat_stale_sec:     float = 60.0
    rest_stale_sec:          float = 120.0

    # Streak thresholds
    reject_streak_degraded:  int   = 2
    reject_streak_unsafe:    int   = 3
    reject_streak_cb:        int   = 5
    cancel_streak_degraded:  int   = 5
    cancel_streak_unsafe:    int   = 10
    replace_streak_degraded: int   = 4

    # Rate limiting
    max_actions_per_min:     int   = 15
    action_budget_warn_pct:  float = 0.80   # varovanie pri 80% budgetu

    # Stale orders
    stale_order_age_sec:     float = 300.0
    max_stale_orders_before_cancel: int = 3

    # Circuit breaker
    cb_cooldown_sec:         float = 120.0
    cb_recovery_ticks:       int   = 5

    # Unknown order states (desync proxy)
    max_unknown_order_states: int  = 2

    # REST latency
    rest_latency_warn_ms:    float = 2000.0
    rest_latency_unsafe_ms:  float = 5000.0


# ─────────────────────────────────────────────────────────────────────────────
# Snapshot
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionSnapshot:
    """
    Aktuálny prevádzkový stav.
    Všetky timestamp polia sú UNIX time (time.time()).
    Chýbajúce / None hodnoty sa interpretujú konzervatívne.
    """
    now_ts:                    float
    last_market_data_ts:       float
    last_ws_message_ts:        float
    last_rest_ok_ts:           float
    exchange_heartbeat_ok:     bool
    open_order_count:          int
    stale_order_count:         int          # poèt orderov > stale_order_age_sec
    order_reject_streak:       int
    cancel_streak:             int
    replace_streak:            int
    actions_last_minute:       int
    max_actions_per_minute:    int
    desync_detected:           bool
    unknown_order_states:      int          # ordery v neznámom stave
    symbol:                    str

    # Voliteľné
    ws_connected:              Optional[bool]  = None
    rest_latency_ms:           Optional[float] = None
    last_successful_fill_ts:   Optional[float] = None
    last_successful_order_ack_ts: Optional[float] = None

    def age(self, ts: float) -> float:
        """Vráti vek timestampu v sekundách."""
        return self.now_ts - ts

    def validate(self) -> list[str]:
        """Vráti zoznam dátových problémov."""
        issues: list[str] = []
        if self.now_ts <= 0:
            issues.append("now_ts invalid")
        if self.last_market_data_ts > self.now_ts:
            issues.append("last_market_data_ts in future")
        if self.open_order_count < 0:
            issues.append("open_order_count < 0")
        if self.order_reject_streak < 0:
            issues.append("order_reject_streak < 0")
        if self.stale_order_count > self.open_order_count:
            issues.append("stale_order_count > open_order_count")
        return issues


# ─────────────────────────────────────────────────────────────────────────────
# Decision
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExecutionSafetyDecision:
    """
    Výstup ExecutionSafetyController.

    Invariants garantované touto triedou:
      safe_to_trade=False → block_new_orders=True
      block_new_orders=True → block_requotes=True
      trigger_circuit_breaker=True → safe_to_trade=False
    """
    state:                   ExecSafetyState
    safe_to_trade:           bool
    block_new_orders:        bool
    block_requotes:          bool
    cancel_stale_orders:     bool
    trigger_circuit_breaker: bool
    stale_data_detected:     bool
    exchange_health_ok:      bool
    recovery_allowed:        bool
    safe_mode:               bool
    action_budget_remaining: int
    reason_codes:            list[ExecReasonCode] = field(default_factory=list)

    def __post_init__(self):
        """Enforcement invariants."""
        if not self.safe_to_trade:
            object.__setattr__(self, "block_new_orders", True)
        if self.block_new_orders:
            object.__setattr__(self, "block_requotes", True)
        if self.trigger_circuit_breaker:
            object.__setattr__(self, "safe_to_trade", False)
            object.__setattr__(self, "block_new_orders", True)
            object.__setattr__(self, "block_requotes", True)

    def log_summary(self, logger: logging.Logger) -> None:
        icon = {
            ExecSafetyState.HEALTHY:         "✅",
            ExecSafetyState.DEGRADED:        "⚠️",
            ExecSafetyState.UNSAFE:          "🚫",
            ExecSafetyState.CIRCUIT_BREAKER: "🛑",
        }[self.state]
        codes = [c.value for c in self.reason_codes]
        logger.info(
            f"{icon} ExecSafety {self.state.value} | "
            f"safe={self.safe_to_trade} block={self.block_new_orders} "
            f"cb={self.trigger_circuit_breaker} "
            f"budget={self.action_budget_remaining} | "
            f"{', '.join(codes)}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# ExecutionSafetyController
# ─────────────────────────────────────────────────────────────────────────────

class ExecutionSafetyController:
    """
    Produkčný execution safety controller.

    Rozhodovací pipeline (v poradí závažnosti):
      1. Validácia vstupných dát → DEGRADED pri problémoch
      2. CIRCUIT_BREAKER stav — cooldown + recovery ticks
      3. Hard UNSAFE triggers (desync, heartbeat fail, critical streaks)
      4. DEGRADED triggers (stale data, moderate streaks, rate limit)
      5. Stale orders check
      6. Recovery evaluation
      7. HEALTHY ak žiadny trigger

    Použitie:
        ctrl = ExecutionSafetyController(cfg)
        decision = ctrl.evaluate(snapshot)
        if not decision.safe_to_trade:
            return  # preskočiť tick
    """

    def __init__(self, cfg: Optional[ExecutionSafetyConfig] = None):
        self.cfg                    = cfg or ExecutionSafetyConfig()
        self._state:                ExecSafetyState = ExecSafetyState.HEALTHY
        self._cb_triggered_at:      Optional[float] = None
        self._healthy_ticks:        int             = 0
        self._consecutive_healthy:  int             = 0
        self._last_state:           ExecSafetyState = ExecSafetyState.HEALTHY
        self._action_window:        deque[float]    = deque()

    # ── Hlavná metóda ─────────────────────────────────────────────────────────

    def evaluate(self, snap: ExecutionSnapshot) -> ExecutionSafetyDecision:
        cfg    = self.cfg
        codes: list[ExecReasonCode] = []

        # ── 0. Validácia dát → DEGRADED (nie HEALTHY) ────────────────────────
        data_issues = snap.validate()
        if data_issues:
            log.warning(f"[ExecSafety] Dátové problémy: {data_issues}")
            codes.append(ExecReasonCode.DATA_VALIDATION_FAILED)
            return self._make(
                ExecSafetyState.DEGRADED,
                safe=False, block=True, block_req=True,
                cancel_stale=False, cb=False,
                stale_data=True, health_ok=False,
                recovery=False, safe_mode=True,
                budget=0, codes=codes,
            )

        # ── 1. CIRCUIT_BREAKER stav — spracuj cooldown ────────────────────────
        if self._state == ExecSafetyState.CIRCUIT_BREAKER:
            result = self._handle_circuit_breaker(snap, codes)
            if result is not None:
                return result

        # ── Vypočítaj action budget ───────────────────────────────────────────
        budget = self._compute_budget(snap)

        # ── 2. Hard UNSAFE / CB triggers ─────────────────────────────────────

        # Desync → okamžite UNSAFE
        if snap.desync_detected:
            codes.append(ExecReasonCode.DESYNC_DETECTED)
            return self._trip_unsafe(snap, codes, budget)

        # Heartbeat failure → CIRCUIT_BREAKER
        if not snap.exchange_heartbeat_ok:
            codes.append(ExecReasonCode.HEARTBEAT_FAILURE)
            return self._trip_circuit_breaker(snap, codes, budget)

        # Unknown order states
        if snap.unknown_order_states >= cfg.max_unknown_order_states:
            codes.append(ExecReasonCode.UNKNOWN_ORDER_STATES)
            return self._trip_unsafe(snap, codes, budget)

        # Reject streak → CB
        if snap.order_reject_streak >= cfg.reject_streak_cb:
            codes.append(ExecReasonCode.REJECT_STREAK)
            return self._trip_circuit_breaker(snap, codes, budget)

        # Cancel streak → UNSAFE
        if snap.cancel_streak >= cfg.cancel_streak_unsafe:
            codes.append(ExecReasonCode.CANCEL_STREAK)
            return self._trip_unsafe(snap, codes, budget)

        # ── 3. DEGRADED triggers ──────────────────────────────────────────────
        state = ExecSafetyState.HEALTHY

        # Market data stale
        market_age = snap.age(snap.last_market_data_ts)
        if market_age > cfg.market_data_stale_sec:
            codes.append(ExecReasonCode.MARKET_DATA_STALE)
            state = ExecSafetyState.UNSAFE if market_age > cfg.market_data_stale_sec * 3 \
                    else ExecSafetyState.DEGRADED

        # WebSocket stale
        ws_age = snap.age(snap.last_ws_message_ts)
        if ws_age > cfg.ws_stale_sec:
            if ExecReasonCode.MARKET_DATA_STALE not in codes:
                codes.append(ExecReasonCode.WS_STREAM_STALE)
            if ws_age > cfg.ws_stale_sec * 2:
                state = max(state, ExecSafetyState.UNSAFE,
                            key=lambda s: list(ExecSafetyState).index(s))

        # REST stale
        rest_age = snap.age(snap.last_rest_ok_ts)
        if rest_age > cfg.rest_stale_sec:
            codes.append(ExecReasonCode.REST_DEGRADED)
            state = ExecSafetyState.DEGRADED if state == ExecSafetyState.HEALTHY else state

        # REST latency
        if snap.rest_latency_ms is not None:
            if snap.rest_latency_ms > cfg.rest_latency_unsafe_ms:
                codes.append(ExecReasonCode.REST_DEGRADED)
                state = ExecSafetyState.UNSAFE
            elif snap.rest_latency_ms > cfg.rest_latency_warn_ms:
                if ExecReasonCode.REST_DEGRADED not in codes:
                    codes.append(ExecReasonCode.REST_DEGRADED)
                state = max(state, ExecSafetyState.DEGRADED,
                            key=lambda s: list(ExecSafetyState).index(s))

        # Reject streak (moderate)
        if snap.order_reject_streak >= cfg.reject_streak_unsafe:
            codes.append(ExecReasonCode.REJECT_STREAK)
            state = ExecSafetyState.UNSAFE
        elif snap.order_reject_streak >= cfg.reject_streak_degraded:
            if ExecReasonCode.REJECT_STREAK not in codes:
                codes.append(ExecReasonCode.REJECT_STREAK)
            state = max(state, ExecSafetyState.DEGRADED,
                        key=lambda s: list(ExecSafetyState).index(s))

        # Cancel streak (moderate)
        if snap.cancel_streak >= cfg.cancel_streak_degraded:
            if ExecReasonCode.CANCEL_STREAK not in codes:
                codes.append(ExecReasonCode.CANCEL_STREAK)
            state = max(state, ExecSafetyState.DEGRADED,
                        key=lambda s: list(ExecSafetyState).index(s))

        # Replace streak
        if snap.replace_streak >= cfg.replace_streak_degraded:
            codes.append(ExecReasonCode.REPLACE_STREAK)
            state = max(state, ExecSafetyState.DEGRADED,
                        key=lambda s: list(ExecSafetyState).index(s))

        # Action budget
        if budget == 0:
            codes.append(ExecReasonCode.ACTION_BUDGET_EXCEEDED)
            state = max(state, ExecSafetyState.DEGRADED,
                        key=lambda s: list(ExecSafetyState).index(s))

        # ── 4. Stale orders ───────────────────────────────────────────────────
        cancel_stale = snap.stale_order_count >= cfg.max_stale_orders_before_cancel
        if snap.stale_order_count > 0:
            codes.append(ExecReasonCode.STALE_OPEN_ORDERS)

        # ── 5. Healthy tick tracking ──────────────────────────────────────────
        if state == ExecSafetyState.HEALTHY:
            self._consecutive_healthy += 1
            self._healthy_ticks += 1
        else:
            self._consecutive_healthy = 0

        if not codes:
            codes.append(ExecReasonCode.ALL_OK)

        stale_data   = ExecReasonCode.MARKET_DATA_STALE in codes or ExecReasonCode.WS_STREAM_STALE in codes
        health_ok    = state in (ExecSafetyState.HEALTHY, ExecSafetyState.DEGRADED)
        safe_to_trade = state in (ExecSafetyState.HEALTHY, ExecSafetyState.DEGRADED) and not stale_data

        # DEGRADED: safe_to_trade ale block_requotes
        block_new    = not safe_to_trade or state == ExecSafetyState.UNSAFE
        block_req    = block_new or state == ExecSafetyState.DEGRADED

        self._state = state
        decision = ExecutionSafetyDecision(
            state                   = state,
            safe_to_trade           = safe_to_trade,
            block_new_orders        = block_new,
            block_requotes          = block_req,
            cancel_stale_orders     = cancel_stale,
            trigger_circuit_breaker = False,
            stale_data_detected     = stale_data,
            exchange_health_ok      = health_ok,
            recovery_allowed        = False,
            safe_mode               = state != ExecSafetyState.HEALTHY,
            action_budget_remaining = budget,
            reason_codes            = codes,
        )
        decision.log_summary(log)
        return decision

    def record_action(self) -> None:
        """Zavolaj po každom úspešnom odoslaní príkazu."""
        self._action_window.append(time.time())

    def reset_reject_streak(self) -> None:
        """Zavolaj po úspešnom orderi — resetuje streak."""
        pass  # streak je v snapshote — caller zodpovedá za reset

    # ── Interné metódy ────────────────────────────────────────────────────────

    def _compute_budget(self, snap: ExecutionSnapshot) -> int:
        now    = snap.now_ts
        window = now - 60.0
        # Vyčisti staré záznamy
        while self._action_window and self._action_window[0] < window:
            self._action_window.popleft()
        used   = len(self._action_window) + snap.actions_last_minute
        limit  = min(snap.max_actions_per_minute, self.cfg.max_actions_per_min)
        return max(0, limit - used)

    def _trip_circuit_breaker(
        self,
        snap:   ExecutionSnapshot,
        codes:  list[ExecReasonCode],
        budget: int,
    ) -> ExecutionSafetyDecision:
        self._state          = ExecSafetyState.CIRCUIT_BREAKER
        self._cb_triggered_at = snap.now_ts
        self._consecutive_healthy = 0
        log.error(f"[ExecSafety] CIRCUIT BREAKER TRIP | {[c.value for c in codes]}")
        return ExecutionSafetyDecision(
            state                   = ExecSafetyState.CIRCUIT_BREAKER,
            safe_to_trade           = False,
            block_new_orders        = True,
            block_requotes          = True,
            cancel_stale_orders     = True,
            trigger_circuit_breaker = True,
            stale_data_detected     = True,
            exchange_health_ok      = False,
            recovery_allowed        = False,
            safe_mode               = True,
            action_budget_remaining = 0,
            reason_codes            = codes,
        )

    def _make(
        self,
        state:      "ExecSafetyState",
        safe:       bool,
        block:      bool,
        block_req:  bool,
        cancel_stale: bool,
        cb:         bool,
        stale_data: bool,
        health_ok:  bool,
        recovery:   bool,
        safe_mode:  bool,
        budget:     int,
        codes:      "list[ExecReasonCode]",
    ) -> ExecutionSafetyDecision:
        return ExecutionSafetyDecision(
            state                   = state,
            safe_to_trade           = safe,
            block_new_orders        = block,
            block_requotes          = block_req,
            cancel_stale_orders     = cancel_stale,
            trigger_circuit_breaker = cb,
            stale_data_detected     = stale_data,
            exchange_health_ok      = health_ok,
            recovery_allowed        = recovery,
            safe_mode               = safe_mode,
            action_budget_remaining = budget,
            reason_codes            = codes,
        )

    def _trip_unsafe(
        self,
        snap:   ExecutionSnapshot,
        codes:  list[ExecReasonCode],
        budget: int,
    ) -> ExecutionSafetyDecision:
        self._state = ExecSafetyState.UNSAFE
        self._consecutive_healthy = 0
        return ExecutionSafetyDecision(
            state                   = ExecSafetyState.UNSAFE,
            safe_to_trade           = False,
            block_new_orders        = True,
            block_requotes          = True,
            cancel_stale_orders     = snap.stale_order_count > 0,
            trigger_circuit_breaker = False,
            stale_data_detected     = ExecReasonCode.MARKET_DATA_STALE in codes,
            exchange_health_ok      = False,
            recovery_allowed        = False,
            safe_mode               = True,
            action_budget_remaining = budget,
            reason_codes            = codes,
        )

    def _handle_circuit_breaker(
        self,
        snap:  ExecutionSnapshot,
        codes: list[ExecReasonCode],
    ) -> Optional[ExecutionSafetyDecision]:
        """
        Spracuje Circuit Breaker stav.
        Vráti None ak môžeme pokračovať s normálnym vyhodnotením (recovery možná).
        Vráti rozhodnutie ak stále v CB.
        """
        cfg     = self.cfg
        elapsed = snap.now_ts - (self._cb_triggered_at or snap.now_ts)

        if elapsed < cfg.cb_cooldown_sec:
            remaining = int(cfg.cb_cooldown_sec - elapsed)
            codes.append(ExecReasonCode.CIRCUIT_BREAKER_COOLDOWN)
            return ExecutionSafetyDecision(
                state                   = ExecSafetyState.CIRCUIT_BREAKER,
                safe_to_trade           = False,
                block_new_orders        = True,
                block_requotes          = True,
                cancel_stale_orders     = False,
                trigger_circuit_breaker = False,
                stale_data_detected     = True,
                exchange_health_ok      = False,
                recovery_allowed        = False,
                safe_mode               = True,
                action_budget_remaining = 0,
                reason_codes            = codes,
            )

        # Cooldown uplynul — skontroluj recovery podmienky
        recovery_ok = self._check_recovery(snap)
        if not recovery_ok:
            codes.append(ExecReasonCode.RECOVERY_IN_PROGRESS)
            return ExecutionSafetyDecision(
                state                   = ExecSafetyState.CIRCUIT_BREAKER,
                safe_to_trade           = False,
                block_new_orders        = True,
                block_requotes          = True,
                cancel_stale_orders     = False,
                trigger_circuit_breaker = False,
                stale_data_detected     = False,
                exchange_health_ok      = False,
                recovery_allowed        = True,    # cooldown OK, čakáme na ticky
                safe_mode               = True,
                action_budget_remaining = 0,
                reason_codes            = codes,
            )

        # Recovery podmienky splnené → exit CB
        log.info("[ExecSafety] Circuit Breaker reset — systém obnovený")
        self._state = ExecSafetyState.HEALTHY
        self._cb_triggered_at = None
        self._consecutive_healthy = 0
        return None   # pokračuj s normálnym vyhodnotením

    def _check_recovery(self, snap: ExecutionSnapshot) -> bool:
        """
        Všetky podmienky musia byť splnené SÚČASNE.
        """
        cfg = self.cfg
        checks = [
            snap.age(snap.last_market_data_ts) <= cfg.market_data_stale_sec,
            snap.exchange_heartbeat_ok,
            snap.order_reject_streak == 0,
            not snap.desync_detected,
            snap.unknown_order_states < cfg.max_unknown_order_states,
            self._consecutive_healthy >= cfg.cb_recovery_ticks,
        ]
        failed = [i for i, ok in enumerate(checks) if not ok]
        if failed:
            log.debug(f"[ExecSafety] Recovery checks failed: indices {failed}")
        return all(checks)
