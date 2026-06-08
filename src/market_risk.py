"""
market_risk.py
==============
Market-risk engine: Value-at-Risk (VaR), Expected Shortfall (ES/CVaR),
Component VaR, Incremental VaR, and VaR backtesting.

Conventions follow Basel 2.5 / Basel III / FRTB:
  * 250-business-day lookback for historical simulation (Basel standard).
  * Square-root-of-time scaling for multi-day horizons: VaR_h = VaR_1 * sqrt(h).
  * Expected Shortfall reported alongside VaR (FRTB makes 97.5% ES the primary
    regulatory metric, replacing 99% VaR).
  * Sign convention: VaR and ES are reported as POSITIVE loss magnitudes (INR).

All P&L is built from a vector of dollar positions v_i (INR market value) and a
matrix of daily returns r_{i,t}. Daily portfolio P&L_t = sum_i v_i * r_{i,t}.

FRM references:
  * VaR definition & methods — FRM Part II, Market Risk Measurement & Mgmt
    (Jorion, "Value at Risk", ch. on HS / Delta-Normal / Monte Carlo).
  * Expected Shortfall / coherent risk measures — Artzner et al.; FRM Part II.
  * Component / Incremental / Marginal VaR — Jorion ch. "Portfolio VaR".
  * Backtesting & Basel Traffic Light — FRM Part II, Kupiec / Basel framework.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy.stats import norm

import config as C
from utils import get_connection, get_logger

LOG = get_logger("market_risk")


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_returns_and_values(conn) -> tuple[pd.DataFrame, pd.Series]:
    """Load the daily log-return matrix and current dollar positions.

    Returns
    -------
    returns : DataFrame (dates x instruments) of daily log returns
    values  : Series of current INR market value per instrument (qty * last px)

    Only instruments with a market_data price series participate in the linear
    VaR (equities + bonds). Options enter market risk via their delta-equivalent
    underlying exposure (handled in greeks_engine); the swap/FX forward are
    treated via their own rate/fx sensitivities. This keeps the linear VaR clean
    and avoids double counting.
    """
    md = pd.read_sql_query(
        "SELECT instrument_id, date, price, log_return FROM market_data "
        "ORDER BY date", conn)
    pos = pd.read_sql_query(
        "SELECT instrument_id, quantity FROM positions", conn)

    prices = md.pivot(index="date", columns="instrument_id", values="price")
    returns = md.pivot(index="date", columns="instrument_id",
                       values="log_return").fillna(0.0)

    qty = pos.set_index("instrument_id")["quantity"]
    last_px = prices.iloc[-1]
    common = [c for c in returns.columns if c in qty.index]
    values = (last_px[common] * qty[common]).astype(float)
    returns = returns[common]

    LOG.info("Loaded returns matrix %s; portfolio MV = %.0f INR",
             returns.shape, values.sum())
    return returns, values


# ---------------------------------------------------------------------------
# Result container
# ---------------------------------------------------------------------------
@dataclass
class VarResult:
    method: str
    confidence: float
    horizon: int
    var_value: float          # positive loss magnitude, INR
    es_value: float           # positive loss magnitude, INR
    portfolio_var: float = 0.0


# ---------------------------------------------------------------------------
# 1) Historical Simulation VaR + ES
# ---------------------------------------------------------------------------
def historical_var(returns: pd.DataFrame, values: pd.Series,
                   confidence: float, horizon: int = 1,
                   lookback: int = C.VAR_LOOKBACK_DAYS) -> VarResult:
    """Historical Simulation VaR & Expected Shortfall.

    Method: revalue the *current* book under every historical daily return
    vector in the lookback window, build the empirical P&L distribution, and
    read off the loss quantile.

        VaR_alpha = -Quantile_{1-alpha}( P&L )
        ES_alpha  = -E[ P&L | P&L <= -VaR_alpha ]   (average tail loss)

    Multi-day horizon uses the sqrt-of-time rule (Basel).

    Why banks use it: HS makes no distributional assumption (captures fat tails
    and observed correlations directly) — the most widely used desk VaR method.
    """
    window = returns.tail(lookback)
    pnl = window.values @ values.values          # daily P&L vector (INR)
    q = np.percentile(pnl, (1.0 - confidence) * 100.0)
    var_1d = -q
    tail = pnl[pnl <= q]
    es_1d = -tail.mean() if tail.size else var_1d
    scale = np.sqrt(horizon)
    return VarResult("Historical", confidence, horizon,
                     var_1d * scale, es_1d * scale)


# ---------------------------------------------------------------------------
# 2) Parametric (Variance-Covariance / Delta-Normal) VaR + ES
# ---------------------------------------------------------------------------
def parametric_var(returns: pd.DataFrame, values: pd.Series,
                   confidence: float, horizon: int = 1,
                   lookback: int = C.VAR_LOOKBACK_DAYS) -> VarResult:
    """Variance-Covariance VaR using the full covariance matrix.

        sigma_p = sqrt( v' * Sigma * v )         (portfolio P&L std, INR)
        VaR      = z_alpha * sigma_p
        ES       = sigma_p * phi(z_alpha) / (1 - alpha)   (normal closed form)

    where Sigma is the daily return covariance matrix, v the dollar positions,
    z_alpha the standard-normal quantile and phi the normal pdf.

    Why banks use it: fast, analytic, and the basis of the original RiskMetrics
    approach. Assumes normality — understates tail risk, hence ES is reported.
    """
    window = returns.tail(lookback)
    cov = np.cov(window.values, rowvar=False)
    v = values.values
    var_pnl = float(v @ cov @ v)
    sigma_p = np.sqrt(max(var_pnl, 0.0))
    z = norm.ppf(confidence)
    var_1d = z * sigma_p
    es_1d = sigma_p * norm.pdf(z) / (1.0 - confidence)
    scale = np.sqrt(horizon)
    return VarResult("Parametric", confidence, horizon,
                     var_1d * scale, es_1d * scale)


# ---------------------------------------------------------------------------
# 3) Monte Carlo VaR + ES (multivariate normal, Cholesky)
# ---------------------------------------------------------------------------
def monte_carlo_var(returns: pd.DataFrame, values: pd.Series,
                    confidence: float, horizon: int = 1,
                    n_sims: int = C.MC_SIMULATIONS,
                    lookback: int = C.VAR_LOOKBACK_DAYS,
                    seed: int = C.RANDOM_SEED) -> VarResult:
    """Monte Carlo VaR via Cholesky-correlated multivariate-normal returns.

    Steps:
      1. Estimate mean mu and covariance Sigma from the lookback window.
      2. Cholesky factor L such that  L L' = Sigma.
      3. Simulate r = mu + L z,  z ~ N(0, I)   (>= 10,000 paths).
      4. P&L = v' r; read VaR/ES off the simulated distribution.

    Why banks use it: handles non-linear instruments and arbitrary factor
    distributions; the workhorse for derivative-heavy books.
    """
    window = returns.tail(lookback)
    mu = window.mean().values
    cov = np.cov(window.values, rowvar=False)
    # nudge for numerical PSD-ness before Cholesky
    cov = cov + np.eye(cov.shape[0]) * 1e-12
    try:
        L = np.linalg.cholesky(cov)
    except np.linalg.LinAlgError:
        # fall back to eigenvalue clipping if not positive-definite
        w, Q = np.linalg.eigh(cov)
        w = np.clip(w, 1e-14, None)
        L = Q @ np.diag(np.sqrt(w))
    rng = np.random.default_rng(seed)
    z = rng.standard_normal((n_sims, len(mu)))
    sim_ret = mu + z @ L.T
    pnl = sim_ret @ values.values
    q = np.percentile(pnl, (1.0 - confidence) * 100.0)
    var_1d = -q
    tail = pnl[pnl <= q]
    es_1d = -tail.mean() if tail.size else var_1d
    scale = np.sqrt(horizon)
    return VarResult("MonteCarlo", confidence, horizon,
                     var_1d * scale, es_1d * scale)


# ---------------------------------------------------------------------------
# 4) Component VaR (marginal contribution of each position)
# ---------------------------------------------------------------------------
def component_var(returns: pd.DataFrame, values: pd.Series,
                  confidence: float,
                  lookback: int = C.VAR_LOOKBACK_DAYS) -> pd.DataFrame:
    """Component VaR: each position's additive contribution to portfolio VaR.

        Marginal VaR_i = z * (Sigma v)_i / sigma_p
        Component VaR_i = v_i * Marginal VaR_i
        sum_i Component VaR_i = total parametric VaR   (additivity)

    Why banks use it: shows *where* the risk is and supports limit allocation;
    component VaRs sum to the total, unlike standalone VaRs.
    """
    window = returns.tail(lookback)
    cov = np.cov(window.values, rowvar=False)
    v = values.values
    sigma_p = np.sqrt(max(float(v @ cov @ v), 0.0))
    z = norm.ppf(confidence)
    if sigma_p == 0:
        marginal = np.zeros_like(v)
    else:
        marginal = z * (cov @ v) / sigma_p
    comp = v * marginal
    out = pd.DataFrame({
        "instrument_id": values.index,
        "marginal_var": marginal,
        "component_var": comp,
        "pct_of_total": comp / comp.sum() if comp.sum() else 0.0,
    })
    return out.sort_values("component_var", ascending=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 5) Incremental VaR (impact of removing each position)
# ---------------------------------------------------------------------------
def incremental_var(returns: pd.DataFrame, values: pd.Series,
                    confidence: float,
                    lookback: int = C.VAR_LOOKBACK_DAYS) -> pd.DataFrame:
    """Incremental VaR: change in portfolio VaR if a position is removed.

        Incremental VaR_i = VaR(full book) - VaR(book without position i)

    Computed with the parametric method for stability. Positive => the position
    adds risk; negative => it is a diversifier/hedge.

    Why banks use it: pre-trade check — "what does this trade do to my VaR?"
    """
    base = parametric_var(returns, values, confidence).var_value
    rows = []
    for inst in values.index:
        keep = [c for c in values.index if c != inst]
        sub_var = parametric_var(returns[keep], values[keep],
                                 confidence).var_value
        rows.append({"instrument_id": inst,
                     "incremental_var": base - sub_var})
    out = pd.DataFrame(rows).sort_values("incremental_var", ascending=False)
    return out.reset_index(drop=True)


# ---------------------------------------------------------------------------
# 6) Backtesting + Basel Traffic Light
# ---------------------------------------------------------------------------
def backtest_var(returns: pd.DataFrame, values: pd.Series,
                 confidence: float = 0.99,
                 lookback: int = C.VAR_LOOKBACK_DAYS) -> pd.DataFrame:
    """Walk-forward HS-VaR backtest over the full history.

    For each day t beyond the first lookback window, compute the 1-day HS VaR
    from days [t-lookback, t-1] and compare to the realised P&L on day t. An
    *exception* is a day where the actual loss exceeds the VaR estimate.

    Returns a per-day DataFrame with actual_pnl, var_estimate, exception_flag.

    Why banks use it: regulators require VaR models to be backtested; the
    exception count drives the Basel Traffic Light multiplier on capital.
    """
    dates = returns.index.tolist()
    v = values.values
    rec = []
    for t in range(lookback, len(dates)):
        window = returns.iloc[t - lookback:t]
        pnl_hist = window.values @ v
        q = np.percentile(pnl_hist, (1.0 - confidence) * 100.0)
        var_est = -q
        actual = float(returns.iloc[t].values @ v)
        exc = 1 if actual < -var_est else 0
        rec.append({"date": dates[t], "actual_pnl": actual,
                    "var_estimate": var_est, "confidence_level": confidence,
                    "exception_flag": exc})
    df = pd.DataFrame(rec)
    LOG.info("Backtest @ %.0f%%: %d days, %d exceptions (%.2f%% vs %.1f%% exp.)",
             confidence * 100, len(df), df["exception_flag"].sum(),
             100 * df["exception_flag"].mean(), (1 - confidence) * 100)
    return df


def basel_traffic_light(n_exceptions: int, n_days: int = 250) -> dict:
    """Basel Traffic Light zone classification (99% VaR, 250-day window).

    Zones (Basel Committee, 1996 backtesting framework):
        Green  : 0-4 exceptions   -> model acceptable, multiplier 3.00
        Yellow : 5-9 exceptions   -> multiplier scales 3.40 .. 3.85
        Red    : 10+ exceptions   -> model rejected,  multiplier 4.00

    Why banks use it: the zone sets the capital multiplier applied to VaR for
    market-risk regulatory capital.
    """
    yellow_mult = {5: 3.40, 6: 3.50, 7: 3.65, 8: 3.75, 9: 3.85}
    if n_exceptions <= 4:
        zone, mult = "Green", 3.00
    elif n_exceptions <= 9:
        zone, mult = "Yellow", yellow_mult[n_exceptions]
    else:
        zone, mult = "Red", 4.00
    return {"n_exceptions": n_exceptions, "n_days": n_days,
            "zone": zone, "capital_multiplier": mult,
            "exception_rate": n_exceptions / n_days}


# ---------------------------------------------------------------------------
# Convenience: run the full VaR suite
# ---------------------------------------------------------------------------
def run_all_var(returns: pd.DataFrame, values: pd.Series) -> list[VarResult]:
    """Compute every (method, confidence, horizon) combination."""
    results = []
    methods = (historical_var, parametric_var, monte_carlo_var)
    for cl in C.CONFIDENCE_LEVELS:
        for h in C.HORIZONS_DAYS:
            for fn in methods:
                results.append(fn(returns, values, cl, h))
    return results
