"""
APEX BOT — Standalone Pre-flight Check
========================================
Spusti toto pred každým deployment:
    python preflight_check.py

Exit 0 = OK, Exit 1 = zablokuj deployment
"""
import sys, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

from step17_parity_audit import RuntimeSpecParityAuditor, ParityAuditConfig
from step16_system_spec  import InvariantTestSuite
from step18_e2e_scenarios import ScenarioRunner, ScenarioConfig
from step19_decision_audit import PaperTradingGate

print("═"*60)
print("  APEX BOT — Pre-flight Check")
print("═"*60)

gate = PaperTradingGate()

print("\n[1/3] Parity Audit...")
parity = RuntimeSpecParityAuditor(ParityAuditConfig(log_each_check=True)).run()
gate.check_parity(parity)

print("\n[2/3] Invariant Tests...")
suite   = InvariantTestSuite()
inv_res = suite.run_all()
gate.check_invariants(inv_res)

print("\n[3/3] E2E Scenarios...")
runner  = ScenarioRunner(ScenarioConfig(log_details=True))
e2e_res = runner.run_all()
gate.check_e2e(e2e_res)

passed, report = gate.evaluate()
print(report)
sys.exit(0 if passed else 1)
