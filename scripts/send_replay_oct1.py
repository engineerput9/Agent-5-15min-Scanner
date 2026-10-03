"""One-shot replay of every original 2026-10-01 15m-flip alert.

The alert set is sourced from state.json (57 entries) and replayed separately to
both Telegram destinations. Credentials come only from GitHub Actions secrets.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

from agent_core import Params, calc_qty, format_signal

CAPITAL = 100_000.0
IST = ZoneInfo("Asia/Kolkata")
# symbol, side, entry, stop, target, original alert time (IST)
TRADES = [['CL=F', 'SHORT', 89.4, 91.13, 88.02, '2026-10-01 09:15'], ['NG=F', 'SHORT', 2.98, 3.02, 2.95, '2026-10-01 09:15'], ['SI=F', 'LONG', 61.44, 60.38, 62.3, '2026-10-01 09:15'], ['GC=F', 'LONG', 4212.2, 4200.8, 4221.32, '2026-10-01 10:30'], ['BHEL.NS', 'SHORT', 418.3, 425.7, 412.38, '2026-10-01 13:00'], ['GC=F', 'SHORT', 4194.3, 4215.3, 4177.5, '2026-10-01 13:00'], ['HAL.NS', 'SHORT', 4628.1, 4678.0, 4588.18, '2026-10-01 13:00'], ['ICICIGI.NS', 'SHORT', 1571.0, 1577.9, 1565.48, '2026-10-01 13:00'], ['JIOFIN.NS', 'SHORT', 211.45, 213.21, 210.04, '2026-10-01 13:00'], ['LAURUSLABS.NS', 'SHORT', 1971.2, 1987.0, 1958.56, '2026-10-01 13:00'], ['NUVAMA.NS', 'SHORT', 1707.5, 1732.4, 1687.58, '2026-10-01 13:00'], ['NYKAA.NS', 'SHORT', 320.45, 323.8, 317.77, '2026-10-01 13:00'], ['PIDILITIND.NS', 'SHORT', 1468.5, 1484.2, 1455.94, '2026-10-01 13:00'], ['RBLBANK.NS', 'SHORT', 409.35, 415.2, 404.67, '2026-10-01 13:00'], ['TATACONSUM.NS', 'SHORT', 952.45, 959.1, 947.13, '2026-10-01 13:30'], ['ADANIGREEN.NS', 'SHORT', 1268.9, 1287.3, 1254.18, '2026-10-01 13:45'], ['APOLLOHOSP.NS', 'SHORT', 8026.5, 8056.5, 8002.5, '2026-10-01 13:45'], ['CAMS.NS', 'SHORT', 667.65, 676.35, 660.69, '2026-10-01 13:45'], ['360ONE.NS', 'LONG', 1035.7, 1009.2, 1056.9, '2026-10-01 14:30'], ['BOSCHLTD.NS', 'LONG', 45310.0, 44930.0, 45614.0, '2026-10-01 14:30'], ['NHPC.NS', 'LONG', 71.74, 70.25, 72.93, '2026-10-01 14:30'], ['APLAPOLLO.NS', 'LONG', 2121.6, 2059.5, 2171.28, '2026-10-01 14:45'], ['BIOCON.NS', 'LONG', 369.85, 363.6, 374.85, '2026-10-01 14:45'], ['IEX.NS', 'LONG', 105.76, 104.26, 106.96, '2026-10-01 14:45'], ['IREDA.NS', 'LONG', 110.86, 108.57, 112.69, '2026-10-01 14:45'], ['JSWENERGY.NS', 'LONG', 483.55, 475.1, 490.31, '2026-10-01 14:45'], ['SHREECEM.NS', 'LONG', 21865.0, 21355.0, 22273.0, '2026-10-01 14:45'], ['SIEMENS.NS', 'LONG', 3785.9, 3710.0, 3846.62, '2026-10-01 14:45'], ['SONACOMS.NS', 'LONG', 802.5, 784.95, 816.54, '2026-10-01 14:45'], ['SUPREMEIND.NS', 'LONG', 3372.2, 3286.9, 3440.44, '2026-10-01 14:45'], ['TORNTPOWER.NS', 'LONG', 1230.8, 1212.0, 1245.84, '2026-10-01 14:45'], ['UPL.NS', 'LONG', 519.85, 511.2, 526.77, '2026-10-01 14:45'], ['VBL.NS', 'LONG', 422.95, 415.75, 428.71, '2026-10-01 14:45'], ['ADANIENSOL.NS', 'LONG', 1332.3, 1310.4, 1349.82, '2026-10-01 15:00'], ['ASTRAL.NS', 'LONG', 1338.1, 1317.2, 1354.82, '2026-10-01 15:00'], ['BANKINDIA.NS', 'LONG', 128.9, 127.14, 130.31, '2026-10-01 15:00'], ['DIVISLAB.NS', 'LONG', 9183.5, 9114.0, 9239.1, '2026-10-01 15:00'], ['KEI.NS', 'LONG', 4495.8, 4442.9, 4538.12, '2026-10-01 15:00'], ['M&M.NS', 'LONG', 2850.9, 2821.5, 2874.42, '2026-10-01 15:00'], ['OIL.NS', 'LONG', 441.6, 434.9, 446.96, '2026-10-01 15:00'], ['SAMMAANCAP.NS', 'LONG', 132.41, 130.71, 133.77, '2026-10-01 15:00'], ['^NSEI', 'LONG', 22393.05, 22266.4, 22494.37, '2026-10-01 15:00'], ['ANGELONE.NS', 'LONG', 276.8, 268.0, 283.84, '2026-10-01 15:15'], ['CAMS.NS', 'LONG', 677.25, 668.75, 684.05, '2026-10-01 15:15'], ['GODREJPROP.NS', 'LONG', 1595.8, 1576.0, 1611.64, '2026-10-01 15:15'], ['HINDZINC.NS', 'LONG', 555.05, 550.8, 558.45, '2026-10-01 15:15'], ['JIOFIN.NS', 'LONG', 212.5, 210.04, 214.47, '2026-10-01 15:15'], ['KPITTECH.NS', 'LONG', 492.0, 485.55, 497.16, '2026-10-01 15:15'], ['NIFTY_MID_SELECT.NS', 'LONG', 13568.9, 13464.85, 13652.14, '2026-10-01 15:15'], ['NYKAA.NS', 'LONG', 324.95, 319.8, 329.07, '2026-10-01 15:15'], ['OBEROIRLTY.NS', 'LONG', 1754.6, 1736.5, 1769.08, '2026-10-01 15:15'], ['SAIL.NS', 'LONG', 174.5, 170.99, 177.31, '2026-10-01 15:15'], ['SBIN.NS', 'LONG', 954.1, 949.55, 957.74, '2026-10-01 15:15'], ['SYNGENE.NS', 'LONG', 361.9, 355.7, 366.86, '2026-10-01 15:15'], ['HG=F', 'LONG', 6.57, 6.53, 6.6, '2026-10-01 16:00'], ['CL=F', 'LONG', 92.54, 91.5, 93.37, '2026-10-01 20:45'], ['NG=F', 'SHORT', 2.96, 3.0, 2.93, '2026-10-01 22:00']]


def destinations():
    pairs = [
        ("primary", os.getenv("TELEGRAM_BOT_TOKEN", ""), os.getenv("TELEGRAM_CHAT_ID", "")),
        ("secondary", os.getenv("TELEGRAM_BOT_TOKEN_2", ""), os.getenv("TELEGRAM_CHAT_ID_2", "")),
    ]
    missing = [name for name, token, chat in pairs if not token.strip() or not chat.strip()]
    if missing:
        raise RuntimeError("Missing Telegram credentials for: " + ", ".join(missing))
    return pairs


def build_message(index, sym, side_word, entry, sl, tp, alert_time, p):
    # format_signal expects the candle-open timestamp and displays its close time.
    close_dt = datetime.strptime(alert_time, "%Y-%m-%d %H:%M").replace(tzinfo=IST)
    candle_open = pd.Timestamp(close_dt - timedelta(minutes=p.base_min))
    side = 1 if side_word == "LONG" else -1
    qty = calc_qty(CAPITAL, entry, abs(entry - sl), p)
    display_sym = "MIDCPNIFTY" if sym == "NIFTY_MID_SELECT.NS" else sym
    body = format_signal(display_sym, side, candle_open, entry, sl, tp, qty, p, CAPITAL, "15m flip", 0.0)
    return (f"<b>🔁 REPLAY — original Oct 1 2026 alert {index}/{len(TRADES)} "
            f"(not a new live signal)</b>\n"
            f"<b>Time (IST): {alert_time}</b>\n\n{body}")


def send_one(token, chat_id, text):
    response = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML",
              "disable_web_page_preview": True},
        timeout=20,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    return response.ok and data.get("ok") is True, response.status_code, data.get("description", "")


def main():
    targets = destinations()
    p = Params(rr1=0.8, base_min=5, htf_min=15, use_htf=True, htf_trigger=True,
               eod_exit=False, risk_pct=1.0)
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
                print(f"telegram_api destination={name} alert={index} status=failed "
                      f"http={status} description={description!r}")
            time.sleep(0.1)
    for name in counts:
        print(f"telegram_api destination={name} "
              f"status={'ok' if counts[name] == len(TRADES) else 'partial'} "
              f"messages={counts[name]}/{len(TRADES)}")
    if failures:
        print(f"replay_failed failures={len(failures)}")
        return 1
    print(f"replay_sent alerts={len(TRADES)} destinations={len(targets)} "
          f"total_messages={len(TRADES) * len(targets)} rr=0.8 entry_type=15m flip "
          "times=IST")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
