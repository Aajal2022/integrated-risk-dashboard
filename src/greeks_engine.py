"""
greeks_engine.py
================
Option risk via the Black-Scholes-Merton (BSM) closed-form model:
Delta, Gamma, Vega, Theta, Rho — per option and aggregated to the book —
plus a delta-gamma P&L approximation.

BSM price (no dividend):
    d1 = [ ln(S/K) + (r + 0.5*sigma^2) T ] / (sigma sqrt(T))
    d2 = d1 - sigma sqrt(T)
    Call = S N(d1) - K e^{-rT} N(d2)
    Put  = K e^{-rT} N(-d2) - S N(-d1)

The Greeks are the partial derivatives of that price. They tell a desk how its
option P&L responds to moves in spot (delta/gamma), volatility (vega), time
(theta) and rates (rho) — the inputs a market-maker hedges every day.

FRM references:
  * Black-Scholes-Merton & the Greeks — FRM Part I, Valuation & Risk Models
    (Hull, "Options, Futures, and Other Derivatives", Greek-letter chapter).
  * Delta-gamma approximation — FRM Part II, Market Risk (Taylor expansion of
    option value).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm

import config as C
from utils import get_connection, get_logger

LOG = get_logger("greeks_engine")


@dataclass
class Greeks:
    instrument_id: str
    delta: float
    gamma: float
    vega: float       # per 1 vol point (1% = 0.01) -> reported per 1% move
    theta: float      # per calendar day
    rho: float        # per 1% rate move
    price: float
    delta_equiv: float  # delta * underlying spot * contracts (INR exposure)


def _d1_d2(S, K, r, sigma, T):
    """Return (d1, d2) for BSM. Guards against T<=0 or sigma<=0."""
    if T <= 0 or sigma <= 0:
        # at/after expiry: treat as deterministic
        d1 = np.inf if S > K else -np.inf
        return d1, d1
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return d1, d2


def bsm_price_greeks(S: float, K: float, r: float, sigma: float, T: float,
                     option_type: str, contracts: float,
                     instrument_id: str = "") -> Greeks:
    """Compute BSM price and all five Greeks for one option line.

    Sign / scaling conventions (market standard):
      * Delta   : per 1.0 move in spot.  Position delta = delta * contracts.
      * Gamma   : d(delta)/dS.
      * Vega    : reported per +1% (0.01) change in vol  (raw vega * 0.01).
      * Theta   : reported per 1 calendar day            (annual theta / 365).
      * Rho     : reported per +1% (0.01) change in rate (raw rho * 0.01).
    `contracts` scales each Greek to the actual position size.
    """
    d1, d2 = _d1_d2(S, K, r, sigma, T)
    pdf = norm.pdf(d1)
    disc = np.exp(-r * T)

    if option_type.lower() == "call":
        price = S * norm.cdf(d1) - K * disc * norm.cdf(d2)
        delta = norm.cdf(d1)
        theta_ann = (-(S * pdf * sigma) / (2 * np.sqrt(T))
                     - r * K * disc * norm.cdf(d2))
        rho_raw = K * T * disc * norm.cdf(d2)
    else:  # put
        price = K * disc * norm.cdf(-d2) - S * norm.cdf(-d1)
        delta = norm.cdf(d1) - 1.0
        theta_ann = (-(S * pdf * sigma) / (2 * np.sqrt(T))
                     + r * K * disc * norm.cdf(-d2))
        rho_raw = -K * T * disc * norm.cdf(-d2)

    gamma = pdf / (S * sigma * np.sqrt(T))
    vega_raw = S * pdf * np.sqrt(T)

    return Greeks(
        instrument_id=instrument_id,
        delta=delta * contracts,
        gamma=gamma * contracts,
        vega=vega_raw * 0.01 * contracts,     # per 1% vol
        theta=theta_ann / 365.0 * contracts,  # per day
        rho=rho_raw * 0.01 * contracts,       # per 1% rate
        price=price * contracts,
        delta_equiv=delta * contracts * S,    # INR equivalent spot exposure
    )


def compute_book_greeks(conn) -> tuple[list[Greeks], dict]:
    """Compute Greeks for every option in the book and aggregate them.

    Spot, strike, vol and maturity come from config / latest risk-factor data.
    Returns (per_option list, portfolio_aggregate dict).
    """
    # latest underlying spot from market_data
    last_px = pd.read_sql_query(
        "SELECT instrument_id, price FROM market_data WHERE date = "
        "(SELECT MAX(date) FROM market_data)", conn
    ).set_index("instrument_id")["price"]

    per_option = []
    for o in C.OPTIONS:
        S = float(last_px[o["underlying"]])
        g = bsm_price_greeks(
            S=S, K=o["strike"], r=C.RISK_FREE_RATE,
            sigma=o["implied_vol"], T=o["maturity_years"],
            option_type=o["option_type"], contracts=o["quantity"],
            instrument_id=o["instrument_id"])
        per_option.append(g)
        LOG.info("Greeks %s: delta=%.0f gamma=%.2f vega=%.0f theta=%.0f rho=%.0f",
                 g.instrument_id, g.delta, g.gamma, g.vega, g.theta, g.rho)

    agg = {
        "total_delta": sum(g.delta for g in per_option),
        "total_delta_equiv": sum(g.delta_equiv for g in per_option),
        "total_gamma": sum(g.gamma for g in per_option),
        "total_vega": sum(g.vega for g in per_option),
        "total_theta": sum(g.theta for g in per_option),
        "total_rho": sum(g.rho for g in per_option),
    }
    LOG.info("Portfolio Greeks: delta-equiv=%.0f INR, gamma=%.2f, vega=%.0f",
             agg["total_delta_equiv"], agg["total_gamma"], agg["total_vega"])
    return per_option, agg


def delta_gamma_pnl(greeks: Greeks, dS: float) -> float:
    """Delta-gamma approximation of option P&L for a spot move dS.

        dV ~= delta * dS + 0.5 * gamma * dS^2

    The second-order (gamma) term captures the convexity that a pure delta
    (linear) approximation misses — important for large moves and for VaR on
    option books.
    """
    return greeks.delta * dS + 0.5 * greeks.gamma * dS ** 2
