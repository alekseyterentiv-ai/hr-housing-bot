import os
import json
import psycopg2
from psycopg2.extras import RealDictCursor
import requests
from flask import Flask, request

app = Flask(__name__)

# ======================
# ENV
# ======================
TOKEN = os.environ.get("TELEGRAM_TOKEN")
TG_API = f"https://api.telegram.org/bot{TOKEN}"

PORT = int(os.environ.get("PORT", 8080))

SUPERADMIN_IDS = set(
    x.strip() for x in os.environ.get("SUPERADMIN_IDS", "").split(",") if x.strip()
)

DB_HOST = os.environ.get("DB_HOST")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASS = os.environ.get("DB_PASS")
DB_PORT = os.environ.get("DB_PORT", "5432")

# ======================
# DB
# ======================
def get_db():
    if not all([DB_HOST, DB_NAME, DB_USER, DB_PASS]):
        return None
    return psycopg2.connect(
        host=DB_HOST,
        port=DB_PORT,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        cursor_factory=RealDictCursor,
        connect_timeout=3,
    )

def get_user_role(telegram_id: str) -> str:
    # fallback
    if telegram_id in SUPERADMIN_IDS:
        return "super_admin"

    try:
        conn = get_db()
        if not conn:
            return "guest"

        with conn.cursor() as cur:
            cur.execute(
                "SELECT role FROM users WHERE telegram_id = %s AND is_active = true",
                (telegram_id,),
            )
            row = cur.fetchone()
            conn.close()

            if row:
                return row["role"]
    except Exception as e:
        print("DB ERROR:", e)

    return "guest"

# ======================
# TELEGRAM
# ======================
def send_message(chat_id, text):
    requests.post(
        f"{TG_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=5,
    )

# ======================
# ROUTES
# ======================
@app.route("/", methods=["GET"])
def health():
    return "ok", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.json

    if "message" not in update:
        return "ok", 200

    message = update["message"]
    chat_id = message["chat"]["id"]
    text = message.get("text", "")
    telegram_id = str(message["from"]["id"])

    role = get_user_role(telegram_id)

    if text == "/start":
        send_message(chat_id, f"Привет 👋\nТвоя роль: {role}")
    elif text == "/whoami":
        send_message(chat_id, f"ID: {telegram_id}\nРоль: {role}")
    else:
        send_message(chat_id, "Команда не распознана")

    return "ok", 200

# ======================
# ENTRY
# ======================
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
