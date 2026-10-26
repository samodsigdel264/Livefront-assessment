-- Referral graph. Edges = converted referrals (referrer -> referred). Each referred user has exactly
-- one parent, so the graph is a forest; the depth cap is just a guard against bad data.
CREATE OR REPLACE TABLE gold.mart_referral_network AS
WITH RECURSIVE edges AS (
    SELECT referrer_user_id AS parent, referred_user_id AS child FROM silver.referrals
    WHERE status = 'converted' AND referred_user_id IS NOT NULL
),
tree AS (
    SELECT parent AS root, child, 1 AS depth FROM edges
    UNION
    SELECT t.root, e.child, t.depth + 1 FROM tree t JOIN edges e ON e.parent = t.child WHERE t.depth < 20
),
reach AS (
    SELECT root AS user_id, count(DISTINCT child) AS network_size, max(depth) AS max_depth
    FROM tree GROUP BY root
),
inv AS (
    SELECT referrer_user_id AS user_id,
           count(*) AS invites_sent,
           count(*) FILTER (WHERE is_clicked) AS clicked,
           count(*) FILTER (WHERE is_converted) AS direct_conversions,
           count(*) FILTER (WHERE is_qualified) AS qualified_conversions
    FROM gold.fct_referrals GROUP BY referrer_user_id
)
SELECT i.user_id AS referrer_user_id, u.school_id, u.user_type, u.is_referred AS referrer_was_referred,
       i.invites_sent, i.clicked, i.direct_conversions, i.qualified_conversions,
       round(i.direct_conversions * 1.0 / i.invites_sent, 4) AS conversion_rate,
       coalesce(r.network_size, 0) AS network_size,          -- all descendants, any depth
       coalesce(r.max_depth, 0)    AS max_depth,
       rank() OVER (ORDER BY i.direct_conversions DESC, i.qualified_conversions DESC) AS conversion_rank
FROM inv i
JOIN gold.dim_user u ON u.user_id = i.user_id
LEFT JOIN reach r ON r.user_id = i.user_id
ORDER BY conversion_rank, referrer_user_id;

-- Generation of each user in the referral forest: 0 = not referred but referred others (a seed),
-- 1 = referred by a seed, 2 = referred by a generation-1 user, ...
CREATE OR REPLACE TABLE gold.mart_referral_generations AS
WITH RECURSIVE edges AS (
    SELECT referrer_user_id AS parent, referred_user_id AS child FROM silver.referrals
    WHERE status = 'converted' AND referred_user_id IS NOT NULL
),
gen AS (
    SELECT DISTINCT parent AS user_id, 0 AS generation FROM edges WHERE parent NOT IN (SELECT child FROM edges)
    UNION ALL
    SELECT e.child, g.generation + 1 FROM gen g JOIN edges e ON e.parent = g.user_id WHERE g.generation < 20
)
SELECT generation, count(*) AS users,
       round(count(*) * 1.0 / sum(count(*)) OVER (), 4) AS share_of_forest
FROM gen GROUP BY generation ORDER BY generation;
