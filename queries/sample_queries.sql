-- Sample analysis against the gold layer.
--   python scripts/run_sample_queries.py            (runs every query below and prints the results)
--   duckdb warehouse/carton_caps.duckdb < queries/sample_queries.sql
-- Each query starts with "-- Q<n>:" so the runner can label it.

-- Q1: Top 10 schools by total raised (purchase donations + referral bonuses), with how they got there
SELECT overall_rank, school_name, state, registered_users, purchasing_users,
       purchase_donations_usd, referral_bonus_usd, total_raised_usd, raised_per_registered_user
FROM gold.mart_school_fundraising
ORDER BY overall_rank
LIMIT 10;

-- Q2: Best school in each state
SELECT state, school_name, total_raised_usd
FROM gold.mart_school_fundraising
WHERE state_rank = 1
ORDER BY total_raised_usd DESC;

-- Q3: Fundraising momentum - schools that moved up the monthly leaderboard the most (May -> June)
SELECT jun.school_name, may.month_rank AS may_rank, jun.month_rank AS june_rank,
       may.month_rank - jun.month_rank AS places_gained, jun.purchase_donations_usd AS june_donations_usd
FROM gold.mart_school_fundraising_monthly jun
JOIN gold.mart_school_fundraising_monthly may
  ON may.school_id = jun.school_id AND may.month_start = '2024-05-01' AND jun.month_start = '2024-06-01'
ORDER BY places_gained DESC
LIMIT 5;

-- Q4: Referral funnel, overall
SELECT stage, invites, pct_of_sent, pct_of_previous_stage
FROM gold.mart_referral_funnel
ORDER BY stage_order;

-- Q5: Is the referral program growing, and what does it cost? (monthly)
SELECT sent_month, invites_sent, converted, qualified, conversion_rate, qualification_rate,
       median_hours_to_convert, k_factor, bonus_cost_usd,
       round(bonus_cost_usd / nullif(qualified, 0), 2) AS cost_per_qualified_user_usd
FROM gold.mart_referral_funnel_monthly
ORDER BY sent_month;

-- Q6: Do referred users behave differently from organic signups? (mature cohorts only)
SELECT * FROM gold.mart_referral_effectiveness;

-- Q7: Top referrers and how deep their networks go
SELECT conversion_rank, referrer_user_id, user_type, invites_sent, direct_conversions, qualified_conversions,
       conversion_rate, network_size, max_depth
FROM gold.mart_referral_network
ORDER BY conversion_rank
LIMIT 10;

-- Q8: Shape of the referral forest (generation 0 = seeds, 1 = referred by a seed, ...)
SELECT * FROM gold.mart_referral_generations;

-- Q9: Weekly retention curve of users who signed up in the window (average across cohorts, weighted by size)
SELECT period_number AS weeks_since_signup,
       sum(cohort_size)  AS users_observed,
       round(sum(active_users) * 1.0 / sum(cohort_size), 4) AS purchase_retention
FROM gold.mart_cohort_retention_weekly
WHERE cohort_type = 'new' AND is_complete_period
GROUP BY period_number
ORDER BY period_number
LIMIT 12;

-- Q10: Monthly cohort triangle (retention % by months since signup)
SELECT cohort_period AS signup_month,
       max(cohort_size) AS cohort_size,
       round(100 * max(retention_rate) FILTER (WHERE period_number = 0), 0) AS m0_pct,
       round(100 * max(retention_rate) FILTER (WHERE period_number = 1), 0) AS m1_pct,
       round(100 * max(retention_rate) FILTER (WHERE period_number = 2), 0) AS m2_pct,
       round(100 * max(retention_rate) FILTER (WHERE period_number = 3), 0) AS m3_pct,
       round(100 * max(retention_rate) FILTER (WHERE period_number = 4), 0) AS m4_pct,
       round(100 * max(retention_rate) FILTER (WHERE period_number = 5), 0) AS m5_pct
FROM gold.mart_cohort_retention_monthly
WHERE cohort_type = 'new'
GROUP BY cohort_period
ORDER BY cohort_period;

-- Q11: Daily active users by school - the busiest school-days
SELECT a.activity_date, s.school_name, a.dau, a.purchases, a.revenue_usd
FROM gold.agg_daily_active_users_by_school a
JOIN gold.dim_school s USING (school_id)
ORDER BY a.dau DESC, a.activity_date
LIMIT 10;

-- Q12: Platform stickiness (DAU / 28-day MAU) by month, ignoring the warm-up period
SELECT date_trunc('month', activity_date)::DATE AS month,
       round(avg(dau), 1) AS avg_dau, round(avg(mau_28d), 1) AS avg_mau, round(avg(stickiness_dau_mau), 3) AS avg_stickiness
FROM gold.mart_daily_platform_activity
WHERE NOT is_warm_up
GROUP BY 1 ORDER BY 1;

-- Q13: Best-selling products and how loyal their buyers are
SELECT popularity_rank, product_name, category, list_price_usd, purchases, buyers, repeat_buyer_rate
FROM gold.mart_product_lifetime
ORDER BY popularity_rank
LIMIT 10;

-- Q14: Category mix by month
SELECT month_start, category, revenue_usd, revenue_share
FROM gold.mart_category_monthly
ORDER BY month_start, revenue_share DESC;

-- Q15: Which category does each user type favour?
SELECT user_type, category, unit_share_within_type
FROM gold.mart_category_affinity
QUALIFY row_number() OVER (PARTITION BY user_type ORDER BY unit_share_within_type DESC) <= 2
ORDER BY user_type, unit_share_within_type DESC;

-- Q16: How concentrated is purchasing? (share of all purchases made by each decile of buyers)
SELECT decile, users, purchases, share_of_purchases, cumulative_share
FROM gold.mart_purchase_concentration
ORDER BY decile;

-- Q17: When do people buy? Top 8 weekday/hour slots (0 = Monday)
SELECT day_of_week, hour_of_day, purchases, share_of_purchases
FROM gold.mart_purchase_heatmap
ORDER BY purchases DESC
LIMIT 8;

-- Q18: Pipeline health - latest run's data-quality scoreboard (rows that failed a check)
SELECT entity, check_name, severity, failed_rows, total_rows, status
FROM meta.dq_results
WHERE run_id = (SELECT run_id FROM meta.runs WHERE status = 'success' ORDER BY started_at DESC LIMIT 1)
  AND failed_rows > 0
ORDER BY entity, failed_rows DESC;

-- Q19: What is sitting in quarantine, and why?
SELECT 'purchases' AS entity, _dq_reasons AS reasons, _retryable AS will_retry, count(*) AS rows
FROM quarantine.purchases GROUP BY ALL
UNION ALL SELECT 'users', _dq_reasons, _retryable, count(*) FROM quarantine.users GROUP BY ALL
UNION ALL SELECT 'referrals', _dq_reasons, _retryable, count(*) FROM quarantine.referrals GROUP BY ALL
ORDER BY rows DESC;

-- Q20: Active users by month, split by what they did (a user can fall in several columns on different days)
SELECT date_trunc('month', activity_date)::DATE AS month,
       count(DISTINCT user_id)                                       AS active_users,
       count(DISTINCT user_id) FILTER (WHERE did_purchase AND NOT did_chat) AS buy_only,
       count(DISTINCT user_id) FILTER (WHERE did_chat AND NOT did_purchase) AS chat_only,
       count(DISTINCT user_id) FILTER (WHERE did_chat AND did_purchase)     AS both_same_day
FROM gold.fct_user_daily_activity
GROUP BY 1 ORDER BY 1;

-- Q21: Do chatbot users buy more? (a correlation: engaged users do both)
SELECT segment, users, buyer_rate, avg_purchases, avg_revenue_usd, avg_user_messages, share_of_chat_days_with_purchase
FROM gold.mart_chat_engagement;

-- Q22: Schools where the chatbot is most adopted, among schools with 10+ members
SELECT school_name, members, chatters, chat_adoption_rate, user_messages
FROM gold.mart_chat_adoption_by_school
WHERE members >= 10
ORDER BY chat_adoption_rate DESC
LIMIT 8;
