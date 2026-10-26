-- incremental: table=gold.fct_purchases partition=purchase_date
-- Grain: one row per purchase line. Rebuilt only for the purchase dates touched in this run.
SELECT
    p.purchase_id,
    p.user_id,
    u.school_id,                                   -- school as of load time (Type 1; see ARCHITECTURE.md)
    p.product_id,
    pr.category,
    p.purchase_date,
    p.purchased_at,
    p.day_of_week,
    p.hour_of_day,
    p.quantity,
    p.price_paid                                                    AS revenue_usd,
    greatest(pr.price * p.quantity - p.price_paid, 0)               AS discount_usd,
    p.price_paid < pr.price * p.quantity * 0.999                    AS is_promo,
    coalesce(p.points_earned, round(p.price_paid * pr.points_per_dollar)) AS points_earned,
    coalesce(p.points_earned, round(p.price_paid * pr.points_per_dollar)) * {{POINT_VALUE_USD}} AS donation_usd
FROM silver.purchases p
JOIN silver.users    u  ON u.user_id = p.user_id
JOIN silver.products pr ON pr.product_id = p.product_id
WHERE {{PARTITION_FILTER:p.purchase_date}}
