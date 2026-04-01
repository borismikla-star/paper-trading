"""
APEX BOT — Telegram Notifikácie
=================================
Posiela štruktúrované správy o stave bota.
Volá sa z main.py — nie je to business logika.
"""

from __future__ import annotations

import logging
import os
import threading
import requests
from datetime import datetime

log = logging.getLogger("ApexBot.Telegram")

_TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")


def _send(text: str) -> None:
    """Odošle správu asynchrónne — neblokuje hlavný loop."""
    if not _TOKEN or not _CHAT_ID:
        return

    def _do():
        try:
            requests.post(
                f"https://api.telegram.org/bot{_TOKEN}/sendMessage",
                json={"chat_id": _CHAT_ID, "text": text,
                      "parse_mode": "HTML"},
                timeout=5,
            )
        except Exception as e:
            log.debug(f"Telegram chyba: {e}")

    threading.Thread(target=_do, daemon=True).start()


def notify_startup(symbol: str, capital: float, mode: str) -> None:
    _send(
        f"🚀 <b>APEX BOT štart</b>\n"
        f"Symbol: <code>{symbol}</code>\n"
        f"Kapitál: <code>{capital:.0f} USDT</code>\n"
        f"Mód: <code>{mode}</code>\n"
        f"Čas: {datetime.now().strftime('%H:%M:%S')}"
    )


def notify_heartbeat(
    tick: int, price: float, pv: float,
    pnl: float, regime: str, uptime_min: int,
) -> None:
    pnl_icon = "📈" if pnl >= 0 else "📉"
    _send(
        f"💓 <b>Heartbeat</b> — Tick #{tick}\n"
        f"Cena: <code>{price:.2f} USDT</code>\n"
        f"Portfólio: <code>{pv:.2f} USDT</code>\n"
        f"{pnl_icon} PnL: <code>{pnl:+.2f} USDT</code>\n"
        f"Regime: <code>{regime}</code>\n"
        f"Uptime: <code>{uptime_min} min</code>"
    )


def notify_regime_change(prev: str, new: str, confidence: float) -> None:
    icons = {
        "RANGE": "↔️", "UPTREND": "📈", "DOWNTREND": "📉",
        "BREAKOUT_UP": "🚀", "BREAKOUT_DOWN": "💥",
        "PANIC": "🚨", "UNDEFINED": "❓",
    }
    icon = icons.get(new, "🔄")
    _send(
        f"{icon} <b>Regime zmena</b>\n"
        f"{prev} → <b>{new}</b>\n"
        f"Confidence: <code>{confidence:.2f}</code>"
    )


def notify_panic(price: float, pnl: float) -> None:
    _send(
        f"🚨 <b>PANIC MODE</b>\n"
        f"Cena: <code>{price:.2f} USDT</code>\n"
        f"PnL: <code>{pnl:+.2f} USDT</code>\n"
        f"Bot zastavil nové ordery."
    )


def notify_circuit_breaker() -> None:
    _send(
        f"⚡ <b>Circuit Breaker aktívny</b>\n"
        f"Obchodovanie pozastavené.\n"
        f"Čas: {datetime.now().strftime('%H:%M:%S')}"
    )


def notify_shutdown(ticks: int, pnl: float) -> None:
    _send(
        f"🛑 <b>APEX BOT zastavený</b>\n"
        f"Celkovo tickov: <code>{ticks}</code>\n"
        f"Celkový PnL: <code>{pnl:+.2f} USDT</code>"
    )
