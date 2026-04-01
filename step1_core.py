"""
APEX BOT — Step 1: Core & Binance Connection  (v3 — simulačný režim)
======================================================================

╔══════════════════════════════════════════════════════════════════╗
║  Dve globálne premenné na vrchu — zmeň čokoľvek tu:            ║
║                                                                  ║
║      symbol    = "BNB/USDT"   →  ľubovoľný pár                 ║
║      test_mode = True         →  False = ostrý live účet        ║
║                                                                  ║
║  Pri test_mode = True:                                           ║
║    • Žiadne API volania na Binance                               ║
║    • Každý obchod sa vypíše ako [SIMULÁCIA] do konzoly          ║
║    • PaperTracker sleduje fiktívny zostatok a P&L               ║
╚══════════════════════════════════════════════════════════════════╝

Požiadavky:
    pip install python-binance python-dotenv
"""

import os
import logging
from decimal import Decimal, ROUND_DOWN
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# ════════════════════════════════════════════════════════════════
#  ★  KONFIGURÁCIA — zmeň tu, nič iné netreba  ★
# ════════════════════════════════════════════════════════════════

symbol    = "BNB/USDT"   # pár:      "BNB/USDT" | "SOL/USDT" | "ETH/USDT" | "BTC/USDT"
test_mode = True          # simulácia: True = papierové | False = ostrý live účet

# ════════════════════════════════════════════════════════════════
#  Interná konfigurácia z .env (API kľúče)
# ════════════════════════════════════════════════════════════════

API_KEY    = os.getenv("BINANCE_API_KEY",    "")
API_SECRET = os.getenv("BINANCE_API_SECRET", "")

# ── Logger ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("apex_bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("ApexBot")


# ════════════════════════════════════════════════════════════════
#  Pomocné konverzie symbolov
# ════════════════════════════════════════════════════════════════

def to_binance_symbol(sym: str) -> str:
    """'BNB/USDT' → 'BNBUSDT'"""
    return sym.replace("/", "").upper()

def to_ccxt_symbol(sym: str) -> str:
    """'BNBUSDT' → 'BNB/USDT'"""
    if "/" in sym:
        return sym.upper()
    for quote in ("USDT", "BTC", "ETH", "BNB", "BUSD"):
        if sym.endswith(quote):
            return f"{sym[:-len(quote)]}/{quote}"
    return sym

def parse_assets(sym: str) -> tuple[str, str]:
    """'BNB/USDT' → ('BNB', 'USDT')"""
    ccxt  = to_ccxt_symbol(sym)
    parts = ccxt.split("/")
    return (parts[0], parts[1]) if len(parts) == 2 else ("UNKNOWN", "USDT")


# ════════════════════════════════════════════════════════════════
#  SymbolConfig — pravidlá páru načítané z Binance
# ════════════════════════════════════════════════════════════════

@dataclass
class SymbolConfig:
    ccxt_symbol:    str
    binance_symbol: str
    base_asset:     str
    quote_asset:    str
    tick_size:      Decimal
    price_decimals: int
    step_size:      Decimal
    qty_decimals:   int
    min_qty:        float
    max_qty:        float
    min_notional:   float
    maker_fee:      float = 0.001
    taker_fee:      float = 0.001

    def __str__(self) -> str:
        return (
            f"{self.ccxt_symbol} | tick={self.tick_size} ({self.price_decimals}d) | "
            f"step={self.step_size} ({self.qty_decimals}d) | "
            f"min_qty={self.min_qty} | min_notional={self.min_notional} USDT"
        )

    @staticmethod
    def _decimals(s: Decimal) -> int:
        return max(0, -s.as_tuple().exponent)

    @classmethod
    def from_binance_info(cls, raw_info: dict, ccxt_sym: str) -> "SymbolConfig":
        base, quote  = parse_assets(ccxt_sym)
        tick_size    = Decimal("0.01")
        step_size    = Decimal("0.01")
        min_qty      = 0.01
        max_qty      = 9_000_000.0
        min_notional = 5.0
        for f in raw_info.get("filters", []):
            ft = f["filterType"]
            if ft == "PRICE_FILTER":
                tick_size = Decimal(f["tickSize"]).normalize()
            elif ft == "LOT_SIZE":
                step_size = Decimal(f["stepSize"]).normalize()
                min_qty   = float(f["minQty"])
                max_qty   = float(f["maxQty"])
            elif ft in ("MIN_NOTIONAL", "NOTIONAL"):
                min_notional = float(f.get("minNotional", min_notional))
        return cls(
            ccxt_symbol=ccxt_sym.upper(), binance_symbol=to_binance_symbol(ccxt_sym),
            base_asset=base, quote_asset=quote,
            tick_size=tick_size, price_decimals=cls._decimals(tick_size),
            step_size=step_size, qty_decimals=cls._decimals(step_size),
            min_qty=min_qty, max_qty=max_qty, min_notional=min_notional,
        )


# ════════════════════════════════════════════════════════════════
#  PrecisionManager — zaokrúhľovanie ceny a qty
# ════════════════════════════════════════════════════════════════

class PrecisionManager:
    def __init__(self, cfg: SymbolConfig):
        self.cfg = cfg

    def floor_price(self, price: float) -> float:
        d = Decimal(str(price))
        return float((d / self.cfg.tick_size).to_integral_value(ROUND_DOWN) * self.cfg.tick_size)

    def floor_qty(self, qty: float) -> float:
        d = Decimal(str(qty))
        return float((d / self.cfg.step_size).to_integral_value(ROUND_DOWN) * self.cfg.step_size)

    def qty_from_usdt(self, usdt: float, price: float) -> float:
        if price <= 0:
            raise ValueError(f"Cena musí byť > 0, dostali sme: {price}")
        return self.floor_qty(usdt / price)

    def fmt_price(self, price: float) -> str:
        return f"{self.floor_price(price):.{self.cfg.price_decimals}f}"

    def fmt_qty(self, qty: float) -> str:
        return f"{self.floor_qty(qty):.{self.cfg.qty_decimals}f}"

    def validate(self, price: float, qty: float) -> tuple[bool, str]:
        p, q = self.floor_price(price), self.floor_qty(qty)
        if q < self.cfg.min_qty:
            return False, f"qty {q} < min_qty {self.cfg.min_qty} {self.cfg.base_asset}"
        if q > self.cfg.max_qty:
            return False, f"qty {q} > max_qty {self.cfg.max_qty}"
        if p * q < self.cfg.min_notional:
            return False, f"notional {p*q:.4f} < min_notional {self.cfg.min_notional} USDT"
        return True, ""

    def safe_params(self, price: float, qty: float) -> tuple[str, str]:
        ok, reason = self.validate(price, qty)
        if not ok:
            raise ValueError(f"[{self.cfg.ccxt_symbol}] {reason}")
        return self.fmt_price(price), self.fmt_qty(qty)

    def describe(self) -> str:
        return (
            f"Presnosť [{self.cfg.ccxt_symbol}]\n"
            f"  Cena: tick_size={self.cfg.tick_size} ({self.cfg.price_decimals} des. miest)\n"
            f"  Qty:  step_size={self.cfg.step_size} ({self.cfg.qty_decimals} des. miest)\n"
            f"  Limity: min_qty={self.cfg.min_qty} {self.cfg.base_asset} | "
            f"min_notional={self.cfg.min_notional} {self.cfg.quote_asset}"
        )


# ════════════════════════════════════════════════════════════════
#  PaperTracker — sledovač fiktívneho portfólia
# ════════════════════════════════════════════════════════════════

@dataclass
class Trade:
    """Jeden zaznamenaný obchod v simulácii."""
    trade_id:   int
    side:       str           # "BUY" | "SELL"
    symbol:     str
    price:      float
    amount:     float         # qty v base assete (napr. BNB)
    notional:   float         # hodnota v quote assete (napr. USDT)
    fee:        float         # poplatok v USDT
    timestamp:  datetime
    usdt_after: float         # zostatok USDT po obchode
    coin_after: float         # zostatok coinu po obchode
    pnl:        float         # realizovaný P&L tohto obchodu (len pri SELL)


class PaperTracker:
    """
    Simulátor portfólia pre test_mode = True.

    Sleduje:
      • usdt_balance  — fiktívny zostatok v USDT
      • coin_balance  — fiktívny zostatok v base coinu (napr. BNB)
      • avg_buy_price — priemerná nákupná cena (pre výpočet P&L)
      • Históriu všetkých obchodov

    Výpočty:
      BUY:  usdt_balance -= notional + fee
            coin_balance += amount
            avg_buy_price sa aktualizuje (weighted average)

      SELL: usdt_balance += notional - fee
            coin_balance -= amount
            P&L = (sell_price - avg_buy_price) * amount - fee
    """

    SEPARATOR = "─" * 62

    def __init__(
        self,
        starting_usdt: float = 1000.0,
        taker_fee_pct: float = 0.001,       # 0.1% Binance štandard
        cfg: Optional[SymbolConfig] = None,
    ):
        self.usdt_balance   = starting_usdt
        self.starting_usdt  = starting_usdt
        self.coin_balance   = 0.0
        self.avg_buy_price  = 0.0
        self.taker_fee_pct  = taker_fee_pct
        self.cfg            = cfg
        self.trades: list[Trade] = []
        self._trade_counter = 0
        self.total_fees     = 0.0
        self.realized_pnl   = 0.0

        log.info(self.SEPARATOR)
        log.info(f"  📋 PaperTracker inicializovaný")
        log.info(f"  Štartovací zostatok: {starting_usdt:.2f} USDT")
        log.info(f"  Poplatok: {taker_fee_pct*100:.2f}% per obchod")
        log.info(self.SEPARATOR)

    # ── Hlavné metódy ─────────────────────────────────────────────────────────

    def buy(self, price: float, amount: float) -> Trade:
        """
        Simuluje BUY príkaz.
        Skontroluje dostatok USDT, aktualizuje zostatky, vypíše log.
        """
        sym      = self.cfg.ccxt_symbol if self.cfg else symbol
        notional = price * amount
        fee      = notional * self.taker_fee_pct
        total    = notional + fee

        # Kontrola zostatku
        if total > self.usdt_balance:
            log.warning(
                f"[SIMULÁCIA] ⚠️  Nedostatok USDT: potrebujem {total:.4f}, "
                f"mám {self.usdt_balance:.4f}"
            )
            raise ValueError(f"Nedostatok USDT pre BUY: {total:.4f} > {self.usdt_balance:.4f}")

        # Aktualizuj priemernu nákupnú cenu (weighted average)
        prev_total_cost   = self.avg_buy_price * self.coin_balance
        new_total_cost    = prev_total_cost + notional
        new_coin_total    = self.coin_balance + amount
        self.avg_buy_price = new_total_cost / new_coin_total if new_coin_total > 0 else price

        # Aktualizuj zostatky
        self.usdt_balance -= total
        self.coin_balance += amount
        self.total_fees   += fee

        trade = self._record_trade("BUY", sym, price, amount, notional, fee, pnl=0.0)
        self._log_trade(trade)
        self._log_portfolio(price)
        return trade

    def sell(self, price: float, amount: float) -> Trade:
        """
        Simuluje SELL príkaz.
        Skontroluje dostatok coinu, vypočíta P&L, aktualizuje zostatky.
        """
        sym      = self.cfg.ccxt_symbol if self.cfg else symbol
        notional = price * amount
        fee      = notional * self.taker_fee_pct

        # Kontrola zostatku
        if amount > self.coin_balance:
            log.warning(
                f"[SIMULÁCIA] ⚠️  Nedostatok {self.cfg.base_asset if self.cfg else 'COIN'}: "
                f"potrebujem {amount}, mám {self.coin_balance:.6f}"
            )
            raise ValueError(f"Nedostatok coinu pre SELL: {amount} > {self.coin_balance:.6f}")

        # Vypočítaj realizovaný P&L
        cost_basis  = self.avg_buy_price * amount   # koľko sme za to zaplatili
        pnl         = (price - self.avg_buy_price) * amount - fee

        # Aktualizuj zostatky
        self.usdt_balance  += notional - fee
        self.coin_balance  -= amount
        self.total_fees    += fee
        self.realized_pnl  += pnl

        # Ak sme predali všetko, resetuj avg_buy_price
        if self.coin_balance <= 0:
            self.coin_balance  = 0.0
            self.avg_buy_price = 0.0

        trade = self._record_trade("SELL", sym, price, amount, notional, fee, pnl=pnl)
        self._log_trade(trade)
        self._log_portfolio(price)
        return trade

    # ── Reporty ──────────────────────────────────────────────────────────────

    def portfolio_value(self, current_price: float) -> float:
        """Celková hodnota portfólia v USDT (zostatok + hodnota coinu)."""
        return self.usdt_balance + self.coin_balance * current_price

    def unrealized_pnl(self, current_price: float) -> float:
        """Nerealizovaný P&L — zisk/strata na držaných coinoch."""
        if self.coin_balance <= 0 or self.avg_buy_price <= 0:
            return 0.0
        return (current_price - self.avg_buy_price) * self.coin_balance

    def print_summary(self, current_price: float):
        """Vypíše kompletný súhrn portfólia do konzoly."""
        base  = self.cfg.base_asset  if self.cfg else "COIN"
        quote = self.cfg.quote_asset if self.cfg else "USDT"
        sym   = self.cfg.ccxt_symbol if self.cfg else symbol

        total_val    = self.portfolio_value(current_price)
        unreal_pnl   = self.unrealized_pnl(current_price)
        total_pnl    = self.realized_pnl + unreal_pnl
        total_return = (total_val - self.starting_usdt) / self.starting_usdt * 100

        log.info("")
        log.info(self.SEPARATOR)
        log.info(f"  📊 PORTFÓLIO — {sym}  [{datetime.now().strftime('%d.%m.%Y %H:%M:%S')}]")
        log.info(self.SEPARATOR)
        log.info(f"  {'Aktuálna cena:':<28} {current_price:.{self.cfg.price_decimals if self.cfg else 2}f} {quote}")
        log.info(f"  {'Zostatok USDT:':<28} {self.usdt_balance:>12.4f} {quote}")
        log.info(f"  {'Zostatok coinu:':<28} {self.coin_balance:>12.6f} {base}")
        if self.coin_balance > 0:
            log.info(f"  {'  └ priem. nák. cena:':<28} {self.avg_buy_price:>12.4f} {quote}")
            log.info(f"  {'  └ hodnota v USDT:':<28} {self.coin_balance * current_price:>12.4f} {quote}")
        log.info(self.SEPARATOR)
        log.info(f"  {'Celková hodnota:':<28} {total_val:>12.4f} {quote}")
        log.info(f"  {'Štart:':<28} {self.starting_usdt:>12.4f} {quote}")
        log.info(f"  {'Zmena:':<28} {total_val - self.starting_usdt:>+12.4f} {quote}  ({total_return:+.2f}%)")
        log.info(self.SEPARATOR)
        log.info(f"  {'Realizovaný P&L:':<28} {self.realized_pnl:>+12.4f} {quote}")
        log.info(f"  {'Nerealizovaný P&L:':<28} {unreal_pnl:>+12.4f} {quote}")
        log.info(f"  {'Celkový P&L:':<28} {total_pnl:>+12.4f} {quote}")
        log.info(f"  {'Zaplatené poplatky:':<28} {self.total_fees:>12.4f} {quote}")
        log.info(self.SEPARATOR)
        log.info(f"  {'Počet obchodov:':<28} {len(self.trades):>12d}")
        buys  = sum(1 for t in self.trades if t.side == "BUY")
        sells = sum(1 for t in self.trades if t.side == "SELL")
        log.info(f"  {'  └ BUY / SELL:':<28} {buys:>6d} / {sells:<6d}")
        if sells > 0:
            wins     = sum(1 for t in self.trades if t.side == "SELL" and t.pnl > 0)
            win_rate = wins / sells * 100
            log.info(f"  {'  └ Win rate (SELL):':<28} {win_rate:>11.1f}%")
        log.info(self.SEPARATOR)
        log.info("")

    def print_trade_history(self, last_n: int = 10):
        """Vypíše históriu posledných N obchodov."""
        trades = self.trades[-last_n:]
        base   = self.cfg.base_asset  if self.cfg else "COIN"
        quote  = self.cfg.quote_asset if self.cfg else "USDT"

        log.info("")
        log.info(self.SEPARATOR)
        log.info(f"  📜 HISTÓRIA OBCHODOV (posledných {len(trades)})")
        log.info(self.SEPARATOR)
        log.info(f"  {'#':>4}  {'Čas':>8}  {'Strana':>4}  {'Cena':>10}  "
                 f"{'Qty':>8}  {'Notional':>10}  {'P&L':>10}")
        log.info(f"  {'─'*4}  {'─'*8}  {'─'*4}  {'─'*10}  "
                 f"{'─'*8}  {'─'*10}  {'─'*10}")
        for t in trades:
            pnl_str = f"{t.pnl:>+10.4f}" if t.side == "SELL" else f"{'—':>10}"
            log.info(
                f"  {t.trade_id:>4}  {t.timestamp.strftime('%H:%M:%S'):>8}  "
                f"{t.side:>4}  {t.price:>10.4f}  {t.amount:>8.4f}  "
                f"{t.notional:>10.4f}  {pnl_str}"
            )
        log.info(self.SEPARATOR)
        log.info("")

    # ── Interné metódy ────────────────────────────────────────────────────────

    def _record_trade(
        self, side: str, sym: str, price: float,
        amount: float, notional: float, fee: float, pnl: float
    ) -> Trade:
        self._trade_counter += 1
        trade = Trade(
            trade_id   = self._trade_counter,
            side       = side,
            symbol     = sym,
            price      = price,
            amount     = amount,
            notional   = notional,
            fee        = fee,
            timestamp  = datetime.now(),
            usdt_after = self.usdt_balance,
            coin_after = self.coin_balance,
            pnl        = pnl,
        )
        self.trades.append(trade)
        return trade

    def _log_trade(self, t: Trade):
        """Vypíše [SIMULÁCIA] riadok — hlavný výstup pre test_mode."""
        base  = self.cfg.base_asset  if self.cfg else "COIN"
        quote = self.cfg.quote_asset if self.cfg else "USDT"
        icon  = "🟢" if t.side == "BUY" else "🔴"

        pnl_part = ""
        if t.side == "SELL":
            pnl_icon = "✅" if t.pnl >= 0 else "❌"
            pnl_part = f" | P&L: {t.pnl:>+.4f} {quote} {pnl_icon}"

        log.info(
            f"[SIMULÁCIA] {icon} Typ: {t.side:<4} | Pár: {t.symbol} | "
            f"Cena: {t.price:.4f} | Množstvo: {t.amount:.{self.cfg.qty_decimals if self.cfg else 4}f} {base} | "
            f"Hodnota: {t.notional:.4f} {quote} | Poplatok: {t.fee:.4f} {quote}"
            f"{pnl_part}"
        )

    def _log_portfolio(self, current_price: float):
        """Stručný inline zostatok po každom obchode."""
        base  = self.cfg.base_asset  if self.cfg else "COIN"
        quote = self.cfg.quote_asset if self.cfg else "USDT"
        total = self.portfolio_value(current_price)
        pnl   = total - self.starting_usdt
        log.info(
            f"            💼 Zostatok: {self.usdt_balance:.4f} {quote} + "
            f"{self.coin_balance:.6f} {base} | "
            f"Celkom: {total:.4f} {quote} ({pnl:>+.4f})"
        )


# ════════════════════════════════════════════════════════════════
#  OrderExecutor — jednotné miesto pre create_order
# ════════════════════════════════════════════════════════════════

class OrderExecutor:
    """
    Jediná trieda cez ktorú prechádza každý príkaz.

    test_mode = True  → PaperTracker, žiadne API volanie
    test_mode = False → reálne Binance create_order()

    Použitie:
        executor = OrderExecutor(client, precision, tracker, test_mode)
        executor.buy(price=618.42, amount=0.02)
        executor.sell(price=625.10, amount=0.02)
    """

    def __init__(
        self,
        client,                          # BinanceConnection alebo None v test_mode
        precision: PrecisionManager,
        tracker: PaperTracker,
        mode: bool = test_mode,          # preberá globálnu premennú
    ):
        self.client    = client
        self.precision = precision
        self.tracker   = tracker
        self.mode      = mode
        self.sym_bin   = precision.cfg.binance_symbol
        self.sym_ccxt  = precision.cfg.ccxt_symbol

        regime = "📋 SIMULÁCIA (test_mode=True)" if mode else "🔴 LIVE (test_mode=False)"
        log.info(f"OrderExecutor: {self.sym_ccxt} | {regime}")

    # ── Verejné metódy ────────────────────────────────────────────────────────

    def buy(self, price: float, amount: float) -> dict:
        """
        Nákupný príkaz.
        test_mode=True  → PaperTracker.buy(), žiadne API
        test_mode=False → Binance LIMIT BUY
        """
        price_str, qty_str = self.precision.safe_params(price, amount)
        price_f = float(price_str)
        qty_f   = float(qty_str)

        if self.mode:
            trade = self.tracker.buy(price_f, qty_f)
            return {
                "orderId":    f"SIM-{trade.trade_id}",
                "side":       "BUY",
                "status":     "SIMULATED",
                "price":      price_str,
                "origQty":    qty_str,
                "timestamp":  trade.timestamp.isoformat(),
            }
        else:
            return self._live_order("BUY", price_str, qty_str)

    def sell(self, price: float, amount: float) -> dict:
        """
        Predajný príkaz.
        test_mode=True  → PaperTracker.sell(), žiadne API
        test_mode=False → Binance LIMIT SELL
        """
        price_str, qty_str = self.precision.safe_params(price, amount)
        price_f = float(price_str)
        qty_f   = float(qty_str)

        if self.mode:
            trade = self.tracker.sell(price_f, qty_f)
            return {
                "orderId":    f"SIM-{trade.trade_id}",
                "side":       "SELL",
                "status":     "SIMULATED",
                "price":      price_str,
                "origQty":    qty_str,
                "pnl":        trade.pnl,
                "timestamp":  trade.timestamp.isoformat(),
            }
        else:
            return self._live_order("SELL", price_str, qty_str)

    def _live_order(self, side: str, price_str: str, qty_str: str) -> dict:
        """Reálny Binance príkaz — volá sa len pri test_mode=False."""
        try:
            from binance.exceptions import BinanceAPIException
            resp = self.client.client.create_order(
                symbol      = self.sym_bin,
                side        = side,
                type        = "LIMIT",
                timeInForce = "GTC",
                quantity    = qty_str,
                price       = price_str,
            )
            log.info(
                f"[LIVE] {side} @ {price_str} qty {qty_str} | "
                f"orderId={resp['orderId']} status={resp['status']}"
            )
            return resp
        except Exception as e:
            log.error(f"[LIVE] {side} zlyhal: {e}")
            raise


# ════════════════════════════════════════════════════════════════
#  BinanceConnection — symbol-aware (nezmenená logika)
# ════════════════════════════════════════════════════════════════

class BinanceConnection:
    def __init__(self, sym: str = symbol):
        if not API_KEY or not API_SECRET:
            raise ValueError(
                "❌ Chýbajú API kľúče.\n"
                "   Vytvor .env: BINANCE_API_KEY=... BINANCE_API_SECRET=..."
            )
        from binance.client import Client
        from binance.exceptions import BinanceAPIException
        self._BinanceAPIException = BinanceAPIException
        self.client = Client(API_KEY, API_SECRET, testnet=False)
        mode = "📋 TESTNET" if test_mode else "🔴 LIVE"
        log.info(f"Binance: {mode}")
        self._init_symbol(sym)

    def _init_symbol(self, sym: str):
        self.ccxt_symbol    = to_ccxt_symbol(sym)
        self.binance_symbol = to_binance_symbol(sym)
        self.base_asset, self.quote_asset = parse_assets(sym)
        self.cfg       = self.load_symbol(sym)
        self.precision = PrecisionManager(self.cfg)

    def load_symbol(self, sym: str) -> SymbolConfig:
        bin_sym = to_binance_symbol(sym)
        log.info(f"Načítavam {sym} ({bin_sym}) z Binance...")
        try:
            raw = self.client.get_symbol_info(bin_sym)
        except Exception as e:
            raise ValueError(f"Symbol '{sym}' nenájdený: {e}")
        if raw is None:
            raise ValueError(f"Symbol '{bin_sym}' neexistuje na Binance.")
        if raw.get("status") != "TRADING":
            raise ValueError(f"Symbol '{bin_sym}' nie je aktívny.")
        cfg = SymbolConfig.from_binance_info(raw, sym)
        log.info(f"✅ {cfg}")
        return cfg

    def switch_symbol(self, new_sym: str):
        log.info(f"Prepínam: {self.ccxt_symbol} → {new_sym}")
        self._init_symbol(new_sym)

    def get_price(self) -> float:
        ticker = self.client.get_symbol_ticker(symbol=self.binance_symbol)
        return float(ticker["price"])

    def get_klines(self, interval: str = "1h", limit: int = 100) -> list[dict]:
        raw = self.client.get_klines(
            symbol=self.binance_symbol, interval=interval, limit=limit
        )
        return [
            {"time": datetime.fromtimestamp(k[0]/1000), "open": float(k[1]),
             "high": float(k[2]), "low": float(k[3]), "close": float(k[4]), "volume": float(k[5])}
            for k in raw
        ]

    def get_relevant_balances(self) -> dict:
        info  = self.client.get_account()
        all_b = {
            b["asset"]: {"free": float(b["free"]), "locked": float(b["locked"]),
                         "total": float(b["free"]) + float(b["locked"])}
            for b in info["balances"] if float(b["free"]) > 0 or float(b["locked"]) > 0
        }
        empty = {"free": 0.0, "locked": 0.0, "total": 0.0}
        return {
            self.base_asset:  all_b.get(self.base_asset,  empty),
            self.quote_asset: all_b.get(self.quote_asset, empty),
        }


# ════════════════════════════════════════════════════════════════
#  Továrenská funkcia — vytvorí celý stack naraz
# ════════════════════════════════════════════════════════════════

def create_bot_stack(
    sym: str = symbol,
    mode: bool = test_mode,
    starting_usdt: float = 1000.0,
) -> tuple:
    """
    Vytvorí a vráti (precision, tracker, executor) pre daný symbol.

    V test_mode=True: nevyžaduje API kľúče, funguje offline.
    V test_mode=False: vyžaduje .env s API kľúčmi.

    Použitie:
        precision, tracker, executor = create_bot_stack()
        executor.buy(618.42, 0.02)
        executor.sell(625.10, 0.02)
        tracker.print_summary(625.10)
    """
    if mode:
        # Simulačný stack — mock SymbolConfig bez API
        base, quote = parse_assets(sym)
        # Defaultné hodnoty (pri živom bote sa prepíšu z Binance)
        MOCK_CONFIGS = {
            "BNB/USDT": ("0.01",   "0.01",    5.0),
            "BTC/USDT": ("0.10",   "0.00001", 5.0),
            "SOL/USDT": ("0.0001", "0.001",   1.0),
            "ETH/USDT": ("0.01",   "0.0001",  5.0),
        }
        tick_s, step_s, min_n = MOCK_CONFIGS.get(
            to_ccxt_symbol(sym), ("0.01", "0.01", 5.0)
        )
        tick_d = Decimal(tick_s)
        step_d = Decimal(step_s)
        cfg = SymbolConfig(
            ccxt_symbol    = to_ccxt_symbol(sym),
            binance_symbol = to_binance_symbol(sym),
            base_asset     = base,
            quote_asset    = quote,
            tick_size      = tick_d,
            price_decimals = SymbolConfig._decimals(tick_d),
            step_size      = step_d,
            qty_decimals   = SymbolConfig._decimals(step_d),
            min_qty        = float(step_s),
            max_qty        = 9_000_000.0,
            min_notional   = min_n,
        )
        precision = PrecisionManager(cfg)
        tracker   = PaperTracker(starting_usdt=starting_usdt, cfg=cfg)
        executor  = OrderExecutor(client=None, precision=precision, tracker=tracker, mode=True)
        return precision, tracker, executor
    else:
        conn      = BinanceConnection(sym)
        tracker   = PaperTracker(starting_usdt=starting_usdt, cfg=conn.cfg)
        executor  = OrderExecutor(client=conn, precision=conn.precision, tracker=tracker, mode=False)
        return conn.precision, tracker, executor


# ════════════════════════════════════════════════════════════════
#  Vstupný bod — demo simulácie grid obchodov
# ════════════════════════════════════════════════════════════════

if __name__ == "__main__":

    log.info("═" * 62)
    log.info(f"  APEX BOT — Simulácia | symbol={symbol} | test_mode={test_mode}")
    log.info("═" * 62)

    # Vytvor stack
    precision, tracker, executor = create_bot_stack(
        sym           = symbol,
        mode          = test_mode,
        starting_usdt = 1000.0,
    )

    # Simulácia pohybu ceny — typický grid scenár
    # Cena osciluje, bot nakupuje pri poklese, predáva pri raste
    grid_scenario = [
        # (akcia, cena, qty_usdt, popis)
        ("buy",  618.00, 15.0, "Prvý nákup — cena na base úrovni"),
        ("buy",  610.58, 15.0, "BUY grid -1.2% — cena klesla"),
        ("buy",  603.32, 15.0, "BUY grid -2.4% — cena klesla ďalej"),
        ("sell", 610.58, None, "SELL grid +1.2% — bounce nahor"),
        ("buy",  610.58, 15.0, "Re-entry BUY — grid sa obnovil"),
        ("sell", 618.00, None, "SELL grid +1.2% — ďalší bounce"),
        ("sell", 625.63, None, "SELL grid +2.4% — silný pohyb"),
        ("buy",  618.00, 15.0, "Re-entry BUY po sell"),
        ("sell", 625.63, None, "SELL — záver simulácie"),
    ]

    base_asset = precision.cfg.base_asset

    log.info(f"\n  Spúšťam simuláciu {len(grid_scenario)} obchodov...\n")

    for i, (action, price, usdt_amount, desc) in enumerate(grid_scenario, 1):
        log.info(f"  ── Obchod {i}/{len(grid_scenario)}: {desc}")
        try:
            if action == "buy":
                qty = precision.qty_from_usdt(usdt_amount, price)
                executor.buy(price, qty)
            else:
                # Predaj aktuálny coin zostatok (alebo jeho časť)
                sell_qty = precision.floor_qty(tracker.coin_balance * 0.34)
                if sell_qty < precision.cfg.min_qty:
                    log.info(f"  ⚠️  Preskočené — qty {sell_qty} < min_qty {precision.cfg.min_qty}")
                    continue
                executor.sell(price, sell_qty)
        except ValueError as e:
            log.warning(f"  ⚠️  Preskočené: {e}")
        log.info("")

    # Záverečný súhrn
    final_price = 625.63
    tracker.print_summary(final_price)
    tracker.print_trade_history(last_n=20)

    log.info(f"  Symbol:    {symbol}")
    log.info(f"  test_mode: {test_mode}")
    log.info(f"  Pre zmenu: edituj riadky 'symbol' a 'test_mode' na vrchu súboru.")
