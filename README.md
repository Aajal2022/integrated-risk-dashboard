# Integrated Risk Dashboard — Multi-Asset Trading Book

A production-style **market & credit risk system** that replicates a mid-size bank's
risk function across a multi-asset trading book. It computes the full suite of
regulatory risk metrics — Value-at-Risk, Expected Shortfall, Greeks, duration/DV01,
credit risk, stress tests and backtesting — persists every result to a SQL database
with audit timestamps, and exposes the data through an interactive dashboard and a
detailed analytical report.

> **Note:** Market data is synthetic (Geometric Brownian Motion with sector-calibrated
> volatilities). This is an educational, methodology-demonstrating project — not live
> risk on the named companies.

---

## What it does

The book spans five asset classes — Indian equities, fixed income, listed options,
an interest-rate swap and an FX forward — and is analysed end to end:

- **Market risk:** VaR via Historical Simulation, Parametric (variance-covariance)
  and Monte Carlo (10,000+ correlated paths); Expected Shortfall; component &
  incremental VaR.
- **Backtesting:** 254-day walk-forward 99% VaR vs realised P&L, with Basel
  traffic-light zone classification.
- **Stress testing:** five calibrated scenarios (COVID crash, Taper Tantrum, IL&FS,
  +300 bps rate shock, INR depreciation) with per-asset-class P&L attribution.
- **Credit risk:** Basel IRB / Vasicek ASRF model — PD, LGD, EAD, Expected Loss,
  99.9% Credit VaR, and HHI concentration.
- **Derivatives & fixed income:** Black-Scholes-Merton Greeks; Macaulay/modified
  duration, DV01 and convexity.

## Repository structure

```
src/                Python risk engine (one module per metric family)
  config.py           book definition + Basel/FRTB conventions
  database_setup.py   schema + synthetic GBM history
  market_risk.py      VaR (x3), ES, component/incremental VaR, backtest
  greeks_engine.py    Black-Scholes-Merton Greeks
  fixed_income_risk.py duration, DV01, convexity, curve shifts
  credit_risk.py      Expected Loss, Vasicek Credit VaR, HHI
  stress_testing.py   five stress scenarios
  main.py             orchestrator — writes all results to SQL
sql/schema.sql        all CREATE TABLE statements
output/               generated database + audit log
docs/                 Power BI connection guide
dashboard/            interactive HTML dashboard (open in any browser)
charts/               12 publication-quality chart images
reports/              50-page analytical risk report (Word)
requirements.txt      Python dependencies
```

## How to run

```bash
pip install -r requirements.txt
cd src
python main.py
```

This rebuilds the SQLite database and computes every metric. To view results without
running anything, open `dashboard/risk_dashboard.html` in a browser, or read
`reports/Integrated_Risk_Dashboard_Report.docx`.

## Deliverables

- **`dashboard/risk_dashboard.html`** — interactive dashboard with asset-class and
  date-range filters across seven analytical views.
- **`charts/`** — the 12 charts (VaR, backtesting, stress, credit, Greeks, etc.).
- **`reports/`** — a 50-page institutional-style written report.

## Frameworks & references

Basel III / FRTB; FRM curriculum — Jorion (VaR), Hull (derivatives), Tuckman
(fixed income), Gordy 2003 (IRB credit model).
