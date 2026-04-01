"""
APEX BOT — Paper Trading Main Loop
====================================
Railway deployment entry point.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import traceback
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("apex_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ApexBot")

# ── Konfigurácia ──────────────────────────────────────────────────────────────
SYMBOL            = os.getenv("SYMBOL",            "BNB/USDT")
TEST_MODE         = os.getenv("TEST_MODE",          "true").lower() == "true"
LOOP_INTERVAL_SEC = int(os.getenv("LOOP_INTERVAL_SEC",  "60"))
GRID_LEVELS       = int(os.getenv("GRID_LEVELS",        "8"))
GRID_STEP_PCT     = float(os.getenv("GRID_STEP_PCT",    "1.2"))
ORDER_AMOUNT_USDT = float(os.getenv("ORDER_AMOUNT_USDT","15"))
STOP_LOSS_PCT     = float(os.getenv("STOP_LOSS_PCT",    "8.0"))
DAILY_TARGET_USDT = float(os.getenv("DAILY_TARGET",     "100"))
BASE_CAPITAL      = float(os.getenv("BASE_CAPITAL",     "10000"))
SKIP_PREFLIGHT    = os.getenv("SKIP_PREFLIGHT",    "false").lower() == "true"
SESSION_LOG       = os.getenv("SESSION_LOG",        "paper_session.jsonl")

# ── Globálne importy (raz pri štarte) ────────────────────────────────────────
from step1_core import create_bot_stack, BinanceConnection
from step10_market_regime_v2 import RegimeDetector, RegimeConfig
from step11_portfolio_risk import (
    PortfolioRiskEngine, PortfolioRiskConfig, PortfolioRiskSnapshot
)
from step12_inventory_risk import (
    InventoryRiskManager, InventoryConfig, InventorySnapshot
)
from step13_execution_safety_v2 import (
    ExecutionSafetyController, ExecutionSafetyConfig, ExecutionSnapshot
)
from step15_decision_engine import DecisionEngine, DecisionInputs
from step19_decision_audit import (
    DecisionAuditor, DecisionAuditEntry, TransitionAuditEntry,
    ActionAuditEntry, ActionType, TransitionDomain
)
from dashboard import start_dashboard, update_state
from telegram_notify import (
    notify_startup, notify_heartbeat, notify_regime_change,
    notify_panic, notify_circuit_breaker, notify_shutdown,
)


# ─────────────────────────────────────────────────────────────────────────────
# PRE-FLIGHT GATE
# ─────────────────────────────────────────────────────────────────────────────

def run_preflight() -> bool:
    from step17_parity_audit import RuntimeSpecParityAuditor, ParityAuditConfig
    from step16_system_spec  import InvariantTestSuite
    from step18_e2e_scenarios import ScenarioRunner, ScenarioConfig
    from step19_decision_audit import PaperTradingGate

    log.info("=" * 60)
    log.info("  PRE-FLIGHT GATE")
    log.info("=" * 60)

    gate = PaperTradingGate()

    log.info("[1/3] Parity audit...")
    parity = RuntimeSpecParityAuditor(ParityAuditConfig(log_each_check=False)).run()
    gate.check_parity(parity)
    log.info(f"  {'✅' if parity.all_ok else '❌'} {parity.summary}")

    log.info("[2/3] Invariant tests...")
    inv_res = InvariantTestSuite().run_all()
    gate.check_invariants(inv_res)
    log.info(f"  {'✅' if inv_res['failed']==0 else '❌'} {inv_res['passed']}/{inv_res['total']}")

    log.info("[3/3] E2E scenarios...")
    e2e_res = ScenarioRunner(ScenarioConfig(log_details=False)).run_all()
    gate.check_e2e(e2e_res)
    log.info(f"  {'✅' if e2e_res['all_passed'] else '⚠️'} {e2e_res['passed']}/{len(e2e_res['results'])}")

    passed, report = gate.evaluate()
    log.info(report)
    return passed


# ─────────────────────────────────────────────────────────────────────────────
# PAPER TRADING BOT
# ─────────────────────────────────────────────────────────────────────────────

class PaperTradingBot:

    def __init__(self):
        log.info(f"Inicializujem PaperTradingBot | symbol={SYMBOL} | test_mode={TEST_MODE}")

        self.precision, self.tracker, self.executor = create_bot_stack(
            sym=SYMBOL, mode=TEST_MODE, starting_usdt=BASE_CAPITAL
        )
        self.cfg_sym    = self.precision.cfg
        self.regime_det = RegimeDetector(RegimeConfig(
            min_bars=55, persistence_ticks=3, cooldown_ticks=4,
            smoothing_alpha=0.20, panic_conf_override=0.65,
        ))
        self.port_risk  = PortfolioRiskEngine(PortfolioRiskConfig())
        self.inv_mgr    = InventoryRiskManager(InventoryConfig())
        self.exec_ctrl  = ExecutionSafetyController(ExecutionSafetyConfig())
        self.engine     = DecisionEngine()
        self.auditor    = DecisionAuditor(SESSION_LOG, symbol=SYMBOL)

        # Binance (read-only)
        try:
            self.conn = BinanceConnection(SYMBOL)
            log.info(f"✅ Binance connected | {self.cfg_sym}")
        except Exception as e:
            log.warning(f"Binance offline — demo mode | {e}")
            self.conn = None

        self._tick          = 0
        self._running       = True
        self._last_regime   = "UNDEFINED"
        self._peak_value    = BASE_CAPITAL
        self._last_ws_ts    = time.time()
        self._started_at    = datetime.now()

        # Dashboard
        start_dashboard(port=int(os.getenv("PORT", "8080")))
        notify_startup(SYMBOL, BASE_CAPITAL, "PAPER" if TEST_MODE else "LIVE")

        log.info("✅ PaperTradingBot pripravený")

    def run(self):
        log.info(f"🚀 Paper trading štart | {SYMBOL} | interval={LOOP_INTERVAL_SEC}s")
        while self._running:
            try:
                self._tick += 1
                self._process_tick()
            except KeyboardInterrupt:
                self._shutdown()
                break
            except Exception as e:
                log.error(f"Tick {self._tick} chyba: {e}")
                log.debug(traceback.format_exc())
                time.sleep(10)
            self._sleep(LOOP_INTERVAL_SEC)

    def _process_tick(self):
        now = time.time()

        # 1. Market data
        price, klines = self._fetch_market_data()
        if price is None:
            log.warning(f"Tick {self._tick}: Nedostupné dáta")
            self.auditor.skip_trading(self._tick, "MARKET_DATA_UNAVAILABLE")
            return

        # 2. Execution safety
        exec_snap = ExecutionSnapshot(
            now_ts=now, last_market_data_ts=now - 5,
            last_ws_message_ts=self._last_ws_ts,
            last_rest_ok_ts=now - 5,
            exchange_heartbeat_ok=True,
            open_order_count=0, stale_order_count=0,
            order_reject_streak=0, cancel_streak=0, replace_streak=0,
            actions_last_minute=0, max_actions_per_minute=15,
            desync_detected=False, unknown_order_states=0,
            symbol=self.cfg_sym.binance_symbol,
        )
        exec_dec = self.exec_ctrl.evaluate(exec_snap)

        # 3. Market regime
        regime_dec = self.regime_det.update(klines)
        if regime_dec.regime_changed:
            self.auditor.log_transition(TransitionAuditEntry(
                tick=self._tick,
                domain=TransitionDomain.REGIME.value,
                prev_state=self._last_regime,
                new_state=regime_dec.effective_regime.value,
                trigger="regime_detector",
                confidence=regime_dec.confidence,
                persistence_ticks=regime_dec.persistence_counter,
                cooldown_ticks=regime_dec.ticks_since_last_change,
            ))
            notify_regime_change(
                self._last_regime,
                regime_dec.effective_regime.value,
                regime_dec.confidence,
            )
            if regime_dec.effective_regime.value == "PANIC":
                notify_panic(price, pv - BASE_CAPITAL)
            self._last_regime = regime_dec.effective_regime.value

        # 4. Portfolio risk
        pv = self.tracker.portfolio_value(price)
        if pv > self._peak_value:
            self._peak_value = pv

        port_snap = PortfolioRiskSnapshot(
            portfolio_value=pv, peak_today=self._peak_value,
            peak_rolling=self._peak_value,
            realized_pnl_today=self.tracker.realized_pnl,
            unrealized_pnl=self.tracker.unrealized_pnl(price),
            total_exposure=self.tracker.coin_balance * price,
            max_symbol_exposure=self.tracker.coin_balance * price,
            stop_loss_today=0, stop_loss_this_week=0, dca_today=0,
            market_regime=regime_dec.effective_regime.value,
            volatility_regime="NORMAL",
        )
        port_dec = self.port_risk.evaluate(port_snap)

        # 5. Inventory risk
        inv_snap = InventorySnapshot(
            coin_qty=self.tracker.coin_balance,
            coin_market_value=self.tracker.coin_balance * price,
            portfolio_value=pv,
            avg_buy_price=self.tracker.avg_buy_price,
            current_price=price,
            unrealized_pnl=self.tracker.unrealized_pnl(price),
            market_regime=regime_dec.effective_regime.value,
            volatility_regime="NORMAL",
            recent_buy_count=0, recent_sell_count=0,
        )
        inv_dec = self.inv_mgr.evaluate(inv_snap)

        # 6. Decision engine
        inputs = DecisionInputs(
            exec_safe_to_trade=exec_dec.safe_to_trade,
            exec_block_new_orders=exec_dec.block_new_orders,
            exec_block_requotes=exec_dec.block_requotes,
            exec_trigger_circuit_breaker=exec_dec.trigger_circuit_breaker,
            exec_cancel_stale_orders=exec_dec.cancel_stale_orders,
            exec_state=exec_dec.state.value,
            port_risk_mode=port_dec.mode.value,
            port_allowed_new_orders=port_dec.allowed_new_orders,
            port_allowed_new_buys=port_dec.allowed_new_buys,
            port_allowed_dca=port_dec.allowed_dca,
            port_max_order_mult=port_dec.max_order_size_mult,
            port_force_reduce_inventory=port_dec.force_reduce_inventory,
            port_trading_halt=port_dec.trading_halt,
            regime_effective=regime_dec.effective_regime.value,
            regime_allow_grid=regime_dec.allow_grid,
            regime_allow_new_buys=regime_dec.allow_new_buys,
            regime_allow_dca=regime_dec.allow_dca,
            regime_protective=regime_dec.protective_mode,
            regime_inv_reduction=regime_dec.inventory_reduction_mode,
            regime_confidence=regime_dec.confidence,
            inv_state=inv_dec.inventory_state.value,
            inv_allow_new_buys=inv_dec.allow_new_buys,
            inv_allow_dca=inv_dec.allow_dca,
            inv_force_reduction=inv_dec.force_inventory_reduction,
            inv_buy_size_mult=inv_dec.buy_size_multiplier,
            vol_regime="NORMAL",
            vol_allow_dca=True, vol_allow_new_buys=True,
            vol_order_size_mult=1.0, vol_max_exposure_mult=1.0,
        )
        outcome = self.engine.decide(inputs)

        # 7. Audit + dashboard update
        uptime = int((datetime.now() - self._started_at).total_seconds())
        pnl_usdt = pv - BASE_CAPITAL
        update_state(
            status        = "RUNNING",
            tick          = self._tick,
            price         = price,
            portfolio     = round(pv, 2),
            base_capital  = BASE_CAPITAL,
            pnl_usdt      = round(pnl_usdt, 4),
            pnl_pct       = round(pnl_usdt / BASE_CAPITAL * 100, 3),
            unrealized_pnl= round(self.tracker.unrealized_pnl(price), 4),
            coin_balance  = round(self.tracker.coin_balance, 6),
            regime        = regime_dec.effective_regime.value,
            regime_conf   = round(regime_dec.confidence, 3),
            exec_state    = exec_dec.state.value,
            port_risk     = port_dec.mode.value,
            winner        = outcome.winning_layer.value,
            allow_trading = outcome.allow_trading,
            allow_buys    = outcome.allow_new_buys,
            uptime_sec    = uptime,
            daily_target  = DAILY_TARGET_USDT,
            test_mode     = TEST_MODE,
        )
        self.auditor.log_decision(DecisionAuditEntry(
            tick=self._tick, symbol=SYMBOL,
            exec_state=exec_dec.state.value,
            effective_regime=regime_dec.effective_regime.value,
            regime_confidence=regime_dec.confidence,
            portfolio_risk_mode=port_dec.mode.value,
            inventory_state=inv_dec.inventory_state.value,
            vol_regime="NORMAL", dca_state="NORMAL",
            allow_trading=outcome.allow_trading,
            allow_new_orders=outcome.allow_new_orders,
            allow_new_buys=outcome.allow_new_buys,
            allow_dca=outcome.allow_dca,
            reduce_only_mode=outcome.reduce_only_mode,
            forced_cancel_all=outcome.forced_cancel_all,
            forced_inventory_reduction=outcome.forced_inventory_reduction,
            winning_layer=outcome.winning_layer.value,
            reason_codes=outcome.reason_codes,
            order_size_mult=outcome.order_size_multiplier,
        ))

        # 8. Reakcia
        if exec_dec.trigger_circuit_breaker:
            notify_circuit_breaker()

        # Heartbeat každých 60 tickov (= každú hodinu pri 60s intervale)
        if self._tick % 60 == 0:
            notify_heartbeat(
                tick=self._tick, price=price, pv=pv,
                pnl=pv - BASE_CAPITAL,
                regime=regime_dec.effective_regime.value,
                uptime_min=int((datetime.now() - self._started_at).total_seconds() // 60),
            )

        if not outcome.allow_trading:
            self.auditor.skip_trading(
                self._tick,
                outcome.reason_codes[0] if outcome.reason_codes else "BLOCKED",
                outcome.winning_layer.value,
            )
            log.info(f"Tick {self._tick} | ⏸ {outcome.explanation[:80]}")
            return

        if outcome.allow_new_buys:
            usdt = ORDER_AMOUNT_USDT * outcome.order_size_multiplier
            if usdt >= self.cfg_sym.min_notional:
                qty = self.precision.qty_from_usdt(usdt, price)
                if qty >= self.cfg_sym.min_qty:
                    try:
                        self.executor.buy(price, qty)
                        self.auditor.place_buy(self._tick, price, qty)
                    except ValueError as e:
                        log.debug(f"Buy skip: {e}")
        else:
            self.auditor.suppress_buy(
                self._tick, price,
                outcome.reason_codes[0] if outcome.reason_codes else "SUPPRESSED",
                outcome.winning_layer.value,
            )

        if self._tick % 10 == 0:
            log.info(
                f"Tick {self._tick} | 💰 {price:.4f} | pv={pv:.2f} USDT | "
                f"coin={self.tracker.coin_balance:.4f} | "
                f"uPnL={self.tracker.unrealized_pnl(price):+.2f} | "
                f"regime={regime_dec.effective_regime.value} "
                f"(conf={regime_dec.confidence:.2f}) | "
                f"winner={outcome.winning_layer.value}"
            )

    def _fetch_market_data(self):
        if self.conn is None:
            return self._mock_price(), self._mock_klines()
        try:
            price  = self.conn.get_price()
            klines = self.conn.get_klines(interval="1h", limit=100)
            self._last_ws_ts = time.time()
            return price, klines
        except Exception as e:
            log.warning(f"Market data chyba: {e}")
            return None, None

    def _mock_price(self) -> float:
        import random
        return round(618.42 + random.gauss(0, 618.42 * 0.008), 2)

    def _mock_klines(self) -> list:
        import random
        price, k = 618.0, []
        for _ in range(100):
            rv = price * 0.010
            price += random.gauss(0, rv)
            price = max(price, 1.0)
            k.append({"open": price-rv*0.1, "high": price+rv*0.5,
                       "low": price-rv*0.5, "close": price,
                       "volume": 100, "time": datetime.now()})
        return k

    def _shutdown(self):
        log.info("Uzatváranie session...")
        notify_shutdown(self._tick, self.tracker.portfolio_value(0) - BASE_CAPITAL)
        self.auditor.print_session_summary()
        self.auditor.close()

    def _sleep(self, seconds: int):
        for _ in range(seconds):
            if not self._running:
                break
            time.sleep(1)


# ─────────────────────────────────────────────────────────────────────────────
# VSTUPNÝ BOD
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("═" * 60)
    log.info(f"  APEX BOT — Paper Trading")
    log.info(f"  Symbol: {SYMBOL} | Mode: {'PAPER' if TEST_MODE else '🔴 LIVE'}")
    log.info(f"  Base: {BASE_CAPITAL} USDT | Target: {DAILY_TARGET_USDT} USDT/deň")
    log.info("═" * 60)

    if not SKIP_PREFLIGHT:
        if not run_preflight():
            log.critical("❌ PRE-FLIGHT FAILED — zablokované")
            sys.exit(1)
        log.info("✅ PRE-FLIGHT PASSED — štartujem")
    else:
        log.warning("⚠️  SKIP_PREFLIGHT=true")

    bot = PaperTradingBot()
    bot.run()
