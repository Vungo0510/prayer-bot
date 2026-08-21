"""Church lifegroup prayer request Telegram bot.

Flow: /start -> name (or anonymous) -> topic -> request -> support -> share?
Saves each request to the Google Sheet via an Apps Script web app,
replies with a random encouraging verse, and optionally posts to the group chat.
"""
import html
import logging
import os
import random
import re
import time

import requests
from flask import Flask, jsonify, request

# ---------- config (set these as environment variables on Render) ----------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")            # from @BotFather
SCRIPT_URL = os.environ.get("SCRIPT_URL", "")          # Apps Script /exec URL
SECRET = os.environ.get("SECRET", "")                  # must match SECRET in Apps Script
GROUP_CHAT_ID = os.environ.get("GROUP_CHAT_ID", "")    # e.g. -1001234567890 (get via /groupid)
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")          # your Render https URL, no trailing slash
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "please-change-me")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"

app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("prayer-bot")

# ---------- conversation state (in-memory; fine for a single-worker deploy) ----------
ASK_NAME, ASK_TOPIC, ASK_REQUEST, ASK_SUPPORT, ASK_SHARE, \
REACT_IDENTITY, REACT_NAME, REACT_MESSAGE = range(8)
pending: dict[int, dict] = {}   # user_id -> {step, name, anonymous, topic, request, support, ts}
STALE_AFTER = 24 * 3600         # drop half-finished conversations after a day

BOT_USERNAME = ""

VERSES = [
    ("Do not be anxious about anything, but in every situation, by prayer and petition, with thanksgiving, present your requests to God.", "Philippians 4:6"),
    ("So do not fear, because I am with you; do not be dismayed, because I am your God.", "Isaiah 41:10"),
    ("Cast all your anxiety on him because he cares for you.", "1 Peter 5:7"),
    ("Come to me, all you who are weary and burdened, and I will give you rest.", "Matthew 11:28"),
    ("The prayer of a righteous person is powerful and effective.", "James 5:16"),
    ("When the righteous call for help, the Lord hears them and saves them from all their troubles.", "Psalm 34:17"),
    ("The Lord is close to the brokenhearted and saves those who are crushed in spirit.", "Psalm 34:18"),
    ("For I know the plans I have for you, declares the Lord, plans to give you hope and a future.", "Jeremiah 29:11"),
    ("And we know that in all things God works for the good of those who love him.", "Romans 8:28"),
    ("God is our refuge and strength, an ever-present help in trouble.", "Psalm 46:1"),
    ("The Lord's compassions are new every morning; great is your faithfulness.", "Lamentations 3:22-23"),
    ("Peace I leave with you; my peace I give you. Do not let your hearts be troubled or fearful.", "John 14:27"),
    ("The name of the Lord is a fortified tower; running into it, people are safe.", "Proverbs 18:10"),
]

# ---------- telegram helpers ----------
def tg(method, **kwargs):
    r = requests.post(f"{API}/{method}", json=kwargs, timeout=30)
    try:
        data = r.json()
    except ValueError:
        raise RuntimeError(f"Telegram API non-JSON response ({r.status_code}): {r.text[:200]}")
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error {data.get('error_code')}: {data.get('description')}")
    return data["result"]


def send(chat_id, text, **kw):
    tg("sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True, **kw)

# ---------- sheet helpers ----------
def sheet_call(action, **fields):
    payload = {"token": SECRET, "action": action}
    payload.update(fields)
    r = requests.post(SCRIPT_URL, json=payload, timeout=60)  # first call can be slow (script cold start)
    r.raise_for_status()
    return r.json()

# ---------- prayer request flow ----------
def cleanup_stale():
    now = time.time()
    for uid in [u for u, s in pending.items() if now - s["ts"] > STALE_AFTER]:
        del pending[uid]


# ---------- read tracking & encouragement ----------
def get_prayer(no):
    """Fetch one prayer by its number from the recent list (or None)."""
    res = sheet_call("recent")
    for it in res.get("items", []):
        if str(it.get("no")) == str(no):
            return it
    return None


def mark_reads(items, user_id):
    """Record that user_id saw these prayers; DM each submitter at most once a day.

    The reader stays anonymous to the submitter — they only learn that
    *someone* read the prayer. Self-reads are ignored (no notification).
    """
    today = time.strftime("%Y-%m-%d")
    for it in items:
        try:
            no = int(it.get("no"))
        except (TypeError, ValueError):
            continue
        submitter_id = str(it.get("submitter_id") or "").strip()
        if not submitter_id or submitter_id == str(user_id):
            continue  # nothing to notify / it's their own prayer
        readers = [r.strip() for r in str(it.get("readers") or "").split(",") if r.strip()]
        if str(user_id) in readers:
            continue  # already credited this reader
        should_notify = str(it.get("last_read_notify") or "") != today
        try:
            sheet_call("mark_read", no=no, reader_id=user_id,
                       notify_date=today if should_notify else "")
        except Exception:
            log.warning("mark_read failed for prayer #%s", no)
            continue
        if should_notify:
            try:
                send(int(submitter_id),
                     f"👁 Someone has read your prayer request #{no}.\n\n"
                     "They're carrying you in their prayers. 🕊️")
            except Exception:
                log.warning("read notification failed for prayer #%s", no)


def start_encouragement(user_id, kind, no):
    """Reader tapped 🙏 on prayer #no in the list; continue privately with them.

    kind is None (pick identity), 'anon', or 'named'. All follow-up happens in a
    private chat so the reader's choice isn't announced publicly.
    """
    try:
        it = get_prayer(no)
    except Exception:
        log.exception("sheet recent failed during encouragement")
        send(user_id, "⚠️ I couldn't reach the prayer list just now. Please try again.")
        return
    if not it:
        send(user_id, f"That prayer #{no} isn't in the recent list anymore.")
        return
    if str(it.get("submitter_id")) == str(user_id):
        send(user_id, f"That's your own prayer #{no} 🙂")
        return
    # Note: this replaces any half-finished flow for this user (they can /start again).
    base = {"react_no": no, "ts": time.time()}
    if kind is None:
        pending[user_id] = {**base, "step": REACT_IDENTITY}
        name = html.escape(str(it.get("name") or "Anonymous"))
        topic = html.escape(str(it.get("topic") or ""))
        send(user_id, f"You're about to encourage {name}'s prayer #{no} ({topic}).\n\n"
                      "How would you like to appear?", parse_mode="HTML",
             reply_markup={"inline_keyboard": [[
                 {"text": "🤫 Anonymously", "callback_data": f"react_anon_{no}"},
                 {"text": "✍️ With my name", "callback_data": f"react_named_{no}"}]]})
    elif kind == "anon":
        pending[user_id] = {**base, "step": REACT_MESSAGE, "anonymous": True}
        send(user_id, "Type a short word of encouragement — or 'skip' to just pray for them silently 🙏")
    else:  # named
        pending[user_id] = {**base, "step": REACT_NAME, "anonymous": False}
        send(user_id, "What name should they see? (It'll show in the message I send them.)")


def start_flow(user_id, chat_id):
    pending[user_id] = {"step": ASK_NAME, "ts": time.time()}
    send(chat_id,
         "I'd love to help you share a prayer request. 🙏\n\n"
         "First — how should I address you?\n"
         "(Type your name, or 'anonymous' if you'd rather keep it private.)")


def finish_request(user_id, chat_id, share):
    s = pending.pop(user_id, None)
    if not s:
        return
    try:
        res = sheet_call("add", name=s.get("name") or "Anonymous", topic=s["topic"],
                         request=s["request"], update="", support=s.get("support", ""),
                         submitter_id=user_id)
    except Exception:
        log.exception("sheet add failed")
        send(chat_id, "⚠️ Sorry, I couldn't save your request just now. Please try again in a moment.")
        return
    if not res.get("ok"):
        send(chat_id, f"⚠️ Something went wrong saving your request: {res.get('error')}")
        return

    no = res["no"]
    date_str = html.escape(str(res.get("date") or ""))
    verse, ref = random.choice(VERSES)
    name_label = "Anonymous" if s.get("anonymous") else html.escape(s["name"])

    send(chat_id,
         f"Your prayer request #{no} has been received. 🙏\n\n"
         f"You're not carrying this alone — the whole group is a circle of prayer around you.\n\n"
         f"<i>“{verse}”</i>\n— {ref}",
         parse_mode="HTML")

    if share:
        if not GROUP_CHAT_ID:
            log.warning("share requested but GROUP_CHAT_ID is not set")
            send(chat_id, "Note: the group chat isn't configured yet, so I couldn't post there.")
            return
        support_line = (f"\n💡 <i>How we can support:</i> {html.escape(s['support'])}"
                        if s.get("support") else "")
        date_tag = f" · 📅 {date_str}" if date_str else ""
        group_msg = (
            f"🙏 <b>New prayer request #{no}</b>{date_tag}\n\n"
            f"<b>Name:</b> {name_label}\n"
            f"<b>Praying for:</b> {html.escape(s['topic'])}\n\n"
            f"“{html.escape(s['request'])}”{support_line}\n\n"
            f"<i>Let's stand in the gap for them this week. 🕊️</i>"
        )
        try:
            send(GROUP_CHAT_ID, group_msg, parse_mode="HTML")
        except Exception:
            log.exception("group post failed (is the bot a member of the group?)")
            send(chat_id, "Note: I couldn't post to the group — check that I've been added to it.")


def handle_message(msg):
    chat = msg.get("chat", {})
    user = msg.get("from", {})
    text = (msg.get("text") or "").strip()
    chat_id, user_id = chat["id"], user.get("id")
    if not user_id:
        return

    is_private = chat.get("type") == "private"

    # In groups, stay silent unless the bot is mentioned (@username).
    # Commands addressed to us (e.g. /prayers@botname) contain the mention too;
    # strip it so "@bot /prayers" also parses as "/prayers".
    if not is_private and BOT_USERNAME:
        if f"@{BOT_USERNAME}".lower() not in text.lower():
            return
        text = re.sub(rf"(?i)@\s*{re.escape(BOT_USERNAME)}\b", "", text).strip()

    # ----- commands -----
    if text.startswith("/"):
        cmd = text.split()[0].lower().split("@")[0]

        if cmd == "/start":
            cleanup_stale()
            if chat.get("type") != "private" and BOT_USERNAME:
                send(chat_id, f"To keep things comfortable, please start in a private chat with me:\nhttps://t.me/{BOT_USERNAME}")
                return
            pending.pop(user_id, None)
            start_flow(user_id, chat_id)
            return

        elif cmd == "/prayers":
            try:
                res = sheet_call("recent")
                items = res.get("items", [])
            except Exception:
                log.exception("sheet recent failed")
                send(chat_id, "⚠️ Couldn't reach the prayer list right now.")
                return
            if not items:
                send(chat_id, "No prayer requests yet. Be the first! 🙏 (send /start)")
                return
            shown = items[:5]  # newest first — only what's displayed counts as read
            mark_reads(shown, user_id)  # credit this reader + notify submitters (once/day each)
            lines = ["📖 <b>Recent prayer requests</b>\n"]
            keyboard = []
            for it in shown:
                name = html.escape(str(it.get("name") or "Anonymous"))
                topic = html.escape(str(it.get("topic") or ""))
                req = html.escape(str(it.get("request") or ""))
                date = html.escape(str(it.get("date") or ""))
                when = f" · 📅 {date}" if date else ""
                readers = [r for r in str(it.get("readers") or "").split(",") if r.strip()]
                eyes = f" · 👁 {len(readers)}" if readers else ""
                lines.append(f"#{it.get('no')} · {name} — {topic}{when}{eyes}\n“{req}”\n")
                keyboard.append([{"text": f"🙏 #{it.get('no')}",
                                  "callback_data": f"react_{it.get('no')}"}])
            lines.append("Tap 🙏 on a prayer to encourage its author — privately, anonymous or with your name.")
            send(chat_id, "\n".join(lines), parse_mode="HTML",
                 reply_markup={"inline_keyboard": keyboard})
            return

        elif cmd == "/groupid":
            send(chat_id, f"This chat id is: <code>{chat_id}</code>", parse_mode="HTML")
            return

        elif cmd == "/help":
            send(chat_id,
                 "🙏 I help our life group share prayer requests.\n\n"
                 "/start — share a new prayer request (you can stay anonymous)\n"
                 "/prayers — see the most recent requests\n"
                 "/groupid — show this chat's id (for setup)\n\n"
                 "Just send /start and I'll walk you through it, step by step.\n\n"
                 "Tap 🙏 under any prayer in the list to encourage its author privately — anonymous or with your name." +
                 (f"\n\nIn our group chat, mention me first — e.g. @{BOT_USERNAME} /prayers."
                  if BOT_USERNAME else ""))
            return

        else:
            return  # unknown command, ignore

    # ----- plain-text mention in a group -> point to private chat -----
    if not is_private:
        tg("sendMessage", chat_id=chat_id,
           text=f"Hi! To share a prayer request privately, message me directly:\nhttps://t.me/{BOT_USERNAME}")
        return

    # ----- conversation flow (private chats only) -----
    s = pending.get(user_id)
    if not s:
        tg("sendMessage", chat_id=chat_id,
           text="Hi! 🙏 I'm the prayer request bot.\nSend /start to share a prayer request, or /prayers to see recent ones.",
           reply_markup={"inline_keyboard": [[{"text": "🙏 Share a prayer request", "callback_data": "new"}]]})
        return

    s["ts"] = time.time()
    step = s["step"]

    if step == ASK_NAME:
        low = text.lower()
        if low in ("anonymous", "anon"):
            s["name"], s["anonymous"] = "Anonymous", True
        else:
            s["name"], s["anonymous"] = text[:100], False
        s["step"] = ASK_TOPIC
        send(chat_id, "Thank you. What would you like us to pray for?\n"
                      "A short phrase is perfect — e.g. 'health', 'job interview', 'my family'.")

    elif step == ASK_TOPIC:
        s["topic"] = text[:200]
        s["step"] = ASK_REQUEST
        send(chat_id, "Now tell us a little more about what's on your heart.\n"
                      "(Share as much or as little as you're comfortable with.)")

    elif step == ASK_REQUEST:
        if not text:
            send(chat_id, "Could you share at least a sentence? 🙂")
            return
        s["request"] = text[:2000]
        s["step"] = ASK_SUPPORT
        send(chat_id, "How can we support you while we pray?\n"
                      "(e.g. 'check in on me this week', 'bring a meal' — or type skip if you'd rather we just pray quietly.)")

    elif step == ASK_SUPPORT:
        low = text.lower()
        s["support"] = "" if low in ("skip", "-", "none", "no", "n/a") else text[:300]
        s["step"] = ASK_SHARE
        tg("sendMessage", chat_id=chat_id,
           text="One last thing — would you like the group to see this request?",
           reply_markup={"inline_keyboard": [[
               {"text": "📢 Share with the group", "callback_data": "share_yes"},
               {"text": "🤫 Keep it private (me & God)", "callback_data": "share_no"},
           ]]})

    elif step == ASK_SHARE:
        # they typed instead of pressing a button; default to private when unclear
        low = text.lower()
        finish_request(user_id, chat_id, share=("yes" in low) or ("share" in low))

    elif step == REACT_NAME:
        s["react_name"] = text[:50]
        s["step"] = REACT_MESSAGE
        send(chat_id, "Type a short word of encouragement — or 'skip' to just pray for them silently 🙏")

    elif step == REACT_MESSAGE:
        if not text:
            send(chat_id, "Type a short word of encouragement — or 'skip' to just pray for them silently 🙏")
            return
        message = "" if text.lower() in ("skip", "-", "none", "no") else text[:300]
        no, anon = s["react_no"], bool(s.get("anonymous"))
        name = (s.get("react_name") or "").strip() or "A friend"
        pending.pop(user_id, None)
        try:
            it = get_prayer(no)
        except Exception:
            log.exception("sheet recent failed during encouragement")
            send(chat_id, "⚠️ I couldn't find that prayer just now. Please try again.")
            return
        submitter_id = str((it or {}).get("submitter_id") or "").strip()
        if not submitter_id:
            send(chat_id, f"⚠️ Prayer #{no} has no contact to notify (it may predate this feature).")
            return
        who = "Someone" if anon else name
        body = (f'💬 {who} left an encouragement on your prayer #{no}:\n\n“{message}”'
                if message else f"🙏 {who} prayed over your request #{no}. You're not alone in this.")
        try:
            send(int(submitter_id), body)
            send(chat_id, "Sent 🕊️ They'll receive it as a private message from me.")
        except Exception:
            log.exception("encouragement delivery failed for prayer #%s", no)
            send(chat_id, "⚠️ I couldn't deliver that just now (they may have blocked me). Please try again later.")


def handle_callback(cb):
    user_id = cb["from"]["id"]
    msg_or_inline = cb.get("message") or {}
    chat_id = (msg_or_inline.get("chat") or {}).get("id") or (cb.get("chat") or {}).get("id")
    data = cb.get("data", "")
    try:
        tg("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass  # answering is cosmetic; don't fail the flow over it

    if data == "new":
        cleanup_stale()
        pending.pop(user_id, None)
        start_flow(user_id, chat_id)
    elif data in ("share_yes", "share_no"):
        s = pending.get(user_id)
        if s and s["step"] == ASK_SHARE:
            finish_request(user_id, chat_id, share=(data == "share_yes"))

    elif data.startswith("react_"):
        m = re.fullmatch(r"react_(?:(anon|named)_)?(\d+)", data)
        if m:
            start_encouragement(user_id, m.group(1), int(m.group(2)))

# ---------- web endpoints ----------
@app.get("/health")
def health():
    return jsonify(ok=True)


@app.post("/webhook")
def webhook():
    if WEBHOOK_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token", "") != WEBHOOK_SECRET:
        log.warning("webhook rejected: bad secret token from %s", request.remote_addr)
        return jsonify(ok=False), 403
    update = request.get_json(force=True, silent=True) or {}
    try:
        if "message" in update:
            handle_message(update["message"])
        elif "callback_query" in update:
            handle_callback(update["callback_query"])
    except Exception:
        log.exception("error handling update")
    return "ok", 200

# ---------- startup (runs on import, so gunicorn triggers it too) ----------
def startup():
    global BOT_USERNAME
    if not BOT_TOKEN or not PUBLIC_URL:
        log.warning("BOT_TOKEN / PUBLIC_URL not set — skipping webhook setup (local test mode?)")
        return
    try:
        me = tg("getMe")
        BOT_USERNAME = me["username"]
        log.info("bot username: @%s", BOT_USERNAME)
    except Exception:
        log.exception("getMe failed (bad BOT_TOKEN?)")
        return
    # Retry: on PaaS the instance may not be routable for a few seconds after boot,
    # and Telegram validates reachability when setting the webhook.
    for attempt in range(1, 6):
        try:
            tg("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
            log.info("webhook set to %s/webhook (attempt %d)", PUBLIC_URL, attempt)
            return
        except Exception as e:
            log.warning("setWebhook attempt %d/5 failed: %s — retrying in 10s", attempt, e)
            time.sleep(10)
    log.error("giving up on setWebhook after 5 attempts (check PUBLIC_URL / logs)")


startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
