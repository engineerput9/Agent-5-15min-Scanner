import csv
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from agent_core import Params, format_signal, tg_send

CSV = "oct1_5m_trades.csv"
CAPITAL = 100000.0
p = Params(rr1=1.2, base_min=5, htf_min=15, htf_trigger=False,
           use_htf=True, eod_exit=False, risk_pct=1.0)

rows = []
with open(CSV, newline="") as fh:
    rows = list(csv.DictReader(fh))

sent = 0
for row in rows:
    ts = pd.Timestamp(row["entry_time"])
    side = 1 if row["side"] == "LONG" else -1
    entry = float(row["entry"])
    sl = float(row["sl"])
    tp1 = float(row["tp1"])
    qty = int(row["qty"])
    msg = format_signal(row["symbol"], side, ts, entry, sl, tp1, qty, p,
                        CAPITAL, row["entry_type"], 0.0)
    if not tg_send(msg):
        raise SystemExit(f"Telegram send failed for {row['symbol']}")
    sent += 1

print(f"sent {sent} October 1 5m-flip signal(s), RR {p.rr1:g}")
