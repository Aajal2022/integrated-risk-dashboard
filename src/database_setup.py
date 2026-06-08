"""
database_setup.py
=================
Layer-1 builder. Creates the full SQL schema and populates the static book
plus two years of synthetic daily market history.

Synthetic prices are generated with **Geometric Brownian Motion (GBM)**:

    S_t = S_{t-1} * exp( (mu - 0.5*sigma^2) * dt  +  sigma * sqrt(dt) * Z_t )

    where dt = 1/252 (one trading day), Z_t ~ N(0,1).

GBM is the standard textbook model for equity price evolution (the same SDE
that underlies Black-Scholes). Using sector-appropriate volatilities makes the
synthetic history behave like a real multi-asset book, which is what the VaR /
stress / backtesting engines downstream consume.

FRM reference: GBM & lognormal price dynamics — FRM Part I, Valuation & Risk
Models (Black-Scholes-Merton); Part II, Market Risk Measurement & Management.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import numpy as np
import pandas as pd

import config as C
from utils import get_connection, get_logger

LOG = get_logger("database_setup")


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------
def create_tables(conn: sqlite3.Connection) -> None:
    """Create every table in the schema (idempotent via IF NOT EXISTS)."""
    LOG.info("Creating tables ...")
    ddl = """
    CREATE TABLE IF NOT EXISTS positions (
        instrument_id TEXT PRIMARY KEY, asset_class TEXT NOT NULL,
        description TEXT NOT NULL, notional REAL NOT NULL, quantity REAL NOT NULL,
        entry_price REAL NOT NULL, entry_date TEXT NOT NULL);

    CREATE TABLE IF NOT EXISTS market_data (
        instrument_id TEXT NOT NULL, date TEXT NOT NULL, price REAL NOT NULL,
        volume REAL, log_return REAL,
        PRIMARY KEY (instrument_id, date));

    CREATE TABLE IF NOT EXISTS risk_factors (
        factor_id TEXT NOT NULL, factor_name TEXT NOT NULL, factor_type TEXT NOT NULL,
        date TEXT NOT NULL, value REAL NOT NULL,
        PRIMARY KEY (factor_id, date));

    CREATE TABLE IF NOT EXISTS var_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT, calculation_date TEXT NOT NULL,
        method TEXT NOT NULL, confidence_level REAL NOT NULL, horizon_days INTEGER NOT NULL,
        var_value REAL NOT NULL, es_value REAL, portfolio_var REAL,
        instrument_id TEXT, metric_type TEXT);

    CREATE TABLE IF NOT EXISTS greeks (
        id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id TEXT NOT NULL,
        calculation_date TEXT NOT NULL, delta REAL, gamma REAL, vega REAL,
        theta REAL, rho REAL);

    CREATE TABLE IF NOT EXISTS duration_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, instrument_id TEXT NOT NULL,
        calculation_date TEXT NOT NULL, modified_duration REAL, macaulay_duration REAL,
        dv01 REAL, convexity REAL);

    CREATE TABLE IF NOT EXISTS credit_metrics (
        id INTEGER PRIMARY KEY AUTOINCREMENT, counterparty_id TEXT NOT NULL,
        calculation_date TEXT NOT NULL, pd_estimate REAL, lgd REAL, ead REAL,
        expected_loss REAL, credit_var REAL);

    CREATE TABLE IF NOT EXISTS stress_test_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT, scenario_name TEXT NOT NULL,
        calculation_date TEXT NOT NULL, pnl_impact REAL, var_impact REAL,
        asset_class TEXT, description TEXT);

    CREATE TABLE IF NOT EXISTS backtesting_results (
        id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL,
        actual_pnl REAL NOT NULL, var_estimate REAL NOT NULL,
        confidence_level REAL NOT NULL, exception_flag INTEGER NOT NULL);
    """
    conn.executescript(ddl)
    conn.commit()
    LOG.info("Tables created.")


def _clear_data(conn: sqlite3.Connection) -> None:
    """Wipe data so re-runs are clean (full rebuild semantics)."""
    for tbl in ("positions", "market_data", "risk_factors", "var_results",
                "greeks", "duration_metrics", "credit_metrics",
                "stress_test_results", "backtesting_results"):
        conn.execute(f"DELETE FROM {tbl};")
    conn.commit()


# ---------------------------------------------------------------------------
# Trading calendar
# ---------------------------------------------------------------------------
def _business_days(n: int) -> list:
    """Return n consecutive business days starting at HISTORY_START."""
    days, d = [], C.HISTORY_START
    while len(days) < n:
        if d.weekday() < 5:  # Mon-Fri
            days.append(d)
        d += timedelta(days=1)
    return days


# ---------------------------------------------------------------------------
# GBM path generator
# ---------------------------------------------------------------------------
def _gbm_path(s0: float, mu: float, sigma: float, n: int,
              rng: np.random.Generator) -> np.ndarray:
    """Simulate a GBM price path of length n (including the starting point).

    Returns an array of prices. dt = 1/252 (daily).
    """
    dt = 1.0 / C.TRADING_DAYS_PER_YEAR
    shocks = rng.standard_normal(n - 1)
    increments = (mu - 0.5 * sigma ** 2) * dt + sigma * np.sqrt(dt) * shocks
    log_path = np.concatenate([[0.0], np.cumsum(increments)])
    return s0 * np.exp(log_path)


# ---------------------------------------------------------------------------
# Population routines
# ---------------------------------------------------------------------------
def populate_positions(conn: sqlite3.Connection) -> None:
    """Insert the full multi-asset book into `positions`."""
    LOG.info("Populating positions ...")
    rows = []
    ed = C.HISTORY_START.isoformat()

    for e in C.EQUITIES:
        rows.append((e["instrument_id"], "Equity", e["description"],
                     e["entry_price"] * e["quantity"], e["quantity"],
                     e["entry_price"], ed))

    for b in C.FIXED_INCOME:
        notional = b["face_value"] * b["quantity"]
        rows.append((b["instrument_id"], "FixedIncome", b["description"],
                     notional, b["quantity"], b["face_value"], ed))

    for o in C.OPTIONS:
        rows.append((o["instrument_id"], "Option", o["description"],
                     o["strike"] * o["quantity"], o["quantity"],
                     o["strike"], ed))

    rows.append((C.SWAP["instrument_id"], "Swap", C.SWAP["description"],
                 C.SWAP["notional"], 1, C.SWAP["fixed_rate"], ed))

    rows.append((C.FX_FORWARD["instrument_id"], "FX", C.FX_FORWARD["description"],
                 C.FX_FORWARD["usd_notional"] * C.FX_FORWARD["spot"],
                 C.FX_FORWARD["usd_notional"], C.FX_FORWARD["contract_rate"], ed))

    conn.executemany(
        "INSERT INTO positions (instrument_id, asset_class, description, "
        "notional, quantity, entry_price, entry_date) VALUES (?,?,?,?,?,?,?)",
        rows)
    conn.commit()
    LOG.info("Inserted %d positions.", len(rows))


def populate_market_data(conn: sqlite3.Connection,
                         rng: np.random.Generator) -> dict:
    """Generate GBM history for equities (and option underlyings already
    covered) plus price proxies for bonds/FX, write to market_data.

    Returns a dict {instrument_id: price_series(np.ndarray)} for reuse.
    """
    LOG.info("Generating %d days of synthetic market history ...",
             C.N_TRADING_DAYS)
    days = _business_days(C.N_TRADING_DAYS)
    iso = [d.isoformat() for d in days]
    series = {}
    rows = []

    # --- equities via GBM ---
    for e in C.EQUITIES:
        p = C.EQUITY_GBM[e["instrument_id"]]
        path = _gbm_path(e["entry_price"], p["mu"], p["sigma"],
                         C.N_TRADING_DAYS, rng)
        series[e["instrument_id"]] = path
        log_ret = np.concatenate([[0.0], np.diff(np.log(path))])
        vol = rng.integers(500_000, 2_000_000, C.N_TRADING_DAYS)
        for i in range(C.N_TRADING_DAYS):
            rows.append((e["instrument_id"], iso[i], float(path[i]),
                         float(vol[i]), float(log_ret[i])))

    # --- bonds: simulate clean-price proxy from yield path (handled in
    #     risk_factors); here store a price series so VaR has a return stream.
    for b in C.FIXED_INCOME:
        # treat bond price as mildly volatile GBM around par-equivalent
        sigma_b = 0.04 if b["fi_type"] == "govt" else 0.05
        path = _gbm_path(b["face_value"], 0.0, sigma_b,
                         C.N_TRADING_DAYS, rng)
        series[b["instrument_id"]] = path
        log_ret = np.concatenate([[0.0], np.diff(np.log(path))])
        for i in range(C.N_TRADING_DAYS):
            rows.append((b["instrument_id"], iso[i], float(path[i]),
                         None, float(log_ret[i])))

    conn.executemany(
        "INSERT INTO market_data (instrument_id, date, price, volume, "
        "log_return) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    LOG.info("Inserted %d market_data rows.", len(rows))
    series["_dates"] = iso
    return series


def populate_risk_factors(conn: sqlite3.Connection,
                          series: dict,
                          rng: np.random.Generator) -> None:
    """Write risk-factor time series: equity levels, key rates, FX, vol."""
    LOG.info("Populating risk_factors ...")
    iso = series["_dates"]
    n = len(iso)
    rows = []

    # equity factors mirror their price series
    for e in C.EQUITIES:
        path = series[e["instrument_id"]]
        for i in range(n):
            rows.append((f"RF_{e['instrument_id']}", e["description"],
                         "equity", iso[i], float(path[i])))

    # rate factors: 5Y, 10Y, 91D yields as mean-reverting-ish random walks
    for fac, base in (("RF_RATE_5Y", 0.0715),
                      ("RF_RATE_10Y", 0.0815),
                      ("RF_RATE_91D", 0.0660)):
        dt = 1.0 / C.TRADING_DAYS_PER_YEAR
        shocks = rng.standard_normal(n - 1) * C.RATE_GBM_VOL * np.sqrt(dt)
        lvl = base + np.concatenate([[0.0], np.cumsum(shocks)])
        lvl = np.clip(lvl, 0.001, None)  # yields stay positive
        for i in range(n):
            rows.append((fac, fac.replace("RF_RATE_", "INR ") + " yield",
                         "rate", iso[i], float(lvl[i])))

    # FX factor: USD/INR spot via GBM with mild drift
    fx = _gbm_path(C.FX_FORWARD["spot"], 0.03, 0.06, n, rng)
    series["RF_FX_USDINR"] = fx
    for i in range(n):
        rows.append(("RF_FX_USDINR", "USD/INR spot", "fx", iso[i], float(fx[i])))

    # implied-vol factors for options
    for o in C.OPTIONS:
        vpath = np.clip(o["implied_vol"]
                        + np.cumsum(rng.standard_normal(n) * 0.002),
                        0.05, 1.0)
        for i in range(n):
            rows.append((f"RF_VOL_{o['instrument_id']}",
                         o["description"] + " IV", "vol", iso[i],
                         float(vpath[i])))

    conn.executemany(
        "INSERT INTO risk_factors (factor_id, factor_name, factor_type, "
        "date, value) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    LOG.info("Inserted %d risk_factor rows.", len(rows))


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def build_database() -> dict:
    """Full Layer-1 build. Returns the in-memory series dict for convenience."""
    rng = np.random.default_rng(C.RANDOM_SEED)
    conn = get_connection()
    try:
        create_tables(conn)
        _clear_data(conn)
        populate_positions(conn)
        series = populate_market_data(conn, rng)
        populate_risk_factors(conn, series, rng)
        LOG.info("Database build complete: %s", C.DB_PATH)
        return series
    except sqlite3.Error as exc:
        LOG.exception("Database build failed: %s", exc)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    build_database()
