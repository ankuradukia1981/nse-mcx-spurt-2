"""
NSE / MCX Combined-Premium Terminal — v6 (Previous-Day Baseline)
---------------------------------------------------------------------
Per PROJECT_SPEC_v6.md:

  - Expiry : real calendar date of the nearest active expiry (never a
             stand-in month; SIM fallback is always labeled SIMULATED,
             never presented as if it were live).
  - Strike : ONE strike per symbol per day, fixed to the PREVIOUS
             trading day's spot: K = round(S_prev / strike_step) * step.
             It does not re-flip to today's live ATM intraday.
  - CE/PE  : live LTPs at that expiry + fixed strike.
  - Combined Premium = CE + PE.
  - Baseline = yesterday's persisted CE+PE sum at that same (E, K).
             Persisted to baseline_store.json on every tick so today's
             final reading becomes tomorrow's baseline automatically.
  - Spurt% = (Combined_today - Baseline) / Baseline * 100, computed
             ONLY after the market has opened today. A symbol with no
             prior-day snapshot shows "baseline pending" and is
             excluded from spurt/watchlist until one exists.
  - Watchlist: default threshold 10%. Once a symbol crosses it, it
             stays on the watchlist for the rest of the day even if it
             cools back off. Cleared only when the next session opens.
  - Chart  : Combined Premium vs session time only (no spot/OI/volume).

Requires a .env (local) or Streamlit Secrets (cloud) with:
    DHAN_CLIENT_ID=...
    DHAN_ACCESS_TOKEN=...
Missing/invalid credentials transparently fall back to a simulated
feed (source tag SIM) rather than crashing.

Run:
    streamlit run app.py
"""
import time
from datetime import date, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import ALL_INSTRUMENTS, DEFAULT_THRESHOLD_PCT, DEFAULT_REFRESH_SECONDS, MAX_HISTORY_POINTS
from dhan_service import (
    get_dhan, dhan_is_connected, fetch_atm_combined_premium, fetch_premium_at_strike,
    resolve_mcx_underlying, resolve_equity_underlying,
    init_sim_state, step_sim, is_market_hours,
    save_snapshot, get_previous_baseline,
)

st.set_page_config(
    page_title="NSE/MCX Premium Terminal",
    page_icon="\U0001F4C8",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ==========================================================================
# THEME
# ==========================================================================
st.markdown("""
<style>
:root{
  --bg-void:#05080f; --bg-panel:#0d121d; --bg-alt:#151b2b; --border:#1e293b;
  --text-main:#e2e8f0; --text-dim:#64748b;
  --amber:#fbbf24; --cyan:#06b6d4; --up:#10b981; --down:#ef4444; --alert:#f97316;
}
html, body, [class*="css"] { font-family:'JetBrains Mono','SF Mono',Consolas,monospace; }
.stApp{ background:var(--bg-void); }
#MainMenu, header[data-testid="stHeader"], footer{ visibility:hidden; height:0; }
.block-container{ padding-top:1rem; max-width:1400px; }
h1,h2,h3,h4,h5{ color:var(--text-main)!important; font-family:'JetBrains Mono',monospace!important; }

.row-header{ display:flex; justify-content:space-between; align-items:center;
  padding:10px 16px; background:var(--bg-panel); border:1px solid var(--border);
  border-radius:4px; margin-bottom:8px; }
.brand{ font-size:17px; font-weight:800; color:var(--amber); letter-spacing:1px; }
.brand span{ color:var(--text-dim); font-size:11px; font-weight:400; margin-left:10px; }
.status-bar{ display:flex; gap:16px; align-items:center; }
.pill{ display:inline-flex; align-items:center; gap:6px; padding:3px 10px; border-radius:4px;
  font-size:11px; font-weight:700; letter-spacing:.5px; text-transform:uppercase; }
.pill::before{ content:''; width:7px; height:7px; border-radius:50%; }
.pill-live{ background:rgba(16,185,129,.15); color:var(--up); border:1px solid rgba(16,185,129,.35); }
.pill-live::before{ background:var(--up); box-shadow:0 0 6px var(--up); }
.pill-closed{ background:rgba(239,68,68,.15); color:var(--down); border:1px solid rgba(239,68,68,.35); }
.pill-closed::before{ background:var(--down); }
.pill-src-live{ background:rgba(6,182,212,.15); color:var(--cyan); border:1px solid rgba(6,182,212,.35); }
.pill-src-live::before{ background:var(--cyan); }
.pill-src-sim{ background:rgba(251,191,36,.15); color:var(--amber); border:1px solid rgba(251,191,36,.35); }
.pill-src-sim::before{ background:var(--amber); }
.clock{ color:var(--text-main); font-size:13px; font-variant-numeric:tabular-nums; }

.row-ticker{ background:var(--bg-alt); border:1px solid var(--border); border-radius:4px;
  padding:6px 14px; display:flex; gap:20px; overflow-x:auto; white-space:nowrap;
  font-size:11px; margin-bottom:8px; }
.tick-up{ color:var(--up); font-weight:600; } .tick-down{ color:var(--down); font-weight:600; }
.tick-spike{ color:var(--alert); font-weight:800; }

.panel{ background:var(--bg-panel); border:1px solid var(--border); border-radius:4px;
  padding:12px 14px; height:100%; }
.panel-title{ font-size:11px; color:var(--amber); text-transform:uppercase; letter-spacing:1px;
  margin-bottom:10px; padding-bottom:6px; border-bottom:1px solid var(--border);
  display:flex; justify-content:space-between; }
.alert-row{ display:flex; justify-content:space-between; align-items:center;
  padding:6px 8px; background:var(--bg-alt); border-left:3px solid var(--alert);
  border-radius:2px; font-size:11px; margin-bottom:6px; }
.alert-time{ color:var(--text-dim); font-size:10px; }
.alert-sym{ color:var(--cyan); font-weight:700; }
.alert-pct{ color:var(--alert); font-weight:700; }
.empty-state{ color:var(--text-dim); font-style:italic; font-size:11px; text-align:center; padding:20px 0; }
.stat-row{ display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px dashed var(--border); font-size:12px; }
.stat-lbl{ color:var(--text-dim); } .stat-val{ color:var(--text-main); font-weight:700; }

.wl-header{ padding:8px 4px; font-size:11px; color:var(--amber); text-transform:uppercase;
  letter-spacing:1px; display:flex; gap:12px; align-items:center; }
.wl-badge{ color:var(--alert); font-weight:700; }
.wl-count{ color:var(--text-dim); font-weight:400; }
.tag-spike{ background:rgba(249,115,22,.18); color:var(--alert); padding:2px 7px; border-radius:3px;
  font-size:10px; font-weight:700; }
.tag-cooled{ background:rgba(100,116,139,.18); color:var(--text-dim); padding:2px 7px; border-radius:3px;
  font-size:10px; font-weight:700; }
.text-up{ color:var(--up); } .text-down{ color:var(--down); } .text-dim{ color:var(--text-dim); }
.spot-sub{ color:var(--text-dim); font-size:10px; }

.stButton>button{ background:var(--bg-alt); border:1px solid var(--border); color:var(--text-main);
  font-family:'JetBrains Mono',monospace; font-size:11px; border-radius:3px; padding:4px 12px; }
.stButton>button:hover{ border-color:var(--cyan); color:var(--cyan); }
.newtab-link{ color:var(--text-dim); text-decoration:none; font-size:13px; }
.newtab-link:hover{ color:var(--cyan); }

.foot{ font-size:10px; color:var(--text-dim); padding:8px 4px; }
</style>
""", unsafe_allow_html=True)

# ==========================================================================
# STATE
# ==========================================================================
_defaults = {
    "history": {sym: [] for sym in ALL_INSTRUMENTS},
    "sim_state": {sym: init_sim_state(cfg) for sym, cfg in ALL_INSTRUMENTS.items()},
    "alerts": [],
    "resolved_ids": {},
    "paused": False,
    "spike_count_today": 0,
    "view": "dashboard",
    "active_sym": "NIFTY",
    "session_date": None,   # trading day the baseline/watchlist below belongs to
    "day_baseline": {},     # sym -> {date,spot,expiry,strike,ce_ltp,pe_ltp,combined_premium} | None
    "spiked_today": set(),  # symbols that have crossed threshold at least once today
}
for _k, _v in _defaults.items():
    if _k not in st.session_state:
        st.session_state[_k] = _v

_qp = st.query_params
if _qp.get("view") == "chart" and _qp.get("symbol") in ALL_INSTRUMENTS:
    st.session_state.view = "chart"
    st.session_state.active_sym = _qp.get("symbol")

dhan_client = get_dhan()
LIVE = dhan_is_connected(dhan_client)
MARKET_OPEN = is_market_hours()
TODAY = date.today()
TODAY_STR = TODAY.isoformat()

# ==========================================================================
# DATA LAYER
# ==========================================================================
def resolve_security_id(sym: str, cfg: dict):
    if cfg["security_id"] is not None:
        return cfg["security_id"], cfg["segment"]
    cached = st.session_state.resolved_ids.get(sym)
    if cached:
        return cached[0], cfg["segment"]
    if cfg["asset_class"] == "COMMODITY":
        sec_id, _expiry, _tsym = resolve_mcx_underlying(cfg["lookup_symbol"])
    elif cfg["asset_class"] == "EQUITY":
        sec_id = resolve_equity_underlying(cfg["lookup_symbol"])
    else:
        sec_id = None
    st.session_state.resolved_ids[sym] = (sec_id, cfg["segment"])
    return sec_id, cfg["segment"]


def get_reading(sym: str, cfg: dict, baseline):
    """baseline=None -> no fixed strike yet, use dynamic ATM (display-only,
    excluded from spurt). baseline set -> fetch the FIXED strike from §3.4."""
    if LIVE:
        try:
            security_id, segment = resolve_security_id(sym, cfg)
            if security_id is not None:
                if baseline is not None:
                    reading = fetch_premium_at_strike(dhan_client, security_id, segment, baseline["strike"])
                else:
                    reading = fetch_atm_combined_premium(dhan_client, security_id, segment, cfg["strike_step"])
                if reading is not None:
                    reading["source"] = "LIVE"
                    return reading
        except Exception:
            pass
    reading = step_sim(st.session_state.sim_state[sym])
    reading["source"] = "SIM"
    return reading


def load_day_baselines():
    """Loads each symbol's fixed (E, K) baseline from yesterday's (or the
    last available prior day's) persisted snapshot. Symbols with no prior
    snapshot get None -> "baseline pending", excluded from spurt/watchlist
    until a first snapshot is captured today."""
    baselines = {}
    for sym, cfg in ALL_INSTRUMENTS.items():
        prev = get_previous_baseline(sym, TODAY_STR)
        if prev and prev.get("spot") and prev.get("combined_premium"):
            step = cfg["strike_step"]
            strike = round(float(prev["spot"]) / step) * step
            baselines[sym] = {**prev, "strike": strike}
        else:
            baselines[sym] = None
    return baselines


def run_universe_tick(threshold: float):
    now_str = datetime.now().strftime("%H:%M:%S")
    for sym, cfg in ALL_INSTRUMENTS.items():
        baseline = st.session_state.day_baseline.get(sym)
        reading = get_reading(sym, cfg, baseline)

        if MARKET_OPEN and baseline is not None:
            bp = baseline["combined_premium"]
            pct_chg = ((reading["combined_premium"] - bp) / bp) * 100 if bp else 0.0
            is_spike = abs(pct_chg) >= threshold
        else:
            pct_chg = None
            is_spike = False

        record = {
            **reading, "time": now_str, "pct_chg": pct_chg, "is_spike": is_spike,
            "baseline": baseline["combined_premium"] if baseline else None,
        }
        hist = st.session_state.history[sym]
        hist.append(record)
        if len(hist) > MAX_HISTORY_POINTS:
            hist.pop(0)

        if is_spike:
            st.session_state.spiked_today.add(sym)
            already_recent = any(
                a["symbol"] == sym and (datetime.now() - a["ts"]).seconds < 25 for a in st.session_state.alerts
            )
            if not already_recent:
                st.session_state.alerts.insert(0, {
                    "symbol": sym, "ts": datetime.now(), "time": now_str, "pct": pct_chg,
                    "premium": reading["combined_premium"], "expiry": reading.get("expiry", "-"),
                })
                st.session_state.alerts = st.session_state.alerts[:50]
                st.session_state.spike_count_today += 1

        # Persist today's snapshot on every tick -> tomorrow's baseline is
        # automatically whatever the last successful tick recorded today.
        save_snapshot(sym, TODAY_STR, {
            "spot": reading.get("spot"), "expiry": reading.get("expiry"),
            "strike": reading.get("atm_strike"), "ce_ltp": reading.get("ce_ltp"),
            "pe_ltp": reading.get("pe_ltp"), "combined_premium": reading.get("combined_premium"),
        })


# ==========================================================================
# CONTROLS
# ==========================================================================
ctrl1, ctrl2, ctrl3, ctrl4, ctrl5 = st.columns([1.4, 1.4, 1.7, 1, 1])
with ctrl1:
    threshold = st.number_input("Spike threshold (%)", min_value=0.5, max_value=50.0,
                                 value=DEFAULT_THRESHOLD_PCT, step=0.5)
with ctrl2:
    refresh_secs = st.slider("Auto-refresh (s)", 5, 60, DEFAULT_REFRESH_SECONDS)
with ctrl3:
    only_active_now = st.checkbox("Show only \u2265 threshold right now", value=False,
                                   help="Off (default): show every symbol that has crossed "
                                        "threshold at any point today, even if it has since "
                                        "cooled off. On: narrow that list to symbols still "
                                        "past threshold on this tick.")
with ctrl4:
    if st.button("\u23f8\ufe0f Pause" if not st.session_state.paused else "\u25b6\ufe0f Resume"):
        st.session_state.paused = not st.session_state.paused
        st.rerun()
with ctrl5:
    if st.button("\U0001F504 Reload baselines"):
        st.session_state.session_date = None  # forces the block below to reload
        st.rerun()

# ==========================================================================
# NEW TRADING DAY? — load fixed (E, K) baselines from the last prior
# snapshot and clear the day-locked watchlist, exactly when the market
# opens on a day we haven't already loaded baselines for.
# ==========================================================================
if MARKET_OPEN and st.session_state.session_date != TODAY:
    st.session_state.history = {s: [] for s in ALL_INSTRUMENTS}
    st.session_state.alerts = []
    st.session_state.spike_count_today = 0
    st.session_state.spiked_today = set()
    st.session_state.day_baseline = load_day_baselines()
    st.session_state.session_date = TODAY

# ==========================================================================
# REFRESH — pre-open: last known/frozen data only, no spurt, no new
# watchlist entries. Once MARKET_OPEN goes False for the day, auto-rerun
# stops entirely (no polling Dhan after hours).
# ==========================================================================
have_any_history = any(st.session_state.history[s] for s in ALL_INSTRUMENTS)
should_fetch = not st.session_state.paused and (MARKET_OPEN or not have_any_history)
if should_fetch:
    run_universe_tick(threshold)

current_records = {sym: st.session_state.history[sym][-1] for sym in ALL_INSTRUMENTS if st.session_state.history[sym]}

# ==========================================================================
# HEADER
# ==========================================================================
market_pill = '<span class="pill pill-live">LIVE</span>' if MARKET_OPEN else '<span class="pill pill-closed">MARKET CLOSE</span>'
src_pill = '<span class="pill pill-src-live">DHAN LIVE</span>' if LIVE else '<span class="pill pill-src-sim">SIMULATED</span>'
now = datetime.now()
st.markdown(
    f'<div class="row-header">'
    f'<div class="brand">\u25c6 NSE/MCX PREMIUM TERMINAL<span>ATM Combined Premium Spike Monitor</span></div>'
    f'<div class="status-bar">{src_pill}{market_pill}'
    f'<span class="clock">{now.strftime("%H:%M:%S")} IST &nbsp;|&nbsp; {now.strftime("%d %b %Y")}</span></div>'
    f'</div>', unsafe_allow_html=True,
)

# ==========================================================================
# TICKER STRIP
# ==========================================================================
ticker_html = '<div class="row-ticker">'
for sym, cfg in ALL_INSTRUMENTS.items():
    r = current_records.get(sym)
    if not r:
        continue
    pct = r.get("pct_chg")
    if pct is None:
        cls, pct_txt = "text-dim", ("pre-open" if not MARKET_OPEN else "baseline pending")
    else:
        cls = "tick-spike" if r["is_spike"] else ("tick-up" if pct >= 0 else "tick-down")
        arrow = "\u25b2" if pct >= 0 else "\u25bc"
        pct_txt = f"{arrow}{pct:+.2f}%"
    ticker_html += (f'<span><b>{cfg["label"]}</b> \u20b9{r["combined_premium"]:.1f} '
                     f'<span class="{cls}">{pct_txt}</span></span>')
ticker_html += '</div>'
st.markdown(ticker_html, unsafe_allow_html=True)

# ==========================================================================
# CHART VIEW — Combined Premium only, per §6 of the v6 spec
# ==========================================================================
if st.session_state.view == "chart" and st.session_state.active_sym in ALL_INSTRUMENTS:
    sym = st.session_state.active_sym
    inst = ALL_INSTRUMENTS[sym]
    hist = st.session_state.history[sym]

    top_l, top_r = st.columns([5, 1])
    with top_l:
        st.markdown(f"### {inst['label']} \u2014 Combined Premium")
    with top_r:
        if st.button("\u2190 Back to Dashboard"):
            st.session_state.view = "dashboard"
            st.query_params.clear()
            st.rerun()

    if hist:
        df = pd.DataFrame(hist)
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=df["time"], y=df["combined_premium"], name="Combined Premium",
            line=dict(color="#fbbf24", width=2.5), fill="tozeroy",
            fillcolor="rgba(251,191,36,0.10)",
        ))
        baseline_val = hist[-1].get("baseline")
        if baseline_val:
            fig.add_hline(y=baseline_val, line_dash="dot", line_color="#64748b",
                           annotation_text="Prev-day baseline", annotation_font_color="#64748b")
        fig.update_layout(
            height=480, margin=dict(l=10, r=10, t=30, b=10),
            paper_bgcolor="#0d121d", plot_bgcolor="#0d121d",
            font=dict(color="#e2e8f0", family="JetBrains Mono"),
            legend=dict(orientation="h", y=1.08, font=dict(size=10)),
            xaxis=dict(title="Session time", showgrid=True, gridcolor="#1e293b", nticks=12),
            yaxis=dict(title="Combined Premium (\u20b9)", showgrid=True, gridcolor="#1e293b"),
        )
        st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})
        latest = hist[-1]
        st.caption(f"Expiry: {latest.get('expiry', '-')} \u2022 Strike: {latest.get('atm_strike', '-')} "
                   f"\u2022 CE: {latest.get('ce_ltp', 0):.2f} \u2022 PE: {latest.get('pe_ltp', 0):.2f} "
                   f"\u2022 Combined: \u20b9{latest.get('combined_premium', 0):.2f} \u2022 Source: {latest.get('source', '-')}")
    else:
        st.caption("No ticks yet for this symbol.")

# ==========================================================================
# DASHBOARD VIEW
# ==========================================================================
else:
    p_left, p_right = st.columns(2)
    with p_left:
        alerts_html = '<div class="panel"><div class="panel-title"><span>\u26a1 Spike Alerts</span>' \
                      f'<span style="color:var(--alert)">{st.session_state.spike_count_today} today</span></div>'
        if not st.session_state.alerts:
            alerts_html += '<div class="empty-state">Waiting for spikes\u2026</div>'
        else:
            for a in st.session_state.alerts[:8]:
                direction = "\u25b2" if a["pct"] >= 0 else "\u25bc"
                label = ALL_INSTRUMENTS.get(a["symbol"], {}).get("label", a["symbol"])
                alerts_html += (
                    f'<div class="alert-row"><span><span class="alert-time">{a["time"]}</span> '
                    f'<span class="alert-sym">{label}</span> (exp {a.get("expiry", "-")})</span>'
                    f'<span class="alert-pct">{direction}{a["pct"]:+.2f}%</span></div>'
                )
        alerts_html += '</div>'
        st.markdown(alerts_html, unsafe_allow_html=True)

    with p_right:
        active = st.session_state.active_sym
        h = st.session_state.history.get(active, [])
        baseline = st.session_state.day_baseline.get(active)
        stats_html = '<div class="panel"><div class="panel-title"><span>Session Stats</span>' \
                     f'<span style="color:var(--text-dim)">{ALL_INSTRUMENTS.get(active, {}).get("label", active)}</span></div>'
        if h:
            prems = [x["combined_premium"] for x in h]
            sym_events = sum(1 for a in st.session_state.alerts if a["symbol"] == active)
            for lbl, val in [
                ("Expiry", h[-1].get("expiry", "-")),
                ("Strike", f"{h[-1].get('atm_strike', '-')}"),
                ("Prev-day Baseline", f"\u20b9{baseline['combined_premium']:.2f} (spot {baseline['spot']:.2f})"
                                       if baseline else "pending \u2014 no prior snapshot"),
                ("Max Prem Today", f"\u20b9{max(prems):.2f}"),
                ("Min Prem Today", f"\u20b9{min(prems):.2f}"),
                ("Vol Events", f"{sym_events}"),
            ]:
                stats_html += f'<div class="stat-row"><span class="stat-lbl">{lbl}</span><span class="stat-val">{val}</span></div>'
        else:
            stats_html += '<div class="empty-state">No ticks yet.</div>'
        stats_html += '</div>'
        st.markdown(stats_html, unsafe_allow_html=True)

    st.write("")

    # ---- WATCHLIST: permanent for the day once a symbol has spiked ----
    member_syms = [s for s in st.session_state.spiked_today if s in current_records]
    if only_active_now:
        member_syms = [s for s in member_syms if abs(current_records[s].get("pct_chg") or 0) >= threshold]
    member_syms.sort(key=lambda s: abs(current_records[s].get("pct_chg") or 0), reverse=True)

    st.markdown(
        f'<div class="wl-header">F&O Watchlist \u2014 Combined Premium Monitor '
        f'<span class="wl-badge">\u2265{threshold:.1f}% vs prev-day close \u2014 STAYS FOR THE DAY</span>'
        f'<span class="wl-count">{len(member_syms)} symbol(s) today</span></div>',
        unsafe_allow_html=True,
    )

    if not member_syms:
        st.markdown('<div class="empty-state">No instrument has crossed the '
                     f'\u00b1{threshold:.1f}% threshold vs its previous-day baseline yet today.</div>',
                     unsafe_allow_html=True)
    else:
        hdr = st.columns([1.5, 1.1, 0.9, 0.8, 0.8, 1.2, 0.9, 0.9, 0.9])
        for c, label in zip(hdr, ["Symbol", "Expiry", "Strike", "CE", "PE",
                                    "Combined Premium", "% Chg", "Status", ""]):
            c.markdown(f'<span style="color:var(--text-dim);font-size:10px;text-transform:uppercase">{label}</span>',
                       unsafe_allow_html=True)

        for sym in member_syms:
            r = current_records[sym]
            label = ALL_INSTRUMENTS[sym]["label"]
            pct = r.get("pct_chg")
            pct_cls = "text-up" if (pct or 0) >= 0 else "text-down"
            pct_txt = f'{pct:+.2f}%' if pct is not None else '\u2013'
            currently_spiking = pct is not None and abs(pct) >= threshold
            status_html = ('<span class="tag-spike">SPIKE</span>' if currently_spiking
                            else '<span class="tag-cooled">COOLED</span>')
            strike_val = r.get("atm_strike")
            strike_txt = f"{strike_val:,.0f}" if isinstance(strike_val, (int, float)) else "-"
            spot_val = r.get("spot")
            spot_txt = f"{spot_val:,.2f}" if isinstance(spot_val, (int, float)) else "-"

            cols = st.columns([1.5, 1.1, 0.9, 0.8, 0.8, 1.2, 0.9, 0.9, 0.9])
            cols[0].markdown(f"**{label}**<br><span class='spot-sub'>Spot {spot_txt}</span>", unsafe_allow_html=True)
            cols[1].markdown(f"{r.get('expiry', '-')}")
            cols[2].markdown(strike_txt)
            cols[3].markdown(f"{r['ce_ltp']:.2f}")
            cols[4].markdown(f"{r['pe_ltp']:.2f}")
            cols[5].markdown(f"\u20b9{r['combined_premium']:.2f}")
            cols[6].markdown(f'<span class="{pct_cls}">{pct_txt}</span>', unsafe_allow_html=True)
            cols[7].markdown(status_html, unsafe_allow_html=True)
            with cols[8]:
                b1, b2 = st.columns([2, 1])
                if b1.button("Chart", key=f"chart_{sym}"):
                    st.session_state.active_sym = sym
                    st.session_state.view = "chart"
                    st.rerun()
                b2.markdown(f'<a class="newtab-link" href="?view=chart&symbol={sym}" '
                            f'target="_blank" title="Open chart in a new tab">\u2197</a>',
                            unsafe_allow_html=True)

    st.markdown(
        '<div class="foot">Combined Premium = CE LTP + PE LTP at ONE fixed strike per symbol '
        '(nearest to the previous trading day\'s spot) and the nearest active expiry. % Chg is '
        'measured against yesterday\'s persisted Combined Premium at that same strike, computed only '
        'after today\'s opening bell. A symbol stays on the watchlist for the rest of the day once it '
        'has crossed threshold, even if it cools off \u2014 not investment advice.</div>',
        unsafe_allow_html=True,
    )

# ==========================================================================
# AUTO-REFRESH — stops entirely once the market is closed for the day
# ==========================================================================
if MARKET_OPEN and not st.session_state.paused:
    time.sleep(refresh_secs)
    st.rerun()
