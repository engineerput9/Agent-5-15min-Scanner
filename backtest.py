"""
backtest.py - Agent Confluence backtester (mirrors the Pine strategy).

Entry  : at the close of the signal candle (+ slippage), risk-based quantity
Exits  : 50% at TP1, 50% at TP2, both share the same SL (optional: SL -> breakeven after TP1)
         + optional end-of-day square-off (--eod); default: trades run until SL/TP, also overnight
Fills  : intrabar path model (TradingView style), gap-through fills at the open,
         commission on every fill, slippage on market/stop fills.

Usage examples
  python backtest.py                       # default universe, last 59 days, 5m / HTF 15m
  python backtest.py --compare             # HTF filter off vs on (age variants)
  python backtest.py --symbols NIFTY,BANKNIFTY,RELIANCE --telegram
  python backtest.py --csv-dir ./history   # your own longer history: SYMBOL.csv files
"""
from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass, replace

import numpy as np
import pandas as pd

from agent_core import (
    Params, build_frame, calc_qty, display_name, fetch_many, levels, load_csv,
    load_symbols, prepare, tg_send, tg_send_file,
)


@dataclass
class BT:
    capital: float = 100000.0        # starting equity per symbol (like the Pine strategy)
    commission_pct: float = 0.03     # % of value, each side
    slippage_ticks: float = 1.0
    tick: float = 0.05
    ambiguity: str = "tv"            # tv | sl_first | tp_first  (when SL and TP are both inside one candle)
    be_after_tp1: bool = False       # move SL to entry after TP1 (Pine version does NOT)
    warmup: int = 450                # bars skipped at start so the EMAs settle
    leverage: float = 1.0            # Pine default = 1 (qty capped at equity/price)


# ----------------------------------------------------------------------------
# Intrabar fill engine
# ----------------------------------------------------------------------------
def process_bar(pos: dict, o: float, h: float, l: float, c: float, bt: BT):
    """Walk the candle's price path and return fills [(price, qty, reason)]."""
    side = pos["side"]
    slip = bt.tick * bt.slippage_ticks
    if bt.ambiguity == "tv":
        high_first = (h - o) <= (o - l)          # TradingView: nearer extreme is visited first
    elif bt.ambiguity == "sl_first":
        high_first = side == -1                    # pessimistic: SL extreme first
    else:
        high_first = side == 1                     # optimistic: TP extreme first
    path = [o, h, l, c] if high_first else [o, l, h, c]
    fills = []
    for a, b in zip(path[:-1], path[1:]):
        while pos["left"] > 0:
            orders = [("sl", pos["sl"], "down" if side == 1 else "up", "stop")]
            if not pos["tp1_done"] and pos["q1"] > 0:
                orders.append(("tp1", pos["tp1"], "up" if side == 1 else "down", "limit"))
            if not pos["tp2_done"]:
                orders.append(("tp2", pos["tp2"], "up" if side == 1 else "down", "limit"))
            best = None
            for kind, lvl, trig, style in orders:
                if trig == "up":
                    hit = a if a >= lvl else (lvl if b >= lvl else None)
                else:
                    hit = a if a <= lvl else (lvl if b <= lvl else None)
                if hit is None:
                    continue
                dist = abs(hit - a)
                if best is None or dist < best[0]:
                    best = (dist, kind, hit, style)
            if best is None:
                break
            _, kind, hit, style = best
            px = hit
            if style == "stop":
                px = hit - slip if side == 1 else hit + slip
            if kind == "sl":
                q = pos["left"]
                reason = "BE" if pos["be_active"] else "SL"
            elif kind == "tp1":
                q = min(pos["q1"], pos["left"])
                pos["tp1_done"] = True
                reason = "TP1"
                if bt.be_after_tp1:
                    pos["sl"] = pos["entry_px"]
                    pos["be_active"] = True
            else:
                q = pos["left"] if pos["tp1_done"] or pos["q1"] == 0 else min(pos["q2"], pos["left"])
                pos["tp2_done"] = True
                reason = "TP2"
            pos["left"] -= q
            fills.append((px, q, reason))
            a = hit
    return fills


def simulate(sym: str, d: pd.DataFrame, p: Params, bt: BT) -> list[dict]:
    O, H, L, C = (d[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    atr, ph, pl = (d[k].to_numpy(float) for k in ("atr", "prev_high", "prev_low"))
    raw_l, raw_s = d["raw_long"].to_numpy(bool), d["raw_short"].to_numpy(bool)
    is_last = d["is_last"].to_numpy(bool)
    htf_bias = d["htf_bias"].to_numpy(float)
    htf_age = d["htf_age"].to_numpy(float)
    entry_type = d["entry_type"].to_numpy(str)
    idx = d.index
    dates = idx.strftime("%Y-%m-%d").to_numpy()
    slip = bt.tick * bt.slippage_ticks
    comm = bt.commission_pct / 100
    equity = bt.capital
    trades: list[dict] = []
    pos = None
    traded_date = None

    def close_trade(pos, i):
        nonlocal equity
        s = pos["side"]
        gross = sum(s * (px - pos["entry_px"]) * q for _, px, q, _ in pos["fills"])
        fees = comm * (pos["entry_px"] * pos["qty"] + sum(px * q for _, px, q, _ in pos["fills"]))
        pnl = gross - fees
        equity += pnl
        reasons = [r for *_, r in pos["fills"]]
        if reasons == ["SL"]:
            label = "SL"
        elif reasons == ["EOD"]:
            label = "EOD (no target)"
        elif reasons == ["OPEN"]:
            label = "Still open at data end"
        elif reasons[0] == "TP1" and reasons[-1] == "TP2":
            label = "TP1 + TP2"
        elif reasons[0] == "TP1":
            label = f"TP1 then {reasons[-1]}"
        elif reasons == ["TP2"]:
            label = "TP2 only"
        else:
            label = " > ".join(reasons)
        rk = pos["risk"] * pos["qty"]
        trades.append({
            "symbol": sym, "side": "LONG" if s == 1 else "SHORT",
            "entry_time": idx[pos["i0"]], "exit_time": pos["fills"][-1][0],
            "entry": round(pos["entry_px"], 2), "sl": round(pos["sl0"], 2),
            "tp1": round(pos["tp1"], 2), "tp2": round(pos["tp2"], 2),
            "qty": pos["qty"], "risk_per_unit": round(pos["risk"], 2),
            "entry_type": entry_type[pos["i0"]], "outcome": label, "hit_sl": "SL" in reasons, "hit_be": "BE" in reasons,
            "hit_tp1": "TP1" in reasons, "hit_tp2": "TP2" in reasons, "eod_exit": "EOD" in reasons,
            "avg_exit": round(sum(px * q for _, px, q, _ in pos["fills"]) / pos["qty"], 2),
            "gross_pnl": round(gross, 2), "fees": round(fees, 2), "pnl": round(pnl, 2),
            "r_multiple": round(pnl / rk, 3) if rk else 0.0,
            "mfe_r": round(pos["mfe"] / pos["risk"], 2), "mae_r": round(pos["mae"] / pos["risk"], 2),
            "bars_held": i - pos["i0"], "equity_after": round(equity, 2),
            "htf_bias": htf_bias[pos["i0"]], "htf_age": htf_age[pos["i0"]],
        })

    for i in range(len(d)):
        if pos is not None:
            f = process_bar(pos, O[i], H[i], L[i], C[i], bt)
            for px, q, r in f:
                pos["fills"].append((idx[i], px, q, r))
            s = pos["side"]
            pos["mfe"] = max(pos["mfe"], (H[i] - pos["entry_px"]) if s == 1 else (pos["entry_px"] - L[i]))
            pos["mae"] = max(pos["mae"], (pos["entry_px"] - L[i]) if s == 1 else (H[i] - pos["entry_px"]))
            if pos["left"] > 0 and p.eod_exit and is_last[i]:
                px = C[i] - slip if s == 1 else C[i] + slip
                pos["fills"].append((idx[i], px, pos["left"], "EOD"))
                pos["left"] = 0
            if pos["left"] == 0:
                close_trade(pos, i)
                pos = None
        if pos is None and i >= bt.warmup and (raw_l[i] or raw_s[i]):
            if p.one_trade_day and traded_date == dates[i]:
                continue
            side = 1 if raw_l[i] else -1
            sl, tp1, tp2, risk = levels(side, C[i], atr[i], ph[i], pl[i], p)
            qty = calc_qty(equity, C[i], risk, p, bt.leverage)
            if qty <= 0 or risk <= 0:
                continue
            entry_px = C[i] + side * slip
            q1 = qty // 2
            pos = {"side": side, "i0": i, "entry_px": entry_px, "sl": sl, "sl0": sl, "tp1": tp1,
                   "tp2": tp2, "qty": qty, "left": qty, "q1": q1, "q2": qty - q1,
                   "tp1_done": False, "tp2_done": False, "be_active": False,
                   "risk": risk, "fills": [], "mfe": 0.0, "mae": 0.0}
            traded_date = dates[i]

    if pos is not None:                      # data ended while in a trade (only if EOD exit is off)
        pos["fills"].append((idx[-1], C[-1], pos["left"], "OPEN"))
        pos["left"] = 0
        close_trade(pos, len(d) - 1)
    return trades


# ----------------------------------------------------------------------------
# Statistics
# ----------------------------------------------------------------------------
def stats(t: pd.DataFrame, capital: float, nsym: int, all_dates) -> dict:
    if t.empty:
        return {"trades": 0}
    w, l = t[t.pnl > 0], t[t.pnl <= 0]
    gl = -l.pnl.sum()
    ts = t.sort_values("exit_time")
    base = capital * max(nsym, 1)
    eq = base + ts.pnl.cumsum()
    peak = np.maximum.accumulate(np.r_[base, eq.to_numpy()])[1:]
    dd = eq.to_numpy() - peak
    daily = ts.groupby(ts.exit_time.dt.strftime("%Y-%m-%d")).pnl.sum().reindex(list(all_dates), fill_value=0) / base
    sharpe = daily.mean() / daily.std() * math.sqrt(252) if daily.std() > 0 else float("nan")
    streak = best = 0
    for v in ts.pnl.to_numpy():
        streak = streak + 1 if v <= 0 else 0
        best = max(best, streak)
    n = len(t)
    return {
        "trades": n,
        "win_rate_%": round(len(w) / n * 100, 1),
        "sl_hit_%": round(t.hit_sl.mean() * 100, 1),
        "tp1_hit_%": round(t.hit_tp1.mean() * 100, 1),
        "tp2_hit_%": round(t.hit_tp2.mean() * 100, 1),
        "eod_exit_%": round(t.eod_exit.mean() * 100, 1),
        "net_pnl": round(t.pnl.sum(), 0),
        "return_%_on_capital": round(t.pnl.sum() / base * 100, 2),
        "profit_factor": round(w.pnl.sum() / gl, 2) if gl > 0 else float("inf"),
        "expectancy_R": round(t.r_multiple.mean(), 3),
        "avg_win_R": round(w.r_multiple.mean(), 2) if len(w) else 0,
        "avg_loss_R": round(l.r_multiple.mean(), 2) if len(l) else 0,
        "avg_pnl": round(t.pnl.mean(), 1),
        "best_trade": round(t.pnl.max(), 0),
        "worst_trade": round(t.pnl.min(), 0),
        "max_drawdown": round(dd.min(), 0),
        "max_drawdown_%": round((dd / peak).min() * 100, 2),
        "max_consec_losses": best,
        "sharpe_daily": round(sharpe, 2) if not math.isnan(sharpe) else None,
        "avg_bars_held": round(t.bars_held.mean(), 1),
        "fees_paid": round(t.fees.sum(), 0),
        "still_open_at_data_end": int(t.outcome.str.contains("OPEN").sum()),
        "avg_days_held": round(((t.exit_time - t.entry_time).dt.total_seconds() / 86400).mean(), 2),
    }


def breakdown(t: pd.DataFrame, key) -> pd.DataFrame:
    g = t.groupby(key)
    out = pd.DataFrame({
        "trades": g.size(),
        "win_%": g.apply(lambda x: round((x.pnl > 0).mean() * 100, 1)),
        "tp1_%": g.apply(lambda x: round(x.hit_tp1.mean() * 100, 1)),
        "tp2_%": g.apply(lambda x: round(x.hit_tp2.mean() * 100, 1)),
        "sl_%": g.apply(lambda x: round(x.hit_sl.mean() * 100, 1)),
        "avg_R": g.r_multiple.mean().round(2),
        "net_pnl": g.pnl.sum().round(0),
        "PF": g.apply(lambda x: round(x[x.pnl > 0].pnl.sum() / -x[x.pnl <= 0].pnl.sum(), 2)
                      if (x.pnl <= 0).any() and x[x.pnl <= 0].pnl.sum() != 0 else float("inf")),
    })
    return out.reset_index()


def md_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_no data_\n"
    cols = [str(c) for c in df.columns]
    lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        lines.append("| " + " | ".join(str(v) for v in row) + " |")
    return "\n".join(lines) + "\n"


def plot_report(t: pd.DataFrame, capital: float, nsym: int, path: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ts = t.sort_values("exit_time").reset_index(drop=True)
    base = capital * max(nsym, 1)
    eq = base + ts.pnl.cumsum()
    dd = eq - eq.cummax().clip(lower=base)
    fig, ax = plt.subplots(2, 2, figsize=(13, 8))
    ax[0, 0].plot(ts.exit_time, eq, lw=1.5)
    ax[0, 0].set_title("Equity curve (all symbols)")
    ax[0, 1].fill_between(ts.exit_time, dd, 0, color="crimson", alpha=.5)
    ax[0, 1].set_title("Drawdown (Rs)")
    oc = t.outcome.value_counts()
    ax[1, 0].barh(oc.index[::-1], oc.values[::-1], color="steelblue")
    ax[1, 0].set_title("Trade outcomes")
    ax[1, 1].hist(t.r_multiple, bins=30, color="seagreen")
    ax[1, 1].axvline(0, color="k", lw=.8)
    ax[1, 1].set_title("R-multiple distribution")
    for a in ax.flat:
        a.grid(alpha=.25)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------
# Runner
# ----------------------------------------------------------------------------
def run(data: dict, p: Params, bt: BT) -> pd.DataFrame:
    rows = []
    for sym, df in data.items():
        try:
            d = build_frame(df, p)
            rows += simulate(sym, d, p, bt)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: error {exc}")
    return pd.DataFrame(rows)


def main() -> None:
    ap = argparse.ArgumentParser(description="Agent Confluence backtest")
    ap.add_argument("--symbols", help="comma list (NIFTY,BANKNIFTY,RELIANCE...) - default: symbols.txt / built-in list")
    ap.add_argument("--days", type=int, help="history in days (Yahoo limit: 59 for <60m candles)")
    ap.add_argument("--csv-dir", help="folder with SYMBOL.csv files instead of Yahoo")
    ap.add_argument("--out", default="out")
    ap.add_argument("--compare", action="store_true", help="compare HTF filter off / on / max-age variants")
    ap.add_argument("--telegram", action="store_true", help="send summary + chart to Telegram")
    ap.add_argument("--capital", type=float, default=100000)
    ap.add_argument("--commission", type=float, default=0.03, help="percent per side")
    ap.add_argument("--slippage-ticks", type=float, default=1.0)
    ap.add_argument("--tick", type=float, default=0.05)
    ap.add_argument("--ambiguity", choices=["tv", "sl_first", "tp_first"], default="tv")
    ap.add_argument("--be-after-tp1", action="store_true")
    ap.add_argument("--warmup", type=int, default=450)
    ap.add_argument("--leverage", type=float, default=1.0)
    for name, typ in [("per", int), ("mult", float), ("base-min", int), ("htf-min", int), ("max-age", int),
                      ("rr1", float), ("rr2", float), ("risk-pct", float), ("atr-mult", float),
                      ("swing-lookback", int), ("atr-length", int)]:
        ap.add_argument(f"--{name}", type=typ)
    ap.add_argument("--no-htf", action="store_true")
    ap.add_argument("--eod", action="store_true", help="square off at the end of each session (default: trades run until SL/TP2)")
    ap.add_argument("--one-trade-day", action="store_true", help="limit to one trade per symbol per day (default: no limit)")
    ap.add_argument("--no-htf-trigger", action="store_true", help="disable the 'HTF flips while chart TF already aligned' entry")
    ap.add_argument("--long-only", action="store_true")
    ap.add_argument("--short-only", action="store_true")
    a = ap.parse_args()

    p = Params.from_env()
    for k in ("per", "mult", "base_min", "htf_min", "max_age", "rr1", "rr2", "risk_pct", "atr_mult",
              "swing_lookback", "atr_length"):
        v = getattr(a, k)
        if v is not None:
            setattr(p, k, v)
    if a.no_htf:
        p.use_htf = False
    if a.eod:
        p.eod_exit = True
    if a.one_trade_day:
        p.one_trade_day = True
    if a.no_htf_trigger:
        p.htf_trigger = False
    if a.long_only:
        p.allow_short = False
    if a.short_only:
        p.allow_long = False
    bt = BT(a.capital, a.commission, a.slippage_ticks, a.tick, a.ambiguity, a.be_after_tp1, a.warmup, a.leverage)

    if a.symbols:
        os.environ["SYMBOLS"] = a.symbols
    symbols = load_symbols()
    days = a.days or (59 if p.base_min < 60 else 365)
    os.makedirs(a.out, exist_ok=True)

    print(f"Loading {len(symbols)} symbols ...")
    raw: dict = {}
    if a.csv_dir:
        for s in symbols:
            fp = os.path.join(a.csv_dir, s.replace(".NS", "").replace("^", "") + ".csv")
            if os.path.exists(fp):
                raw[s] = load_csv(fp)
    else:
        raw = fetch_many(symbols, p.base_min, f"{days}d")
    data = {}
    for s, df in raw.items():
        df = prepare(df, p)
        if len(df) < bt.warmup + 100:
            print(f"  {s}: not enough data, skipped")
            continue
        data[s] = df
    if not data:
        raise SystemExit("No data loaded.")
    all_dates = sorted({x for df in data.values() for x in df.index.strftime("%Y-%m-%d")})
    span = f"{all_dates[0]} -> {all_dates[-1]} ({len(all_dates)} sessions)"
    print(f"{len(data)} symbols, {span}")

    trades = run(data, p, bt)
    if trades.empty:
        raise SystemExit("No trades generated. Try more history, --warmup 300, or --no-htf.")
    trades["entry_time"] = pd.to_datetime(trades["entry_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    trades["exit_time"] = pd.to_datetime(trades["exit_time"], utc=True).dt.tz_convert("Asia/Kolkata")
    trades["weekday"] = trades.entry_time.dt.day_name()
    trades["entry_hour"] = trades.entry_time.dt.hour
    trades["month"] = trades.entry_time.dt.strftime("%Y-%m")
    trades = trades.sort_values("entry_time")
    trades.to_csv(os.path.join(a.out, "trades.csv"), index=False)

    st = stats(trades, bt.capital, len(data), all_dates)
    with open(os.path.join(a.out, "summary.json"), "w") as fh:
        json.dump(st, fh, indent=2, default=str)
    plot_report(trades, bt.capital, len(data), os.path.join(a.out, "equity.png"))

    outcome = trades.outcome.value_counts().rename_axis("outcome").reset_index(name="trades")
    outcome["share_%"] = (outcome.trades / len(trades) * 100).round(1)
    sym_tbl = breakdown(trades, "symbol").sort_values("net_pnl", ascending=False)
    sym_tbl["symbol"] = sym_tbl.symbol.map(display_name)

    md = [f"# Agent Confluence backtest\n",
          f"Data: {span} | {p.base_min}m chart, HTF {p.htf_min}m {'ON' if p.use_htf else 'OFF'} (max age {p.max_age}) | "
          f"15m-flip trigger {'ON' if p.htf_trigger else 'OFF'} | one trade/day {'ON' if p.one_trade_day else 'OFF'} | EOD square-off {'ON' if p.eod_exit else 'OFF'} | RR {p.rr1:g}/{p.rr2:g} | risk {p.risk_pct:g}% | commission {bt.commission_pct}%/side | "
          f"slippage {bt.slippage_ticks:g} tick | ambiguity `{bt.ambiguity}` | BE after TP1: {bt.be_after_tp1} | EOD square-off: {'ON' if p.eod_exit else 'OFF (trades run)'}\n",
          "## Summary\n", md_table(pd.DataFrame([st]).T.reset_index().rename(columns={"index": "metric", 0: "value"})),
          "## Outcomes (what price did after entry)\n", md_table(outcome),
          "## By entry type\n", md_table(breakdown(trades, "entry_type")),
          "## By side\n", md_table(breakdown(trades, "side")),
          "## By weekday\n", md_table(breakdown(trades, "weekday")),
          "## By entry hour\n", md_table(breakdown(trades, "entry_hour")),
          "## By month\n", md_table(breakdown(trades, "month")),
          "## By symbol\n", md_table(sym_tbl)]

    cmp_rows = []
    if a.compare:
        variants = [("HTF off (baseline)", replace(p, use_htf=False)),
                    ("HTF on, any age", replace(p, use_htf=True, max_age=0)),
                    ("HTF on, age<=1", replace(p, use_htf=True, max_age=1)),
                    ("HTF on, age<=2", replace(p, use_htf=True, max_age=2)),
                    ("HTF on, age<=4", replace(p, use_htf=True, max_age=4)),
                    ("age<=2, no 15m-flip trigger", replace(p, use_htf=True, max_age=2, htf_trigger=False)),
                    ("age<=2, one trade/day", replace(p, use_htf=True, max_age=2, one_trade_day=True))]
        for name, cfg in variants:
            tt = run(data, cfg, bt)
            if tt.empty:
                cmp_rows.append({"variant": name, "trades": 0})
                continue
            tt["exit_time"] = pd.to_datetime(tt["exit_time"], utc=True).dt.tz_convert("Asia/Kolkata")
            s2 = stats(tt, bt.capital, len(data), all_dates)
            cmp_rows.append({"variant": name, "trades": s2["trades"], "win_%": s2["win_rate_%"],
                             "tp1_%": s2["tp1_hit_%"], "tp2_%": s2["tp2_hit_%"], "sl_%": s2["sl_hit_%"],
                             "PF": s2["profit_factor"], "exp_R": s2["expectancy_R"],
                             "net_pnl": s2["net_pnl"], "max_dd_%": s2["max_drawdown_%"]})
        cmp_df = pd.DataFrame(cmp_rows)
        cmp_df.to_csv(os.path.join(a.out, "compare.csv"), index=False)
        md += ["## Filter comparison\n", md_table(cmp_df)]
        print("\n" + cmp_df.to_string(index=False))

    with open(os.path.join(a.out, "report.md"), "w") as fh:
        fh.write("\n".join(md))

    print("\n=== SUMMARY ===")
    for k, v in st.items():
        print(f"{k:>22}: {v}")
    print("\n" + outcome.to_string(index=False))
    print(f"\nFiles written to ./{a.out}/  (trades.csv, summary.json, report.md, equity.png)")

    if a.telegram:
        lines = [f"📊 <b>Agent Confluence backtest</b>", f"<i>{span}</i>",
                 f"{len(data)} symbols • {p.base_min}m / HTF {p.htf_min}m {'on' if p.use_htf else 'off'} • RR {p.rr1:g}/{p.rr2:g}", "",
                 f"Trades: <b>{st['trades']}</b> • Win {st['win_rate_%']}% • PF {st['profit_factor']}",
                 f"TP1 hit {st['tp1_hit_%']}% • TP2 hit {st['tp2_hit_%']}% • SL hit {st['sl_hit_%']}% • EOD {st['eod_exit_%']}%",
                 f"Net P&L ₹{st['net_pnl']:,.0f} ({st['return_%_on_capital']}%) • Expectancy {st['expectancy_R']}R",
                 f"Max DD {st['max_drawdown_%']}% • Max losing streak {st['max_consec_losses']}", "",
                 "<b>Outcomes</b>"]
        lines += [f"{r.outcome}: {r.trades} ({r._3}%)" for r in outcome.itertuples()]
        tg_send("\n".join(lines))
        tg_send_file(os.path.join(a.out, "equity.png"), "Equity / drawdown / outcomes")
        tg_send_file(os.path.join(a.out, "trades.csv"), "All trades")


if __name__ == "__main__":
    main()
