"""Shared helpers: build tiny, hand-checkable CSV worlds and run the real pipeline over them."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline import ingest, warehouse                      # noqa: E402
from pipeline.config import Settings                        # noqa: E402
from pipeline.run import run                                # noqa: E402

HEADERS = {
    "schools": ["school_id", "name", "address", "city", "state", "zip_code", "created_at"],
    "products": ["product_id", "name", "category", "price", "points_per_dollar", "created_at"],
    "users": ["user_id", "first_name", "last_name", "email", "school_id", "created_at", "user_type"],
    "purchases": ["purchase_id", "user_id", "product_id", "quantity", "price_paid", "points_earned",
                  "purchased_at", "day_of_week", "hour_of_day"],
    "referrals": ["referral_id", "referrer_user_id", "referred_email", "referred_user_id", "referral_code",
                  "sent_at", "converted_at", "status"],
    "chat_messages": ["message_id", "user_id", "message", "sender", "sent_at"],
}


def write_csv(directory: Path, name: str, rows: list, suffix: str = "", headers=None) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}{('__' + suffix) if suffix else ''}.csv"
    cols = headers or HEADERS[name]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({c: ("" if r.get(c) is None else r.get(c)) for c in cols})
    return path


def school(i, name=None, state="TX", **kw):
    return dict(school_id=i, name=name or f"School {i}", address="1 Main St", city="Town", state=state,
                zip_code="75001", created_at="2020-01-01 00:00:00", **kw)


def product(i, price, ppd, category="Snacks", name=None):
    return dict(product_id=i, name=name or f"Product {i}", category=category, price=price,
                points_per_dollar=ppd, created_at="2023-01-01 00:00:00")


def user(i, school_id, created_at, user_type="parent", email=None):
    return dict(user_id=i, first_name=f"F{i}", last_name=f"L{i}", email=email or f"user{i}@email.com",
                school_id=school_id, created_at=created_at, user_type=user_type)


def purchase(i, user_id, product_id, qty, paid, pts, ts):
    from datetime import datetime
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
    return dict(purchase_id=i, user_id=user_id, product_id=product_id, quantity=qty, price_paid=paid,
                points_earned=pts, purchased_at=ts, day_of_week=dt.weekday(), hour_of_day=dt.hour)


def referral(i, referrer, email, referred, sent, converted, status, code="CODE"):
    return dict(referral_id=i, referrer_user_id=referrer, referred_email=email, referred_user_id=referred,
                referral_code=f"{code}{referrer}", sent_at=sent, converted_at=converted, status=status)


def chat(i, user_id, ts, sender="user", message=None):
    return dict(message_id=i, user_id=user_id, sender=sender, sent_at=ts,
                message=message or ("How many points do I have?" if sender == "user" else "Check the Earnings tab."))


def settings(**kw):
    return Settings(**kw)


def run_world(directory: Path, con=None, **settings_kw):
    """Run the whole pipeline over `directory` against an in-memory warehouse; returns (con, summary)."""
    con = con or warehouse.connect(":memory:")
    summary = run(settings(**settings_kw), [ingest.CsvSource(directory)], export=False, con=con)
    return con, summary


@pytest.fixture
def basic_dims():
    """Two schools, two products, no users yet."""
    return dict(
        schools=[school(1, "Alpha", "TX"), school(2, "Beta", "TX")],
        products=[product(1, 10.00, 2), product(2, 5.00, 1, "Dairy")],
    )


def table(con, sql):
    return con.execute(sql).fetchall()
