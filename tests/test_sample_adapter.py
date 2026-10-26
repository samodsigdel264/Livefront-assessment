"""The adapter for the challenge's own SQLite file (Schools / Users / Products / Purchase_History / Conversation_History)."""

import sqlite3

from conftest import settings, table
from pipeline import ingest, warehouse
from pipeline.run import run

SAMPLE_DDL = """
CREATE TABLE Schools (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, address TEXT, created_at TEXT NOT NULL);
CREATE TABLE Users (id INTEGER PRIMARY KEY AUTOINCREMENT, school_id INTEGER, name TEXT NOT NULL, email TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL, FOREIGN KEY (school_id) REFERENCES Schools(id));
CREATE TABLE Products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, description TEXT, price REAL NOT NULL,
                       created_at TEXT NOT NULL);
CREATE TABLE Purchase_History (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
                               quantity INTEGER NOT NULL, purchased_at TEXT NOT NULL);
CREATE TABLE Conversation_History (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, message TEXT NOT NULL,
                                   sender TEXT CHECK(sender IN ('user', 'bot')), timestamp TEXT NOT NULL);
"""


def sample_db(path, with_purchase=True):
    c = sqlite3.connect(path)
    c.executescript(SAMPLE_DDL)
    c.executemany("INSERT INTO Schools (name, address, created_at) VALUES (?,?,?)", [
        ("Sunnydale Primary", "PSC 9954, Box 6655, APO AE 70058", "2020-04-08T22:45:50"),
        ("Cedar Grove Elementary", "205 Gabriel Meadows, Matthewhaven, MD 77827", "2020-11-21T18:50:55")])
    c.executemany("INSERT INTO Users (school_id, name, email, created_at) VALUES (?,?,?,?)", [
        (2, "Tracy Mendez", "tracy@yahoo.com", "2021-11-10T17:31:16"),
        (1, "Cher", "cher@gmail.com", "2022-09-30T16:14:28")])
    c.executemany("INSERT INTO Products (name, description, price, created_at) VALUES (?,?,?,?)", [
        ("Macaroni & Cheese Box", "Creamy and cheesy pasta", 9.61, "2021-07-09T02:43:42"),
        ("Granola Cereal Bars", "Chewy bars", 5.92, "2021-07-10T13:24:31")])
    if with_purchase:
        c.execute("INSERT INTO Purchase_History (user_id, product_id, quantity, purchased_at) VALUES (1, 2, 3, '2025-03-01T10:00:00')")
    c.executemany("INSERT INTO Conversation_History (user_id, message, sender, timestamp) VALUES (?,?,?,?)", [
        (1, "Why do we go to school?", "user", "2025-04-03T15:37:09"),
        (1, "To learn new things!", "bot", "2025-04-03T15:37:14")])
    c.commit()
    c.close()


def test_detects_the_sample_schema_and_maps_every_entity(tmp_path):
    db = tmp_path / "sample.sqlite"
    sample_db(db)
    src = ingest.open_sqlite(db)
    assert isinstance(src, ingest.SampleSchemaSource)
    con = warehouse.connect(":memory:")
    summary = run(settings(), [src], export=False, con=con)
    assert {e: s["rows"] for e, s in summary["ingest"].items()} == {
        "schools": 2, "products": 2, "users": 2, "purchases": 1, "referrals": 0, "chat_messages": 2}
    assert all(m["quarantined"] == 0 for m in summary["silver"].values())
    assert summary["reconciliation_failures"] == []

    assert table(con, "SELECT first_name, last_name, user_type FROM silver.users ORDER BY user_id") == [
        ("Tracy", "Mendez", "unknown"), ("Cher", None, "unknown")]
    assert table(con, "SELECT city, state, zip_code FROM silver.schools ORDER BY school_id") == [
        (None, None, None), ("Matthewhaven", "MD", "77827")]            # the APO address is not a city/state/zip
    assert table(con, "SELECT category FROM silver.products ORDER BY product_id") == [("Pantry",), ("Breakfast",)]
    # no price column in the source: price_paid = quantity x list price; donation then comes from points_per_dollar
    assert table(con, "SELECT price_paid FROM silver.purchases") == [(17.76,)]
    assert table(con, "SELECT round(donation_usd, 4) FROM gold.fct_purchases") == [(0.36,)]   # round(17.76 x 2) = 36 whole points x $0.01
    assert table(con, "SELECT sender, sent_date FROM silver.chat_messages ORDER BY message_id")[0][0] == "user"
    assert table(con, "SELECT user_messages, bot_messages, chat_sessions FROM gold.fct_user_daily_activity "
                      "WHERE did_chat") == [(1, 1, 1)]


def test_sample_without_purchases_still_builds_every_model(tmp_path):
    """The shipped sample has zero purchases; gold must still build cleanly (chat-only activity)."""
    db = tmp_path / "sample.sqlite"
    sample_db(db, with_purchase=False)
    con = warehouse.connect(":memory:")
    s = run(settings(), [ingest.open_sqlite(db)], export=False, con=con)
    assert s["reconciliation_failures"] == []
    assert table(con, "SELECT count(*) FROM gold.fct_purchases") == [(0,)]
    assert table(con, "SELECT dau, chatting_users FROM gold.mart_daily_platform_activity") == [(1, 1)]


def test_rerun_on_same_file_is_a_noop_and_canonical_sqlite_still_uses_the_plain_reader(tmp_path):
    db = tmp_path / "sample.sqlite"
    sample_db(db)
    con = warehouse.connect(":memory:")
    run(settings(), [ingest.open_sqlite(db)], export=False, con=con)
    again = run(settings(), [ingest.open_sqlite(db)], export=False, con=con)
    assert all(v["rows"] == 0 and v["skipped"] == (0 if k == "referrals" else 1) for k, v in again["ingest"].items())

    canon = tmp_path / "canonical.db"
    c = sqlite3.connect(canon)
    c.execute("CREATE TABLE users (user_id INTEGER, email TEXT)")
    c.commit()
    c.close()
    assert type(ingest.open_sqlite(canon)) is ingest.SqliteSource


def test_missing_sqlite_file_fails_loudly_and_does_not_create_one(tmp_path):
    import pytest
    from pipeline import run as run_mod
    missing = tmp_path / "nope.sqlite"
    with pytest.raises(FileNotFoundError):
        ingest.open_sqlite(missing)
    assert not missing.exists()                                     # no empty database left behind
    assert run_mod.main(["--sqlite", str(missing), "--db", str(tmp_path / "w.duckdb")]) == 1
    assert run_mod.main(["--source-dir", str(tmp_path / "no_such_dir"), "--db", str(tmp_path / "w.duckdb")]) == 1
