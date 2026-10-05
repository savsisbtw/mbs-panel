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

created = db.create_api_token("reseller")
check("api token has the mbs_ prefix", created["token"].startswith("mbs_"))
check("api token verifies", db.verify_api_token(created["token"])["name"] == "reseller")
check("wrong api token is refused", db.verify_api_token("mbs_wrong") is None)
check("empty api token is refused", db.verify_api_token("") is None)
check("token list never holds the token itself", all("token" not in row for row in db.list_api_tokens()))
db.revoke_api_token(created["id"])
check("revoked api token is refused", db.verify_api_token(created["token"]) is None)

ext_user = 600
db.get_or_create_user(ext_user, None)
ext = db.create_subscription(ext_user, "de1", 10, "7d")
before_exp = datetime.datetime.fromisoformat(ext["expires_at"])
extended = db.extend_subscription(ext["uuid"], 5)
check("extend adds days to a live subscription", datetime.datetime.fromisoformat(extended["expires_at"]) - before_exp == datetime.timedelta(days=5))
db.revoke_subscription(ext["uuid"])
back = db.extend_subscription(ext["uuid"], 3)
check("extend reactivates a revoked subscription", back["active"] == 1)

import payments
check("paid statuses include cryptobot paid", "paid" in payments.PAID_STATUSES)
check("failed statuses include cryptobot expired", "expired" in payments.FAILED_STATUSES)
check("cryptobot has a display name", "cryptobot" in payments.PROVIDER_NAMES)

import json as jsonmod
import config
import formats
import links

sample = links._transport_uris("11111111-1111-1111-1111-111111111111", config.DE1_TRANSPORTS, "DE")
sample.append(links.hysteria_uri_for_node(
    {"hysteria_enabled": 1, "hysteria_password": "pw", "hysteria_obfs_password": "ob", "sni": "x.test",
     "address": "h.test", "hysteria_port": 443}, "DE"))
parsed = formats.parse_all(sample)
check("all sample uris parse", len(parsed) == len(sample) and len(sample) == 5)
clash = jsonmod.loads(formats.build_clash(sample, ru_direct=True))
kinds = [p["type"] for p in clash["proxies"]]
check("clash has vless and hysteria2 proxies", "vless" in kinds and "hysteria2" in kinds)
check("clash skips xhttp which it cannot run", len(clash["proxies"]) == 4)
reality = [p for p in clash["proxies"] if p.get("reality-opts")]
check("clash reality proxy carries the public key", reality and reality[0]["reality-opts"]["public-key"] == "x")
check("clash routes ru direct when asked", "GEOIP,RU,DIRECT" in clash["rules"] and clash["rules"][-1] == "MATCH,PROXY")
check("clash without ru flag has no ru rule", "GEOIP,RU,DIRECT" not in jsonmod.loads(formats.build_clash(sample))["rules"])
box = jsonmod.loads(formats.build_singbox(sample))
tags = [o["tag"] for o in box["outbounds"]]
check("singbox has a selector and urltest", "proxy" in tags and "auto" in tags and box["route"]["final"] == "proxy")
check("singbox keeps four real outbounds", len([o for o in box["outbounds"] if o["type"] in ("vless", "hysteria2")]) == 4)
check("detect clash by user agent", formats.detect_format("ClashMeta/1.0", "") == "clash")
check("detect singbox by user agent", formats.detect_format("sing-box 1.9", "") == "singbox")
check("explicit format wins", formats.detect_format("Happ/1", "clash") == "clash")
check("default format is base64", formats.detect_format("Happ/1", "") == "base64")

import backup

blob = backup.encrypt_backup(b"secret archive bytes", "correct horse battery")
check("encrypted backup hides the content", b"secret archive" not in blob and blob.startswith(backup.MAGIC))
check("decrypt returns the original bytes", backup.decrypt_backup(blob, "correct horse battery") == b"secret archive bytes")
try:
    backup.decrypt_backup(blob, "wrong phrase")
    check("wrong passphrase is refused", False)
except backup.RestoreError:
    check("wrong passphrase is refused", True)
try:
    backup.decrypt_backup(b"not a backup at all", "x")
    check("garbage input is refused", False)
except backup.RestoreError:
    check("garbage input is refused", True)
check("two encryptions of the same data differ", backup.encrypt_backup(b"a", "pppppppp") != backup.encrypt_backup(b"a", "pppppppp"))

hist_sub = db.create_subscription(100, "de1", 5, "7d")
db.add_traffic_sample(hist_sub["uuid"], 1000)
db.add_traffic_sample(hist_sub["uuid"], 4000)
hist = db.traffic_history(7)
check("history has one entry per day", len(hist) == 7)
check("history counts only the deltas for today", hist[-1]["total"] >= 4000 and hist[-1]["nodes"].get("de1", 0) >= 4000)
check("history fills missing days with zeros", hist[0]["total"] == 0)

db.get_or_create_user(700, "carol")
db.set_user_note(700, "friend of alice")
found_users = db.list_users(q="friend")
check("notes are searchable in the users list", any(u["tg_id"] == 700 for u in found_users))
check("note comes back in the users list", next(u for u in found_users if u["tg_id"] == 700)["note"] == "friend of alice")
db.set_user_note(700, "   ")
check("blank note clears it", db.get_user(700)["note"] is None)

import json as _json
import sqlite3 as _sqlite
import importer

xui_path = os.path.join(tmp, "x-ui.db")
xc = _sqlite.connect(xui_path)
xc.execute("CREATE TABLE inbounds (id INTEGER, protocol TEXT, settings TEXT)")
future_ms = int((datetime.datetime.utcnow() + datetime.timedelta(days=20)).timestamp() * 1000)
xc.execute("INSERT INTO inbounds VALUES (1, 'vless', ?)", (_json.dumps({"clients": [
    {"id": "aaaaaaaa-0000-0000-0000-000000000001", "email": "tgguy", "tgId": "555", "expiryTime": future_ms, "totalGB": 5 * GB, "enable": True},
    {"id": "aaaaaaaa-0000-0000-0000-000000000002", "email": "plain", "expiryTime": 0, "totalGB": 0, "enable": True},
    {"id": "aaaaaaaa-0000-0000-0000-000000000003", "email": "old", "expiryTime": 1000, "enable": True},
]}),))
xc.execute("INSERT INTO inbounds VALUES (2, 'vmess', ?)", (_json.dumps({"clients": [{"id": "ignored"}]}),))
xc.commit()
xc.close()
xui_clients = importer.parse_xui(xui_path)
check("xui import reads only vless clients", len(xui_clients) == 3)
check("xui keeps the telegram id when present", xui_clients[0]["tg_id"] == 555)
check("xui limit is carried over in bytes", xui_clients[0]["traffic_limit"] == 5 * GB and xui_clients[1]["traffic_limit"] is None)
check("xui zero expiry means no expiry", xui_clients[1]["expires_at"] is None)
check("xui client without telegram id gets a stable negative id", xui_clients[1]["tg_id"] < 0 and xui_clients[1]["tg_id"] == importer.synthetic_tg_id("plain"))

mz_path = os.path.join(tmp, "marzban.db")
mc = _sqlite.connect(mz_path)
mc.execute("CREATE TABLE users (id INTEGER, username TEXT, status TEXT, data_limit INTEGER, expire TEXT)")
mc.execute("CREATE TABLE proxies (id INTEGER, user_id INTEGER, type TEXT, settings TEXT)")
mc.execute("INSERT INTO users VALUES (1, 'alice', 'active', 1073741824, '2099-01-01 00:00:00')")
mc.execute("INSERT INTO users VALUES (2, 'bob', 'disabled', NULL, NULL)")
mc.execute("INSERT INTO proxies VALUES (1, 1, 'vless', ?)", (_json.dumps({"id": "bbbbbbbb-0000-0000-0000-000000000001"}),))
mc.execute("INSERT INTO proxies VALUES (2, 2, 'vless', ?)", (_json.dumps({"id": "bbbbbbbb-0000-0000-0000-000000000002"}),))
mc.execute("INSERT INTO proxies VALUES (3, 1, 'trojan', ?)", (_json.dumps({"password": "x"}),))
mc.commit()
mc.close()
mz_clients = importer.parse_marzban(mz_path)
check("marzban import reads vless proxies only", len(mz_clients) == 2)
check("marzban expiry and limit are parsed", mz_clients[0]["expires_at"].startswith("2099") and mz_clients[0]["traffic_limit"] == GB)
check("marzban disabled status is carried over", mz_clients[1]["enabled"] is False)

try:
    importer.parse_xui(mz_path)
    check("wrong source file is refused", False)
except importer.ImportError_:
    check("wrong source file is refused", True)
try:
    importer.parse_upload("xui", b"this is not sqlite")
    check("non sqlite upload is refused", False)
except importer.ImportError_:
    check("non sqlite upload is refused", True)

outcome = db.import_subscription(555, "tgguy", "de1", "aaaaaaaa-0000-0000-0000-000000000001", xui_clients[0]["expires_at"], 5 * GB, True)
check("import creates a live subscription", outcome == "created" and db.get_subscription("aaaaaaaa-0000-0000-0000-000000000001")["traffic_limit"] == 5 * GB)
check("import twice does not duplicate", db.import_subscription(555, "tgguy", "de1", "aaaaaaaa-0000-0000-0000-000000000001", None, None, True) == "exists")
check("import of an expired client is inactive", db.import_subscription(-5, "old", "de1", "aaaaaaaa-0000-0000-0000-000000000003", xui_clients[2]["expires_at"], None, True) == "inactive")

import base64 as _b64
import config
import plugins

for fn in (links.build_expired_placeholder_text, links.build_device_limit_placeholder_text, links.build_device_blocked_placeholder_text):
    decoded = _b64.b64decode(fn()).decode().split("\n")
    check("placeholder is two vless links", len(decoded) == 2 and all(x.startswith("vless://") for x in decoded))
check("empty subscription list gives the expired placeholder", links.build_subscription_text([]) != "")

dev_user = 800
db.get_or_create_user(dev_user, None)
_, ok, reason = db.add_device_if_under_limit(dev_user, "hwid-aaaaaaaaaa", 1, None, None, None)
check("first device is accepted", ok and reason is None)
_, ok, reason = db.add_device_if_under_limit(dev_user, "hwid-bbbbbbbbbb", 1, None, None, None)
check("second device is refused with the limit reason", (not ok) and reason == "limit")
dev_row = db.list_devices(dev_user)[0]
db.delete_device(dev_row["id"], block=True)
_, ok, reason = db.add_device_if_under_limit(dev_user, "hwid-aaaaaaaaaa", 5, None, None, None)
check("removed device stays blocked when blocking is on", (not ok) and reason == "blocked")
_, ok, reason = db.add_device_if_under_limit(dev_user, "hwid-bbbbbbbbbb", 5, None, None, None)
check("another device is still accepted", ok)
other_row = db.list_devices(dev_user)[0]
db.delete_device(other_row["id"])
_, ok, reason = db.add_device_if_under_limit(dev_user, "hwid-bbbbbbbbbb", 5, None, None, None)
check("plain delete does not block", ok)

db.create_node("x1", "X One", "external", "x1.test", 443, "pbk", "sid", "sni.test", "xtls-rprx-vision", shared_uuid="shared-1")
mode_sub = db.create_subscription(dev_user, "de1", 5, "7d")
config.ALL_NODES_MODE = False
plain = _b64.b64decode(links.build_subscription_text(db.list_active_subscriptions(tg_id=dev_user))).decode()
check("single node mode leaves other nodes out", "x1.test" not in plain)
config.ALL_NODES_MODE = True
shared = _b64.b64decode(links.build_subscription_text(db.list_active_subscriptions(tg_id=dev_user))).decode()
check("all nodes mode lists every enabled node", "x1.test" in shared)
check("all nodes mode keeps the local node too", mode_sub["uuid"] in shared)
config.ALL_NODES_MODE = False

rotated = db.regenerate_primary_subscription_uuid(dev_user)
check("uuid rotation returns old and new", rotated and rotated["old_uuid"] != rotated["new_uuid"])
check("rotated subscription keeps its owner", db.get_subscription(rotated["new_uuid"])["tg_id"] == dev_user)

plan_flags = {p["code"]: p["trial"] for p in settings.get_plans()}
check("plans carry a trial flag that is off by default", plan_flags and not any(plan_flags.values()))
check("trial mark works once", db.mark_trial_used(dev_user) and not db.mark_trial_used(dev_user))

plug_dir = os.path.join(tmp, "plug")
os.makedirs(os.path.join(plug_dir, "custom", "templates"))
open(os.path.join(plug_dir, "custom", "__init__.py"), "w").write(
    "def subscription_lines(lines, subs, nodes):\n    return lines + ['vless://extra@h:1']\n"
    "def about_footer():\n    return 'footer from custom'\n"
)
open(os.path.join(plug_dir, "custom", "templates", "page.html"), "w").write("custom page")
saved = (plugins.CUSTOM_DIR, plugins.BASE_DIR, plugins._module, plugins._checked)
plugins.CUSTOM_DIR = os.path.join(plug_dir, "custom")
plugins.BASE_DIR = plug_dir
plugins._module, plugins._checked = None, False
check("plugin module loads from the custom folder", plugins.load() is not None)
check("plugin hook output is used", plugins.call("about_footer", "") == "footer from custom")
check("missing plugin hook falls back to the default", plugins.call("nothing_here", "dflt") == "dflt")
check("plugin template overrides the default", plugins.template("page.html", "default") == "custom page")
check("missing plugin template keeps the default", plugins.template("missing.html", "default") == "default")
check("plugin can extend subscription lines", plugins.call("subscription_lines", ["a"], ["a"], [], {}) == ["a", "vless://extra@h:1"])
plugins.CUSTOM_DIR, plugins.BASE_DIR, plugins._module, plugins._checked = saved
plugins._module, plugins._checked = None, False
plugins.CUSTOM_DIR = os.path.join(tmp, "no-such-folder")
check("without a custom folder hooks do nothing", plugins.call("about_footer", "x") == "x")
plugins.CUSTOM_DIR = saved[0]

print(f"RESULT pass={passed} fail={failed}")
sys.exit(1 if failed else 0)
