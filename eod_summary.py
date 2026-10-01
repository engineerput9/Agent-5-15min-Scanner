"""
eod_summary.py - End-of-day Telegram summary for signals sent today.

Runs after NSE close (~15:30 IST). Uses the same simulate() / SL / TP1 / TP2 /
commission / slippage model as the live scanner and backtest so PnL and win rate
match the engine that produced the alerts.

Trigger (recurring, Mon–Fri):
  - GitHub Actions cron ~15:40 IST (see scanner.yml eod job), and/or
  - end of the afternoon live loop (loop.py when LOOP_UNTIL >= session_end).

Dedup: state.json key "eod_sent" = YYYY-MM-DD once the summary has been posted.
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

from agent_core import (
    IST, Params, build_frame, closed_only, display_name, env_bool, fetch_many,
    load_symbols, prepare, tg_send,
)
from backtest import BT, simulate
from scanner import STATE_FILE, load_state, save_state


def todays_sent_keys(state: dict, today: str) -> list[tuple[str, str, str]]:
    """Return [(symbol, entry_iso), ...] for alerts sent on `today`."""
    out = []
    for key in state.get("sent", {}):
        if "|" not in key:
            continue
        sym, ts = key.split("|", 1)
        if ts[:10] == today:
            out.append((sym, ts, key))
    out.sort(key=lambda x: x[1])
    return out


def match_trade(trades: list[dict], entry_iso: str):
    for t in trades:
        if t["entry_time"].isoformat() == entry_iso:
            return t
        # tolerate minor tz/repr differences
        if pd.Timestamp(t["entry_time"]).isoformat() == pd.Timestamp(entry_iso).isoformat():
            return t
    return None


def collect_day_trades(today: str, p: Params, bt: BT, sent: list[tuple[str, str, str]]) -> list[dict]:
    """Re-simulate only symbols that had alerts today; keep trades matching sent keys."""
    if not sent:
        return []
    symbols = sorted({s for s, _, _ in sent})
    print(f"EOD: re-simulating {len(symbols)} symbol(s) with {len(sent)} sent alert(s)")
    data = fetch_many(symbols, p.base_min, "30d")
    now = pd.Timestamp.now(tz=IST)
    # Include a few minutes past session so the last candle is closed if available
    by_sym: dict[str, list[dict]] = {}
    for sym in symbols:
        df = data.get(sym)
        if df is None:
            print(f"  {sym}: no data")
            continue
        try:
            df = closed_only(prepare(df, p), p.base_min, now)
            if len(df) < 300:
                print(f"  {sym}: only {len(df)} bars, skipped")
                continue
            d = build_frame(df, p)
            by_sym[sym] = simulate(sym, d, p, bt)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: error {exc}")

    matched = []
    for sym, entry_iso, key in sent:
        trades = by_sym.get(sym) or []
        t = match_trade(trades, entry_iso)
        if t is None:
            # Still list the alert even if we cannot price it (data gap / still forming)
            matched.append({
                "symbol": sym, "side": "?", "entry_time": pd.Timestamp(entry_iso),
                "outcome": "NO_FILL_DATA", "pnl": 0.0, "r_multiple": 0.0,
                "hit_tp1": False, "hit_tp2": False, "hit_sl": False,
                "eod_exit": False, "fees": 0.0, "_key": key, "_priced": False,
            })
            print(f"  {sym} @{entry_iso}: no matching simulated trade")
            continue
        row = dict(t)
        row["_key"] = key
        row["_priced"] = True
        matched.append(row)
    return matched

def _is_open_trade(t: dict) -> bool:
    o = str(t.get("outcome", "")).upper()
    return "OPEN" in o or "STILL OPEN" in o


def _trade_line(t: dict) -> str:
    """One compact mobile-friendly line: time, side, symbol, PnL (R), outcome."""
    side = t.get("side", "?")
    name = display_name(t["symbol"])
    ts = pd.Timestamp(t["entry_time"]).strftime("%H:%M")
    pnl = float(t.get("pnl") or 0)
    r = float(t.get("r_multiple") or 0)
    out = str(t.get("outcome", "?"))
    out = (out.replace("Still open at data end", "open")
              .replace("TP1 + TP2", "TP1+TP2")
              .replace("TP1 then ", "TP1→")
              .replace("EOD (no target)", "EOD"))
    mark = "⏳ " if _is_open_trade(t) else ""
    return f"{mark}{ts} {side} {name}: ₹{pnl:+,.0f} ({r:+.2f}R) {out}".strip()


def _section_lines(title: str, rows: list[dict], limit: int, sort_desc: bool) -> list[str]:
    """Winners/losers block: list up to `limit`, then '+N more' with residual PnL."""
    if not rows:
        return [f"<b>{title}</b> (0)", "<i>none</i>"]
    ordered = sorted(rows, key=lambda t: float(t.get("pnl") or 0), reverse=sort_desc)
    shown = ordered[:limit]
    rest = len(ordered) - len(shown)
    rest_pnl = sum(float(t.get("pnl") or 0) for t in ordered[limit:])
    lines = [f"<b>{title}</b> ({len(ordered)})"]
    lines.extend(_trade_line(t) for t in shown)
    if rest > 0:
        lines.append(f"… +{rest} more (₹{rest_pnl:+,.0f})")
    return lines


def format_eod(today: str, trades: list[dict], p: Params, capital: float, bt: BT,
               max_each: int = 12) -> str:
    """Build EOD Telegram HTML with explicit winners & losers lists."""
    n = len(trades)
    priced = [t for t in trades if t.get("_priced", True) and t.get("outcome") != "NO_FILL_DATA"]
    openish = [t for t in priced if _is_open_trade(t)]
    wins = [t for t in priced if float(t.get("pnl") or 0) > 0]
    losses = [t for t in priced if float(t.get("pnl") or 0) <= 0]
    net = sum(float(t.get("pnl") or 0) for t in priced)
    fees = sum(float(t.get("fees") or 0) for t in priced)
    avg_r = (sum(float(t.get("r_multiple") or 0) for t in priced) / len(priced)) if priced else 0.0
    wr = (len(wins) / len(priced) * 100) if priced else 0.0
    win_pnl = sum(float(t.get("pnl") or 0) for t in wins)
    loss_pnl = sum(float(t.get("pnl") or 0) for t in losses)

    limit = max_each
    if len(priced) > 40:
        limit = min(limit, 8)
    elif len(priced) > 24:
        limit = min(limit, 10)

    lines = [
        f"📊 <b>EOD Summary – {today}</b>",
        f"<i>{p.base_min}m / HTF {p.htf_min}m • RR {p.rr1:g}/{p.rr2:g} • "
        f"risk {p.risk_pct:g}% • ₹{capital:,.0f}</i>",
        "",
        f"Signals: <b>{n}</b>  •  priced: <b>{len(priced)}</b>"
        + (f"  •  open/MTM: {len(openish)}" if openish else ""),
    ]
    if priced:
        lines += [
            f"Win rate: <b>{wr:.1f}%</b>  ({len(wins)}W / {len(losses)}L)",
            f"Net PnL: <b>₹{net:,.0f}</b>  (W ₹{win_pnl:+,.0f} / L ₹{loss_pnl:+,.0f} • "
            f"avg {avg_r:+.2f}R • fees ₹{fees:,.0f})",
        ]
    else:
        lines += ["Win rate: —", "Net PnL: —"]

    if priced:
        lines.append("")
        lines.extend(_section_lines("✅ Winners", wins, limit, sort_desc=True))
        lines.append("")
        lines.extend(_section_lines("❌ Losers", losses, limit, sort_desc=False))

    lines += [
        "",
        f"<i>Same fill model as backtest: close±slip, 50% TP1/TP2, shared SL, "
        f"comm {bt.commission_pct:g}%/side, slip {bt.slippage_ticks:g}×{bt.tick}"
        f"{'; open=MTM' if not p.eod_exit else '; EOD square-off'}.</i>",
    ]
    unpriced = n - len(priced)
    if unpriced:
        lines.append(f"⚠️ {unpriced} alert(s) could not be re-priced.")

    msg = "\n".join(lines)
    if len(msg) > 3900 and limit > 3:
        return format_eod(today, trades, p, capital, bt, max_each=limit - 1)
    return msg


def already_sent(state: dict, today: str) -> bool:
    return state.get("eod_sent") == today


def mark_sent(state: dict, today: str) -> None:
    state["eod_sent"] = today


def run(dry_run: bool = False, force: bool = False) -> int:
    p = Params.from_env()
    now = pd.Timestamp.now(tz=IST)
    today = now.strftime("%Y-%m-%d")
    # Optional override for testing a specific calendar day
    day_override = (os.getenv("EOD_DATE") or "").strip()
    if day_override:
        today = day_override

    state = load_state(today)
    if not force and not dry_run and already_sent(state, today):
        print(f"EOD summary already sent for {today}, skipping")
        return 0

    if now.weekday() >= 5 and not force and not day_override:
        print(f"{now:%Y-%m-%d %H:%M} IST - weekend, no EOD summary")
        return 0

    capital = float(os.getenv("CAPITAL") or 100000)
    bt = BT(
        capital=capital,
        commission_pct=float(os.getenv("COMMISSION") or 0.03),
        slippage_ticks=float(os.getenv("SLIPPAGE_TICKS") or 1.0),
        tick=float(os.getenv("TICK") or 0.05),
        warmup=int(float(os.getenv("WARMUP") or 250)),
    )

    sent = todays_sent_keys(state, today)
    print(f"{now:%Y-%m-%d %H:%M} IST | EOD for {today} | {len(sent)} sent alert(s)")

    if not sent:
        msg = (
            f"📊 <b>EOD Summary – {today}</b>\n"
            f"<i>Agent Confluence {p.base_min}m / HTF {p.htf_min}m</i>\n\n"
            f"Signals sent: <b>0</b>\n"
            f"Win rate: —\n"
            f"Net PnL: —\n\n"
            f"<i>No alerts were sent today.</i>"
        )
        trades = []
    else:
        trades = collect_day_trades(today, p, bt, sent)
        msg = format_eod(today, trades, p, capital, bt)

    print("--- message preview ---")
    print(msg.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", ""))
    print("--- end preview ---")

    if dry_run:
        print("DRY_RUN: not sending Telegram / not marking eod_sent")
        return 0

    ok = tg_send(msg)
    if ok:
        mark_sent(state, today)
        save_state(state)
        print("EOD summary sent")
        return 0
    print("EOD summary FAILED to send")
    return 1


def main() -> None:
    dry = env_bool("EOD_DRY_RUN") or "--dry-run" in sys.argv
    force = env_bool("FORCE_RUN") or "--force" in sys.argv
    raise SystemExit(run(dry_run=dry, force=force))


if __name__ == "__main__":
    main()
