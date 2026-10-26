-- Funnel as stages (overall) ...
CREATE OR REPLACE TABLE gold.mart_referral_funnel AS
WITH t AS (
    SELECT count(*) FILTER (WHERE NOT is_existing_member)                     AS sent_eligible,
           count(*) FILTER (WHERE is_clicked AND NOT is_existing_member)      AS clicked,
           count(*) FILTER (WHERE is_converted)                               AS converted,
           count(*) FILTER (WHERE is_qualified)                               AS qualified
    FROM gold.fct_referrals
),
stages AS (
    SELECT 1 AS stage_order, 'invite sent (eligible)' AS stage, sent_eligible AS invites FROM t UNION ALL
    SELECT 2, 'link clicked',            clicked   FROM t UNION ALL
    SELECT 3, 'signed up (converted)',   converted FROM t UNION ALL
    SELECT 4, 'qualified (first purchase within {{QUALIFY_WINDOW_DAYS}}d)', qualified FROM t
)
SELECT stage_order, stage, invites,
       round(invites * 1.0 / first_value(invites) OVER (ORDER BY stage_order), 4)       AS pct_of_sent,
       round(invites * 1.0 / nullif(lag(invites) OVER (ORDER BY stage_order), 0), 4)    AS pct_of_previous_stage
FROM stages ORDER BY stage_order;

-- ... and by month the invite was sent, with cost and a simple K-factor.
CREATE OR REPLACE TABLE gold.mart_referral_funnel_monthly AS
WITH f AS (
    SELECT sent_month,
           count(*)                                                      AS invites_total,
           count(*) FILTER (WHERE is_existing_member)                    AS invites_to_existing_members,
           count(*) FILTER (WHERE NOT is_existing_member)                AS invites_sent,
           count(*) FILTER (WHERE is_clicked AND NOT is_existing_member) AS clicked,
           count(*) FILTER (WHERE is_converted)                          AS converted,
           count(*) FILTER (WHERE is_qualified)                          AS qualified,
           round(median(hours_to_convert), 1)                            AS median_hours_to_convert,
           count(DISTINCT referrer_user_id)                              AS active_referrers
    FROM gold.fct_referrals GROUP BY sent_month
)
SELECT f.*,
       round(clicked * 1.0 / nullif(invites_sent, 0), 4)    AS click_rate,
       round(converted * 1.0 / nullif(clicked, 0), 4)       AS click_to_signup_rate,
       round(converted * 1.0 / nullif(invites_sent, 0), 4)  AS conversion_rate,
       round(qualified * 1.0 / nullif(converted, 0), 4)     AS qualification_rate,
       qualified * 2 * {{REFERRAL_BONUS_USD}}               AS bonus_cost_usd,   -- referrer + referred, both $ bonus
       round(invites_sent * 1.0 / (SELECT count(*) FROM gold.dim_user u WHERE u.signup_date < f.sent_month), 4) AS invites_per_registered_user,
       -- K = invites per existing user x conversion rate = new users generated per existing user
       round((invites_sent * 1.0 / (SELECT count(*) FROM gold.dim_user u WHERE u.signup_date < f.sent_month))
             * (converted * 1.0 / nullif(invites_sent, 0)), 4) AS k_factor
FROM f ORDER BY sent_month;

-- Does the program bring *better* users, or just more? Window signups only, referred vs organic.
-- Only users with >= 14 days of observation are included, otherwise late signups (who simply haven't had
-- time to buy yet) would drag both groups down in proportion to when they joined (right-censoring).
CREATE OR REPLACE TABLE gold.mart_referral_effectiveness AS
SELECT CASE WHEN is_referred THEN 'referred' ELSE 'organic' END AS acquisition,
       count(*)                                                  AS users,
       count(*) FILTER (WHERE n_purchases > 0)                   AS activated_users,
       round(count(*) FILTER (WHERE n_purchases > 0) * 1.0 / count(*), 4) AS activation_rate,
       round(count(*) FILTER (WHERE n_purchases >= 2) * 1.0 / count(*), 4) AS repeat_rate,
       round(avg(days_to_first_purchase), 2)                     AS avg_days_to_first_purchase,
       round(avg(n_purchases), 2)                                AS avg_purchases_per_user,
       round(avg(revenue_usd), 2)                                AS avg_revenue_per_user
FROM gold.dim_user
WHERE NOT is_legacy
  AND signup_date <= (SELECT max(date_day) FROM gold.dim_date) - 14
GROUP BY is_referred ORDER BY acquisition DESC;
