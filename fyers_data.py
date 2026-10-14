"""Fyers API v3 candle source for the scanner (runs on GitHub Actions).

Login: fyers_login.py (TOTP, secrets from env FYERS_*; never printed).
Symbol map (Yahoo-style names used across the repo -> Fyers):
  RELIANCE.NS -> NSE:RELIANCE-EQ      ^NSEI -> NSE:NIFTY50-INDEX
  ^NSEBANK -> NSE:NIFTYBANK-INDEX     NIFTY_MID_SELECT.NS -> NSE:MIDCPNIFTY-INDEX
  GC=F/SI=F/CL=F/HG=F/NG=F -> MCX near-month GOLD/SILVER/CRUDEOIL/COPPER/NATURALGAS
  futures (from the public Fyers MCX symbol master), continuous history (cont_flag=1).
Rate limit: Fyers allows ~10 req/s and 200 req/min; we stay at <= FYERS_RPM (default 170/min).

Self-test (no Telegram):  python fyers_data.py [SYM ...]
"""
from __future__ import annotations

import io
import os
import sys
import time
from collections import deque

import pandas as pd

IST = "Asia/Kolkata"
INDEX_MAP = {
    "^NSEI": "NSE:NIFTY50-INDEX",
    "^NSEBANK": "NSE:NIFTYBANK-INDEX",
    "NIFTY_MID_SELECT.NS": "NSE:MIDCPNIFTY-INDEX",
    "^CNXFIN": "NSE:FINNIFTY-INDEX",
}
MCX_UNDERLYING = {"GC=F": "GOLD", "SI=F": "SILVER", "CL=F": "CRUDEOIL",
                  "HG=F": "COPPER", "NG=F": "NATURALGAS"}
MCX_MASTER = "https://public.fyers.in/sym_details/MCX_COM.csv"
NAME_FIX = {"M&M": "M&M", "BAJAJ-AUTO": "BAJAJ-AUTO"}

_client = None
_calls: deque = deque()
_mcx_cache: dict | None = None


def enabled() -> bool:
    src = (os.getenv("DATA_SOURCE") or "").strip().lower()
    if src == "yahoo":
        return False
    return bool(os.getenv("FYERS_APP_ID", "").strip())


def _throttle() -> None:
    rpm = int(float(os.getenv("FYERS_RPM") or 170))
    rps = 4.0  # well under 10/s
    now = time.time()
    while _calls and now - _calls[0] > 60:
        _calls.popleft()
    if len(_calls) >= rpm:
        time.sleep(60 - (now - _calls[0]) + 0.2)
    if _calls and time.time() - _calls[-1] < 1.0 / rps:
        time.sleep(1.0 / rps - (time.time() - _calls[-1]))
    _calls.append(time.time())


def client(force: bool = False):
    global _client
    if _client is None or force:
        from fyers_login import get_fyers
        _client = get_fyers(force=force)
    return _client


def _mcx_symbols() -> dict:
    """{underlying: nearest unexpired FUT symbol} from the public symbol master."""
    global _mcx_cache
    if _mcx_cache is not None:
        return _mcx_cache
    import requests
    out: dict = {}
    try:
        txt = requests.get(MCX_MASTER, timeout=30).text
        df = pd.read_csv(io.StringIO(txt), header=None)
        # cols: 0 fytoken, 1 name, 8 expiry epoch, 9 symbol, 13 underlying
        now = time.time() + 2 * 86400  # roll 2 days before expiry
        df = df[df[9].astype(str).str.endswith("FUT")]
        for und in set(MCX_UNDERLYING.values()):
            sub = df[(df[13].astype(str) == und) & (df[8].astype(float) > now)]
            if len(sub):
                out[und] = str(sub.sort_values(8).iloc[0][9])
    except Exception as exc:  # noqa: BLE001
        print(f"  MCX symbol master error: {exc}")
    _mcx_cache = out
    return out


def to_fyers(sym: str) -> str | None:
    if sym in INDEX_MAP:
        return INDEX_MAP[sym]
    if sym in MCX_UNDERLYING:
        return _mcx_symbols().get(MCX_UNDERLYING[sym])
    if sym.endswith("=F") or sym.startswith("^"):
        return None
    base = sym[:-3] if sym.endswith((".NS", ".BO")) else sym
    ex = "BSE" if sym.endswith(".BO") else "NSE"
    return f"{ex}:{base}-EQ"


def fetch_one(sym: str, interval_min: int, days: int = 30):
    fy = to_fyers(sym)
    if not fy:
        return None
    now = int(time.time())
    req = {"symbol": fy, "resolution": str(interval_min), "date_format": "0",
           "range_from": str(now - days * 86400), "range_to": str(now), "cont_flag": "1"}
    for attempt in range(3):
        _throttle()
        try:
            r = client().history(data=req)
        except Exception as exc:  # noqa: BLE001
            r = {"s": "error", "message": str(exc)[:120]}
        if isinstance(r, dict) and r.get("s") == "ok":
            c = r.get("candles") or []
            if not c:
                return None
            df = pd.DataFrame(c, columns=["t", "open", "high", "low", "close", "volume"])
            df.index = pd.to_datetime(df["t"], unit="s", utc=True).tz_convert(IST)
            return df.drop(columns=["t"]).astype(float)
        msg = str((r or {}).get("message", ""))[:120] if isinstance(r, dict) else ""
        code = (r or {}).get("code") if isinstance(r, dict) else None
        if code in (-16, -15, -17, -8) or "token" in msg.lower() or "auth" in msg.lower():
            print("  fyers auth error, re-login once")
            client(force=True)
            continue
        if code == 429 or "limit" in msg.lower():
            time.sleep(5 * (attempt + 1))
            continue
        print(f"  fyers {sym} ({fy}): {msg or r}")
        return None
    return None


def fetch_many(symbols: list[str], interval_min: int, days: int = 30) -> dict:
    out: dict = {}
    t0 = time.time()
    try:
        client()
    except BaseException as exc:  # noqa: BLE001  (LoginError / SystemExit on missing env)
        print(f"  FYERS LOGIN FAILED: {str(exc)[:200]}")
        return out
    for s in symbols:
        df = fetch_one(s, interval_min, days)
        if df is not None and len(df):
            out[s] = df
    print(f"  fyers: {len(out)}/{len(symbols)} symbols in {time.time() - t0:.0f}s")
    return out


if __name__ == "__main__":
    syms = sys.argv[1:] or ["^NSEI", "^NSEBANK", "NIFTY_MID_SELECT.NS", "RELIANCE.NS", "M&M.NS",
                            "GC=F", "SI=F", "CL=F", "HG=F", "NG=F"]
    print("MCX map:", _mcx_symbols())
    data = fetch_many(syms, 5, 30)
    for s in syms:
        df = data.get(s)
        if df is None:
            print(f"{s:22} -> {to_fyers(s)}: NO DATA")
        else:
            print(f"{s:22} -> {to_fyers(s)}: {len(df)} bars, last {df.index[-1]:%Y-%m-%d %H:%M} close {df['close'].iloc[-1]}")
    sys.exit(0 if len(data) == len(syms) else 1)
