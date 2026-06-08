"""
fixed_income_risk.py
====================
Interest-rate risk for the bond book and the interest-rate swap:
Macaulay & Modified Duration, DV01, Convexity, Bucket DV01 across tenor
buckets, and parallel yield-curve shift scenarios.

Core valuation — present value of a coupon bond (periodic compounding):

    P = sum_{k=1..N} CF_k / (1 + y_p)^k ,   y_p = ytm / freq ,  N = T * freq

Risk measures (per unit of face, then scaled by holding quantity to INR):

    Macaulay Duration  D_mac = (1/P) * sum_k  t_k * PV(CF_k)          [years]
    Modified Duration  D_mod = D_mac / (1 + y_p)
    DV01               = D_mod * P * 1e-4                              [price/bp]
    Convexity          C = (1/(P (1+y_p)^2)) * sum_k t_k (t_k+1/freq) PV(CF_k)

DV01 ("dollar value of a basis point") is the desk's primary rate-risk number:
the P&L from a 1bp parallel move. Duration linearises rate risk; convexity is
the second-order correction (bonds gain more from a yield fall than they lose
from an equal yield rise).

FRM references:
  * Duration, modified duration, DV01, convexity — FRM Part I, Valuation & Risk
    Models (Tuckman, "Fixed Income Securities"); Part II, Market Risk.
  * Key-rate / bucket DV01 — FRM Part II, Market Risk Measurement & Management.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import config as C
from utils import get_connection, get_logger

LOG = get_logger("fixed_income_risk")


@dataclass
class DurationResult:
    instrument_id: str
    price: float                # clean price per unit face
    macaulay_duration: float    # years
    modified_duration: float    # years
    dv01: float                 # INR per 1bp for the whole position
    convexity: float            # years^2
    maturity_years: float


# ---------------------------------------------------------------------------
# Cash-flow bond pricing
# ---------------------------------------------------------------------------
def _cashflows(coupon, ytm, T, face, freq):
    """Return (times_in_years, cashflows, periodic_yield) for a bond.

    Handles zero-coupon (freq == 0) by a single bullet cash flow at maturity.
    """
    if freq == 0:  # zero-coupon / discount instrument (e.g. T-bill)
        return np.array([T]), np.array([face]), ytm
    n = max(int(round(T * freq)), 1)
    y_p = ytm / freq
    cpn = coupon / freq * face
    times = np.array([(k + 1) / freq for k in range(n)])
    cfs = np.full(n, cpn)
    cfs[-1] += face  # principal at maturity
    return times, cfs, y_p


def price_bond(coupon, ytm, T, face, freq) -> float:
    """Present value of a bond per unit face at the given yield."""
    times, cfs, y_p = _cashflows(coupon, ytm, T, face, freq)
    if freq == 0:
        return float(face / (1 + ytm) ** T)
    k = np.arange(1, len(cfs) + 1)
    return float(np.sum(cfs / (1 + y_p) ** k))


def bond_risk(coupon, ytm, T, face, freq, quantity,
              instrument_id="") -> DurationResult:
    """Compute price, durations, DV01 and convexity for one bond line."""
    if freq == 0:
        # closed forms for a zero-coupon instrument
        price = face / (1 + ytm) ** T
        d_mac = T
        d_mod = d_mac / (1 + ytm)
        convexity = T * (T + 1) / (1 + ytm) ** 2
    else:
        times, cfs, y_p = _cashflows(coupon, ytm, T, face, freq)
        k = np.arange(1, len(cfs) + 1)
        pv = cfs / (1 + y_p) ** k
        price = pv.sum()
        d_mac = float(np.sum(times * pv) / price)
        d_mod = d_mac / (1 + y_p)
        convexity = float(
            np.sum(times * (times + 1.0 / freq) * pv)
            / (price * (1 + y_p) ** 2))

    dv01_per_unit = d_mod * price * 1e-4          # price change per 1bp
    dv01_inr = dv01_per_unit * quantity           # scale to position
    return DurationResult(instrument_id, price, d_mac, d_mod,
                          dv01_inr, convexity, T)


# ---------------------------------------------------------------------------
# Book-level fixed-income risk
# ---------------------------------------------------------------------------
def compute_book_duration(conn=None) -> list[DurationResult]:
    """Duration metrics for every bond in the book (from config)."""
    out = []
    for b in C.FIXED_INCOME:
        r = bond_risk(b["coupon"], b["ytm"], b["maturity_years"],
                      b["face_value"], b["freq"], b["quantity"],
                      b["instrument_id"])
        out.append(r)
        LOG.info("%s: P=%.2f ModDur=%.2f DV01=%.0f INR Convexity=%.1f",
                 r.instrument_id, r.price, r.modified_duration,
                 r.dv01, r.convexity)
    return out


def swap_dv01() -> float:
    """Approximate DV01 of the pay-fixed interest-rate swap.

    A pay-fixed swap ~ short a fixed-rate bond + long floating (~par). Its rate
    sensitivity is dominated by the fixed leg, so DV01 ~ ModDur(fixed leg) *
    notional * 1e-4. Pay-fixed gains when rates rise, hence the sign.
    """
    s = C.SWAP
    fixed_leg = bond_risk(s["fixed_rate"], s["float_index_rate"],
                          s["tenor_years"], 100.0, s["freq"], 1.0)
    dv01_unit = fixed_leg.modified_duration * 100.0 * 1e-4
    # pay-fixed => long rates => positive DV01 sign on rate rise
    sign = 1.0 if s["pay_fixed"] else -1.0
    return sign * dv01_unit * (s["notional"] / 100.0)


def bucket_dv01(results: list[DurationResult]) -> pd.DataFrame:
    """Bucket DV01: allocate each instrument's DV01 to its tenor bucket.

    For bullet bonds the rate sensitivity sits at the maturity point, so each
    bond's DV01 is assigned to the bucket containing its maturity. The swap's
    DV01 is added to the 3-5Y bucket (5Y tenor). This produces the classic
    'duration ladder' the desk uses to see where curve risk is concentrated.
    """
    buckets = {name: 0.0 for name, _, _ in C.TENOR_BUCKETS}

    def _assign(years, dv01):
        # right-closed intervals (lo, hi], with the first bucket including 0,
        # so a 5Y instrument maps to the 3-5Y bucket rather than spilling up.
        for i, (name, lo, hi) in enumerate(C.TENOR_BUCKETS):
            lower_ok = years >= lo if i == 0 else years > lo
            if lower_ok and years <= hi:
                buckets[name] += dv01
                return
        buckets[C.TENOR_BUCKETS[-1][0]] += dv01

    for r in results:
        _assign(r.maturity_years, r.dv01)
    _assign(C.SWAP["tenor_years"], swap_dv01())

    return pd.DataFrame(
        [{"tenor_bucket": k, "bucket_dv01": v} for k, v in buckets.items()])


def yield_curve_shift(shifts_bps=(-200, -100, -50, 50, 100, 200)) -> pd.DataFrame:
    """Parallel yield-curve shift scenarios: book P&L for each shift.

    Reprices every bond at (ytm + shift) and sums the P&L vs base. This is the
    full-revaluation rate scenario (more accurate than the duration estimate
    for large shifts because it captures convexity).
    """
    rows = []
    for bps in shifts_bps:
        shift = bps / 10000.0
        pnl = 0.0
        for b in C.FIXED_INCOME:
            base = price_bond(b["coupon"], b["ytm"], b["maturity_years"],
                              b["face_value"], b["freq"])
            shocked = price_bond(b["coupon"], b["ytm"] + shift,
                                 b["maturity_years"], b["face_value"],
                                 b["freq"])
            pnl += (shocked - base) * b["quantity"]
        # swap P&L: pay-fixed gains when rates rise
        pnl += swap_dv01() * bps
        rows.append({"shift_bps": bps, "pnl_impact": pnl})
    return pd.DataFrame(rows)
