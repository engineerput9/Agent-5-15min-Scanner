"""Pick the live scan window from the current IST clock.

GitHub's schedule is best-effort and often late. A late job must scan whatever
session is open now, not exit because a hardcoded LOOP_UNTIL already passed.

Mon–Fri (IST):
  08:55–12:32  equities + commodities
  12:32–15:32  equities + commodities (through NSE close)
  15:32–19:47  commodities only
  19:47–23:32  commodities only (through MCX evening)
  23:32–23:55  EOD Telegram summary
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

# minute-of-day boundaries
START = 8 * 60 + 55
H12 = 12 * 60 + 32
H15 = 15 * 60 + 32
H19 = 19 * 60 + 47
H23 = 23 * 60 + 32
EOD_END = 23 * 60 + 55


def plan(now: datetime | None = None) -> dict:
    now = now or datetime.now(IST)
    if now.weekday() >= 5:
        return {"mode": "idle", "reason": "weekend", "chain_next": False}
    mins = now.hour * 60 + now.minute
    if mins < START:
        return {"mode": "idle", "reason": "before session", "chain_next": False}
    if mins < H12:
        return {"mode": "loop", "loop_until": "12:32", "commodities_only": False, "chain_next": True}
    if mins < H15:
        return {"mode": "loop", "loop_until": "15:32", "commodities_only": False, "chain_next": True}
    if mins < H19:
        return {"mode": "loop", "loop_until": "19:47", "commodities_only": True, "chain_next": True}
    if mins < H23:
        return {"mode": "loop", "loop_until": "23:32", "commodities_only": True, "chain_next": True}
    if mins < EOD_END:
        return {"mode": "eod", "reason": "after MCX close", "chain_next": False}
    return {"mode": "idle", "reason": "after EOD window", "chain_next": False}


def as_env(p: dict) -> str:
    lines = [
        f"MODE={p.get('mode', 'idle')}",
        f"LOOP_UNTIL={p.get('loop_until', '')}",
        f"COMMODITIES_ONLY={'true' if p.get('commodities_only') else 'false'}",
        f"CHAIN_NEXT={'true' if p.get('chain_next') else 'false'}",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", metavar="PATH", help="write KEY=value env file")
    args = ap.parse_args()
    p = plan()
    if args.write:
        with open(args.write, "w") as fh:
            fh.write(as_env(p))
    if args.json or not args.write:
        json.dump(p, sys.stdout)
        sys.stdout.write("\n")


if __name__ == "__main__":
    main()
