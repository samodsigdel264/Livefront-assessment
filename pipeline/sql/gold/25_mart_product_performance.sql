CREATE OR REPLACE TABLE gold.mart_product_monthly AS
WITH m AS (
    SELECT CAST(date_trunc('month', f.purchase_date) AS DATE) AS month_start, f.product_id, f.category,
           count(*) AS purchases, sum(f.quantity) AS units, round(sum(f.revenue_usd), 2) AS revenue_usd,
           count(DISTINCT f.user_id) AS buyers,
           round(avg(CASE WHEN f.is_promo THEN 1.0 ELSE 0.0 END), 4) AS promo_share
    FROM gold.fct_purchases f GROUP BY 1, 2, 3
)
SELECT m.month_start, m.product_id, p.product_name, m.category,
       m.purchases, m.units, m.revenue_usd, m.buyers, m.promo_share,
       rank() OVER (PARTITION BY m.month_start ORDER BY m.revenue_usd DESC)                   AS revenue_rank,
       rank() OVER (PARTITION BY m.month_start, m.category ORDER BY m.revenue_usd DESC)       AS category_rank,
       round(m.revenue_usd / nullif(lag(m.revenue_usd) OVER (PARTITION BY m.product_id ORDER BY m.month_start), 0) - 1, 4) AS mom_revenue_growth
FROM m JOIN gold.dim_product p USING (product_id)
ORDER BY m.month_start, revenue_rank;

CREATE OR REPLACE TABLE gold.mart_category_monthly AS
WITH c AS (
    SELECT CAST(date_trunc('month', purchase_date) AS DATE) AS month_start, category,
           count(*) AS purchases, sum(quantity) AS units, round(sum(revenue_usd), 2) AS revenue_usd,
           count(DISTINCT user_id) AS buyers
    FROM gold.fct_purchases GROUP BY 1, 2
)
SELECT *, round(revenue_usd / sum(revenue_usd) OVER (PARTITION BY month_start), 4) AS revenue_share
FROM c ORDER BY month_start, revenue_usd DESC;

-- Lifetime product table: who buys it again? (repeat = same user bought the product >= 2 times)
CREATE OR REPLACE TABLE gold.mart_product_lifetime AS
WITH per_user AS (
    SELECT product_id, user_id, count(*) AS n FROM gold.fct_purchases GROUP BY 1, 2
)
SELECT p.product_id, p.product_name, p.category, p.list_price_usd,
       sum(n) AS purchases, count(*) AS buyers,
       count(*) FILTER (WHERE n >= 2) AS repeat_buyers,
       round(count(*) FILTER (WHERE n >= 2) * 1.0 / count(*), 4) AS repeat_buyer_rate,
       rank() OVER (ORDER BY sum(n) DESC) AS popularity_rank
FROM per_user JOIN gold.dim_product p USING (product_id)
GROUP BY p.product_id, p.product_name, p.category, p.list_price_usd
ORDER BY popularity_rank;
