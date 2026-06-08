# Connecting Power BI to the SQLite Risk Database

Power BI has no native SQLite connector, so you connect through **ODBC**. This
guide covers the driver setup, the data import, and a page-by-page build of all
seven dashboard pages with the DAX measures you need.

The database is at `output/risk_dashboard.db` (created by `python src/main.py`).

---

## A. One-time setup — SQLite ODBC driver

1. Download and install the **SQLite ODBC Driver** (Christian Werner's driver):
   <http://www.ch-werner.de/sqliteodbc/> — install the 64-bit version
   (`sqliteodbc_w64.exe`) to match 64-bit Power BI Desktop.
2. Open **ODBC Data Sources (64-bit)** in Windows.
3. Go to the **System DSN** tab → **Add** → choose **SQLite3 ODBC Driver**.
4. **Data Source Name:** `RiskDashboard`. **Database Name:** browse to the full
   path of `risk_dashboard.db`. Click **OK**.

> macOS/Linux: install `sqliteodbc` via Homebrew/apt and register the DSN in
> `odbc.ini`, or use the alternative DirectQuery path below.

---

## B. Import the data into Power BI

1. Power BI Desktop → **Home → Get Data → ODBC**.
2. Pick the `RiskDashboard` DSN → **OK** → in the Navigator select all nine
   tables: `positions`, `market_data`, `risk_factors`, `var_results`,
   `greeks`, `duration_metrics`, `credit_metrics`, `stress_test_results`,
   `backtesting_results`.
3. Click **Load** (Import mode — fast, supports the full visual set). Use
   **DirectQuery** instead only if you want the dashboard to reflect a fresh
   `main.py` run without clicking Refresh.
4. **Refresh** (Home → Refresh) re-pulls metrics after each pipeline run.

### Model relationships

In **Model view**, create these relationships (single-direction, many-to-one
toward `positions`):

- `market_data[instrument_id]` → `positions[instrument_id]`
- `greeks[instrument_id]` → `positions[instrument_id]`
- `duration_metrics[instrument_id]` → `positions[instrument_id]`
- `var_results[instrument_id]` → `positions[instrument_id]` (inactive is fine)

`risk_factors`, `credit_metrics`, `stress_test_results` and
`backtesting_results` stand alone (no FK needed for the visuals below).

---

## C. Core DAX measures

Create these in a new table called `_Measures` (Home → Enter Data → blank
table, then add measures).

```DAX
Portfolio MV =
SUMX ( 'positions', 'positions'[notional] )

-- latest portfolio 99% 1-day Historical VaR
VaR 99 1d =
CALCULATE (
    MAX ( 'var_results'[var_value] ),
    'var_results'[method] = "Historical",
    'var_results'[confidence_level] = 0.99,
    'var_results'[horizon_days] = 1,
    'var_results'[metric_type] = "Portfolio"
)

-- Basel III primary metric: 97.5% Expected Shortfall, 1-day
ES 97.5 1d =
CALCULATE (
    MAX ( 'var_results'[es_value] ),
    'var_results'[method] = "Historical",
    'var_results'[confidence_level] = 0.975,
    'var_results'[horizon_days] = 1
)

VaR Limit = [Portfolio MV] * 0.02          -- 2% of NAV desk limit

VaR Utilisation = DIVIDE ( [VaR 99 1d], [VaR Limit] )

-- conditional formatting helper: red when limit breached
VaR Breach Flag = IF ( [VaR 99 1d] > [VaR Limit], 1, 0 )

Total Expected Loss =
SUM ( 'credit_metrics'[expected_loss] )

Total Credit VaR =
SUM ( 'credit_metrics'[credit_var] )

Exception Count =
CALCULATE (
    SUM ( 'backtesting_results'[exception_flag] ),
    'backtesting_results'[confidence_level] = 0.99
)

Exception Rate =
DIVIDE ( [Exception Count], COUNTROWS ( 'backtesting_results' ) )

Traffic Light Zone =
VAR n = [Exception Count]
RETURN SWITCH ( TRUE (),
    n <= 4, "GREEN",
    n <= 9, "YELLOW",
    "RED" )
```

---

## D. Page-by-page build

### Page 1 — Executive Risk Summary
- **Cards:** `Portfolio MV`, `VaR 99 1d` (set conditional formatting → font
  colour red when `VaR Breach Flag = 1`), `ES 97.5 1d`.
- **Gauge:** value `VaR Utilisation`, max = 1, target = 1 (limit). 
- **Pie:** legend `positions[asset_class]`, values `Portfolio MV`.
- **Bar (Top-5 Component VaR):** filter `var_results[metric_type]="Component"`,
  axis `instrument_id`, value `var_value`, Top-5 by value.

### Page 2 — Market Risk Detail
- **Matrix (VaR comparison):** rows `var_results[method]`, columns
  `confidence_level`, values `var_value` — filter `metric_type="Portfolio"`,
  `horizon_days=1`. Add a second matrix for `es_value`.
- **VaR term structure:** line/clustered column, axis `horizon_days` (1/5/10),
  value `var_value`, legend `method`.
- **Rolling 60-day VaR:** line chart, axis `backtesting_results[date]`, value
  `var_estimate` (already a rolling HS-VaR from the walk-forward backtest).
- **Correlation heatmap:** build a correlation table from `market_data` returns
  (Power Query → pivot), or use a matrix with conditional background colour. A
  Python visual using `seaborn.heatmap` on `market_data` is the simplest.

### Page 3 — Greeks Monitor
- **Table:** `greeks` columns delta/gamma/vega/theta/rho by `instrument_id`.
- **Bar (delta-equivalent):** axis `instrument_id`, value `delta` (or a
  `Delta Equivalent` measure = `delta * underlying spot`).
- **Gamma/Vega surface:** scatter with X = strike, Y = maturity, size = gamma
  (pull strike/maturity from `positions`/config; for two options use a small
  table). A 3-D surface needs a Python/`matplotlib` visual.

### Page 4 — Fixed Income Risk
- **Duration ladder:** clustered bar, axis = tenor bucket, value = bucket DV01.
  (Bucket DV01 is computed in `fixed_income_risk.bucket_dv01`; persist it to a
  small helper table or load via a Python visual if you want it live.)
- **Scatter (ModDur vs Convexity):** X `duration_metrics[modified_duration]`,
  Y `convexity`, legend/details `instrument_id`.
- **Rate-shock table:** matrix of shift_bps vs P&L (from `yield_curve_shift`).

### Page 5 — Credit Risk
- **Bar (EL by counterparty):** axis `counterparty_id`, value `expected_loss`.
- **Bubble scatter:** X `pd_estimate`, Y `lgd`, size `ead`, details
  `counterparty_id`.
- **Donut (concentration):** legend `counterparty_id`, values `ead`.
- **Gauge:** `Total Credit VaR`.

### Page 6 — Stress Testing
- **Waterfall:** category `stress_test_results[scenario_name]` (filter
  `asset_class="Total"`), Y `pnl_impact`.
- **Heatmap (scenario x asset class):** matrix rows `scenario_name`, columns
  `asset_class` (exclude "Total"), values `pnl_impact`, conditional background
  colour (red negative / green positive).

### Page 7 — Backtesting
- **Line + scatter:** axis `backtesting_results[date]`; line `actual_pnl` and a
  second line `-var_estimate` (negative so it sits below as the loss threshold);
  overlay scatter of exception days (filter `exception_flag = 1`) coloured red.
- **Card / KPI:** `Traffic Light Zone` (conditional colour Green/Yellow/Red).
- **Comparison:** two cards — `Exception Rate` vs constant `0.01` (1% theoretical
  for 99% VaR).

---

## E. Refreshing after a new risk run

1. Re-run `python src/main.py` (rebuilds the DB, appends a new timestamped run).
2. In Power BI: **Home → Refresh**. Filter visuals to the latest
   `calculation_date` with a relative/Top-1 date filter so the dashboard always
   shows the most recent run while older runs remain for audit history.
