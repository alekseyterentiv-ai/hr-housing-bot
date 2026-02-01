import os
import requests
from flask import Flask, request

app = Flask(__name__)

# =========================
# ENV
# =========================
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""

TELEGRAM_SECRET_TOKEN = os.environ.get("TELEGRAM_SECRET_TOKEN", "").strip()

# fallback roles by env (if DB is off)
SUPERADMIN_IDS = {x.strip() for x in os.environ.get("SUPERADMIN_IDS", "").split(",") if x.strip()}
ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
BRIGADIER_IDS = {x.strip() for x in os.environ.get("BRIGADIER_IDS", "").split(",") if x.strip()}

# Optional DB settings (Cloud SQL Postgres)
DB_NAME = os.environ.get("DB_NAME", "").strip()
DB_USER = os.environ.get("DB_USER", "").strip()
DB_PASS = os.environ.get("DB_PASS", "").strip()
INSTANCE_CONNECTION_NAME = os.environ.get("INSTANCE_CONNECTION_NAME", "").strip()  # "project:region:instance"

# =========================
# Helpers
# =========================
def send_message(chat_id: int, text: str, reply_markup=None):
    if not TG_API:
        # If TOKEN is missing, we can't send messages, but service should still run
        return
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TG_API}/sendMessage", json=payload, timeout=10)
    except Exception:
        # don't crash web server because telegram is temporarily unreachable
        pass

def kb(rows):
    return {
        "keyboard": [[{"text": x} for x in r] for r in rows],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def main_menu(role: str):
    if role == "superadmin":
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

def get_role_from_env(chat_id: int) -> str:
    cid = str(chat_id)
    if cid in SUPERADMIN_IDS:
        return "superadmin"
    if cid in ADMIN_IDS:
        return "admin"
    if cid in BRIGADIER_IDS:
        return "brigadier"
    return "guest"

def db_enabled() -> bool:
    return bool(DB_NAME and DB_USER and DB_PASS and INSTANCE_CONNECTION_NAME)

def get_role_from_db(telegram_id: int) -> str | None:
    """
    Returns role from DB or None if not found / db off / error.
    IMPORTANT: Lazy import psycopg2 INSIDE function so Cloud Run boot is fast.
    """
    if not db_enabled():
        return None

    try:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        # Cloud SQL Unix socket path
        host = f"/cloudsql/{INSTANCE_CONNECTION_NAME}"

        conn = psycopg2.connect(
            dbname=DB_NAME,
            user=DB_USER,
            password=DB_PASS,
            host=host,
            connect_timeout=3,  # IMPORTANT: don't hang
        )
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cur:
                cur.execute(
                    "SELECT role FROM users WHERE telegram_id = %s LIMIT 1;",
                    (int(telegram_id),)
                )
                row = cur.fetchone()
                if row and row.get("role"):
                    return str(row["role"]).strip().lower()
                return None
        finally:
            conn.close()

    except Exception:
        return None

def get_role(chat_id: int) -> str:
    # 1) try DB
    role = get_role_from_db(chat_id)
    if role in ("superadmin", "admin", "brigadier", "guest"):
        return role
    # 2) fallback env
    return get_role_from_env(chat_id)

# =========================
# Routes (Cloud Run health)
# =========================
@app.get("/")
def index():
    # IMPORTANT: always return fast
    return "ok", 200

@app.get("/health")
def health():
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

    # /whoami
    if text.lower() == "/whoami":
        user_id = from_user.get("id")
        username = from_user.get("username", "")
        full_name = (" ".join([from_user.get("first_name", ""), from_user.get("last_name", "")])).strip()

        extra = ""
        if db_enabled():
            extra = "\n(db: on)"
        else:
            extra = "\n(db: off, env roles)"

        send_message(
            chat_id,
            f"chat_id: {chat_id}\nuser_id: {user_id}\nusername: @{username}\nname: {full_name}\nrole: {role}{extra}",
            reply_markup=main_menu(role),
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
            "Если доступ не выдан — пришли мне /whoami.",
            reply_markup=main_menu(role)
        )
        return "ok", 200

    # Access control
    if role == "guest":
        send_message(chat_id, "⛔ Нет доступа. Напиши /whoami и пришли мне chat_id.")
        return "ok", 200

    # Temporary echo
    send_message(chat_id, f"Принял: {text}", reply_markup=main_menu(role))
    return "ok", 200

# Local run (optional)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
