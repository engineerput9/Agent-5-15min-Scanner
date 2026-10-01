# Agent Scanner – Agent Confluence (Range Filter + HTF confirmation)

Live Telegram alerts + backtest, run free on GitHub Actions.
Python port of your Pine strategy **"Agent Confluence Backtest"**.

## What it does
- **Live signal (default):** only **5m flip with 15m aligned** — the 5m Range Filter flips on a closed candle while the 15m HTF regime already agrees and the 15m flipped recently (`htf_age` ≤ `MAX_AGE`, default 2). Telegram wording: *5m flip • 15m aligned ✓*.
- **15m-flip trigger is OFF by default** (`HTF_TRIGGER=false`). That path (15m flips while 5m already aligned) is still in the engine for backtests / optional use; re-enable with `HTF_TRIGGER=true` in the workflow env (or Pine input). When enabled, Telegram says *15m flip • 5m already aligned ✓*.
- **Levels** (same as Pine): SL = swing low/high of last 10 candles, or 1×ATR if that is tighter. **TP1 = 1.2R (sole target, full size)** — no TP2.
- **No daily limit, no square-off**: a symbol can signal any number of times, and a trade runs (also overnight) until SL or TP1. A new signal for the same symbol is not sent while its previous trade is still running.
- **Rules**: signals only on closed candles. No end-of-day square-off: a trade runs (even overnight) until SL or TP1 is hit; **full size books at TP1**.
- **Alert**: Entry, SL, TP1, risk-based quantity and why it fired (5m flip with HTF aligned by default).
- **EOD summary**: after close each trading day, one Telegram message with that day's signal count, win rate, and PnL (same SL/TP fill model as the backtest). Still splits by trigger type if any 15m-flip alerts remain in history or if you re-enable `HTF_TRIGGER`.
- **Backtest**: same engine as the scanner. Full-size exit at TP1, shared SL, optional EOD exit, commission, slippage, intrabar fill model, gap fills.

## Files
| File | Purpose |
|---|---|
| `agent_core.py` | Indicator logic, data, Telegram |
| `scanner.py` | Live scanner |
| `backtest.py` | Backtester + reports |
| `symbols.txt` | Full F&O list (210 symbols) |
| `commodities.txt` | MCX-relevant Yahoo futures proxies (gold, silver, crude, …) |
| `Agent_Confluence_Indicator.pine` | TradingView indicator (entry, SL, TP1 labels + alerts) |
| `requirements.txt` | Python packages |
| `loop.py` | Runs the scanner right after every 5m candle close until a stop time; posts EOD summary when the afternoon session ends |
| `eod_summary.py` | End-of-day Telegram summary (signal count, win rate, PnL) for alerts sent that day |
| `.github/workflows/scanner.yml` | Live loops (morning + afternoon equities/commodities; evening commodities-only through ~23:30 IST) + daily EOD ~23:40 IST |
| `.github/workflows/backtest.yml` | Manual backtest run |

## Setup, step by step (phone-friendly)

1. **Repo**: open your private `Agent-Scanner` repo on GitHub.
2. **Add files**: tap *Add file → Create new file*. Type the file name, paste the code, *Commit changes*. Do this for each file above. For workflow files type the full path, e.g. `.github/workflows/scanner.yml` (typing `/` creates folders). Replace old files of the same name (open file → pencil icon → select all → paste).
3. **Secrets**: *Settings → Secrets and variables → Actions → New repository secret*. Add:
   - `TELEGRAM_BOT_TOKEN` – token from @BotFather
   - `TELEGRAM_CHAT_ID` – your chat id (message your bot once, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`)
   If your old workflow already used other secret names, either keep them and edit the two `env:` lines in `scanner.yml`, or add these names.
4. **Allow commits**: *Settings → Actions → General → Workflow permissions → Read and write permissions → Save*. (Needed so the bot can save `state.json`, which prevents duplicate alerts.)
5. **Test Telegram**: *Actions → Agent Scanner → Run workflow*, tick **test_mode**, Run. You should get a "✅ Agent Scanner test" message.
6. **Go live**: nothing more to do. Scheduled Mon–Fri sessions (all scan ~25 s after each 5m candle close):
   - **09:00 IST** → loop until **12:32** (equities + commodities)
   - **12:20 IST** → loop until **15:32** (equities + commodities, through NSE close)
   - **15:50 IST** → loop until **19:47** (**commodities only**, MCX evening part 1)
   - **19:50 IST** → loop until **23:32** (**commodities only**, MCX evening part 2 through ~23:30)
   Evening is split into two jobs because GitHub Actions caps a single job at ~6 hours. If a start is missed, run the workflow by hand with **loop_until** (e.g. `15:32` or `23:32`); tick **commodities_only** for an evening-style run.
7. **EOD summary**: every trading day around **23:40 IST** (GitHub cron `10 18 * * 1-5` UTC, after the MCX evening window) the workflow runs `eod_summary.py` and Telegrams a day summary: signals sent, win rate, and net PnL. The last evening loop also posts EOD when it finishes (~23:32); `state.json` `eod_sent` prevents duplicates. Manual: *Run workflow* → tick **eod_summary** (optional **eod_dry_run**).
8. **Backtest**: *Actions → Agent Backtest → Run workflow*. Leave defaults (compare = on, telegram = on). Results arrive on Telegram (summary, chart, trades.csv) and under the run's **Artifacts** (`trades.csv`, `report.md`, `summary.json`, `equity.png`, `compare.csv`).


## End-of-day (EOD) Telegram summary
Every Mon–Fri after the MCX evening window (~23:40 IST; or right after the last evening loop) the bot posts one message covering **that calendar day only**:
- **Signals sent** – count of alerts in `state.json` for today (equities **and** commodities when `INCLUDE_COMMODITIES` is on)
- **Win rate / PnL** – **overall** plus **by trigger** (5m flip / 15m flip when present): count, W/L, win rate %, net PnL. With default `HTF_TRIGGER=false`, live days are 5m-flip only.
- **Equities vs MCX** – separate sections: 📈 Equities and 🛢️ MCX / Commodities, each with its own stats and winners/losers. Commodity lines use MCX-style names (`GOLD`, `SILVER`, `CRUDEOIL`, `COPPER`, `NATURALGAS`), not Yahoo tickers like `GC=F`
- **Trigger storage** – `state.json` records `entry_type` (and side) when each alert is sent so EOD does not depend only on re-sim

**Fill model (same as scanner / backtest):** entry at signal candle close ± slippage; **full size exits at TP1**; shared SL; commission default 0.03%/side; slippage 1 tick × 0.05; with `EOD_EXIT=false` (default) any trade still open is marked-to-market at the last available close (outcome `Still open at data end`). Capital / risk % match alert sizing (`CAPITAL`, `RISK_PCT`).

**Triggers (recurring every trading day):**
1. Scheduled Actions cron `10 18 * * 1-5` (~23:40 IST, after MCX evening) → `python eod_summary.py`
2. End of the last evening `loop.py` when `LOOP_UNTIL` ≥ session end (23:30 with commodities; equity-only defaults stay 15:30)

With `INCLUDE_COMMODITIES=true`, session end is 23:30, so the afternoon loop (until 15:32) does **not** post EOD — that waits for the evening window so commodity alerts are included. Dedup via `state.json` → `"eod_sent": "YYYY-MM-DD"`. Dry-run locally: `EOD_DRY_RUN=1 python eod_summary.py`.

## Reliable 5-minute scans (important)
GitHub's built-in schedule is best-effort: runs are often delayed by 10-30+ minutes or skipped entirely, which looks like "alerts only when I run it manually". Fix: let a free external timer trigger the workflow at exact times.

1. **GitHub token**: GitHub → profile photo → *Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token*. Repository access: *Only select repositories → Agent-5-15min-Scanner*. Permissions → *Repository permissions → Actions: Read and write*. Generate and copy the token (set the longest expiry, note the date).
2. **cron-job.org**: create a free account → *Create cronjob*.
   - URL: `https://api.github.com/repos/engineerput9/Agent-5-15min-Scanner/actions/workflows/scanner.yml/dispatches`
   - Schedule: *Custom* → time zone **Asia/Kolkata**; Days of week Mon–Fri; Hours 9–15; Minutes `1,6,11,16,21,26,31,36,41,46,51,56` (one minute after each 5m candle closes, so Yahoo has the candle).
   - *Advanced*: Request method **POST**; Headers: `Authorization: Bearer YOUR_TOKEN`, `Accept: application/vnd.github+json`, `Content-Type: application/json`; Request body: `{"ref":"master"}` (this repo’s default branch).

   - Optional **evening MCX** coverage: either rely on the built-in schedule jobs (15:50 → 19:47 and 19:50 → 23:32, commodities-only), or one-shot dispatch at ~15:50 IST with body
     `{"ref":"master","inputs":{"commodities_only":true,"loop_until":"23:32"}}`
     (do **not** put `loop_until` on the every-5-minute daytime cron — that would stack long-running jobs).
   - Save, then press *Test run*: a response of **204** means it worked, and a new "Agent Scanner" run appears in the Actions tab.
3. **Check it**: *Run workflow* → tick **heartbeat** (optional; default off). Each scan then sends a small "💓 Scan OK" Telegram so you can confirm cron is firing every 5 minutes; leave it unticked once you trust the schedule.
4. **Also check** *Actions → Agent Scanner*: the "Event" of each run says `schedule` or `workflow_dispatch`. If the workflow file is not on the default branch, or the repo was inactive for 60 days, GitHub stops `schedule` runs.
5. **Minutes**: public repos have unlimited free Actions minutes. A private repo on the Free plan gets 2,000 minutes a month, and a full day of 5-minute scans uses roughly 100-150, so it would run out in about two weeks. If your repo is private, make it public (the Telegram token stays secret) or use a paid plan.

## Your symbol list
`symbols.txt` already contains the full F&O list you shared (207 stocks) plus NIFTY, BANKNIFTY and MIDCPNIFTY (210 in total). Edit it any time: one symbol per line, no `.NS` needed, `#` for comments. Symbols Yahoo cannot find (renamed or newly listed) are skipped and listed in the log.

Downloads are batched (40 symbols per Yahoo call), so a full scan should take about a minute rather than several.

### MCX / commodities
Yahoo does **not** list India MCX continuous contracts. Use the Yahoo US COMEX/NYMEX continuous futures in `commodities.txt` only as price proxies (same as TradingView `GC1!` / `CL1!` style feeds). These are **not a true MCX INR feed**; Telegram labels them with MCX-style names via `NAMES` in `agent_core.py`. Verified working on Yahoo (5m):

| Yahoo ticker | Alert / display (MCX-style) |
|---|---|
| `GC=F` | GOLD |
| `SI=F` | SILVER |
| `CL=F` | CRUDEOIL |
| `HG=F` | COPPER |
| `NG=F` | NATURALGAS |

Only these five are scanned (brent / platinum / palladium / micros / ETFs dropped). Telegram uses the MCX-style labels shown above (`GOLD`, `SILVER`, `CRUDEOIL`, `COPPER`, `NATURALGAS`) via `NAMES` in `agent_core.py`; this does not make the Yahoo prices an MCX INR feed. Direct names like `MCXGOLD` / `GOLD.NS` **do not** work on Yahoo.

Enable in the scanner with `INCLUDE_COMMODITIES=true` (merges `commodities.txt`). When any `=F` futures are loaded, session defaults widen to **09:00–23:30 IST** (approx. MCX hours) unless you override `SESSION_START` / `SESSION_END`. Equity-only runs stay at 09:15–15:30.

**Evening schedule (GitHub Actions):** after NSE close, two **commodities-only** loops keep scanning through the MCX evening window (~23:30 IST):
| Cron (UTC) | Starts (IST) | `LOOP_UNTIL` | Mode |
|---|---|---|---|
| `20 10 * * 1-5` | ~15:50 | 19:47 | `COMMODITIES_ONLY=true` |
| `20 14 * * 1-5` | ~19:50 | 23:32 | `COMMODITIES_ONLY=true` |
| `10 18 * * 1-5` | ~23:40 | — | EOD summary |

`COMMODITIES_ONLY=true` loads only `commodities.txt` (skips the 210-symbol equity list outside cash hours). Manual evening: *Run workflow* → **commodities_only** + **loop_until** = `23:32`.

## Settings (env vars in `scanner.yml`, or flags in `backtest.py`)
| Pine input | Env var | Backtest flag | Default |
|---|---|---|---|
| Sampling Period | `PER` | `--per` | 100 |
| Range Multiplier | `MULT` | `--mult` | 3.0 |
| Require HTF confirmation | `USE_HTF` | `--no-htf` | on |
| Chart TF (min) | `BASE_MIN` | `--base-min` | 5 |
| Confirmation TF (min) | `HTF_MIN` | `--htf-min` | 15 |
| Max age of HTF flip (HTF candles) | `MAX_AGE` | `--max-age` | 2 (0 = any) |
| 15m flip + 5m already aligned trigger | `HTF_TRIGGER` | `--no-htf-trigger` | **off** (live default; set `HTF_TRIGGER=true` to re-enable) |
| Swing lookback / ATR length / ATR mult | `SWING_LOOKBACK`, `ATR_LENGTH`, `ATR_MULT` | same | 10 / 14 / 1.0 |
| Target 1 RR (sole target, full size) | `RR1` | `--rr1` | 1.2 |
| Include commodities.txt | `INCLUDE_COMMODITIES` | — | off |
| Session (auto-widens for futures) | `SESSION_START`, `SESSION_END` | — | 09:15 / 15:30 (equity) or 09:00 / 23:30 (with futures) |
| Risk % of equity | `RISK_PCT` | `--risk-pct` | 1.0 |
| One trade per day | `ONE_TRADE_DAY` | `--one-trade-day` | off (no limit) |
| Square off at end of session | `EOD_EXIT` | `--eod` | off |
| Longs / shorts | `ALLOW_LONG`, `ALLOW_SHORT` | `--long-only`, `--short-only` | both |

To use a 15m chart with 60m confirmation: `BASE_MIN=15`, `HTF_MIN=60` (or run the workflow manually with those inputs).

**Re-enable the 15m-flip trigger:** uncomment / set `HTF_TRIGGER: "true"` under the scanner job env in `.github/workflows/scanner.yml` (or export `HTF_TRIGGER=true` locally). Backtest: omit `--no-htf-trigger` and set `HTF_TRIGGER=true` (default Params is off). Pine: turn on *Also signal when HTF flips…*.

## Backtest options (`extra_args` box or command line)
- `--compare` : HTF off vs on vs max-age ≤1/2/4, with and without the 15m-flip trigger, and with a one-trade-a-day limit
- `--be-after-tp1` : move SL to entry after TP1 (the Pine strategy does not)
- `--ambiguity tv|sl_first|tp_first` : when SL and a target are both inside one candle (`tv` = TradingView's rule, `sl_first` = pessimistic)
- `--commission 0.03` (% per side), `--slippage-ticks 1`, `--tick 0.05`, `--capital 100000`, `--leverage 1`, `--warmup 450`
- `--csv-dir folder` : use your own longer history. Files: `RELIANCE.csv`, `NSEI.csv` (Nifty), `NSEBANK.csv`; columns `datetime,open,high,low,close,volume` (naive times = IST)

Report contents: win rate, % hitting SL / TP1 / EOD, outcome mix (SL, TP1, TP1 then EOD, …), profit factor, expectancy in R, drawdown, losing streak, daily Sharpe, MFE/MAE per trade, and breakdowns by side, weekday, entry hour, month, symbol.

## Things to know (honest limits)
- **Yahoo intraday history is ~60 days** for 5m/15m, so a backtest covers about 2 months. Treat results as indicative; for real confidence use `--csv-dir` with a year or more from your broker.
- **GitHub cron is not exact**: runs can be delayed 5–20 minutes at busy times. Alerts are sent for any fresh signal from the last `FRESH_MIN` (30) minutes, with duplicates blocked. If your entries need to be tighter, use 15m/60m.
- Yahoo NSE intraday data can lag slightly and small values will differ from TradingView (EMA warm-up, data feed).
- Backtest sizing follows Pine (1% risk, quantity capped at equity ÷ price). Quantity in alerts is a cash-market number, not F&O lots.
- Nifty/BankNifty index values are signals only; trade them via futures/options at your own discretion.
- Observation levels only, not investment advice.

## If alerts stop arriving on schedule
1. Open *Actions → Agent Scanner*. Are there runs whose trigger says **schedule** for today? If not, GitHub skipped them.
2. **Private repo + free plan = 2,000 Actions minutes per month.** Scanning every 5 minutes all day uses far more than that (several thousand minutes), so scheduled runs stop once the allowance is used. Check *Settings → Billing → Usage*.
3. Fixes: make the repository **public** (Actions minutes are then free and secrets stay hidden, but your code and symbol list become visible), or run `loop.py` on any always-on computer / cloud VM:
   ```
   pip install -r requirements.txt
   export TELEGRAM_BOT_TOKEN=xxxx TELEGRAM_CHAT_ID=xxxx
   python loop.py
   ```

## Running somewhere other than GitHub
Any always-on computer or server with Python 3.10+ works:
```
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=xxxx TELEGRAM_CHAT_ID=xxxx
python loop.py          # scans every 5m candle close until session end (+2m); start each morning (or set LOOP_UNTIL / COMMODITIES_ONLY)
python backtest.py --compare --telegram
```
`state.json` (de-duplication) is kept in the working folder.
