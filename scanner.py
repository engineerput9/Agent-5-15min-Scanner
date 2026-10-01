"""
scanner.py - live Agent Confluence scanner (run by GitHub Actions every 5 min).
Signals use the same engine as the backtest, so alerts and backtest always agree:
  - 5m flip while the 15m regime agrees (15m flip within MAX_AGE candles), or
  - 15m flip while the 5m regime already agrees.
No daily trade limit and no square-off: while a signal's trade is still running (SL/TP1 not hit) no overlapping signal is sent.
"""
from __future__ import annotations

import json
import os

import pandas as pd

from agent_core import (
    IST, Params, apply_session_for_universe, build_frame, calc_qty, closed_only,
    env_bool, fetch_many, format_signal, levels, load_symbols, prepare, tg_send,
)
from backtest import BT, simulate

STATE_FILE = "state.json"


def load_state(today: str) -> dict:
    try:
        with open(STATE_FILE) as fh:
            st = json.load(fh)
    except Exception:  # noqa: BLE001
        st = {}
    sent = st.get("sent", {})
    cutoff = (pd.Timestamp(today) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
    out = {"sent": {k: v for k, v in sent.items() if k.split("|")[1][:10] >= cutoff}}
    if "eod_sent" in st:
        out["eod_sent"] = st["eod_sent"]
    return out


def save_state(st: dict) -> None:
    with open(STATE_FILE, "w") as fh:
        json.dump(st, fh, indent=1, sort_keys=True)


def in_market(now: pd.Timestamp, p: Params) -> bool:
    if now.weekday() >= 5:
        return False
    hm = lambda t: int(t[:2]) * 60 + int(t[3:5])  # noqa: E731
    mins = now.hour * 60 + now.minute
    return hm(p.session_start) <= mins <= hm(p.session_end) + 10   # +10 min: catch the last candle


def main() -> None:
    p = Params.from_env()
    symbols_early = load_symbols()
    p = apply_session_for_universe(p, symbols_early)
    now = pd.Timestamp.now(tz=IST)
    today = now.strftime("%Y-%m-%d")

    if env_bool("TEST_MODE"):
        ok = tg_send(
            "✅ <b>Agent Scanner test</b>\n"
            f"Telegram link works. Config: {p.base_min}m chart, HTF {p.htf_min}m "
            f"({'on' if p.use_htf else 'off'}, max age {p.max_age}), 15m-flip trigger "
            f"{'on' if p.htf_trigger else 'off'}, RR {p.rr1:g} (TP1 only), risk {p.risk_pct:g}%."
        )
        print("test message sent" if ok else "test message FAILED")
        save_state(load_state(today))
        return

    state = load_state(today)
    if not env_bool("FORCE_RUN") and not in_market(now, p):
        print(f"{now:%Y-%m-%d %H:%M} IST - outside market hours, nothing to do")
        save_state(state)
        return

    fresh_min = int(float(os.getenv("FRESH_MIN") or 60))      # ignore signals older than this
    entry_after_raw = (os.getenv("ENTRY_AFTER") or "").strip()  # optional IST HH:MM cutoff today
    entry_after = None
    if entry_after_raw:
        entry_after = pd.Timestamp(f"{today} {entry_after_raw}", tz=IST)
    capital = float(os.getenv("CAPITAL") or 100000)
    symbols = symbols_early
    extra = f" | after {entry_after:%H:%M}" if entry_after is not None else ""
    print(f"{now:%Y-%m-%d %H:%M} IST | scanning {len(symbols)} symbols | session {p.session_start}-{p.session_end} | {p.base_min}m / HTF {p.htf_min}m | fresh<{fresh_min}m{extra}")

    data = fetch_many(symbols, p.base_min, "30d")
    print(f"  downloaded {len(data)}/{len(symbols)} symbols")
    bt = BT(warmup=250)

    sent = 0
    for sym in symbols:
        try:
            df = data.get(sym)
            if df is None:
                continue
            df = closed_only(prepare(df, p), p.base_min, now)
            if len(df) < 300:
                print(f"  {sym}: only {len(df)} bars, skipped")
                continue
            d = build_frame(df, p)
            todays = [t for t in simulate(sym, d, p, bt) if t["entry_time"].strftime("%Y-%m-%d") == today]
            for t in todays:
                ts = t["entry_time"]
                key = f"{sym}|{ts.isoformat()}"
                if key in state["sent"]:
                    continue
                if entry_after is not None and ts < entry_after:
                    continue
                age_min = (now - (ts + pd.Timedelta(minutes=p.base_min))).total_seconds() / 60
                if age_min > fresh_min:
                    print(f"  {sym}: signal at {ts:%H:%M} is stale ({age_min:.0f} min old), not sent")
                    continue
                row = d.loc[ts]
                side = 1 if t["side"] == "LONG" else -1
                entry = float(row["close"])
                sl, tp1, risk = levels(side, entry, float(row["atr"]), float(row["prev_high"]), float(row["prev_low"]), p)
                qty = calc_qty(capital, entry, risk, p)
                msg = format_signal(sym, side, ts, entry, sl, tp1, qty, p, capital, str(row["entry_type"]), age_min)
                if tg_send(msg):
                    # Persist trigger type so EOD can split 5m vs 15m flip without re-deriving
                    state["sent"][key] = {
                        "sent_at": now.isoformat(),
                        "entry_type": str(row["entry_type"]),
                        "side": t["side"],
                    }
                    sent += 1
                    print(f"  {sym}: {t['side']} alert sent ({row['entry_type']})")
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: error {exc}")

    save_state(state)
    print(f"done - {sent} alert(s) sent")
    if env_bool("HEARTBEAT"):
        tg_send(f"💓 Scan OK {now:%H:%M} IST • {len(data)}/{len(symbols)} symbols • {sent} new signal(s)")


if __name__ == "__main__":
    main()
