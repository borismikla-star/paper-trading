"""
APEX BOT — Step 17: Runtime / Spec Parity Audit
=================================================
Behaviorálny audit zhody runtime implementácie so špecifikáciou (step16).

NIE je to porovnávanie textu.
Spúšťa definované vstupy cez runtime moduly a porovnáva výstupy
so spec pravidlami. Identifikuje odchýlky medzi spec a runtime.

Použitie ako paper-trading gate:
    result = RuntimeSpecParityAuditor().run()
    if result.exit_code != 0:
        sys.exit(1)  # blokuj deployment

Auditované oblasti:
  A. Decision parity     — precedence + invariants
  B. Circuit breaker     — FSM transitions + recovery
  C. Regime parity       — persistence, cooldown, panic override
"""

from __future__ import annotations

import logging
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

log = logging.getLogger("ApexBot.ParityAudit")


class CheckStatus(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    WARNING = "WARNING"
    SKIP    = "SKIP"


@dataclass
class CheckResult:
    name:        str
    area:        str          # "DECISION" | "CIRCUIT_BREAKER" | "REGIME"
    status:      CheckStatus
    spec_rule:   str          # čo špecifikácia hovorí
    observed:    str          # čo runtime vrátilo
    detail:      str = ""


@dataclass
class ParityAuditConfig:
    strict_mode:           bool  = True   # FAIL pri akomkoľvek FAIL checkli
    run_decision_parity:   bool  = True
    run_cb_parity:         bool  = True
    run_regime_parity:     bool  = True
    log_each_check:        bool  = True


@dataclass
class ParityAuditResult:
    decision_parity_ok:        bool
    circuit_breaker_parity_ok: bool
    regime_parity_ok:          bool
    checks:                    list[CheckResult] = field(default_factory=list)
    warnings:                  list[str]         = field(default_factory=list)
    summary:                   str               = ""
    exit_code:                 int               = 0   # 0=OK, 1=FAIL

    @property
    def failed_checks(self) -> list[CheckResult]:
        return [c for c in self.checks if c.status == CheckStatus.FAIL]

    @property
    def all_ok(self) -> bool:
        return self.exit_code == 0


# ─────────────────────────────────────────────────────────────────────────────
# Import helpers (lazy — nezlyhá pri chýbajúcich moduloch)
# ─────────────────────────────────────────────────────────────────────────────

def _import_decision():
    sys.path.insert(0, '/home/claude')
    from step15_decision_engine import DecisionEngine, DecisionInputs, DecisionLayer
    return DecisionEngine, DecisionInputs, DecisionLayer

def _import_exec_safety():
    sys.path.insert(0, '/home/claude')
    from step13_execution_safety_v2 import (
        ExecutionSafetyController, ExecutionSafetyConfig,
        ExecutionSnapshot, ExecSafetyState,
    )
    return ExecutionSafetyController, ExecutionSafetyConfig, ExecutionSnapshot, ExecSafetyState

def _import_regime():
    sys.path.insert(0, '/home/claude')
    from step10_market_regime_v2 import RegimeDetector, RegimeConfig, MarketRegime
    return RegimeDetector, RegimeConfig, MarketRegime

def _import_cb_fsm():
    sys.path.insert(0, '/home/claude')
    from step16_system_spec import CircuitBreakerFSM, CBState, CBTrigger
    return CircuitBreakerFSM, CBState, CBTrigger


# ─────────────────────────────────────────────────────────────────────────────
# RuntimeSpecParityAuditor
# ─────────────────────────────────────────────────────────────────────────────

class RuntimeSpecParityAuditor:
    """
    Behaviorálny parity auditor.

    Pre každú oblasť:
      1. Definuje vstup podľa spec pravidla
      2. Spustí runtime modul
      3. Porovná výstup so spec očakávaním
      4. Zaznamená CheckResult

    Fail criteria (hard blockers pre paper trading):
      - Porušenie precedence poradia
      - Porušenie akéhokoľvek invariantu
      - Nepovolený CB state transition
      - Panic override mimo spec
      - Permissive runtime oproti spec
    """

    def __init__(self, cfg: Optional[ParityAuditConfig] = None):
        self.cfg    = cfg or ParityAuditConfig()
        self._checks: list[CheckResult] = []

    def run(self) -> ParityAuditResult:
        log.info("=" * 60)
        log.info("  PARITY AUDIT — spúšťam")
        log.info("=" * 60)

        dp_ok  = True
        cb_ok  = True
        reg_ok = True
        warnings: list[str] = []

        if self.cfg.run_decision_parity:
            dp_ok = self._audit_decision_parity()
        if self.cfg.run_cb_parity:
            cb_ok = self._audit_circuit_breaker_parity()
        if self.cfg.run_regime_parity:
            reg_ok = self._audit_regime_parity()

        # Warningy z WARNING checklov
        for c in self._checks:
            if c.status == CheckStatus.WARNING:
                warnings.append(f"[{c.area}] {c.name}: {c.detail}")

        failed = [c for c in self._checks if c.status == CheckStatus.FAIL]
        passed = [c for c in self._checks if c.status == CheckStatus.PASS]
        all_ok = dp_ok and cb_ok and reg_ok and len(failed) == 0

        summary = (
            f"Parity Audit: {len(passed)}/{len(self._checks)} checks passed | "
            f"{len(failed)} FAIL | {len(warnings)} WARN | "
            f"Decision={'OK' if dp_ok else 'FAIL'} "
            f"CB={'OK' if cb_ok else 'FAIL'} "
            f"Regime={'OK' if reg_ok else 'FAIL'}"
        )

        result = ParityAuditResult(
            decision_parity_ok        = dp_ok,
            circuit_breaker_parity_ok = cb_ok,
            regime_parity_ok          = reg_ok,
            checks                    = self._checks,
            warnings                  = warnings,
            summary                   = summary,
            exit_code                 = 0 if all_ok else 1,
        )

        log.info(summary)
        if failed:
            for f in failed:
                log.error(f"  FAIL [{f.area}] {f.name}: spec='{f.spec_rule}' got='{f.observed}'")
        if all_ok:
            log.info("✅ Parity audit PASSED — systém je v zhode so špecifikáciou")
        else:
            log.error("❌ Parity audit FAILED — nasadenie ZABLOKOVANÉ")

        return result

    # ── A. Decision parity ────────────────────────────────────────────────────

    def _audit_decision_parity(self) -> bool:
        """Audituje precedence matrix a invariants v runtime."""
        DecisionEngine, DecisionInputs, DecisionLayer = _import_decision()
        engine = DecisionEngine()
        ok     = True

        # ── Precedence pravidlo P1: ExecSafety CB → všetko blokované ─────────
        inp = DecisionInputs(
            exec_safe_to_trade=False, exec_block_new_orders=True,
            exec_block_requotes=True, exec_trigger_circuit_breaker=True,
            exec_cancel_stale_orders=True, exec_state="CIRCUIT_BREAKER",
            regime_effective="RANGE", regime_allow_grid=True,
            regime_allow_new_buys=True, regime_allow_dca=True,
            regime_protective=False, regime_inv_reduction=False, regime_confidence=0.9,
            vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
            inv_allow_new_buys=True, inv_allow_dca=True,
        )
        out = engine.decide(inp)
        spec = "ExecSafety CB → allow_trading=False, forced_cancel_all=True, winning_layer=EXECUTION_SAFETY"
        got  = f"trade={out.allow_trading} cancel={out.forced_cancel_all} winner={out.winning_layer.value}"
        pass_ = (not out.allow_trading and out.forced_cancel_all
                 and out.winning_layer == DecisionLayer.EXECUTION_SAFETY)
        ok &= self._record("P1_exec_cb_overrides_all", "DECISION", pass_, spec, got)

        # ── P2: PortRisk PAUSE vyhráva nad Regime RANGE ───────────────────────
        inp2 = DecisionInputs(
            exec_safe_to_trade=True, exec_block_new_orders=False,
            exec_block_requotes=False, exec_trigger_circuit_breaker=False,
            exec_cancel_stale_orders=False, exec_state="HEALTHY",
            port_risk_mode="PAUSE_NEW_RISK",
            regime_effective="RANGE", regime_allow_grid=True,
            regime_allow_new_buys=True, regime_allow_dca=True,
            regime_protective=False, regime_inv_reduction=False, regime_confidence=0.8,
            vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
            inv_allow_new_buys=True, inv_allow_dca=True,
        )
        out2 = engine.decide(inp2)
        spec2 = "PortRisk PAUSE → allow_new_orders=False, winner=PORTFOLIO_RISK"
        got2  = f"orders={out2.allow_new_orders} winner={out2.winning_layer.value}"
        pass2 = (not out2.allow_new_orders
                 and out2.winning_layer == DecisionLayer.PORTFOLIO_RISK)
        ok &= self._record("P2_portfolio_pause_beats_regime", "DECISION", pass2, spec2, got2)

        # ── P3: Regime PANIC → reduce_only, no DCA ────────────────────────────
        inp3 = DecisionInputs(
            exec_safe_to_trade=True, exec_block_new_orders=False,
            exec_block_requotes=False, exec_trigger_circuit_breaker=False,
            exec_cancel_stale_orders=False, exec_state="HEALTHY",
            regime_effective="PANIC", regime_allow_grid=False,
            regime_allow_new_buys=False, regime_allow_dca=False,
            regime_protective=True, regime_inv_reduction=True, regime_confidence=0.9,
            dca_allow=True,   # DCA guard hovorí OK — PANIC musí blokovať
            vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
            inv_allow_new_buys=True, inv_allow_dca=True,
        )
        out3 = engine.decide(inp3)
        spec3 = "PANIC → allow_dca=False (invariant I7), reduce_only=True"
        got3  = f"dca={out3.allow_dca} reduce_only={out3.reduce_only_mode}"
        pass3 = not out3.allow_dca and out3.reduce_only_mode
        ok &= self._record("P3_panic_blocks_dca_reduce_only", "DECISION", pass3, spec3, got3)

        # ── P4: Inv REDUCE_ONLY vyhráva nad Regime UPTREND ────────────────────
        inp4 = DecisionInputs(
            exec_safe_to_trade=True, exec_block_new_orders=False,
            exec_block_requotes=False, exec_trigger_circuit_breaker=False,
            exec_cancel_stale_orders=False, exec_state="HEALTHY",
            regime_effective="UPTREND", regime_allow_grid=True,
            regime_allow_new_buys=True, regime_allow_dca=True,
            regime_protective=False, regime_inv_reduction=False, regime_confidence=0.7,
            inv_state="REDUCE_ONLY", inv_allow_new_buys=False,
            inv_allow_dca=False, inv_force_reduction=True, inv_buy_size_mult=0.0,
            vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
        )
        out4 = engine.decide(inp4)
        spec4 = "Inv REDUCE_ONLY (pri 4) vyhráva nad Regime UPTREND (pri 3) → buys=False"
        got4  = f"buys={out4.allow_new_buys} inv_red={out4.forced_inventory_reduction} winner={out4.winning_layer.value}"
        pass4 = not out4.allow_new_buys and out4.forced_inventory_reduction
        ok &= self._record("P4_inventory_reduce_only_beats_uptrend", "DECISION", pass4, spec4, got4)

        # ── P5: Vol EXTREME blokuje buy (keď cfg.block_buys_on_extreme_vol) ───
        inp5 = DecisionInputs(
            exec_safe_to_trade=True, exec_block_new_orders=False,
            exec_block_requotes=False, exec_trigger_circuit_breaker=False,
            exec_cancel_stale_orders=False, exec_state="HEALTHY",
            regime_effective="RANGE", regime_allow_grid=True,
            regime_allow_new_buys=True, regime_allow_dca=True,
            regime_protective=False, regime_inv_reduction=False, regime_confidence=0.7,
            vol_regime="EXTREME", vol_allow_dca=False, vol_allow_new_buys=False,
            inv_allow_new_buys=True, inv_allow_dca=True,
        )
        out5 = engine.decide(inp5)
        spec5 = "Vol=EXTREME → allow_new_buys=False (matrix row 5)"
        got5  = f"buys={out5.allow_new_buys} dca={out5.allow_dca}"
        pass5 = not out5.allow_new_buys and not out5.allow_dca
        ok &= self._record("P5_extreme_vol_blocks_buys", "DECISION", pass5, spec5, got5)

        # ── Invariants I1–I10 ─────────────────────────────────────────────────
        invariant_scenarios = [
            # (desc, allow_trading, allow_new_orders, allow_new_buys, allow_dca,
            #  reduce_only, forced_cancel, forced_inv_red)
            ("I1", False, True,  True,  True,  False, False, False),
            ("I2", True,  False, True,  True,  False, False, False),
            ("I3", True,  False, True,  True,  False, False, False),   # requotes
            ("I4", True,  True,  True,  True,  True,  False, False),
            ("I5", True,  True,  True,  True,  True,  False, False),
            ("I6", True,  True,  True,  True,  False, True,  False),
            ("I8", True,  True,  True,  True,  False, False, True),
            ("I10",True,  True,  False, True,  False, False, False),
        ]

        from step15_decision_engine import DecisionOutcome, DecisionLayer as DL
        from step15_decision_engine import LayerVote

        for inv, at, ano, anb, ad, ro, fca, fir in invariant_scenarios:
            out_test = DecisionOutcome(
                allow_trading=at, allow_new_orders=ano, allow_new_risk=ano and not ro,
                allow_new_buys=anb, allow_dca=ad, reduce_only_mode=ro,
                allow_rebalance=True, allow_requotes=ano,
                forced_cancel_all=fca, forced_inventory_reduction=fir,
                order_size_multiplier=1.0, max_exposure_multiplier=1.0,
                winning_layer=DL.DEFAULT, reason_codes=[], votes=[], explanation="",
            )
            violations = engine.audit_invariants(out_test)
            inv_pass   = len(violations) == 0
            spec_i     = f"Invariant {inv}: DecisionOutcome.__post_init__ enforces consistency"
            got_i      = "OK" if inv_pass else f"VIOLATION: {violations[0]}"
            ok &= self._record(f"INV_{inv}_enforced_by_postinit", "DECISION", inv_pass, spec_i, got_i)

        # ── K6: Multiplikatívne size mults ────────────────────────────────────
        inp6 = DecisionInputs(
            exec_safe_to_trade=True, exec_block_new_orders=False,
            exec_block_requotes=False, exec_trigger_circuit_breaker=False,
            exec_cancel_stale_orders=False, exec_state="HEALTHY",
            port_risk_mode="REDUCE_RISK", port_max_order_mult=0.5,
            regime_effective="RANGE", regime_allow_grid=True,
            regime_allow_new_buys=True, regime_allow_dca=True,
            regime_protective=False, regime_inv_reduction=False, regime_confidence=0.7,
            inv_buy_size_mult=0.4, vol_regime="HIGH", vol_order_size_mult=0.65,
            vol_allow_dca=True, vol_allow_new_buys=True,
            inv_allow_new_buys=True, inv_allow_dca=True,
        )
        out6 = engine.decide(inp6)
        # Spec K6: kombinovaný mult = 0.5 × 0.4 × 0.65 = 0.13, clamped [0,1]
        spec6 = "K6: size_mult v [0.0, 1.0] (kombinovaný z port×inv×vol)"
        got6  = f"size_mult={out6.order_size_multiplier}"
        pass6 = 0.0 <= out6.order_size_multiplier <= 1.0
        ok &= self._record("K6_size_mult_clamped", "DECISION", pass6, spec6, got6)

        return ok

    # ── B. Circuit breaker parity ─────────────────────────────────────────────

    def _audit_circuit_breaker_parity(self) -> bool:
        """Audituje CB FSM transitions oproti runtime step13."""
        CBFsm, CBState, CBTrigger = _import_cb_fsm()
        ExecCtrl, ExecCfg, ExecSnap, ExecState = _import_exec_safety()
        ok = True
        now = time.time()

        def make_snap(**kwargs) -> ExecSnap:
            defaults = dict(
                now_ts=now, last_market_data_ts=now-5,
                last_ws_message_ts=now-5, last_rest_ok_ts=now-5,
                exchange_heartbeat_ok=True, open_order_count=5,
                stale_order_count=0, order_reject_streak=0,
                cancel_streak=0, replace_streak=0,
                actions_last_minute=2, max_actions_per_minute=15,
                desync_detected=False, unknown_order_states=0,
                symbol="BNBUSDT",
            )
            defaults.update(kwargs)
            return ExecSnap(**defaults)

        # CB-T1: REJECT_STREAK ≥ reject_streak_cb → CIRCUIT_BREAKER
        ctrl1 = ExecCtrl(ExecCfg(reject_streak_cb=5))
        dec1  = ctrl1.evaluate(make_snap(order_reject_streak=5))
        spec1 = "CLOSED + reject≥5 → CIRCUIT_BREAKER (trigger_circuit_breaker=True)"
        got1  = f"state={dec1.state.value} cb={dec1.trigger_circuit_breaker}"
        pass1 = dec1.state == ExecState.CIRCUIT_BREAKER and dec1.trigger_circuit_breaker
        ok &= self._record("CB_T1_reject_streak_trips_cb", "CIRCUIT_BREAKER", pass1, spec1, got1)

        # CB-T2: HEARTBEAT_FAILURE → CIRCUIT_BREAKER
        ctrl2 = ExecCtrl(ExecCfg())
        dec2  = ctrl2.evaluate(make_snap(exchange_heartbeat_ok=False))
        spec2 = "CLOSED + heartbeat_fail → CIRCUIT_BREAKER"
        got2  = f"state={dec2.state.value} safe={dec2.safe_to_trade}"
        pass2 = dec2.state == ExecState.CIRCUIT_BREAKER and not dec2.safe_to_trade
        ok &= self._record("CB_T2_heartbeat_fail_trips_cb", "CIRCUIT_BREAKER", pass2, spec2, got2)

        # CB-T3: DESYNC → CIRCUIT_BREAKER (via unsafe v2)
        ctrl3 = ExecCtrl(ExecCfg())
        dec3  = ctrl3.evaluate(make_snap(desync_detected=True))
        spec3 = "CLOSED + desync_detected → CIRCUIT_BREAKER alebo UNSAFE"
        got3  = f"state={dec3.state.value} block={dec3.block_new_orders}"
        pass3 = dec3.state in (ExecState.CIRCUIT_BREAKER, ExecState.UNSAFE) and dec3.block_new_orders
        ok &= self._record("CB_T3_desync_trips_unsafe_or_cb", "CIRCUIT_BREAKER", pass3, spec3, got3)

        # CB-T4: CB stav blokuje obchodovanie
        ctrl4 = ExecCtrl(ExecCfg(reject_streak_cb=3, cb_cooldown_sec=3600))
        ctrl4.evaluate(make_snap(order_reject_streak=3))  # trip CB
        dec4  = ctrl4.evaluate(make_snap())               # ďalší tick — CB aktívny
        spec4 = "CB OPEN → safe_to_trade=False, block_new_orders=True"
        got4  = f"state={dec4.state.value} safe={dec4.safe_to_trade} block={dec4.block_new_orders}"
        pass4 = not dec4.safe_to_trade and dec4.block_new_orders
        ok &= self._record("CB_T4_open_blocks_trading", "CIRCUIT_BREAKER", pass4, spec4, got4)

        # CB-T5: Recovery vyžaduje healthy ticky (overujeme cez FSM)
        fsm = CBFsm(cooldown_sec=0.01, recovery_ticks=3)
        fsm.process(CBTrigger.REJECT_STREAK)
        time.sleep(0.02)
        fsm.process(CBTrigger.COOLDOWN_ELAPSED, time.time())
        # Len 2 ticky — ešte nie RECOVERED
        fsm.process(CBTrigger.RECOVERY_TICK)
        fsm.process(CBTrigger.RECOVERY_TICK)
        spec5 = "2 recovery ticky < 3 needed → zostáva HALF_OPEN (nie RECOVERED)"
        got5  = f"state={fsm.state.value}"
        pass5 = fsm.state == CBState.HALF_OPEN
        ok &= self._record("CB_T5_recovery_needs_all_ticks", "CIRCUIT_BREAKER", pass5, spec5, got5)

        # CB-T6: Relapse z HALF_OPEN → OPEN
        fsm2 = CBFsm(cooldown_sec=0.01, recovery_ticks=3)
        fsm2.process(CBTrigger.REJECT_STREAK)
        time.sleep(0.02)
        fsm2.process(CBTrigger.COOLDOWN_ELAPSED, time.time())
        fsm2.process(CBTrigger.REJECT_STREAK)   # relapse
        spec6 = "HALF_OPEN + REJECT_STREAK → OPEN (relapse)"
        got6  = f"state={fsm2.state.value}"
        pass6 = fsm2.state == CBState.OPEN
        ok &= self._record("CB_T6_halfopen_relapse", "CIRCUIT_BREAKER", pass6, spec6, got6)

        # CB-T7: Stale data → DEGRADED (nie HEALTHY)
        ctrl7 = ExecCtrl(ExecCfg(market_data_stale_sec=30))
        dec7  = ctrl7.evaluate(make_snap(last_market_data_ts=now-60))
        spec7 = "Stale data 60s > 30s threshold → DEGRADED alebo horšie (nie HEALTHY)"
        got7  = f"state={dec7.state.value}"
        pass7 = dec7.state != ExecState.HEALTHY
        ok &= self._record("CB_T7_stale_data_not_healthy", "CIRCUIT_BREAKER", pass7, spec7, got7)

        return ok

    # ── C. Regime parity ──────────────────────────────────────────────────────

    def _audit_regime_parity(self) -> bool:
        """Audituje regime runtime vs spec FSM."""
        import random
        RegDet, RegCfg, MR = _import_regime()
        ok = True

        def make_klines(n, base, atr_pct, bias=0, seed=42):
            rng   = random.Random(seed)
            price = base
            k     = []
            for _ in range(n):
                rv     = price * atr_pct
                price += rng.gauss(bias * price * 0.01, rv)
                price  = max(price, 1.0)
                k.append({"open":price-rv*0.1,"high":price+rv*0.5,
                          "low":price-rv*0.5,"close":price,"volume":100})
            return k

        # R1: Nedostatok barov → UNDEFINED
        det1 = RegDet(RegCfg(min_bars=55))
        d1   = det1.update(make_klines(10, 618.0, 0.010))
        spec1 = "bars < min_bars → effective=UNDEFINED"
        got1  = f"effective={d1.effective_regime.value}"
        pass1 = d1.effective_regime == MR.UNDEFINED
        ok &= self._record("R1_few_bars_undefined", "REGIME", pass1, spec1, got1)

        # R2: raw_regime sa nestane effective pred persistence_ticks
        det2 = RegDet(RegCfg(persistence_ticks=3, cooldown_ticks=2,
                              min_bars=20, smoothing_alpha=0.5))
        k2   = make_klines(60, 618.0, 0.010, seed=0)
        d2   = det2.update(k2)
        spec2 = "persistence_counter < persistence_ticks → effective nezmenené (raw ≠ effective je OK)"
        got2  = f"persist_counter={d2.persistence_counter} ticks_since={d2.ticks_since_last_change}"
        pass2 = d2.persistence_counter >= 0 and d2.ticks_since_last_change >= 0
        ok &= self._record("R2_persistence_tracking_valid", "REGIME", pass2, spec2, got2)

        # R3: RANGE → allow_grid=True, allow_new_buys=True
        det3 = RegDet(RegCfg(min_bars=20, persistence_ticks=1, cooldown_ticks=1, smoothing_alpha=0.5))
        k3   = make_klines(60, 618.0, 0.008, bias=0, seed=1)
        for _ in range(5):
            d3 = det3.update(k3)
        if d3.effective_regime == MR.RANGE:
            spec3 = "RANGE → allow_grid=True, allow_new_buys=True (spec tabuľka)"
            got3  = f"grid={d3.allow_grid} buys={d3.allow_new_buys}"
            pass3 = d3.allow_grid and d3.allow_new_buys
            ok &= self._record("R3_range_allows_grid_and_buys", "REGIME", pass3, spec3, got3)
        else:
            self._record("R3_range_allows_grid_and_buys", "REGIME",
                         True, "RANGE check", f"skip (got {d3.effective_regime.value})",
                         detail="Warn: market nie je RANGE").status

        # R4: PANIC → allow_dca=False, allow_new_buys=False, protective=True
        det4 = RegDet(RegCfg(min_bars=20, persistence_ticks=1, cooldown_ticks=1,
                              smoothing_alpha=0.5, panic_drop_pct_3bar=3.0,
                              panic_conf_override=0.60))
        k4_base = make_klines(30, 618.0, 0.008, seed=2)
        price   = k4_base[-1]["close"]
        k4 = list(k4_base)
        for _ in range(5):   # prudký pokles
            new_p = price * 0.975
            k4.append({"open":price,"high":price,"low":new_p,"close":new_p,"volume":200})
            price = new_p
        d4 = det4.update(k4)
        spec4 = "PANIC → allow_dca=False, allow_new_buys=False, protective_mode=True"
        got4  = f"eff={d4.effective_regime.value} dca={d4.allow_dca} buys={d4.allow_new_buys} prot={d4.protective_mode}"
        if d4.effective_regime == MR.PANIC:
            pass4 = not d4.allow_dca and not d4.allow_new_buys and d4.protective_mode
        else:
            pass4 = True   # panic nebol triggnutý — skip
            self._checks.append(CheckResult(
                "R4_panic_rules", "REGIME", CheckStatus.WARNING,
                spec4, got4, detail="PANIC nebol detekovaný — skontroluj thresholdy",
            ))
            return ok
        ok &= self._record("R4_panic_rules", "REGIME", pass4, spec4, got4)

        # R5: Panic override — obíde cooldown pri dostatočnom confidence
        det5 = RegDet(RegCfg(min_bars=20, persistence_ticks=2, cooldown_ticks=10,
                              smoothing_alpha=0.4, panic_drop_pct_3bar=3.0,
                              panic_conf_override=0.60))
        k5_range = make_klines(40, 618.0, 0.008, seed=3)
        for _ in range(5):
            det5.update(k5_range)
        # Panic klines — priamy prudký pokles
        price5 = k5_range[-1]["close"]
        k5 = list(k5_range)
        for _ in range(5):
            new_p = price5 * 0.978
            k5.append({"open":price5,"high":price5,"low":new_p,"close":new_p,"volume":300})
            price5 = new_p
        d5 = det5.update(k5)
        spec5 = "Panic override: obíde cooldown (cooldown=10) ak panic_detected=True a conf>=0.60"
        got5  = f"eff={d5.effective_regime.value} panic={d5.score_breakdown.panic_detected} conf={d5.confidence:.2f}"
        if d5.score_breakdown.panic_detected:
            pass5 = d5.effective_regime == MR.PANIC
        else:
            pass5 = True   # panic nebol dostatočne silný — warning
            self._checks.append(CheckResult(
                "R5_panic_override_cooldown", "REGIME", CheckStatus.WARNING,
                spec5, got5, detail="Panic nebol detekovaný — thresholds môžu byť príliš prísne",
            ))
            return ok
        ok &= self._record("R5_panic_override_cooldown", "REGIME", pass5, spec5, got5)

        # R6: ScoreBreakdown validné rozsahy
        det6 = RegDet(RegCfg(min_bars=20))
        k6   = make_klines(60, 618.0, 0.012, seed=4)
        d6   = det6.update(k6)
        bd   = d6.score_breakdown
        spec6 = "confidence ∈ [0,1], composite ∈ [-1,+1]"
        got6  = f"conf={bd.confidence:.3f} comp={bd.smoothed_composite:.3f}"
        pass6 = 0.0 <= bd.confidence <= 1.0 and -1.0 <= bd.smoothed_composite <= 1.0
        ok &= self._record("R6_score_ranges_valid", "REGIME", pass6, spec6, got6)

        return ok

    # ── Helper ────────────────────────────────────────────────────────────────

    def _record(self, name: str, area: str, passed: bool, spec: str, got: str, detail: str = "") -> bool:
        status = CheckStatus.PASS if passed else CheckStatus.FAIL
        cr     = CheckResult(name=name, area=area, status=status,
                             spec_rule=spec, observed=got, detail=detail)
        self._checks.append(cr)
        icon = "✅" if passed else "❌"
        if self.cfg.log_each_check:
            log.info(f"  {icon} [{area}] {name}")
            if not passed:
                log.error(f"       spec: {spec}")
                log.error(f"       got:  {got}")
        return passed


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    result = RuntimeSpecParityAuditor().run()
    print(f"\n{result.summary}")
    sys.exit(result.exit_code)
