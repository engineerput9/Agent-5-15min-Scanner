"""One-shot: replay last 3 state.json alerts to all Telegram destinations."""
import json, os, sys
sys.path.insert(0, ".")
from agent_core import tg_send

st = json.load(open("state.json"))
rows = []
for k, v in st.get("sent", {}).items():
    sym, ts = k.split("|", 1)
    rows.append({
        "sym": sym, "signal_ts": ts, "side": v.get("side"),
        "entry_type": v.get("entry_type"), "sent_at": v.get("sent_at") or "",
    })
rows.sort(key=lambda r: (r["sent_at"], r["signal_ts"]), reverse=True)
last = rows[:3]
lines = ["🧪 <b>Last 3 alerts replay</b> (GitHub → both bots verify)\n"]
for i, r in enumerate(last, 1):
    name = r["sym"].replace(".NS", "")
    icon, word = ("🟢", "BUY") if r["side"] == "LONG" else ("🔴", "SELL")
    lines.append(
        f"{i}. {icon} {word} <b>{name}</b>\n"
        f"   {r['entry_type']} · signal {r['signal_ts'][:16].replace('T', ' ')}\n"
        f"   sent {r['sent_at'][:16].replace('T', ' ')} IST"
    )
ok = tg_send("\n".join(lines))
print("replay_sent", ok, "n", len(last))
sys.exit(0 if ok else 1)
