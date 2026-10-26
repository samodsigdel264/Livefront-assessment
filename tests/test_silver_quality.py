"""Silver layer: typing, validation, quarantine, de-duplication, late-arriving data, circuit breaker."""

import pytest

from conftest import (product, purchase, referral, run_world, school, table, user, write_csv)
from pipeline import ingest, warehouse
from pipeline.silver import DataQualityError


@pytest.fixture
def base(tmp_path, basic_dims):
    d = tmp_path / "src"
    write_csv(d, "schools", basic_dims["schools"])
    write_csv(d, "products", basic_dims["products"])
    write_csv(d, "users", [user(1, 1, "2024-01-01 08:00:00"), user(2, 2, "2024-01-01 09:00:00")])
    write_csv(d, "purchases", [purchase(1, 1, 1, 1, 10.0, 20, "2024-01-02 10:00:00")])
    write_csv(d, "referrals", [])
    return d


def quarantine_reasons(con, entity):
    return {r[0]: r[1] for r in table(con, f"SELECT {entity}_key, _dq_reasons FROM "
                                      f"(SELECT *, {'purchase_id' if entity=='purchases' else 'user_id' if entity=='users' else 'referral_id'} AS {entity}_key FROM quarantine.{entity})")}


# ----------------------------------------------------------------------------- typing & rules

@pytest.mark.parametrize("bad_row, reason", [
    (dict(quantity=0), "non_positive_quantity"),
    (dict(quantity=-2), "non_positive_quantity"),
    (dict(price_paid=-1), "negative_price_paid"),
    (dict(price_paid=None), "null_price_paid"),
    (dict(purchased_at="not-a-date"), "invalid_purchased_at"),
    (dict(user_id=999), "orphan_user"),
    (dict(product_id=999), "orphan_product"),
    (dict(purchased_at="2023-12-31 23:00:00"), "purchased_before_signup"),
    (dict(quantity="abc"), "invalid_quantity"),
    (dict(quantity=5000), "implausible_quantity"),
])
def test_bad_purchases_are_quarantined_with_reason(base, bad_row, reason):
    good = purchase(10, 1, 1, 1, 10.0, 20, "2024-01-03 10:00:00")
    bad = {**purchase(11, 1, 1, 1, 10.0, 20, "2024-01-03 11:00:00"), **bad_row}
    write_csv(base, "purchases", [good, bad], suffix="b2")
    con, _ = run_world(base, max_reject_rate=1.0)
    assert reason in quarantine_reasons(con, "purchases")["11"]
    assert table(con, "SELECT count(*) FROM silver.purchases WHERE purchase_id IN (10)")[0][0] == 1
    assert table(con, "SELECT count(*) FROM silver.purchases WHERE purchase_id = 11")[0][0] == 0


def test_warn_rules_load_the_row_but_are_counted(base):
    # status needs normalising -> fixed, not rejected; wrong day_of_week -> silver re-derives it
    bad_dow = {**purchase(5, 1, 1, 1, 10.0, 20, "2024-01-03 10:00:00"), "day_of_week": 6}  # Wed, claims Sunday
    write_csv(base, "purchases", [bad_dow], suffix="b2")
    write_csv(base, "referrals", [referral(1, 1, "a@b.com", None, "2024-01-02 00:00:00", None, " SENT ")])
    con, _ = run_world(base)
    assert table(con, "SELECT day_of_week FROM silver.purchases WHERE purchase_id = 5") == [(2,)]
    assert table(con, "SELECT status FROM silver.referrals") == [("sent",)]
    dq = dict(table(con, "SELECT check_name, failed_rows FROM meta.dq_results WHERE failed_rows > 0"))
    assert dq["derived_field_mismatch"] == 1 and dq["status_needs_normalising"] == 1
    assert table(con, "SELECT count(*) FROM quarantine.purchases")[0][0] == 0


def test_user_rules(base):
    write_csv(base, "users", [
        user(1, 1, "2024-01-01 08:00:00"),
        user(2, 2, "2024-01-01 09:00:00"),
        user(3, 1, "2024-01-01 09:00:00", email="no-at-sign.com"),
        user(4, 77, "2024-01-01 09:00:00"),                                  # unknown school
        user(5, 1, "2024-01-01 09:00:00", user_type="alien"),                # coerced, not rejected
        user(6, 1, "2024-01-01 09:00:00", email="  MiXeD@Email.com "),       # normalised
    ], suffix="b2")
    con, _ = run_world(base, max_reject_rate=1.0)
    assert set(quarantine_reasons(con, "users")) == {"3", "4"}
    assert table(con, "SELECT user_type FROM silver.users WHERE user_id = 5") == [("unknown",)]
    assert table(con, "SELECT email FROM silver.users WHERE user_id = 6") == [("mixed@email.com",)]


def test_referral_rules(base):
    write_csv(base, "referrals", [
        referral(1, 1, "friend@x.com", None, "2024-01-02 00:00:00", None, "clicked"),                  # fine
        referral(2, 1, "user1@email.com", None, "2024-01-02 00:00:00", None, "sent"),                  # self (own e-mail)
        referral(3, 1, "user1b@x.com", 1, "2024-01-02 00:00:00", "2024-01-03 00:00:00", "converted"),  # self (same id)
        referral(4, 1, "user2@email.com", 2, "2024-01-02 00:00:00", None, "converted"),                # converted w/o ts
        referral(5, 1, "z@x.com", None, "2024-01-02 00:00:00", "2024-01-03 00:00:00", "sent"),         # ts but not converted
        referral(6, 1, "user2@email.com", 2, "2024-01-05 00:00:00", "2024-01-04 00:00:00", "converted"),  # before sent
        referral(7, 1, "q@x.com", None, "2024-01-02 00:00:00", None, "maybe"),                         # bad status
    ])
    con, _ = run_world(base, max_reject_rate=1.0)
    q = quarantine_reasons(con, "referrals")
    assert "self_referral" in q["2"] and "self_referral" in q["3"]
    assert "inconsistent_conversion" in q["4"] and "inconsistent_conversion" in q["5"]
    assert "converted_before_sent" in q["6"]
    assert "invalid_status" in q["7"]
    assert "1" not in q


# ----------------------------------------------------------------------------- de-duplication

def test_exact_duplicates_collapse_and_are_counted(base):
    dup = purchase(20, 1, 1, 1, 10.0, 20, "2024-01-03 10:00:00")
    write_csv(base, "purchases", [dup, dup, dup], suffix="b2")
    con, summary = run_world(base)
    assert table(con, "SELECT count(*) FROM silver.purchases WHERE purchase_id = 20") == [(1,)]
    assert summary["silver"]["purchases"]["duplicates_collapsed"] == 2


def test_latest_batch_wins_for_corrections(base):
    con, _ = run_world(base)
    write_csv(base, "purchases", [purchase(1, 1, 1, 3, 30.0, 60, "2024-01-02 10:00:00")], suffix="b2")
    run_world(base, con=con)
    assert table(con, "SELECT quantity, price_paid FROM silver.purchases WHERE purchase_id = 1") == [(3, 30.0)]


def test_bad_update_never_overwrites_a_good_record(base):
    con, _ = run_world(base)
    write_csv(base, "purchases", [{**purchase(1, 1, 1, 1, 10.0, 20, "2024-01-02 10:00:00"), "quantity": -5}], suffix="b2")
    run_world(base, con=con, max_reject_rate=1.0)
    assert table(con, "SELECT quantity FROM silver.purchases WHERE purchase_id = 1") == [(1,)]
    assert "non_positive_quantity" in quarantine_reasons(con, "purchases")["1"]


# ----------------------------------------------------------------------------- late-arriving parents

def test_orphans_are_held_and_released_when_parent_arrives(base):
    write_csv(base, "purchases", [purchase(30, 3, 1, 1, 10.0, 20, "2024-01-05 10:00:00")], suffix="b2")  # user 3 unknown
    con, _ = run_world(base)
    assert table(con, "SELECT _retryable FROM quarantine.purchases") == [(True,)]
    assert table(con, "SELECT count(*) FROM silver.purchases WHERE purchase_id = 30") == [(0,)]

    write_csv(base, "users", [user(3, 1, "2024-01-04 08:00:00")], suffix="b2")                           # parent shows up
    _, summary = run_world(base, con=con)
    assert table(con, "SELECT count(*) FROM silver.purchases WHERE purchase_id = 30") == [(1,)]
    assert table(con, "SELECT count(*) FROM quarantine.purchases") == [(0,)]
    assert summary["silver"]["purchases"]["recovered"] == 1
    assert table(con, "SELECT count(*) FROM gold.fct_purchases WHERE purchase_id = 30") == [(1,)]      # reached gold too


def test_non_retryable_rows_are_not_retried_forever(base):
    write_csv(base, "purchases", [{**purchase(31, 1, 1, 1, 10.0, 20, "2024-01-03 10:00:00"), "quantity": 0}], suffix="b2")
    con, _ = run_world(base, max_reject_rate=1.0)
    assert table(con, "SELECT _retryable FROM quarantine.purchases") == [(False,)]
    _, summary = run_world(base, con=con)
    assert summary["silver"]["purchases"].get("retried", 0) == 0


# ----------------------------------------------------------------------------- circuit breaker

def _many_purchases(n, bad_every):
    rows = []
    for i in range(n):
        r = purchase(100 + i, 1, 1, 1, 10.0, 20, "2024-01-03 10:00:00")
        if i % bad_every == 0:
            r["quantity"] = 0
        rows.append(r)
    return rows


def test_breaker_trips_rolls_back_and_keeps_the_evidence(base):
    con, _ = run_world(base)
    write_csv(base, "purchases", _many_purchases(100, bad_every=4), suffix="b2")      # 25% rejected
    with pytest.raises(DataQualityError, match="25/100"):
        run_world(base, con=con, max_reject_rate=0.10)
    assert table(con, "SELECT count(*) FROM silver.purchases") == [(1,)]               # nothing from the bad batch
    assert table(con, "SELECT count(*) FROM quarantine.purchases") == [(0,)]
    assert table(con, "SELECT last_batch_id FROM meta.watermarks WHERE entity='purchases'") == [(4,)]  # not advanced
    failed = table(con, "SELECT failed_rows FROM meta.dq_results WHERE entity='purchases' AND check_name='non_positive_quantity' "
                        "ORDER BY recorded_at DESC LIMIT 1")
    assert failed == [(25,)]
    assert table(con, "SELECT status FROM meta.runs ORDER BY started_at DESC LIMIT 1") == [("failed",)]


def test_breaker_stays_quiet_below_threshold_and_on_tiny_batches(base):
    write_csv(base, "purchases", _many_purchases(100, bad_every=25), suffix="b2")      # 4% rejected
    con, summary = run_world(base, max_reject_rate=0.10)
    assert summary["silver"]["purchases"]["quarantined"] == 4

    tiny = base.parent / "tiny"
    for name in ("schools", "products", "users", "referrals"):
        write_csv(tiny, name, [])
    write_csv(tiny, "purchases", [{**purchase(1, 1, 1, 1, 10.0, 20, "2024-01-03 10:00:00"), "quantity": 0}])
    write_csv(tiny, "users", [user(1, 1, "2024-01-01 08:00:00")])
    write_csv(tiny, "schools", [school(1)])
    write_csv(tiny, "products", [product(1, 10, 2)])
    _, s = run_world(tiny, max_reject_rate=0.10)                                      # 100% rejected but 1 row
    assert s["silver"]["purchases"]["quarantined"] == 1


# ----------------------------------------------------------------------------- ingestion

def test_same_file_is_ingested_once(base):
    con, _ = run_world(base)
    n = table(con, "SELECT count(*) FROM bronze.purchases")[0][0]
    _, summary = run_world(base, con=con)
    assert table(con, "SELECT count(*) FROM bronze.purchases") == [(n,)]
    assert summary["ingest"]["purchases"]["batches"] == 0 and summary["ingest"]["purchases"]["skipped"] == 1


def test_missing_required_column_fails_the_load(base):
    write_csv(base, "purchases", [purchase(1, 1, 1, 1, 10.0, 20, "2024-01-02 10:00:00")],
              headers=["purchase_id", "user_id", "product_id", "quantity", "purchased_at"])  # no price_paid
    with pytest.raises(ingest.SchemaContractError, match="price_paid"):
        run_world(base)


def test_unknown_extra_column_is_tolerated(base):
    write_csv(base, "purchases", [{**purchase(2, 1, 1, 1, 10.0, 20, "2024-01-03 10:00:00"), "coupon": "X"}],
              suffix="b2", headers=["purchase_id", "user_id", "product_id", "quantity", "price_paid", "points_earned",
                                    "purchased_at", "day_of_week", "hour_of_day", "coupon"])
    con, _ = run_world(base)
    assert table(con, "SELECT count(*) FROM silver.purchases WHERE purchase_id = 2") == [(1,)]


def test_sqlite_source_matches_csv_source(tmp_path):
    import data_generator
    g = data_generator.CartonCapsDataGenerator(seed=5, output_dir=str(tmp_path))
    g.generate_all(n_schools=5, n_users=80, n_products=25, n_purchases=400, n_referrals=60)
    con_csv, _ = run_world(tmp_path)
    con_sql = warehouse.connect(":memory:")
    from pipeline.run import run
    from conftest import settings
    run(settings(), [ingest.SqliteSource(tmp_path / "carton_caps_generated.db")], export=False, con=con_sql)
    for t in ("users", "purchases", "referrals"):
        a = table(con_csv, f"SELECT * EXCLUDE (_batch_id, _loaded_at) FROM silver.{t} ORDER BY 1")
        b = table(con_sql, f"SELECT * EXCLUDE (_batch_id, _loaded_at) FROM silver.{t} ORDER BY 1")
        assert a == b


def test_rebuild_from_bronze_reproduces_silver_and_gold(base):
    write_csv(base, "purchases", _many_purchases(60, bad_every=30), suffix="b2")
    con, _ = run_world(base, max_reject_rate=1.0)
    before_s = table(con, "SELECT * EXCLUDE (_batch_id, _loaded_at) FROM silver.purchases ORDER BY 1")
    before_g = table(con, "SELECT * FROM gold.mart_school_fundraising ORDER BY 1")
    from pipeline.run import run
    from conftest import settings
    run(settings(max_reject_rate=1.0), [ingest.CsvSource(base)], export=False, con=con, rebuild_from_bronze=True)
    assert table(con, "SELECT * EXCLUDE (_batch_id, _loaded_at) FROM silver.purchases ORDER BY 1") == before_s
    assert table(con, "SELECT * FROM gold.mart_school_fundraising ORDER BY 1") == before_g
