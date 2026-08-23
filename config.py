"""
Instrument universe & tunables for the NSE/MCX Combined-Premium Terminal.

Each instrument entry:
    label        : display name
    asset_class  : "INDEX" | "COMMODITY" | "EQUITY"
    segment      : Dhan exchange segment for the OPTION CHAIN call
                   ("IDX_I" for index underlyings, "MCX_COMM" for MCX
                   commodity underlyings, "NSE_EQ" for stock underlyings)
    security_id  : Dhan numeric security id of the underlying. Hardcoded
                   for indices (these are stable & well documented by
                   Dhan itself). Left as None for instruments that must
                   be resolved dynamically from the scrip master
                   (MCX commodities roll monthly, so there is no fixed id).
    lookup_symbol: trading-symbol fragment used to resolve `security_id`
                   dynamically via the Dhan scrip master when it is None.
    strike_step  : strike interval used to round spot -> ATM strike.
    base_spot / base_premium : only used to seed the simulated fallback
                   feed with realistic numbers; ignored once live data
                   is flowing.
"""

INDEX_INSTRUMENTS = {
    "NIFTY": {
        "label": "NIFTY 50", "asset_class": "INDEX", "segment": "IDX_I",
        "security_id": 13, "lookup_symbol": None,
        "strike_step": 50, "base_spot": 24850, "base_premium": 185,
    },
    "BANKNIFTY": {
        "label": "BANK NIFTY", "asset_class": "INDEX", "segment": "IDX_I",
        "security_id": 25, "lookup_symbol": None,
        "strike_step": 100, "base_spot": 51200, "base_premium": 420,
    },
    "FINNIFTY": {
        "label": "FIN NIFTY", "asset_class": "INDEX", "segment": "IDX_I",
        "security_id": 27, "lookup_symbol": None,
        "strike_step": 50, "base_spot": 23800, "base_premium": 160,
    },
    "SENSEX": {
        "label": "SENSEX", "asset_class": "INDEX", "segment": "IDX_I",
        "security_id": 51, "lookup_symbol": None,
        "strike_step": 100, "base_spot": 81200, "base_premium": 580,
    },
}

EQUITY_INSTRUMENTS = {
    "RELIANCE": {
        "label": "RELIANCE", "asset_class": "EQUITY", "segment": "NSE_EQ",
        "security_id": None, "lookup_symbol": "RELIANCE",
        "strike_step": 20, "base_spot": 2980, "base_premium": 48,
    },
    "HDFCBANK": {
        "label": "HDFC BANK", "asset_class": "EQUITY", "segment": "NSE_EQ",
        "security_id": None, "lookup_symbol": "HDFCBANK",
        "strike_step": 10, "base_spot": 1685, "base_premium": 28,
    },
    "ICICIBANK": {
        "label": "ICICI BANK", "asset_class": "EQUITY", "segment": "NSE_EQ",
        "security_id": None, "lookup_symbol": "ICICIBANK",
        "strike_step": 10, "base_spot": 1220, "base_premium": 22,
    },
    "INFY": {
        "label": "INFY", "asset_class": "EQUITY", "segment": "NSE_EQ",
        "security_id": None, "lookup_symbol": "INFY",
        "strike_step": 10, "base_spot": 1890, "base_premium": 32,
    },
    "TCS": {
        "label": "TCS", "asset_class": "EQUITY", "segment": "NSE_EQ",
        "security_id": None, "lookup_symbol": "TCS",
        "strike_step": 20, "base_spot": 4150, "base_premium": 55,
    },
}

# MCX commodity underlyings roll to a new futures contract every month, so
# there is no fixed security_id — dhan_service.resolve_mcx_underlying()
# looks these up live from Dhan's scrip master each session.
COMMODITY_INSTRUMENTS = {
    "CRUDEOIL": {
        "label": "CRUDE OIL", "asset_class": "COMMODITY", "segment": "MCX_COMM",
        "security_id": None, "lookup_symbol": "CRUDEOIL",
        "strike_step": 50, "base_spot": 6200, "base_premium": 95,
    },
    "GOLD": {
        "label": "GOLD", "asset_class": "COMMODITY", "segment": "MCX_COMM",
        "security_id": None, "lookup_symbol": "GOLD",
        "strike_step": 100, "base_spot": 72100, "base_premium": 420,
    },
    "SILVER": {
        "label": "SILVER", "asset_class": "COMMODITY", "segment": "MCX_COMM",
        "security_id": None, "lookup_symbol": "SILVER",
        "strike_step": 250, "base_spot": 84500, "base_premium": 680,
    },
    "NATURALGAS": {
        "label": "NATURAL GAS", "asset_class": "COMMODITY", "segment": "MCX_COMM",
        "security_id": None, "lookup_symbol": "NATURALGAS",
        "strike_step": 5, "base_spot": 210, "base_premium": 12,
    },
    "COPPER": {
        "label": "COPPER", "asset_class": "COMMODITY", "segment": "MCX_COMM",
        "security_id": None, "lookup_symbol": "COPPER",
        "strike_step": 5, "base_spot": 780, "base_premium": 22,
    },
}

ALL_INSTRUMENTS = {**INDEX_INSTRUMENTS, **EQUITY_INSTRUMENTS, **COMMODITY_INSTRUMENTS}

DEFAULT_THRESHOLD_PCT = 5.0
DEFAULT_REFRESH_SECONDS = 10
MAX_HISTORY_POINTS = 300
SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
SCRIP_MASTER_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6 hours
