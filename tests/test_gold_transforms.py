"""
Gold layer, checked against a tiny world whose answers were worked out by hand.

Window: 2024-01-01 (Mon) .. 2024-01-25.  Point value $0.01, referral bonus $5 per side, qualify window 7 days.

users                                       purchases (id: user product qty paid pts when)
  1 legacy, school 1 (Alpha)                  1: u1 p1 x1 10.00 20  Jan 01 10:00   (Mon)
  2 legacy, school 1, teacher                 2: u2 p2 x2 10.00 10  Jan 01 18:00
  3 new Jan 02 09:00, school 2, referred by 1 3: u1 p1 x1  8.00 16  Jan 08 10:00   (20% promo)
  4 new Jan 09, school 2, organic             4: u3 p1 x1 10.00 20  Jan 03 12:00   (day 1 after signup -> qualifies)
  5 new Jan 10 08:00, school 1, ref. by 3     5: u3 p2 x1  5.00  5  Jan 09 12:00
                                              6: u4 p2 x1  5.00  5  Jan 25 09:00   (16 days after signup)
products: 1 = $10 (2 pts/$), 2 = $5 (1 pt/$, Dairy)
referrals: 1 u1->u3 converted | 2 u1->x@e.com clicked | 3 u1->y@e.com sent
           4 u3->u5 converted | 5 u2->u1's e-mail (already a member)
"""

import pytest

from conftest import (product, purchase, referral, run_world, school, table, user, write_csv)


@pytest.fixture(scope="module")
def con(tmp_path_factory):
    d = tmp_path_factory.mktemp("world")
    write_csv(d, "schools", [school(1, "Alpha", "TX"), school(2, "Beta", "TX")])
    write_csv(d, "products", [product(1, 10.00, 2, "Snacks"), product(2, 5.00, 1, "Dairy")])
    write_csv(d, "users", [
        user(1, 1, "2023-06-01 00:00:00", "parent"),
        user(2, 1, "2023-06-01 00:00:00", "teacher"),
        user(3, 2, "2024-01-02 09:00:00"),
        user(4, 2, "2024-01-09 09:00:00"),
        user(5, 1, "2024-01-10 08:00:00"),
    ])
    write_csv(d, "purchases", [
        purchase(1, 1, 1, 1, 10.00, 20, "2024-01-01 10:00:00"),
        purchase(2, 2, 2, 2, 10.00, 10, "2024-01-01 18:00:00"),
        purchase(3, 1, 1, 1, 8.00, 16, "2024-01-08 10:00:00"),
        purchase(4, 3, 1, 1, 10.00, 20, "2024-01-03 12:00:00"),
        purchase(5, 3, 2, 1, 5.00, 5, "2024-01-09 12:00:00"),
        purchase(6, 4, 2, 1, 5.00, 5, "2024-01-25 09:00:00"),
    ])
    write_csv(d, "referrals", [
        referral(1, 1, "user3@email.com", 3, "2024-01-01 20:00:00", "2024-01-02 09:00:00", "converted"),
        referral(2, 1, "x@e.com", None, "2024-01-05 10:00:00", None, "clicked"),
        referral(3, 1, "y@e.com", None, "2024-01-06 10:00:00", None, "sent"),
        referral(4, 3, "user5@email.com", 5, "2024-01-09 20:00:00", "2024-01-10 08:00:00", "converted"),
        referral(5, 2, "user1@email.com", None, "2024-01-07 10:00:00", None, "sent"),
    ])
    c, summary = run_world(d)
    assert summary["reconciliation_failures"] == []
    return c


def q(con, sql):
    return table(con, sql)


# ------------------------------------------------------------------------------ facts & dimensions

def test_fact_enrichment(con):
    assert q(con, "SELECT school_id, category, revenue_usd, discount_usd, is_promo, points_earned, donation_usd "
                  "FROM gold.fct_purchases WHERE purchase_id IN (1, 3) ORDER BY purchase_id") == [
        (1, "Snacks", 10.0, 0.0, False, 20, pytest.approx(0.20)),
        (1, "Snacks", 8.0, 2.0, True, 16, pytest.approx(0.16)),
    ]


def test_dim_user_attributes(con):
    rows = {r[0]: r for r in q(con, "SELECT user_id, is_legacy, is_referred, referrer_user_id, n_purchases, purchase_segment, "
                                   "days_to_first_purchase FROM gold.dim_user")}
    assert rows[1][1:4] == (True, False, None) and rows[2][1] is True
    assert rows[3][1:4] == (False, True, 1)
    assert rows[5][1:4] == (False, True, 3)
    assert rows[1][5] == "occasional" and rows[4][5] == "one_time" and rows[5][5] == "never_purchased"
    assert rows[3][6] == pytest.approx(1.13, abs=0.001)         # Jan 02 09:00 -> Jan 03 12:00 = 1.125 d, rounded to 2dp


def test_gold_dimension_has_no_direct_pii(con):
    cols = {r[0] for r in q(con, "SELECT column_name FROM information_schema.columns WHERE table_name='dim_user'")}
    assert not ({"email", "first_name", "last_name"} & cols) and "email_hash" in cols


# ------------------------------------------------------------------------------ activity

def test_daily_active_users_by_school(con):
    assert q(con, "SELECT dau, purchases, units, revenue_usd FROM gold.agg_daily_active_users_by_school "
                  "WHERE activity_date = '2024-01-01' AND school_id = 1") == [(2, 2, 3, 20.0)]
    assert q(con, "SELECT dau FROM gold.agg_daily_active_users_by_school WHERE activity_date='2024-01-03' AND school_id=2") == [(1,)]
    assert q(con, "SELECT count(*) FROM gold.agg_daily_active_users_by_school") == [(5,)]   # Jan 1(s1) 3(s2) 8(s1) 9(s2) 25(s2)


def test_platform_dau_wau_mau(con):
    row = q(con, "SELECT dau, wau, mau_28d, purchases, new_users FROM gold.mart_daily_platform_activity "
                 "WHERE activity_date = '2024-01-08'")[0]
    assert row == (1, 2, 3, 1, 0)        # wau = {u1 (Jan 8), u3 (Jan 3)}; u2's Jan 1 purchase is 7 days back -> out
    assert q(con, "SELECT new_users FROM gold.mart_daily_platform_activity WHERE activity_date = '2024-01-02'") == [(1,)]
    assert q(con, "SELECT dau FROM gold.mart_daily_platform_activity WHERE activity_date = '2024-01-02'") == [(0,)]  # zero-fill


# ------------------------------------------------------------------------------ cohorts

def test_weekly_retention(con):
    def ret(ctype, cohort, n):
        return q(con, f"SELECT cohort_size, active_users, retention_rate FROM gold.mart_cohort_retention_weekly "
                      f"WHERE cohort_type='{ctype}' AND cohort_period='{cohort}' AND period_number={n}")[0]
    assert ret("legacy", "2024-01-01", 0) == (2, 2, 1.0)
    assert ret("legacy", "2024-01-01", 1) == (2, 1, 0.5)
    assert ret("legacy", "2024-01-01", 2) == (2, 0, 0.0)
    assert ret("new", "2024-01-01", 0) == (1, 1, 1.0)         # u3 signs up Jan 2, buys Jan 3
    assert ret("new", "2024-01-01", 1) == (1, 1, 1.0)         # and again Jan 9
    assert ret("new", "2024-01-08", 0) == (2, 0, 0.0)         # u4 + u5, nobody buys in their signup week
    assert ret("new", "2024-01-08", 2) == (2, 1, 0.5)         # u4 buys Jan 25


def test_last_period_is_flagged_incomplete(con):
    flags = dict(q(con, "SELECT period_number, is_complete_period FROM gold.mart_cohort_retention_weekly "
                        "WHERE cohort_type='legacy'"))
    assert flags == {0: True, 1: True, 2: True, 3: False}     # data ends Jan 25, week of Jan 22 is partial


def test_cohorts_cover_every_user_once(con):
    assert q(con, "SELECT sum(cohort_size) FROM (SELECT DISTINCT cohort_type, cohort_period, cohort_size "
                  "FROM gold.mart_cohort_retention_weekly)") == [(5,)]
    assert q(con, "SELECT sum(cohort_size) FROM (SELECT DISTINCT cohort_type, cohort_period, cohort_size "
                  "FROM gold.mart_cohort_retention_monthly)") == [(5,)]


# ------------------------------------------------------------------------------ schools

def test_school_fundraising_and_ranking(con):
    rows = q(con, "SELECT school_name, purchase_donations_usd, referral_bonus_usd, total_raised_usd, overall_rank, "
                  "state_rank, registered_users, purchasing_users FROM gold.mart_school_fundraising ORDER BY overall_rank")
    # Alpha: (20+10+16)=46 pts -> $0.46 ; Beta: (20+5+5)=30 pts -> $0.30.
    # One qualified referral (u1->u3) pays $5 to u1's school (Alpha) and $5 to u3's school (Beta).
    assert rows[0] == ("Alpha", 0.46, 5.0, 5.46, 1, 1, 3, 2)
    assert rows[1] == ("Beta", 0.30, 5.0, 5.30, 2, 2, 2, 2)


def test_school_with_no_activity_still_appears(tmp_path):
    write_csv(tmp_path, "schools", [school(1), school(2, "Quiet")])
    write_csv(tmp_path, "products", [product(1, 10, 1)])
    write_csv(tmp_path, "users", [user(1, 1, "2024-01-01 00:00:00")])
    write_csv(tmp_path, "purchases", [purchase(1, 1, 1, 1, 10.0, 10, "2024-01-02 00:00:00")])
    write_csv(tmp_path, "referrals", [])
    c, _ = run_world(tmp_path)
    assert q(c, "SELECT school_name, total_raised_usd, overall_rank FROM gold.mart_school_fundraising ORDER BY overall_rank") == [
        ("School 1", 0.10, 1), ("Quiet", 0.0, 2)]


def test_ties_share_a_rank(tmp_path):
    write_csv(tmp_path, "schools", [school(1), school(2), school(3)])
    write_csv(tmp_path, "products", [product(1, 10, 1)])
    write_csv(tmp_path, "users", [user(i, i, "2024-01-01 00:00:00") for i in (1, 2, 3)])
    write_csv(tmp_path, "purchases", [purchase(1, 1, 1, 1, 10.0, 10, "2024-01-02 00:00:00"),
                                      purchase(2, 2, 1, 1, 10.0, 10, "2024-01-02 00:00:00"),
                                      purchase(3, 3, 1, 1, 10.0, 5, "2024-01-02 00:00:00")])
    write_csv(tmp_path, "referrals", [])
    c, _ = run_world(tmp_path)
    assert [r[0] for r in q(c, "SELECT overall_rank FROM gold.mart_school_fundraising ORDER BY school_id")] == [1, 1, 3]


# ------------------------------------------------------------------------------ referrals

def test_referral_funnel_stages(con):
    assert q(con, "SELECT stage, invites, pct_of_sent, pct_of_previous_stage FROM gold.mart_referral_funnel ORDER BY stage_order") == [
        ("invite sent (eligible)", 4, 1.0, None),              # 5 invites, one to an existing member
        ("link clicked", 3, 0.75, 0.75),                       # referrals 1, 2, 4
        ("signed up (converted)", 2, 0.5, pytest.approx(0.6667, abs=1e-4)),
        ("qualified (first purchase within 7d)", 1, 0.25, 0.5),  # u3 yes; u5 never bought
    ]


def test_qualification_window_boundaries(con):
    assert q(con, "SELECT referral_id, is_qualified FROM gold.fct_referrals WHERE is_converted ORDER BY 1") == [(1, True), (4, False)]
    assert q(con, "SELECT referral_id FROM gold.fct_referrals WHERE is_existing_member") == [(5,)]
    assert q(con, "SELECT hours_to_convert FROM gold.fct_referrals WHERE referral_id = 1") == [(13.0,)]


def test_qualification_window_is_configurable(tmp_path):
    write_csv(tmp_path, "schools", [school(1)])
    write_csv(tmp_path, "products", [product(1, 10, 1)])
    write_csv(tmp_path, "users", [user(1, 1, "2023-01-01 00:00:00"), user(2, 1, "2024-01-02 00:00:00")])
    write_csv(tmp_path, "purchases", [purchase(1, 2, 1, 1, 10.0, 10, "2024-01-12 00:00:00")])   # 10 days after signup
    write_csv(tmp_path, "referrals", [referral(1, 1, "user2@email.com", 2, "2024-01-01 00:00:00", "2024-01-02 00:00:00", "converted")])
    c7, _ = run_world(tmp_path, qualify_window_days=7)
    c14, _ = run_world(tmp_path, qualify_window_days=14)
    assert q(c7, "SELECT is_qualified FROM gold.fct_referrals") == [(False,)]
    assert q(c14, "SELECT is_qualified FROM gold.fct_referrals") == [(True,)]


def test_monthly_funnel_and_k_factor(con):
    row = q(con, "SELECT invites_sent, clicked, converted, qualified, conversion_rate, bonus_cost_usd, k_factor "
                 "FROM gold.mart_referral_funnel_monthly")[0]
    assert row[:5] == (4, 3, 2, 1, 0.5)
    assert float(row[5]) == 10.0                               # one qualified referral x ($5 + $5)
    # month starts Jan 1: users registered before that = the 2 legacy users -> invites/user = 2, K = 2 * 0.5
    assert row[6] == pytest.approx(1.0)


def test_referral_network_depth_and_generations(con):
    assert q(con, "SELECT referrer_user_id, direct_conversions, network_size, max_depth FROM gold.mart_referral_network "
                  "ORDER BY referrer_user_id") == [(1, 1, 2, 2), (2, 0, 0, 0), (3, 1, 1, 1)]
    assert q(con, "SELECT generation, users FROM gold.mart_referral_generations ORDER BY generation") == [(0, 1), (1, 1), (2, 1)]


def test_referral_effectiveness_only_counts_mature_window_signups(con):
    # Data ends Jan 25, so users who signed up after Jan 11 are excluded; u3 (Jan 2), u4 (Jan 9) and u5 (Jan 10) all
    # have >= 14 days. Referred = u3 (bought) + u5 (never bought); organic = u4 (bought).
    rows = {r[0]: r[1:] for r in q(con, "SELECT acquisition, users, activated_users FROM gold.mart_referral_effectiveness")}
    assert rows == {"referred": (2, 1), "organic": (1, 1)}


# ------------------------------------------------------------------------------ products & patterns

def test_product_monthly_rankings(con):
    rows = q(con, "SELECT product_id, purchases, units, revenue_usd, buyers, revenue_rank, category_rank "
                  "FROM gold.mart_product_monthly ORDER BY revenue_rank")
    assert rows == [(1, 3, 3, 28.0, 2, 1, 1), (2, 3, 4, 20.0, 3, 2, 1)]


def test_category_shares_sum_to_one(con):
    assert q(con, "SELECT round(sum(revenue_share), 4) FROM gold.mart_category_monthly") == [(1.0,)]


def test_repeat_buyer_rate(con):
    assert q(con, "SELECT product_id, buyers, repeat_buyers FROM gold.mart_product_lifetime ORDER BY product_id") == [(1, 2, 1), (2, 3, 0)]


def test_heatmap_uses_monday_zero(con):
    assert q(con, "SELECT purchases FROM gold.mart_purchase_heatmap WHERE day_of_week = 0 AND hour_of_day = 10") == [(2,)]  # Jan 1 + Jan 8


def test_concentration_deciles_cover_everything(con):
    assert q(con, "SELECT sum(purchases), max(cumulative_share) FROM gold.mart_purchase_concentration") == [(6, 1.0)]


# ------------------------------------------------------------------------------ run-level contracts

def test_reconciliation_checks_were_recorded(con):
    n = q(con, "SELECT count(*) FROM meta.dq_results WHERE entity = 'gold' AND status = 'pass'")[0][0]
    assert n >= 10
    assert q(con, "SELECT count(*) FROM meta.dq_results WHERE entity = 'gold' AND status = 'fail'") == [(0,)]


# ------------------------------------------------------------------------------ incremental == full rebuild

def test_incremental_load_equals_from_scratch_build(tmp_path):
    """The key correctness property of the incremental design."""
    d1, d2 = tmp_path / "incremental", tmp_path / "scratch"
    dims = dict(schools=[school(1), school(2)], products=[product(1, 10, 2), product(2, 5, 1, "Dairy")])
    users = [user(1, 1, "2023-01-01 00:00:00"), user(2, 2, "2023-01-01 00:00:00"), user(3, 1, "2024-01-05 00:00:00")]
    first = [purchase(1, 1, 1, 1, 10.0, 20, "2024-01-06 10:00:00"), purchase(2, 2, 2, 1, 5.0, 5, "2024-01-06 11:00:00"),
             purchase(3, 3, 1, 2, 20.0, 40, "2024-01-07 12:00:00")]
    second = [purchase(1, 1, 1, 3, 30.0, 60, "2024-01-09 10:00:00"),        # correction: quantity AND date change
              purchase(4, 2, 2, 1, 5.0, 5, "2024-01-09 09:00:00"),          # new on the same day
              purchase(5, 3, 1, 1, 10.0, 20, "2024-01-20 09:00:00")]        # new, later partition
    refs = [referral(1, 1, "user3@email.com", 3, "2024-01-04 00:00:00", "2024-01-05 00:00:00", "converted")]

    for d in (d1, d2):
        for name, rows in {**dims, "users": users, "referrals": refs}.items():
            write_csv(d, name, rows)
    write_csv(d1, "purchases", first)
    inc, _ = run_world(d1)
    write_csv(d1, "purchases", second, suffix="b2")
    run_world(d1, con=inc)

    write_csv(d2, "purchases", first)
    write_csv(d2, "purchases", second, suffix="b2")
    scratch, _ = run_world(d2)

    tables = [r[0] for r in table(inc, "SELECT table_name FROM information_schema.tables WHERE table_schema='gold' ORDER BY 1")]
    assert len(tables) >= 20
    for t in tables:
        a = table(inc, f"SELECT * FROM gold.{t} ORDER BY ALL")
        b = table(scratch, f"SELECT * FROM gold.{t} ORDER BY ALL")
        assert a == b, f"gold.{t} differs between incremental and from-scratch builds"
    # and the partition logic really was incremental: only the touched dates were rebuilt on the 2nd run
    rebuilt = table(inc, "SELECT value FROM meta.run_metrics WHERE stage='gold' AND entity='10_fct_purchases' "
                         "AND metric='partitions_rebuilt' ORDER BY recorded_at")
    # run 1 builds Jan 6 + Jan 7. Run 2 touches Jan 6 (purchase 1's OLD date), Jan 9 (its new date + purchase 4)
    # and Jan 20 (purchase 5): 3 partitions, Jan 7 is left alone.
    assert [int(r[0]) for r in rebuilt] == [2, 3]


def test_rolling_active_users_match_brute_force_definition(tmp_path):
    """The O(user-days) interval sweep must equal the literal definition (purchase OR chat) on realistic data."""
    import data_generator
    g = data_generator.CartonCapsDataGenerator(seed=11, output_dir=str(tmp_path))
    g.generate_all(n_schools=8, n_users=300, n_products=25, n_purchases=2500, n_referrals=150)
    c, _ = run_world(tmp_path)
    bad = q(c, """
        WITH ev AS (SELECT user_id, purchase_date AS d FROM gold.fct_purchases
                    UNION SELECT user_id, sent_date FROM silver.chat_messages),
        brute AS (
            SELECT d.date_day AS day,
                   (SELECT count(DISTINCT user_id) FROM ev WHERE ev.d BETWEEN d.date_day - 6  AND d.date_day) AS wau,
                   (SELECT count(DISTINCT user_id) FROM ev WHERE ev.d BETWEEN d.date_day - 27 AND d.date_day) AS mau,
                   (SELECT count(DISTINCT user_id) FROM ev WHERE ev.d = d.date_day) AS dau
            FROM gold.dim_date d)
        SELECT count(*) FROM brute b JOIN gold.mart_daily_platform_activity m ON m.activity_date = b.day
        WHERE b.wau <> m.wau OR b.mau <> m.mau_28d OR b.dau <> m.dau""")
    assert bad == [(0,)]
    assert q(c, "SELECT max(wau) FROM gold.mart_daily_platform_activity")[0][0] > 0
