-- Reporting layer. Raw tables (accounts, actuals, budget, *_drivers) are loaded by fpa/db.py.
-- Sign convention: amounts are stored positive; the P&L line decides whether they add or subtract.

-- Duplicate imports: the same month/account/amount booked more than once.
CREATE VIEW dq_duplicates AS
SELECT month, account, amount, COUNT(*) AS copies
FROM actuals
GROUP BY month, account, amount
HAVING COUNT(*) > 1;

-- Bookings on accounts that are missing from the cost-centre mapping.
CREATE VIEW dq_unmapped AS
SELECT a.month, a.account, SUM(a.amount) AS amount
FROM actuals a
LEFT JOIN accounts m ON m.account = a.account
WHERE m.account IS NULL
GROUP BY a.month, a.account;

-- Clean actuals: duplicates collapsed to one booking, unmapped accounts left out
-- (they are reported separately, never silently dropped).
CREATE VIEW actuals_clean AS
SELECT DISTINCT a.month, a.account, a.amount
FROM actuals a
JOIN accounts m ON m.account = a.account;

-- Plan vs. actual per month and account, with the mapping attached.
CREATE VIEW plan_vs_actual AS
SELECT
    b.month,
    m.account,
    m.name,
    m.pnl_line,
    m.cost_centre,
    m.product,
    COALESCE(act.amount, 0) AS actual,
    b.amount AS budget,
    COALESCE(act.amount, 0) - b.amount AS variance,
    CASE WHEN b.amount = 0 THEN NULL
         ELSE (COALESCE(act.amount, 0) - b.amount) / b.amount END AS variance_pct
FROM budget b
JOIN accounts m ON m.account = b.account
LEFT JOIN (
    SELECT month, account, SUM(amount) AS amount FROM actuals_clean GROUP BY month, account
) act ON act.month = b.month AND act.account = b.account
WHERE b.month IN (SELECT DISTINCT month FROM actuals);

-- Monthly P&L lines with signed totals: Revenue - COGS = Gross margin, minus OpEx = EBITDA.
CREATE VIEW pnl_month AS
SELECT
    month,
    SUM(CASE WHEN pnl_line = 'Revenue' THEN actual ELSE 0 END) AS revenue,
    SUM(CASE WHEN pnl_line = 'COGS' THEN actual ELSE 0 END) AS cogs,
    SUM(CASE WHEN pnl_line = 'OpEx' THEN actual ELSE 0 END) AS opex,
    SUM(CASE WHEN pnl_line = 'Revenue' THEN budget ELSE 0 END) AS revenue_budget,
    SUM(CASE WHEN pnl_line = 'COGS' THEN budget ELSE 0 END) AS cogs_budget,
    SUM(CASE WHEN pnl_line = 'OpEx' THEN budget ELSE 0 END) AS opex_budget
FROM plan_vs_actual
GROUP BY month;
