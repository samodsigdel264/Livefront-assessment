-- Calendar spanning the observed activity window (purchases + referral invites + chat messages).
CREATE OR REPLACE TABLE gold.dim_date AS
WITH bounds AS (
    SELECT
        least(
            (SELECT min(purchase_date) FROM silver.purchases),
            (SELECT min(CAST(sent_at AS DATE)) FROM silver.referrals),
            (SELECT min(sent_date) FROM silver.chat_messages)
        ) AS d0,
        greatest(
            (SELECT max(purchase_date) FROM silver.purchases),
            (SELECT max(CAST(sent_at AS DATE)) FROM silver.referrals),
            (SELECT max(sent_date) FROM silver.chat_messages)
        ) AS d1
)
SELECT
    CAST(d AS DATE)                                  AS date_day,
    CAST(date_trunc('week', d) AS DATE)              AS week_start,       -- Monday
    CAST(date_trunc('month', d) AS DATE)             AS month_start,
    year(d)                                          AS year,
    month(d)                                         AS month,
    (dayofweek(d) + 6) % 7                           AS day_of_week,      -- 0 = Monday
    (dayofweek(d) + 6) % 7 >= 5                      AS is_weekend
FROM bounds, generate_series(d0, d1, INTERVAL 1 DAY) AS t(d);
