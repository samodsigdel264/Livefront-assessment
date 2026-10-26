-- When do people buy? (weekday x hour heat-map)
CREATE OR REPLACE TABLE gold.mart_purchase_heatmap AS
SELECT day_of_week, hour_of_day, count(*) AS purchases, sum(quantity) AS units,
       round(sum(revenue_usd), 2) AS revenue_usd,
       round(count(*) * 1.0 / sum(count(*)) OVER (), 5) AS share_of_purchases
FROM gold.fct_purchases GROUP BY 1, 2 ORDER BY 1, 2;

-- Who buys? User segment x user type
CREATE OR REPLACE TABLE gold.mart_user_segments AS
SELECT user_type, purchase_segment,
       count(*) AS users,
       round(count(*) * 1.0 / sum(count(*)) OVER (PARTITION BY user_type), 4) AS share_within_type,
       round(avg(n_purchases), 2) AS avg_purchases,
       round(avg(revenue_usd), 2) AS avg_revenue_usd,
       round(avg(active_days), 2) AS avg_active_days
FROM gold.dim_user GROUP BY 1, 2 ORDER BY 1, 2;

-- Power-law check: how concentrated is purchasing? decile 1 = heaviest 10% of *purchasing* users
CREATE OR REPLACE TABLE gold.mart_purchase_concentration AS
WITH ranked AS (
    SELECT user_id, n_purchases, revenue_usd,
           ntile(10) OVER (ORDER BY n_purchases DESC, user_id) AS decile
    FROM gold.dim_user WHERE n_purchases > 0
)
SELECT decile, count(*) AS users, sum(n_purchases) AS purchases, round(sum(revenue_usd), 2) AS revenue_usd,
       round(sum(n_purchases) * 1.0 / sum(sum(n_purchases)) OVER (), 4) AS share_of_purchases,
       round(sum(sum(n_purchases)) OVER (ORDER BY decile) * 1.0 / sum(sum(n_purchases)) OVER (), 4) AS cumulative_share
FROM ranked GROUP BY decile ORDER BY decile;

-- Category preference by user type (units share within type)
CREATE OR REPLACE TABLE gold.mart_category_affinity AS
SELECT u.user_type, f.category, sum(f.quantity) AS units, round(sum(f.revenue_usd), 2) AS revenue_usd,
       round(sum(f.quantity) * 1.0 / sum(sum(f.quantity)) OVER (PARTITION BY u.user_type), 4) AS unit_share_within_type
FROM gold.fct_purchases f JOIN gold.dim_user u USING (user_id)
GROUP BY 1, 2 ORDER BY 1, units DESC;
