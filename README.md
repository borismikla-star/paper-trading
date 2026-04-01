# APEX BOT — Paper Trading

Systematický grid trading bot pre Binance Spot s plnou risk & decision vrstvou.

## Štruktúra projektu

```
apex_bot/
├── main.py                      ← VSTUPNÝ BOD (spúšťaš toto)
│
├── Jadro systému
│   ├── step1_core.py            ← Binance, PaperTracker, symbol info
│   ├── step2_grid_engine.py     ← Grid výpočet, fill logika
│   ├── step3_custom_logic.py    ← RSI, ATR, DCA, stop-loss
│   ├── step7_position_sizing.py ← Risk-based sizing
│   └── step8_volatility_scaling.py ← Volatility režimy
│
├── Risk & Decision vrstvy
│   ├── step10_market_regime_v2.py  ← Regime detection (persistence+cooldown)
│   ├── step11_portfolio_risk.py    ← Portfolio-level risk
│   ├── step12_inventory_risk.py    ← Inventory management
│   ├── step13_execution_safety_v2.py ← Execution safety + CB
│   └── step15_decision_engine.py  ← Central decision gate
│
├── Verifikácia & Audit
│   ├── step16_system_spec.py    ← Spec, FSMs, 33 invariant testov
│   ├── step17_parity_audit.py   ← Runtime vs spec parity
│   ├── step18_e2e_scenarios.py  ← 10 E2E scenárov
│   └── step19_decision_audit.py ← Audit logging + session summary
│
├── requirements.txt
├── runtime.txt                  ← Python 3.11
├── Procfile                     ← Railway: worker: python main.py
├── .env.example                 ← Skopíruj ako .env
└── .gitignore
```

---

## Rýchly štart — lokálne

```bash
# 1. Inštalácia
pip install -r requirements.txt

# 2. Konfigurácia
cp .env.example .env
# Otvor .env a vlož Binance API kľúče (READ-ONLY pre paper trading)

# 3. Spustenie (s pre-flight gate)
python main.py

# Alebo bez pre-flight gate (rýchlejší debug start)
SKIP_PREFLIGHT=true python main.py
```

---

## Railway Deployment (5 krokov)

### Krok 1 — GitHub repozitár
```bash
git init
git add .
git commit -m "APEX BOT paper trading"

# Vytvor repozitár na github.com (Private!) a pushni
git remote add origin https://github.com/TVOJE_MENO/apex-bot.git
git push -u origin main
```

### Krok 2 — Railway projekt
1. Choď na [railway.app](https://railway.app) → **New Project**
2. **Deploy from GitHub repo** → vyber `apex-bot`
3. Railway automaticky detekuje `Procfile` → spustí `python main.py`

### Krok 3 — Environment Variables
V Railway dashboarde → tvoj projekt → **Variables → Raw Editor**:

```
BINANCE_API_KEY     = tvoj_key
BINANCE_API_SECRET  = tvoj_secret
SYMBOL              = BNB/USDT
TEST_MODE           = true
GRID_LEVELS         = 8
GRID_STEP_PCT       = 1.2
ORDER_AMOUNT_USDT   = 15
BASE_CAPITAL        = 10000
DAILY_TARGET        = 100
LOOP_INTERVAL_SEC   = 60
TELEGRAM_BOT_TOKEN  = ...
TELEGRAM_CHAT_ID    = ...
SKIP_PREFLIGHT      = false
```

### Krok 4 — Deploy
Railway automaticky nasadí po každom `git push`.

### Krok 5 — Sledovanie
- **Logy**: Railway dashboard → Deployments → Logs
- **Audit**: Súbor `paper_session.jsonl` (v Railway ephemeral storage)
- **Telegram**: Notifikácie pri dôležitých udalostiach

---

## Čo sa spustí pri štarte

```
1. PRE-FLIGHT GATE
   ├── Parity Audit (27 checks)     ← runtime vs spec zhoda
   ├── Invariant Tests (33 tests)   ← garantované invariants
   └── E2E Scenarios (10 scenárov) ← workflow testy

2. PAPER TRADING LOOP (každých 60s)
   ├── Binance market data (read-only)
   ├── Execution Safety check
   ├── Market Regime detection
   ├── Portfolio Risk evaluation
   ├── Inventory Risk evaluation
   ├── Central Decision Engine
   ├── PaperTracker simulácia
   └── Audit logging → paper_session.jsonl
```

---

## Bezpečnostné pravidlá

| Pravidlo | Detail |
|----------|--------|
| API kľúče | NIKDY `git push` `.env` súbor |
| Read-only | Pre paper trading: **len Enable Reading** na Binance API |
| Withdrawals | NIKDY nezapínaj pre akýkoľvek bot |
| test_mode | `TEST_MODE=true` — papierové obchodovanie, nulové riziko |
| Git repozitár | Nastav ako **Private** na GitHub |

---

## Konzervatívne default nastavenia

| Parameter | Hodnota | Dôvod |
|-----------|---------|-------|
| `GRID_LEVELS` | 8 | Vyvážený počet úrovní |
| `GRID_STEP_PCT` | 1.2% | Štandardný krok |
| `ORDER_AMOUNT_USDT` | 15 | Min. notional buffer |
| `STOP_LOSS_PCT` | 8.0% | Maximálna tolerovaná strata |
| `DAILY_TARGET` | 100 USDT | Realistický denný cieľ |
| `LOOP_INTERVAL_SEC` | 60 | 1h klines + 1min tick |

---

## Po paper tradingu — prechod na live

1. Analyzuj `paper_session.jsonl` — identifikuj slabé miesta
2. Zmeň `TEST_MODE=false` v Railway Variables
3. Na Binance API pridaj **Enable Spot & Margin Trading**
4. Nastav IP whitelist na Railway server IP
5. Začni s malou sumou (~$100 ORDER_AMOUNT_USDT)
