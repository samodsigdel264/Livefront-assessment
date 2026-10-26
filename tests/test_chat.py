"""Chatbot source: quality rules, the user-day activity fact, 'active user' semantics, PII boundary, incrementality."""

from conftest import (chat, product, purchase, run_world, school, table, user, write_csv)


def world(tmp_path, chats=None, extra_users=()):
    d = tmp_path / "src"
    write_csv(d, "schools", [school(1, "Alpha"), school(2, "Beta")])
    write_csv(d, "products", [product(1, 10.00, 2)])
    write_csv(d, "users", [user(1, 1, "2023-12-01 00:00:00"), user(2, 2, "2023-12-01 00:00:00"),
                           user(3, 1, "2023-12-01 00:00:00"), *extra_users])
    write_csv(d, "purchases", [purchase(1, 1, 1, 1, 10.0, 20, "2024-01-03 15:00:00"),
                               purchase(2, 2, 1, 1, 10.0, 20, "2024-01-05 09:00:00")])
    write_csv(d, "referrals", [])
    if chats is not None:
        write_csv(d, "chat_messages", chats)
    return d


HAND = [
    chat(1, 1, "2024-01-03 10:00:00"), chat(2, 1, "2024-01-03 10:00:05", "bot"),
    chat(3, 1, "2024-01-03 10:01:00"), chat(4, 1, "2024-01-03 10:01:04", "bot"),
    chat(5, 1, "2024-01-03 12:00:00"), chat(6, 1, "2024-01-03 12:00:03", "bot"),   # >30 min later: 2nd session
    chat(7, 2, "2024-01-03 09:00:00"),                                              # unanswered
]


def test_user_day_activity_fact_by_hand(tmp_path):
    con, _ = run_world(world(tmp_path, HAND))
    rows = table(con, """SELECT user_id, activity_date, purchases, revenue_usd, user_messages, bot_messages,
                                chat_sessions, did_purchase, did_chat
                         FROM gold.fct_user_daily_activity ORDER BY user_id, activity_date""")
    assert [tuple(map(str, r)) for r in rows] == [
        ("1", "2024-01-03", "1", "10.0", "3", "3", "2", "True", "True"),   # bought AND chatted, 2 sessions
        ("2", "2024-01-03", "0", "0.0", "1", "0", "1", "False", "True"),   # chat-only day
        ("2", "2024-01-05", "1", "10.0", "0", "0", "0", "True", "False"),  # purchase-only day
    ]


def test_active_user_means_purchase_or_chat(tmp_path):
    con, _ = run_world(world(tmp_path, HAND))
    # school DAU: user 2 is active on Jan 3 only by chatting; she is not a "purchasing user" that day
    assert table(con, """SELECT school_id, dau, purchasing_users, chatting_users FROM gold.agg_daily_active_users_by_school
                         WHERE activity_date = '2024-01-03' ORDER BY school_id""") == [(1, 1, 1, 1), (2, 1, 0, 1)]
    assert table(con, "SELECT dau, buying_users, chatting_users, user_messages, chat_sessions "
                      "FROM gold.mart_daily_platform_activity WHERE activity_date = '2024-01-03'") == [(2, 1, 2, 4, 3)]
    # Jan 5: user 2 buys (dau 1); trailing week still contains user 1 (Jan 3) and user 2 -> wau 2
    assert table(con, "SELECT dau, wau FROM gold.mart_daily_platform_activity WHERE activity_date = '2024-01-05'") == [(1, 2)]
    # the calendar is wide enough for a chat-only edge day
    assert [tuple(map(str, r)) for r in table(con, "SELECT min(date_day), max(date_day) FROM gold.dim_date")] == [
        ("2024-01-03", "2024-01-05")]


def test_chat_engagement_mart(tmp_path):
    con, _ = run_world(world(tmp_path, HAND))
    assert table(con, "SELECT segment, users, buyers, buyer_rate, share_of_chat_days_with_purchase "
                      "FROM gold.mart_chat_engagement ORDER BY segment") == [
        ("chatted", 2, 2, 1.0, 0.5),         # chat days: u1 (also bought) + u2 (did not) -> 1/2
        ("never_chatted", 1, 0, 0.0, None),  # user 3
    ]
    assert table(con, "SELECT school_id, members, chatters FROM gold.mart_chat_adoption_by_school ORDER BY school_id") == [
        (1, 2, 1), (2, 1, 1)]


def test_pipeline_works_without_any_chat_source(tmp_path):
    con, _ = run_world(world(tmp_path, None))
    assert table(con, "SELECT count(*) FROM gold.fct_user_daily_activity") == [(2,)]
    assert table(con, "SELECT dau FROM gold.mart_daily_platform_activity WHERE activity_date = '2024-01-03'") == [(1,)]


# ----------------------------------------------------------------------------- quality rules

def test_chat_quality_rules(tmp_path):
    rows = [
        chat(1, 1, "2024-01-03 10:00:00"),                                          # ok
        chat(2, 1, "2024-01-03 10:00:05", "robot"),                                  # invalid_sender
        chat(3, 1, "2024-01-03 10:00:09", " BOT "),                                  # fixable -> loaded as 'bot', warn
        {**chat(4, 1, "2024-01-03 10:01:00"), "message": "   "},                     # blank message
        chat(5, 999, "2024-01-03 10:02:00"),                                         # orphan user (retryable)
        chat(6, 1, "2023-11-01 10:00:00"),                                           # before signup
        chat(7, 1, "2024-01-03 11:00:00", message="email me at parent@example.com"),  # loads, PII warn
        chat(8, 1, "not-a-date"),                                                    # unparseable
    ]
    con, _ = run_world(world(tmp_path, rows), max_reject_rate=1.0)
    reasons = {int(r[0]): r[1] for r in table(con, "SELECT message_id, _dq_reasons FROM quarantine.chat_messages")}
    assert "invalid_sender" in reasons[2]
    assert "null_message" in reasons[4]
    assert "orphan_user" in reasons[5]
    assert "sent_before_signup" in reasons[6]
    assert "invalid_sent_at" in reasons[8]
    assert sorted(r[0] for r in table(con, "SELECT message_id FROM silver.chat_messages")) == [1, 3, 7]
    assert table(con, "SELECT sender FROM silver.chat_messages WHERE message_id = 3") == [("bot",)]
    dq = dict(table(con, "SELECT check_name, failed_rows FROM meta.dq_results WHERE entity='chat_messages' AND failed_rows > 0"))
    assert dq["sender_needs_normalising"] == 1 and dq["possible_pii_in_message"] == 1


def test_orphan_chat_is_released_when_the_user_arrives(tmp_path):
    d = world(tmp_path, [chat(1, 1, "2024-01-03 10:00:00"), chat(2, 50, "2024-01-04 10:00:00")])
    con, _ = run_world(d, max_reject_rate=1.0)
    assert table(con, "SELECT _retryable FROM quarantine.chat_messages") == [(True,)]
    write_csv(d, "users", [user(50, 1, "2023-12-01 00:00:00")], suffix="late")
    run_world(d, con=con, max_reject_rate=1.0)
    assert table(con, "SELECT count(*) FROM quarantine.chat_messages") == [(0,)]
    assert sorted(r[0] for r in table(con, "SELECT message_id FROM silver.chat_messages")) == [1, 2]
    assert table(con, "SELECT user_messages FROM gold.fct_user_daily_activity WHERE user_id = 50") == [(1,)]


# ----------------------------------------------------------------------------- privacy boundary

def test_message_text_never_reaches_gold(tmp_path):
    secret = "my email is secret.parent@example.com"
    con, _ = run_world(world(tmp_path, [chat(1, 1, "2024-01-03 10:00:00", message=secret)]))
    assert table(con, "SELECT count(*) FROM silver.chat_messages WHERE message LIKE '%secret.parent%'") == [(1,)]
    cols = table(con, "SELECT table_name, column_name FROM information_schema.columns WHERE table_schema='gold'")
    assert not [c for c in cols if c[1] in ("message", "email", "first_name", "last_name")]
    for (t,) in table(con, "SELECT table_name FROM information_schema.tables WHERE table_schema='gold'"):
        for row in table(con, f"SELECT * FROM gold.{t}"):
            assert "secret.parent" not in " ".join(map(str, row))


# ----------------------------------------------------------------------------- incrementality

def test_chat_incremental_load_equals_from_scratch(tmp_path):
    first = [chat(1, 1, "2024-01-03 10:00:00"), chat(2, 1, "2024-01-03 10:00:04", "bot"), chat(3, 2, "2024-01-04 08:00:00")]
    second = [chat(3, 2, "2024-01-08 08:00:00"),                    # correction: message moves to another day
              chat(9, 1, "2024-01-09 10:00:00"), chat(10, 1, "2024-01-09 10:00:03", "bot")]
    scratch_dir = tmp_path / "scratch"
    world(scratch_dir, first)
    write_csv(scratch_dir / "src", "chat_messages", second, suffix="b2")
    scratch, _ = run_world(scratch_dir / "src")

    inc_dir = tmp_path / "inc"
    world(inc_dir, first)
    inc, _ = run_world(inc_dir / "src")
    write_csv(inc_dir / "src", "chat_messages", second, suffix="b2")
    run_world(inc_dir / "src", con=inc)

    tables = [r[0] for r in table(inc, "SELECT table_name FROM information_schema.tables WHERE table_schema='gold' ORDER BY 1")]
    assert len(tables) >= 20
    for t in tables:   # str(): the empty-referral funnel holds NaN, and NaN != NaN
        assert str(table(inc, f"SELECT * FROM gold.{t} ORDER BY ALL")) == str(table(scratch, f"SELECT * FROM gold.{t} ORDER BY ALL")), t
    # the moved message (id 3) must have left its old partition
    assert table(inc, "SELECT count(*) FROM gold.fct_user_daily_activity WHERE user_id = 2 AND activity_date = '2024-01-04' AND did_chat") == [(0,)]
