# Agent Scanner – Agent Confluence (Range Filter + HTF confirmation)

Live Telegram alerts + backtest, run free on GitHub Actions.
Python port of your Pine strategy **"Agent Confluence Backtest"**.

## What it does
- **Signal (either way, both timeframes must agree):**
  1. **5m flip**: the 5m Range Filter flips on a closed candle while the 15m regime agrees and the 15m flipped recently (within `MAX_AGE` 15m candles, default 2).
  2. **15m flip**: the 15m flips and the 5m regime already points the same way. The alert comes on the first 5m candle after that 15m candle closes.
- **Levels** (same as Pine): SL = swing low/high of last 10 candles, or 1×ATR if that is tighter. TP1 = 0.8R, TP2 = 1.5R.
- **No daily limit, no square-off**: a symbol can signal any number of times, and a trade runs (also overnight) until SL or TP2. A new signal for the same symbol is not sent while its previous trade is still running.
- **Rules**: signals only on closed candles. No end-of-day square-off: a trade runs (even overnight) until SL or TP2 is hit; TP1 books 50%.
- **Alert**: Entry, SL, TP1, TP2, risk-based quantity and which of the two triggers fired.
- **Backtest**: same engine as the scanner. 50% exits at TP1, 50% at TP2, shared SL, EOD exit, commission, slippage, intrabar fill model, gap fills.

## Files
| File | Purpose |
|---|---|
| `agent_core.py` | Indicator logic, data, Telegram |
| `scanner.py` | Live scanner |
| `backtest.py` | Backtester + reports |
| `symbols.txt` | Full F&O list (210 symbols) |
| `Agent_Confluence_Indicator.pine` | TradingView indicator (entry, SL, TP1, TP2 labels + alerts) |
| `requirements.txt` | Python packages |
| `loop.py` | Runs the scanner right after every 5m candle close until a stop time |
| `.github/workflows/scanner.yml` | Starts the live loop twice a day (or a one-off scan by hand) |
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
6. **Go live**: nothing more to do. Two scheduled sessions start at about 09:00 and 12:20 IST (Mon–Fri) and scan right after every 5m candle closes (about 25 s later). If a morning start is ever missed, run the workflow by hand with **loop_until** = `15:32`.
7. **Backtest**: *Actions → Agent Backtest → Run workflow*. Leave defaults (compare = on, telegram = on). Results arrive on Telegram (summary, chart, trades.csv) and under the run's **Artifacts** (`trades.csv`, `report.md`, `summary.json`, `equity.png`, `compare.csv`).

## Reliable 5-minute scans (important)
GitHub's built-in schedule is best-effort: runs are often delayed by 10-30+ minutes or skipped entirely, which looks like "alerts only when I run it manually". Fix: let a free external timer trigger the workflow at exact times.

1. **GitHub token**: GitHub → profile photo → *Settings → Developer settings → Personal access tokens → Fine-grained tokens → Generate new token*. Repository access: *Only select repositories → Agent-Scanner*. Permissions → *Repository permissions → Actions: Read and write*. Generate and copy the token (set the longest expiry, note the date).
2. **cron-job.org**: create a free account → *Create cronjob*.
   - URL: `https://api.github.com/repos/YOUR_USERNAME/Agent-Scanner/actions/workflows/scanner.yml/dispatches`
   - Schedule: *Custom* → time zone **Asia/Kolkata**; Days of week Mon–Fri; Hours 9–15; Minutes `1,6,11,16,21,26,31,36,41,46,51,56` (one minute after each 5m candle closes, so Yahoo has the candle).
   - *Advanced*: Request method **POST**; Headers: `Authorization: Bearer YOUR_TOKEN`, `Accept: application/vnd.github+json`, `Content-Type: application/json`; Request body: `{"ref":"main"}` (use your default branch name).
   - Save, then press *Test run*: a response of **204** means it worked, and a new "Agent Scanner" run appears in the Actions tab.
3. **Check it**: run the workflow once manually with **heartbeat** ticked. With `HEARTBEAT` on (set `HEARTBEAT: "true"` in `scanner.yml`), every scan sends a small "💓 Scan OK" message so you can see scans happen every 5 minutes; remove it once you trust it.
4. **Also check** *Actions → Agent Scanner*: the "Event" of each run says `schedule` or `workflow_dispatch`. If the workflow file is not on the default branch, or the repo was inactive for 60 days, GitHub stops `schedule` runs.
5. **Minutes**: public repos have unlimited free Actions minutes. A private repo on the Free plan gets 2,000 minutes a month, and a full day of 5-minute scans uses roughly 100-150, so it would run out in about two weeks. If your repo is private, make it public (the Telegram token stays secret) or use a paid plan.

## Your symbol list
`symbols.txt` already contains the full F&O list you shared (207 stocks) plus NIFTY, BANKNIFTY and MIDCPNIFTY (210 in total). Edit it any time: one symbol per line, no `.NS` needed, `#` for comments. Symbols Yahoo cannot find (renamed or newly listed) are skipped and listed in the log.

Downloads are batched (40 symbols per Yahoo call), so a full scan should take about a minute rather than several. MCX/commodities later: add Yahoo futures tickers (e.g. `GC=F`) and set `SESSION_START` / `SESSION_END` env vars.

## Settings (env vars in `scanner.yml`, or flags in `backtest.py`)
| Pine input | Env var | Backtest flag | Default |
|---|---|---|---|
| Sampling Period | `PER` | `--per` | 100 |
| Range Multiplier | `MULT` | `--mult` | 3.0 |
| Require HTF confirmation | `USE_HTF` | `--no-htf` | on |
| Chart TF (min) | `BASE_MIN` | `--base-min` | 5 |
| Confirmation TF (min) | `HTF_MIN` | `--htf-min` | 15 |
| Max age of HTF flip (HTF candles) | `MAX_AGE` | `--max-age` | 2 (0 = any) |
| 15m flip + 5m already aligned trigger | `HTF_TRIGGER` | `--no-htf-trigger` | on |
| Swing lookback / ATR length / ATR mult | `SWING_LOOKBACK`, `ATR_LENGTH`, `ATR_MULT` | same | 10 / 14 / 1.0 |
| Target 1 / 2 RR | `RR1`, `RR2` | `--rr1`, `--rr2` | 0.8 / 1.5 |
| Risk % of equity | `RISK_PCT` | `--risk-pct` | 1.0 |
| One trade per day | `ONE_TRADE_DAY` | `--one-trade-day` | off (no limit) |
| Square off at end of session | `EOD_EXIT` | `--eod` | off |
| Longs / shorts | `ALLOW_LONG`, `ALLOW_SHORT` | `--long-only`, `--short-only` | both |

To use a 15m chart with 60m confirmation: `BASE_MIN=15`, `HTF_MIN=60` (or run the workflow manually with those inputs).

## Backtest options (`extra_args` box or command line)
- `--compare` : HTF off vs on vs max-age ≤1/2/4, with and without the 15m-flip trigger, and with a one-trade-a-day limit
- `--be-after-tp1` : move SL to entry after TP1 (the Pine strategy does not)
- `--ambiguity tv|sl_first|tp_first` : when SL and a target are both inside one candle (`tv` = TradingView's rule, `sl_first` = pessimistic)
- `--commission 0.03` (% per side), `--slippage-ticks 1`, `--tick 0.05`, `--capital 100000`, `--leverage 1`, `--warmup 450`
- `--csv-dir folder` : use your own longer history. Files: `RELIANCE.csv`, `NSEI.csv` (Nifty), `NSEBANK.csv`; columns `datetime,open,high,low,close,volume` (naive times = IST)

Report contents: win rate, % hitting SL / TP1 / TP2 / EOD, outcome mix (SL, TP1 then SL, TP1+TP2, TP1 then EOD, …), profit factor, expectancy in R, drawdown, losing streak, daily Sharpe, MFE/MAE per trade, and breakdowns by side, weekday, entry hour, month, symbol.

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
python loop.py          # scans every 5m candle close until 15:32 IST; start it each morning (cron: 45 3 * * 1-5 UTC)
python backtest.py --compare --telegram
```
`state.json` (de-duplication) is kept in the working folder.
