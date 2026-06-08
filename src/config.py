"""
config.py
=========
Central configuration for the Integrated Risk Dashboard.

Holds:
  * File paths (database location, log location)
  * The full hypothetical multi-asset trading book (the "portfolio")
  * Synthetic-data generation parameters (GBM drift / vol per instrument)
  * Basel III / FRTB convention constants used across the risk engine

Every downstream module imports from here so that there is a single source
of truth for the book and for regulatory conventions. Centralising the
conventions (confidence levels, lookback window, holding periods) is itself
an audit/governance best practice: a regulator can read one file to see how
the desk has parameterised its risk system.
"""

from __future__ import annotations

import os
from datetime import date

# --------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
DB_PATH = os.path.join(OUTPUT_DIR, "risk_dashboard.db")
LOG_PATH = os.path.join(OUTPUT_DIR, "risk_engine.log")

os.makedirs(OUTPUT_DIR, exist_ok=True)

# --------------------------------------------------------------------------
# Synthetic history window
# --------------------------------------------------------------------------
# Two years of *daily* business-day history. ~252 trading days/year.
HISTORY_START = date(2024, 1, 1)
TRADING_DAYS_PER_YEAR = 252
HISTORY_YEARS = 2
N_TRADING_DAYS = TRADING_DAYS_PER_YEAR * HISTORY_YEARS  # 504

RANDOM_SEED = 20240101  # reproducible synthetic data

# --------------------------------------------------------------------------
# Basel III / FRTB convention constants
# --------------------------------------------------------------------------
# Basel mandates a 1-year (250 business-day) lookback for historical VaR.
VAR_LOOKBACK_DAYS = 250

# Confidence levels. Basel III/FRTB moved the primary metric to 97.5% ES,
# but 95% and 99% VaR remain standard internal/Basel 2.5 reference points.
CONFIDENCE_LEVELS = [0.95, 0.975, 0.99]
ES_BASEL_CONFIDENCE = 0.975  # FRTB primary metric

# Holding-period horizons (square-root-of-time scaling applies).
HORIZONS_DAYS = [1, 5, 10]

# Monte Carlo
MC_SIMULATIONS = 10_000

# VaR limit for utilisation gauge: Basel-style desk limit = 2% of NAV.
VAR_LIMIT_PCT_OF_NAV = 0.02

# Vasicek single-factor asset correlations (Basel IRB prescribed bands).
# Basel uses ~0.12 for general corporates and a higher figure (0.12-0.24)
# for large regulated financials; we use 0.15 for bank counterparties.
VASICEK_RHO_CORPORATE = 0.12
VASICEK_RHO_BANK = 0.15

# Risk-free / discount rate used for option pricing and PV of swaps/forwards.
RISK_FREE_RATE = 0.0675  # ~6.75% INR risk-free (RBI repo-anchored)

# --------------------------------------------------------------------------
# THE TRADING BOOK
# --------------------------------------------------------------------------
# Notionals are sized for a mid-size bank trading desk (tens of crores INR).
# entry_price is in INR. Equity quantities chosen so notional ~ qty*price.
#
# Each dict row maps onto the `positions` table.
# --------------------------------------------------------------------------

EQUITIES = [
    # instrument_id, description, sector, entry_price, quantity
    {"instrument_id": "EQ_HDFCBANK", "description": "HDFC Bank Ltd",
     "sector": "Banking", "entry_price": 1650.0, "quantity": 60_000},
    {"instrument_id": "EQ_SUNPHARMA", "description": "Sun Pharmaceutical Inds",
     "sector": "Pharma", "entry_price": 1480.0, "quantity": 40_000},
    {"instrument_id": "EQ_INFY", "description": "Infosys Ltd",
     "sector": "IT", "entry_price": 1550.0, "quantity": 45_000},
    {"instrument_id": "EQ_MARUTI", "description": "Maruti Suzuki India",
     "sector": "Auto", "entry_price": 11200.0, "quantity": 6_000},
    {"instrument_id": "EQ_HINDUNILVR", "description": "Hindustan Unilever",
     "sector": "FMCG", "entry_price": 2450.0, "quantity": 25_000},
]

# Annualised GBM parameters per equity (sector-appropriate volatilities).
# mu = expected annual drift, sigma = annual volatility.
EQUITY_GBM = {
    "EQ_HDFCBANK":  {"mu": 0.11, "sigma": 0.24},  # banking
    "EQ_SUNPHARMA": {"mu": 0.12, "sigma": 0.26},  # pharma
    "EQ_INFY":      {"mu": 0.10, "sigma": 0.28},  # IT (USD-sensitive)
    "EQ_MARUTI":    {"mu": 0.13, "sigma": 0.30},  # auto (cyclical)
    "EQ_HINDUNILVR":{"mu": 0.08, "sigma": 0.18},  # FMCG (defensive)
}

# Fixed income instruments. coupon annual; ytm = entry yield; face per unit.
FIXED_INCOME = [
    {"instrument_id": "FI_GSEC_5Y", "description": "GOI G-Sec 5Y",
     "coupon": 0.0710, "ytm": 0.0715, "maturity_years": 5.0,
     "face_value": 100.0, "quantity": 500_000, "freq": 2,
     "fi_type": "govt"},
    {"instrument_id": "FI_CORP_10Y", "description": "AAA Corporate Bond 10Y",
     "coupon": 0.0790, "ytm": 0.0815, "maturity_years": 10.0,
     "face_value": 100.0, "quantity": 300_000, "freq": 2,
     "fi_type": "corp"},
    {"instrument_id": "FI_TBILL_91D", "description": "T-Bill 91-day",
     "coupon": 0.0000, "ytm": 0.0660, "maturity_years": 0.2493,
     "face_value": 100.0, "quantity": 1_000_000, "freq": 0,
     "fi_type": "govt"},
]

# Annual vol of yield *level* moves used to generate rate history (in abs yield).
RATE_GBM_VOL = 0.0090  # ~90bps annual sigma on the yield level

# Equity options (Black-Scholes-Merton). Written on underlying equities.
OPTIONS = [
    {"instrument_id": "OPT_INFY_CALL", "description": "INFY Call Option",
     "underlying": "EQ_INFY", "option_type": "call",
     "strike": 1650.0, "maturity_years": 0.50, "quantity": 200_000,
     "implied_vol": 0.27},
    {"instrument_id": "OPT_HDFCBANK_PUT", "description": "HDFCBANK Put Option",
     "underlying": "EQ_HDFCBANK", "option_type": "put",
     "strike": 1550.0, "maturity_years": 0.25, "quantity": 250_000,
     "implied_vol": 0.25},
]

# Interest rate swap (pay fixed, receive floating). 5Y tenor.
SWAP = {
    "instrument_id": "IRS_5Y", "description": "INR IRS 5Y Pay-Fixed",
    "notional": 250_000_000.0, "fixed_rate": 0.0705, "tenor_years": 5.0,
    "freq": 2, "float_index_rate": 0.0675, "pay_fixed": True,
}

# FX forward USD/INR 3-month. Long USD notional.
FX_FORWARD = {
    "instrument_id": "FXF_USDINR_3M", "description": "USD/INR 3M Forward",
    "usd_notional": 5_000_000.0, "contract_rate": 83.50,
    "spot": 83.20, "maturity_years": 0.25,
    "usd_rate": 0.0525, "inr_rate": 0.0675,
}

# Counterparties for credit risk (Basel IRB: PD / LGD / EAD framework).
# EAD in INR. PD = 1-year probability of default. LGD = loss given default.
COUNTERPARTIES = [
    {"counterparty_id": "CP_BANK_A", "name": "Large Bank Counterparty",
     "ctype": "bank", "pd": 0.0080, "lgd": 0.45, "ead": 250_000_000.0},
    {"counterparty_id": "CP_CORP_B", "name": "AAA Corporate Issuer",
     "ctype": "corp", "pd": 0.0150, "lgd": 0.55, "ead": 180_000_000.0},
    {"counterparty_id": "CP_CORP_C", "name": "AA NBFC Counterparty",
     "ctype": "corp", "pd": 0.0350, "lgd": 0.60, "ead": 120_000_000.0},
    {"counterparty_id": "CP_CORP_D", "name": "A-rated Mid Corporate",
     "ctype": "corp", "pd": 0.0600, "lgd": 0.65, "ead": 90_000_000.0},
]

# --------------------------------------------------------------------------
# Stress scenarios — historically calibrated simultaneous shocks.
# Shocks are expressed as relative moves (equity %), absolute yield moves
# (rate, in decimal), fx move (% INR depreciation = USD up), and vol move.
# --------------------------------------------------------------------------
STRESS_SCENARIOS = [
    {
        "scenario_name": "COVID Crash Mar-2020",
        "description": "Pandemic risk-off: equities -35%, rates -80bps "
                       "(flight to quality), INR -7%, vol +120%.",
        "equity_shock": -0.35, "rate_shock": -0.0080,
        "fx_shock": 0.07, "vol_shock": 1.20, "credit_pd_mult": 2.5,
    },
    {
        "scenario_name": "Taper Tantrum 2013",
        "description": "EM outflows on Fed taper signal: equities -12%, "
                       "rates +250bps, INR -15%, vol +60%.",
        "equity_shock": -0.12, "rate_shock": 0.0250,
        "fx_shock": 0.15, "vol_shock": 0.60, "credit_pd_mult": 1.6,
    },
    {
        "scenario_name": "IL&FS Crisis 2018",
        "description": "NBFC/credit crunch: equities -10%, rates +90bps, "
                       "INR -8%, vol +50%, credit PDs blow out.",
        "equity_shock": -0.10, "rate_shock": 0.0090,
        "fx_shock": 0.08, "vol_shock": 0.50, "credit_pd_mult": 3.0,
    },
    {
        "scenario_name": "Global Rate Shock +300bps",
        "description": "Parallel upward shift of the curve by 300bps, "
                       "equities -8%, INR -5%, vol +40%.",
        "equity_shock": -0.08, "rate_shock": 0.0300,
        "fx_shock": 0.05, "vol_shock": 0.40, "credit_pd_mult": 1.4,
    },
    {
        "scenario_name": "INR Depreciation -15%",
        "description": "Sharp INR sell-off: USD/INR +15%, equities -6%, "
                       "rates +120bps, vol +45%.",
        "equity_shock": -0.06, "rate_shock": 0.0120,
        "fx_shock": 0.15, "vol_shock": 0.45, "credit_pd_mult": 1.5,
    },
]

# Tenor buckets for bucket-DV01 (years).
TENOR_BUCKETS = [
    ("0-1Y", 0.0, 1.0),
    ("1-3Y", 1.0, 3.0),
    ("3-5Y", 3.0, 5.0),
    ("5-10Y", 5.0, 10.0),
    ("10Y+", 10.0, 100.0),
]
