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
def _nearest_expiry(dhan_client, security_id: int, segment: str):
    resp = dhan_client.expiry_list(security_id, segment)
    data = resp.get("data") if isinstance(resp, dict) else resp
    if not data:
        return None
    return data[0]


def fetch_atm_combined_premium(dhan_client, security_id: int, segment: str, strike_step: int):
    """Pulls the live option chain and returns the ATM CE+PE combined
    premium, or None on any failure (caller falls back to simulation)."""
    try:
        expiry = _nearest_expiry(dhan_client, security_id, segment)
        if not expiry:
            return None
        resp = dhan_client.option_chain(security_id, segment, expiry)
        data = resp.get("data") if isinstance(resp, dict) else resp
        if not data:
            return None
        spot = float(data["last_price"])
        oc = data.get("oc", {})
        if not oc:
            return None
        atm_strike = round(spot / strike_step) * strike_step
        # strike keys come back as "24850.000000"-style strings — match the closest
        best_key, best_diff = None, math.inf
        for k in oc.keys():
            try:
                diff = abs(float(k) - atm_strike)
            except ValueError:
                continue
            if diff < best_diff:
                best_diff, best_key = diff, k
        if best_key is None:
            return None
        leg = oc[best_key]
        ce_ltp = float(leg.get("ce", {}).get("last_price", 0) or 0)
        pe_ltp = float(leg.get("pe", {}).get("last_price", 0) or 0)
        ce_iv = leg.get("ce", {}).get("implied_volatility") or 0
        pe_iv = leg.get("pe", {}).get("implied_volatility") or 0
        return {
            "spot": spot,
            "atm_strike": float(best_key),
            "ce_ltp": ce_ltp,
            "pe_ltp": pe_ltp,
            "combined_premium": ce_ltp + pe_ltp,
            "atm_iv": round((ce_iv + pe_iv) / 2, 2) if (ce_iv or pe_iv) else None,
            "expiry": expiry,
        }
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

    state["last_spot"] = spot
    state["last_premium"] = prem

    return {
        "spot": spot,
        "atm_strike": atm_strike,
        "ce_ltp": ce,
        "pe_ltp": pe,
        "combined_premium": prem,
        "atm_iv": round(12 + random.random() * 8, 2),
        "expiry": "SIMULATED",
    }
