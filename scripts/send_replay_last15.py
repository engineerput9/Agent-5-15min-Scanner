"""One-shot replay of the 15 supplied 2026-10-01 original alerts.

Sends each alert separately to the primary and secondary Telegram destinations.
Credentials are supplied by GitHub Actions secrets and are never printed.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from agent_core import Params, calc_qty, format_signal  # noqa: E402

CAPITAL = 100_000.0
IST = ZoneInfo("Asia/Kolkata")
# symbol, side, entry, stop, target, displayed signal-close time (IST)
TRADES = [
    ("NG=F", "SELL", 2.96, 3.00, 2.93, "22:00"),
    ("CL=F", "BUY", 92.54, 91.50, 93.37, "20:45"),
    ("HG=F", "BUY", 6.57, 6.53, 6.60, "16:00"),
    ("SYNGENE", "BUY", 361.90, 355.70, 366.86, "15:15"),
    ("SBIN", "BUY", 954.10, 949.55, 957.74, "15:15"),
    ("SAIL", "BUY", 174.50, 170.99, 177.31, "15:15"),
    ("OBEROIRLTY", "BUY", 1754.60, 1736.50, 1769.08, "15:15"),
    ("NYKAA", "BUY", 324.95, 319.80, 329.07, "15:15"),
    ("MIDCPNIFTY", "BUY", 13568.90, 13464.85, 13652.14, "15:15"),
    ("KPITTECH", "BUY", 492.00, 485.55, 497.16, "15:15"),
    ("JIOFIN", "BUY", 212.50, 210.04, 214.47, "15:15"),
    ("HINDZINC", "BUY", 555.05, 550.80, 558.45, "15:15"),
    ("GODREJPROP", "BUY", 1595.80, 1576.00, 1611.64, "15:15"),
    ("CAMS", "BUY", 677.25, 668.75, 684.05, "15:15"),
    ("ANGELONE", "BUY", 276.80, 268.00, 283.84, "15:15"),
]


def destinations():
    pairs = [
        ("primary", os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")),
        ("secondary", os.getenv("TELEGRAM_BOT_TOKEN_2", ""), os.getenv("TELEGRAM_CHAT_ID_2", "")),
    ]
    missing = [name for name, token, chat in pairs if not token.strip() or not chat.strip()]
    if missing:
        raise RuntimeError("Missing Telegram credentials for: " + ", ".join(missing))
    return pairs


def build_message(index, sym, side_word, entry, sl, tp, close_time, p):
    # format_signal adds base_min to the candle timestamp; subtract it so the
    # displayed candle-close time matches the supplied original alert time.
    close_dt = datetime.fromisoformat(f"2026-10-01 {close_time}").replace(tzinfo=IST)
    candle_open = pd.Timestamp(close_dt - timedelta(minutes=p.base_min))
    side = 1 if side_word == "BUY" else -1
    qty = calc_qty(CAPITAL, entry, abs(entry - sl), p)
    body = format_signal(sym, side, candle_open, entry, sl, tp, qty, p, CAPITAL, "15m flip", 0.0)
    return f"<b>🔁 REPLAY — original alert {index}/15 (not a new live signal)</b>\n\n{body}"


def send_one(token, chat_id, text):
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    return response.ok and data.get("ok") is True, response.status_code, data.get("description", "")


def main():
    targets = destinations()
    p = Params(rr1=0.8, base_min=5, htf_min=15, use_htf=True, htf_trigger=True, eod_exit=False, risk_pct=1.0)
    counts = {name: 0 for name, _, _ in targets}
    failures = []
    for index, trade in enumerate(TRADES, 1):
        message = build_message(index, *trade, p)
        for name, token, chat_id in targets:
            ok, status, description = send_one(token, chat_id, message)
            if ok:
                counts[name] += 1
            else:
                failures.append((name, index, status, description))
                print(f"telegram_api destination={name} alert={index} status=failed http={status} description={description!r}")
            time.sleep(0.1)
    for name in counts:
        print(f"telegram_api destination={name} status={'ok' if counts[name] == len(TRADES) else 'partial'} messages={counts[name]}/{len(TRADES)}")
    if failures:
        print(f"replay_failed failures={len(failures)}")
        return 1
    print(f"replay_sent alerts={len(TRADES)} destinations={len(targets)} total_messages={len(TRADES) * len(targets)} rr=0.8 entry_type=15m flip")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
