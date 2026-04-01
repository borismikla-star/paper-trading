"""
APEX BOT — Step 18: End-to-End Scenario Test Suite
=====================================================
Testuje celý systém cez realistické multi-layer scenáre.

Každý scenár:
  1. Definuje vstupný stav všetkých modulov
  2. Spustí celý decision pipeline
  3. Validuje výsledky oproti očakávaniu
  4. Reportuje pass/fail s presným reason

10 scenárov pokrývajúcich kľúčové workflow:
  S1.  Healthy range market
  S2.  Breakout down + growing inventory
  S3.  Panic override during cooldown
  S4.  Stale data + open orders
  S5.  Reject streak → circuit breaker → recovery
  S6.  Portfolio pause despite healthy regime
  S7.  Reduce-only conflict with rebalance
  S8.  DCA conflict scenario
  S9.  Desync detected
  S10. Paper trading startup sanity
"""

from __future__ import annotations

import logging
import random
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

log = logging.getLogger("ApexBot.E2E")


class ScenarioStatus(str, Enum):
    PASS    = "PASS"
    FAIL    = "FAIL"
    WARNING = "WARNING"
    ERROR   = "ERROR"


@dataclass
class ScenarioExpected:
    """Očakávané výsledky scenára."""
    allow_trading:            Optional[bool] = None
    allow_new_orders:         Optional[bool] = None
    allow_new_buys:           Optional[bool] = None
    allow_dca:                Optional[bool] = None
    reduce_only_mode:         Optional[bool] = None
    forced_cancel_all:        Optional[bool] = None
    forced_inventory_reduction: Optional[bool] = None
    effective_regime:         Optional[str]  = None   # MarketRegime.value
    exec_state:               Optional[str]  = None   # ExecSafetyState.value
    winning_layer_contains:   Optional[str]  = None   # čiastočná zhoda
    reason_code_contains:     Optional[str]  = None   # čiastočná zhoda
    invariants_ok:            bool           = True


@dataclass
class ScenarioResult:
    scenario_id:  str
    name:         str
    status:       ScenarioStatus
    violations:   list[str] = field(default_factory=list)
    warnings:     list[str] = field(default_factory=list)
    outcome_summary: str = ""
    duration_ms:  float = 0.0

    @property
    def passed(self) -> bool:
        return self.status == ScenarioStatus.PASS


@dataclass
class ScenarioConfig:
    fail_fast:    bool = False   # zastaviť pri prvom zlyhaní
    log_details:  bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Imports
# ─────────────────────────────────────────────────────────────────────────────

def _imports():
    sys.path.insert(0, '/home/claude')
    from step15_decision_engine import DecisionEngine, DecisionInputs
    from step13_execution_safety_v2 import (
        ExecutionSafetyController, ExecutionSafetyConfig, ExecutionSnapshot,
        ExecSafetyState,
    )
    from step10_market_regime_v2 import RegimeDetector, RegimeConfig, MarketRegime
    from step16_system_spec import CircuitBreakerFSM, CBState, CBTrigger
    return (DecisionEngine, DecisionInputs, ExecutionSafetyController,
            ExecutionSafetyConfig, ExecutionSnapshot, ExecSafetyState,
            RegimeDetector, RegimeConfig, MarketRegime,
            CircuitBreakerFSM, CBState, CBTrigger)


# ─────────────────────────────────────────────────────────────────────────────
# Helper builders
# ─────────────────────────────────────────────────────────────────────────────

def _klines(n: int, base: float, atr_pct: float, bias: float = 0, seed: int = 42):
    rng, price, k = random.Random(seed), base, []
    for _ in range(n):
        rv     = price * atr_pct
        price += rng.gauss(bias * price * 0.01, rv)
        price  = max(price, 1.0)
        k.append({"open":price-rv*0.1,"high":price+rv*0.5,
                  "low":price-rv*0.5,"close":price,"volume":100})
    return k


def _exec_snap(now: float, **overrides):
    base = dict(
        now_ts=now, last_market_data_ts=now-5,
        last_ws_message_ts=now-5, last_rest_ok_ts=now-5,
        exchange_heartbeat_ok=True, open_order_count=5,
        stale_order_count=0, order_reject_streak=0,
        cancel_streak=0, replace_streak=0,
        actions_last_minute=2, max_actions_per_minute=15,
        desync_detected=False, unknown_order_states=0,
        symbol="BNBUSDT",
    )
    base.update(overrides)
    return base


def _inp_healthy(**overrides):
    """Zdravý stav — všetky vrstvy OK."""
    base = dict(
        exec_safe_to_trade=True, exec_block_new_orders=False,
        exec_block_requotes=False, exec_trigger_circuit_breaker=False,
        exec_cancel_stale_orders=False, exec_state="HEALTHY",
        port_risk_mode="NORMAL", regime_effective="RANGE",
        regime_allow_grid=True, regime_allow_new_buys=True,
        regime_allow_dca=True, regime_protective=False,
        regime_inv_reduction=False, regime_confidence=0.7,
        inv_allow_new_buys=True, inv_allow_dca=True,
        inv_force_reduction=False, inv_buy_size_mult=1.0,
        inv_state="BALANCED",
        vol_regime="NORMAL", vol_allow_dca=True, vol_allow_new_buys=True,
        vol_order_size_mult=1.0, vol_max_exposure_mult=1.0,
        dca_allow=None,
    )
    base.update(overrides)
    return base


# ─────────────────────────────────────────────────────────────────────────────
# ScenarioRunner
# ─────────────────────────────────────────────────────────────────────────────

class ScenarioRunner:
    """
    Spúšťa E2E scenáre a validuje výsledky.

    Použitie:
        runner = ScenarioRunner()
        report = runner.run_all()
        sys.exit(0 if report["all_passed"] else 1)
    """

    def __init__(self, cfg: Optional[ScenarioConfig] = None):
        self.cfg     = cfg or ScenarioConfig()
        self._results: list[ScenarioResult] = []
        (self.DecisionEngine, self.DecisionInputs, self.ExecCtrl,
         self.ExecCfg, self.ExecSnap, self.ExecState,
         self.RegDet, self.RegCfg, self.MR,
         self.CBFsm, self.CBState, self.CBTrigger) = _imports()

    def run_all(self) -> dict:
        scenarios = [
            self.s01_healthy_range,
            self.s02_breakout_down_inventory,
            self.s03_panic_override_cooldown,
            self.s04_stale_data_open_orders,
            self.s05_reject_streak_cb_recovery,
            self.s06_portfolio_pause_healthy_regime,
            self.s07_reduce_only_rebalance_conflict,
            self.s08_dca_conflict,
            self.s09_desync_detected,
            self.s10_paper_trading_startup,
        ]
        print(f"\n{'═'*64}")
        print(f"  E2E SCENARIO TEST SUITE ({len(scenarios)} scenárov)")
        print(f"{'═'*64}")

        for fn in scenarios:
            import time as _t
            t0 = _t.perf_counter()
            try:
                result = fn()
            except Exception as e:
                result = ScenarioResult(
                    scenario_id="?", name=fn.__name__,
                    status=ScenarioStatus.ERROR,
                    violations=[f"EXCEPTION: {type(e).__name__}: {e}"],
                )
            result.duration_ms = (_t.perf_counter() - t0) * 1000
            self._results.append(result)
            icon = {"PASS":"✅","FAIL":"❌","WARNING":"⚠️","ERROR":"💥"}[result.status.value]
            print(f"  {icon} {result.scenario_id} {result.name} ({result.duration_ms:.1f}ms)")
            if result.violations:
                for v in result.violations:
                    print(f"       ❌ {v}")
            if result.warnings:
                for w in result.warnings:
                    print(f"       ⚠️  {w}")
            if self.cfg.fail_fast and not result.passed:
                break

        passed = sum(1 for r in self._results if r.passed)
        failed = sum(1 for r in self._results if not r.passed)
        print(f"\n{'═'*64}")
        print(f"  VÝSLEDOK: {passed}/{len(self._results)} scenárov prešlo | {failed} zlyhali")
        print(f"{'═'*64}\n")
        return {"passed": passed, "failed": failed, "all_passed": failed == 0,
                "results": self._results}

    # ── Validačný helper ──────────────────────────────────────────────────────

    def _validate(
        self, sid: str, name: str,
        out, exec_dec, regime_dec,
        expected: ScenarioExpected,
        engine,
    ) -> ScenarioResult:
        violations, warnings = [], []

        def check(cond: bool, msg: str, is_warning: bool = False):
            if not cond:
                (warnings if is_warning else violations).append(msg)

        from step15_decision_engine import DecisionInputs as DI

        if expected.allow_trading is not None:
            check(out.allow_trading == expected.allow_trading,
                  f"allow_trading: expected={expected.allow_trading} got={out.allow_trading}")
        if expected.allow_new_orders is not None:
            check(out.allow_new_orders == expected.allow_new_orders,
                  f"allow_new_orders: expected={expected.allow_new_orders} got={out.allow_new_orders}")
        if expected.allow_new_buys is not None:
            check(out.allow_new_buys == expected.allow_new_buys,
                  f"allow_new_buys: expected={expected.allow_new_buys} got={out.allow_new_buys}")
        if expected.allow_dca is not None:
            check(out.allow_dca == expected.allow_dca,
                  f"allow_dca: expected={expected.allow_dca} got={out.allow_dca}")
        if expected.reduce_only_mode is not None:
            check(out.reduce_only_mode == expected.reduce_only_mode,
                  f"reduce_only: expected={expected.reduce_only_mode} got={out.reduce_only_mode}")
        if expected.forced_cancel_all is not None:
            check(out.forced_cancel_all == expected.forced_cancel_all,
                  f"forced_cancel_all: expected={expected.forced_cancel_all} got={out.forced_cancel_all}")
        if expected.forced_inventory_reduction is not None:
            check(out.forced_inventory_reduction == expected.forced_inventory_reduction,
                  f"forced_inv_red: expected={expected.forced_inventory_reduction} got={out.forced_inventory_reduction}")
        if expected.effective_regime is not None and regime_dec is not None:
            check(regime_dec.effective_regime.value == expected.effective_regime,
                  f"regime: expected={expected.effective_regime} got={regime_dec.effective_regime.value}")
        if expected.exec_state is not None and exec_dec is not None:
            check(exec_dec.state.value == expected.exec_state,
                  f"exec_state: expected={expected.exec_state} got={exec_dec.state.value}")
        if expected.winning_layer_contains is not None:
            check(expected.winning_layer_contains in out.winning_layer.value,
                  f"winning_layer: expected contains '{expected.winning_layer_contains}' got='{out.winning_layer.value}'")
        if expected.reason_code_contains is not None:
            codes_str = " ".join(out.reason_codes)
            check(expected.reason_code_contains in codes_str,
                  f"reason_code: expected '{expected.reason_code_contains}' in {out.reason_codes}")

        # Vždy overíme invariants
        if expected.invariants_ok:
            inv_violations = engine.audit_invariants(out)
            for v in inv_violations:
                violations.append(f"INVARIANT_VIOLATION: {v}")

        status = ScenarioStatus.PASS if not violations else ScenarioStatus.FAIL
        if not violations and warnings:
            status = ScenarioStatus.WARNING

        summary = (
            f"trade={out.allow_trading} orders={out.allow_new_orders} "
            f"buys={out.allow_new_buys} dca={out.allow_dca} "
            f"ro={out.reduce_only_mode} cancel={out.forced_cancel_all} "
            f"winner={out.winning_layer.value}"
        )
        return ScenarioResult(sid, name, status, violations, warnings, summary)

    # ── S1: Healthy range market ───────────────────────────────────────────────

    def s01_healthy_range(self) -> ScenarioResult:
        sid, name = "S01", "Healthy range market"
        engine     = self.DecisionEngine()
        now        = time.time()
        exec_ctrl  = self.ExecCtrl(self.ExecCfg())
        exec_dec   = exec_ctrl.evaluate(self.ExecSnap(**_exec_snap(now)))

        inp = self.DecisionInputs(**_inp_healthy(
            exec_safe_to_trade=exec_dec.safe_to_trade,
            exec_block_new_orders=exec_dec.block_new_orders,
            exec_block_requotes=exec_dec.block_requotes,
            exec_trigger_circuit_breaker=exec_dec.trigger_circuit_breaker,
            exec_cancel_stale_orders=exec_dec.cancel_stale_orders,
            exec_state=exec_dec.state.value,
        ))
        out = engine.decide(inp)

        return self._validate(sid, name, out, exec_dec, None,
            ScenarioExpected(
                allow_trading=True, allow_new_orders=True,
                allow_new_buys=True,
                winning_layer_contains="DEFAULT",
            ), engine)

    # ── S2: Breakout down + growing inventory ─────────────────────────────────

    def s02_breakout_down_inventory(self) -> ScenarioResult:
        sid, name = "S02", "Breakout down + growing inventory"
        engine    = self.DecisionEngine()
        now       = time.time()
        exec_ctrl = self.ExecCtrl(self.ExecCfg())
        exec_dec  = exec_ctrl.evaluate(self.ExecSnap(**_exec_snap(now)))

        inp = self.DecisionInputs(**_inp_healthy(
            exec_safe_to_trade=exec_dec.safe_to_trade,
            exec_block_new_orders=exec_dec.block_new_orders,
            exec_block_requotes=exec_dec.block_requotes,
            exec_trigger_circuit_breaker=exec_dec.trigger_circuit_breaker,
            exec_cancel_stale_orders=exec_dec.cancel_stale_orders,
            exec_state=exec_dec.state.value,
            regime_effective="BREAKOUT_DOWN",
            regime_allow_grid=False, regime_allow_new_buys=False,
            regime_allow_dca=False, regime_protective=True,
            regime_inv_reduction=True, regime_confidence=0.8,
            inv_state="REDUCE_ONLY", inv_allow_new_buys=False,
            inv_allow_dca=False, inv_force_reduction=True,
            inv_buy_size_mult=0.0,
        ))
        out = engine.decide(inp)

        return self._validate(sid, name, out, exec_dec, None,
            ScenarioExpected(
                allow_new_buys=False, allow_dca=False,
                forced_inventory_reduction=True,
            ), engine)

    # ── S3: Panic override during cooldown ────────────────────────────────────

    def s03_panic_override_cooldown(self) -> ScenarioResult:
        sid, name = "S03", "Panic override during cooldown"
        det = self.RegDet(self.RegCfg(
            min_bars=20, persistence_ticks=1, cooldown_ticks=20,
            smoothing_alpha=0.5, panic_drop_pct_3bar=3.0, panic_conf_override=0.60,
        ))
        # Establish initial RANGE state
        k_range = _klines(40, 618.0, 0.008, seed=5)
        for _ in range(4):
            det.update(k_range)
        # Prudký pokles — panic override
        price = k_range[-1]["close"]
        k_panic = list(k_range)
        for _ in range(5):
            new_p = price * 0.977
            k_panic.append({"open":price,"high":price,"low":new_p,"close":new_p,"volume":200})
            price = new_p
        d = det.update(k_panic)

        engine = self.DecisionEngine()
        inp    = self.DecisionInputs(**_inp_healthy(
            regime_effective=d.effective_regime.value,
            regime_allow_grid=d.allow_grid, regime_allow_new_buys=d.allow_new_buys,
            regime_allow_dca=d.allow_dca, regime_protective=d.protective_mode,
            regime_inv_reduction=d.inventory_reduction_mode,
            regime_confidence=d.confidence,
        ))
        out = engine.decide(inp)

        if d.score_breakdown.panic_detected:
            expected = ScenarioExpected(
                allow_dca=False, allow_new_buys=False,
                effective_regime="PANIC",
            )
        else:
            # Panic nebol dostatočne silný — warning
            return ScenarioResult(sid, name, ScenarioStatus.WARNING,
                warnings=["Panic nebol detekovaný — thresholds môžu byť príliš prísne"])

        return self._validate(sid, name, out, None, d, expected, engine)

    # ── S4: Stale data + open orders ─────────────────────────────────────────

    def s04_stale_data_open_orders(self) -> ScenarioResult:
        sid, name  = "S04", "Stale data + open orders"
        now        = time.time()
        exec_ctrl  = self.ExecCtrl(self.ExecCfg(market_data_stale_sec=30))
        snap       = self.ExecSnap(**_exec_snap(
            now, last_market_data_ts=now-90,   # stale 90s
            open_order_count=8, stale_order_count=3,
        ))
        exec_dec   = exec_ctrl.evaluate(snap)
        engine     = self.DecisionEngine()
        inp        = self.DecisionInputs(**_inp_healthy(
            exec_safe_to_trade=exec_dec.safe_to_trade,
            exec_block_new_orders=exec_dec.block_new_orders,
            exec_block_requotes=exec_dec.block_requotes,
            exec_trigger_circuit_breaker=exec_dec.trigger_circuit_breaker,
            exec_cancel_stale_orders=exec_dec.cancel_stale_orders,
            exec_state=exec_dec.state.value,
        ))
        out = engine.decide(inp)

        # DEGRADED (nie UNSAFE): trading môže byť True ale orders False
        # Spec: stale data → orders blokované, trading môže pokračovať (DEGRADED policy)
        return self._validate(sid, name, out, exec_dec, None,
            ScenarioExpected(
                allow_new_orders=False,
                winning_layer_contains="EXECUTION",
            ), engine)

    # ── S5: Reject streak → circuit breaker → recovery ───────────────────────

    def s05_reject_streak_cb_recovery(self) -> ScenarioResult:
        sid, name   = "S05", "Reject streak → CB → recovery"
        violations, warnings = [], []
        now         = time.time()
        exec_ctrl   = self.ExecCtrl(self.ExecCfg(reject_streak_cb=5, cb_cooldown_sec=0.05,
                                                   cb_recovery_ticks=2))
        engine      = self.DecisionEngine()

        # Fáza 1: Trip CB
        dec_cb = exec_ctrl.evaluate(self.ExecSnap(**_exec_snap(now, order_reject_streak=5)))
        if dec_cb.state.value != "CIRCUIT_BREAKER":
            violations.append(f"CB nebol triggnutý: state={dec_cb.state.value}")

        # Fáza 2: CB aktívny — trading blokovaný
        inp_cb = self.DecisionInputs(**_inp_healthy(
            exec_safe_to_trade=dec_cb.safe_to_trade,
            exec_block_new_orders=dec_cb.block_new_orders,
            exec_block_requotes=dec_cb.block_requotes,
            exec_trigger_circuit_breaker=dec_cb.trigger_circuit_breaker,
            exec_cancel_stale_orders=dec_cb.cancel_stale_orders,
            exec_state=dec_cb.state.value,
        ))
        out_cb = engine.decide(inp_cb)
        if out_cb.allow_trading:
            violations.append(f"Trading povolený počas CB: allow_trading={out_cb.allow_trading}")

        # Fáza 3: Čakaj na cooldown + zdravé ticky
        time.sleep(0.1)
        for _ in range(3):
            dec_rec = exec_ctrl.evaluate(self.ExecSnap(**_exec_snap(time.time(),
                order_reject_streak=0)))
        # Po recovery by mal byť healthier stav
        if dec_rec.state.value == "CIRCUIT_BREAKER":
            warnings.append(f"CB stale aktívny po recovery tickoch: {dec_rec.state.value}")

        inv_violations = engine.audit_invariants(out_cb)
        violations.extend([f"INV: {v}" for v in inv_violations])

        status = ScenarioStatus.PASS if not violations else ScenarioStatus.FAIL
        if not violations and warnings:
            status = ScenarioStatus.WARNING
        return ScenarioResult(sid, name, status, violations, warnings,
                              f"cb_state={dec_cb.state.value} recovery_state={dec_rec.state.value}")

    # ── S6: Portfolio pause despite healthy regime ────────────────────────────

    def s06_portfolio_pause_healthy_regime(self) -> ScenarioResult:
        sid, name = "S06", "Portfolio pause despite healthy regime"
        engine    = self.DecisionEngine()
        inp       = self.DecisionInputs(**_inp_healthy(
            port_risk_mode="PAUSE_NEW_RISK",
            regime_effective="RANGE", regime_allow_new_buys=True,
        ))
        out = engine.decide(inp)

        return self._validate(sid, name, out, None, None,
            ScenarioExpected(
                allow_trading=True,
                allow_new_orders=False, allow_new_buys=False,
                allow_new_risk_implied=False,
                winning_layer_contains="PORTFOLIO",
            ), engine)

    def s06_portfolio_pause_healthy_regime(self) -> ScenarioResult:
        sid, name = "S06", "Portfolio pause despite healthy regime"
        engine    = self.DecisionEngine()
        inp       = self.DecisionInputs(**_inp_healthy(
            port_risk_mode="PAUSE_NEW_RISK",
            regime_effective="RANGE", regime_allow_new_buys=True,
        ))
        out = engine.decide(inp)
        return self._validate(sid, name, out, None, None,
            ScenarioExpected(
                allow_trading=True, allow_new_orders=False, allow_new_buys=False,
                winning_layer_contains="PORTFOLIO",
            ), engine)

    # ── S7: Reduce-only conflict with rebalance ───────────────────────────────

    def s07_reduce_only_rebalance_conflict(self) -> ScenarioResult:
        sid, name = "S07", "Reduce-only conflict with rebalance"
        engine    = self.DecisionEngine()
        inp       = self.DecisionInputs(**_inp_healthy(
            inv_state="REDUCE_ONLY", inv_allow_new_buys=False,
            inv_allow_dca=False, inv_force_reduction=True, inv_buy_size_mult=0.0,
            rebalance_type="FULL_REBUILD", rebalance_cancel_orders=True,
            # Exec blokuje requotes (rebalance potrebuje cancel+replace)
            exec_block_requotes=True,
        ))
        out = engine.decide(inp)

        return self._validate(sid, name, out, None, None,
            ScenarioExpected(
                allow_new_buys=False, allow_dca=False,
                forced_inventory_reduction=True,
            ), engine)

    # ── S8: DCA conflict scenario ─────────────────────────────────────────────

    def s08_dca_conflict(self) -> ScenarioResult:
        sid, name = "S08", "DCA conflict — guardrails block despite vol/regime OK"
        engine    = self.DecisionEngine()
        inp       = self.DecisionInputs(**_inp_healthy(
            regime_effective="RANGE", regime_allow_dca=True,
            vol_regime="NORMAL", vol_allow_dca=True,
            dca_allow=False, dca_state="DISABLED",   # DCA guardrails blokujú
        ))
        out = engine.decide(inp)

        return self._validate(sid, name, out, None, None,
            ScenarioExpected(
                allow_trading=True, allow_new_buys=True,
                allow_dca=False,
                winning_layer_contains="DCA",
            ), engine)

    # ── S9: Desync detected ───────────────────────────────────────────────────

    def s09_desync_detected(self) -> ScenarioResult:
        sid, name  = "S09", "Desync detected — unsafe mode"
        now        = time.time()
        exec_ctrl  = self.ExecCtrl(self.ExecCfg())
        exec_dec   = exec_ctrl.evaluate(self.ExecSnap(**_exec_snap(
            now, desync_detected=True,
        )))
        engine     = self.DecisionEngine()
        inp        = self.DecisionInputs(**_inp_healthy(
            exec_safe_to_trade=exec_dec.safe_to_trade,
            exec_block_new_orders=exec_dec.block_new_orders,
            exec_block_requotes=exec_dec.block_requotes,
            exec_trigger_circuit_breaker=exec_dec.trigger_circuit_breaker,
            exec_cancel_stale_orders=exec_dec.cancel_stale_orders,
            exec_state=exec_dec.state.value,
        ))
        out = engine.decide(inp)

        return self._validate(sid, name, out, exec_dec, None,
            ScenarioExpected(
                allow_trading=False, allow_new_orders=False,
                winning_layer_contains="EXECUTION",
            ), engine)

    # ── S10: Paper trading startup sanity ─────────────────────────────────────

    def s10_paper_trading_startup(self) -> ScenarioResult:
        sid, name  = "S10", "Paper trading startup sanity"
        violations, warnings = [], []

        # Cold start: fresh data, healthy exec, enough klines
        det       = self.RegDet(self.RegCfg(min_bars=20, persistence_ticks=1,
                                             cooldown_ticks=1, smoothing_alpha=0.5))
        k_init    = _klines(60, 618.0, 0.010, seed=7)
        for _ in range(3):
            d_init = det.update(k_init)

        now       = time.time()
        exec_ctrl = self.ExecCtrl(self.ExecCfg())
        exec_dec  = exec_ctrl.evaluate(self.ExecSnap(**_exec_snap(now)))
        engine    = self.DecisionEngine()
        inp       = self.DecisionInputs(**_inp_healthy(
            exec_safe_to_trade=exec_dec.safe_to_trade,
            exec_block_new_orders=exec_dec.block_new_orders,
            exec_block_requotes=exec_dec.block_requotes,
            exec_trigger_circuit_breaker=exec_dec.trigger_circuit_breaker,
            exec_cancel_stale_orders=exec_dec.cancel_stale_orders,
            exec_state=exec_dec.state.value,
            regime_effective=d_init.effective_regime.value,
            regime_allow_grid=d_init.allow_grid,
            regime_allow_new_buys=d_init.allow_new_buys,
            regime_allow_dca=d_init.allow_dca,
            regime_protective=d_init.protective_mode,
            regime_inv_reduction=d_init.inventory_reduction_mode,
            regime_confidence=d_init.confidence,
        ))
        out = engine.decide(inp)

        # Validácia: žiadne nevalidné prechodové stavy
        inv_violations = engine.audit_invariants(out)
        violations.extend([f"INV: {v}" for v in inv_violations])

        # Startup nesmie byť v totálnom halte bez dôvodu
        if not out.allow_trading and exec_dec.state.value == "HEALTHY":
            violations.append(
                f"Startup: allow_trading=False napriek HEALTHY exec a OK dátam "
                f"(reason: {out.reason_codes})"
            )

        # Regime by nemal byť UNDEFINED ak máme dosť barov
        if d_init.effective_regime.value == "UNDEFINED":
            warnings.append(f"Startup regime=UNDEFINED napriek {len(k_init)} barom")

        status = ScenarioStatus.PASS if not violations else ScenarioStatus.FAIL
        if not violations and warnings:
            status = ScenarioStatus.WARNING
        summary = (
            f"exec={exec_dec.state.value} regime={d_init.effective_regime.value} "
            f"trade={out.allow_trading} buys={out.allow_new_buys}"
        )
        return ScenarioResult(sid, name, status, violations, warnings, summary)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    runner = ScenarioRunner(ScenarioConfig(log_details=True))
    report = runner.run_all()
    sys.exit(0 if report["all_passed"] else 1)
