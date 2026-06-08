"""
stress_testing.py
=================
Scenario-based stress testing. Each named scenario applies a set of
historically calibrated shocks to ALL risk factors simultaneously (equity,
rate, FX, volatility, credit) and revalues the book to produce a P&L impact,
broken down by asset class, plus an impact on VaR.

Stress testing complements VaR: VaR answers "how bad on a normal bad day?",
while stress tests answer "how bad in a specific historical/ hypothetical
crisis?" Basel and the RBI both require a stress-testing programme alongside
VaR because tail events are not well captured by a 1-year lookback.

Scenarios (calibrated to actual episodes):
  1. COVID Crash (Mar-2020)      4. Global Rate Shock +300bps
  2. Taper Tantrum (2013)        5. INR Depreciation -15%
  3. IL&FS Crisis (2018)

FRM references:
  * Stress testing & scenario analysis — FRM Part II, Market Risk Measurement
    & Management; "Stress Testing" (Basel principles for sound stress testing).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

import config as C
from utils import get_logger
import greeks_engine as ge
import fixed_income_risk as fi

LOG = get_logger("stress_testing")


def _equity_pnl(last_px: pd.Series, shock: float) -> float:
    """Linear revaluation of the equity sleeve under an equity index shock."""
    pnl = 0.0
    for e in C.EQUITIES:
        mv = float(last_px[e["instrument_id"]]) * e["quantity"]
        pnl += mv * shock
    return pnl


def _fixed_income_pnl(rate_shock: float) -> float:
    """Full-revaluation P&L of bonds + swap under a parallel rate shock."""
    pnl = 0.0
    for b in C.FIXED_INCOME:
        base = fi.price_bond(b["coupon"], b["ytm"], b["maturity_years"],
                             b["face_value"], b["freq"])
        shocked = fi.price_bond(b["coupon"], b["ytm"] + rate_shock,
                                b["maturity_years"], b["face_value"], b["freq"])
        pnl += (shocked - base) * b["quantity"]
    # swap: pay-fixed gains as rates rise; DV01 is per 1bp
    pnl += fi.swap_dv01() * (rate_shock * 10000.0)
    return pnl


def _option_pnl(last_px: pd.Series, equity_shock: float,
                vol_shock: float) -> float:
    """Option sleeve P&L: delta-gamma from spot move + vega from vol move.

    dV ~= delta*dS + 0.5*gamma*dS^2  +  vega_per_1% * (vol move in % points)
    """
    pnl = 0.0
    for o in C.OPTIONS:
        S = float(last_px[o["underlying"]])
        g = ge.bsm_price_greeks(S, o["strike"], C.RISK_FREE_RATE,
                                o["implied_vol"], o["maturity_years"],
                                o["option_type"], o["quantity"])
        dS = S * equity_shock
        spot_pnl = ge.delta_gamma_pnl(g, dS)
        vol_points = o["implied_vol"] * vol_shock * 100.0  # % points
        vega_pnl = g.vega * vol_points
        pnl += spot_pnl + vega_pnl
    return pnl


def _fx_pnl(fx_shock: float) -> float:
    """FX-forward P&L. Long USD gains when INR depreciates (fx_shock > 0)."""
    f = C.FX_FORWARD
    return f["usd_notional"] * f["spot"] * fx_shock


def run_stress_tests(last_px: pd.Series, base_var: float) -> pd.DataFrame:
    """Run all scenarios; return a tidy DataFrame with per-asset-class and
    total P&L plus the VaR impact.

    VaR impact is modelled as the change in VaR when volatilities jump in the
    stress (stressed VaR = base VaR * (1 + vol_shock)); a transparent proxy for
    the de-correlation / vol-spike that accompanies crises.
    """
    rows = []
    for sc in C.STRESS_SCENARIOS:
        eq = _equity_pnl(last_px, sc["equity_shock"])
        figi = _fixed_income_pnl(sc["rate_shock"])
        opt = _option_pnl(last_px, sc["equity_shock"], sc["vol_shock"])
        fx = _fx_pnl(sc["fx_shock"])
        total = eq + figi + opt + fx

        stressed_var = base_var * (1.0 + sc["vol_shock"])
        var_impact = stressed_var - base_var

        for ac, val in (("Equity", eq), ("FixedIncome", figi),
                        ("Option", opt), ("FX", fx), ("Total", total)):
            rows.append({
                "scenario_name": sc["scenario_name"],
                "asset_class": ac,
                "pnl_impact": val,
                "var_impact": var_impact if ac == "Total" else None,
                "description": sc["description"] if ac == "Total" else None,
            })
        LOG.info("Scenario '%s': total P&L = %.0f INR, VaR impact = %.0f",
                 sc["scenario_name"], total, var_impact)
    return pd.DataFrame(rows)
