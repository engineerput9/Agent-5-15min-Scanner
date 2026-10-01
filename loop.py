"""
loop.py - runs the scanner every base-timeframe candle close (e.g. 09:20:25, 09:25:25 ...) until a stop time.
Use it (a) inside GitHub Actions (scanner.yml starts it twice a day) or (b) on any always-on machine:
    export TELEGRAM_BOT_TOKEN=...  TELEGRAM_CHAT_ID=...
    python loop.py
Env: LOOP_UNTIL="HH:MM" (IST, default = session end + 2 min), LOOP_DELAY_SEC (default 25).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
import traceback

import pandas as pd

import scanner
from agent_core import IST, Params, apply_session_for_universe, load_symbols


def _hash(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()
    except OSError:
        return ""


def _push_state() -> None:
    """When running in GitHub Actions, commit state.json so a restart never re-sends alerts."""
    if os.getenv("GITHUB_ACTIONS") != "true":
        return
    try:
        subprocess.run(["git", "config", "user.name", "agent-scanner-bot"], check=False)
        subprocess.run(["git", "config", "user.email", "agent-scanner-bot@users.noreply.github.com"], check=False)
        subprocess.run(["git", "add", "state.json"], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode != 0:
            subprocess.run(["git", "commit", "-m", "scanner state"], check=False)
            subprocess.run(["git", "pull", "--rebase", "--autostash"], check=False)
            subprocess.run(["git", "push"], check=False)
    except Exception as exc:  # noqa: BLE001
        print("state push failed:", exc)


def main() -> None:
    p = Params.from_env()
    p = apply_session_for_universe(p, load_symbols())
    delay = int(float(os.getenv("LOOP_DELAY_SEC") or 25))
    now = pd.Timestamp.now(tz=IST)
    until = os.getenv("LOOP_UNTIL")
    if not until:
        eh, em = (int(x) for x in p.session_end.split(":"))
        until = f"{eh:02d}:{em + 2:02d}" if em + 2 < 60 else f"{eh + 1:02d}:00"
    stop = pd.Timestamp(f"{now:%Y-%m-%d} {until}", tz=IST)
    print(f"loop started {now:%H:%M:%S} IST, every {p.base_min}m candle close, until {until}")

    while True:
        now = pd.Timestamp.now(tz=IST)
        step = p.base_min * 60
        secs = now.hour * 3600 + now.minute * 60 + now.second
        nxt = now + pd.Timedelta(seconds=(step - secs % step) + delay)
        if nxt > stop:
            break
        wait = (nxt - now).total_seconds()
        print(f"sleeping {wait:.0f}s until {nxt:%H:%M:%S}")
        time.sleep(max(wait, 1))
        before = _hash(scanner.STATE_FILE)
        try:
            scanner.main()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        if _hash(scanner.STATE_FILE) != before:
            _push_state()

    # After the afternoon session (LOOP_UNTIL at/after session_end), post EOD summary.
    # Deduped in eod_summary via state.json "eod_sent"; morning loop (until ~12:32) skips.
    eh, em = (int(x) for x in p.session_end.split(":"))
    stop_hm = int(until[:2]) * 60 + int(until[3:5])
    if stop_hm >= eh * 60 + em:
        print("afternoon loop ended — running EOD Telegram summary")
        try:
            import eod_summary
            eod_summary.run(dry_run=False, force=False)
            _push_state()
        except Exception as exc:  # noqa: BLE001
            print("EOD summary failed:", exc)
            traceback.print_exc()
    print("loop finished")


if __name__ == "__main__":
    main()
