"""
main.py
=======
Pipeline orchestrator for the Integrated Risk Dashboard (Layer 2).

Runs the full risk run end to end:
    1. Build / refresh the SQL database and 2-year synthetic history.
    2. Compute market risk  (VaR x3 methods, ES, Component & Incremental VaR).
    3. Compute option Greeks (BSM) and portfolio aggregates.
    4. Compute fixed-income risk (duration, DV01, convexity, bucket DV01).
    5. Compute credit risk (EL, Vasicek Credit VaR, HHI).
    6. Run stress scenarios.
    7. Backtest VaR and classify the Basel Traffic Light zone.
    8. Persist EVERY result to SQL with a calculation timestamp (auditability).

Run:  python main.py
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

import config as C
from utils import get_connection, get_logger
import database_setup as db
import market_risk as mr
import greeks_engine as ge
import fixed_income_risk as fi
import credit_risk as cr
import stress_testing as st

LOG = get_logger("main")
NOW = datetime.now().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------
def _persist_var(conn, results, component, incremental):
    """Write portfolio, component and incremental VaR/ES rows."""
    rows = []
    # portfolio-level VaR/ES across method x confidence x horizon
    for r in results:
        rows.append((NOW, r.method, r.confidence, r.horizon,
                     r.var_value, r.es_value, r.var_value, None, "Portfolio"))
    # component VaR (1-day, 99%)
    for _, c in component.iterrows():
        rows.append((NOW, "Parametric", 0.99, 1, c["component_var"], None,
                     None, c["instrument_id"], "Component"))
    # incremental VaR (1-day, 99%)
    for _, c in incremental.iterrows():
        rows.append((NOW, "Parametric", 0.99, 1, c["incremental_var"], None,
                     None, c["instrument_id"], "Incremental"))
    conn.executemany(
        "INSERT INTO var_results (calculation_date, method, confidence_level, "
        "horizon_days, var_value, es_value, portfolio_var, instrument_id, "
        "metric_type) VALUES (?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    LOG.info("Persisted %d var_results rows.", len(rows))


def _persist_greeks(conn, per_option):
    rows = [(g.instrument_id, NOW, g.delta, g.gamma, g.vega, g.theta, g.rho)
            for g in per_option]
    conn.executemany(
        "INSERT INTO greeks (instrument_id, calculation_date, delta, gamma, "
        "vega, theta, rho) VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    LOG.info("Persisted %d greeks rows.", len(rows))


def _persist_duration(conn, dur_results):
    rows = [(d.instrument_id, NOW, d.modified_duration, d.macaulay_duration,
             d.dv01, d.convexity) for d in dur_results]
    conn.executemany(
        "INSERT INTO duration_metrics (instrument_id, calculation_date, "
        "modified_duration, macaulay_duration, dv01, convexity) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    LOG.info("Persisted %d duration_metrics rows.", len(rows))


def _persist_credit(conn, credit_results):
    rows = [(c.counterparty_id, NOW, c.pd_estimate, c.lgd, c.ead,
             c.expected_loss, c.credit_var) for c in credit_results]
    conn.executemany(
        "INSERT INTO credit_metrics (counterparty_id, calculation_date, "
        "pd_estimate, lgd, ead, expected_loss, credit_var) "
        "VALUES (?,?,?,?,?,?,?)", rows)
    conn.commit()
    LOG.info("Persisted %d credit_metrics rows.", len(rows))


def _persist_stress(conn, stress_df):
    rows = [(r["scenario_name"], NOW, r["pnl_impact"], r["var_impact"],
             r["asset_class"], r["description"])
            for _, r in stress_df.iterrows()]
    conn.executemany(
        "INSERT INTO stress_test_results (scenario_name, calculation_date, "
        "pnl_impact, var_impact, asset_class, description) "
        "VALUES (?,?,?,?,?,?)", rows)
    conn.commit()
    LOG.info("Persisted %d stress_test_results rows.", len(rows))


def _persist_backtest(conn, bt_df):
    rows = [(r["date"], r["actual_pnl"], r["var_estimate"],
             r["confidence_level"], int(r["exception_flag"]))
            for _, r in bt_df.iterrows()]
    conn.executemany(
        "INSERT INTO backtesting_results (date, actual_pnl, var_estimate, "
        "confidence_level, exception_flag) VALUES (?,?,?,?,?)", rows)
    conn.commit()
    LOG.info("Persisted %d backtesting_results rows.", len(rows))


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run_pipeline() -> None:
    """Execute the full risk run and persist all outputs to SQL."""
    LOG.info("=" * 70)
    LOG.info("INTEGRATED RISK DASHBOARD — pipeline start @ %s", NOW)
    LOG.info("=" * 70)

    # ---- Layer 1: build database + synthetic data ----
    db.build_database()

    conn = get_connection()
    try:
        # ---- Market risk ----
        LOG.info("--- Market risk (VaR / ES / Component / Incremental) ---")
        returns, values = mr.load_returns_and_values(conn)
        portfolio_mv = float(values.sum())
        var_results = mr.run_all_var(returns, values)
        component = mr.component_var(returns, values, 0.99)
        incremental = mr.incremental_var(returns, values, 0.99)
        _persist_var(conn, var_results, component, incremental)

        # base 1-day 99% VaR for stress / limit calcs
        base_var = next(r.var_value for r in var_results
                        if r.method == "Historical"
                        and r.confidence == 0.99 and r.horizon == 1)

        # ---- Greeks ----
        LOG.info("--- Option Greeks (BSM) ---")
        per_option, agg = ge.compute_book_greeks(conn)
        _persist_greeks(conn, per_option)

        # ---- Fixed income ----
        LOG.info("--- Fixed income risk ---")
        dur_results = fi.compute_book_duration(conn)
        _persist_duration(conn, dur_results)
        bucket = fi.bucket_dv01(dur_results)
        curve = fi.yield_curve_shift()

        # ---- Credit ----
        LOG.info("--- Credit risk (IRB / Vasicek) ---")
        credit_results = cr.compute_book_credit()
        _persist_credit(conn, credit_results)
        hhi = cr.herfindahl_index([c.ead for c in credit_results])
        port_cvar = cr.portfolio_credit_var_mc()

        # ---- Stress testing ----
        LOG.info("--- Stress testing ---")
        last_px = pd.read_sql_query(
            "SELECT instrument_id, price FROM market_data WHERE date = "
            "(SELECT MAX(date) FROM market_data)", conn
        ).set_index("instrument_id")["price"]
        stress_df = st.run_stress_tests(last_px, base_var)
        _persist_stress(conn, stress_df)

        # ---- Backtesting ----
        LOG.info("--- Backtesting & Basel Traffic Light ---")
        bt = mr.backtest_var(returns, values, 0.99)
        _persist_backtest(conn, bt)
        tl = mr.basel_traffic_light(int(bt["exception_flag"].sum()), len(bt))

        # ---- Console summary ----
        _print_summary(portfolio_mv, base_var, var_results, component,
                       hhi, port_cvar, tl, bucket, curve)
        LOG.info("Pipeline complete. Database ready for Power BI: %s", C.DB_PATH)
    except Exception as exc:  # noqa: BLE001 — top-level guard for logging
        LOG.exception("Pipeline failed: %s", exc)
        raise
    finally:
        conn.close()


def _print_summary(mv, base_var, var_results, component, hhi, port_cvar,
                   tl, bucket, curve):
    """Human-readable run summary to console."""
    es975 = next(r.es_value for r in var_results
                 if r.method == "Historical" and r.confidence == 0.975
                 and r.horizon == 1)
    var_limit = mv * C.VAR_LIMIT_PCT_OF_NAV
    print("\n" + "=" * 64)
    print("  RISK RUN SUMMARY")
    print("=" * 64)
    print(f"  Portfolio market value      : INR {mv:,.0f}")
    print(f"  1-day 99% VaR (Historical)  : INR {base_var:,.0f}")
    print(f"  1-day 97.5% ES (Basel III)  : INR {es975:,.0f}")
    print(f"  VaR limit (2% of NAV)       : INR {var_limit:,.0f}")
    print(f"  VaR utilisation             : {base_var / var_limit:6.1%}")
    print(f"  Top risk contributor        : {component.iloc[0]['instrument_id']}"
          f" (Comp VaR INR {component.iloc[0]['component_var']:,.0f})")
    print(f"  Credit HHI                  : {hhi['hhi']:.4f} "
          f"(eff. names {hhi['effective_n']:.1f})")
    print(f"  Portfolio Credit VaR 99.9%  : INR {port_cvar['credit_var']:,.0f}")
    print(f"  Basel Traffic Light         : {tl['zone']} "
          f"({tl['n_exceptions']} exceptions, x{tl['capital_multiplier']})")
    print("=" * 64 + "\n")


if __name__ == "__main__":
    run_pipeline()
