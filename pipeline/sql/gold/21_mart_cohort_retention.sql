-- Cohort retention at grain {{GRAIN}}.
-- cohort_type 'new'    = users who signed up inside the observation window, cohorted by signup {{GRAIN}}
-- cohort_type 'legacy' = users who signed up earlier; all share one cohort anchored at the first {{GRAIN}} of data
-- Retention = share of the cohort with >= 1 purchase in period N after the cohort period.
-- Denominator is every registered user, so period 0 also shows activation (<100%).
CREATE OR REPLACE TABLE gold.{{TABLE}} AS
WITH obs AS (
    SELECT CAST(date_trunc('{{GRAIN}}', min(date_day)) AS DATE) AS first_period,
           CAST(date_trunc('{{GRAIN}}', max(date_day)) AS DATE) AS last_period,
           max(date_day) AS last_day
    FROM gold.dim_date
),
u AS (
    SELECT user_id,
           CASE WHEN is_legacy THEN 'legacy' ELSE 'new' END AS cohort_type,
           CASE WHEN is_legacy THEN (SELECT first_period FROM obs)
                ELSE CAST(date_trunc('{{GRAIN}}', signup_date) AS DATE) END AS cohort_period
    FROM gold.dim_user
),
activity AS (
    SELECT DISTINCT user_id, CAST(date_trunc('{{GRAIN}}', purchase_date) AS DATE) AS activity_period
    FROM gold.fct_purchases
),
sizes AS (SELECT cohort_type, cohort_period, count(*) AS cohort_size FROM u GROUP BY 1, 2),
grid AS (
    SELECT s.cohort_type, s.cohort_period, s.cohort_size,
           unnest(range(0, date_diff('{{GRAIN}}', s.cohort_period, o.last_period) + 1)) AS period_number
    FROM sizes s CROSS JOIN obs o
),
act AS (
    SELECT u.cohort_type, u.cohort_period,
           date_diff('{{GRAIN}}', u.cohort_period, a.activity_period) AS period_number,
           count(DISTINCT a.user_id) AS active_users
    FROM u JOIN activity a USING (user_id)
    WHERE a.activity_period >= u.cohort_period
    GROUP BY 1, 2, 3
)
SELECT g.cohort_type, g.cohort_period, g.period_number, g.cohort_size,
       coalesce(a.active_users, 0) AS active_users,
       round(coalesce(a.active_users, 0) * 1.0 / g.cohort_size, 4) AS retention_rate,
       -- the latest period is usually partial; flag it so dashboards can grey it out
       CAST(CASE '{{GRAIN}}'
                WHEN 'week' THEN g.cohort_period + to_days(CAST(g.period_number * 7 + 6 AS INTEGER))
                ELSE g.cohort_period + to_months(CAST(g.period_number + 1 AS INTEGER)) - INTERVAL 1 DAY END AS DATE)
            <= (SELECT last_day FROM obs) AS is_complete_period
FROM grid g
LEFT JOIN act a USING (cohort_type, cohort_period, period_number)
ORDER BY g.cohort_type, g.cohort_period, g.period_number;
