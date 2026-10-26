-- School fundraising: purchase donations (points x point value) + referral bonuses on qualified
-- referrals (both the referrer's and the referred user's school are credited, per rules PDF s3).
CREATE OR REPLACE TABLE gold.mart_school_fundraising AS
WITH sales AS (
    SELECT school_id,
           count(*) AS purchases, count(DISTINCT user_id) AS purchasing_users, sum(quantity) AS units,
           sum(revenue_usd) AS revenue_usd, sum(points_earned) AS points, sum(donation_usd) AS purchase_donations_usd
    FROM gold.fct_purchases GROUP BY school_id
),
members AS (
    SELECT school_id, count(*) AS registered_users,
           count(*) FILTER (WHERE is_referred) AS referred_users
    FROM gold.dim_user GROUP BY school_id
),
bonus AS (
    SELECT school_id, sum(amount) AS referral_bonus_usd, count(*) AS bonus_credits FROM (
        SELECT referrer_school_id AS school_id, {{REFERRAL_BONUS_USD}} AS amount FROM gold.fct_referrals WHERE is_qualified
        UNION ALL
        SELECT referred_school_id,              {{REFERRAL_BONUS_USD}}           FROM gold.fct_referrals WHERE is_qualified
    ) GROUP BY school_id
),
joined AS (
    SELECT s.school_id, s.school_name, s.state, s.city,
           coalesce(m.registered_users, 0)           AS registered_users,
           coalesce(m.referred_users, 0)             AS referred_users,
           coalesce(a.purchasing_users, 0)           AS purchasing_users,
           coalesce(a.purchases, 0)                  AS purchases,
           coalesce(a.units, 0)                      AS units,
           round(coalesce(a.revenue_usd, 0), 2)      AS revenue_usd,
           coalesce(a.points, 0)                     AS points,
           round(coalesce(a.purchase_donations_usd, 0), 2) AS purchase_donations_usd,
           round(coalesce(b.referral_bonus_usd, 0), 2)     AS referral_bonus_usd
    FROM gold.dim_school s
    LEFT JOIN members m USING (school_id)
    LEFT JOIN sales   a USING (school_id)
    LEFT JOIN bonus   b USING (school_id)
)
SELECT *,
       round(purchase_donations_usd + referral_bonus_usd, 2)                                  AS total_raised_usd,
       round((purchase_donations_usd + referral_bonus_usd) / nullif(registered_users, 0), 2)  AS raised_per_registered_user,
       round(purchasing_users * 1.0 / nullif(registered_users, 0), 4)                         AS activation_rate,
       rank() OVER (ORDER BY purchase_donations_usd + referral_bonus_usd DESC)                AS overall_rank,
       rank() OVER (PARTITION BY state ORDER BY purchase_donations_usd + referral_bonus_usd DESC) AS state_rank,
       ntile(4) OVER (ORDER BY purchase_donations_usd + referral_bonus_usd DESC, school_id)           AS quartile   -- 1 = top
FROM joined
ORDER BY overall_rank, school_id;

-- Same, by month, with month-over-month movement.
CREATE OR REPLACE TABLE gold.mart_school_fundraising_monthly AS
WITH m AS (
    SELECT CAST(date_trunc('month', purchase_date) AS DATE) AS month_start, school_id,
           count(DISTINCT user_id) AS purchasing_users, count(*) AS purchases,
           round(sum(revenue_usd), 2) AS revenue_usd, round(sum(donation_usd), 2) AS purchase_donations_usd
    FROM gold.fct_purchases GROUP BY 1, 2
)
SELECT m.month_start, m.school_id, s.school_name, s.state,
       m.purchasing_users, m.purchases, m.revenue_usd, m.purchase_donations_usd,
       rank() OVER (PARTITION BY m.month_start ORDER BY m.purchase_donations_usd DESC) AS month_rank,
       lag(m.purchase_donations_usd) OVER (PARTITION BY m.school_id ORDER BY m.month_start) AS prev_month_donations_usd,
       round(m.purchase_donations_usd / nullif(lag(m.purchase_donations_usd) OVER (PARTITION BY m.school_id ORDER BY m.month_start), 0) - 1, 4) AS mom_growth
FROM m JOIN gold.dim_school s USING (school_id)
ORDER BY m.month_start, month_rank;
