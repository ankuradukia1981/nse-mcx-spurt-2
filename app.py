"""
NSE / MCX Combined-Premium Terminal (v2)
------------------------------------------
A Bloomberg-terminal-style Streamlit dashboard tracking ATM (CE+PE) combined
option premium across NSE indices, NSE stocks, and MCX commodities, flagging
>=X% spikes from session baseline. Uses live Dhan API data when valid
credentials are configured, and falls back to a realistic simulated feed
otherwise (e.g. local dev without keys, market closed, API hiccup).

v2 improvements over the first cut:
  - Every instrument in the universe refreshes EVERY cycle (not just the
    focused one), so the ticker strip and watchlist are genuinely live
    instead of only updating whichever row you last clicked.
  - Scrolling ticker strip + market OPEN/CLOSED indicator.
  - Per-row LIVE/SIM source tag - some symbols can be live while others
    fall back individually (e.g. one API call rate-limited, others fine).
  - OI, PCR (put/call OI ratio) and ATM delta columns.
  - Session stats panel (max/min/avg premium, spike count) for the
    focused symbol, "show only spikes" filter, and a Pause control.

Run:
    streamlit run app.py

Requires a .env (local) or Streamlit Secrets (cloud) with:
    DHAN_CLIENT_ID=...
    DHAN_ACCESS_TOKEN=...
"""
import time
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import (
    ALL_INSTRUMENTS, DEFAULT_THRESHOLD_PCT, DEFAULT_REFRESH_SECONDS, MAX_HISTORY_POINTS,
)
from dhan_service import (
    get_dhan, dhan_is_connected, fetch_atm_combined_premium,
    resolve_mcx_underlying, resolve_equity_underlying, search_scrip_master,
    init_sim_state, step_sim, is_market_hours,
)

st.set_page_config(
    page_title="NSE/MCX Premium Terminal",
    page_icon="\U0001F4C8",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ==========================================================================
# THEME - matches the standalone premium-terminal.html palette exactly
# ==========================================================================
st.markdown("""
<style>
:root{
  --bg-void:#05080f; --bg-panel:#0d121d; --bg-alt:#151b2b; --border:#1e293b;
  --text-main:#e2e8f0; --text-dim:#64748b;
  --amber:#fbbf24; --cyan:#06b6d4; --up:#10b981; --down:#ef4444; --alert:#f97316;
}
html, body, [class*="css"]  { font-family: 'JetBrains Mono','SF Mono',Consolas,monospace; }
.stApp { background: var(--bg-void); }
section[data-testid="stSidebar"] { background: var(--bg-panel); border-right: 1px solid var(--border); }
h1, h2, h3, h4, h5 { color: var(--text-main) !important; font-family: 'JetBrains Mono',monospace !important; }
.term-header{
  display:flex; justify-content:space-between; align-items:center;
  padding:10px 4px 10px; border-bottom:1px solid var(--border); margin-bottom:2px;
}
.term-brand{ font-size:22px; font-weight:800; letter-spacing:1px; color:var(--amber); }
.term-sub{ font-size:11px; letter-spacing:2px; color:var(--text-dim); text-transform:uppercase; }
.badge{ display:inline-block; padding:3px 10px; border-radius:4px; font-size:11px; font-weight:700;
  letter-spacing:.5px; text-transform:uppercase; }
.badge-live{ background:rgba(16,185,129,.15); color:var(--up); border:1px solid rgba(16,185,129,.35); }
.badge-sim{ background:rgba(251,191,36,.15); color:var(--amber); border:1px solid rgba(251,191,36,.35); }
.badge-open{ background:rgba(16,185,129,.15); color:var(--up); border:1px solid rgba(16,185,129,.35); }
.badge-closed{ background:rgba(239,68,68,.15); color:var(--down); border:1px solid rgba(239,68,68,.35); }
.ticker-strip{
  display:flex; gap:20px; overflow-x:auto; white-space:nowrap; padding:7px 10px;
  background:var(--bg-alt); border:1px solid var(--border); border-radius:4px; margin-bottom:14px;
  font-size:11.5px;
}
.ticker-item b{ color:var(--text-main); margin-right:5px; }
.tick-up{ color:var(--up); font-weight:600; } .tick-down{ color:var(--down); font-weight:600; }
.tick-spike{ color:var(--alert); font-weight:800; }
.stat-card{
  background:var(--bg-alt); border:1px solid var(--border); border-radius:4px;
  padding:12px 14px; height:100%;
}
.stat-lbl{ font-size:10px; letter-spacing:1.5px; color:var(--text-dim); text-transform:uppercase; }
.stat-val{ font-size:24px; font-weight:800; margin-top:3px; color:var(--text-main); }
.stat-val.up{ color:var(--up); } .stat-val.down{ color:var(--down); } .stat-val.spike{ color:var(--alert); }
.stat-sub{ font-size:10.5px; color:var(--text-dim); margin-top:2px; }
.alert-banner{
  background:linear-gradient(90deg, rgba(249,115,22,.18), rgba(249,115,22,.03));
  border:1px solid var(--alert); border-left:4px solid var(--alert);
  padding:10px 16px; border-radius:3px; margin-bottom:14px; font-size:13px; color:var(--text-main);
}
.watch-spike{ color:var(--alert); font-weight:700; }
.watch-up{ color:var(--up); font-weight:600; }
.watch-down{ color:var(--down); font-weight:600; }
.mini-stat-row{ display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px dashed var(--border); font-size:12px; }
[data-testid="stMetricValue"] { font-family: 'JetBrains Mono',monospace; }
</style>
""", unsafe_allow_html=True)

# ==========================================================================
# STATE
# ==========================================================================
if "history" not in st.session_state:
    st.session_state.history = {sym: [] for sym in ALL_INSTRUMENTS}
if "sim_state" not in st.session_state:
    st.session_state.sim_state = {sym: init_sim_state(cfg) for sym, cfg in ALL_INSTRUMENTS.items()}
if "alerts" not in st.session_state:
    st.session_state.alerts = []
if "resolved_ids" not in st.session_state:
    st.session_state.resolved_ids = {}  # sym -> (security_id, segment)
if "paused" not in st.session_state:
    st.session_state.paused = False
if "spike_count_today" not in st.session_state:
    st.session_state.spike_count_today = 0

dhan_client = get_dhan()
LIVE = dhan_is_connected(dhan_client)
MARKET_OPEN = is_market_hours()

# ==========================================================================
# SIDEBAR - CONTROLS
# ==========================================================================
with st.sidebar:
    st.markdown("### \u25c6 TERMINAL CONTROLS")

    symbol = st.selectbox(
        "Focus instrument", list(ALL_INSTRUMENTS.keys()),
        format_func=lambda s: ALL_INSTRUMENTS[s]["label"],
    )
    inst = ALL_INSTRUMENTS[symbol]

    threshold = st.number_input("Spike alert threshold (%)", min_value=0.5, max_value=50.0,
                                 value=DEFAULT_THRESHOLD_PCT, step=0.5)
    refresh_secs = st.slider("Auto-refresh (seconds)", 5, 60, DEFAULT_REFRESH_SECONDS)
    auto_refresh = st.toggle("Auto-refresh", value=True)
    only_spikes = st.checkbox("Show only \u2265 threshold in watchlist", value=False)

    col_a, col_b = st.columns(2)
    with col_a:
        if st.button("\u23f8\ufe0f Pause" if not st.session_state.paused else "\u25b6\ufe0f Resume",
                      use_container_width=True):
            st.session_state.paused = not st.session_state.paused
            st.rerun()
    with col_b:
        if st.button("\U0001F504 Reset all", use_container_width=True):
            st.session_state.history = {s: [] for s in ALL_INSTRUMENTS}
            st.session_state.sim_state = {s: init_sim_state(c) for s, c in ALL_INSTRUMENTS.items()}
            st.session_state.alerts = []
            st.session_state.spike_count_today = 0
            st.rerun()

    st.markdown("---")
    st.markdown("##### \U0001F50D Look up a Security ID")
    st.caption("MCX commodity underlyings roll monthly — search Dhan's "
               "live scrip master here instead of relying on a hardcoded ID.")
    q = st.text_input("Search trading symbol", placeholder="e.g. CRUDEOIL, RELIANCE")
    if q:
        try:
            hits = search_scrip_master(q, limit=15)
            if hits.empty:
                st.caption("No matches.")
            else:
                st.dataframe(hits, use_container_width=True, height=220)
        except Exception as e:
            st.caption(f"Lookup failed: {e}")

    st.markdown("---")
    badge = '<span class="badge badge-live">\u25cf LIVE \u2014 DHAN</span>' if LIVE \
        else '<span class="badge badge-sim">\u25cf SIMULATED</span>'
    st.markdown(badge, unsafe_allow_html=True)
    if not LIVE:
        st.caption(
            "No valid Dhan credentials found (or connection failed) — running "
            "on a realistic simulated feed. Add DHAN_CLIENT_ID / "
            "DHAN_ACCESS_TOKEN to `.env` (local) or Streamlit Secrets (cloud) to go live."
        )

# ==========================================================================
# HEADER
# ==========================================================================
h1, h2, h3 = st.columns([2.4, 0.9, 0.9])
with h1:
    st.markdown(
        '<div class="term-header">'
        '<div><div class="term-brand">\u25c6 NSE/MCX PREMIUM TERMINAL</div>'
        '<div class="term-sub">ATM Combined Premium Spike Monitor</div></div>'
        '</div>', unsafe_allow_html=True
    )
with h2:
    mkt_badge = '<span class="badge badge-open">\u25cf MARKET OPEN</span>' if MARKET_OPEN \
        else '<span class="badge badge-closed">\u25cf MARKET CLOSED</span>'
    st.markdown(f"<div style='padding-top:14px'>{mkt_badge}</div>", unsafe_allow_html=True)
with h3:
    st.markdown(f"<div style='text-align:right; padding-top:16px; color:#64748b; font-size:13px;'>"
                f"{datetime.now().strftime('%H:%M:%S')} &nbsp;|&nbsp; {datetime.now().strftime('%d %b %Y')}"
                f"</div>", unsafe_allow_html=True)


# ==========================================================================
# DATA FETCH
# ==========================================================================
def resolve_security_id(sym: str, cfg: dict):
    """Returns (security_id, segment) for live calls, resolving dynamically
    (and caching in session_state) for equities & MCX commodities."""
    if cfg["security_id"] is not None:
        return cfg["security_id"], cfg["segment"]
    cached = st.session_state.resolved_ids.get(sym)
    if cached:
        return cached[0], cfg["segment"]
    if cfg["asset_class"] == "COMMODITY":
        sec_id, expiry, tsym = resolve_mcx_underlying(cfg["lookup_symbol"])
    elif cfg["asset_class"] == "EQUITY":
        sec_id = resolve_equity_underlying(cfg["lookup_symbol"])
    else:
        sec_id = None
    st.session_state.resolved_ids[sym] = (sec_id, cfg["segment"])
    return sec_id, cfg["segment"]


def get_reading(sym: str, cfg: dict):
    if LIVE:
        try:
            security_id, segment = resolve_security_id(sym, cfg)
            if security_id is not None:
                reading = fetch_atm_combined_premium(dhan_client, security_id, segment, cfg["strike_step"])
                if reading is not None:
                    reading["source"] = "LIVE"
                    return reading
        except Exception:
            pass
    reading = step_sim(st.session_state.sim_state[sym])
    reading["source"] = "SIM"
    return reading


# ---- refresh the WHOLE universe every cycle (unless paused) ----
current_records = {}
if not st.session_state.paused:
    now_str = datetime.now().strftime("%H:%M:%S")
    for sym, cfg in ALL_INSTRUMENTS.items():
        reading = get_reading(sym, cfg)
        hist = st.session_state.history[sym]
        baseline = hist[0]["combined_premium"] if hist else reading["combined_premium"]
        pct_chg = ((reading["combined_premium"] - baseline) / baseline) * 100 if baseline else 0.0
        is_spike = abs(pct_chg) >= threshold

        record = {**reading, "time": now_str, "pct_chg": pct_chg, "is_spike": is_spike, "baseline": baseline}
        hist.append(record)
        if len(hist) > MAX_HISTORY_POINTS:
            hist.pop(0)
        current_records[sym] = record

        if is_spike:
            already_recent = any(
                a["symbol"] == sym and (datetime.now() - a["ts"]).seconds < 25 for a in st.session_state.alerts
            )
            if not already_recent:
                st.session_state.alerts.insert(0, {
                    "symbol": sym, "ts": datetime.now(),
                    "time": now_str, "pct": pct_chg,
                    "premium": reading["combined_premium"], "spot": reading["spot"],
                })
                st.session_state.alerts = st.session_state.alerts[:50]
                st.session_state.spike_count_today += 1
else:
    for sym in ALL_INSTRUMENTS:
        h = st.session_state.history[sym]
        if h:
            current_records[sym] = h[-1]

record = current_records.get(symbol)
if record is None:
    # nothing accumulated yet for this symbol (e.g. straight after a reset while paused)
    record = get_reading(symbol, inst)
    record["time"] = datetime.now().strftime("%H:%M:%S")
    record["pct_chg"] = 0.0
    record["is_spike"] = False
    record["baseline"] = record["combined_premium"]

hist = st.session_state.history[symbol]
baseline = record["baseline"]
pct_chg = record["pct_chg"]
is_spike = record["is_spike"]
reading = record

# ==========================================================================
# TICKER STRIP
# ==========================================================================
ticker_html = '<div class="ticker-strip">'
for sym, cfg in ALL_INSTRUMENTS.items():
    r = current_records.get(sym)
    if not r:
        continue
    cls = "tick-spike" if r["is_spike"] else ("tick-up" if r["pct_chg"] >= 0 else "tick-down")
    arrow = "\u25b2" if r["pct_chg"] >= 0 else "\u25bc"
    ticker_html += (f'<span class="ticker-item"><b>{cfg["label"]}</b>'
                     f'\u20b9{r["combined_premium"]:.1f} '
                     f'<span class="{cls}">{arrow}{r["pct_chg"]:+.2f}%</span></span>')
ticker_html += '</div>'
st.markdown(ticker_html, unsafe_allow_html=True)

# ==========================================================================
# ALERT BANNER
# ==========================================================================
if is_spike:
    direction = "spiked \u25b2" if pct_chg >= 0 else "dropped \u25bc"
    st.markdown(
        f'<div class="alert-banner">\u26a0\ufe0f <b>SPIKE — {inst["label"]}</b> '
        f'combined premium {direction} <b>{pct_chg:+.2f}%</b> vs session baseline '
        f'(\u20b9{reading["combined_premium"]:.2f})</div>',
        unsafe_allow_html=True,
    )

# ==========================================================================
# STAT CARDS
# ==========================================================================
c1, c2, c3, c4, c5 = st.columns(5)


def stat_card(col, label, value, sub="", cls=""):
    with col:
        st.markdown(
            f'<div class="stat-card"><div class="stat-lbl">{label}</div>'
            f'<div class="stat-val {cls}">{value}</div>'
            f'<div class="stat-sub">{sub}</div></div>',
            unsafe_allow_html=True,
        )


stat_card(c1, "Spot", f"{reading['spot']:,.2f}", f"{inst['label']}")
stat_card(c2, "ATM Strike", f"{reading['atm_strike']:,.0f}", f"Expiry: {reading.get('expiry', '-')}")
stat_card(c3, "CE / PE Premium", f"{reading['ce_ltp']:.2f} / {reading['pe_ltp']:.2f}",
          f"\u0394 {reading.get('ce_delta', '-')} / {reading.get('pe_delta', '-')}")
stat_card(c4, "Combined Premium", f"\u20b9{reading['combined_premium']:.2f}",
          f"baseline \u20b9{baseline:.2f}")
stat_card(c5, "% vs Baseline", f"{pct_chg:+.2f}%",
          f"threshold {threshold:.1f}% \u2022 {reading['source']} \u2022 PCR {reading.get('pcr', '-')}",
          "spike" if is_spike else ("up" if pct_chg >= 0 else "down"))

st.write("")

# ==========================================================================
# CHART + ALERTS + SESSION STATS
# ==========================================================================
df = pd.DataFrame(hist)
left, right = st.columns([3, 1])

with left:
    st.markdown(f"##### {inst['label']} — Spot vs Combined Premium (session)")
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df["time"], y=df["combined_premium"], name="Combined Premium",
        line=dict(color="#fbbf24", width=2), fill="tozeroy",
        fillcolor="rgba(251,191,36,0.08)", yaxis="y1",
    ))
    fig.add_trace(go.Scatter(
        x=df["time"], y=df["spot"], name="Spot",
        line=dict(color="#06b6d4", width=1.5), yaxis="y2",
    ))
    thresh_line = [baseline * (1 + threshold / 100)] * len(df)
    fig.add_trace(go.Scatter(
        x=df["time"], y=thresh_line, name=f"+{threshold:.1f}% threshold",
        line=dict(color="#f97316", width=1, dash="dash"), yaxis="y1",
    ))
    fig.update_layout(
        height=340, margin=dict(l=10, r=10, t=10, b=10),
        paper_bgcolor="#0d121d", plot_bgcolor="#0d121d",
        font=dict(color="#e2e8f0", family="JetBrains Mono"),
        legend=dict(orientation="h", y=1.1, font=dict(size=10)),
        xaxis=dict(showgrid=True, gridcolor="#1e293b", nticks=10),
        yaxis=dict(title="Combined Premium", showgrid=True, gridcolor="#1e293b"),
        yaxis2=dict(title="Spot", overlaying="y", side="right", showgrid=False),
    )
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

with right:
    tab_alerts, tab_stats = st.tabs(["\u26a1 Alerts", "\U0001F4CA Stats"])
    with tab_alerts:
        if not st.session_state.alerts:
            st.caption("Waiting for spikes\u2026")
        else:
            for a in st.session_state.alerts[:12]:
                direction = "\u25b2" if a["pct"] >= 0 else "\u25bc"
                st.markdown(
                    f'<div style="border-bottom:1px solid #1e293b; padding:6px 0; font-size:12px;">'
                    f'<span style="color:#64748b;">{a["time"]}</span> — '
                    f'<b>{ALL_INSTRUMENTS.get(a["symbol"], {}).get("label", a["symbol"])}</b><br>'
                    f'<span class="watch-spike">{direction} {a["pct"]:+.2f}%</span> '
                    f'\u2192 \u20b9{a["premium"]:.2f} (spot {a["spot"]:,.1f})</div>',
                    unsafe_allow_html=True,
                )
    with tab_stats:
        if hist:
            prems = [h["combined_premium"] for h in hist]
            sym_alert_count = sum(1 for a in st.session_state.alerts if a["symbol"] == symbol)
            for lbl, val in [
                ("Session High", f"\u20b9{max(prems):.2f}"),
                ("Session Low", f"\u20b9{min(prems):.2f}"),
                ("Session Avg", f"\u20b9{sum(prems)/len(prems):.2f}"),
                ("Ticks logged", f"{len(prems)}"),
                ("Vol events (this symbol)", f"{sym_alert_count}"),
                ("Vol events (all today)", f"{st.session_state.spike_count_today}"),
            ]:
                st.markdown(f'<div class="mini-stat-row"><span>{lbl}</span><b>{val}</b></div>',
                             unsafe_allow_html=True)
        else:
            st.caption("No ticks yet.")

# ==========================================================================
# WATCHLIST - every instrument in the universe (all refreshed this cycle)
# ==========================================================================
st.markdown("##### F&O Watchlist — Combined Premium Monitor")
rows = []
for s, meta in ALL_INSTRUMENTS.items():
    last = current_records.get(s)
    if not last:
        continue
    if only_spikes and not last.get("is_spike"):
        continue
    rows.append({
        "Symbol": meta["label"],
        "Src": last.get("source", "-"),
        "Spot": f"{last['spot']:,.2f}",
        "Comb. Premium": f"\u20b9{last['combined_premium']:.2f}",
        "% Chg": last.get("pct_chg", 0.0),
        "CE": f"{last['ce_ltp']:.2f}",
        "PE": f"{last['pe_ltp']:.2f}",
        "IV": last.get("atm_iv", "-"),
        "PCR": last.get("pcr", "-"),
        "Status": "SPIKE" if last.get("is_spike") else ("Rising" if last.get("pct_chg", 0) >= 0 else "Falling"),
    })

if rows:
    wdf = pd.DataFrame(rows).sort_values("% Chg", key=abs, ascending=False)
    wdf["% Chg"] = wdf["% Chg"].map(lambda v: f"{v:+.2f}%")
    st.dataframe(wdf, use_container_width=True, hide_index=True, height=min(460, 45 + 38 * len(wdf)))
elif only_spikes:
    st.caption("No instrument is currently past the spike threshold.")
else:
    st.caption("Waiting for the first tick\u2026")

# ==========================================================================
# SESSION LOG (download)
# ==========================================================================
with st.expander("\U0001F4C4 Session tick log — " + inst["label"]):
    log_df = pd.DataFrame(hist)
    st.dataframe(log_df.tail(200)[::-1], use_container_width=True, hide_index=True, height=300)
    st.download_button(
        "Download full session log (CSV)",
        log_df.to_csv(index=False).encode("utf-8"),
        file_name=f"{symbol}_combined_premium_session.csv",
        mime="text/csv",
    )

st.caption(
    "Combined Premium = ATM Call LTP + ATM Put LTP (straddle value). PCR = total Put OI / "
    "total Call OI across the chain (>1 typically read as bullish positioning, <1 bearish — "
    "context dependent). A premium spike with a flat spot often signals rising IV / event risk; "
    "a drop with a flat spot suggests theta/IV crush. Not investment advice."
)

# ==========================================================================
# AUTO-REFRESH
# ==========================================================================
if auto_refresh and not st.session_state.paused:
    time.sleep(refresh_secs)
    st.rerun()
