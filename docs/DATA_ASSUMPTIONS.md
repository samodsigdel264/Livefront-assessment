# Data generation: assumptions, inferred rules, simplifications

*Deliverable 1. Code: [`data_generator.py`](../data_generator.py). Every number below is a named constant at the top of that file, so any of them can be changed and the data regenerated from the same seed.*

## 1. What was generated

`python data_generator.py` (seed 42) writes `data/{schools,users,products,purchases,referrals,chat_messages}.csv` plus `data/carton_caps_generated.db` (same tables in SQLite).

| Table | Rows | Notes |
|---|---:|---|
| schools | 50 | 5 states; school size is log-normal, so a few schools are large |
| users | 1,000 | ~40% "legacy" (signed up before 2024-01-01; 37.6% realised), the rest sign up inside the window with a rising daily rate |
| products | 100 | 5 categories x 5-6 items x 4 variants |
| purchases | 10,000 | 2024-01-01 .. 2024-06-30 |
| referrals | 1,000 | invite attempts; 17.9% convert |
| chat_messages | 13,705 | in-app chatbot, modelled on the sample DB's `Conversation_History`: 579 users (58%), 2,516 sessions, 6,921 user messages and 6,784 bot replies |

Observation window: **2024-01-01 to 2024-06-30** (the scaffold's dates, 182 days).

## 2. Changes to the provided scaffold (and why)

The brief says the generator is "partially implemented". I implemented the two `NotImplementedError` methods and made these changes to the rest, each because the pipeline can't be exercised without it:

| Change | Reason |
|---|---|
| **Users now sign up across the window.** The scaffold placed every user 0-365 days *before* `start_date`. | With no in-window signups there are no new cohorts to measure retention on, and no one a referral can convert into ("referred users must be new"). |
| **E-mails are unique.** | The scaffold's `random.randint(1, 999)` suffix collides (about 2 expected collisions in 1,000 users). The provided SQLite schema declares `UNIQUE(email)`, so the scaffold's data would not even load there. |
| **School membership is skewed** (log-normal weights) instead of uniform. | A uniform split makes every school look alike and the rankings meaningless. |
| **Referrals are generated before purchases** in `generate_all`. | Referred users activate faster (see 4.1), so purchase generation needs to know who they are. |
| `n // 25` product fan-out replaced with `ceil(n / 25)` | The scaffold silently returned fewer than `n` products for n not divisible by 25. |
| `save_to_sqlite` deletes the old file first and infers column types from the first non-NULL value. | Re-running used to append duplicate rows; a NULL in the first row typed the column as TEXT. |
| Optional `--dirty-rate` | Injects realistic data-quality problems (section 6) so the DQ layer has something to catch. Off by default. |

### Mismatch between the provided SQLite file and the generator

`Carton_Caps_Data.sqlite` (Schools / Users / Products / Purchase_History / Conversation_History, columns `id`, `name`, ...) does **not** use the schema the generator emits (`schools.school_id`, `purchases.price_paid`, `referrals`, ...). The brief tells us to complete the generator, so **the generator's schema is the pipeline's input contract**, and `pipeline/ingest.py::SampleSchemaSource` is a thin adapter that maps the sample file onto it (`python -m pipeline.run --sqlite Carton_Caps_Data.sqlite`). What the adapter has to invent, because the sample has no counterpart: `first_name`/`last_name` (split `name` on the first space), `user_type` (absent -> `unknown`), product `category` (keywords in the product name) and `points_per_dollar` (2), `price_paid` (= quantity x list price), and `city`/`state`/`zip_code` (parsed from `address` when it ends `City, ST 12345`). The sample has 34 users, all in 2025, and zero purchases; its 484 `Conversation_History` rows are the model for the generated `chat_messages` below.

## 3. Purchases

Goal: look like consumer behaviour (skewed, habitual, seasonal), so the aggregations have something real to find.

**Volume - power law.** Each user gets a weight `Pareto(2.2) x segment multiplier x user-type multiplier`. The 10,000 purchases are then allocated across users by those weights (multinomial), so the total is exact.

| Segment | Share of users | Volume multiplier | Mean active lifetime |
|---|---:|---:|---:|
| casual | 55% | 1x | 25 days |
| regular | 33% | 3x | 70 days |
| power | 12% | 8x | 220 days |

User-type multipliers: parent 1.0, teacher 0.8, supporter 0.6. **18% of users never purchase** (8% of referred users; 18.4% realised overall). Result: the heaviest 10% of buying users make **36.5%** of purchases.

**Lifecycle / churn.** First purchase = signup + exponential delay (mean 9 days organic, 2 days referred; legacy users start from the beginning of the window). Later purchases follow a truncated exponential with the segment's lifetime as the scale, which produces natural cohort retention decay. The allocation weight is multiplied by `1 - exp(-remaining_days / lifetime)`, so late signups get proportionally fewer purchases instead of an unrealistic burst. No purchase is ever earlier than the user's signup.

**Time patterns**
- Day of week (Mon..Sun): 0.90, 0.85, 0.90, 0.95, 1.10, **1.35, 1.20** (weekend peak), applied by accept/reject sampling.
- Hour of day: small 7-9am bump (school run), strong 5-8pm peak, almost nothing 1-5am.
- Month: Jan 1.00, Feb 0.95, Mar 1.05, Apr 1.00, **May 1.15** (end-of-year school push), **Jun 0.80** (summer).
- January is the busiest month in the output mostly because all legacy users are "alive" at the start of the window and then churn; this is the cohort effect, not seasonality.

**Products.** Weight = Zipf(0.7) global popularity x user-type category affinity x 3x boost for a per-user favourite category x a price-sensitivity factor. Teachers lean to beverages and snacks (classroom), supporters to pantry and dairy, parents to breakfast, snacks and dairy.

**Price & points.** `price_sensitivity ~ Beta(2, 3)` per user. A purchase has an 8% + 20% x sensitivity chance of a 10-30% promo discount. `price_paid` is the **line total after discount** (`list price x quantity x (1 - discount)`); `points_earned = round(price_paid x points_per_dollar)`. Quantity: 60% x1, 23% x2, ... tail to 6; power users buy bigger baskets.

`day_of_week` is 0 = Monday (Python `weekday()`); `hour_of_day` is 0-23. The pipeline re-derives both from `purchased_at` and treats the source columns as untrusted.

## 4. Referrals

**Funnel per attempt:** 60% `sent`, 22% `clicked`, 18% `converted` (realised 17.9%, inside the brief's 10-30% band). Each conversion implies a click, so click-through is 41% of eligible invites and click-to-signup 44%.

**Who refers.** Only 15% of users are "advocates" (the rest never send an invite). Advocates' invite propensity is `Pareto(1.2)` capped at 60, multiplied by user type (teacher 1.5, parent 1.0, supporter 0.5). In the output 126 users sent invites; the top referrer sent 197 invites, the median referrer 3.5.

**Who converts, and when.** A converted referral is tied to a user who signed up *during* the window and had not been referred before, with `converted_at == user.created_at`. The invite was sent a log-normally distributed delay earlier (median ~20 h, long tail, capped at 21 days; invites before the window are out of scope). The referrer must already exist at `sent_at`.

**Chains.** Referred users are ordinary users and can themselves be advocates, so referral chains emerge: 61 seed users, 166 users referred by a seed, 13 referred by a referred user (depth 2).

**Ineligible invites.** 3% of unconverted invites go to e-mails that already belong to a user (the rules PDF says referred users must be new); the pipeline flags these `is_existing_member` and excludes them from the funnel.

## 4b. Chatbot messages

The sample database's only populated behavioural table is `Conversation_History` (a user question, a canned bot answer a few seconds later, `sender` in `user|bot`), so the generator produces the same shape, with real structure behind it. It is generated **last, from its own random stream**, so adding or tuning chat never changes any other table.

- **Who chats:** 65% of buyers and 35% of non-buyers (58% overall). Chat usage therefore *correlates* with buying by construction, as engaged users do more of everything. `mart_chat_engagement` recovers that gap (89% of chatters have bought vs 71% of the rest); it is planted, not a finding, and the report says so.
- **When:** each chatter has 1 + lognormal sessions, front-loaded after signup (exponential, mean 35 days, because people ask questions while they are new) and evening-peaked in hour of day. Power users chat 1.5x more.
- **Purchase-linked:** 10% of a chatter's purchases are preceded by a session 3-60 minutes earlier.
- **A session:** 1-12 user messages (mean about 3), each answered 2-8 s later (2% go unanswered), the next question 15-150 s after the reply. Median chatter sends 9 messages in the window, the heaviest 102.
- **Content:** six topics (small talk modelled on the sample, points, school, product, referral, account) with canned answers. Text is simple on purpose: no metric reads it.
- **PII:** 1.5% of user messages contain the user's own e-mail address, so the pipeline's PII flag has something to find (95 in the default run).
- **Simplification:** a session that crosses midnight counts once on each day (`fct_user_daily_activity` is per calendar day).

## 5. Business rules inferred from `Carton_Caps_Referral_Program_Rules.pdf`

The brief says referral logic need not be implemented, only used as inspiration. The pipeline does use three of the rules for *measurement*:

| Rule in the PDF | How it is used |
|---|---|
| s1 "Referred users must be new" | Generator only converts new signups; DQ warns (`referred_not_new`); invites to existing members are excluded from the funnel |
| s3 Bonus of "$5 or equivalent" for referrer **and** referred, after "a qualifying action (e.g. first eligible product scan)" | `REFERRAL_BONUS_USD = 5` each side. **Qualifying action = first purchase within 7 days of signup**, because purchases are the only event we have (`QUALIFY_WINDOW_DAYS`, configurable). `is_qualified` drives bonus cost and school credit |
| s3 "Bonuses ... may be limited to school donations" | Bonuses are credited to the referrer's and the referred user's school in `mart_school_fundraising.referral_bonus_usd` |
| s5 "Self-referrals are not allowed (same device, email, ...)" | DQ error `self_referral` (same user id, or referred e-mail equals the referrer's e-mail). Device and payment method are not in the data |
| s2 48-hour window to use a code after install | Not modelled: we have no install event. Signup time is the conversion time |

### Rules I invented because the data doesn't say

| Assumption | Value | Where |
|---|---|---|
| Points -> dollars | **1 point = $0.01** (donation = points x 1 cent) | `Settings.point_value_usd` |
| "Active user" | made >= 1 purchase **or** sent >= 1 chatbot message that day | `fct_user_daily_activity` -> `agg_daily_active_users_by_school`, `mart_daily_platform_activity`. The buying and chatting parts are reported separately; app opens / scans are not in the data. Cohort *retention* still means "bought in period N" (purchases fund schools) |
| Observation start | first date seen in the data (here 2024-01-01); users created earlier are `legacy` | `dim_user.is_legacy` |
| Timestamps | naive, assumed UTC | |
| Currency | USD | |

Because the cent-per-point rate is invented, the **split** between purchase donations (about $2.0k) and referral bonuses (about $1.5k) in the school totals is an artefact of two made-up constants; the pipeline reports the two components separately for that reason.

## 6. Injected data-quality problems (`--dirty-rate 0.01`)

Applied to *copies* of the tables, so ground truth is untouched. At 1% per issue type (about 1,600 injected problems across the six files: 100 per type on purchases, 137 per type on chat messages):

| Table | Problem |
|---|---|
| users | e-mail without `@`; `school_id` pointing at a non-existent school |
| purchases | exact duplicate rows; quantity 0 / negative; user_id not in users; NULL `price_paid`; unparseable `purchased_at`; purchase *before* the user signed up; wrong `day_of_week` |
| chat_messages | exact duplicate rows; sender `admin`/`system` (invalid) or `USER `/` BOT` (fixable); user_id not in users; blank message; unparseable `sent_at`; message sent *before* the user signed up |
| referrals | self-referral; `converted` with no timestamp; `converted_at` before `sent_at`; `referred_user_id` that doesn't exist; status with stray case/whitespace (fixable rather than rejectable) |

Every one of these is asserted by a test or visible in `meta.dq_results` after a run.

## 7. Simplifications

- Two event types (purchase, chatbot message); no app opens / scans, returns / refunds / cancellations, so no deletes or negative adjustments.

- Users never change school or user type over time (no slowly-changing dimensions).
- No fraud patterns beyond self-referral (no rings, bots, device sharing).
- No time zones; all schools share one calendar. No public holidays or school-calendar breaks (spring break, summer start) beyond the monthly multipliers.
- Names, addresses and cities are random and not mutually consistent (a school in "Springfield, CA" with a random zip code). They do not affect any metric.
- Referred users are better by construction (faster activation, lower never-buyer rate). **The pipeline reporting that referred users activate faster (`mart_referral_effectiveness`) is therefore a check that the pipeline recovers the planted effect, not a discovery.**
- Right-censoring is real: users who joined in June have had little time to buy. `mart_referral_effectiveness` only includes users with >= 14 days of history; cohort tables flag the partial last period.
