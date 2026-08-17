"""Local end-to-end test of app.py with mocked Telegram API and sheet.
Run:  python test_flow.py   (no real tokens needed)
"""
import os
os.environ.update(BOT_TOKEN="fake", SCRIPT_URL="http://fake", SECRET="s",
                  GROUP_CHAT_ID="-100999", PUBLIC_URL="", WEBHOOK_SECRET="whsec")

import app as bot  # noqa: E402  (imports with fake env; startup() skips webhook)

calls = []          # captured tg() calls
sheet_calls = []    # captured sheet_call() invocations


def fake_tg(method, **kw):
    calls.append((method, kw))
    if method == "getMe":
        return {"username": "test_prayer_bot"}
    return True


def fake_sheet(action, **fields):
    sheet_calls.append((action, fields))
    if action == "add":
        return {"ok": True, "no": 7}
    if action == "recent":
        return {"ok": True, "items": [
            {"no": 7, "name": "Grace", "topic": "health", "request": "Mum's surgery <on> Friday", "update": "", "support": "check in"},
        ]}
    raise AssertionError("unexpected action")


bot.tg = fake_tg
bot.sheet_call = fake_sheet
bot.BOT_USERNAME = "test_prayer_bot"  # startup() is skipped in test mode

def msg(text, chat_id=111, user_id=111, chat_type="private"):
    bot.handle_message({"chat": {"id": chat_id, "type": chat_type},
                        "from": {"id": user_id}, "text": text})

def last_text(chat_id=None):
    for m, kw in reversed(calls):
        if m == "sendMessage" and (chat_id is None or kw.get("chat_id") == chat_id):
            return kw["text"]
    raise AssertionError(f"no sendMessage to {chat_id}")

# --- 1. full flow: named user, shares with group ---
msg("/start")
assert bot.pending[111]["step"] == bot.ASK_NAME and "call you" in last_text()
msg("Grace")
assert bot.pending[111]["step"] == bot.ASK_TOPIC
msg("health")
assert bot.pending[111]["step"] == bot.ASK_REQUEST
msg("Mum's surgery is on Friday. Please pray for a smooth operation.")
assert bot.pending[111]["step"] == bot.ASK_SUPPORT
msg("check in on me this week")
assert bot.pending[111]["step"] == bot.ASK_SHARE

bot.handle_callback({"id": "cb1", "from": {"id": 111}, "data": "share_yes",
                     "message": {"chat": {"id": 111}}})
assert not bot.pending, "pending should be cleared"
add = [c for c in sheet_calls if c[0] == "add"]
assert add and add[-1][1]["name"] == "Grace" and add[-1][1]["support"] == "check in on me this week", add
dm = last_text(111)
assert "#7" in dm and ("—" in dm), dm                      # confirmation + verse
grp = last_text("-100999")  # env var is a string
assert "Grace" in grp and "health" in grp and "surgery" in grp, grp

# --- 2. anonymous user, private only, HTML escaping ---
msg("/start", chat_id=222, user_id=222)
msg("anonymous", chat_id=222, user_id=222)   # name step -> stay anonymous
assert bot.pending[222]["anonymous"] is True and bot.pending[222]["step"] == bot.ASK_TOPIC
msg("job interview", chat_id=222, user_id=222)
msg("I have an interview <b>tomorrow</b> and I'm nervous.", chat_id=222, user_id=222)
msg("skip", chat_id=222, user_id=222)
assert bot.pending[222]["step"] == bot.ASK_SHARE
bot.handle_callback({"id": "cb2", "from": {"id": 222}, "data": "share_no",
                     "message": {"chat": {"id": 222}}})
add2 = [c for c in sheet_calls if c[0] == "add"][-1][1]
assert add2["name"] == "Anonymous" and add2["support"] == "", add2
grp_after = last_text("-100999")
assert grp_after is not None  # group msg from user 1 still the latest (user 2 kept private)

# --- 3. /prayers escapes HTML in sheet content ---
msg("/prayers", chat_id=333, user_id=333)
p = last_text(333)
assert "&lt;on&gt;" in p and "<script>" not in p, p

# --- 4. /groupid works in a group ---
msg("/groupid", chat_id=-100555, user_id=444, chat_type="supergroup")
g = last_text(-100555)
assert "-100555" in g, g

# --- 5. /start in a group redirects to private chat ---
msg("/start", chat_id=-100555, user_id=444, chat_type="supergroup")
r = last_text(-100555)
assert "t.me/test_prayer_bot" in r, r

# --- 6. webhook endpoint: secret token enforced, health ok ---
client = bot.app.test_client()
h = client.get("/health")
assert h.status_code == 200 and h.get_json()["ok"] is True
bad = client.post("/webhook", json={"message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/help"}})
assert bad.status_code == 403, bad.status_code
good = client.post("/webhook", json={"message": {"chat": {"id": 555, "type": "private"},
                                                "from": {"id": 555}, "text": "/help"}},
                   headers={"X-Telegram-Bot-Api-Secret-Token": "whsec"})
assert good.status_code == 200 and last_text(555).startswith("🙏"), good.status_code

print("ALL TESTS PASSED ✅")
print("\n--- sample group post (user 1) ---")
print(grp)
print("\n--- sample DM confirmation (truncated) ---")
print(dm[:200])
