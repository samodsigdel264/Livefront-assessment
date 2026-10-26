-- incremental: table=gold.agg_daily_active_users_by_school partition=activity_date
-- "Active" = did anything in the app that day (purchase and/or chatbot message); the split columns
-- show how much of the activity is transacting vs. conversational.
SELECT
    a.activity_date,
    a.school_id,
    count(DISTINCT a.user_id)                               AS dau,
    count(DISTINCT a.user_id) FILTER (WHERE a.did_purchase) AS purchasing_users,
    count(DISTINCT a.user_id) FILTER (WHERE a.did_chat)     AS chatting_users,
    sum(a.purchases)                                        AS purchases,
    sum(a.units)                                            AS units,
    round(sum(a.revenue_usd), 2)                            AS revenue_usd,
    round(sum(a.donation_usd), 4)                           AS donation_usd,
    sum(a.user_messages)                                    AS user_messages
FROM gold.fct_user_daily_activity a
WHERE {{PARTITION_FILTER:a.activity_date}}
GROUP BY a.activity_date, a.school_id
