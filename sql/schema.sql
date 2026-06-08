-- ===========================================================================
-- schema.sql  —  Integrated Risk Dashboard
-- ---------------------------------------------------------------------------
-- Complete relational schema for a multi-asset trading-book market-risk system.
-- Dialect: SQLite (portable, file-based). All risk-result tables carry a
-- timestamp / calculation_date for auditability — regulators (RBI, Basel)
-- require that every reported number be reproducible and time-stamped.
--
-- Layer 1 of the three-layer architecture:
--   positions / market_data / risk_factors  -> raw + reference data
--   var_results / greeks / duration_metrics / credit_metrics /
--   stress_test_results / backtesting_results -> computed risk metrics
-- ===========================================================================

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- POSITIONS: the static book. One row per instrument held.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS positions (
    instrument_id   TEXT PRIMARY KEY,
    asset_class     TEXT    NOT NULL,   -- Equity / FixedIncome / Option / Swap / FX
    description     TEXT    NOT NULL,
    notional        REAL    NOT NULL,   -- INR notional / market value at entry
    quantity        REAL    NOT NULL,
    entry_price     REAL    NOT NULL,
    entry_date      TEXT    NOT NULL    -- ISO date
);

-- ---------------------------------------------------------------------------
-- MARKET_DATA: daily time series per instrument (synthetic GBM history).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS market_data (
    instrument_id   TEXT NOT NULL,
    date            TEXT NOT NULL,       -- ISO date
    price           REAL NOT NULL,
    volume          REAL,
    log_return      REAL,                -- daily log return ln(P_t / P_t-1)
    PRIMARY KEY (instrument_id, date),
    FOREIGN KEY (instrument_id) REFERENCES positions (instrument_id)
);

-- ---------------------------------------------------------------------------
-- RISK_FACTORS: the underlying risk drivers (equity levels, rates, fx, vol).
-- Market risk is modelled on factors, not just instrument prices.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS risk_factors (
    factor_id       TEXT NOT NULL,
    factor_name     TEXT NOT NULL,
    factor_type     TEXT NOT NULL,       -- equity / rate / fx / vol
    date            TEXT NOT NULL,
    value           REAL NOT NULL,
    PRIMARY KEY (factor_id, date)
);

-- ---------------------------------------------------------------------------
-- VAR_RESULTS: Value-at-Risk and Expected Shortfall outputs.
-- One row per (method, confidence, horizon[, instrument]) per run.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS var_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    calculation_date  TEXT NOT NULL,     -- timestamp of the run
    method            TEXT NOT NULL,     -- Historical / Parametric / MonteCarlo
    confidence_level  REAL NOT NULL,     -- 0.95 / 0.975 / 0.99
    horizon_days      INTEGER NOT NULL,  -- 1 / 5 / 10
    var_value         REAL NOT NULL,     -- VaR (positive = loss magnitude, INR)
    es_value          REAL,              -- Expected Shortfall (CVaR), INR
    portfolio_var     REAL,              -- total portfolio VaR for the run
    instrument_id     TEXT,              -- NULL for portfolio-level rows
    metric_type       TEXT               -- Portfolio / Component / Incremental
);

-- ---------------------------------------------------------------------------
-- GREEKS: option sensitivities (Black-Scholes-Merton).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS greeks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id     TEXT NOT NULL,
    calculation_date  TEXT NOT NULL,
    delta             REAL,
    gamma             REAL,
    vega              REAL,
    theta             REAL,
    rho               REAL,
    FOREIGN KEY (instrument_id) REFERENCES positions (instrument_id)
);

-- ---------------------------------------------------------------------------
-- DURATION_METRICS: interest-rate sensitivity of fixed-income instruments.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS duration_metrics (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id       TEXT NOT NULL,
    calculation_date    TEXT NOT NULL,
    modified_duration   REAL,
    macaulay_duration   REAL,
    dv01                REAL,            -- INR change per 1bp move
    convexity           REAL,
    FOREIGN KEY (instrument_id) REFERENCES positions (instrument_id)
);

-- ---------------------------------------------------------------------------
-- CREDIT_METRICS: Basel IRB credit risk per counterparty.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS credit_metrics (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    counterparty_id   TEXT NOT NULL,
    calculation_date  TEXT NOT NULL,
    pd_estimate       REAL,              -- 1y probability of default
    lgd               REAL,              -- loss given default
    ead               REAL,              -- exposure at default (INR)
    expected_loss     REAL,              -- PD * LGD * EAD
    credit_var        REAL               -- Vasicek unexpected loss (INR)
);

-- ---------------------------------------------------------------------------
-- STRESS_TEST_RESULTS: scenario P&L and VaR impacts.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stress_test_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    scenario_name     TEXT NOT NULL,
    calculation_date  TEXT NOT NULL,
    pnl_impact        REAL,              -- total P&L under scenario (INR)
    var_impact        REAL,              -- change in VaR under scenario (INR)
    asset_class       TEXT,              -- NULL = total; else per-asset-class
    description       TEXT
);

-- ---------------------------------------------------------------------------
-- BACKTESTING_RESULTS: daily VaR vs realised P&L, exception flagging.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS backtesting_results (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT NOT NULL,
    actual_pnl        REAL NOT NULL,     -- realised daily P&L (INR)
    var_estimate      REAL NOT NULL,     -- 1-day VaR for that day (INR, +ve)
    confidence_level  REAL NOT NULL,
    exception_flag    INTEGER NOT NULL   -- 1 if loss > VaR else 0
);

-- ===========================================================================
-- SAMPLE INSERT DATA (illustrative — the Python pipeline populates the full
-- book and 2-year history programmatically; these rows document the format).
-- ===========================================================================
INSERT OR IGNORE INTO positions
    (instrument_id, asset_class, description, notional, quantity, entry_price, entry_date)
VALUES
    ('EQ_HDFCBANK', 'Equity', 'HDFC Bank Ltd', 99000000.0, 60000, 1650.0, '2024-01-01'),
    ('FI_GSEC_5Y', 'FixedIncome', 'GOI G-Sec 5Y', 50000000.0, 500000, 100.0, '2024-01-01');

INSERT OR IGNORE INTO market_data
    (instrument_id, date, price, volume, log_return)
VALUES
    ('EQ_HDFCBANK', '2024-01-01', 1650.0, 1200000, 0.0),
    ('EQ_HDFCBANK', '2024-01-02', 1662.3, 1185000, 0.007427);

INSERT OR IGNORE INTO risk_factors
    (factor_id, factor_name, factor_type, date, value)
VALUES
    ('RF_EQ_HDFCBANK', 'HDFC Bank price', 'equity', '2024-01-01', 1650.0),
    ('RF_RATE_5Y', 'INR 5Y yield', 'rate', '2024-01-01', 0.0715),
    ('RF_FX_USDINR', 'USD/INR spot', 'fx', '2024-01-01', 83.20);
