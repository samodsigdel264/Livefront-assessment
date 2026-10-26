"""
Post-build reconciliation: checks that gold agrees with silver and with itself.

Silver DQ (entities.py) answers "is the input sane?". These answer "did we compute the outputs
correctly?" - the class of bug (fan-out joins, lost partitions, double counting) that row-level
checks can't see. Results land in meta.dq_results with entity='gold' next to the input checks.
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import duckdb

log = logging.getLogger("pipeline.quality")

# (name, SQL returning the number of violations / absolute discrepancy; 0 = pass)
CHECKS: List[Tuple[str, str]] = [
    ("fct_purchases_row_count_matches_silver",
     "SELECT abs((SELECT count(*) FROM gold.fct_purchases) - (SELECT count(*) FROM silver.purchases))"),
    ("fct_purchases_revenue_matches_silver",
     "SELECT CAST(abs((SELECT coalesce(sum(revenue_usd),0) FROM gold.fct_purchases) - "
     "(SELECT coalesce(sum(price_paid),0) FROM silver.purchases)) > 0.005 AS INTEGER)"),
    ("daily_school_agg_revenue_matches_fact",
     "SELECT CAST(abs((SELECT coalesce(sum(revenue_usd),0) FROM gold.agg_daily_active_users_by_school) - "
     "(SELECT coalesce(sum(revenue_usd),0) FROM gold.fct_purchases)) > 0.05 AS INTEGER)"),
    ("school_fundraising_donations_match_fact",
     "SELECT CAST(abs((SELECT coalesce(sum(purchase_donations_usd),0) FROM gold.mart_school_fundraising) - "
     "(SELECT coalesce(sum(donation_usd),0) FROM gold.fct_purchases)) > 0.5 AS INTEGER)"),
    ("school_fundraising_ranks_are_dense_from_1",
     "SELECT CAST(min(overall_rank) <> 1 AS INTEGER) FROM gold.mart_school_fundraising"),
    ("referral_fact_matches_silver",
     "SELECT abs((SELECT count(*) FROM gold.fct_referrals) - (SELECT count(*) FROM silver.referrals))"),
    ("referral_funnel_is_monotonic",
     "SELECT count(*) FROM gold.mart_referral_funnel WHERE pct_of_previous_stage > 1"),
    ("cohort_retention_within_0_1",
     "SELECT count(*) FROM gold.mart_cohort_retention_weekly WHERE retention_rate < 0 OR retention_rate > 1"),
    ("cohort_sizes_sum_to_users",
     "SELECT abs((SELECT sum(cohort_size) FROM (SELECT DISTINCT cohort_type, cohort_period, cohort_size "
     "FROM gold.mart_cohort_retention_weekly)) - (SELECT count(*) FROM gold.dim_user))"),
    ("every_purchase_has_a_school",
     "SELECT count(*) FROM gold.fct_purchases WHERE school_id IS NULL"),
    ("activity_fact_purchases_match_fct_purchases",
     "SELECT abs((SELECT coalesce(sum(purchases),0) FROM gold.fct_user_daily_activity) - "
     "(SELECT count(*) FROM gold.fct_purchases))"),
    ("activity_fact_messages_match_silver_chat",
     "SELECT abs((SELECT coalesce(sum(user_messages + bot_messages),0) FROM gold.fct_user_daily_activity) - "
     "(SELECT count(*) FROM silver.chat_messages))"),
    ("dau_never_exceeds_registered_users",
     "SELECT count(*) FROM gold.mart_daily_platform_activity "
     "WHERE dau > (SELECT count(*) FROM gold.dim_user)"),
]


def run_reconciliation(con: duckdb.DuckDBPyConnection, run_id: str) -> List[Tuple[str, int]]:
    failures = []
    for name, sql in CHECKS:
        try:
            bad = int(con.execute(sql).fetchone()[0] or 0)
        except duckdb.Error as exc:  # a missing table means gold didn't build: surface it, don't hide it
            log.error("reconciliation %s errored: %s", name, exc)
            bad = 1
        con.execute(
            "INSERT INTO meta.dq_results (run_id, entity, check_name, severity, failed_rows, total_rows, status) "
            "VALUES (?, 'gold', ?, 'error', ?, NULL, ?)", [run_id, name, bad, "pass" if bad == 0 else "fail"])
        if bad:
            failures.append((name, bad))
            log.error("RECONCILIATION FAILED: %s (%s)", name, bad)
    return failures
