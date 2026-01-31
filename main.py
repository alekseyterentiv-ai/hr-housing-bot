from flask import Flask, request
import os
import requests

app = Flask(__name__)

# =========================
# ENV
# =========================
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TOKEN}"

TELEGRAM_SECRET_TOKEN = os.environ.get("TELEGRAM_SECRET_TOKEN", "").strip()

SUPERADMIN_IDS = {x.strip() for x in os.environ.get("SUPERADMIN_IDS", "").split(",") if x.strip()}
ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
BRIGADIER_IDS = {x.strip() for x in os.environ.get("BRIGADIER_IDS", "").split(",") if x.strip()}


# =========================
# HELPERS
# =========================
def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {
        "chat_id": chat_id,
        "text": text
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    requests.post(
        f"{TG_API}/sendMessage",
        json=payload,
        timeout=10
    )


def keyboard(rows):
    return {
        "keyboard": [[{"text": b} for b in row] for row in rows],
        "resize_keyboard": True
    }


def get_role(chat_id: int) -> str:
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
        return keyboard([
            ["🏢 Объекты", "🏠 Жильё"],
            ["👥 Сотрудники", "📄 Документы"],
            ["⚙️ Настройки"]
        ])

    if role == "admin":
        return keyboard([
            ["👥 Сотрудники", "🏠 Жильё"],
            ["📄 Документы"]
        ])

    if role == "brigadier":
        return keyboard([
            ["👥 Мои сотрудники"],
            ["🏠 Моё жильё"]
        ])

    return keyboard([["/start"]])


# =========================
# ROUTES
# =========================
@app.get("/")
def index():
    return "ok", 200


@app.post("/webhook")
def webhook():
    # Security header
    if TELEGRAM_SECRET_TOKEN:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if secret != TELEGRAM_SECRET_TOKEN:
            return "forbidden", 403

    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if not message:
        return "ok", 200

    chat_id = message["chat"]["id"]
    text = message.get("text", "").strip()
    from_user = message.get("from", {})

    role = get_role(chat_id)

    # /whoami
    if text == "/whoami":
        send_message(
            chat_id,
            f"chat_id: {chat_id}\n"
            f"user_id: {from_user.get('id')}\n"
            f"username: @{from_user.get('username','')}\n"
            f"name: {from_user.get('first_name','')}\n"
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
            "/whoami — информация",
            reply_markup=main_menu(role)
        )
        return "ok", 200

    # no access
    if role == "guest":
        send_message(chat_id, "⛔ Нет доступа. Напиши /whoami и пришли chat_id.")
        return "ok", 200

    # echo
    send_message(chat_id, f"Принял: {text}", reply_markup=main_menu(role))
    return "ok", 200


# =========================
# LOCAL
# =========================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
