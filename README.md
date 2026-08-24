# NSE/MCX Combined-Premium Terminal (v6 — Previous-Day Baseline)

A single-page, Bloomberg-terminal-style Streamlit dashboard that tracks
**ATM combined option premium** (Call LTP + Put LTP) across NSE indices,
NSE F&O stocks, and MCX commodities, pulling live data from the **Dhan
API**. Built to `PROJECT_SPEC_v6.md`.

## v6 baseline rule (the core change from earlier versions)

Per symbol, per trading day:

1. **Expiry** — always the real calendar date of the nearest active
   expiry (never a placeholder month; never shown as `SIMULATED` unless
   the reading genuinely came from the fallback feed).
2. **Strike** — ONE fixed strike for the whole day:
   `K = round(previous_day_spot / strike_step) * strike_step`.
   It does **not** re-flip to today's live ATM intraday — the same (E, K)
   is used from market open to close.
3. **CE / PE** — live LTPs at that fixed expiry + strike.
4. **Combined Premium** = CE + PE, shown as `₹123.45`.
5. **Baseline** = yesterday's persisted Combined Premium at that same
   (E, K), loaded from `baseline_store.json` when the market opens each
   day. If no prior snapshot exists for a symbol yet, it shows
   "baseline pending" and is excluded from spurt / the watchlist until
   one is captured.
6. **% Change** = `(today's Combined Premium − baseline) / baseline × 100`,
   computed **only after the opening bell** — never pre-market.
7. **Watchlist** — default threshold **10%**. A symbol enters once
   `|% change| ≥ threshold` and **stays for the rest of the day** even
   if it later cools back below threshold (Status shows `SPIKE` while
   currently past threshold, `COOLED` once it's back under but still
   listed). Cleared only when the next session opens.
8. **Chart** — Combined Premium vs session time only (a dotted line
   marks yesterday's baseline for reference). No spot/OI/volume series.

Every tick's reading is persisted back to `baseline_store.json`
(overwriting today's entry each time), so today's last successful tick
automatically becomes tomorrow's baseline — no manual EOD step needed.

## Project layout

```
├── app.py                  # Streamlit entry point (single panel, v6 logic)
├── config.py                # instrument universe + thresholds/cache TTLs
├── dhan_service.py          # Dhan API wrapper, chain cache, baseline persistence, sim fallback
├── baseline_store.json      # created at runtime — per-symbol daily snapshots (git-ignored)
├── requirements.txt
├── .env.example
├── .streamlit/
│   ├── config.toml
│   └── secrets.toml.example
└── .gitignore
```

## 1. Run locally

```bash
git clone <your-repo-url>
cd nse-mcx-premium-terminal
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env -> DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN

streamlit run app.py
```

No credentials? The app still runs — it just falls back to a clearly
labeled `SIMULATED` feed so you can try the UI without a broker account.
Access tokens on Dhan's standard plan expire periodically; if the header
badge switches from **DHAN LIVE** to **SIMULATED** unexpectedly,
regenerate the token.

**First run for any symbol** will show "baseline pending" — there's no
previous day's snapshot yet. Once the market has been open for one tick,
that reading is saved, and it becomes tomorrow's baseline automatically.

## 2. Deploy to Streamlit Community Cloud

1. Push the repo (`.env` is git-ignored — never committed).
2. [share.streamlit.io](https://share.streamlit.io) → connect the repo → main file `app.py`.
3. **App settings → Secrets** → paste your `DHAN_CLIENT_ID` /
   `DHAN_ACCESS_TOKEN` (see `.streamlit/secrets.toml.example`).
4. Deploy.

> **Note on `baseline_store.json` persistence:** it's a plain file on
> the app's local disk. On Streamlit Community Cloud that disk persists
> for the life of the running container but is wiped on a redeploy or
> reboot — same as restarting a local process from a fresh checkout. For
> long-term production use, swap `save_snapshot` / `get_previous_baseline`
> in `dhan_service.py` for a real database or cloud key-value store.

## How the data flows

- **Indices** use Dhan's stable index security IDs (`IDX_I`).
- **Stocks** (`NSE_EQ`) and **MCX commodities** (`MCX_COMM`) resolve
  their security ID at runtime from Dhan's public scrip master (cached
  6 hours) — commodity futures roll monthly, so there's no fixed ID to
  hardcode.
- `expiry_list` → nearest expiry (cached 30 min). `option_chain` for
  that expiry is cached **45 seconds** (`CHAIN_CACHE_TTL_SECONDS`) so
  several instruments refreshing on the same tick don't blow through
  Dhan's ~1 request/3s per-underlying option-chain rate limit.
- Once a day's baseline is loaded, every live tick reads CE/PE off the
  **same fixed strike** via `fetch_premium_at_strike` — it does not
  recompute ATM from today's spot. Symbols with no baseline yet use a
  dynamic best-effort ATM reading (`fetch_atm_combined_premium`,
  most-active-by-OI among the nearest few strikes) purely for display,
  and are excluded from spurt/watchlist until a real baseline exists.
- Any failure at any step (expired token, market closed, rate limit,
  symbol not resolvable) falls back per-instrument to the simulated
  feed, clearly tagged `SIM`.
- **Market-hours gating**: `is_market_hours()` (IST, NSE cash/index
  session 9:15–15:30, Mon–Fri) controls whether the app fetches a new
  tick and whether it auto-reruns at all — outside those hours the
  dashboard freezes on its last known state and stops polling Dhan.

## Notes

- Combined Premium = CE LTP + PE LTP at one fixed strike + nearest
  expiry per symbol — never multiple strikes/expiries for the same
  underlying.
- This tool is for monitoring/education — **not investment advice**.
- The MCX night session isn't separately modeled — `is_market_hours()`
  uses the NSE cash/index window for simplicity, same simplification as
  earlier versions.
