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


# In-memory stand-in for the Google Sheet (the /prayers recent list), newest first.
SHEET_ROWS = [
    {"no": 7, "name": "Grace", "topic": "health", "request": "Mum's surgery <on> Friday",
     "update": "", "support": "check in", "date": "17 Aug 2026",
     "submitter_id": "111", "readers": "", "last_read_notify": ""},
    {"no": 6, "name": "Daniel", "topic": "job interview", "request": "Please pray for my interview on Monday.",
     "update": "", "support": "", "date": "18 Aug 2026",
     "submitter_id": "222", "readers": "", "last_read_notify": ""},
    {"no": 5, "name": "Ruth", "topic": "family", "request": "Our family is going through a hard season.",
     "update": "", "support": "", "date": "19 Aug 2026",
     "submitter_id": "333", "readers": "", "last_read_notify": ""},
    # Predates the Submitter ID feature — no contact to notify.
    {"no": 4, "name": "Sam", "topic": "health", "request": "Pray for my recovery.",
     "update": "", "support": "", "date": "20 Aug 2026",
     "submitter_id": "", "readers": "", "last_read_notify": ""},
]

# When True, fake_sheet pretends to be a pre-feature deployment (no get_by_no action).
OLD_SCRIPT_MODE = False


def fake_sheet(action, **fields):
    sheet_calls.append((action, fields))
    if action == "add":
        return {"ok": True, "no": 7, "date": "17 Aug 2026"}
    if action == "recent":
        limit = int(fields.get("limit") or 10)
        rows = sorted(SHEET_ROWS, key=lambda r: -int(r["no"]))[:limit]
        return {"ok": True, "items": [dict(r) for r in rows]}
    if action == "get_by_no":
        if OLD_SCRIPT_MODE:
            return {"ok": False, "error": "unknown action"}  # old deployment
        row = next((r for r in SHEET_ROWS if str(r["no"]) == str(fields.get("no"))), None)
        if row is None:
            return {"ok": False, "error": f"prayer not found: {fields.get('no')}"}
        return {"ok": True, "item": dict(row)}
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
for r in SHEET_ROWS:                                   # clean slate for all rows
    r.update(readers="", last_read_notify="")
n_dm = len(dms_to(111))
msg("@test_prayer_bot /prayers", chat_id=-100555, user_id=444, chat_type="supergroup")
lst = last_text(-100555)
assert "Recent prayer requests" in lst and "👁" not in lst, lst
btns = [b["callback_data"] for row in last_kwargs(-100555)["reply_markup"]["inline_keyboard"] for b in row]
assert btns == ["react_7", "react_6", "react_5", "react_4"], btns   # 🙏 button per shown prayer
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

# --- 12. /prayers --number N (default is now 10; sheet receives the limit) ---
def recent_limits():
    return [f.get("limit") for a, f in sheet_calls if a == "recent"]

msg("/prayers", chat_id=333, user_id=333)          # no flag -> default 10 requested
assert recent_limits()[-1] == bot.DEFAULT_LIST_COUNT == 10, recent_limits()

msg("/prayers --number 2", chat_id=333, user_id=333)
p = last_text(333)
assert "#7" in p and "#6" in p and "#5" not in p and "#4" not in p, p
btns = [b["callback_data"] for row in last_kwargs(333)["reply_markup"]["inline_keyboard"] for b in row]
assert btns == ["react_7", "react_6"], btns
assert recent_limits()[-1] == 2, recent_limits()

msg("/prayers --number=1", chat_id=333, user_id=333)     # = form works too
p = last_text(333)
assert "#7" in p and "#6" not in p, p

msg("/prayers 3", chat_id=333, user_id=333)              # bare number as a convenience
btns = [b["callback_data"] for row in last_kwargs(333)["reply_markup"]["inline_keyboard"] for b in row]
assert btns == ["react_7", "react_6", "react_5"], btns

msg("/prayers --number abc", chat_id=333, user_id=333)
assert "isn't a number" in last_text(333), last_text(333)

msg("/prayers --number 999", chat_id=333, user_id=333)
assert f"between 1 and {bot.MAX_LIST_COUNT}" in last_text(333), last_text(333)

# --- 13. long lists are split into Telegram-sized messages (each keeps its own buttons) ---
saved_limit = bot.TG_MSG_LIMIT
bot.TG_MSG_LIMIT = 150          # force chunking with the small test data
n_before = len(calls)
msg("/prayers", chat_id=888, user_id=888)
new_calls = [c for c in calls[n_before:] if c[1].get("chat_id") == 888]
bot.TG_MSG_LIMIT = saved_limit
assert len(new_calls) >= 2, f"expected chunked list, got {len(new_calls)} message(s)"
for m, kw in new_calls:
    assert len(kw["text"]) < 4096, "chunk must fit Telegram's limit"
last_body = new_calls[-1][1]["text"]
earlier = [kw["text"] for _, kw in new_calls[:-1]]
assert bot.LIST_FOOTER.split("—")[0] in last_body, last_body     # footer only on final chunk
assert all("continued below" in t for t in earlier), earlier
all_btns = [b["callback_data"] for _, kw in new_calls
            for row in (kw.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]
assert sorted(all_btns) == ["react_4", "react_5", "react_6", "react_7"], all_btns

# --- 14. greeting tells users about /help ---
msg("hello there", chat_id=777, user_id=777)
g = last_text(777)
assert "/help" in g and "I'm the prayer request bot" in g, g
msg("/help", chat_id=777, user_id=777)
h = last_text(777)
assert "--number" in h and str(bot.DEFAULT_LIST_COUNT) in h, h

# --- 15. group tap -> reader is told (in-group) they've been messaged privately ---
bot.handle_callback({"id": "cb20", "from": {"id": 444, "first_name": "Peter"}, "data": "react_6",
                     "message": {"chat": {"id": -100555, "type": "supergroup"}}})
assert "How would you like to appear" in last_text(444), last_text(444)
notice = last_text(-100555)
assert "private message" in notice and "prayer #6" in notice, notice
assert 'tg://user?id=444' in notice and "Peter" in notice, notice

n_sends = len([c for c in calls if c[0] == "sendMessage"])
bot.handle_callback({"id": "cb21", "from": {"id": 999}, "data": "react_5",
                     "message": {"chat": {"id": 999, "type": "private"}}})   # tap in a DM: no group noise
assert "How would you like to appear" in last_text(999), last_text(999)
new_sends = [c for c in calls if c[0] == "sendMessage"][n_sends:]
assert len(new_sends) == 1 and new_sends[0][1]["chat_id"] == 999, \
    f"only the private prompt should be sent for a private tap: {new_sends}"

# --- 16. bug fix: 'no contact to notify' must not fire for prayers that do have a submitter ---
# (a) a prayer outside the recent-10 window is still found via get_by_no and delivered
saved_rows = [dict(r) for r in SHEET_ROWS]
for n in range(8, 16):                     # push prayer #5 out of the latest-10 window
    SHEET_ROWS.append({"no": n, "name": f"U{n}", "topic": "misc", "request": f"prayer {n}",
                       "update": "", "support": "", "date": "20 Aug 2026",
                       "submitter_id": str(900 + n), "readers": "", "last_read_notify": ""})
res = bot.sheet_call("recent", limit=10)
assert "5" not in [str(it["no"]) for it in res["items"]], "setup: #5 should be out of window"
got = bot.get_prayer(5)
assert got is not None and str(got["submitter_id"]) == "333", got   # exact lookup finds it anyway

bot.handle_callback({"id": "cb40", "from": {"id": 700}, "data": "react_5",
                     "message": {"chat": {"id": -100555, "type": "supergroup"}}})
assert "How would you like to appear" in last_text(700), last_text(700)
bot.handle_callback({"id": "cb41", "from": {"id": 700}, "data": "react_anon_5"})
msg("you're not alone", chat_id=700, user_id=700)
enc = last_text(333)
assert enc.startswith("💬 Someone") and "#5" in enc, enc   # delivered despite being out of window
SHEET_ROWS[:] = saved_rows

# (b) a genuinely pre-feature prayer still gets the no-contact message
bot.handle_callback({"id": "cb42", "from": {"id": 555}, "data": "react_4",
                     "message": {"chat": {"id": -100555, "type": "supergroup"}}})
assert "How would you like to appear" in last_text(555), last_text(555)
bot.handle_callback({"id": "cb43", "from": {"id": 555}, "data": "react_anon_4"})
msg("hold fast", chat_id=555, user_id=555)
assert "no contact to notify" in last_text(555), last_text(555)

# (c) a prayer that no longer exists gets the not-found message, not 'no contact'
bot.handle_callback({"id": "cb44", "from": {"id": 800}, "data": "react_999"})
assert "isn't in the recent list anymore" in last_text(800), last_text(800)

# --- 17. old script deployment (no get_by_no action): get_prayer falls back to the recent scan ---
OLD_SCRIPT_MODE = True
got = bot.get_prayer(7)
assert got is not None and str(got["no"]) == "7" and str(got["submitter_id"]) == "111", got
OLD_SCRIPT_MODE = False

print("ALL TESTS PASSED ✅")
print("\n--- sample group post (user 1) ---")
print(grp)
print("\n--- sample DM confirmation (truncated) ---")
print(dm[:200])
