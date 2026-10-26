CREATE OR REPLACE TABLE gold.dim_school AS
SELECT school_id, name AS school_name, city, state, zip_code, created_at AS school_created_at
FROM silver.schools;
