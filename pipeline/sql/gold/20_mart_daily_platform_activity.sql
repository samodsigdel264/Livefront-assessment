-- Platform-level DAU / WAU / MAU (trailing 7 / 28 days) and stickiness. "Active" = purchased OR chatted
-- (gold.fct_user_daily_activity); the buying / chatting split is carried alongside.
-- The first 27 days are flagged is_warm_up: the trailing windows are truncated at the start of data.
--
-- Rolling distinct counts via an interval sweep instead of a (date x 28-day) range join:
-- a purchase on day d keeps a user "active" for days d .. d+W-1. Per user, overlapping coverage
-- intervals are merged into islands; +1 at an island's first day and -1 the day after its last,
-- then a running sum over the calendar gives exact distinct users per day in O(user-days),
-- not O(days x purchases). (9 s -> <1 s at 1M purchases; identical output, see tests.)
CREATE OR REPLACE TABLE gold.mart_daily_platform_activity AS
WITH ud AS (                               -- distinct user-days
    SELECT DISTINCT user_id, activity_date AS d FROM gold.fct_user_daily_activity
),
gaps AS (
    SELECT user_id, d, d - lag(d) OVER (PARTITION BY user_id ORDER BY d) AS gap FROM ud
),
w7 AS (
    SELECT user_id, d, sum(CASE WHEN gap IS NULL OR gap > 7 THEN 1 ELSE 0 END)
                           OVER (PARTITION BY user_id ORDER BY d) AS island FROM gaps
),
w28 AS (
    SELECT user_id, d, sum(CASE WHEN gap IS NULL OR gap > 28 THEN 1 ELSE 0 END)
                            OVER (PARTITION BY user_id ORDER BY d) AS island FROM gaps
),
ev7 AS (
    SELECT s AS day, 1 AS delta FROM (SELECT min(d) AS s, max(d) + 7 AS e FROM w7 GROUP BY user_id, island)
    UNION ALL
    SELECT e, -1 FROM (SELECT min(d) AS s, max(d) + 7 AS e FROM w7 GROUP BY user_id, island)
),
ev28 AS (
    SELECT s AS day, 1 AS delta FROM (SELECT min(d) AS s, max(d) + 28 AS e FROM w28 GROUP BY user_id, island)
    UNION ALL
    SELECT e, -1 FROM (SELECT min(d) AS s, max(d) + 28 AS e FROM w28 GROUP BY user_id, island)
),
d7  AS (SELECT day, sum(delta) AS delta FROM ev7  GROUP BY day),
d28 AS (SELECT day, sum(delta) AS delta FROM ev28 GROUP BY day),
base AS (
    SELECT dd.date_day AS activity_date,
           coalesce(dau.n, 0)                                                         AS dau,
           sum(coalesce(d7.delta, 0))  OVER (ORDER BY dd.date_day)                    AS wau,
           sum(coalesce(d28.delta, 0)) OVER (ORDER BY dd.date_day)                    AS mau_28d
    FROM gold.dim_date dd
    LEFT JOIN (SELECT d, count(*) AS n FROM ud GROUP BY d) dau ON dau.d = dd.date_day
    LEFT JOIN d7  ON d7.day  = dd.date_day
    LEFT JOIN d28 ON d28.day = dd.date_day
),
day_totals AS (
    SELECT activity_date,
           sum(purchases) AS purchases, sum(units) AS units, round(sum(revenue_usd), 2) AS revenue_usd,
           count(*) FILTER (WHERE did_purchase) AS buying_users,
           count(*) FILTER (WHERE did_chat)     AS chatting_users,
           sum(user_messages) AS user_messages, sum(chat_sessions) AS chat_sessions
    FROM gold.fct_user_daily_activity GROUP BY 1
),
signups AS (SELECT signup_date AS activity_date, count(*) AS new_users FROM gold.dim_user GROUP BY 1)
SELECT b.activity_date,
       dd.day_of_week, dd.is_weekend,
       b.dau, CAST(b.wau AS BIGINT) AS wau, CAST(b.mau_28d AS BIGINT) AS mau_28d,
       round(b.dau * 1.0 / nullif(b.mau_28d, 0), 4)   AS stickiness_dau_mau,
       coalesce(t.buying_users, 0)                    AS buying_users,
       coalesce(t.chatting_users, 0)                  AS chatting_users,
       coalesce(t.user_messages, 0)                   AS user_messages,
       coalesce(t.chat_sessions, 0)                   AS chat_sessions,
       coalesce(t.purchases, 0)                       AS purchases,
       coalesce(t.units, 0)                           AS units,
       coalesce(t.revenue_usd, 0)                     AS revenue_usd,
       coalesce(s.new_users, 0)                       AS new_users,
       b.activity_date < (SELECT min(date_day) FROM gold.dim_date) + 27 AS is_warm_up
FROM base b
JOIN gold.dim_date dd ON dd.date_day = b.activity_date
LEFT JOIN day_totals t ON t.activity_date = b.activity_date
LEFT JOIN signups    s ON s.activity_date = b.activity_date
ORDER BY b.activity_date;
