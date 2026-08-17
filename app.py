"""Church cell prayer request Telegram bot.

Flow: /start -> name (or anonymous) -> topic -> request -> support -> share?
Saves each request to the Google Sheet via an Apps Script web app,
replies with a random encouraging verse, and optionally posts to the group chat.
"""
import html
import logging
import os
import random
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
ASK_NAME, ASK_TOPIC, ASK_REQUEST, ASK_SUPPORT, ASK_SHARE = range(5)
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
    r.raise_for_status()
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error: {data}")
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


def start_flow(user_id, chat_id):
    pending[user_id] = {"step": ASK_NAME, "ts": time.time()}
    send(chat_id,
         "I'd love to help you share a prayer request. 🙏\n\n"
         "First — what should we call you?\n"
         "(Type your name, or 'anonymous' if you'd rather keep it private.)")


def finish_request(user_id, chat_id, share):
    s = pending.pop(user_id, None)
    if not s:
        return
    try:
        res = sheet_call("add", name=s.get("name") or "Anonymous", topic=s["topic"],
                         request=s["request"], update="", support=s.get("support", ""))
    except Exception:
        log.exception("sheet add failed")
        send(chat_id, "⚠️ Sorry, I couldn't save your request just now. Please try again in a moment.")
        return
    if not res.get("ok"):
        send(chat_id, f"⚠️ Something went wrong saving your request: {res.get('error')}")
        return

    no = res["no"]
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
        group_msg = (
            f"🙏 <b>New prayer request #{no}</b>\n\n"
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
            lines = ["📖 <b>Recent prayer requests</b>\n"]
            for it in items[:5]:  # newest first
                name = html.escape(str(it.get("name") or "Anonymous"))
                topic = html.escape(str(it.get("topic") or ""))
                req = html.escape(str(it.get("request") or ""))
                lines.append(f"#{it.get('no')} · {name} — {topic}\n“{req}”\n")
            send(chat_id, "\n".join(lines), parse_mode="HTML")
            return

        elif cmd == "/groupid":
            send(chat_id, f"This chat id is: <code>{chat_id}</code>", parse_mode="HTML")
            return

        elif cmd == "/help":
            send(chat_id,
                 "🙏 I help our cell group share prayer requests.\n\n"
                 "/start — share a new prayer request (you can stay anonymous)\n"
                 "/prayers — see the most recent requests\n"
                 "/groupid — show this chat's id (for setup)\n\n"
                 "Just send /start and I'll walk you through it, step by step.")
            return

        else:
            return  # unknown command, ignore

    # ----- conversation flow -----
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


def handle_callback(cb):
    user_id = cb["from"]["id"]
    chat_id = (cb.get("message") or cb.get("chat") or {}).get("chat", {}).get("id") or cb.get("chat", {}).get("id")
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
    try:
        tg("setWebhook", url=f"{PUBLIC_URL}/webhook", secret_token=WEBHOOK_SECRET, drop_pending_updates=False)
        log.info("webhook set to %s/webhook", PUBLIC_URL)
    except Exception:
        log.exception("setWebhook failed (check PUBLIC_URL)")


startup()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
