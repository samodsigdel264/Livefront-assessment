-- incremental: table=gold.fct_user_daily_activity partition=activity_date
-- Grain: one row per (user, day) on which the user did ANYTHING in the app: bought, chatted, or both.
-- This is the single source of truth for "active user" (DAU / WAU / MAU, school DAU, retention of
-- engagement). Message TEXT never reaches gold: only counts, so the chat mart carries no PII.
-- Chat "sessions" = runs of messages separated by < 30 min of silence, counted within the calendar day
-- (a session spanning midnight counts once on each day; documented simplification).
WITH purch AS (
    SELECT user_id, purchase_date AS activity_date,
           count(*)                          AS purchases,
           sum(quantity)                     AS units,
           round(sum(revenue_usd), 2)        AS revenue_usd,
           round(sum(donation_usd), 4)       AS donation_usd
    FROM gold.fct_purchases
    WHERE {{PARTITION_FILTER:purchase_date}}
    GROUP BY 1, 2
),
msgs AS (
    SELECT user_id, sent_date, sender, sent_at, message_id,
           CASE WHEN date_diff('second',
                    lag(sent_at) OVER (PARTITION BY user_id, sent_date ORDER BY sent_at, message_id),
                    sent_at) <= 1800 THEN 0 ELSE 1 END AS new_session
    FROM silver.chat_messages
    WHERE {{PARTITION_FILTER:sent_date}}
),
chat AS (
    SELECT user_id, sent_date AS activity_date,
           count(*) FILTER (WHERE sender = 'user')  AS user_messages,
           count(*) FILTER (WHERE sender = 'bot')   AS bot_messages,
           sum(new_session)                         AS chat_sessions
    FROM msgs GROUP BY 1, 2
)
SELECT
    coalesce(p.user_id, c.user_id)             AS user_id,
    u.school_id,
    coalesce(p.activity_date, c.activity_date) AS activity_date,
    coalesce(p.purchases, 0)                   AS purchases,
    coalesce(p.units, 0)                       AS units,
    coalesce(p.revenue_usd, 0)                 AS revenue_usd,
    coalesce(p.donation_usd, 0)                AS donation_usd,
    coalesce(c.user_messages, 0)               AS user_messages,
    coalesce(c.bot_messages, 0)                AS bot_messages,
    coalesce(c.chat_sessions, 0)               AS chat_sessions,
    p.user_id IS NOT NULL                      AS did_purchase,
    c.user_id IS NOT NULL                      AS did_chat
FROM purch p
FULL OUTER JOIN chat c ON c.user_id = p.user_id AND c.activity_date = p.activity_date
JOIN silver.users u ON u.user_id = coalesce(p.user_id, c.user_id)
