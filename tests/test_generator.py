import csv
import sqlite3
from collections import Counter

import pytest

from data_generator import CartonCapsDataGenerator


@pytest.fixture(scope="module")
def gen(tmp_path_factory):
    g = CartonCapsDataGenerator(seed=7, output_dir=str(tmp_path_factory.mktemp("gen")))
    g.generate_schools(20)
    g.generate_users(600)
    g.generate_products(100)
    g.generate_referrals(500)
    g.generate_purchases(6000)
    return g


def test_requested_volumes(gen):
    assert len(gen.schools) == 20 and len(gen.users) == 600 and len(gen.products) == 100
    assert len(gen.purchases) == 6000
    assert 450 <= len(gen.referrals) <= 500   # an attempt is dropped only if no valid referrer exists yet


def test_deterministic_for_same_seed(tmp_path):
    def build():
        g = CartonCapsDataGenerator(seed=3, output_dir=str(tmp_path))
        g.generate_schools(5); g.generate_users(50); g.generate_products(25)
        g.generate_referrals(40); g.generate_purchases(300)
        return g.purchases, g.referrals, g.users
    assert build() == build()


def test_emails_unique(gen):
    emails = [u["email"] for u in gen.users]
    assert len(emails) == len(set(emails))


def test_foreign_keys_valid(gen):
    uids = {u["user_id"] for u in gen.users}
    sids = {s["school_id"] for s in gen.schools}
    pids = {p["product_id"] for p in gen.products}
    assert all(u["school_id"] in sids for u in gen.users)
    assert all(p["user_id"] in uids and p["product_id"] in pids for p in gen.purchases)
    assert all(r["referrer_user_id"] in uids for r in gen.referrals)
    assert all(r["referred_user_id"] in uids for r in gen.referrals if r["referred_user_id"])


def test_users_span_before_and_inside_the_window(gen):
    legacy = [u for u in gen.users if u["created_at"] < gen.start_date]
    new = [u for u in gen.users if u["created_at"] >= gen.start_date]
    assert 0.3 < len(legacy) / len(gen.users) < 0.5
    assert all(u["created_at"] <= gen.end_date for u in new)


def test_no_purchase_before_signup_or_outside_window(gen):
    created = {u["user_id"]: u["created_at"] for u in gen.users}
    for p in gen.purchases:
        assert p["purchased_at"] >= created[p["user_id"]]
        assert gen.start_date <= p["purchased_at"] <= gen.end_date


def test_purchase_fields_consistent(gen):
    prod = {p["product_id"]: p for p in gen.products}
    for p in gen.purchases:
        assert p["quantity"] >= 1
        assert p["day_of_week"] == p["purchased_at"].weekday()
        assert p["hour_of_day"] == p["purchased_at"].hour
        list_total = prod[p["product_id"]]["price"] * p["quantity"]
        assert 0 < p["price_paid"] <= list_total + 0.01          # never above list; promos only reduce
        assert p["price_paid"] >= list_total * 0.70 - 0.01       # promo is at most 30% off
        assert p["points_earned"] == round(p["price_paid"] * prod[p["product_id"]]["points_per_dollar"])


def test_purchase_volume_is_heavy_tailed(gen):
    per_user = sorted(Counter(p["user_id"] for p in gen.purchases).values(), reverse=True)
    top10 = sum(per_user[: max(1, len(gen.users) // 10)]) / sum(per_user)
    assert top10 > 0.30                         # a uniform allocation would give ~0.10-0.12
    assert len(per_user) < len(gen.users)       # some registered users never buy


def test_weekends_are_busier_per_day(gen):
    c = Counter(p["day_of_week"] for p in gen.purchases)
    assert (c[5] + c[6]) / 2 > sum(c[d] for d in range(5)) / 5


def test_evening_peak(gen):
    c = Counter(p["hour_of_day"] for p in gen.purchases)
    assert sum(c[h] for h in (17, 18, 19)) > sum(c[h] for h in (1, 2, 3))


def test_referral_status_and_timestamps_consistent(gen):
    for r in gen.referrals:
        if r["status"] == "converted":
            assert r["referred_user_id"] is not None and r["converted_at"] is not None
            assert r["converted_at"] >= r["sent_at"]
        else:
            assert r["referred_user_id"] is None and r["converted_at"] is None
        assert r["status"] in ("sent", "clicked", "converted")


def test_conversion_rate_is_realistic(gen):
    rate = sum(r["status"] == "converted" for r in gen.referrals) / len(gen.referrals)
    assert 0.10 <= rate <= 0.30


def test_referral_rules_respected(gen):
    users = {u["user_id"]: u for u in gen.users}
    referred = [r["referred_user_id"] for r in gen.referrals if r["referred_user_id"]]
    assert len(referred) == len(set(referred))                              # a user is referred once
    for r in gen.referrals:
        assert r["referrer_user_id"] != r["referred_user_id"]               # no self referral
        assert users[r["referrer_user_id"]]["created_at"] < r["sent_at"]    # referrer existed when inviting
        if r["referred_user_id"]:
            u = users[r["referred_user_id"]]
            assert u["created_at"] == r["converted_at"] and u["created_at"] >= gen.start_date  # referred user is new
            assert u["email"] == r["referred_email"]


def test_few_users_refer_and_a_few_refer_a_lot(gen):
    per_referrer = Counter(r["referrer_user_id"] for r in gen.referrals)
    assert len(per_referrer) < 0.25 * len(gen.users)
    top = sorted(per_referrer.values(), reverse=True)
    assert top[0] >= 5 * (sum(top) / len(top))


def test_referral_chains_exist(gen):
    """A referred user who goes on to refer someone else (depth >= 2)."""
    referred = {r["referred_user_id"] for r in gen.referrals if r["referred_user_id"]}
    assert any(r["referrer_user_id"] in referred for r in gen.referrals if r["status"] == "converted")


def test_referred_users_activate_faster(gen):
    referred = {r["referred_user_id"] for r in gen.referrals if r["referred_user_id"]}
    first = {}
    for p in gen.purchases:
        first.setdefault(p["user_id"], p["purchased_at"])
    created = {u["user_id"]: u["created_at"] for u in gen.users}
    def mean_delay(ids):
        d = [(first[i] - created[i]).total_seconds() for i in ids if i in first]
        return sum(d) / len(d)
    organic_new = {u["user_id"] for u in gen.users if u["created_at"] >= gen.start_date} - referred
    assert mean_delay(referred) < mean_delay(organic_new)


def test_dirty_injection_leaves_clean_data_untouched(gen):
    before = [dict(p) for p in gen.purchases[:50]]
    dirty = gen.make_dirty(0.02)
    assert gen.purchases[:50] == before
    assert len(dirty["purchases"]) > len(gen.purchases)                  # duplicates appended
    assert any(p["quantity"] <= 0 for p in dirty["purchases"])
    assert any(p["purchased_at"] == "not-a-date" for p in dirty["purchases"])
    assert any("@" not in u["email"] for u in dirty["users"])
    assert sum(gen.dirty_manifest.values()) > 0


def test_outputs_written_and_sqlite_rerun_is_idempotent(tmp_path):
    g = CartonCapsDataGenerator(seed=1, output_dir=str(tmp_path))
    g.generate_all(n_schools=5, n_users=60, n_products=25, n_purchases=300, n_referrals=40)
    g.save_to_sqlite()                                                   # second save must not duplicate
    for name in ("schools", "users", "products", "purchases", "referrals"):
        with open(tmp_path / f"{name}.csv", encoding="utf-8") as f:
            n_csv = sum(1 for _ in csv.DictReader(f))
        n_db = sqlite3.connect(tmp_path / "carton_caps_generated.db").execute(f"select count(*) from {name}").fetchone()[0]
        assert n_csv == n_db > 0
