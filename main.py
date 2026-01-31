from flask import Flask, request
import os
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# =========================
# ENV
# =========================
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TOKEN}"

TELEGRAM_SECRET_TOKEN = os.environ.get("TELEGRAM_SECRET_TOKEN", "").strip()

# fallback роли (если БД временно недоступна)
SUPERADMIN_IDS = {x.strip() for x in os.environ.get("SUPERADMIN_IDS", "").split(",") if x.strip()}
ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
BRIGADIER_IDS = {x.strip() for x in os.environ.get("BRIGADIER_IDS", "").split(",") if x.strip()}

# =========================
# DATABASE (Cloud SQL)
# =========================
DB_HOST = os.environ.get("DB_HOST")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASSWORD = os.environ.get("DB_PASSWORD")
DB_PORT = os.environ.get("DB_PORT", "5432")


def get_db_conn():
    return psycopg2.connect(
        host=DB_HOST,
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        port=DB_PORT,
        cursor_factory=RealDictCursor,
        connect_timeout=5
    )


def get_role_from_db(telegram_id: int) -> str | None:
    try:
        conn = get_db_conn()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role FROM users WHERE telegram_id = %s AND is_active = TRUE",
                    (telegram_id,)
                )
                row = cur.fetchone()
                return row["role"] if row else None
    except Exception as e:
        print("DB error:", e)
        return None


# =========================
# TELEGRAM HELPERS
# =========================
def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TG_API}/sendMessage", json=payload, timeout=15)


def kb(rows):
    return {
        "keyboard": [[{"text": x} for x in r] for r in rows],
        "resize_keyboard": True
    }


def get_role(chat_id: int) -> str:
    # 1. Пытаемся взять роль из БД
    role = get_role_from_db(chat_id)
    if role:
        return role

    # 2. fallback на ENV
    cid = str(chat_id)
    if cid in SUPERADMIN_IDS:
        return "super_admin"
    if cid in ADMIN_IDS:
        return "admin"
    if cid in BRIGADIER_IDS:
        return "brigadier"

    return "guest"


def main_menu(role: str):
    if role == "super_admin":
        return kb([
            ["🏢 Объекты", "👥 Штат"],
            ["🏠 Жильё", "📄 Документы"],
            ["🔁 Переводы", "📊 Отчёты"],
            ["⚙️ Настройки"],
        ])
    if role == "admin":
        return kb([
            ["👥 Штат", "🏠 Жильё"],
            ["📄 Документы", "📊 Отчёты"],
        ])
    if role == "brigadier":
        return kb([
            ["👥 Штат (просмотр)"],
            ["🏠 Жильё"],
        ])
    return kb([["/start"]])


# =========================
# ROUTES
# =========================
@app.get("/")
def index():
    return "ok", 200


@app.post("/webhook")
def webhook():
    # защита webhook
    if TELEGRAM_SECRET_TOKEN:
        token = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != TELEGRAM_SECRET_TOKEN:
            return "forbidden", 403

    data = request.get_json(silent=True) or {}
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return "ok", 200

    chat = msg.get("chat", {})
    user = msg.get("from", {})
    chat_id = chat.get("id")
    text = (msg.get("text") or "").strip()

    if not chat_id:
        return "ok", 200

    role = get_role(chat_id)

    if text == "/whoami":
        send_message(
            chat_id,
            f"chat_id: {chat_id}\n"
            f"user_id: {user.get('id')}\n"
            f"username: @{user.get('username','')}\n"
            f"name: {user.get('first_name','')}\n"
            f"role: {role}"
        )
        return "ok", 200

    if text.startswith("/start"):
        send_message(
            chat_id,
            "HR Housing Control ✅\n\n"
            "/start — меню\n"
            "/whoami — показать роль\n",
            reply_markup=main_menu(role)
        )
        return "ok", 200

    if role == "guest":
        send_message(chat_id, "⛔ Нет доступа. Обратись к администратору.")
        return "ok", 200

    send_message(chat_id, f"Принял: {text}", reply_markup=main_menu(role))
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
