"""
agent_core.py  -  Agent Confluence core
Python port of the TradingView Pine v6 strategy "Agent Confluence Backtest"
(Range Filter + higher-timeframe confirmation) + data loading + Telegram helpers.
Used by scanner.py (live alerts) and backtest.py.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, fields

import numpy as np
import pandas as pd
import requests

IST = "Asia/Kolkata"
# Approx MCX / overnight futures hours (IST). Equity stays 09:15–15:30 unless overridden.
COMMODITY_SESSION_START = "09:00"
COMMODITY_SESSION_END = "23:30"

NAMES = {
    "^NSEI": "NIFTY 50",
    "^NSEBANK": "BANK NIFTY",
    "NIFTY_MID_SELECT.NS": "MIDCAP NIFTY",
    # Yahoo continuous futures → MCX-style display/alert names
    "GC=F": "GOLD",
    "SI=F": "SILVER",
    "CL=F": "CRUDEOIL",
    "HG=F": "COPPER",
    "NG=F": "NATURALGAS",
}
ALIASES = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "MIDCPNIFTY": "NIFTY_MID_SELECT.NS",
}

# Starter universe. Put your full F&O list in symbols.txt (one per line) to override.
DEFAULT_SYMBOLS = [
    "^NSEI", "^NSEBANK", "NIFTY_MID_SELECT.NS",
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "SBIN", "BHARTIARTL",
    "ITC", "LT", "KOTAKBANK", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI",
    "SUNPHARMA", "TATASTEEL", "NTPC", "POWERGRID", "ONGC", "ADANIENT",
    "ADANIPORTS", "ASIANPAINT", "TITAN", "ULTRACEMCO", "WIPRO", "HCLTECH",
    "TECHM", "JSWSTEEL", "HINDALCO", "COALINDIA", "M&M", "BAJAJFINSV",
    "INDUSINDBK", "CIPLA", "DRREDDY", "EICHERMOT", "GRASIM", "BPCL", "DLF",
    "BEL", "HAL", "TRENT",
]


# ----------------------------------------------------------------------------
# Parameters (defaults = Pine inputs). Override with env vars (upper-case names)
# ----------------------------------------------------------------------------
@dataclass
class Params:
    per: int = 100               # Sampling Period
    mult: float = 3.0            # Range Multiplier
    use_htf: bool = True         # Require higher-TF confirmation
    base_min: int = 5            # chart timeframe in minutes
    htf_min: int = 15            # confirmation timeframe in minutes
    max_age: int = 2             # HTF flip must be within this many HTF bars (0 = any age)
    swing_lookback: int = 10
    atr_length: int = 14
    atr_mult: float = 1.0
    rr1: float = 1.2             # sole target RR (full size)
    risk_pct: float = 1.0        # risk per trade, % of equity
    one_trade_day: bool = False  # no daily limit; a running trade simply continues (no overlap)
    htf_trigger: bool = False    # OFF by default: live scanner only fires on 5m RF flip when HTF already aligned (htf_age ≤ MAX_AGE). Set HTF_TRIGGER=true to also signal when HTF flips and chart TF is already aligned
    eod_exit: bool = False       # False = no square-off, the trade runs until SL / TP1
    allow_long: bool = True
    allow_short: bool = True
    session_start: str = "09:15"
    session_end: str = "15:30"

    @classmethod
    def from_env(cls) -> "Params":
        p = cls()
        for f in fields(cls):
            raw = os.getenv(f.name.upper())
            if raw is None or raw.strip() == "":
                continue
            cur = getattr(p, f.name)
            if isinstance(cur, bool):
                val = raw.strip().lower() in ("1", "true", "yes", "y", "on")
            elif isinstance(cur, int):
                val = int(float(raw))
            elif isinstance(cur, float):
                val = float(raw)
            else:
                val = raw.strip()
            setattr(p, f.name, val)
        return p


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


# ----------------------------------------------------------------------------
# Symbols
# ----------------------------------------------------------------------------
def _read_symbol_file(path: str) -> list[str]:
    if not os.path.exists(path):
        return []
    with open(path) as fh:
        return [ln.split("#")[0].strip() for ln in fh]


def _normalize_symbol(s: str) -> str | None:
    s = s.strip().upper()
    if not s:
        return None
    s = ALIASES.get(s, s)
    if not (s.startswith("^") or s.endswith((".NS", ".BO", "=F"))):
        s += ".NS"
    return s


def load_symbols(path: str = "symbols.txt", commodities_path: str = "commodities.txt") -> list[str]:
    """Load equity/F&O list; optionally merge commodities.txt (INCLUDE_COMMODITIES=true).

    SYMBOLS env (comma list) overrides files entirely. Futures tickers end with =F.
    """
    env = os.getenv("SYMBOLS")
    raw: list[str] = []
    if env and env.strip():
        raw = [s.strip() for s in env.split(",")]
    else:
        raw = _read_symbol_file(path)
        if env_bool("INCLUDE_COMMODITIES", False):
            raw = list(raw) + _read_symbol_file(commodities_path)
        # Commodities-only mode: COMMODITIES_ONLY=true uses only commodities.txt
        if env_bool("COMMODITIES_ONLY", False):
            raw = _read_symbol_file(commodities_path)
    if not raw or not any(raw):
        raw = list(DEFAULT_SYMBOLS)
    out = []
    for s in raw:
        n = _normalize_symbol(s)
        if n and n not in out:
            out.append(n)
    return out


def has_futures(symbols: list[str] | None = None) -> bool:
    syms = symbols if symbols is not None else load_symbols()
    return any(s.endswith("=F") for s in syms)


def apply_session_for_universe(p: "Params", symbols: list[str] | None = None) -> "Params":
    """If futures are in the universe and session was left at equity defaults, widen to MCX hours.

    Explicit SESSION_START / SESSION_END env always wins (Params.from_env already applied).
    """
    if not has_futures(symbols):
        return p
    start_set = os.getenv("SESSION_START") not in (None, "")
    end_set = os.getenv("SESSION_END") not in (None, "")
    if not start_set and p.session_start == "09:15":
        p.session_start = os.getenv("COMMODITY_SESSION_START") or COMMODITY_SESSION_START
    if not end_set and p.session_end == "15:30":
        p.session_end = os.getenv("COMMODITY_SESSION_END") or COMMODITY_SESSION_END
    return p


def display_name(sym: str) -> str:
    if sym in NAMES:
        return NAMES[sym]
    return sym.replace(".NS", "").replace("=F", "")


# ----------------------------------------------------------------------------
# Data
# ----------------------------------------------------------------------------
def _clean(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=lambda c: str(c).strip().lower())
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df = df[["open", "high", "low", "close", "volume"]].dropna(
        subset=["open", "high", "low", "close"]
    )
    idx = pd.DatetimeIndex(df.index)
    idx = idx.tz_localize(IST) if idx.tz is None else idx.tz_convert(IST)
    df.index = idx
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df


def fetch_yf(symbol: str, interval_min: int, period: str = "30d", retries: int = 3):
    """Download intraday candles from Yahoo Finance (free)."""
    import yfinance as yf

    for k in range(retries):
        try:
            df = yf.download(
                symbol, period=period, interval=f"{interval_min}m",
                progress=False, auto_adjust=False, threads=False,
            )
            if df is not None and len(df):
                return _clean(df)
        except Exception as exc:  # noqa: BLE001
            print(f"  fetch error {symbol}: {exc}")
        time.sleep(1.5 * (k + 1))
    return None


def fetch_many(symbols: list[str], interval_min: int, period: str = "30d",
               chunk: int = 40, retry_single: int = 15) -> dict:
    """Batched Yahoo download (a few calls for 200+ symbols). Returns {symbol: DataFrame}."""
    import yfinance as yf

    out: dict = {}
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        raw = None
        for k in range(3):
            try:
                raw = yf.download(part, period=period, interval=f"{interval_min}m", group_by="ticker",
                                  progress=False, auto_adjust=False, threads=True)
                if raw is not None and len(raw):
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"  batch error: {exc}")
            time.sleep(2 * (k + 1))
        if raw is None or raw.empty:
            continue
        for s in part:
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if s not in raw.columns.get_level_values(0):
                        continue
                    sub = raw[s]
                elif len(part) == 1:
                    sub = raw
                else:
                    continue
                sub = sub.dropna(how="all")
                if len(sub):
                    out[s] = _clean(sub.copy())
            except Exception as exc:  # noqa: BLE001
                print(f"  {s}: parse error {exc}")
    missing = [s for s in symbols if s not in out]
    for s in missing[:retry_single]:          # one more try for a few stragglers
        df = fetch_yf(s, interval_min, period, retries=2)
        if df is not None:
            out[s] = df
    still = [s for s in symbols if s not in out]
    if still:
        print(f"  no data for {len(still)} symbol(s): {', '.join(still[:20])}{' ...' if len(still) > 20 else ''}")
    return out


def load_csv(path: str):
    """CSV with columns: datetime,open,high,low,close[,volume]. Naive times = IST."""
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = next((c for c in ("datetime", "date", "time", "timestamp") if c in df.columns), df.columns[0])
    df.index = pd.to_datetime(df[tcol])
    return _clean(df.drop(columns=[tcol]))


def prepare(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """Keep only regular-session bars."""
    return df.between_time(p.session_start, p.session_end, inclusive="left")


def closed_only(df: pd.DataFrame, base_min: int, now: pd.Timestamp) -> pd.DataFrame:
    """Drop the still-forming candle (the Pine code uses barstate.isconfirmed)."""
    return df[df.index + pd.Timedelta(minutes=base_min) <= now]


def resample_ohlc(df: pd.DataFrame, minutes: int, session_start: str) -> pd.DataFrame:
    hh, mm = (int(x) for x in session_start.split(":"))
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    out = df.resample(
        f"{minutes}min", origin="start_day", offset=pd.Timedelta(hours=hh, minutes=mm)
    ).agg(agg)
    return out.dropna(subset=["open"])


# ----------------------------------------------------------------------------
# Indicator maths (exact Pine semantics)
# ----------------------------------------------------------------------------
def _ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def _rma(x, n: int) -> np.ndarray:
    x = np.asarray(x, float)
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    out[n - 1] = x[:n].mean()
    a = 1.0 / n
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def range_filter(close: np.ndarray, rng: np.ndarray):
    """Returns ci (+1/-1 regime), age, long_sig, short_sig arrays."""
    n = len(close)
    ci = np.zeros(n, np.int8)
    age = np.zeros(n, np.int32)
    ls = np.zeros(n, bool)
    ss = np.zeros(n, bool)
    up = dn = 0
    c = 0
    a = 0
    prev = np.nan
    for i in range(n):
        s = close[i]
        r = rng[i]
        p_ = s if np.isnan(prev) else prev            # nz(rf[1], src)
        if np.isnan(r):
            cur = np.nan
        elif s > p_:
            cur = p_ if (s - r) < p_ else s - r
        else:
            cur = p_ if (s + r) > p_ else s + r
        if not np.isnan(cur):
            pz = 0.0 if np.isnan(prev) else prev      # nz(rf[1])
            if cur > pz:
                up += 1
                dn = 0
            elif cur < pz:
                up = 0
                dn += 1
        ok = not np.isnan(cur)
        longc = ok and s > cur and up > 0
        shortc = ok and s < cur and dn > 0
        pc = c
        if longc:
            c = 1
        elif shortc:
            c = -1
        ls[i] = longc and pc == -1
        ss[i] = shortc and pc == 1
        a = 0 if c != pc else a + 1
        ci[i] = c
        age[i] = a
        prev = cur
    return ci, age, ls, ss


def _rng(close: pd.Series, p: Params) -> np.ndarray:
    avrng = _ema(close.diff().abs(), p.per)
    return (_ema(avrng, p.per * 2 - 1) * p.mult).to_numpy(float)


def build_frame(df: pd.DataFrame, p: Params) -> pd.DataFrame:
    """Adds signal columns to an OHLCV frame (bars = closed candles)."""
    d = df.copy()
    close = d["close"].to_numpy(float)
    ci, age, ls, ss = range_filter(close, _rng(d["close"], p))
    d["ci"], d["age"], d["long_sig"], d["short_sig"] = ci, age, ls, ss

    # ATR (Pine ta.atr = RMA of true range)
    pc = d["close"].shift(1)
    tr = pd.concat([d["high"] - d["low"], (d["high"] - pc).abs(), (d["low"] - pc).abs()], axis=1).max(axis=1)
    tr.iloc[0] = d["high"].iloc[0] - d["low"].iloc[0]
    d["atr"] = _rma(tr.to_numpy(float), p.atr_length)
    d["prev_high"] = d["high"].shift(1).rolling(p.swing_lookback).max()
    d["prev_low"] = d["low"].shift(1).rolling(p.swing_lookback).min()

    # Higher-TF confirmation == request.security(..., [ci[1], age[1]], lookahead_on)
    if p.use_htf:
        h = resample_ohlc(d[["open", "high", "low", "close", "volume"]], p.htf_min, p.session_start)
        hci, hage, _, _ = range_filter(h["close"].to_numpy(float), _rng(h["close"], p))
        tbl = pd.DataFrame(
            {"htf_bias": pd.Series(hci, index=h.index).shift(1),
             "htf_age": pd.Series(hage, index=h.index).shift(1)}
        )
        m = pd.merge_asof(pd.DataFrame(index=d.index), tbl, left_index=True, right_index=True, direction="backward")
        d["htf_bias"], d["htf_age"] = m["htf_bias"], m["htf_age"]
        age_ok = (p.max_age == 0) | (d["htf_age"] <= p.max_age)
        d["htf_long"] = (d["htf_bias"] == 1) & age_ok
        d["htf_short"] = (d["htf_bias"] == -1) & age_ok
    else:
        d["htf_bias"], d["htf_age"] = np.nan, np.nan
        d["htf_long"] = True
        d["htf_short"] = True

    # HTF flip trigger: the HTF regime just flipped (this is the first chart bar after that HTF candle closed)
    # and the chart-TF regime already points the same way. Same session day only.
    day = pd.Series(d.index.strftime("%Y-%m-%d"), index=d.index)
    same_day = day == day.shift(1)
    prev_bias = d["htf_bias"].shift(1)
    on = bool(p.htf_trigger and p.use_htf)
    d["trig_long"] = ((d["htf_bias"] == 1) & (prev_bias == -1) & (d["ci"] == 1) & same_day) & on
    d["trig_short"] = ((d["htf_bias"] == -1) & (prev_bias == 1) & (d["ci"] == -1) & same_day) & on

    # last bar of the session (session.islastbar_regular)
    end_h, end_m = (int(x) for x in p.session_end.split(":"))
    mins = d.index.hour * 60 + d.index.minute + p.base_min
    d["is_last"] = mins >= end_h * 60 + end_m

    ok = d["atr"].notna() & d["prev_high"].notna() & d["prev_low"].notna()
    block = d["is_last"] if p.eod_exit else pd.Series(False, index=d.index)   # no entry on the square-off bar
    a_long = d["long_sig"] & d["htf_long"]
    a_short = d["short_sig"] & d["htf_short"]
    d["raw_long"] = (a_long | d["trig_long"]) & p.allow_long & ok & ~block
    d["raw_short"] = (a_short | d["trig_short"]) & p.allow_short & ok & ~block
    d["entry_type"] = np.where(a_long | a_short, "5m flip", "15m flip")
    return d


def levels(side: int, close: float, atr: float, ph: float, pl: float, p: Params):
    """Entry stop-loss and sole target (TP1), identical to the Pine code. Returns sl, tp1, risk."""
    if side == 1:
        sl = close - atr * p.atr_mult if (close - pl) < atr * p.atr_mult else pl
        risk = close - sl
        return sl, close + risk * p.rr1, risk
    sl = close + atr * p.atr_mult if (ph - close) < atr * p.atr_mult else ph
    risk = sl - close
    return sl, close - risk * p.rr1, risk


def calc_qty(equity: float, close: float, risk: float, p: Params, leverage: float = 1.0) -> int:
    if risk <= 0:
        return 0
    q = min(equity * p.risk_pct / 100 / risk, equity * leverage / close)
    return max(int(np.floor(q)), 1)


def todays_entries(d: pd.DataFrame, p: Params, today: str) -> pd.DataFrame:
    """Signal rows for `today` (YYYY-MM-DD). With one_trade_day only the first counts."""
    mask = (d["raw_long"] | d["raw_short"]) & (d.index.strftime("%Y-%m-%d") == today)
    c = d[mask]
    if p.one_trade_day:
        c = c.iloc[:1]
    return c


# ----------------------------------------------------------------------------
# Telegram
# ----------------------------------------------------------------------------
def _telegram_destinations(token: str | None = None, chat_id: str | None = None) -> list[tuple[str, str]]:
    """(bot_token, chat_id) pairs. Primary token + chats; optional second bot pair."""
    dests: list[tuple[str, str]] = []
    tok1 = token or os.getenv("TELEGRAM_BOT_TOKEN") or ""
    chats: list[str] = []
    raw = chat_id if chat_id is not None else os.getenv("TELEGRAM_CHAT_ID", "")
    chats.extend(x.strip() for x in str(raw).split(",") if x.strip())
    extra = os.getenv("TELEGRAM_CHAT_IDS", "")
    if extra:
        chats.extend(x.strip() for x in extra.split(",") if x.strip())
    # Same-bot second chat only when no dedicated second bot token
    tok2 = os.getenv("TELEGRAM_BOT_TOKEN_2", "").strip()
    chat2 = os.getenv("TELEGRAM_CHAT_ID_2", "").strip()
    if not tok2 and chat2:
        chats.extend(x.strip() for x in chat2.split(",") if x.strip())
    seen_c: set[str] = set()
    for c in chats:
        if tok1 and c and c not in seen_c:
            seen_c.add(c)
            dests.append((tok1, c))
    if tok2 and chat2:
        for c in (x.strip() for x in chat2.split(",") if x.strip()):
            pair = (tok2, c)
            if pair not in dests:
                dests.append(pair)
    return dests


def tg_send(text: str, token: str | None = None, chat_id: str | None = None) -> bool:
    dests = _telegram_destinations(token, chat_id)
    if not dests:
        print("Telegram credentials missing (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False
    ok_any = False
    for tok, cid in dests:
        r = requests.post(
            f"https://api.telegram.org/bot{tok}/sendMessage",
            json={"chat_id": cid, "text": text[:4000], "parse_mode": "HTML",
                  "disable_web_page_preview": True},
            timeout=20,
        )
        if not r.ok:
            print(f"Telegram error ({cid}):", r.text)
        else:
            ok_any = True
    return ok_any


def tg_send_file(path: str, caption: str = "", token: str | None = None, chat_id: str | None = None) -> bool:
    dests = _telegram_destinations(token, chat_id)
    if not dests:
        return False
    method = "sendPhoto" if path.lower().endswith((".png", ".jpg", ".jpeg")) else "sendDocument"
    field = "photo" if method == "sendPhoto" else "document"
    ok_any = False
    for tok, cid in dests:
        with open(path, "rb") as fh:
            r = requests.post(
                f"https://api.telegram.org/bot{tok}/{method}",
                data={"chat_id": cid, "caption": caption[:1000]},
                files={field: fh}, timeout=60,
            )
        if not r.ok:
            print(f"Telegram error ({cid}):", r.text)
        else:
            ok_any = True
    return ok_any


def format_signal(sym: str, side: int, ts: pd.Timestamp, entry: float, sl: float,
                  tp1: float, qty: int, p: Params, capital: float, entry_type: str = "",
                  delay_min: float = 0.0) -> str:
    risk = abs(entry - sl)
    delay_txt = f"⏱ Alert came ~{delay_min:.0f} min after the candle closed (entry = candle close)\n" if delay_min > 8 else ""
    exit_txt = (f"Square-off at {p.session_end} if TP/SL not hit." if p.eod_exit
                else "No square-off: trade runs until SL / TP1.")
    icon, word = ("🟢", "BUY") if side == 1 else ("🔴", "SELL")
    if not p.use_htf:
        why = f"{p.base_min}m flip • HTF filter off"
    elif entry_type == "15m flip":
        # Only when HTF_TRIGGER=true (disabled by default on live scanner)
        why = f"{p.htf_min}m flip • {p.base_min}m already aligned ✓"
    else:
        # Default live path: 5m RF flip with HTF (15m) already aligned (htf_age ≤ MAX_AGE)
        why = f"{p.base_min}m flip • {p.htf_min}m aligned ✓"
    f = lambda v: f"{v:,.2f}"  # noqa: E731
    return (
        f"{icon} <b>{word} – {display_name(sym)}</b>\n"
        f"<i>{why}</i>\n\n"
        f"Entry: <b>{f(entry)}</b>\n"
        f"SL: <b>{f(sl)}</b>  (risk {f(risk)} • {risk / entry * 100:.2f}%)\n"
        f"TP1 ({p.rr1:g}R): <b>{f(tp1)}</b>  → book 100%\n\n"
        f"Qty @ ₹{capital:,.0f} / {p.risk_pct:g}% risk: {qty}\n"
        f"Signal candle closed: {(ts + pd.Timedelta(minutes=p.base_min)).strftime('%H:%M')} IST\n"
        f"{delay_txt}"
        f"{exit_txt}\n"
        f"<i>Observation levels only – not investment advice.</i>"
    )
