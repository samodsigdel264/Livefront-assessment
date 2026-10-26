"""
Declarative description of each source entity: its raw contract (bronze), its typed shape (silver)
and its data-quality rules.

A rule is a SQL predicate that is TRUE when the row is BAD. Predicates can reference:
  * typed columns of the entity (unqualified), e.g.  quantity <= 0
  * raw text columns prefixed r_, e.g.               r_status IS NOT NULL
  * prefixed lookup columns exposed by `joins`,       e.g. su_created_at (no name clashes with typed columns)

severity "error" -> row is quarantined (not loaded to silver)
severity "warn"  -> row is loaded, the rule is counted in meta.dq_results
retryable        -> failure is caused by a *missing parent* (late-arriving dimension): the quarantined
                    row is re-evaluated on every later run and released once the parent shows up.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple

EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


@dataclass(frozen=True)
class Col:
    name: str
    dtype: str                      # DuckDB type in silver
    expr: str = ""                  # typed expression over {raw}; default = TRY_CAST
    required: bool = False

    def typed_expr(self) -> str:
        if self.expr:
            return self.expr
        return f"TRY_CAST(NULLIF(trim({{raw}}), '') AS {self.dtype})"


@dataclass(frozen=True)
class Rule:
    name: str
    predicate: str
    severity: str = "error"         # error | warn
    retryable: bool = False
    description: str = ""


@dataclass(frozen=True)
class Derived:
    name: str
    dtype: str
    expr: str                       # evaluated over typed silver columns


@dataclass(frozen=True)
class Entity:
    name: str
    key: str
    cols: Tuple[Col, ...]
    rules: Tuple[Rule, ...] = ()
    joins: str = ""                 # LEFT JOINs against silver lookups, appended to the typed CTE
    derived: Tuple[Derived, ...] = ()
    raw_extra: Tuple[str, ...] = ()  # optional source columns kept in bronze but re-derived in silver
    date_col: str = ""              # silver date column that drives partition-incremental gold facts

    @property
    def raw_cols(self) -> List[str]:
        return [c.name for c in self.cols] + list(self.raw_extra)

    @property
    def silver_cols(self) -> List[Tuple[str, str]]:
        return [(c.name, c.dtype) for c in self.cols] + [(d.name, d.dtype) for d in self.derived]


TEXT = "VARCHAR"
_TXT = "NULLIF(trim({raw}), '')"

# --------------------------------------------------------------------------------------
SCHOOLS = Entity(
    name="schools",
    key="school_id",
    cols=(
        Col("school_id", "BIGINT", required=True),
        Col("name", TEXT, _TXT, required=True),
        Col("address", TEXT, _TXT),
        Col("city", TEXT, _TXT),
        Col("state", TEXT, "upper(NULLIF(trim({raw}), ''))"),
        Col("zip_code", TEXT, _TXT),
        Col("created_at", "TIMESTAMP"),
    ),
    rules=(
        Rule("invalid_state_code", "state IS NOT NULL AND length(state) <> 2", "warn",
             description="State should be a 2-letter code"),
    ),
)

PRODUCTS = Entity(
    name="products",
    key="product_id",
    cols=(
        Col("product_id", "BIGINT", required=True),
        Col("name", TEXT, _TXT, required=True),
        Col("category", TEXT, _TXT),
        Col("price", "DOUBLE", required=True),
        Col("points_per_dollar", "DOUBLE"),
        Col("created_at", "TIMESTAMP"),
    ),
    rules=(
        Rule("non_positive_price", "price <= 0", description="List price must be > 0"),
        Rule("negative_points_rate", "points_per_dollar < 0"),
    ),
)

USERS = Entity(
    name="users",
    key="user_id",
    cols=(
        Col("user_id", "BIGINT", required=True),
        Col("first_name", TEXT, _TXT),
        Col("last_name", TEXT, _TXT),
        Col("email", TEXT, "lower(NULLIF(trim({raw}), ''))", required=True),
        Col("school_id", "BIGINT", required=True),
        Col("created_at", "TIMESTAMP", required=True),
        Col("user_type", TEXT,
            "CASE WHEN lower(trim({raw})) IN ('parent','teacher','supporter') "
            "THEN lower(trim({raw})) ELSE 'unknown' END"),
    ),
    joins="LEFT JOIN (SELECT school_id AS ss_school_id FROM silver.schools) ss ON ss.ss_school_id = typed.school_id",
    rules=(
        Rule("malformed_email", f"email IS NOT NULL AND NOT regexp_matches(email, '{EMAIL_RE}')"),
        Rule("orphan_school", "school_id IS NOT NULL AND ss_school_id IS NULL", retryable=True,
             description="School not (yet) known; held for retry"),
        Rule("unexpected_user_type", "r_user_type IS NOT NULL AND user_type = 'unknown'", "warn",
             description="Coerced to 'unknown'"),
        Rule("shared_email", "min(user_id) OVER (PARTITION BY email) <> max(user_id) OVER (PARTITION BY email)",
             "warn", description="Same e-mail on several user_ids within the batch"),
    ),
)

PURCHASES = Entity(
    name="purchases",
    key="purchase_id",
    cols=(
        Col("purchase_id", "BIGINT", required=True),
        Col("user_id", "BIGINT", required=True),
        Col("product_id", "BIGINT", required=True),
        Col("quantity", "INTEGER", required=True),
        Col("price_paid", "DOUBLE", required=True),
        Col("points_earned", "INTEGER"),
        Col("purchased_at", "TIMESTAMP", required=True),
    ),
    joins=("LEFT JOIN (SELECT user_id AS su_user_id, created_at AS su_created_at FROM silver.users) su "
           "ON su.su_user_id = typed.user_id "
           "LEFT JOIN (SELECT product_id AS sp_product_id, price AS sp_price FROM silver.products) sp "
           "ON sp.sp_product_id = typed.product_id"),
    rules=(
        Rule("non_positive_quantity", "quantity <= 0"),
        Rule("implausible_quantity", "quantity > 1000"),
        Rule("negative_price_paid", "price_paid < 0"),
        Rule("negative_points", "points_earned < 0"),
        Rule("orphan_user", "user_id IS NOT NULL AND su_user_id IS NULL", retryable=True),
        Rule("orphan_product", "product_id IS NOT NULL AND sp_product_id IS NULL", retryable=True),
        Rule("purchased_before_signup", "su_user_id IS NOT NULL AND purchased_at < su_created_at"),
        Rule("purchased_in_future", "purchased_at > now() + INTERVAL 1 DAY"),
        Rule("price_exceeds_list", "sp_product_id IS NOT NULL AND price_paid > sp_price * quantity * 1.001", "warn",
             description="Paid more than list price x qty"),
        Rule("derived_field_mismatch",
             "purchased_at IS NOT NULL AND ("
             "(r_day_of_week IS NOT NULL AND TRY_CAST(r_day_of_week AS INTEGER) <> (dayofweek(purchased_at) + 6) % 7) OR "
             "(r_hour_of_day IS NOT NULL AND TRY_CAST(r_hour_of_day AS INTEGER) <> hour(purchased_at)))",
             "warn", description="Source day_of_week/hour_of_day disagree with timestamp; silver re-derives them"),
    ),
    raw_extra=("day_of_week", "hour_of_day"),
    date_col="purchase_date",
    derived=(
        Derived("purchase_date", "DATE", "CAST(purchased_at AS DATE)"),
        Derived("day_of_week", "INTEGER", "(dayofweek(purchased_at) + 6) % 7"),   # 0 = Monday
        Derived("hour_of_day", "INTEGER", "hour(purchased_at)"),
    ),
)

REFERRALS = Entity(
    name="referrals",
    key="referral_id",
    cols=(
        Col("referral_id", "BIGINT", required=True),
        Col("referrer_user_id", "BIGINT", required=True),
        Col("referred_email", TEXT, "lower(NULLIF(trim({raw}), ''))", required=True),
        Col("referred_user_id", "BIGINT"),
        Col("referral_code", TEXT, _TXT, required=True),
        Col("sent_at", "TIMESTAMP", required=True),
        Col("converted_at", "TIMESTAMP"),
        Col("status", TEXT, "lower(NULLIF(trim({raw}), ''))", required=True),
    ),
    joins=("LEFT JOIN (SELECT user_id AS sr_user_id, email AS sr_email FROM silver.users) sr "
           "ON sr.sr_user_id = typed.referrer_user_id "
           "LEFT JOIN (SELECT user_id AS sru_user_id, created_at AS sru_created_at FROM silver.users) sru "
           "ON sru.sru_user_id = typed.referred_user_id"),
    rules=(
        Rule("invalid_status", "status IS NOT NULL AND status NOT IN ('sent','clicked','converted')"),
        Rule("status_needs_normalising", "r_status IS NOT NULL AND r_status <> status", "warn",
             description="Case/whitespace fixed in silver"),
        Rule("malformed_referred_email",
             f"referred_email IS NOT NULL AND NOT regexp_matches(referred_email, '{EMAIL_RE}')"),
        Rule("orphan_referrer", "referrer_user_id IS NOT NULL AND sr_user_id IS NULL", retryable=True),
        Rule("orphan_referred_user", "referred_user_id IS NOT NULL AND sru_user_id IS NULL", retryable=True),
        Rule("self_referral",
             "referrer_user_id = referred_user_id OR (sr_email IS NOT NULL AND sr_email = referred_email)",
             description="Referral rules s5: self-referrals not allowed"),
        Rule("inconsistent_conversion",
             "(status = 'converted' AND (referred_user_id IS NULL OR converted_at IS NULL)) OR "
             "(status IN ('sent','clicked') AND (referred_user_id IS NOT NULL OR converted_at IS NOT NULL))"),
        Rule("converted_before_sent", "converted_at < sent_at"),
        Rule("referred_not_new", "sru_user_id IS NOT NULL AND sru_created_at < sent_at", "warn",
             description="Referral rules s1: referred user must be new"),
    ),
)


PHONE_RE = r"(\+?\d[\d\-\s().]{8,}\d)"

CHAT_MESSAGES = Entity(
    name="chat_messages",
    key="message_id",
    cols=(
        Col("message_id", "BIGINT", required=True),
        Col("user_id", "BIGINT", required=True),
        Col("message", TEXT, _TXT, required=True),
        Col("sender", TEXT, "lower(NULLIF(trim({raw}), ''))", required=True),
        Col("sent_at", "TIMESTAMP", required=True),
    ),
    joins=("LEFT JOIN (SELECT user_id AS su_user_id, created_at AS su_created_at FROM silver.users) su "
           "ON su.su_user_id = typed.user_id"),
    rules=(
        Rule("invalid_sender", "sender IS NOT NULL AND sender NOT IN ('user','bot')"),
        Rule("sender_needs_normalising", "r_sender IS NOT NULL AND r_sender <> sender", "warn",
             description="Case/whitespace fixed in silver"),
        Rule("orphan_user", "user_id IS NOT NULL AND su_user_id IS NULL", retryable=True),
        Rule("sent_before_signup", "su_user_id IS NOT NULL AND sent_at < su_created_at"),
        Rule("sent_in_future", "sent_at > now() + INTERVAL 1 DAY"),
        Rule("possible_pii_in_message",
             f"sender = 'user' AND (regexp_matches(message, '[^@\\s]+@[^@\\s]+\\.[^@\\s]+') OR "
             f"regexp_matches(message, '{PHONE_RE}'))",
             "warn", description="User typed an e-mail / phone number into the chat; text stays in silver only"),
    ),
    date_col="sent_date",
    derived=(
        Derived("sent_date", "DATE", "CAST(sent_at AS DATE)"),
        Derived("message_chars", "INTEGER", "length(message)"),
    ),
)

# Order matters: parents before children so FK lookups see freshly loaded dimension rows.
ENTITIES: List[Entity] = [SCHOOLS, PRODUCTS, USERS, PURCHASES, REFERRALS, CHAT_MESSAGES]
BY_NAME = {e.name: e for e in ENTITIES}
