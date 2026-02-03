import os
import json
import requests
import psycopg2
from psycopg2.extras import RealDictCursor
from flask import Flask, request

app = Flask(__name__)

# =========================
# ENV
# =========================
TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TG_API = f"https://api.telegram.org/bot{TOKEN}"

TELEGRAM_SECRET_TOKEN = os.environ.get("TELEGRAM_SECRET_TOKEN", "").strip()
SUPERADMIN_IDS = {x.strip() for x in os.environ.get("SUPERADMIN_IDS", "").split(",") if x.strip()}

DB_NAME = os.environ.get("DB_NAME", "").strip()
DB_USER = os.environ.get("DB_USER", "").strip()
DB_PASS = os.environ.get("DB_PASS", "").strip()
INSTANCE_CONNECTION_NAME = os.environ.get("INSTANCE_CONNECTION_NAME", "").strip()

# Cloud SQL unix socket path:
# /cloudsql/<PROJECT:REGION:INSTANCE>
CLOUDSQL_DIR = "/cloudsql"

# =========================
# Helpers
# =========================
def tg(method: str, payload: dict):
    r = requests.post(f"{TG_API}/{method}", json=payload, timeout=30)
    return r.json()

def is_superadmin(user_id: int) -> bool:
    return str(user_id) in SUPERADMIN_IDS

def db_on() -> bool:
    return bool(DB_NAME and DB_USER and DB_PASS and INSTANCE_CONNECTION_NAME)

def get_conn():
    """
    Cloud Run -> Cloud SQL via unix socket.
    IMPORTANT: In Cloud Run you must add Cloud SQL connection in service settings (Connections)
    and INSTANCE_CONNECTION_NAME must match.
    """
    if not db_on():
        return None

    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        host=f"{CLOUDSQL_DIR}/{INSTANCE_CONNECTION_NAME}",
        cursor_factory=RealDictCursor,
    )

def sql_exec(query: str, params=None, fetch=False):
    conn = get_conn()
    if conn is None:
        return None
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(query, params or ())
                if fetch:
                    return cur.fetchall()
        return True
    finally:
        conn.close()

def reply_kb_main():
    # ReplyKeyboard (нижние кнопки)
    return {
        "keyboard": [
            [{"text": "🏢 Подразделения"}, {"text": "👥 Штат сотрудников"}],
            [{"text": "🏠 Жильё"}, {"text": "📄 Документы"}],
            [{"text": "🔁 Переводы"}, {"text": "📊 Отчёты"}],
            [{"text": "⚙️ Настройки"}],
        ],
        "resize_keyboard": True
    }

def safe_text(x):
    return "" if x is None else str(x)

# =========================
# DB schema + seed
# =========================
SEED_DEPARTMENTS = ["Обухово", "Одинцово", "Октябрьский", "Экипаж", "Ярцево"]

def init_db():
    if not db_on():
        return False

    sql_exec("""
    CREATE TABLE IF NOT EXISTS departments (
        id SERIAL PRIMARY KEY,
        name TEXT UNIQUE NOT NULL
    );
    """)

    sql_exec("""
    CREATE TABLE IF NOT EXISTS staff (
        id SERIAL PRIMARY KEY,
        full_name TEXT NOT NULL,
        position TEXT NOT NULL,
        department_id INT REFERENCES departments(id) ON DELETE SET NULL,
        created_at TIMESTAMP DEFAULT NOW()
    );
    """)

    # seed departments
    for name in SEED_DEPARTMENTS:
        sql_exec("INSERT INTO departments(name) VALUES(%s) ON CONFLICT (name) DO NOTHING;", (name,))

    return True

def list_departments():
    return sql_exec("SELECT id, name FROM departments ORDER BY name;", fetch=True) or []

def find_department_id_by_name(dep_name: str):
    rows = sql_exec("SELECT id FROM departments WHERE lower(name)=lower(%s) LIMIT 1;", (dep_name,), fetch=True) or []
    return rows[0]["id"] if rows else None

def list_staff(dep_name: str = None):
    if dep_name:
        return sql_exec("""
            SELECT s.id, s.full_name, s.position, d.name AS department
            FROM staff s
            LEFT JOIN departments d ON d.id = s.department_id
            WHERE lower(d.name)=lower(%s)
            ORDER BY s.id DESC;
        """, (dep_name,), fetch=True) or []
    return sql_exec("""
        SELECT s.id, s.full_name, s.position, d.name AS department
        FROM staff s
        LEFT JOIN departments d ON d.id = s.department_id
        ORDER BY s.id DESC;
    """, fetch=True) or []

# =========================
# Telegram logic
# =========================
def handle_start(chat_id: int):
    text = (
        "HR Housing Control ✅\n\n"
        "Команды:\n"
        "/start — меню\n"
        "/whoami — показать chat_id\n\n"
        "Если доступ не выдан — пришли мне /whoami."
    )
    tg("sendMessage", {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": reply_kb_main()
    })

def handle_whoami(chat_id: int, user_id: int, first_name: str, username: str):
    role = "superadmin" if is_superadmin(user_id) else "user"
    text = (
        f"chat_id: {chat_id}\n"
        f"user_id: {user_id}\n"
        f"username: @{safe_text(username)}\n"
        f"name: {safe_text(first_name)}\n"
        f"role: {role}\n"
        f"(db: {'on' if db_on() else 'off'})"
    )
    tg("sendMessage", {"chat_id": chat_id, "text": text, "reply_markup": reply_kb_main()})

def show_departments(chat_id: int):
    if not db_on():
        tg("sendMessage", {"chat_id": chat_id, "text": "База выключена (db: off). Проверь переменные Cloud Run."})
        return

    deps = list_departments()
    lines = ["🏢 Подразделения (объекты клиента):"]
    for d in deps:
        lines.append(f"- {d['name']} (id={d['id']})")

    lines.append("\nКоманды супер-админа:")
    lines.append("/dep — список")
    lines.append("/dep_add <название> — добавить")
    lines.append("/seed — создать таблицы + добавить 5 объектов")
    tg("sendMessage", {"chat_id": chat_id, "text": "\n".join(lines), "reply_markup": reply_kb_main()})

def show_staff(chat_id: int):
    if not db_on():
        tg("sendMessage", {"chat_id": chat_id, "text": "База выключена (db: off). Проверь переменные Cloud Run."})
        return

    rows = list_staff()
    lines = ["👥 Штат сотрудников:"]
    if not rows:
        lines.append("Пока пусто.")
    else:
        for r in rows[:50]:
            dep = r["department"] or "—"
            lines.append(f"#{r['id']} | {r['full_name']} | {r['position']} | {dep}")

    lines.append("\nКоманды супер-админа:")
    lines.append("/staff — список")
    lines.append("/staff <подразделение> — список по объекту")
    lines.append("/staff_add ФИО | должность | подразделение")
    lines.append("/staff_move <id> | подразделение")
    lines.append("/staff_del <id>")
    tg("sendMessage", {"chat_id": chat_id, "text": "\n".join(lines), "reply_markup": reply_kb_main()})

def handle_admin_commands(chat_id: int, user_id: int, text: str):
    if not is_superadmin(user_id):
        tg("sendMessage", {"chat_id": chat_id, "text": "Команда доступна только супер-админу."})
        return

    if text.startswith("/seed"):
        ok = init_db()
        tg("sendMessage", {"chat_id": chat_id, "text": "✅ DB init + seed готово" if ok else "❌ DB off / нет доступа"})
        return

    if text.startswith("/dep_add"):
        name = text.replace("/dep_add", "", 1).strip()
        if not name:
            tg("sendMessage", {"chat_id": chat_id, "text": "Пример: /dep_add Обухово-2"})
            return
        sql_exec("INSERT INTO departments(name) VALUES(%s) ON CONFLICT (name) DO NOTHING;", (name,))
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ Добавил подразделение: {name}"})
        return

    if text.startswith("/dep"):
        show_departments(chat_id)
        return

    if text.startswith("/staff_add"):
        # format: /staff_add ФИО | должность | подразделение
        raw = text.replace("/staff_add", "", 1).strip()
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 3:
            tg("sendMessage", {"chat_id": chat_id, "text": "Формат: /staff_add Иванов Иван | грузчик | Обухово"})
            return
        fio, pos, dep = parts
        dep_id = find_department_id_by_name(dep)
        if dep_id is None:
            tg("sendMessage", {"chat_id": chat_id, "text": f"❌ Не нашёл подразделение '{dep}'. Сначала добавь /dep_add {dep}"})
            return
        sql_exec("INSERT INTO staff(full_name, position, department_id) VALUES(%s,%s,%s);", (fio, pos, dep_id))
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ Добавил сотрудника: {fio} | {pos} | {dep}"})
        return

    if text.startswith("/staff_move"):
        # format: /staff_move <id> | <подразделение>
        raw = text.replace("/staff_move", "", 1).strip()
        parts = [p.strip() for p in raw.split("|")]
        if len(parts) != 2:
            tg("sendMessage", {"chat_id": chat_id, "text": "Формат: /staff_move 12 | Одинцово"})
            return
        staff_id, dep = parts
        if not staff_id.isdigit():
            tg("sendMessage", {"chat_id": chat_id, "text": "ID должен быть числом. Пример: /staff_move 12 | Одинцово"})
            return
        dep_id = find_department_id_by_name(dep)
        if dep_id is None:
            tg("sendMessage", {"chat_id": chat_id, "text": f"❌ Не нашёл подразделение '{dep}'"})
            return
        sql_exec("UPDATE staff SET department_id=%s WHERE id=%s;", (dep_id, int(staff_id)))
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ Перевёл сотрудника #{staff_id} в {dep}"})
        return

    if text.startswith("/staff_del"):
        sid = text.replace("/staff_del", "", 1).strip()
        if not sid.isdigit():
            tg("sendMessage", {"chat_id": chat_id, "text": "Формат: /staff_del 12"})
            return
        sql_exec("DELETE FROM staff WHERE id=%s;", (int(sid),))
        tg("sendMessage", {"chat_id": chat_id, "text": f"✅ Удалил сотрудника #{sid}"})
        return

    if text.startswith("/staff"):
        arg = text.replace("/staff", "", 1).strip()
        if arg:
            rows = list_staff(arg)
            lines = [f"👥 Штат — {arg}:"]
            if not rows:
                lines.append("Пусто.")
            else:
                for r in rows[:50]:
                    dep = r["department"] or "—"
                    lines.append(f"#{r['id']} | {r['full_name']} | {r['position']} | {dep}")
            tg("sendMessage", {"chat_id": chat_id, "text": "\n".join(lines)})
        else:
            show_staff(chat_id)
        return

def handle_text(chat_id: int, user_id: int, txt: str):
    txt = (txt or "").strip()

    # кнопки
    if txt == "🏢 Подразделения":
        show_departments(chat_id)
        return
    if txt == "👥 Штат сотрудников":
        show_staff(chat_id)
        return

    # команды
    if txt.startswith("/start"):
        handle_start(chat_id)
        return
    if txt.startswith("/whoami"):
        # whoami handled in webhook (we need user fields)
        return

    # админские команды
    if txt.startswith(("/seed", "/dep", "/dep_add", "/staff", "/staff_add", "/staff_move", "/staff_del")):
        handle_admin_commands(chat_id, user_id, txt)
        return

    # заглушка на остальные пункты
    if txt in ("🏠 Жильё", "📄 Документы", "🔁 Переводы", "📊 Отчёты", "⚙️ Настройки"):
        tg("sendMessage", {"chat_id": chat_id, "text": f"Принял: {txt}\n(Этот раздел подключим следующим шагом)", "reply_markup": reply_kb_main()})
        return

    tg("sendMessage", {"chat_id": chat_id, "text": "Не понял. Нажми /start", "reply_markup": reply_kb_main()})

# =========================
# Webhook + health
# =========================
@app.get("/")
def health():
    return "ok", 200

@app.route("/webhook", methods=["POST"])
def webhook():
    # Secret header check (optional)
    if TELEGRAM_SECRET_TOKEN:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != TELEGRAM_SECRET_TOKEN:
            return "forbidden", 403

    update = request.get_json(silent=True) or {}
    message = update.get("message") or update.get("edited_message")

    if not message:
        return "ok", 200

    chat_id = message["chat"]["id"]
    user = message.get("from", {})
    user_id = user.get("id")
    first_name = user.get("first_name", "")
    username = user.get("username", "")

    text = message.get("text", "")

    if text.strip().startswith("/whoami"):
        handle_whoami(chat_id, user_id, first_name, username)
        return "ok", 200

    handle_text(chat_id, user_id, text)
    return "ok", 200

# For local run (optional)
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    app.run(host="0.0.0.0", port=port)
