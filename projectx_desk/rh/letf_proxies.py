"""Hand-curated map for the leveraged ETF desk (display only; no I/O, no imports from the desk).

The vendored fund list (`letf_data.json`, built by `scripts/letf_sync.py`) names each fund's underlying with a LABEL, not always a
ticker: "Gold", "Semiconductors", "EUR/USD". The underlying chart needs a real equity ticker, so each label that is not one gets an
entry here. This file is reviewed by a human and committed; `scripts/letf_sync.py` merges it and `tests/test_letf_sync.py` fails
when the committed JSON and this file disagree.

Quality is a promise about how well the proxy tracks the label:
  exact        the proxy IS (or holds) the thing the label names (GLD for Gold).
  approximate  same exposure, different construction (USO holds futures, SOXX is not the index every semiconductor fund tracks).
The desk says so on screen and every analytics figure that compares the fund with its underlying is marked approximate.

A label in NO_PROXY is a deliberate "no underlying chart": the ETF chart is shown alone with a sentence saying why. A crypto,
commodity, currency or volatility label that is not listed in PROXIES never charts itself even when it looks like a ticker
(BTC, ETH and HYPE are unrelated equities), so a new label there needs a decision here before it gets a chart.
"""
from __future__ import annotations

PROXIES = {
    # Broad index
    "Magnificent 7": ("MAGS", "approximate"),
    "Mid cap 400": ("MDY", "exact"),
    "Small cap 600": ("IJR", "exact"),
    "S&P 500 High Beta": ("SPHB", "exact"),
    "Total US market": ("VTI", "exact"),
    "World": ("ACWI", "approximate"),
    "Micro cap": ("IWC", "approximate"),
    "US growth": ("IWF", "approximate"),
    "Ex-US": ("VEU", "approximate"),
    # Sector
    "Semiconductors": ("SOXX", "approximate"),
    "Energy": ("XLE", "approximate"),
    "Technology": ("XLK", "approximate"),
    "Financials": ("XLF", "approximate"),
    "Health care": ("XLV", "approximate"),
    "Utilities": ("XLU", "approximate"),
    "Real estate": ("XLRE", "approximate"),
    "Industrials": ("XLI", "approximate"),
    "Materials": ("XLB", "approximate"),
    "Consumer staples": ("XLP", "approximate"),
    "Consumer discretionary": ("XLY", "approximate"),
    "Communication services": ("XLC", "approximate"),
    "Gold miners": ("GDX", "exact"),
    "Gold miners (junior)": ("GDXJ", "exact"),
    "Oil & gas E&P": ("XOP", "approximate"),
    "Homebuilders": ("XHB", "approximate"),
    "Retail": ("XRT", "approximate"),
    "Pharma": ("XPH", "approximate"),
    "Transportation": ("IYT", "approximate"),
    "Uranium": ("URA", "approximate"),
    "ARKK / Innovation": ("ARKK", "exact"),
    "Airlines (JETS)": ("JETS", "exact"),
    "Cannabis (MSOS)": ("MSOS", "exact"),
    "Internet (FDN)": ("FDN", "exact"),
    "FANG+": ("FNGS", "approximate"),
    "Copper miners": ("COPX", "approximate"),
    "Cloud computing": ("SKYY", "approximate"),
    "Infrastructure": ("PAVE", "approximate"),
    "Defense": ("ITA", "approximate"),
    "Space": ("UFO", "approximate"),
    "Quantum computing": ("QTUM", "approximate"),
    "AI & Big Data index": ("AIQ", "approximate"),
    "Big US Banks": ("KBWB", "approximate"),
    "Auto industry": ("CARZ", "approximate"),
    # Commodity (futures-based funds: the proxy holds metal or futures, the leveraged fund tracks an index)
    "Gold": ("GLD", "exact"),
    "Silver": ("SLV", "exact"),
    "Crude oil": ("USO", "approximate"),
    "Natural gas": ("UNG", "approximate"),
    "Copper": ("CPER", "approximate"),
    "Wheat": ("WEAT", "approximate"),
    "Corn": ("CORN", "approximate"),
    "Palladium": ("PALL", "exact"),
    "Platinum": ("PPLT", "exact"),
    "Broad commodities": ("DBC", "approximate"),
    # Volatility, currency, bonds
    "VIX futures": ("VIXY", "approximate"),
    "EUR/USD": ("FXE", "approximate"),
    "JPY/USD": ("FXY", "approximate"),
    "20+yr Treasury": ("TLT", "exact"),
    "7-10yr Treasury": ("IEF", "exact"),
    "High yield": ("HYG", "approximate"),
    # Country / region
    "South Korea": ("EWY", "approximate"),
    "China": ("FXI", "approximate"),
    "Brazil": ("EWZ", "approximate"),
    "China internet": ("KWEB", "approximate"),
    "India": ("INDA", "approximate"),
    "China A-shares": ("ASHR", "approximate"),
    "Europe (VGK)": ("VGK", "exact"),
    "Japan": ("EWJ", "approximate"),
    "Mexico": ("EWW", "approximate"),
    "Taiwan": ("EWT", "approximate"),
    # Crypto: spot ETFs only where the underlying is the coin itself and RH lists the fund.
    # The others need a check against the live tradable list before they are added.
    "BTC": ("IBIT", "approximate"),
    "ETH": ("ETHA", "approximate"),
}

# Labels with no clean proxy. Listed so that "no chart" is a decision someone made, not a gap.
NO_PROXY = frozenset({
    "Innovation 100",
    "Tech Top 5",
    "AI index",
    "Travel",
    "SK Hynix",
    "SPCX (SpaceX)",
})

# The inverse CSV's `bucket` is sometimes spelt differently from the underlying it belongs to.
BUCKET_ALIASES = {
    "ARKK": "ARKK / Innovation",
    "Health care (IYH)": "Health care",
}
