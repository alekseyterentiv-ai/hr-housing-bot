from flask import Flask, request
import os
import requests
import psycopg
from psycopg.rows import dict_row

app = Flask(__name__)

# =========================
# ENV
# =========================
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TOKEN}"
TELEGRAM_SECRET_TOKEN = os.environ.get("TELEGRAM_SECRET_TOKEN", "").strip()

# fallback роли (если БД недоступна)
SUPERADMIN_IDS = {x.strip() for x in os.environ.get("SUPERADMIN_IDS", "").split(",") if x.strip()}
ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
BRIGADIER_IDS = {x.strip() for x in os.environ.get("BRIGADIER_IDS", "").split(",") if x.strip()}

# DB
DB_NAME = os.environ.get("DB_NAME", "hr_housing").strip()
DB_USER = os.environ.get("DB_USER", "postgres").strip()
DB_PASS = os.environ.get("DB_PASS", "").strip()

# Cloud SQL socket (лучший вариант)
INSTANCE_CONNECTION_NAME = os.environ.get("INSTANCE_CONNECTION_NAME", "").strip()
# Если вдруг подключение по IP:
DB_HOST = os.environ.get("DB_HOST", "").strip()
DB_PORT = os.environ.get("DB_PORT", "5432").strip()


def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TG_API}/sendMessage", json=payload, timeout=20)


def kb(rows):
    return {
        "keyboard": [[{"text": x} for x in r] for r in rows],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }


def get_role_fallback(chat_id: int) -> str:
    cid = str(chat_id)
    if cid in SUPERADMIN_IDS:
        return "super_admin"
    if cid in ADMIN_IDS:
        return "admin"
    if cid in BRIGADIER_IDS:
        return "brigadier"
    return "guest"


def get_db_conn():
    # Через Cloud SQL socket
    if INSTANCE_CONNECTION_NAME:
        host = f"/cloudsql/{INSTANCE_CONNECTION_NAME}"
        return psycopg.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=host,
            row_factory=dict_row,
            connect_timeout=5,
        )

    # Через IP/host
    if DB_HOST:
        return psycopg.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=DB_HOST,
            port=int(DB_PORT),
            row_factory=dict_row,
            connect_timeout=5,
        )

    raise RuntimeError("Нет настроек БД: INSTANCE_CONNECTION_NAME или DB_HOST")


def get_role(chat_id: int) -> str:
    # 1) пробуем БД
    try:
        with get_db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT role FROM users WHERE telegram_id = %s AND is_active = TRUE LIMIT 1",
                    (int(chat_id),)
                )
                row = cur.fetchone()
                if row and row.get("role"):
                    return row["role"]
        return "guest"
    except Exception:
        # 2) fallback если БД недоступна
        return get_role_fallback(chat_id)


def main_menu(role: str):
    if role == "super_admin":
        return kb([
            ["🏢 Подразделения", "👥 Штат сотрудников"],
            ["🏠 Жильё", "📄 Документы"],
            ["🔁 Переводы", "📊 Отчёты"],
            ["⚙️ Настройки"],
        ])
    if role == "admin":
        return kb([
            ["👥 Штат сотрудников", "🏠 Жильё"],
            ["📄 Документы", "📊 Отчёты"],
        ])
    if role == "brigadier":
        return kb([
            ["🏠 Жильё", "👥 Штат (просмотр)"],
        ])
    return kb([["/start"]])


@app.get("/")
def index():
    return "ok", 200


@app.post("/webhook")
def webhook():
    # --- Webhook security ---
    if TELEGRAM_SECRET_TOKEN:
        got = (request.headers.get("X-Telegram-Bot-Api-Secret-Token") or "").strip()
        if got != TELEGRAM_SECRET_TOKEN:
            return "forbidden", 403

    data = request.get_json(silent=True) or {}
    msg = data.get("message") or data.get("edited_message")
    if not msg:
        return "no message", 200

    chat = msg.get("chat") or {}
    from_user = msg.get("from") or {}
    chat_id = chat.get("id")
    if not chat_id:
        return "no chat", 200

    text = (msg.get("text") or "").strip()
    role = get_role(chat_id)

    if text.lower() == "/whoami":
        user_id = from_user.get("id")
        username = from_user.get("username", "")
        full_name = (" ".join([from_user.get("first_name", ""), from_user.get("last_name", "")])).strip()
        send_message(chat_id, f"chat_id: {chat_id}\nuser_id: {user_id}\nusername: @{username}\nname: {full_name}\nrole: {role}")
        return "ok", 200

    if text.startswith("/start"):
        send_message(
            chat_id,
            "HR Housing Control ✅\n\n"
            "Команды:\n"
            "/start — меню\n"
            "/whoami — показать chat_id\n\n"
            "Меню зависит от роли (берём из БД users).",
            reply_markup=main_menu(role)
        )
        return "ok", 200

    if role == "guest":
        send_message(chat_id, "⛔ Нет доступа. Напиши /whoami и пришли мне chat_id.")
        return "ok", 200

    send_message(chat_id, f"Принял: {text}", reply_markup=main_menu(role))
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
