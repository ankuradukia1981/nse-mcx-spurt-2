"""
Dhan API integration + realistic simulated fallback feed.

Design goal: the terminal must NEVER crash or blank out just because a
credential is missing, an API call fails, or a market is closed. Every
public function here degrades gracefully — if live data can't be fetched
for any reason, callers fall back to `step_sim()`.

Live data path (when DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN are valid):
    1. Resolve the underlying's `security_id` (hardcoded for indices,
       looked up from Dhan's public scrip master for equities & MCX
       commodities — MCX contracts roll monthly so there's no fixed id).
    2. Pull the nearest expiry via `expiry_list`.
    3. Pull the full option chain via `option_chain` and read off the
       ATM strike's CE + PE last price -> combined premium.
"""
import io
import math
import os
import random
import time
from functools import lru_cache

import pandas as pd
import requests
from dotenv import load_dotenv

from config import SCRIP_MASTER_URL, SCRIP_MASTER_CACHE_TTL_SECONDS

load_dotenv()

# ==============================================================================
# CLIENT SETUP
# ==============================================================================
def get_dhan():
    """Builds a dhanhq client from env vars. Returns None if creds are missing
    or the SDK can't be constructed — callers must treat None as 'go simulate'."""
    client_id = os.getenv("DHAN_CLIENT_ID", "").strip()
    access_token = os.getenv("DHAN_ACCESS_TOKEN", "").strip()
    if not client_id or not access_token:
        return None
    try:
        from dhanhq import DhanContext, dhanhq
        ctx = DhanContext(client_id, access_token)
        return dhanhq(ctx)
    except Exception:
        try:
            # older dhanhq releases (<2.0) take creds directly, no DhanContext
            from dhanhq import dhanhq
            return dhanhq(client_id, access_token)
        except Exception:
            return None


def dhan_is_connected(dhan_client) -> bool:
    """Cheap connectivity check — doesn't guarantee every downstream call
    will succeed (segments/permissions can still fail per-call), but
    filters out a missing/garbage credential immediately."""
    if dhan_client is None:
        return False
    try:
        resp = dhan_client.get_fund_limits()
        if isinstance(resp, dict):
            return resp.get("status", "success") != "failure"
        return True
    except Exception:
        return False


# ==============================================================================
# SCRIP MASTER (for dynamic security_id lookup — equities & MCX commodities)
# ==============================================================================
_scrip_cache = {"df": None, "ts": 0}


def _load_scrip_master() -> pd.DataFrame:
    now = time.time()
    if _scrip_cache["df"] is not None and (now - _scrip_cache["ts"]) < SCRIP_MASTER_CACHE_TTL_SECONDS:
        return _scrip_cache["df"]
    resp = requests.get(SCRIP_MASTER_URL, timeout=20)
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text), low_memory=False)
    _scrip_cache["df"] = df
    _scrip_cache["ts"] = now
    return df


def search_scrip_master(query: str, limit: int = 15) -> pd.DataFrame:
    """Free-text search across the scrip master's trading-symbol columns."""
    df = _load_scrip_master()
    cols = [c for c in ("SEM_TRADING_SYMBOL", "SEM_CUSTOM_SYMBOL", "SM_SYMBOL_NAME") if c in df.columns]
    if not cols:
        return pd.DataFrame()
    mask = False
    for c in cols:
        mask = mask | df[c].astype(str).str.contains(query, case=False, na=False)
    hits = df[mask].head(limit)
    keep = [c for c in ("SEM_TRADING_SYMBOL", "SEM_SMST_SECURITY_ID", "SEM_EXM_EXCH_ID",
                         "SEM_SEGMENT", "SEM_INSTRUMENT_NAME", "SEM_EXPIRY_DATE") if c in hits.columns]
    return hits[keep] if keep else hits


def resolve_equity_underlying(lookup_symbol: str):
    """Resolves an NSE cash-equity security_id by exact trading symbol match."""
    df = _load_scrip_master()
    try:
        rows = df[
            (df["SEM_TRADING_SYMBOL"].astype(str).str.upper() == lookup_symbol.upper())
            & (df["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "NSE")
            & (df["SEM_INSTRUMENT_NAME"].astype(str).str.upper().isin(["EQUITY", "ES"]))
        ]
        if rows.empty:
            return None
        return int(rows.iloc[0]["SEM_SMST_SECURITY_ID"])
    except Exception:
        return None


def resolve_mcx_underlying(lookup_symbol: str):
    """Resolves the current-month MCX futures contract (used as the option
    chain's underlying) for a commodity like CRUDEOIL / GOLD / SILVER.
    Returns (security_id, expiry_str, trading_symbol) or (None, None, None)."""
    df = _load_scrip_master()
    try:
        rows = df[
            (df["SEM_TRADING_SYMBOL"].astype(str).str.upper().str.startswith(lookup_symbol.upper()))
            & (df["SEM_EXM_EXCH_ID"].astype(str).str.upper() == "MCX")
            & (df["SEM_INSTRUMENT_NAME"].astype(str).str.upper().str.contains("FUT", na=False))
        ].copy()
        if rows.empty or "SEM_EXPIRY_DATE" not in rows.columns:
            return None, None, None
        rows["SEM_EXPIRY_DATE"] = pd.to_datetime(rows["SEM_EXPIRY_DATE"], errors="coerce")
        rows = rows.dropna(subset=["SEM_EXPIRY_DATE"]).sort_values("SEM_EXPIRY_DATE")
        rows = rows[rows["SEM_EXPIRY_DATE"] >= pd.Timestamp.now()]
        if rows.empty:
            return None, None, None
        near = rows.iloc[0]
        return (int(near["SEM_SMST_SECURITY_ID"]),
                str(near["SEM_EXPIRY_DATE"].date()),
                str(near["SEM_TRADING_SYMBOL"]))
    except Exception:
        return None, None, None


# ==============================================================================
# LIVE OPTION CHAIN
# ==============================================================================
from config import CHAIN_CACHE_TTL_SECONDS

_expiry_cache = {}  # (security_id, segment) -> (expiry, ts)
_EXPIRY_CACHE_TTL = 60 * 30  # expiry lists barely change intraday — cache 30 min

_chain_cache = {}  # (security_id, segment, expiry) -> (parsed_data, ts)


def _nearest_expiry(dhan_client, security_id: int, segment: str):
    """Real calendar date of the nearest ACTIVE expiry, e.g. '2026-08-28'.
    Never hard-coded, never a stand-in month — always whatever Dhan's
    expiry_list returns first for this underlying today."""
    key = (security_id, segment)
    cached = _expiry_cache.get(key)
    if cached and (time.time() - cached[1]) < _EXPIRY_CACHE_TTL:
        return cached[0]
    resp = dhan_client.expiry_list(security_id, segment)
    data = resp.get("data") if isinstance(resp, dict) else resp
    if not data:
        return None
    expiry = data[0]
    _expiry_cache[key] = (expiry, time.time())
    return expiry


def _get_chain_data(dhan_client, security_id: int, segment: str, expiry: str):
    """Cached wrapper around dhan_client.option_chain — Dhan's option-chain
    endpoint is rate-limited to ~1 unique request / 3s per underlying, and
    several instruments can refresh on the same auto-refresh tick, so every
    chain is cached for CHAIN_CACHE_TTL_SECONDS (~45s) regardless of which
    strike ends up being read off of it. Returns {'spot': float, 'oc': {...}}
    or None on any failure."""
    key = (security_id, segment, expiry)
    cached = _chain_cache.get(key)
    if cached and (time.time() - cached[1]) < CHAIN_CACHE_TTL_SECONDS:
        return cached[0]
    resp = dhan_client.option_chain(security_id, segment, expiry)
    data = resp.get("data") if isinstance(resp, dict) else resp
    if not data:
        return None
    oc = data.get("oc", {})
    if not oc:
        return None
    parsed = {"spot": float(data["last_price"]), "oc": oc}
    _chain_cache[key] = (parsed, time.time())
    return parsed


def _leg_reading(oc: dict, best_key, spot: float, expiry: str):
    """Builds the standard reading dict from one strike's CE/PE leg."""
    leg = oc[best_key]
    ce, pe = leg.get("ce", {}) or {}, leg.get("pe", {}) or {}
    ce_ltp = float(ce.get("last_price", 0) or 0)
    pe_ltp = float(pe.get("last_price", 0) or 0)
    ce_iv = ce.get("implied_volatility") or 0
    pe_iv = pe.get("implied_volatility") or 0
    ce_oi = int(ce.get("oi", 0) or 0)
    pe_oi = int(pe.get("oi", 0) or 0)
    ce_volume = int(ce.get("volume", 0) or 0)
    pe_volume = int(pe.get("volume", 0) or 0)
    ce_delta = (ce.get("greeks") or {}).get("delta")
    pe_delta = (pe.get("greeks") or {}).get("delta")
    total_ce_oi = sum(int((v.get("ce") or {}).get("oi", 0) or 0) for v in oc.values())
    total_pe_oi = sum(int((v.get("pe") or {}).get("oi", 0) or 0) for v in oc.values())
    pcr = round(total_pe_oi / total_ce_oi, 2) if total_ce_oi else None
    return {
        "spot": spot,
        "atm_strike": float(best_key),
        "ce_ltp": ce_ltp,
        "pe_ltp": pe_ltp,
        "combined_premium": ce_ltp + pe_ltp,
        "atm_iv": round((ce_iv + pe_iv) / 2, 2) if (ce_iv or pe_iv) else None,
        "ce_oi": ce_oi,
        "pe_oi": pe_oi,
        "ce_volume": ce_volume,
        "pe_volume": pe_volume,
        "combined_volume": ce_volume + pe_volume,
        "pcr": pcr,
        "ce_delta": round(ce_delta, 3) if ce_delta is not None else None,
        "pe_delta": round(pe_delta, 3) if pe_delta is not None else None,
        "expiry": expiry,
    }


def fetch_premium_at_strike(dhan_client, security_id: int, segment: str, target_strike: float):
    """v6 baseline rule: the display strike is fixed to the previous trading
    day's spot (computed by the caller), NOT re-derived from today's live
    spot. This pulls the nearest expiry's chain and reads off whichever
    listed strike is numerically closest to `target_strike`, or None on any
    failure (caller falls back to simulation)."""
    try:
        expiry = _nearest_expiry(dhan_client, security_id, segment)
        if not expiry:
            return None
        chain = _get_chain_data(dhan_client, security_id, segment, expiry)
        if not chain:
            return None
        best_key, best_diff = None, math.inf
        for k in chain["oc"].keys():
            try:
                diff = abs(float(k) - target_strike)
            except ValueError:
                continue
            if diff < best_diff:
                best_diff, best_key = diff, k
        if best_key is None:
            return None
        return _leg_reading(chain["oc"], best_key, chain["spot"], expiry)
    except Exception:
        return None


def fetch_atm_combined_premium(dhan_client, security_id: int, segment: str, strike_step: int):
    """Dynamic ATM reading (today's live spot -> nearest strike, most-active
    by OI among the closest few). Used ONLY as a display fallback for
    symbols that don't have a previous-day baseline yet (so there's no
    fixed target_strike to pass to fetch_premium_at_strike) — once a
    baseline exists, the day's display strike must stay fixed per §3.4 of
    the spec, so callers should prefer fetch_premium_at_strike."""
    try:
        expiry = _nearest_expiry(dhan_client, security_id, segment)
        if not expiry:
            return None
        chain = _get_chain_data(dhan_client, security_id, segment, expiry)
        if not chain:
            return None
        spot, oc = chain["spot"], chain["oc"]
        atm_strike = round(spot / strike_step) * strike_step
        parsed = []
        for k in oc.keys():
            try:
                parsed.append((abs(float(k) - atm_strike), k))
            except ValueError:
                continue
        if not parsed:
            return None
        parsed.sort(key=lambda p: p[0])
        candidates = [k for _, k in parsed[:5]]

        def _leg_oi(key):
            leg = oc[key]
            return int((leg.get("ce") or {}).get("oi", 0) or 0) + int((leg.get("pe") or {}).get("oi", 0) or 0)

        best_key = max(candidates, key=_leg_oi)
        return _leg_reading(oc, best_key, spot, expiry)
    except Exception:
        return None


# ==============================================================================
# SIMULATED FEED (used whenever live data isn't available — no creds,
# market closed, API hiccup, rate limit, etc.). Mirrors the random-walk +
# occasional-spike model used in the standalone HTML terminal so the feel
# is identical whether you're looking at the Streamlit app or the static page.
# ==============================================================================
def init_sim_state(sym_cfg: dict):
    open_prem = sym_cfg["base_premium"] * random.uniform(0.94, 1.06)
    return {
        "base_spot": sym_cfg["base_spot"],
        "base_premium": sym_cfg["base_premium"],
        "strike_step": sym_cfg["strike_step"],
        "open_premium": open_prem,
        "last_spot": None,
        "last_premium": None,
    }


def step_sim(state: dict):
    base_spot = state["base_spot"]
    base_prem = state["base_premium"]
    step = state["strike_step"]

    if state["last_spot"] is None:
        spot = base_spot + random.gauss(0, 1) * base_spot * 0.0018
        prem = state["open_premium"]
    else:
        spot = state["last_spot"] + random.gauss(0, 1) * base_spot * 0.00075
        d_prem = (state["open_premium"] - state["last_premium"]) * 0.018 + random.gauss(0, 1) * (base_prem * 0.011)
        if random.random() < 0.048:  # occasional spike event
            d_prem += (1 if random.random() > 0.42 else -1) * base_prem * (0.045 + random.random() * 0.09)
        d_prem -= base_prem * 0.0007  # mild theta decay
        prem = max(base_prem * 0.38, state["last_premium"] + d_prem)

    split = 0.43 + random.random() * 0.14
    ce = prem * split
    pe = prem * (1 - split)
    atm_strike = round(spot / step) * step
    ce_oi = int(random.uniform(0.4, 1.6) * 1_000_000)
    pe_oi = int(random.uniform(0.4, 1.6) * 1_000_000)
    ce_volume = int(random.uniform(0.2, 1.2) * 1_500_000)
    pe_volume = int(random.uniform(0.2, 1.2) * 1_500_000)

    state["last_spot"] = spot
    state["last_premium"] = prem

    return {
        "spot": spot,
        "atm_strike": atm_strike,
        "ce_ltp": ce,
        "pe_ltp": pe,
        "combined_premium": prem,
        "atm_iv": round(12 + random.random() * 8, 2),
        "ce_oi": ce_oi,
        "pe_oi": pe_oi,
        "ce_volume": ce_volume,
        "pe_volume": pe_volume,
        "combined_volume": ce_volume + pe_volume,
        "pcr": round(pe_oi / ce_oi, 2) if ce_oi else None,
        "ce_delta": round(0.5 + random.uniform(-0.08, 0.08), 3),
        "pe_delta": round(-0.5 + random.uniform(-0.08, 0.08), 3),
        "expiry": "SIMULATED",
    }


# ==============================================================================
# MARKET HOURS (IST, NSE/MCX cash-equity/index session — commodity night
# session runs later, but this keeps the header badge simple & honest)
# ==============================================================================
def is_market_hours() -> bool:
    try:
        from zoneinfo import ZoneInfo
        now = __import__("datetime").datetime.now(ZoneInfo("Asia/Kolkata"))
    except Exception:
        now = __import__("datetime").datetime.utcnow()  # best-effort fallback
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    return 9 * 60 + 15 <= mins <= 15 * 60 + 30


# ==============================================================================
# BASELINE PERSISTENCE (previous-day spot -> strike -> CE/PE snapshot)
# ------------------------------------------------------------------------------
# §3.3 of the spec: persist { date, spot, expiry, strike, ce_ltp, pe_ltp,
# combined_premium } per symbol, on each live tick (last write for a given
# date simply wins — good enough for a single-process terminal). Next day,
# the caller loads the latest snapshot with date < today as that symbol's
# fixed baseline. If none exists yet, baseline is "pending" and the symbol
# is excluded from spurt calculation until a first snapshot is captured.
#
# NOTE: on ephemeral hosting (e.g. Streamlit Community Cloud), this file
# only survives for the lifetime of the running container — a redeploy
# clears it, same as restarting a local process from a fresh checkout.
# ==============================================================================
import json

from config import BASELINE_STORE_PATH

_store_lock_cache = {"data": None, "mtime": None}


def _load_store() -> dict:
    try:
        mtime = os.path.getmtime(BASELINE_STORE_PATH)
    except OSError:
        mtime = None
    if _store_lock_cache["data"] is not None and _store_lock_cache["mtime"] == mtime:
        return _store_lock_cache["data"]
    if not os.path.exists(BASELINE_STORE_PATH):
        data = {}
    else:
        try:
            with open(BASELINE_STORE_PATH, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    _store_lock_cache["data"] = data
    _store_lock_cache["mtime"] = mtime
    return data


def _save_store(data: dict):
    try:
        with open(BASELINE_STORE_PATH, "w") as f:
            json.dump(data, f, indent=2, default=str)
        _store_lock_cache["data"] = data
        _store_lock_cache["mtime"] = os.path.getmtime(BASELINE_STORE_PATH)
    except Exception:
        pass  # persistence is best-effort; never crash the dashboard over it


def save_snapshot(symbol: str, date_str: str, snapshot: dict):
    """Overwrites (or creates) today's snapshot for `symbol`. Safe to call
    on every tick — the latest call for a given date is what's kept."""
    data = _load_store()
    data.setdefault(symbol, {})[date_str] = snapshot
    _save_store(data)


def get_previous_baseline(symbol: str, today_str: str):
    """Returns the most recent persisted snapshot for `symbol` strictly
    before `today_str`, or None if no prior snapshot exists yet."""
    data = _load_store()
    dates = data.get(symbol, {})
    prior_dates = sorted(d for d in dates if d < today_str)
    if not prior_dates:
        return None
    return dates[prior_dates[-1]]
