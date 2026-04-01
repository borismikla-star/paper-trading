"""
APEX BOT — Web Dashboard
=========================
Jednoduchý HTTP status dashboard pre Railway.
Beží na porte 8080 v samostatnom threade popri hlavnom bote.

URL: https://tvoj-projekt.railway.app
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional


# Globálny stav — aktualizuje ho hlavný bot, číta dashboard
_state: dict = {
    "status":        "STARTING",
    "symbol":        "BNB/USDT",
    "tick":          0,
    "price":         0.0,
    "portfolio":     0.0,
    "base_capital":  0.0,
    "pnl_usdt":      0.0,
    "pnl_pct":       0.0,
    "unrealized_pnl":0.0,
    "coin_balance":  0.0,
    "regime":        "UNDEFINED",
    "regime_conf":   0.0,
    "exec_state":    "UNKNOWN",
    "port_risk":     "NORMAL",
    "winner":        "—",
    "allow_trading": False,
    "allow_buys":    False,
    "uptime_sec":    0,
    "started_at":    datetime.now().isoformat(),
    "last_tick_at":  "—",
    "daily_target":  100.0,
    "test_mode":     True,
}
_lock = threading.Lock()


def update_state(**kwargs) -> None:
    """Volaj z hlavného bota každý tick."""
    with _lock:
        _state.update(kwargs)
        _state["last_tick_at"] = datetime.now().strftime("%H:%M:%S")


def _html() -> str:
    with _lock:
        s = dict(_state)

    uptime_min = s["uptime_sec"] // 60
    uptime_h   = uptime_min // 60
    uptime_m   = uptime_min % 60
    uptime_str = f"{uptime_h}h {uptime_m}min" if uptime_h > 0 else f"{uptime_m}min"

    status_color = {
        "RUNNING":  "#00d4aa",
        "STARTING": "#f5c518",
        "STOPPED":  "#ff4d6d",
        "ERROR":    "#ff4d6d",
    }.get(s["status"], "#888")

    regime_color = {
        "RANGE":         "#00d4aa",
        "UPTREND":       "#00d4aa",
        "DOWNTREND":     "#ff4d6d",
        "BREAKOUT_UP":   "#f5c518",
        "BREAKOUT_DOWN": "#ff4d6d",
        "PANIC":         "#ff0000",
        "UNDEFINED":     "#888888",
    }.get(s["regime"], "#888")

    pnl_color  = "#00d4aa" if s["pnl_usdt"] >= 0 else "#ff4d6d"
    mode_badge = "📋 PAPER" if s["test_mode"] else "🔴 LIVE"
    trade_icon = "✅" if s["allow_trading"] else "⏸"
    buy_icon   = "✅" if s["allow_buys"]    else "🚫"

    return f"""<!DOCTYPE html>
<html lang="sk">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>APEX BOT — {s['symbol']}</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: #060a0f;
      color: #c8d4e0;
      font-family: 'IBM Plex Mono', 'Courier New', monospace;
      min-height: 100vh;
      padding: 24px 16px;
    }}
    .container {{ max-width: 680px; margin: 0 auto; }}
    .header {{
      display: flex;
      align-items: center;
      justify-content: space-between;
      border-bottom: 1px solid #0d1a24;
      padding-bottom: 16px;
      margin-bottom: 24px;
    }}
    .logo {{ font-size: 20px; font-weight: 700; letter-spacing: 1px; color: #e8f0f8; }}
    .logo span {{ color: #00d4aa; }}
    .badge {{
      font-size: 11px;
      padding: 3px 10px;
      border-radius: 12px;
      background: rgba(0,212,170,0.12);
      color: #00d4aa;
      border: 1px solid rgba(0,212,170,0.25);
    }}
    .status-dot {{
      display: inline-block;
      width: 8px; height: 8px;
      border-radius: 50%;
      background: {status_color};
      box-shadow: 0 0 8px {status_color};
      animation: pulse 2s infinite;
      margin-right: 6px;
    }}
    @keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.4}} }}
    .grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      margin-bottom: 12px;
    }}
    .grid-3 {{ grid-template-columns: 1fr 1fr 1fr; }}
    .card {{
      background: #080d13;
      border: 1px solid #0d1a24;
      border-radius: 10px;
      padding: 14px 16px;
    }}
    .card-label {{
      font-size: 10px;
      color: #3a5a7a;
      letter-spacing: 1.5px;
      text-transform: uppercase;
      margin-bottom: 6px;
    }}
    .card-value {{
      font-size: 22px;
      font-weight: 700;
      color: #e8f0f8;
    }}
    .card-sub {{
      font-size: 11px;
      color: #3a5a7a;
      margin-top: 2px;
    }}
    .pnl {{ color: {pnl_color}; }}
    .regime {{ color: {regime_color}; }}
    .sep {{ border: none; border-top: 1px solid #0d1a24; margin: 20px 0; }}
    .row {{
      display: flex;
      justify-content: space-between;
      padding: 8px 0;
      border-bottom: 1px solid #080d13;
      font-size: 13px;
    }}
    .row:last-child {{ border-bottom: none; }}
    .row-label {{ color: #3a5a7a; }}
    .footer {{
      text-align: center;
      font-size: 10px;
      color: #1a2a3a;
      margin-top: 24px;
      letter-spacing: 1px;
    }}
    .refresh {{ font-size: 10px; color: #1a3a2a; float: right; }}
    .progress-bar {{
      background: #0d1a24;
      border-radius: 4px;
      height: 6px;
      margin-top: 8px;
      overflow: hidden;
    }}
    .progress-fill {{
      height: 100%;
      background: linear-gradient(90deg, #00d4aa, #0066ff);
      border-radius: 4px;
      width: {min(100, max(0, (s['pnl_usdt'] / s['daily_target'] * 100) if s['daily_target'] > 0 else 0)):.1f}%;
      transition: width 0.5s;
    }}
  </style>
</head>
<body>
<div class="container">

  <div class="header">
    <div>
      <div class="logo">◈ APEX <span>BOT</span></div>
      <div style="font-size:11px;color:#3a5a7a;margin-top:2px;">
        <span class="status-dot"></span>{s['status']} · {mode_badge}
      </div>
    </div>
    <div class="badge">{s['symbol']}</div>
  </div>

  <!-- Cena + portfólio -->
  <div class="grid">
    <div class="card">
      <div class="card-label">Aktuálna cena</div>
      <div class="card-value">{s['price']:.2f}</div>
      <div class="card-sub">USDT</div>
    </div>
    <div class="card">
      <div class="card-label">Portfólio</div>
      <div class="card-value">{s['portfolio']:.2f}</div>
      <div class="card-sub pnl">{s['pnl_usdt']:+.2f} USDT ({s['pnl_pct']:+.2f}%)</div>
    </div>
  </div>

  <!-- PnL + coin -->
  <div class="grid">
    <div class="card">
      <div class="card-label">Nerealizovaný PnL</div>
      <div class="card-value pnl">{s['unrealized_pnl']:+.4f}</div>
      <div class="card-sub">USDT</div>
    </div>
    <div class="card">
      <div class="card-label">Coin zostatok</div>
      <div class="card-value">{s['coin_balance']:.4f}</div>
      <div class="card-sub">{s['symbol'].split('/')[0]}</div>
    </div>
  </div>

  <!-- Denný cieľ progress -->
  <div class="card" style="margin-bottom:12px;">
    <div class="card-label">Denný cieľ — {s['daily_target']:.0f} USDT</div>
    <div style="font-size:16px;font-weight:700;color:#00d4aa;margin-top:4px;">
      {s['pnl_usdt']:+.2f} / {s['daily_target']:.0f} USDT
      ({min(100, max(0, s['pnl_usdt']/s['daily_target']*100) if s['daily_target']>0 else 0):.1f}%)
    </div>
    <div class="progress-bar"><div class="progress-fill"></div></div>
  </div>

  <!-- Regime + status -->
  <div class="grid grid-3" style="margin-bottom:12px;">
    <div class="card">
      <div class="card-label">Regime</div>
      <div style="font-size:14px;font-weight:700;" class="regime">{s['regime']}</div>
      <div class="card-sub">conf {s['regime_conf']:.2f}</div>
    </div>
    <div class="card">
      <div class="card-label">Trading</div>
      <div style="font-size:18px;">{trade_icon}</div>
      <div class="card-sub">{s['winner']}</div>
    </div>
    <div class="card">
      <div class="card-label">Buy</div>
      <div style="font-size:18px;">{buy_icon}</div>
      <div class="card-sub">{s['exec_state']}</div>
    </div>
  </div>

  <!-- Detail -->
  <div class="card">
    <div class="row">
      <span class="row-label">Tick</span>
      <span>#{s['tick']}</span>
    </div>
    <div class="row">
      <span class="row-label">Posledný tick</span>
      <span>{s['last_tick_at']}</span>
    </div>
    <div class="row">
      <span class="row-label">Portfolio risk</span>
      <span>{s['port_risk']}</span>
    </div>
    <div class="row">
      <span class="row-label">Uptime</span>
      <span>{uptime_str}</span>
    </div>
    <div class="row">
      <span class="row-label">Spustený</span>
      <span>{s['started_at'][:16].replace('T',' ')}</span>
    </div>
  </div>

  <div class="footer">
    APEX BOT · Paper Trading · Auto-refresh 30s
    <span class="refresh">{datetime.now().strftime('%H:%M:%S')}</span>
  </div>

</div>
</body>
</html>"""


def _json_status() -> dict:
    with _lock:
        return dict(_state)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
        elif self.path == "/api/status":
            body = json.dumps(_json_status(), default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = _html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass   # potlač HTTP logy


def start_dashboard(port: int = 8080) -> None:
    """Spustí dashboard v daemon threade — volaj raz pri štarte bota."""
    server = HTTPServer(("0.0.0.0", port), _Handler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    import logging
    logging.getLogger("ApexBot").info(f"📊 Dashboard: http://0.0.0.0:{port}")
