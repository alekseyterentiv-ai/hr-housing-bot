from flask import Flask, request
import os
import time
import requests
import psycopg2
from psycopg2.extras import RealDictCursor

app = Flask(__name__)

# =========================
# ENV (Telegram)
# =========================
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TOKEN}"
TELEGRAM_SECRET_TOKEN = os.environ.get("TELEGRAM_SECRET_TOKEN", "").strip()

# =========================
# ENV (DB)
# =========================
DB_NAME = os.environ.get("DB_NAME", "").strip()          # например: hr_housing
DB_USER = os.environ.get("DB_USER", "").strip()          # например: postgres или твой пользователь
DB_PASS = os.environ.get("DB_PASS", "").strip()

# Вариант A: Unix socket (Cloud Run + Cloud SQL connection)
# пример: mini-bux:europe-west1:hr-housing-db
INSTANCE_CONNECTION_NAME = os.environ.get("INSTANCE_CONNECTION_NAME", "").strip()

# Вариант B: TCP
DB_HOST = os.environ.get("DB_HOST", "").strip()          # например: 10.x.x.x или public ip
DB_PORT = os.environ.get("DB_PORT", "5432").strip()

# Fallback роли (если БД временно недоступна)
SUPERADMIN_IDS = {x.strip() for x in os.environ.get("SUPERADMIN_IDS", "").split(",") if x.strip()}
ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
BRIGADIER_IDS = {x.strip() for x in os.environ.get("BRIGADIER_IDS", "").split(",") if x.strip()}


# =========================
# Telegram helpers
# =========================
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

def main_menu(role: str):
    # роли: super_admin, hr, house_manager, finance, brigadier, guest
    if role == "super_admin":
        return kb([
            ["🏢 Объекты", "👥 Сотрудники"],
            ["🏠 Квартиры", "📄 Документы"],
            ["🔁 Переводы", "📊 Отчёты"],
            ["⚙️ Настройки"],
        ])
    if role in ("hr", "admin"):
        return kb([
            ["🏢 Объекты", "👥 Сотрудники"],
            ["🏠 Квартиры", "📄 Документы"],
            ["🔁 Переводы", "📊 Отчёты"],
        ])
    if role == "house_manager":
        return kb([
            ["🏠 Квартиры", "👥 Сотрудники (просмотр)"],
            ["🔁 Переселить", "📊 Отчёт по жилью"],
        ])
    if role == "finance":
        return kb([
            ["📄 Документы", "⏰ Сроки оплат"],
            ["📊 Отчёты"],
        ])
    if role == "brigadier":
        return kb([
            ["👥 Сотрудники (объект)", "📄 Документы (объект)"],
            ["🏠 Жильё (объект)"],
        ])
    return kb([["/start"]])


# =========================
# DB helpers
# =========================
def db_connect():
    """
    Подключение к Cloud SQL Postgres:
    - если задан INSTANCE_CONNECTION_NAME -> unix socket /cloudsql/...
    - иначе TCP host/port
    """
    if not (DB_NAME and DB_USER and DB_PASS):
        raise RuntimeError("DB env vars are not set (DB_NAME/DB_USER/DB_PASS).")

    if INSTANCE_CONNECTION_NAME:
        # Unix socket
        return psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=f"/cloudsql/{INSTANCE_CONNECTION_NAME}",
            cursor_factory=RealDictCursor,
            connect_timeout=5,
        )

    if not DB_HOST:
        raise RuntimeError("No INSTANCE_CONNECTION_NAME and no DB_HOST set.")
    # TCP
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        host=DB_HOST,
        port=int(DB_PORT or "5432"),
        cursor_factory=RealDictCursor,
        connect_timeout=5,
        sslmode=os.environ.get("DB_SSLMODE", "prefer"),  # можно оставить prefer
    )

def ensure_user_row(telegram_id: int, chat_id: int, full_name: str, username: str):
    """
    Автосоздание пользователя как guest, если его нет (чтобы /whoami и меню работали).
    Роль не повышаем тут, только сохраняем данные.
    """
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (telegram_id, chat_id, full_name, username, role, is_active)
                VALUES (%s, %s, %s, %s, 'guest', TRUE)
                ON CONFLICT (telegram_id) DO UPDATE
                SET chat_id = EXCLUDED.chat_id,
                    full_name = EXCLUDED.full_name,
                    username = EXCLUDED.username,
                    is_active = TRUE;
                """,
                (telegram_id, chat_id, full_name, username)
            )
        conn.commit()

def get_role_from_db(telegram_id: int) -> str:
    with db_connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT role FROM users WHERE telegram_id = %s AND is_active = TRUE;", (telegram_id,))
            row = cur.fetchone()
            if not row:
                return "guest"
            return (row.get("role") or "guest").strip()

def get_role_fallback(telegram_id: int) -> str:
    tid = str(telegram_id)
    if tid in SUPERADMIN_IDS:
        return "super_admin"
    if tid in ADMIN_IDS:
        return "admin"
    if tid in BRIGADIER_IDS:
        return "brigadier"
    return "guest"

def get_role(telegram_id: int) -> str:
    """
    Сначала БД, если не получилось — fallback на env-списки.
    """
    try:
        return get_role_from_db(telegram_id)
    except Exception:
        return get_role_fallback(telegram_id)


# =========================
# Routes
# =========================
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
    telegram_id = from_user.get("id")  # ВАЖНО: роль по telegram user_id, не по chat_id

    if not chat_id or not telegram_id:
        return "no ids", 200

    text = (msg.get("text") or "").strip()
    username = from_user.get("username", "") or ""
    full_name = (" ".join([from_user.get("first_name", ""), from_user.get("last_name", "")])).strip()

    # 1) сохраняем пользователя в БД (если БД доступна)
    try:
        ensure_user_row(telegram_id, chat_id, full_name, username)
    except Exception:
        pass

    # 2) читаем роль
    role = get_role(telegram_id)

    # /whoami
    if text.lower() == "/whoami":
        send_message(
            chat_id,
            f"chat_id: {chat_id}\n"
            f"user_id: {telegram_id}\n"
            f"username: @{username}\n"
            f"name: {full_name}\n"
            f"role: {role}"
        )
        return "ok", 200

    # /start
    if text.startswith("/start"):
        send_message(
            chat_id,
            "HR Housing Control ✅\n\n"
            "Команды:\n"
            "/start — меню\n"
            "/whoami — показать chat_id\n\n"
            "Дальше подключаем таблицы/кнопки и делаем всё как в ТЗ.",
            reply_markup=main_menu(role)
        )
        return "ok", 200

    # Пока закрыто для guest
    if role == "guest":
        send_message(chat_id, "⛔ Нет доступа. Напиши /whoami и пришли user_id (это telegram_id).")
        return "ok", 200

    send_message(chat_id, f"Принял: {text}", reply_markup=main_menu(role))
    return "ok", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
