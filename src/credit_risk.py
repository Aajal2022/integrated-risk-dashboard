"""
credit_risk.py
==============
Credit risk under the Basel IRB (Internal Ratings-Based) framework:
Expected Loss, Credit VaR via the Vasicek single-factor (ASRF) model, and
portfolio concentration via the Herfindahl-Hirschman Index.

Expected Loss (the price of credit risk, provisioned through P&L):

    EL = PD * LGD * EAD

Vasicek / Asymptotic Single Risk Factor (the engine of Basel IRB capital):
each obligor's standardised asset value is

    A_i = sqrt(rho) * M + sqrt(1 - rho) * Z_i ,   M, Z_i ~ N(0,1)

default occurs when A_i < N^{-1}(PD). Conditioning on a stressed systematic
factor at confidence q gives the worst-case conditional default probability

    PD(q) = N( ( N^{-1}(PD) + sqrt(rho) * N^{-1}(q) ) / sqrt(1 - rho) )

and the Credit VaR (Unexpected Loss = stressed loss minus expected loss):

    CreditVaR = LGD * EAD * ( PD(q) - PD )

Basel sets q = 99.9% for IRB capital. Asset correlation rho is prescribed:
~0.12 for general corporates, higher for large financials (we use 0.15 banks).

FRM references:
  * EL / UL, PD-LGD-EAD — FRM Part II, Credit Risk Measurement & Management.
  * Vasicek / ASRF single-factor model & Basel IRB — FRM Part II, Credit Risk;
    Basel II/III IRB capital formula (Gordy 2003).
  * HHI concentration — FRM Part II, portfolio credit / concentration risk.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.stats import norm

import config as C
from utils import get_logger

LOG = get_logger("credit_risk")

CREDIT_VAR_CONFIDENCE = 0.999  # Basel IRB capital confidence level


@dataclass
class CreditResult:
    counterparty_id: str
    pd_estimate: float
    lgd: float
    ead: float
    expected_loss: float
    credit_var: float       # unexpected loss at 99.9% (INR)


def _rho_for(ctype: str) -> float:
    """Basel-prescribed asset correlation by counterparty type."""
    return C.VASICEK_RHO_BANK if ctype == "bank" else C.VASICEK_RHO_CORPORATE


def vasicek_conditional_pd(pd_: float, rho: float,
                           q: float = CREDIT_VAR_CONFIDENCE) -> float:
    """Worst-case conditional default probability at confidence q (ASRF)."""
    return float(norm.cdf(
        (norm.ppf(pd_) + np.sqrt(rho) * norm.ppf(q)) / np.sqrt(1 - rho)))


def counterparty_credit(cp: dict,
                        q: float = CREDIT_VAR_CONFIDENCE) -> CreditResult:
    """Expected Loss and Vasicek Credit VaR for one counterparty."""
    pd_, lgd, ead = cp["pd"], cp["lgd"], cp["ead"]
    rho = _rho_for(cp["ctype"])
    el = pd_ * lgd * ead
    cond_pd = vasicek_conditional_pd(pd_, rho, q)
    credit_var = lgd * ead * (cond_pd - pd_)   # unexpected loss
    LOG.info("%s: EL=%.0f CreditVaR(99.9%%)=%.0f (PD=%.2f%% condPD=%.2f%%)",
             cp["counterparty_id"], el, credit_var, pd_ * 100, cond_pd * 100)
    return CreditResult(cp["counterparty_id"], pd_, lgd, ead, el, credit_var)


def compute_book_credit() -> list[CreditResult]:
    """Run EL / Credit VaR for every counterparty in the book."""
    return [counterparty_credit(cp) for cp in C.COUNTERPARTIES]


def herfindahl_index(eads: list[float]) -> dict:
    """Herfindahl-Hirschman Index of credit-exposure concentration.

        share_i = EAD_i / sum(EAD) ;  HHI = sum_i share_i^2

    HHI ranges from 1/n (perfectly diversified) to 1 (single name). Reported
    here both raw and scaled x10000 (the antitrust convention). A higher HHI =
    more concentrated = more name/sector risk than EL alone reveals.
    """
    total = sum(eads)
    shares = np.array(eads) / total
    hhi = float(np.sum(shares ** 2))
    n = len(eads)
    return {"hhi": hhi, "hhi_scaled": hhi * 10000,
            "effective_n": 1.0 / hhi, "n_counterparties": n,
            "min_hhi": 1.0 / n}


def portfolio_credit_var_mc(q: float = CREDIT_VAR_CONFIDENCE,
                            n_sims: int = 100_000,
                            seed: int = C.RANDOM_SEED) -> dict:
    """Monte Carlo portfolio Credit VaR under the correlated Vasicek model.

    Simulates one common systematic factor M and idiosyncratic shocks per
    counterparty, triggers defaults, and builds the loss distribution. This
    captures default *correlation* across names (which the per-name ASRF sum
    ignores) and is the basis of economic-capital models.
    """
    rng = np.random.default_rng(seed)
    cps = C.COUNTERPARTIES
    pds = np.array([c["pd"] for c in cps])
    lgds = np.array([c["lgd"] for c in cps])
    eads = np.array([c["ead"] for c in cps])
    rhos = np.array([_rho_for(c["ctype"]) for c in cps])
    thresh = norm.ppf(pds)

    M = rng.standard_normal(n_sims)
    losses = np.zeros(n_sims)
    for i in range(len(cps)):
        Z = rng.standard_normal(n_sims)
        A = np.sqrt(rhos[i]) * M + np.sqrt(1 - rhos[i]) * Z
        defaulted = A < thresh[i]
        losses += defaulted * lgds[i] * eads[i]

    el = float((pds * lgds * eads).sum())
    var_q = float(np.percentile(losses, q * 100))
    credit_var = var_q - el
    LOG.info("Portfolio MC Credit VaR(99.9%%)=%.0f (EL=%.0f, worst loss=%.0f)",
             credit_var, el, losses.max())
    return {"expected_loss": el, "loss_var": var_q,
            "credit_var": credit_var, "max_loss": float(losses.max())}
