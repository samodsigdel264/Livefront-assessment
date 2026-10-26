-- Grain: one row per invite. Funnel flags are cumulative (converted implies clicked implies sent).
CREATE OR REPLACE TABLE gold.fct_referrals AS
WITH first_buy AS (
    SELECT user_id, min(purchased_at) AS first_purchase_at FROM silver.purchases GROUP BY user_id
)
SELECT
    r.referral_id,
    r.referrer_user_id,
    ru.school_id                                              AS referrer_school_id,
    r.referred_user_id,
    nu.school_id                                              AS referred_school_id,
    r.referral_code,
    r.sent_at,
    CAST(r.sent_at AS DATE)                                   AS sent_date,
    CAST(date_trunc('month', r.sent_at) AS DATE)              AS sent_month,
    r.converted_at,
    r.status,
    true                                                      AS is_sent,
    r.status IN ('clicked', 'converted')                      AS is_clicked,
    r.status = 'converted'                                    AS is_converted,
    -- invite to somebody who already had an account when it was sent: ineligible per program rules
    (ex.user_id IS NOT NULL AND ex.created_at < r.sent_at)    AS is_existing_member,
    CASE WHEN r.converted_at IS NULL THEN NULL
         ELSE round(date_diff('second', r.sent_at, r.converted_at) / 3600.0, 2) END AS hours_to_convert,
    fb.first_purchase_at                                      AS referred_first_purchase_at,
    -- "qualifying action" proxy: first purchase within N days of signing up
    (r.status = 'converted' AND fb.first_purchase_at IS NOT NULL
        AND fb.first_purchase_at >= r.converted_at
        AND fb.first_purchase_at <= r.converted_at + INTERVAL ({{QUALIFY_WINDOW_DAYS}}) DAY) AS is_qualified
FROM silver.referrals r
LEFT JOIN silver.users ru ON ru.user_id = r.referrer_user_id
LEFT JOIN silver.users nu ON nu.user_id = r.referred_user_id
LEFT JOIN silver.users ex ON ex.email = r.referred_email
LEFT JOIN first_buy   fb ON fb.user_id = r.referred_user_id;
