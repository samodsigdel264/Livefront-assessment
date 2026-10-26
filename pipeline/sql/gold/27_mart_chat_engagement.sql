-- Chatbot engagement and its relationship to buying. Counts only; message text never leaves silver.
-- Correlation, not causation: people who chat are also the people who are engaged to begin with.
CREATE OR REPLACE TABLE gold.mart_chat_engagement AS
WITH per_user AS (
    SELECT user_id,
           sum(user_messages)                                       AS user_messages,
           sum(chat_sessions)                                       AS chat_sessions,
           count(*) FILTER (WHERE did_chat)                         AS chat_days,
           count(*) FILTER (WHERE did_chat AND did_purchase)        AS chat_and_buy_days
    FROM gold.fct_user_daily_activity GROUP BY 1
),
u AS (
    SELECT d.user_id, d.n_purchases, d.revenue_usd, d.school_id,
           coalesce(p.user_messages, 0) AS user_messages, coalesce(p.chat_sessions, 0) AS chat_sessions,
           coalesce(p.chat_days, 0) AS chat_days, coalesce(p.chat_and_buy_days, 0) AS chat_and_buy_days,
           coalesce(p.chat_days, 0) > 0 AS is_chatter
    FROM gold.dim_user d LEFT JOIN per_user p USING (user_id)
)
SELECT
    CASE WHEN is_chatter THEN 'chatted' ELSE 'never_chatted' END        AS segment,
    count(*)                                                            AS users,
    round(count(*) * 1.0 / sum(count(*)) OVER (), 4)                    AS share_of_users,
    count(*) FILTER (WHERE n_purchases > 0)                             AS buyers,
    round(avg(CASE WHEN n_purchases > 0 THEN 1.0 ELSE 0.0 END), 4)      AS buyer_rate,
    round(avg(n_purchases), 2)                                          AS avg_purchases,
    round(avg(revenue_usd), 2)                                          AS avg_revenue_usd,
    round(avg(user_messages), 2)                                        AS avg_user_messages,
    round(avg(chat_sessions), 2)                                        AS avg_chat_sessions,
    round(sum(chat_and_buy_days) * 1.0 / nullif(sum(chat_days), 0), 4)  AS share_of_chat_days_with_purchase
FROM u GROUP BY is_chatter ORDER BY is_chatter DESC;

-- Reach by school: how many of a school's members use the bot (a product-adoption view).
CREATE OR REPLACE TABLE gold.mart_chat_adoption_by_school AS
SELECT s.school_id, s.school_name,
       count(*)                                                        AS members,
       count(*) FILTER (WHERE a.user_id IS NOT NULL)                   AS chatters,
       round(count(*) FILTER (WHERE a.user_id IS NOT NULL) * 1.0 / count(*), 4) AS chat_adoption_rate,
       coalesce(sum(a.msgs), 0)                                        AS user_messages
FROM gold.dim_user d
JOIN gold.dim_school s USING (school_id)
LEFT JOIN (SELECT user_id, sum(user_messages) AS msgs FROM gold.fct_user_daily_activity
           WHERE did_chat GROUP BY 1) a USING (user_id)
GROUP BY s.school_id, s.school_name
ORDER BY chat_adoption_rate DESC, members DESC;
