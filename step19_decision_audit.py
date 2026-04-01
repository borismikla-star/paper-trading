"""
APEX BOT — Step 19: Decision Audit Logging
============================================
Observability layer pre paper trading — štruktúrovaný audit trail.

NIE JE to business logika. Je to čistá observability vrstva.
Nesmie meniť správanie systému.

Formát: JSONL (jeden JSON objekt per riadok) + memory buffer
Export: .jsonl súbor + summary report

Integrácia do step5_main.py:
    auditor = DecisionAuditor("paper_trading_session.jsonl")
    # V každom tiku:
    auditor.log_decision(tick_context)
    auditor.log_transition(state_transition)
    auditor.log_action(action_taken)
    # Na konci:
    auditor.print_session_summary()
"""

from __future__ import annotations

import json
import logging
import threading
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("ApexBot.Audit")


# ─────────────────────────────────────────────────────────────────────────────
# Event types
# ─────────────────────────────────────────────────────────────────────────────

class AuditEventType(str, Enum):
    DECISION   = "DECISION"     # výsledok decision engine
    TRANSITION = "TRANSITION"   # state transition (CB, regime, risk)
    ACTION     = "ACTION"       # čo main loop reálne vykonal


class TransitionDomain(str, Enum):
    CIRCUIT_BREAKER  = "CIRCUIT_BREAKER"
    REGIME           = "REGIME"
    PORTFOLIO_RISK   = "PORTFOLIO_RISK"
    INVENTORY        = "INVENTORY"
    EXECUTION_SAFETY = "EXECUTION_SAFETY"


class ActionType(str, Enum):
    CANCEL_ALL          = "CANCEL_ALL"
    SKIP_TRADING        = "SKIP_TRADING"
    PLACE_BUY           = "PLACE_BUY"
    SUPPRESS_BUY        = "SUPPRESS_BUY"
    EXECUTE_DCA         = "EXECUTE_DCA"
    BLOCK_DCA           = "BLOCK_DCA"
    BLOCK_REBALANCE     = "BLOCK_REBALANCE"
    EXECUTE_REBALANCE   = "EXECUTE_REBALANCE"
    REDUCE_ONLY_ENFORCE = "REDUCE_ONLY_ENFORCE"
    PLACE_SELL          = "PLACE_SELL"
    HEARTBEAT           = "HEARTBEAT"


# ─────────────────────────────────────────────────────────────────────────────
# Audit entry models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DecisionAuditEntry:
    """
    Jeden záznam decision outcome — loguj každý tick.

    JSONL schéma:
    {
      "event": "DECISION",
      "ts": "2026-01-01T12:00:00",
      "tick": 42,
      "symbol": "BNBUSDT",
      "exec_state": "HEALTHY",
      "effective_regime": "RANGE",
      "regime_confidence": 0.72,
      "portfolio_risk_mode": "NORMAL",
      "inventory_state": "BALANCED",
      "vol_regime": "NORMAL",
      "dca_state": "NORMAL",
      "allow_trading": true,
      "allow_new_orders": true,
      "allow_new_buys": true,
      "allow_dca": false,
      "reduce_only_mode": false,
      "forced_cancel_all": false,
      "forced_inventory_reduction": false,
      "winning_layer": "DEFAULT",
      "reason_codes": ["ALL_OK"],
      "order_size_mult": 1.0,
      "blocked_actions": [],
      "forced_actions": []
    }
    """
    event:                      str = AuditEventType.DECISION.value
    ts:                         str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    tick:                       int = 0
    symbol:                     str = ""

    # Vstupný kontext
    exec_state:                 str = ""
    effective_regime:           str = ""
    regime_confidence:          float = 0.0
    portfolio_risk_mode:        str = ""
    inventory_state:            str = ""
    vol_regime:                 str = ""
    dca_state:                  str = ""

    # Výstup decision engine
    allow_trading:              bool = True
    allow_new_orders:           bool = True
    allow_new_buys:             bool = True
    allow_dca:                  bool = False
    reduce_only_mode:           bool = False
    forced_cancel_all:          bool = False
    forced_inventory_reduction: bool = False
    winning_layer:              str = ""
    reason_codes:               list[str] = field(default_factory=list)
    order_size_mult:            float = 1.0

    # Odvodené — čo bolo zablokované / vynútené
    blocked_actions:            list[str] = field(default_factory=list)
    forced_actions:             list[str] = field(default_factory=list)

    def __post_init__(self):
        """Odvoď blocked/forced z výstupu."""
        if not self.allow_trading:
            self.blocked_actions.append("TRADING")
        if not self.allow_new_orders:
            self.blocked_actions.append("NEW_ORDERS")
        if not self.allow_new_buys:
            self.blocked_actions.append("NEW_BUYS")
        if not self.allow_dca:
            self.blocked_actions.append("DCA")
        if self.reduce_only_mode:
            self.forced_actions.append("REDUCE_ONLY")
        if self.forced_cancel_all:
            self.forced_actions.append("CANCEL_ALL")
        if self.forced_inventory_reduction:
            self.forced_actions.append("INV_REDUCTION")


@dataclass
class TransitionAuditEntry:
    """
    State transition — loguj pri každej zmene stavu.

    JSONL schéma:
    {
      "event": "TRANSITION",
      "ts": "...",
      "tick": 42,
      "domain": "REGIME",
      "prev_state": "RANGE",
      "new_state": "PANIC",
      "trigger": "panic_detected",
      "confidence": 0.87,
      "cooldown_ticks": 0,
      "persistence_ticks": 0,
      "reason_codes": ["PANIC_OVERRIDE"],
      "detail": "panic confidence >= 0.60 override threshold"
    }
    """
    event:              str = AuditEventType.TRANSITION.value
    ts:                 str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    tick:               int = 0
    domain:             str = ""       # TransitionDomain.value
    prev_state:         str = ""
    new_state:          str = ""
    trigger:            str = ""
    confidence:         float = 0.0
    cooldown_ticks:     int = 0
    persistence_ticks:  int = 0
    reason_codes:       list[str] = field(default_factory=list)
    detail:             str = ""


@dataclass
class ActionAuditEntry:
    """
    Čo main loop reálne vykonal — loguj každú akciu.

    JSONL schéma:
    {
      "event": "ACTION",
      "ts": "...",
      "tick": 42,
      "action_type": "SUPPRESS_BUY",
      "symbol": "BNBUSDT",
      "price": 618.42,
      "qty": 0.0,
      "notional": 0.0,
      "reason": "INVENTORY_RISK:INV_REDUCE_ONLY_NO_BUY",
      "winning_layer": "INVENTORY_RISK",
      "detail": ""
    }
    """
    event:          str = AuditEventType.ACTION.value
    ts:             str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    tick:           int = 0
    action_type:    str = ""       # ActionType.value
    symbol:         str = ""
    price:          float = 0.0
    qty:            float = 0.0
    notional:       float = 0.0
    reason:         str = ""
    winning_layer:  str = ""
    detail:         str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Session statistics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class SessionSummary:
    """Záverečný súhrn paper trading session."""
    start_ts:              str
    end_ts:                str
    total_ticks:           int
    total_decisions:       int
    total_transitions:     int
    total_actions:         int

    # Blokovacie štatistiky
    trading_blocked_count: int
    buy_suppressed_count:  int
    dca_blocked_count:     int
    cancel_all_count:      int
    skip_trading_count:    int

    # Prechody
    panic_activations:     int
    cb_trips:              int
    regime_changes:        int
    portfolio_risk_changes:int

    # Winning layer distribúcia
    winning_layer_counts:  dict[str, int]

    # Najčastejšie reason codes
    top_reason_codes:      dict[str, int]

    # Varianty
    warnings:              list[str]

    def print_report(self) -> None:
        sep = "─" * 64
        print(f"\n{'═'*64}")
        print(f"  PAPER TRADING SESSION SUMMARY")
        print(f"{'═'*64}")
        print(f"  {'Obdobie:':<35} {self.start_ts} → {self.end_ts}")
        print(f"  {'Celkovo tickov:':<35} {self.total_ticks}")
        print(f"  {'Rozhodnutí:':<35} {self.total_decisions}")
        print(f"  {'State transitions:':<35} {self.total_transitions}")
        print(f"  {'Akcií:':<35} {self.total_actions}")
        print(sep)
        print(f"  BLOKOVACIE UDALOSTI")
        print(f"  {'Trading zablokovaný:':<35} {self.trading_blocked_count}×")
        print(f"  {'Buy potlačený:':<35} {self.buy_suppressed_count}×")
        print(f"  {'DCA zablokovaný:':<35} {self.dca_blocked_count}×")
        print(f"  {'Cancel all:':<35} {self.cancel_all_count}×")
        print(f"  {'Skip trading tick:':<35} {self.skip_trading_count}×")
        print(sep)
        print(f"  STATE PRECHODY")
        print(f"  {'PANIC aktivácie:':<35} {self.panic_activations}×")
        print(f"  {'Circuit Breaker tripy:':<35} {self.cb_trips}×")
        print(f"  {'Regime zmeny:':<35} {self.regime_changes}×")
        print(f"  {'Portfolio risk zmeny:':<35} {self.portfolio_risk_changes}×")
        print(sep)
        print(f"  WINNING LAYER DISTRIBÚCIA")
        for layer, count in sorted(self.winning_layer_counts.items(), key=lambda x: -x[1]):
            pct = count / max(self.total_decisions, 1) * 100
            print(f"  {'  '+layer+':':<35} {count:>5}× ({pct:.1f}%)")
        print(sep)
        print(f"  TOP REASON CODES")
        for code, count in sorted(self.top_reason_codes.items(), key=lambda x: -x[1])[:8]:
            print(f"  {'  '+code+':':<35} {count:>5}×")
        if self.warnings:
            print(sep)
            print(f"  VAROVANIA")
            for w in self.warnings[:5]:
                print(f"  ⚠️  {w}")
        print(f"{'═'*64}\n")


# ─────────────────────────────────────────────────────────────────────────────
# DecisionAuditor — hlavná trieda
# ─────────────────────────────────────────────────────────────────────────────

class DecisionAuditor:
    """
    Hlavný audit logger pre paper trading.

    Thread-safe: používa lock pre zápis do súboru.
    Memory buffer: posledných N entries vždy v RAM (pre rýchly prístup).
    JSONL súbor: kompletný audit trail na disku.

    Integrácia do step5_main.py:
        auditor = DecisionAuditor("session.jsonl", buffer_size=1000)

        # Decision:
        entry = DecisionAuditEntry(
            tick=tick_num, symbol=symbol,
            exec_state=exec_dec.state.value,
            effective_regime=regime_dec.effective_regime.value,
            ...výstup z DecisionEngine...
        )
        auditor.log_decision(entry)

        # Transition (len pri zmene):
        if regime_dec.regime_changed:
            auditor.log_transition(TransitionAuditEntry(
                tick=tick_num, domain=TransitionDomain.REGIME.value,
                prev_state=prev_regime, new_state=regime_dec.effective_regime.value,
                ...
            ))

        # Action:
        auditor.log_action(ActionAuditEntry(
            tick=tick_num, action_type=ActionType.SUPPRESS_BUY.value,
            reason=out.reason_codes[0] if out.reason_codes else "",
            ...
        ))
    """

    def __init__(
        self,
        jsonl_path:  Optional[str] = None,
        buffer_size: int           = 2000,
        symbol:      str           = "",
    ):
        self.symbol       = symbol
        self._buffer:     deque = deque(maxlen=buffer_size)
        self._lock        = threading.Lock()
        self._started_at  = datetime.now().isoformat(timespec="seconds")
        self._tick_count  = 0
        self._file        = None
        self._path        = Path(jsonl_path) if jsonl_path else None

        # Štatistiky
        self._dec_count   = 0
        self._trans_count = 0
        self._act_count   = 0
        self._stats:      dict[str, int] = defaultdict(int)

        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._file = open(self._path, "a", encoding="utf-8", buffering=1)
            log.info(f"[Audit] Zápis do {self._path}")

    # ── Loggers ───────────────────────────────────────────────────────────────

    def log_decision(self, entry: DecisionAuditEntry) -> None:
        """Loguj výsledok decision engine. Volaj každý tick."""
        self._tick_count += 1
        self._dec_count  += 1
        entry.tick       = entry.tick or self._tick_count
        entry.symbol     = entry.symbol or self.symbol
        self._stats["decision_total"] += 1

        # Štatistiky
        if not entry.allow_trading:
            self._stats["trading_blocked"] += 1
        if not entry.allow_new_buys and entry.allow_trading:
            self._stats["buy_suppressed"] += 1
        if not entry.allow_dca:
            self._stats["dca_blocked"] += 1
        if entry.forced_cancel_all:
            self._stats["cancel_all"] += 1
        if not entry.allow_trading:
            self._stats["skip_trading"] += 1

        self._stats[f"winner_{entry.winning_layer}"] += 1
        for code in entry.reason_codes:
            self._stats[f"reason_{code}"] += 1

        self._write(entry)

    def log_transition(self, entry: TransitionAuditEntry) -> None:
        """Loguj state transition. Volaj len pri zmene stavu."""
        self._trans_count += 1
        entry.tick        = entry.tick or self._tick_count
        self._stats["transition_total"] += 1
        self._stats[f"trans_{entry.domain}_{entry.new_state}"] += 1

        if entry.domain == TransitionDomain.REGIME.value and entry.new_state == "PANIC":
            self._stats["panic_activations"] += 1
        if entry.domain == TransitionDomain.CIRCUIT_BREAKER.value and entry.new_state == "OPEN":
            self._stats["cb_trips"] += 1
        if entry.domain == TransitionDomain.REGIME.value:
            self._stats["regime_changes"] += 1
        if entry.domain == TransitionDomain.PORTFOLIO_RISK.value:
            self._stats["portfolio_risk_changes"] += 1

        log.info(
            f"[Audit][TRANSITION] {entry.domain}: "
            f"{entry.prev_state} → {entry.new_state} "
            f"(trigger={entry.trigger} conf={entry.confidence:.2f})"
        )
        self._write(entry)

    def log_action(self, entry: ActionAuditEntry) -> None:
        """Loguj akciu main loopu."""
        self._act_count += 1
        entry.tick       = entry.tick or self._tick_count
        entry.symbol     = entry.symbol or self.symbol
        self._stats["action_total"] += 1
        self._stats[f"action_{entry.action_type}"] += 1
        self._write(entry)

    # ── Skratky pre časté akcie ───────────────────────────────────────────────

    def suppress_buy(self, tick: int, price: float, reason: str, winning_layer: str = "") -> None:
        self.log_action(ActionAuditEntry(
            tick=tick, action_type=ActionType.SUPPRESS_BUY.value,
            symbol=self.symbol, price=price, reason=reason,
            winning_layer=winning_layer,
        ))

    def block_dca(self, tick: int, reason: str, winning_layer: str = "") -> None:
        self.log_action(ActionAuditEntry(
            tick=tick, action_type=ActionType.BLOCK_DCA.value,
            symbol=self.symbol, reason=reason, winning_layer=winning_layer,
        ))

    def cancel_all(self, tick: int, reason: str) -> None:
        self.log_action(ActionAuditEntry(
            tick=tick, action_type=ActionType.CANCEL_ALL.value,
            symbol=self.symbol, reason=reason,
        ))

    def skip_trading(self, tick: int, reason: str, winning_layer: str = "") -> None:
        self.log_action(ActionAuditEntry(
            tick=tick, action_type=ActionType.SKIP_TRADING.value,
            symbol=self.symbol, reason=reason, winning_layer=winning_layer,
        ))

    def place_buy(self, tick: int, price: float, qty: float) -> None:
        self.log_action(ActionAuditEntry(
            tick=tick, action_type=ActionType.PLACE_BUY.value,
            symbol=self.symbol, price=price, qty=qty,
            notional=price*qty,
        ))

    # ── Summary ───────────────────────────────────────────────────────────────

    def generate_summary(self) -> SessionSummary:
        """Vygeneruje záverečný súhrn session."""
        now = datetime.now().isoformat(timespec="seconds")

        # Winning layer counts
        wl_counts: dict[str, int] = {}
        for k, v in self._stats.items():
            if k.startswith("winner_"):
                wl_counts[k[7:]] = v

        # Top reason codes
        rc_counts: dict[str, int] = {}
        for k, v in self._stats.items():
            if k.startswith("reason_"):
                rc_counts[k[7:]] = v

        # Warnings
        warnings = []
        total_dec = max(self._stats.get("decision_total", 1), 1)
        if self._stats.get("panic_activations", 0) > 5:
            warnings.append(f"Veľa PANIC aktivácií: {self._stats['panic_activations']}×")
        if self._stats.get("cb_trips", 0) > 3:
            warnings.append(f"Veľa CB tripov: {self._stats['cb_trips']}×")
        blocked_pct = self._stats.get("trading_blocked", 0) / total_dec * 100
        if blocked_pct > 30:
            warnings.append(f"Obchodovanie zablokované v {blocked_pct:.1f}% tickov")
        buy_supp_pct = self._stats.get("buy_suppressed", 0) / total_dec * 100
        if buy_supp_pct > 50:
            warnings.append(f"Buy potlačený v {buy_supp_pct:.1f}% tickov — skontroluj config")

        return SessionSummary(
            start_ts               = self._started_at,
            end_ts                 = now,
            total_ticks            = self._tick_count,
            total_decisions        = self._dec_count,
            total_transitions      = self._trans_count,
            total_actions          = self._act_count,
            trading_blocked_count  = self._stats.get("trading_blocked", 0),
            buy_suppressed_count   = self._stats.get("buy_suppressed", 0),
            dca_blocked_count      = self._stats.get("dca_blocked", 0),
            cancel_all_count       = self._stats.get("cancel_all", 0),
            skip_trading_count     = self._stats.get("skip_trading", 0),
            panic_activations      = self._stats.get("panic_activations", 0),
            cb_trips               = self._stats.get("cb_trips", 0),
            regime_changes         = self._stats.get("regime_changes", 0),
            portfolio_risk_changes = self._stats.get("portfolio_risk_changes", 0),
            winning_layer_counts   = wl_counts,
            top_reason_codes       = rc_counts,
            warnings               = warnings,
        )

    def print_session_summary(self) -> SessionSummary:
        """Vypíše a vráti session summary."""
        summary = self.generate_summary()
        summary.print_report()
        return summary

    def recent_entries(self, n: int = 10) -> list[dict]:
        """Vráti posledných N záznamov z bufferu."""
        return list(self._buffer)[-n:]

    def flush(self) -> None:
        """Vynutí zápis na disk."""
        if self._file:
            self._file.flush()

    def close(self) -> None:
        """Uzavrie súbor."""
        if self._file:
            self._file.flush()
            self._file.close()
            self._file = None

    def __enter__(self) -> "DecisionAuditor":
        return self

    def __exit__(self, *args) -> None:
        self.close()

    # ── Interné ───────────────────────────────────────────────────────────────

    def _write(self, entry: object) -> None:
        """Thread-safe zápis do bufferu a súboru."""
        try:
            d = asdict(entry) if hasattr(entry, '__dataclass_fields__') else vars(entry)
        except Exception:
            d = {"raw": str(entry)}

        with self._lock:
            self._buffer.append(d)
            if self._file:
                try:
                    self._file.write(json.dumps(d, ensure_ascii=False) + "\n")
                except Exception as e:
                    log.error(f"[Audit] Zápis zlyhal: {e}")

    # ── Filter / analýza bufferu ──────────────────────────────────────────────

    def filter_by_event(self, event_type: AuditEventType) -> list[dict]:
        return [e for e in self._buffer if e.get("event") == event_type.value]

    def filter_by_action(self, action_type: ActionType) -> list[dict]:
        return [e for e in self._buffer
                if e.get("event") == AuditEventType.ACTION.value
                and e.get("action_type") == action_type.value]

    def decisions_where_blocked(self) -> list[dict]:
        return [e for e in self._buffer
                if e.get("event") == AuditEventType.DECISION.value
                and not e.get("allow_trading", True)]


# ─────────────────────────────────────────────────────────────────────────────
# Paper Trading Gate
# ─────────────────────────────────────────────────────────────────────────────

class PaperTradingGate:
    """
    Finálna brána pred paper trading nasadením.

    Agreguje výsledky parity auditu, E2E scenárov a invariant testov.
    Rozhodne: PASS (nasadiť) alebo FAIL (blokovať).

    Hard blockers (FAIL):
      - Parity audit failure
      - Invariant failure
      - Nepovolený FSM transition
      - Panic override mimo spec
      - Nekonzistentný DecisionOutcome (akýkoľvek)
      - E2E scenár FAIL

    Soft warnings (PASS s varovaniami):
      - Príliš časté regime switching v scenároch
      - Príliš veľa blocked buys v healthy scenario
      - Nadmerný počet stale order warnings
      - Chýbajúce nepodstatné audit polia
    """

    HARD_BLOCKERS = [
        "parity_audit_failed",
        "invariant_test_failed",
        "e2e_scenario_failed",
        "fsm_transition_mismatch",
        "panic_override_mismatch",
        "decision_outcome_inconsistent",
    ]

    SOFT_WARNINGS = [
        "frequent_regime_switching",
        "excessive_buy_suppression_in_healthy",
        "stale_order_warnings",
        "missing_audit_fields",
    ]

    def __init__(self):
        self._blockers: list[str] = []
        self._warnings: list[str] = []

    def check_parity(self, parity_result) -> None:
        if not parity_result.all_ok:
            self._blockers.append(
                f"parity_audit_failed: {len(parity_result.failed_checks)} checks failed"
            )
            for fc in parity_result.failed_checks:
                self._blockers.append(f"  ↳ [{fc.area}] {fc.name}: {fc.observed}")

    def check_invariants(self, invariant_result: dict) -> None:
        if invariant_result.get("failed", 0) > 0:
            self._blockers.append(
                f"invariant_test_failed: {invariant_result['failed']}/{invariant_result['total']} failed"
            )

    def check_e2e(self, e2e_report: dict) -> None:
        from step18_e2e_scenarios import ScenarioStatus
        results = e2e_report.get("results", [])
        hard_fails = [r for r in results
                      if hasattr(r, 'status') and r.status == ScenarioStatus.FAIL]
        warnings_only = [r for r in results
                         if hasattr(r, 'status') and r.status == ScenarioStatus.WARNING]
        if hard_fails:
            self._blockers.append(
                f"e2e_scenario_failed: {len(hard_fails)} scenárov zlyhalo (FAIL)"
            )
            for r in hard_fails:
                self._blockers.append(
                    f"  ↳ {r.scenario_id} {r.name}: {'; '.join(r.violations[:2])}"
                )
        for r in warnings_only:
            self._warnings.append(
                f"E2E WARNING {r.scenario_id} {r.name}: {'; '.join(r.warnings[:1])}"
            )

    def add_warning(self, msg: str) -> None:
        self._warnings.append(msg)

    def evaluate(self) -> tuple[bool, str]:
        """
        Vráti (passed: bool, report: str).
        passed=True → paper trading POVOLENÝ
        passed=False → paper trading BLOKOVANÝ
        """
        passed = len(self._blockers) == 0
        lines  = [
            "═" * 64,
            "  PAPER TRADING GATE — VÝSLEDOK",
            "═" * 64,
        ]

        if passed:
            lines.append("  ✅ GATE: PASS — systém je pripravený na paper trading")
        else:
            lines.append("  ❌ GATE: FAIL — nasadenie ZABLOKOVANÉ")

        if self._blockers:
            lines.append(f"\n  ⛔ HARD BLOCKERS ({len(self._blockers)}):")
            for b in self._blockers:
                lines.append(f"     {b}")

        if self._warnings:
            lines.append(f"\n  ⚠️  SOFT WARNINGS ({len(self._warnings)}):")
            for w in self._warnings:
                lines.append(f"     {w}")

        lines.append("═" * 64)
        report = "\n".join(lines)
        return passed, report
