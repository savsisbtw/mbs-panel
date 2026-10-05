import datetime
import os
import sys
import tempfile
import types

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

os.environ.update({
    "BOT_TOKEN": "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "BOT_USERNAME": "x",
    "ADMIN_IDS": "1",
    "ADMIN_PANEL_PASSWORD": "ci-test-password-not-real",
    "PANEL_DOMAIN": "panel.test",
    "SUB_DOMAIN": "sub.test",
    "SITE_DOMAIN": "test",
    "XRAY_PUBLIC_KEY": "x",
    "XRAY_SHORT_ID_TCP": "x",
    "XRAY_SHORT_ID_GRPC": "x",
    "XRAY_SHORT_ID_XHTTP": "x",
})

try:
    import fcntl
except ImportError:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import db
import features
import settings

tmp = tempfile.mkdtemp()
db.DB_PATH = os.path.join(tmp, "test.db")
db.init_db()

passed = 0
failed = 0


def check(name, cond):
    global passed, failed
    if cond:
        passed += 1
        print("PASS", name)
    else:
        failed += 1
        print("FAIL", name)


GB = settings.GB

db.get_or_create_user(100, "alice")
sub = db.create_subscription(100, "de1", 30, "1m", traffic_limit=2 * GB)
uid = sub["uuid"]

used = db.add_traffic_sample(uid, 500)
check("first sample counts whole value", used == 500)
used = db.add_traffic_sample(uid, 1500)
check("second sample adds only the delta", used == 1500)
used = db.add_traffic_sample(uid, 300)
check("counter reset (xray restart) adds the new raw value", used == 1800)
used = db.add_traffic_sample(uid, 300)
check("same raw value adds nothing", used == 1800)
check("under the limit is not flagged", db.list_over_limit() == [])

db.add_traffic_sample(uid, 300 + 2 * GB)
over = db.list_over_limit()
check("over the limit is flagged", len(over) == 1 and over[0]["uuid"] == uid)
db.mark_limit_hit(uid)
check("limit hit deactivates the subscription", db.get_subscription(uid)["active"] == 0)
check("deactivated subscription is not in the active list", db.list_active_subscriptions(tg_id=100) == [])
check("limit hit is not flagged twice", db.list_over_limit() == [])

db.set_traffic_limit(uid, 10 * GB)
fresh = db.get_subscription(uid)
check("raising the limit reactivates", fresh["active"] == 1 and fresh["limit_hit_at"] is None)

db.add_traffic_sample(uid, 12 * GB)
db.mark_limit_hit(uid)
check("reset counter reactivates a limited subscription", db.reset_traffic_counter(uid) is True)
fresh = db.get_subscription(uid)
check("reset counter zeroes usage", fresh["traffic_used"] == 0 and fresh["active"] == 1)

db.set_traffic_limit(uid, None)
check("no limit means never flagged", db.list_over_limit() == [])

expired_sub = db.create_subscription(100, "de1", 30, "1m")
revoked_before = db.get_subscription(expired_sub["uuid"])
check("subscription without limit has no limit stored", revoked_before["traffic_limit"] is None)

check("trial is available for a user without subscriptions", db.get_or_create_user(200, "bob") and db.trial_available(200))
check("trial claim succeeds once", db.claim_trial(200) is True)
check("trial claim fails the second time", db.claim_trial(200) is False)
check("trial is not offered after claim", db.trial_available(200) is False)
check("trial is not offered to users with a subscription", db.trial_available(100) is False)

promo = db.create_promo("sale20", "percent", 20, max_uses=2)
check("promo code is stored uppercase", promo["code"] == "SALE20")
check("percent discount is applied", db.discounted_price(1000, promo) == 800)
fixed = db.create_promo("minus100", "fixed", 100)
check("fixed discount is applied", db.discounted_price(399, fixed) == 299)
check("fixed discount never goes below zero", db.discounted_price(50, fixed) == 0)
try:
    db.create_promo("SALE20", "percent", 10)
    check("duplicate promo is rejected", False)
except ValueError:
    check("duplicate promo is rejected", True)
try:
    db.create_promo("bad", "percent", 150)
    check("percent over 100 is rejected", False)
except ValueError:
    check("percent over 100 is rejected", True)
try:
    db.create_promo("bad code!", "fixed", 5)
    check("promo with symbols is rejected", False)
except ValueError:
    check("promo with symbols is rejected", True)

found, err = db.validate_promo("sale20", 100)
check("valid promo validates", err is None and found["code"] == "SALE20")
found, err = db.validate_promo("nope", 100)
check("unknown promo is not found", err == "not_found")

db.create_payment("p1", 100, "de1", "1m", "yookassa", 319)
db.set_payment_promo("p1", "SALE20", 399)
check("promo is not consumed before payment", db.get_promo("SALE20")["used_count"] == 0)
db.mark_payment_paid("p1")
check("promo is consumed when payment is paid", db.get_promo("SALE20")["used_count"] == 1)
db.mark_payment_paid("p1")
check("paying twice does not consume twice", db.get_promo("SALE20")["used_count"] == 1)
_, err = db.validate_promo("sale20", 100)
check("same user cannot reuse the promo", err == "already_used")

for tg in (300, 301):
    db.get_or_create_user(tg, None)
    db.create_payment(f"pp{tg}", tg, "de1", "1m", "yookassa", 319)
    db.set_payment_promo(f"pp{tg}", "SALE20", 399)
    db.mark_payment_paid(f"pp{tg}")
_, err = db.validate_promo("sale20", 999)
check("promo with exhausted uses is refused", err == "exhausted")

db.set_promo_active("MINUS100", False)
_, err = db.validate_promo("minus100", 100)
check("disabled promo is refused", err == "not_found")

past = (datetime.datetime.utcnow() - datetime.timedelta(days=1)).isoformat()
db.create_promo("OLDONE", "fixed", 10, expires_at=past)
_, err = db.validate_promo("oldone", 100)
check("expired promo is refused", err == "expired")

db.create_promo("BONUS5", "days", 5)
db.get_or_create_user(400, None)
db.create_subscription(400, "de1", 10, "7d")
before = db.list_active_subscriptions(tg_id=400)[0]["expires_at"]
promo_days, err = db.redeem_days_promo("bonus5", 400)
after = db.list_active_subscriptions(tg_id=400)[0]["expires_at"]
delta = datetime.datetime.fromisoformat(after) - datetime.datetime.fromisoformat(before)
check("days promo extends the subscription", err is None and delta == datetime.timedelta(days=5))
_, err = db.redeem_days_promo("bonus5", 400)
check("days promo works once per user", err == "already_used")

db.set_promo_pending(400, "SALE20")
check("pending promo that is exhausted is dropped", db.get_pending_promo(400) is None)

soon = db.create_subscription(500 if db.get_or_create_user(500, None) else 500, "de1", 1, "7d")
due = features.reminders_due()
check("subscription ending within a day is due", any(item[0]["uuid"] == soon["uuid"] for item in due))
for item in due:
    features.mark_stage_sent(item[0]["uuid"], item[0]["expires_at"])
check("reminders are not repeated after sending", features.reminders_due() == [])

print(f"RESULT pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
