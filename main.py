from flask import Flask, request
import os
import json
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

SUPERADMIN_IDS = {x.strip() for x in os.environ.get("SUPERADMIN_IDS", "").split(",") if x.strip()}
ADMIN_IDS = {x.strip() for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}
BRIGADIER_IDS = {x.strip() for x in os.environ.get("BRIGADIER_IDS", "").split(",") if x.strip()}

DB_NAME = os.environ.get("DB_NAME", "").strip()
DB_USER = os.environ.get("DB_USER", "").strip()
DB_PASS = os.environ.get("DB_PASS", "").strip()
INSTANCE_CONNECTION_NAME = os.environ.get("INSTANCE_CONNECTION_NAME", "").strip()

PORT = int(os.environ.get("PORT", "8080"))

# =========================
# SIMPLE KEYBOARD HELPERS
# =========================
def kb(rows):
    return {"keyboard": [[{"text": t} for t in row] for row in rows], "resize_keyboard": True}

def main_menu(role: str):
    rows = [
        ["🏢 Подразделения", "👥 Штат сотрудников"],
        ["🏠 Жильё", "📄 Документы"],
        ["💸 Переводы", "📊 Отчёты"],
        ["⚙️ Настройки"],
    ]
    return kb(rows)

def departments_menu():
    return kb([
        ["⚡ Создать 5 объектов", "➕ Добавить подразделение"],
        ["📋 Список подразделений"],
        ["⬅️ Назад"]
    ])

def staff_menu():
    return kb([
        ["➕ Добавить сотрудника", "📋 Список по подразделению"],
        ["🔎 Найти сотрудника"],
        ["⬅️ Назад"]
    ])

# =========================
# STATE (simple FSM)
# =========================
USER_STATE = {}  # chat_id(str) -> {"step": "...", "data": {...}}

BOOTSTRAP_DEPARTMENTS = ["Обухово", "Одинцово", "Октябрьский", "Экипаж", "Ярцево"]

# =========================
# TELEGRAM SEND
# =========================
def send_message(chat_id: int, text: str, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TG_API}/sendMessage", json=payload, timeout=10)
    except Exception:
        pass

# =========================
# DB CONNECTION (Cloud SQL via unix socket)
# DB_HOST НЕ НУЖЕН
# =========================
def get_db_conn():
    if not (DB_NAME and DB_USER and DB_PASS and INSTANCE_CONNECTION_NAME):
        raise RuntimeError("DB env vars not set")

    # Cloud Run + Cloud SQL (unix socket)
    host = f"/cloudsql/{INSTANCE_CONNECTION_NAME}"
    return psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS,
        host=host,
        port=5432,
        connect_timeout=5,
    )

def db_ping() -> bool:
    try:
        conn = get_db_conn()
        conn.close()
        return True
    except Exception:
        return False

# =========================
# ROLE: DB -> fallback env
# =========================
def role_from_env(telegram_id: int) -> str:
    tid = str(telegram_id)
    if tid in SUPERADMIN_IDS:
        return "superadmin"
    if tid in ADMIN_IDS:
        return "admin"
    if tid in BRIGADIER_IDS:
        return "brigadier"
    return "guest"

def role_from_db(telegram_id: int) -> str | None:
    conn = get_db_conn()
    with conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("select role from users where telegram_id = %s limit 1", (telegram_id,))
            row = cur.fetchone()
            return row["role"] if row and row.get("role") else None

def get_role(telegram_id: int) -> tuple[str, bool]:
    """
    returns (role, db_on)
    """
    try:
        r = role_from_db(telegram_id)
        if r:
            return r, True
        return role_from_env(telegram_id), True  # db ok, user not found -> env fallback
    except Exception:
        return role_from_env(telegram_id), False

# =========================
# DB TABLES for departments/staff
# =========================
def ensure_tables():
    conn = get_db_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
            create table if not exists departments (
              id bigserial primary key,
              name text not null,
              city text,
              address text,
              note text,
              is_active boolean not null default true,
              created_at timestamptz not null default now()
            );
            """)
            cur.execute("""
            create unique index if not exists ux_departments_name
            on departments (lower(name));
            """)

            cur.execute("""
            create table if not exists employees (
              id bigserial primary key,
              fio text not null,
              phone text,
              telegram_id bigint,
              birth_date date,
              passport text,
              note text,
              is_active boolean not null default true,
              created_at timestamptz not null default now()
            );
            """)
            cur.execute("create index if not exists ix_employees_fio on employees (lower(fio));")
            cur.execute("create index if not exists ix_employees_telegram on employees (telegram_id);")

            cur.execute("""
            create table if not exists employee_department (
              id bigserial primary key,
              employee_id bigint not null references employees(id) on delete cascade,
              department_id bigint not null references departments(id) on delete restrict,
              position text,
              start_date date not null default current_date,
              end_date date,
              created_at timestamptz not null default now()
            );
            """)
            cur.execute("create index if not exists ix_empdep_employee on employee_department(employee_id);")
            cur.execute("create index if not exists ix_empdep_dept on employee_department(department_id);")

def db_create_department(name: str) -> bool:
    conn = get_db_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                "insert into departments(name) values (%s) on conflict do nothing",
                (name.strip(),)
            )
            return cur.rowcount == 1

def db_bootstrap_departments(names):
    created = 0
    for n in names:
        try:
            if db_create_department(n):
                created += 1
        except Exception:
            pass
    return created

def db_list_departments(limit=50):
    conn = get_db_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            select id, name, is_active
            from departments
            where is_active = true
            order by name asc
            limit %s
        """, (limit,))
        return cur.fetchall()

def db_get_department_by_name(name: str):
    conn = get_db_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            select id, name
            from departments
            where lower(name) = lower(%s) and is_active = true
            limit 1
        """, (name.strip(),))
        return cur.fetchone()

def db_create_employee(fio: str, phone: str, telegram_id: int | None = None) -> int | None:
    conn = get_db_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                insert into employees(fio, phone, telegram_id)
                values (%s, %s, %s)
                returning id
            """, (fio.strip(), phone.strip(), telegram_id))
            row = cur.fetchone()
            return row[0] if row else None

def db_assign_employee_to_department(employee_id: int, department_id: int, position: str | None = None) -> bool:
    conn = get_db_conn()
    with conn:
        with conn.cursor() as cur:
            cur.execute("""
                update employee_department
                set end_date = current_date
                where employee_id = %s and end_date is null
            """, (employee_id,))
            cur.execute("""
                insert into employee_department(employee_id, department_id, position)
                values (%s, %s, %s)
            """, (employee_id, department_id, position))
            return True

def db_list_staff_by_department(dept_id: int, limit=200):
    conn = get_db_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            select e.id, e.fio, e.phone
            from employee_department ed
            join employees e on e.id = ed.employee_id
            where ed.department_id = %s and ed.end_date is null and e.is_active = true
            order by e.fio asc
            limit %s
        """, (dept_id, limit))
        return cur.fetchall()

def db_find_employee(q: str, limit=20):
    conn = get_db_conn()
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            select id, fio, phone
            from employees
            where is_active = true
              and (lower(fio) like lower(%s) or phone like %s)
            order by fio asc
            limit %s
        """, (f"%{q.strip()}%", f"%{q.strip()}%", limit))
        return cur.fetchall()

# =========================
# ROUTES
# =========================
@app.get("/")
def root():
    return "ok", 200

@app.get("/health")
def health():
    return {"ok": True, "db": db_ping()}, 200

@app.post("/webhook")
def webhook():
    # Telegram secret token verification (optional but recommended)
    if TELEGRAM_SECRET_TOKEN:
        got = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if got != TELEGRAM_SECRET_TOKEN:
            return "forbidden", 403

    update = request.get_json(silent=True) or {}
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return "ok", 200

    chat_id = msg["chat"]["id"]
    from_user = msg.get("from", {})
    user_id = from_user.get("id")
    text = (msg.get("text") or "").strip()

    role, db_on = get_role(user_id)

    # access control
    if role == "guest":
        if text in ("/start", "/whoami"):
            send_message(chat_id, f"chat_id: {chat_id}\nuser_id: {user_id}\nrole: guest\n(db: {'on' if db_on else 'off'})")
        else:
            send_message(chat_id, "⛔ Нет доступа. Пришли /whoami и отправь мне chat_id.")
        return "ok", 200

    # commands
    if text == "/start":
        send_message(chat_id,
                     "Меню:\n/start — меню\n/whoami — показать chat_id\n\nЕсли доступ не выдан — пришли мне /whoami.",
                     reply_markup=main_menu(role))
        return "ok", 200

    if text == "/whoami":
        username = from_user.get("username", "")
        name = (from_user.get("first_name", "") + " " + from_user.get("last_name", "")).strip()
        send_message(chat_id,
                     f"chat_id: {chat_id}\nuser_id: {user_id}\nusername: @{username}\nname: {name}\nrole: {role}\n(db: {'on' if db_on else 'off'})",
                     reply_markup=main_menu(role))
        return "ok", 200

    # ensure tables (only if DB is on)
    if db_on:
        try:
            ensure_tables()
        except Exception:
            pass

    cid = str(chat_id)
    state = USER_STATE.get(cid, {})

    # ---- main sections ----
    if text == "🏢 Подразделения":
        send_message(chat_id, "🏢 Подразделения:", reply_markup=departments_menu())
        return "ok", 200

    if text == "👥 Штат сотрудников":
        send_message(chat_id, "👥 Штат сотрудников:", reply_markup=staff_menu())
        return "ok", 200

    if text == "⬅️ Назад":
        USER_STATE.pop(cid, None)
        send_message(chat_id, "Главное меню:", reply_markup=main_menu(role))
        return "ok", 200

    # =========================
    # DEPARTMENTS
    # =========================
    if text == "⚡ Создать 5 объектов":
        if not db_on:
            send_message(chat_id, "⚠️ БД недоступна. Невозможно создать подразделения.", reply_markup=departments_menu())
            return "ok", 200
        created = db_bootstrap_departments(BOOTSTRAP_DEPARTMENTS)
        send_message(chat_id, f"✅ Готово. Создано новых: {created}", reply_markup=departments_menu())
        return "ok", 200

    if text == "➕ Добавить подразделение":
        if not db_on:
            send_message(chat_id, "⚠️ БД недоступна. Невозможно добавить.", reply_markup=departments_menu())
            return "ok", 200
        USER_STATE[cid] = {"step": "dept_add_name", "data": {}}
        send_message(chat_id, "Введи название подразделения (например: Обухово):")
        return "ok", 200

    if state.get("step") == "dept_add_name":
        name = text.strip()
        ok = db_create_department(name)
        USER_STATE.pop(cid, None)
        if ok:
            send_message(chat_id, f"✅ Подразделение добавлено: {name}", reply_markup=departments_menu())
        else:
            send_message(chat_id, f"⚠️ Не добавил (возможно уже есть): {name}", reply_markup=departments_menu())
        return "ok", 200

    if text == "📋 Список подразделений":
        if not db_on:
            send_message(chat_id, "⚠️ БД недоступна. Невозможно показать список.", reply_markup=departments_menu())
            return "ok", 200
        rows = db_list_departments()
        if not rows:
            send_message(chat_id, "Пока нет подразделений.", reply_markup=departments_menu())
            return "ok", 200
        lines = [f'#{r["id"]} — {r["name"]}' for r in rows]
        send_message(chat_id, "📋 Подразделения:\n" + "\n".join(lines), reply_markup=departments_menu())
        return "ok", 200

    # =========================
    # STAFF
    # =========================
    if text == "➕ Добавить сотрудника":
        if not db_on:
            send_message(chat_id, "⚠️ БД недоступна. Невозможно добавить сотрудника.", reply_markup=staff_menu())
            return "ok", 200
        USER_STATE[cid] = {"step": "emp_add_fio", "data": {}}
        send_message(chat_id, "Введи ФИО сотрудника:")
        return "ok", 200

    if state.get("step") == "emp_add_fio":
        fio = text.strip()
        USER_STATE[cid] = {"step": "emp_add_phone", "data": {"fio": fio}}
        send_message(chat_id, "Введи телефон (как есть, можно без +):")
        return "ok", 200

    if state.get("step") == "emp_add_phone":
        phone = text.strip()
        fio = state.get("data", {}).get("fio", "")
        USER_STATE[cid] = {"step": "emp_choose_dept", "data": {"fio": fio, "phone": phone}}
        deps = db_list_departments()
        if not deps:
            send_message(chat_id, "⚠️ Нет подразделений. Сначала создай их в 'Подразделения'.", reply_markup=staff_menu())
            USER_STATE.pop(cid, None)
            return "ok", 200
        buttons = [[d["name"]] for d in deps[:20]]
        buttons.append(["⬅️ Назад"])
        send_message(chat_id, "Выбери подразделение:", reply_markup=kb(buttons))
        return "ok", 200

    if state.get("step") == "emp_choose_dept":
        dept_name = text.strip()
        dept = db_get_department_by_name(dept_name)
        if not dept:
            send_message(chat_id, "Не понял подразделение. Нажми кнопку из списка.")
            return "ok", 200

        fio = state.get("data", {}).get("fio", "")
        phone = state.get("data", {}).get("phone", "")
        emp_id = db_create_employee(fio=fio, phone=phone, telegram_id=from_user.get("id"))
        if emp_id:
            db_assign_employee_to_department(emp_id, dept["id"])
            send_message(chat_id, f"✅ Сотрудник добавлен:\n{fio}\n{phone}\n→ {dept['name']}", reply_markup=staff_menu())
        else:
            send_message(chat_id, "⚠️ Не смог создать сотрудника.", reply_markup=staff_menu())
        USER_STATE.pop(cid, None)
        return "ok", 200

    if text == "📋 Список по подразделению":
        if not db_on:
            send_message(chat_id, "⚠️ БД недоступна.", reply_markup=staff_menu())
            return "ok", 200
        USER_STATE[cid] = {"step": "staff_list_choose_dept", "data": {}}
        deps = db_list_departments()
        if not deps:
            send_message(chat_id, "⚠️ Нет подразделений.", reply_markup=staff_menu())
            USER_STATE.pop(cid, None)
            return "ok", 200
        buttons = [[d["name"]] for d in deps[:20]]
        buttons.append(["⬅️ Назад"])
        send_message(chat_id, "Выбери подразделение:", reply_markup=kb(buttons))
        return "ok", 200

    if state.get("step") == "staff_list_choose_dept":
        dept = db_get_department_by_name(text.strip())
        if not dept:
            send_message(chat_id, "Нажми подразделение кнопкой.")
            return "ok", 200
        staff = db_list_staff_by_department(dept["id"])
        USER_STATE.pop(cid, None)
        if not staff:
            send_message(chat_id, f"В {dept['name']} пока нет сотрудников.", reply_markup=staff_menu())
            return "ok", 200
        lines = [f'#{s["id"]} — {s["fio"]} ({s["phone"] or "-"})' for s in staff]
        send_message(chat_id, f"👥 {dept['name']}:\n" + "\n".join(lines), reply_markup=staff_menu())
        return "ok", 200

    if text == "🔎 Найти сотрудника":
        if not db_on:
            send_message(chat_id, "⚠️ БД недоступна.", reply_markup=staff_menu())
            return "ok", 200
        USER_STATE[cid] = {"step": "emp_find_query", "data": {}}
        send_message(chat_id, "Введи ФИО или телефон для поиска:")
        return "ok", 200

    if state.get("step") == "emp_find_query":
        q = text.strip()
        rows = db_find_employee(q)
        USER_STATE.pop(cid, None)
        if not rows:
            send_message(chat_id, "Ничего не нашёл.", reply_markup=staff_menu())
            return "ok", 200
        lines = [f'#{r["id"]} — {r["fio"]} ({r["phone"] or "-"})' for r in rows]
        send_message(chat_id, "🔎 Найдено:\n" + "\n".join(lines), reply_markup=staff_menu())
        return "ok", 200

    # default
    send_message(chat_id, "Принял: " + text, reply_markup=main_menu(role))
    return "ok", 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
