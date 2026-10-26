"""
Carton Caps Data Generator

Generates synthetic data for the Carton Caps analytics pipeline challenge.

Usage:
    python data_generator.py                      # clean data -> ./data/
    python data_generator.py --dirty-rate 0.01    # inject ~1% realistic data-quality problems
    python data_generator.py --users 100000 --purchases 1000000 --referrals 100000   # "100x" run

Outputs (CSV + one SQLite file): schools, users, products, purchases, referrals, chat_messages.

What changed relative to the provided scaffold (all documented in docs/DATA_ASSUMPTIONS.md):
  * generate_purchases / generate_referrals implemented.
  * generate_users: signups now span the observation window (the scaffold put every user
    *before* the window, so there was nothing to convert referrals into or to build cohorts
    from), e-mails are guaranteed unique (the provided SQLite schema has UNIQUE(email) and the
    scaffold's random suffix collides), and school sizes are skewed rather than uniform.
  * generate_all: referrals are generated before purchases so referred users can behave
    differently (faster activation). Optional dirty-data injection.
  * save_to_sqlite: idempotent (re-runs no longer duplicate rows) and type inference no longer
    breaks when the first row has a NULL.
"""

from __future__ import annotations

import argparse
import bisect
import copy
import csv
import hashlib
import math
import os
import random
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from itertools import accumulate
from typing import Dict, List, Optional


# --------------------------------------------------------------------------------------
# Behavioural constants (see docs/DATA_ASSUMPTIONS.md for the reasoning behind each)
# --------------------------------------------------------------------------------------

# Weekday multipliers, Monday=0. Grocery shopping skews to the weekend.
DOW_WEIGHTS = [0.90, 0.85, 0.90, 0.95, 1.10, 1.35, 1.20]

# Hour-of-day multipliers: small morning bump (school run), strong evening peak.
HOUR_WEIGHTS = [
    0.2, 0.1, 0.1, 0.1, 0.1, 0.2, 0.5, 1.2, 1.5, 1.0, 0.8, 0.9,
    1.2, 1.0, 0.8, 0.9, 1.4, 2.0, 2.2, 2.0, 1.5, 1.0, 0.6, 0.3,
]

# Month multipliers: post-holiday January, end-of-school-year push in May, summer dip in June.
MONTH_WEIGHTS = {1: 1.00, 2: 0.95, 3: 1.05, 4: 1.00, 5: 1.15, 6: 0.80}

# Behavioural segments. share = population share, mult = purchase-volume multiplier,
# tau_days = mean "active lifetime" (exponential decay of purchase intensity after first purchase).
SEGMENTS = {
    "casual":  {"share": 0.55, "mult": 1.0, "tau_days": 25.0},
    "regular": {"share": 0.33, "mult": 3.0, "tau_days": 70.0},
    "power":   {"share": 0.12, "mult": 8.0, "tau_days": 220.0},
}

USER_TYPE_BUY_MULT = {"parent": 1.0, "teacher": 0.8, "supporter": 0.6}

# Category affinity by user type (before the per-user "favourite category" boost).
CATEGORY_AFFINITY = {
    "parent":    {"Breakfast": 1.3, "Snacks": 1.2, "Beverages": 1.0, "Dairy": 1.2, "Pantry": 1.0},
    "teacher":   {"Breakfast": 0.9, "Snacks": 1.4, "Beverages": 1.5, "Dairy": 0.6, "Pantry": 0.6},
    "supporter": {"Breakfast": 0.8, "Snacks": 0.8, "Beverages": 1.1, "Dairy": 1.2, "Pantry": 1.4},
}

# Referral funnel (per attempt). Overall sent->converted lands at 18%, inside the 10-30% band.
P_CONVERTED = 0.18
P_CLICKED_ONLY = 0.22        # remainder (0.60) never click
P_ADVOCATE = 0.15            # share of users that ever send invites ("most refer none")
P_ALREADY_MEMBER = 0.03      # unconverted invites that went to someone who already had an account

# Chatbot (the sample DB's Conversation_History). Chatters skew towards buyers (engaged users do more of everything).
P_CHATTER_BUYER = 0.65
P_CHATTER_NONBUYER = 0.35
P_PURCHASE_LINKED_SESSION = 0.10      # a chatter opens the bot shortly before this share of their purchases
P_UNANSWERED = 0.02                   # bot never replied
P_PII_IN_MESSAGE = 0.015              # user types their own e-mail into the chat
SESSION_GAP_RANGE = (15, 150)         # seconds between a bot reply and the user's next message

CHAT_TOPICS = {   # topic -> (weight, user questions, bot answers)
    "small_talk": (0.34, [
        "Why do we go to school?", "Do you sleep?", "What's your favorite color?", "Do you like music?",
        "What's your favorite movie?", "What do you think about school lunches?", "Tell me a joke",
        "Are you a robot?", "What's your favorite food?"],
        ["To learn new things, grow, and prepare for the future!", "I don't sleep, I standby!",
         "I like all colors equally, especially binary ones!", "Beep boop! I groove to the sound of electricity.",
         "I'd say The Matrix, but only because it feels relatable.", "I don't eat, but I hear pizza day is the best!",
         "Why did the cereal box get promoted? It was outstanding in its field!", "Yes, and proud of it."]),
    "points": (0.20, [
        "How many points do I have?", "How do points turn into money for my school?", "When do points expire?",
        "Why didn't I get points for my last purchase?", "How much does my school earn per box?"],
        ["Points are added after each eligible purchase and convert into donations for your school.",
         "Check the Earnings tab: it shows your points and what they have raised.",
         "Points can take a day or two to appear after a purchase."]),
    "school": (0.15, [
        "How do I change my school?", "Can I support two schools?", "How is my school doing?",
        "My school isn't listed, what do I do?"],
        ["You can switch schools in Settings > My School.", "Each account supports one school at a time.",
         "You can see your school's total raised on the home screen."]),
    "product": (0.15, [
        "Which products earn the most?", "Where can I buy Granola Cereal Bars?", "Is this snack eligible?",
        "What's on promotion this week?"],
        ["Look for the Carton Caps badge on eligible products.", "Use the Product Finder to see nearby stores.",
         "This week's promotions are on the Deals tab."]),
    "referral": (0.10, [
        "How do I invite a friend?", "Did my friend's signup count?", "Where is my referral code?",
        "How long do referral bonuses take?"],
        ["Share your code from the Invite screen.", "A referral counts once your friend signs up with your code and buys.",
         "Your referral code is on the Invite screen."]),
    "account": (0.06, [
        "I forgot my password", "How do I update my email?", "How do I delete my account?"],
        ["Use 'Forgot password' on the sign-in screen.", "You can update your email in Settings > Account.",
         "I can connect you with support for that."]),
}

# Share of users who register but never purchase.
P_NEVER_BUYS = 0.18
P_NEVER_BUYS_REFERRED = 0.08  # referred users are warmer (the $5 welcome bonus nudges a first scan)

FIRST_PURCHASE_MEAN_DELAY_DAYS = {"organic": 9.0, "referred": 2.0}


def _weighted_cum(weights: List[float]) -> List[float]:
    return list(accumulate(weights))


class CartonCapsDataGenerator:
    """Generate synthetic data for Carton Caps analytics pipeline."""

    def __init__(self, seed: int = 42, output_dir: str = "./data"):
        """
        Args:
            seed: Random seed for reproducibility
            output_dir: Directory to write output files
        """
        self.seed = seed
        self.rng = random.Random(seed)
        random.seed(seed)  # keep global RNG seeded too, in case callers rely on it
        self.output_dir = output_dir
        self.start_date = datetime(2024, 1, 1)
        self.end_date = datetime(2024, 6, 30, 23, 59, 59)
        self.window_days = (self.end_date.date() - self.start_date.date()).days + 1

        os.makedirs(self.output_dir, exist_ok=True)

        # These will be populated by generate methods
        self.schools: List[Dict] = []
        self.users: List[Dict] = []
        self.products: List[Dict] = []
        self.purchases: List[Dict] = []
        self.referrals: List[Dict] = []
        self.chat_messages: List[Dict] = []

        # Latent (non-exported) attributes, handy for validating the pipeline against ground truth
        self._user_latent: Dict[int, Dict] = {}
        self._dirty_tables: Optional[Dict[str, List[Dict]]] = None

    # ------------------------------------------------------------------ helpers

    def _random_time_of_day(self) -> timedelta:
        hour = self.rng.choices(range(24), weights=HOUR_WEIGHTS)[0]
        return timedelta(hours=hour, minutes=self.rng.randint(0, 59), seconds=self.rng.randint(0, 59))

    def _sample_window_day(self, growth_power: float = 1.0, dow_weighted: bool = True) -> int:
        """Day offset in [0, window_days). growth_power<1 pushes mass toward later days (growth)."""
        for _ in range(50):
            offset = int(self.window_days * (self.rng.random() ** growth_power))
            offset = min(offset, self.window_days - 1)
            if not dow_weighted:
                return offset
            d = self.start_date + timedelta(days=offset)
            w = DOW_WEIGHTS[d.weekday()] * MONTH_WEIGHTS[d.month] / (max(DOW_WEIGHTS) * max(MONTH_WEIGHTS.values()))
            if self.rng.random() < w:
                return offset
        return offset

    # ------------------------------------------------------------------ schools

    def generate_schools(self, n: int = 50) -> List[Dict]:
        """Generate school records."""
        school_types = ['Elementary', 'Middle', 'High']
        school_names = [
            'Sunnydale', 'Springfield', 'Hawkins', 'Hill Valley', 'Twin Peaks',
            'Weyland', 'Yutani', 'Cyberdyne', 'OCP', 'Tyrell',
            'Wonka', 'Duff', 'Brawndo', 'Slurm', 'Soylent',
            'Rivendell', 'Waterdeep', 'Neverwinter', 'Baldur', 'Ravenloft',
            'Ooo', 'Dungeon', 'Candy Kingdom', 'Fire Kingdom', 'Cloud Kingdom',
            'Arrakis', 'Caladan', 'Giedi Prime', 'Kaitain', 'Ix',
            'Veidt', 'Manhattan', 'Rorschach', 'Comedian', 'Nite Owl'
        ]
        max_unique = len(school_names) * len(school_types)

        schools = []
        used_combinations = set()
        for i in range(n):
            # Unique "<name> <type>" while we can; once the name pool is exhausted add a campus number
            for _ in range(200):
                name = self.rng.choice(school_names)
                school_type = self.rng.choice(school_types)
                combination = f"{name} {school_type}"
                if combination not in used_combinations or len(used_combinations) >= max_unique:
                    break
            if len(used_combinations) >= max_unique:
                combination = f"{combination} {len(used_combinations) - max_unique + 2}"
            used_combinations.add(combination)

            schools.append({
                'school_id': i + 1,
                'name': f"{combination} School",
                'address': f"{self.rng.randint(100, 9999)} {self.rng.choice(['Main', 'Oak', 'Elm', 'School'])} St",
                'city': self.rng.choice(['Springfield', 'Riverside', 'Madison', 'Georgetown', 'Fairview']),
                'state': self.rng.choice(['CA', 'TX', 'NY', 'FL', 'IL']),
                'zip_code': f"{self.rng.randint(10000, 99999)}",
                'created_at': (self.start_date - timedelta(days=self.rng.randint(365, 1825))).replace(microsecond=0),
            })

        self.schools = schools
        return schools

    # ------------------------------------------------------------------ users

    def generate_users(self, n: int = 1000, legacy_share: float = 0.40) -> List[Dict]:
        """
        Generate user records.

        ~legacy_share of users signed up before the observation window ("legacy"); the rest sign up
        during the window with a gently growing daily rate (app is publicly available and growing).
        School membership is skewed (log-normal school size) so rankings are interesting.
        """
        if not self.schools:
            raise ValueError("Schools must be generated before users")

        first_names = [
            'James', 'Mary', 'John', 'Patricia', 'Robert', 'Jennifer',
            'Michael', 'Linda', 'William', 'Elizabeth', 'David', 'Barbara',
            'Richard', 'Susan', 'Joseph', 'Jessica', 'Thomas', 'Sarah'
        ]
        last_names = [
            'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia',
            'Miller', 'Davis', 'Rodriguez', 'Martinez', 'Hernandez', 'Lopez'
        ]

        school_ids = [s['school_id'] for s in self.schools]
        school_cum = _weighted_cum([self.rng.lognormvariate(0, 0.7) for _ in school_ids])

        raw = []
        used_emails = set()
        for _ in range(n):
            first = self.rng.choice(first_names)
            last = self.rng.choice(last_names)

            if self.rng.random() < legacy_share:
                created_at = self.start_date - timedelta(days=self.rng.randint(1, 730)) + self._random_time_of_day()
            else:
                offset = self._sample_window_day(growth_power=0.65, dow_weighted=False)
                created_at = self.start_date + timedelta(days=offset) + self._random_time_of_day()

            # Unique e-mail (provided schema: UNIQUE(email))
            suffix = self.rng.randint(1, 999)
            email = f"{first.lower()}.{last.lower()}{suffix}@email.com"
            while email in used_emails:
                suffix += 1
                email = f"{first.lower()}.{last.lower()}{suffix}@email.com"
            used_emails.add(email)

            raw.append({
                'first_name': first,
                'last_name': last,
                'email': email,
                'school_id': self.rng.choices(school_ids, cum_weights=school_cum)[0],
                'created_at': min(created_at, self.end_date).replace(microsecond=0),
                'user_type': self.rng.choices(['parent', 'teacher', 'supporter'], weights=[0.7, 0.2, 0.1])[0],
            })

        raw.sort(key=lambda u: u['created_at'])  # ids are assigned chronologically, like an autoincrement PK
        users = [{'user_id': i + 1, **u} for i, u in enumerate(raw)]
        # keep the scaffold's column order
        users = [{k: u[k] for k in
                  ('user_id', 'first_name', 'last_name', 'email', 'school_id', 'created_at', 'user_type')} for u in users]

        self.users = users
        self._user_latent = {}
        return users

    # ------------------------------------------------------------------ products

    def generate_products(self, n: int = 100) -> List[Dict]:
        """Generate product records."""
        categories = {
            'Breakfast': ['Cereal', 'Oatmeal', 'Granola Bars', 'Yogurt', 'Waffles'],
            'Snacks': ['Chips', 'Crackers', 'Cookies', 'Pretzels', 'Popcorn', "Plate of Shrimp"],
            'Beverages': ['Juice', 'Water', 'Sports Drink', 'Tea', 'Coffee'],
            'Dairy': ['Milk', 'Cheese', 'Butter', 'Ice Cream', 'Cream Cheese'],
            'Pantry': ['Pasta', 'Rice', 'Soup', 'Sauce', 'Oil']
        }

        brands = [
            'Wonka', 'Duff', 'Buzz Cola', 'Krusty', 'Lard Lad', 'Super Porp',
            'Soylent', 'Brawndo', 'Slurm', 'Bachelor Chow', 'Popplers',
            'Weyland-Yutani', 'Cyberdyne', 'OCP', 'Umbrella', 'Initech',
            'Tyrell Corp', 'Replicant Foods', 'Off-World', 'Shimago-Domínguez', 'Atari',
            'Rekall', 'CHOAM', 'Bene Gesserit', 'Harkonnen Industries', 'Atreides Organic',
            'Veidt Enterprises', 'Pyramid Deliveries', 'Nostalgia', 'Gunga Diner', 'Gordian Knot'
        ]

        n_items = sum(len(v) for v in categories.values())
        per_item = max(1, math.ceil(n / n_items))

        products = []
        product_id = 1
        for category, items in categories.items():
            for item in items:
                for _ in range(per_item):
                    products.append({
                        'product_id': product_id,
                        'name': f"{self.rng.choice(brands)} {item}",
                        'category': category,
                        'price': round(self.rng.uniform(1.99, 9.99), 2),
                        'points_per_dollar': self.rng.choice([1, 2, 3, 5]),
                        'created_at': (self.start_date - timedelta(days=self.rng.randint(30, 365))).replace(microsecond=0),
                    })
                    product_id += 1

        self.products = products[:n]  # Ensure we have exactly n products
        return self.products

    # ------------------------------------------------------------------ referrals

    def _referral_code(self, user_id: int) -> str:
        digest = hashlib.sha1(f"{self.seed}-{user_id}".encode()).hexdigest()
        return f"CC{digest[:6].upper()}"

    def generate_referrals(self, n: int = 1000) -> List[Dict]:
        """
        Generate referral network data.

        Model
        -----
        * Only ~15% of users are "advocates" (can send invites); their invite volume follows a
          Pareto tail, so a handful of super-referrers account for a large share of attempts.
        * Each attempt ends in one of: sent (60%), clicked (22%), converted (18%).
        * A converted referral is tied to a user who signs up *during* the window and was not
          referred before ("referred users must be new"). Signup time == converted_at, and the
          invite was sent a log-normally distributed delay earlier (median ~20h, long tail).
        * The referrer must already exist when the invite is sent. Referred users are ordinary
          users, so they can become advocates themselves -> referral chains (depth > 1).
        * 3% of unconverted invites go to e-mails that already belong to a user (ineligible).

        Returns dicts with: referral_id, referrer_user_id, referred_email, referred_user_id,
        referral_code, sent_at, converted_at, status.
        """
        if not self.users:
            raise ValueError("Users must be generated before referrals")

        created = {u['user_id']: u['created_at'] for u in self.users}

        # -- advocates and their (heavy-tailed) invite propensity --------------------------
        type_mult = {'parent': 1.0, 'teacher': 1.5, 'supporter': 0.5}
        adv_ids, adv_w = [], []
        for u in self.users:
            if self.rng.random() < P_ADVOCATE:
                adv_ids.append(u['user_id'])
                adv_w.append(min(self.rng.paretovariate(1.2), 60.0) * type_mult[u['user_type']])
        if not adv_ids:  # tiny test populations
            adv_ids = [self.users[0]['user_id']]
            adv_w = [1.0]
        adv_cum = _weighted_cum(adv_w)

        def pick_referrer(sent_at: datetime, exclude: Optional[int] = None) -> Optional[int]:
            # rejection-sample from the global weighted advocate list: referrer must predate the invite
            for _ in range(200):
                uid = self.rng.choices(adv_ids, cum_weights=adv_cum)[0]
                if created[uid] < sent_at and uid != exclude:
                    return uid
            older = [uid for uid in adv_ids if created[uid] < sent_at and uid != exclude]
            return self.rng.choice(older) if older else None

        # -- decide outcomes ---------------------------------------------------------------
        statuses = self.rng.choices(
            ['converted', 'clicked', 'sent'],
            weights=[P_CONVERTED, P_CLICKED_ONLY, 1 - P_CONVERTED - P_CLICKED_ONLY], k=n)

        # candidates to be referred: window signups, each can be referred at most once
        candidates = [u for u in self.users if u['created_at'] >= self.start_date]
        self.rng.shuffle(candidates)

        rows: List[Dict] = []
        referred_so_far = set()
        invited_emails = set()

        for status in statuses:
            if status == 'converted':
                if not candidates:
                    status = 'clicked'  # ran out of signups to convert (small populations)
                else:
                    referred = candidates.pop()
                    converted_at = referred['created_at']
                    delay = timedelta(hours=min(self.rng.lognormvariate(math.log(20), 1.0), 21 * 24))
                    sent_at = converted_at - delay
                    if sent_at < self.start_date:  # invites before the window are out of scope
                        span = (converted_at - self.start_date).total_seconds()
                        sent_at = converted_at - timedelta(seconds=span * self.rng.uniform(0.1, 0.9))
                    sent_at = sent_at.replace(microsecond=0)
                    referrer = pick_referrer(sent_at, exclude=referred['user_id'])
                    if referrer is None:
                        continue
                    referred_so_far.add(referred['user_id'])
                    rows.append({
                        'referrer_user_id': referrer,
                        'referred_email': referred['email'],
                        'referred_user_id': referred['user_id'],
                        'referral_code': self._referral_code(referrer),
                        'sent_at': sent_at,
                        'converted_at': converted_at,
                        'status': 'converted',
                    })
                    continue

            # unconverted attempts (sent / clicked)
            day = self._sample_window_day(growth_power=0.8)
            sent_at = (self.start_date + timedelta(days=day) + self._random_time_of_day()).replace(microsecond=0)
            referrer = pick_referrer(min(sent_at, self.end_date))
            if referrer is None:
                continue
            if self.rng.random() < P_ALREADY_MEMBER:
                existing = [u for u in self.rng.sample(self.users, k=min(5, len(self.users)))
                            if u['created_at'] < sent_at and u['user_id'] != referrer]
                email = existing[0]['email'] if existing else None
            else:
                email = None
            if email is None:
                email = f"friend{len(invited_emails) + 1}.{self.rng.randint(1000, 9999)}@invite.example"
                while email in invited_emails:
                    email = f"friend{len(invited_emails) + 1}.{self.rng.randint(1000, 9999)}@invite.example"
            invited_emails.add(email)
            rows.append({
                'referrer_user_id': referrer,
                'referred_email': email,
                'referred_user_id': None,
                'referral_code': self._referral_code(referrer),
                'sent_at': sent_at,
                'converted_at': None,
                'status': status,
            })

        rows.sort(key=lambda r: r['sent_at'])
        referrals = [{'referral_id': i + 1, **r} for i, r in enumerate(rows)]
        self.referrals = referrals
        return referrals

    # ------------------------------------------------------------------ purchases

    def generate_purchases(self, n: int = 10000) -> List[Dict]:
        """
        Generate purchase records with realistic patterns.

        Model
        -----
        * Volume: each user gets a heavy-tailed weight = Pareto(2.2) x segment multiplier x user-type
          multiplier. ``n`` purchases are allocated across users by those weights (multinomial), so a
          small share of users produce most purchases. 18% of users never buy.
        * Lifecycle: first purchase happens after signup (exponential delay; 2d for referred users, 9d
          for organic; legacy users are already active at the window start). After that, purchase
          times follow a truncated exponential with a segment-specific mean lifetime -> natural churn
          and cohort retention decay. Late signups get proportionally fewer purchases (shorter exposure).
        * Time: accept/reject on weekday and month multipliers; hour of day drawn from an
          evening-peaked profile.
        * Products: user-type category affinity x a per-user favourite category x Zipf product
          popularity x a price-sensitivity factor (cheap items favoured by price-sensitive users).
        * Price: 8% base chance of a 10-30% promo discount, up to 28% for price-sensitive users.
        * Quantity: mostly 1-2, long tail up to 6, larger for power users.

        Fields: purchase_id, user_id, product_id, quantity, price_paid (total for the line, after any
        discount), points_earned, purchased_at, day_of_week (0=Mon), hour_of_day.
        """
        if not self.users or not self.products:
            raise ValueError("Users and products must be generated before purchases")

        rng = self.rng
        referred_ids = {r['referred_user_id'] for r in self.referrals if r['referred_user_id'] is not None}
        categories = sorted({p['category'] for p in self.products})

        # global product popularity (Zipf over a random ranking)
        ranks = list(range(1, len(self.products) + 1))
        rng.shuffle(ranks)
        popularity = {p['product_id']: 1.0 / (ranks[i] ** 0.7) for i, p in enumerate(self.products)}

        seg_names = list(SEGMENTS)
        seg_shares = [SEGMENTS[s]['share'] for s in seg_names]

        # -- per-user latent attributes ---------------------------------------------------------
        buyers, alloc_w, first_times = [], [], []
        for u in self.users:
            uid = u['user_id']
            referred = uid in referred_ids
            seg = rng.choices(seg_names, weights=seg_shares)[0]
            sens = rng.betavariate(2, 3)
            fav_cat = rng.choice(categories)
            latent = {'segment': seg, 'price_sensitivity': sens, 'favourite_category': fav_cat,
                      'referred': referred, 'buyer': False}
            self._user_latent[uid] = latent

            if rng.random() < (P_NEVER_BUYS_REFERRED if referred else P_NEVER_BUYS):
                continue

            # first purchase time
            if u['created_at'] < self.start_date:
                t0 = self.start_date + timedelta(days=rng.expovariate(1 / 4.0))   # already-active legacy user: first in-window purchase follows their normal cadence
            else:
                mean = FIRST_PURCHASE_MEAN_DELAY_DAYS['referred' if referred else 'organic']
                t0 = u['created_at'] + timedelta(days=rng.expovariate(1 / mean), minutes=rng.randint(5, 120))
            if t0 > self.end_date:
                t0 = min(u['created_at'] + timedelta(minutes=rng.randint(5, 120)), self.end_date)

            tau = SEGMENTS[seg]['tau_days']
            remaining = max((self.end_date - t0).total_seconds() / 86400, 0.01)
            w = (min(rng.paretovariate(2.2), 40.0) * SEGMENTS[seg]['mult']
                 * USER_TYPE_BUY_MULT[u['user_type']] * (1 - math.exp(-remaining / tau)))
            latent['buyer'] = True
            latent['tau'] = tau
            buyers.append(u)
            alloc_w.append(w)
            first_times.append(t0)

        if not buyers:
            self.purchases = []
            return self.purchases

        counts = Counter(rng.choices(range(len(buyers)), cum_weights=_weighted_cum(alloc_w), k=n))

        max_w = max(DOW_WEIGHTS) * max(MONTH_WEIGHTS.values())
        rows: List[Dict] = []

        for idx, k in counts.items():
            u = buyers[idx]
            uid = u['user_id']
            lat = self._user_latent[uid]
            t0 = first_times[idx]
            tau = lat['tau']
            remaining = max((self.end_date - t0).total_seconds() / 86400, 0.01)
            trunc = 1 - math.exp(-remaining / tau)

            # product choice distribution for this user
            aff = CATEGORY_AFFINITY[u['user_type']]
            pw = []
            for p in self.products:
                cat_w = aff[p['category']] * (3.0 if p['category'] == lat['favourite_category'] else 1.0)
                price_f = math.exp(-lat['price_sensitivity'] * (p['price'] - 5.9) / 2.5)
                pw.append(popularity[p['product_id']] * cat_w * price_f)
            pcum = _weighted_cum(pw)
            chosen = rng.choices(self.products, cum_weights=pcum, k=k)

            promo_p = 0.08 + 0.20 * lat['price_sensitivity']
            qty_w = [0.35, 0.28, 0.17, 0.10, 0.06, 0.04] if lat['segment'] == 'power' else [0.60, 0.23, 0.10, 0.04, 0.02, 0.01]

            for j, prod in enumerate(chosen):
                if j == 0:
                    ts = t0
                else:
                    for _ in range(25):  # accept/reject on weekday + month seasonality
                        off = -tau * math.log(1 - rng.random() * trunc)
                        cand = t0 + timedelta(days=off)
                        if cand > self.end_date:
                            continue
                        wt = DOW_WEIGHTS[cand.weekday()] * MONTH_WEIGHTS[cand.month]
                        if rng.random() < wt / max_w:
                            break
                    else:
                        cand = t0 + timedelta(days=rng.random() * remaining)
                    ts = cand.replace(hour=0, minute=0, second=0, microsecond=0) + self._random_time_of_day()
                    if ts < t0:
                        ts = t0 + timedelta(minutes=rng.randint(1, 600))
                ts = min(ts, self.end_date)
                ts = max(ts, u['created_at'] + timedelta(minutes=1))
                ts = ts.replace(microsecond=0)

                qty = rng.choices([1, 2, 3, 4, 5, 6], weights=qty_w)[0]
                discount = rng.choice([0.10, 0.15, 0.20, 0.25, 0.30]) if rng.random() < promo_p else 0.0
                price_paid = round(prod['price'] * qty * (1 - discount), 2)
                rows.append({
                    'user_id': uid,
                    'product_id': prod['product_id'],
                    'quantity': qty,
                    'price_paid': price_paid,
                    'points_earned': int(round(price_paid * prod['points_per_dollar'])),
                    'purchased_at': ts,
                    'day_of_week': ts.weekday(),
                    'hour_of_day': ts.hour,
                })

        rows.sort(key=lambda r: (r['purchased_at'], r['user_id']))
        self.purchases = [{'purchase_id': i + 1, **r} for i, r in enumerate(rows)]
        return self.purchases


    # ------------------------------------------------------------------ chatbot

    def generate_chat_messages(self, chatter_scale: float = 1.0) -> List[Dict]:
        """
        Generate in-app chatbot conversations (the sample database's ``Conversation_History``).

        Model
        -----
        * Who chats: 65% of buyers and 35% of non-buyers (x ``chatter_scale``) -> chat usage correlates
          with buying, as engaged users do everything more. This is *planted*, not discovered.
        * When: each chatter has 1 + lognormal sessions, front-loaded after signup (exponential, mean
          35 days: people ask questions when they are new), evening-peaked hours.
        * Purchase-linked: 10% of a chatter's purchases are preceded by a session 3-60 min earlier.
        * A session is 1-12 user messages, each answered by the bot 2-8 s later (2% go unanswered),
          next question 15-150 s after the reply. Topics: small talk, points, school, product,
          referral, account. 1.5% of user messages contain the user's own e-mail address (PII demo).

        Uses its own RNG stream so adding or tuning chat never changes any other table.
        Fields: message_id, user_id, message, sender ('user'|'bot'), sent_at.
        """
        if not self.users:
            raise ValueError("Users must be generated before chat messages")
        rng = random.Random(self.seed + 7)
        topics = list(CHAT_TOPICS)
        topic_w = [CHAT_TOPICS[t][0] for t in topics]
        hour_cum = _weighted_cum(HOUR_WEIGHTS)
        buyers = {r['user_id'] for r in self.purchases}
        purchases_by_user: Dict[int, List[datetime]] = {}
        for r in self.purchases:
            purchases_by_user.setdefault(r['user_id'], []).append(r['purchased_at'])

        rows: List[Dict] = []

        def session(uid: int, start: datetime) -> None:
            t = start
            for _ in range(min(12, 1 + int(rng.expovariate(1 / 2.2)))):
                topic = rng.choices(topics, weights=topic_w)[0]
                _, questions, answers = CHAT_TOPICS[topic]
                text = rng.choice(questions)
                if rng.random() < P_PII_IN_MESSAGE:
                    text = f"{text} my email is user{uid}@example.com"
                rows.append({'user_id': uid, 'message': text, 'sender': 'user', 'sent_at': t})
                if rng.random() >= P_UNANSWERED:
                    t = t + timedelta(seconds=rng.randint(2, 8))
                    rows.append({'user_id': uid, 'message': rng.choice(answers), 'sender': 'bot', 'sent_at': t})
                t = t + timedelta(seconds=rng.randint(*SESSION_GAP_RANGE))

        for u in self.users:
            uid = u['user_id']
            p = (P_CHATTER_BUYER if uid in buyers else P_CHATTER_NONBUYER) * chatter_scale
            if rng.random() >= min(p, 1.0):
                continue
            seg = self._user_latent.get(uid, {}).get('segment', 'casual')
            floor = max(u['created_at'], self.start_date)
            span_days = max((self.end_date - floor).total_seconds() / 86400, 0.01)
            n_sessions = min(40, 1 + int(rng.lognormvariate(0.5, 0.9) * (1.5 if seg == 'power' else 1.0)))
            for _ in range(n_sessions):
                day = floor + timedelta(days=min(rng.expovariate(1 / 35.0), span_days * rng.random()))
                hour = bisect.bisect_left(hour_cum, rng.random() * hour_cum[-1])
                start = day.replace(hour=min(hour, 23), minute=rng.randint(0, 59), second=rng.randint(0, 59),
                                    microsecond=0)
                start = max(start, u['created_at'] + timedelta(minutes=1))
                if start < self.end_date:
                    session(uid, start)
            for ts in purchases_by_user.get(uid, []):
                if rng.random() < P_PURCHASE_LINKED_SESSION:
                    start = ts - timedelta(minutes=rng.randint(3, 60))
                    if start > u['created_at']:
                        session(uid, start)

        rows = [r for r in rows if r['sent_at'] <= self.end_date]
        order = {'user': 0, 'bot': 1}
        rows.sort(key=lambda r: (r['sent_at'], r['user_id'], order[r['sender']]))
        self.chat_messages = [{'message_id': i + 1, **r} for i, r in enumerate(rows)]
        return self.chat_messages

    # ------------------------------------------------------------------ dirty data

    def make_dirty(self, rate: float = 0.01) -> Dict[str, List[Dict]]:
        """
        Return *copies* of the tables with realistic data-quality problems injected at ``rate`` per
        issue type. Ground-truth tables (self.users, ...) stay clean.

        Injected issues (the pipeline's DQ layer is expected to catch each of these):
          users:      malformed e-mail, orphan school_id
          purchases:  exact duplicate row, non-positive quantity, orphan user_id, NULL price,
                      unparseable timestamp, purchase before signup, wrong day_of_week/hour (derived-field drift)
          referrals:  self-referral, converted w/o converted_at, status with stray case/whitespace (fixable),
                      converted_at earlier than sent_at, referred_user_id pointing to a missing user
        """
        rng = random.Random(self.seed + 1)
        t = {
            'schools': copy.deepcopy(self.schools),
            'users': copy.deepcopy(self.users),
            'products': copy.deepcopy(self.products),
            'purchases': copy.deepcopy(self.purchases),
            'referrals': copy.deepcopy(self.referrals),
            'chat_messages': copy.deepcopy(self.chat_messages),
        }
        self.dirty_manifest = Counter()

        def sample(rows, extra=1.0):
            k = max(1, int(len(rows) * rate * extra)) if rows else 0
            return rng.sample(range(len(rows)), min(k, len(rows)))

        for i in sample(t['users']):
            t['users'][i]['email'] = t['users'][i]['email'].replace('@', '')
            self.dirty_manifest['users.bad_email'] += 1
        for i in sample(t['users']):
            t['users'][i]['school_id'] = 99999
            self.dirty_manifest['users.orphan_school'] += 1

        p = t['purchases']
        dup = [copy.deepcopy(p[i]) for i in sample(p)]
        self.dirty_manifest['purchases.duplicate'] += len(dup)
        for i in sample(p):
            p[i]['quantity'] = rng.choice([0, -1, -3]); self.dirty_manifest['purchases.bad_quantity'] += 1
        for i in sample(p):
            p[i]['user_id'] = 9_999_999; self.dirty_manifest['purchases.orphan_user'] += 1
        for i in sample(p):
            p[i]['price_paid'] = None; self.dirty_manifest['purchases.null_price'] += 1
        for i in sample(p):
            p[i]['purchased_at'] = 'not-a-date'; self.dirty_manifest['purchases.bad_timestamp'] += 1
        users_by_id = {u['user_id']: u for u in self.users}
        for i in sample(p):
            u = users_by_id.get(p[i]['user_id'])
            if u and isinstance(p[i]['purchased_at'], datetime):
                p[i]['purchased_at'] = u['created_at'] - timedelta(days=rng.randint(1, 30))
                self.dirty_manifest['purchases.before_signup'] += 1
        for i in sample(p):
            p[i]['day_of_week'] = (p[i]['day_of_week'] + 3) % 7; self.dirty_manifest['purchases.derived_drift'] += 1
        p.extend(dup)

        r = t['referrals']
        for i in sample(r):
            r[i]['referred_user_id'] = r[i]['referrer_user_id']
            r[i]['status'] = 'converted'
            r[i]['converted_at'] = r[i]['sent_at'] + timedelta(hours=1)
            self.dirty_manifest['referrals.self_referral'] += 1
        for i in sample(r):
            if r[i]['status'] == 'converted' and r[i]['converted_at'] is not None:
                r[i]['converted_at'] = None; self.dirty_manifest['referrals.converted_without_ts'] += 1
        for i in sample(r, 3):
            r[i]['status'] = f" {r[i]['status'].upper()} "; self.dirty_manifest['referrals.status_format'] += 1
        for i in sample(r):
            if r[i]['converted_at'] is not None and isinstance(r[i]['converted_at'], datetime):
                r[i]['converted_at'] = r[i]['sent_at'] - timedelta(hours=5)
                self.dirty_manifest['referrals.converted_before_sent'] += 1
        for i in sample(r):
            if r[i]['referred_user_id'] is not None:
                r[i]['referred_user_id'] = 8_888_888; self.dirty_manifest['referrals.orphan_referred'] += 1

        c = t['chat_messages']
        dup_c = [copy.deepcopy(c[i]) for i in sample(c)]
        self.dirty_manifest['chat_messages.duplicate'] += len(dup_c)
        for i in sample(c):
            c[i]['sender'] = rng.choice(['admin', 'system', 'USER ']) if rng.random() < 0.5 else ' BOT'
            self.dirty_manifest['chat_messages.sender_format_or_invalid'] += 1
        for i in sample(c):
            c[i]['user_id'] = 9_999_999; self.dirty_manifest['chat_messages.orphan_user'] += 1
        for i in sample(c):
            c[i]['message'] = '   '; self.dirty_manifest['chat_messages.empty_message'] += 1
        for i in sample(c):
            c[i]['sent_at'] = 'not-a-date'; self.dirty_manifest['chat_messages.bad_timestamp'] += 1
        for i in sample(c):
            u = users_by_id.get(c[i]['user_id'])
            if u and isinstance(c[i]['sent_at'], datetime):
                c[i]['sent_at'] = u['created_at'] - timedelta(days=rng.randint(1, 30))
                self.dirty_manifest['chat_messages.before_signup'] += 1
        c.extend(dup_c)

        return t

    # ------------------------------------------------------------------ persistence

    @staticmethod
    def _fmt(value):
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d %H:%M:%S")
        return value

    def _output_tables(self) -> Dict[str, List[Dict]]:
        if self._dirty_tables is not None:
            return self._dirty_tables
        return {'schools': self.schools, 'users': self.users, 'products': self.products,
                'purchases': self.purchases, 'referrals': self.referrals, 'chat_messages': self.chat_messages}

    def save_to_csv(self, data: List[Dict], filename: str) -> None:
        """Save data to CSV file (datetimes as 'YYYY-MM-DD HH:MM:SS', NULL as empty string)."""
        if not data:
            print(f"Warning: No data to save for {filename}")
            return

        filepath = os.path.join(self.output_dir, filename)
        with open(filepath, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=list(data[0].keys()))
            writer.writeheader()
            for row in data:
                writer.writerow({k: self._fmt(v) for k, v in row.items()})

        print(f"Saved {len(data)} records to {filepath}")

    def save_to_sqlite(self, db_path: str | None = None) -> None:
        """Save all generated data to a (re-created) SQLite database."""
        if db_path is None:
            db_path = os.path.join(self.output_dir, "carton_caps_generated.db")
        if os.path.exists(db_path):
            os.remove(db_path)  # idempotent re-runs

        conn = sqlite3.connect(db_path)

        def create_and_insert(table_name: str, data: List[Dict]):
            if not data:
                return
            cols = list(data[0].keys())
            col_defs = []
            for c in cols:
                sample = next((r[c] for r in data if r.get(c) is not None), None)
                if isinstance(sample, bool) or isinstance(sample, int):
                    col_type = "INTEGER"
                elif isinstance(sample, float):
                    col_type = "REAL"
                elif isinstance(sample, datetime):
                    col_type = "TIMESTAMP"
                else:
                    col_type = "TEXT"
                col_defs.append(f"{c} {col_type}")
            conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} ({', '.join(col_defs)})")
            conn.executemany(
                f"INSERT INTO {table_name} VALUES ({', '.join('?' for _ in cols)})",
                [[self._fmt(r[c]) for c in cols] for r in data])

        for name, rows in self._output_tables().items():
            create_and_insert(name, rows)

        conn.commit()
        conn.close()
        print(f"Saved all data to {db_path}")

    def generate_all(self,
                     n_schools: int = 50,
                     n_users: int = 1000,
                     n_products: int = 100,
                     n_purchases: int = 10000,
                     n_referrals: int = 1000,
                     dirty_rate: float = 0.0,
                     chatter_scale: float = 1.0) -> None:
        """Generate all datasets and save to files."""
        print("Generating Carton Caps data...")
        print("-" * 40)

        print(f"Generating {n_schools} schools...")
        self.generate_schools(n_schools)
        print(f"Generating {n_users} users...")
        self.generate_users(n_users)
        print(f"Generating {n_products} products...")
        self.generate_products(n_products)
        # Referrals before purchases: referred users activate faster, so purchases need to know them.
        print(f"Generating {n_referrals} referrals...")
        self.generate_referrals(n_referrals)
        print(f"Generating {n_purchases} purchases...")
        self.generate_purchases(n_purchases)
        print("Generating chatbot conversations...")
        self.generate_chat_messages(chatter_scale)
        print(f"  {len(self.chat_messages)} messages")

        if dirty_rate > 0:
            print(f"Injecting data-quality problems at rate {dirty_rate:.3%} ...")
            self._dirty_tables = self.make_dirty(dirty_rate)
            for k, v in sorted(self.dirty_manifest.items()):
                print(f"  injected {v:>5}  {k}")
        else:
            self._dirty_tables = None

        for name, rows in self._output_tables().items():
            self.save_to_csv(rows, f"{name}.csv")

        print("\nSaving to SQLite database...")
        self.save_to_sqlite()

        print("-" * 40)
        print("Data generation complete!")


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate Carton Caps synthetic data")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--output-dir", default="./data")
    ap.add_argument("--schools", type=int, default=50)
    ap.add_argument("--users", type=int, default=1000)
    ap.add_argument("--products", type=int, default=100)
    ap.add_argument("--purchases", type=int, default=10000)
    ap.add_argument("--referrals", type=int, default=1000)
    ap.add_argument("--chatter-scale", type=float, default=1.0,
                    help="multiply the share of users who use the chatbot (0 disables chat)")
    ap.add_argument("--dirty-rate", type=float, default=0.0,
                    help="fraction of rows (per issue type) to corrupt, e.g. 0.01")
    a = ap.parse_args()

    CartonCapsDataGenerator(seed=a.seed, output_dir=a.output_dir).generate_all(
        n_schools=a.schools, n_users=a.users, n_products=a.products,
        n_purchases=a.purchases, n_referrals=a.referrals, dirty_rate=a.dirty_rate,
        chatter_scale=a.chatter_scale)


if __name__ == "__main__":
    main()
