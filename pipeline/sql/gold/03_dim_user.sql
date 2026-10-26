-- One row per user. PII (name, e-mail) deliberately stays in silver; gold carries a salted-free hash
-- only so analysts can join to external exports without ever seeing the address.
CREATE OR REPLACE TABLE gold.dim_user AS
WITH obs AS (SELECT min(date_day) AS obs_start FROM gold.dim_date),
p AS (
    SELECT user_id,
           count(*)                       AS n_purchases,
           sum(quantity)                  AS units,
           sum(price_paid)                AS revenue_usd,
           sum(points_earned)             AS points_earned,
           count(DISTINCT purchase_date)  AS active_days,
           min(purchased_at)              AS first_purchase_at,
           max(purchased_at)              AS last_purchase_at
    FROM silver.purchases GROUP BY user_id
),
r AS (   -- the (single) conversion that brought a user in
    SELECT referred_user_id AS user_id, referrer_user_id, referral_id, converted_at
    FROM silver.referrals
    WHERE status = 'converted' AND referred_user_id IS NOT NULL
    QUALIFY row_number() OVER (PARTITION BY referred_user_id ORDER BY converted_at, referral_id) = 1
)
SELECT
    u.user_id,
    sha256(u.email)                                         AS email_hash,
    u.school_id,
    u.user_type,
    u.created_at                                            AS signup_at,
    CAST(u.created_at AS DATE)                              AS signup_date,
    u.created_at < CAST(obs.obs_start AS TIMESTAMP)         AS is_legacy,        -- signed up before the observation window
    r.user_id IS NOT NULL                                   AS is_referred,
    r.referrer_user_id,
    coalesce(p.n_purchases, 0)                              AS n_purchases,
    coalesce(p.units, 0)                                    AS units,
    coalesce(p.revenue_usd, 0)                              AS revenue_usd,
    coalesce(p.points_earned, 0)                            AS points_earned,
    coalesce(p.active_days, 0)                              AS active_days,
    p.first_purchase_at,
    p.last_purchase_at,
    CASE WHEN p.first_purchase_at IS NULL THEN NULL
         ELSE round(date_diff('second', u.created_at, p.first_purchase_at) / 86400.0, 2) END AS days_to_first_purchase,
    CASE WHEN coalesce(p.n_purchases, 0) = 0 THEN 'never_purchased'
         WHEN p.n_purchases = 1              THEN 'one_time'
         WHEN p.n_purchases <= 4             THEN 'occasional'
         ELSE 'frequent' END                                AS purchase_segment
FROM silver.users u
CROSS JOIN obs
LEFT JOIN p USING (user_id)
LEFT JOIN r USING (user_id);
