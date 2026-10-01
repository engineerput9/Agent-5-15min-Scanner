"""
eod_summary.py - End-of-day Telegram summary for signals sent today.

Runs after the MCX evening window (~23:30 IST) when commodities are enabled
(else after NSE close). Uses the same simulate() / SL / TP1 / commission /
slippage model as the live scanner and backtest so PnL and win rate match the
engine that produced the alerts.

Trigger (recurring, Mon–Fri):
  - GitHub Actions cron ~23:40 IST (see scanner.yml EOD cron), and/or
  - end of the last live loop when LOOP_UNTIL >= session_end (evening ~23:32
    with commodities; afternoon ~15:32 for equity-only).

Dedup: state.json key "eod_sent" = YYYY-MM-DD once the summary has been posted.
"""
from __future__ import annotations

import json
import os
import sys

import pandas as pd

from agent_core import (
    IST, Params, apply_session_for_universe, build_frame, closed_only,
    display_name, env_bool, fetch_many, load_symbols, prepare, tg_send,
)
from backtest import BT, simulate
from scanner import STATE_FILE, load_state, save_state


def sent_meta(raw) -> dict:
    """Normalize state['sent'][key] (legacy ISO string or dict with entry_type)."""
    if isinstance(raw, dict):
        return {
            "sent_at": raw.get("sent_at") or raw.get("at") or "",
            "entry_type": (raw.get("entry_type") or "").strip() or None,
            "side": raw.get("side"),
        }
    return {"sent_at": str(raw) if raw is not None else "", "entry_type": None, "side": None}


def todays_sent_keys(state: dict, today: str) -> list[tuple[str, str, str, dict]]:
    """Return [(symbol, entry_iso, key, meta), ...] for alerts sent on `today`."""
    out = []
    for key, raw in state.get("sent", {}).items():
        if "|" not in key:
            continue
        sym, ts = key.split("|", 1)
        if ts[:10] == today:
            out.append((sym, ts, key, sent_meta(raw)))
    out.sort(key=lambda x: x[1])
    return out


def match_trade(trades: list[dict], entry_iso: str):
    for t in trades:
        if t["entry_time"].isoformat() == entry_iso:
            return t
        if pd.Timestamp(t["entry_time"]).isoformat() == pd.Timestamp(entry_iso).isoformat():
            return t
    return None


def collect_day_trades(today: str, p: Params, bt: BT,
                       sent: list[tuple[str, str, str, dict]]) -> list[dict]:
    """Re-simulate symbols with alerts today; prefer state entry_type when present."""
    if not sent:
        return []
    symbols = sorted({s for s, _, _, _ in sent})
    print(f"EOD: re-simulating {len(symbols)} symbol(s) with {len(sent)} sent alert(s)")
    data = fetch_many(symbols, p.base_min, "30d")
    now = pd.Timestamp.now(tz=IST)
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
    for sym, entry_iso, key, meta in sent:
        trades = by_sym.get(sym) or []
        t = match_trade(trades, entry_iso)
        if t is None:
            matched.append({
                "symbol": sym,
                "side": meta.get("side") or "?",
                "entry_time": pd.Timestamp(entry_iso),
                "entry_type": meta.get("entry_type") or "?",
                "outcome": "NO_FILL_DATA", "pnl": 0.0, "r_multiple": 0.0,
                "hit_tp1": False, "hit_sl": False,
                "eod_exit": False, "fees": 0.0, "_key": key, "_priced": False,
            })
            print(f"  {sym} @{entry_iso}: no matching simulated trade")
            continue
        row = dict(t)
        # Prefer trigger type recorded at send time; fall back to re-sim
        if meta.get("entry_type"):
            row["entry_type"] = meta["entry_type"]
        if meta.get("side") and not row.get("side"):
            row["side"] = meta["side"]
        row["_key"] = key
        row["_priced"] = True
        matched.append(row)
    return matched


def backfill_entry_types(state: dict, trades: list[dict]) -> int:
    """Write entry_type/side from priced trades into state['sent'] (upgrade legacy strings)."""
    n = 0
    sent = state.setdefault("sent", {})
    for t in trades:
        key = t.get("_key")
        if not key or key not in sent:
            continue
        et = t.get("entry_type")
        if not et or et == "?":
            continue
        meta = sent_meta(sent[key])
        if meta.get("entry_type") == et and meta.get("side") == t.get("side"):
            if isinstance(sent[key], dict):
                continue
        sent[key] = {
            "sent_at": meta.get("sent_at") or "",
            "entry_type": str(et),
            "side": t.get("side"),
        }
        n += 1
    return n




def _asset_class(sym: str) -> str:
    """Equity/index vs commodity futures proxy (Yahoo =F)."""
    s = str(sym)
    if s.endswith("=F"):
        return "commodity"
    return "equity"


def _is_open_trade(t: dict) -> bool:
    o = str(t.get("outcome", "")).upper()
    return "OPEN" in o or "STILL OPEN" in o


def _trade_line(t: dict) -> str:
    """One compact mobile-friendly line: time, side, MCX/equity display name, PnL."""
    side = t.get("side", "?")
    name = display_name(t["symbol"])  # GC=F → GOLD, etc.
    ts = pd.Timestamp(t["entry_time"]).strftime("%H:%M")
    pnl = float(t.get("pnl") or 0)
    r = float(t.get("r_multiple") or 0)
    out = str(t.get("outcome", "?"))
    out = (out.replace("Still open at data end", "open")
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


def _trigger_stats_line(label: str, rows: list[dict]) -> str:
    """One line: count, W/L, win rate, net PnL for a trigger subset."""
    if not rows:
        return f"{label}: <b>0</b>  —"
    wins = [t for t in rows if float(t.get("pnl") or 0) > 0]
    losses = [t for t in rows if float(t.get("pnl") or 0) <= 0]
    net = sum(float(t.get("pnl") or 0) for t in rows)
    wr = len(wins) / len(rows) * 100
    return (
        f"{label}: <b>{len(rows)}</b>  •  {len(wins)}W/{len(losses)}L  •  "
        f"<b>{wr:.1f}%</b>  •  ₹{net:+,.0f}"
    )


def _class_stats_line(rows: list[dict]) -> str:
    """Compact WR / net for an equities or commodities bucket."""
    if not rows:
        return "priced: <b>0</b>  —"
    wins = [t for t in rows if float(t.get("pnl") or 0) > 0]
    losses = [t for t in rows if float(t.get("pnl") or 0) <= 0]
    net = sum(float(t.get("pnl") or 0) for t in rows)
    wr = len(wins) / len(rows) * 100
    openish = [t for t in rows if _is_open_trade(t)]
    extra = f"  •  open/MTM: {len(openish)}" if openish else ""
    return (
        f"priced: <b>{len(rows)}</b>  •  {len(wins)}W/{len(losses)}L  •  "
        f"<b>{wr:.1f}%</b>  •  Net ₹{net:+,.0f}{extra}"
    )


def _asset_block(title: str, rows: list[dict], limit: int) -> list[str]:
    """One asset-class section: stats + winners/losers (or empty note)."""
    lines = [f"<b>{title}</b>", _class_stats_line(rows)]
    if not rows:
        lines.append("<i>none today</i>")
        return lines
    wins = [t for t in rows if float(t.get("pnl") or 0) > 0]
    losses = [t for t in rows if float(t.get("pnl") or 0) <= 0]
    lines.append("")
    lines.extend(_section_lines("✅ Winners", wins, limit, sort_desc=True))
    lines.append("")
    lines.extend(_section_lines("❌ Losers", losses, limit, sort_desc=False))
    return lines


def format_eod(today: str, trades: list[dict], p: Params, capital: float, bt: BT,
               max_each: int = 10) -> str:
    """Build EOD Telegram HTML: overall + by-trigger, then Equities vs MCX sections."""
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

    five = [t for t in priced if t.get("entry_type") == "5m flip"]
    fifteen = [t for t in priced if t.get("entry_type") == "15m flip"]
    other = [t for t in priced if t.get("entry_type") not in ("5m flip", "15m flip")]

    eq_all = [t for t in trades if _asset_class(t.get("symbol", "")) == "equity"]
    co_all = [t for t in trades if _asset_class(t.get("symbol", "")) == "commodity"]
    eq_priced = [t for t in priced if _asset_class(t.get("symbol", "")) == "equity"]
    co_priced = [t for t in priced if _asset_class(t.get("symbol", "")) == "commodity"]

    limit = max_each
    if len(priced) > 40:
        limit = min(limit, 6)
    elif len(priced) > 24:
        limit = min(limit, 8)
    # Shared budget across two asset sections
    per_class = max(3, limit // 2) if (eq_priced and co_priced) else limit

    lines = [
        f"📊 <b>EOD Summary – {today}</b>",
        f"<i>{p.base_min}m / HTF {p.htf_min}m • RR {p.rr1:g} (TP1 only) • "
        f"risk {p.risk_pct:g}% • ₹{capital:,.0f}</i>",
        "",
        f"Signals: <b>{n}</b>  •  priced: <b>{len(priced)}</b>"
        + (f"  •  open/MTM: {len(openish)}" if openish else ""),
        f"Universe: equities <b>{len(eq_all)}</b>  •  commodities <b>{len(co_all)}</b>",
    ]
    if priced:
        lines += [
            f"Overall: <b>{wr:.1f}%</b>  ({len(wins)}W / {len(losses)}L)  •  "
            f"Net <b>₹{net:,.0f}</b>  (W ₹{win_pnl:+,.0f} / L ₹{loss_pnl:+,.0f} • "
            f"avg {avg_r:+.2f}R • fees ₹{fees:,.0f})",
            "",
            "<b>By trigger</b>",
            _trigger_stats_line("5m flip (15m aligned)", five),
            _trigger_stats_line("15m flip (5m aligned)", fifteen),
        ]
        if other:
            lines.append(_trigger_stats_line("other / unknown", other))
    else:
        lines += ["Overall: —", "", "<b>By trigger</b>", "5m flip: 0  —", "15m flip: 0  —"]

    # Equities (main) and MCX / Commodities as distinct blocks
    lines.append("")
    lines.extend(_asset_block("📈 Equities", eq_priced, per_class))
    lines.append("")
    lines.extend(_asset_block("🛢️ MCX / Commodities", co_priced, per_class))

    lines += [
        "",
        f"<i>Same fill model as backtest: close±slip, full size at TP1, shared SL, "
        f"comm {bt.commission_pct:g}%/side, slip {bt.slippage_ticks:g}×{bt.tick}"
        f"{'; open=MTM' if not p.eod_exit else '; EOD square-off'}. "
        f"Commodities shown with MCX-style names (GOLD, SILVER, …). "
        f"Trigger type from state at send (fallback: re-sim).</i>",
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
    p = apply_session_for_universe(p)
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
        n_bf = backfill_entry_types(state, trades)
        if n_bf:
            print(f"EOD: backfilled entry_type on {n_bf} state alert(s)")
            save_state(state)
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
