CREATE OR REPLACE TABLE gold.dim_product AS
SELECT product_id, name AS product_name, category, price AS list_price_usd, points_per_dollar,
       created_at AS product_created_at
FROM silver.products;
