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


# In-memory stand-in for the Google Sheet (the /prayers recent list).
SHEET_ROWS = [
    {"no": 7, "name": "Grace", "topic": "health", "request": "Mum's surgery <on> Friday",
     "update": "", "support": "check in", "date": "17 Aug 2026",
     "submitter_id": "111", "readers": "", "last_read_notify": ""},
]


def fake_sheet(action, **fields):
    sheet_calls.append((action, fields))
    if action == "add":
        return {"ok": True, "no": 7, "date": "17 Aug 2026"}
    if action == "recent":
        return {"ok": True, "items": [dict(r) for r in SHEET_ROWS]}
    if action == "mark_read":
        row = next((r for r in SHEET_ROWS if str(r["no"]) == str(fields.get("no"))), None)
        assert row is not None, f"no prayer #{fields.get('no')}"
        readers = [x.strip() for x in (row["readers"].split(",") if row["readers"] else []) if x.strip()]
        rid = str(fields.get("reader_id"))
        if rid and rid not in readers:
            readers.append(rid)
            row["readers"] = ",".join(readers)
        if fields.get("notify_date"):
            row["last_read_notify"] = fields["notify_date"]
        return {"ok": True, "count": len(readers)}
    raise AssertionError(f"unexpected action: {action}")


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

def last_kwargs(chat_id):
    for m, kw in reversed(calls):
        if m == "sendMessage" and kw.get("chat_id") == chat_id:
            return kw
    raise AssertionError(f"no sendMessage to {chat_id}")

def dms_to(uid):
    return [c for c in calls if c[0] == "sendMessage" and c[1].get("chat_id") == uid]

# --- 1. full flow: named user, shares with group ---
msg("/start")
assert bot.pending[111]["step"] == bot.ASK_NAME and ("address you" in last_text() or "call you" in last_text())
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
assert add[-1][1].get("submitter_id") == 111, add
dm = last_text(111)
assert "#7" in dm and ("—" in dm), dm                      # confirmation + verse
grp = last_text("-100999")  # env var is a string
assert "Grace" in grp and "health" in grp and "surgery" in grp, grp
assert "📅 17 Aug 2026" in grp, grp

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
assert "📅 17 Aug 2026" in p, p

# --- 4. /groupid works in a group (with mention) ---
msg("/groupid@test_prayer_bot", chat_id=-100555, user_id=444, chat_type="supergroup")
g = last_text(-100555)
assert "-100555" in g, g

# --- 4b. "@bot /prayers" (mention before command) lists prayers in a group ---
msg("@test_prayer_bot /prayers", chat_id=-100555, user_id=444, chat_type="supergroup")
p = last_text(-100555)
assert "Recent prayer requests" in p and "#7" in p, p

# --- 5. /start in a group redirects to private chat (with mention) ---
msg("/start@test_prayer_bot", chat_id=-100555, user_id=444, chat_type="supergroup")
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

# --- 7. group: silent without mention; plain-text mention -> DM pointer ---
n_before = len(calls)
msg("hello everyone, how are we?", chat_id=-100555, user_id=444, chat_type="supergroup")
assert len(calls) == n_before, "bot must stay silent in groups without a mention"
msg("@test_prayer_bot can you help me?", chat_id=-100555, user_id=444, chat_type="supergroup")
p = last_text(-100555)
assert "t.me/test_prayer_bot" in p and "privately" in p, p

# --- 8. read notifications: reader credited once, submitter notified once/day, no self-notify ---
SHEET_ROWS[0].update(readers="", last_read_notify="")
n_dm = len(dms_to(111))
msg("@test_prayer_bot /prayers", chat_id=-100555, user_id=444, chat_type="supergroup")
lst = last_text(-100555)
assert "Recent prayer requests" in lst and "👁" not in lst, lst
btns = [b["callback_data"] for row in last_kwargs(-100555)["reply_markup"]["inline_keyboard"] for b in row]
assert btns == ["react_7"], btns                       # 🙏 encourage button offered
note = last_text(111)
assert "#7" in note and "Someone has read" in note and "444" not in note, note  # reader stays anonymous
assert SHEET_ROWS[0]["readers"] == "444", SHEET_ROWS[0]

msg("@test_prayer_bot /prayers", chat_id=-100555, user_id=444, chat_type="supergroup")  # same reader again
assert len(dms_to(111)) == n_dm + 1 and SHEET_ROWS[0]["readers"] == "444"

msg("/prayers@test_prayer_bot", chat_id=-100555, user_id=666, chat_type="supergroup")   # new reader, same day
assert SHEET_ROWS[0]["readers"] == "444,666" and len(dms_to(111)) == n_dm + 1           # count grows, no extra DM

SHEET_ROWS[0].update(readers="", last_read_notify="1970-01-01")                          # new day
msg("@test_prayer_bot /prayers", chat_id=-100555, user_id=666, chat_type="supergroup")
assert SHEET_ROWS[0]["readers"] == "666" and len(dms_to(111)) == n_dm + 2                # notified again

msg("/prayers", chat_id=111, user_id=111)                                                # submitter reads own prayer
assert SHEET_ROWS[0]["readers"] == "666" and "Someone has read" not in last_text(111), last_text(111)

# --- 9. encouragement: anonymous, skip the message ---
bot.handle_callback({"id": "cb3", "from": {"id": 444}, "data": "react_7",
                     "message": {"chat": {"id": -100555, "type": "supergroup"}}})
q = last_text(444)
assert "Grace" in q and "#7" in q, q
btns = [b["callback_data"] for row in last_kwargs(444)["reply_markup"]["inline_keyboard"] for b in row]
assert btns == ["react_anon_7", "react_named_7"], btns   # identity choice happens privately (DM)
bot.handle_callback({"id": "cb4", "from": {"id": 444}, "data": "react_anon_7"})
assert "skip" in last_text(444).lower(), last_text(444)
msg("skip", chat_id=444, user_id=444)                    # typed in their DM
enc = last_text(111)
assert enc.startswith("🙏 Someone") and "#7" in enc and "444" not in enc, enc
assert "Sent 🕊️" in last_text(444), last_text(444)

# --- 10. encouragement: named, with a message (plain-text delivery) ---
bot.handle_callback({"id": "cb5", "from": {"id": 666}, "data": "react_7"})
assert "How would you like to appear" in last_text(666), last_text(666)
bot.handle_callback({"id": "cb6", "from": {"id": 666}, "data": "react_named_7"})
assert "What name should they see" in last_text(666), last_text(666)
msg("Peter", chat_id=666, user_id=666)                   # REACT_NAME -> message prompt
assert "skip" in last_text(666).lower(), last_text(666)
msg("You're not alone in this <3", chat_id=666, user_id=666)  # REACT_MESSAGE
enc = last_text(111)
assert enc.startswith("💬 Peter") and "prayer #7" in enc and "<3" in enc, enc

# --- 11. can't encourage your own prayer ---
bot.handle_callback({"id": "cb7", "from": {"id": 111}, "data": "react_7",
                     "message": {"chat": {"id": -100555, "type": "supergroup"}}})
assert "your own prayer" in last_text(111), last_text(111)

print("ALL TESTS PASSED ✅")
print("\n--- sample group post (user 1) ---")
print(grp)
print("\n--- sample DM confirmation (truncated) ---")
print(dm[:200])
