import os
import asyncio
import csv
import io
import random
from datetime import datetime

# Windows: policy for Python 3.11+ (fix "no current event loop")
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
import aiosqlite

# ===== SETTINGS =====
BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
ADMIN = os.getenv("ADMIN", "@Vladz97")  # можна вказати @username або список через кому, або numeric id як рядок

DB_PATH = "scores.db"
SCORES = list(range(0, 6))  # 0..5

# Пер-раундні критерії (сума), з емоджі для 1 і 2 раунду
CRITERIA_BY_ROUND = {
    1: ["💡 Креативність", "🧩 Концепт", "🎨 Візуальна привабливість"],
    2: ["😂 Гумор", "🎭 Артистизм", "🌟 Оригінальність ідеї"],
}

# ===== HELPERS =====
def is_admin(user) -> bool:
    if not ADMIN:
        return False
    uid = str(user.id)
    uname = (user.username or "").lower()
    admin_ids = set()
    admin_usernames = set()
    for token in str(ADMIN).split(","):
        token = token.strip()
        if not token:
            continue
        if token.isdigit():
            admin_ids.add(token)
        else:
            admin_usernames.add(token.lstrip("@").lower())
    return uid in admin_ids or (uname and uname in admin_usernames)

# журі — кожен, хто розшарив телефон (зберігаємо у БД)
async def judge_allowed(user) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT phone FROM users WHERE user_id=?", (user.id,))
        row = await cur.fetchone()
        return bool(row and row[0])

# ===== DB =====
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # aggregate (sum per team per round)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scores (
                judge_id INTEGER,
                judge_username TEXT,
                team TEXT,
                round INTEGER,
                score INTEGER,
                ts TEXT,
                PRIMARY KEY (judge_id, team, round)
            )
        """)
        # detailed by criterion
        await db.execute("""
            CREATE TABLE IF NOT EXISTS scores_detailed (
                judge_id INTEGER,
                judge_username TEXT,
                team TEXT,
                round INTEGER,
                criterion TEXT,
                raw_score INTEGER,
                weight REAL,
                ts TEXT,
                PRIMARY KEY (judge_id, team, round, criterion)
            )
        """)
        # teams: position = 1..N
        await db.execute("""
            CREATE TABLE IF NOT EXISTS teams (
                position INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)
        # dynamic rounds: id, name
        await db.execute("""
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL
            )
        """)
        # users: хто розшарив телефон — може бути журі
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                full_name TEXT,
                phone TEXT
            )
        """)
        # alias words
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alias_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT UNIQUE NOT NULL
            )
        """)
        # які слова вже отримував користувач (щоб не повторювались для нього)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alias_used (
                user_id INTEGER,
                word_id INTEGER,
                PRIMARY KEY (user_id, word_id)
            )
        """)
        await db.commit()

        # seed teams if empty
        cur = await db.execute("SELECT COUNT(*) FROM teams")
        (cnt,) = await cur.fetchone()
        if cnt == 0:
            defaults = [f"Команда {i}" for i in range(1, 11)]
            for i, name in enumerate(defaults, start=1):
                await db.execute("INSERT INTO teams(position, name) VALUES(?, ?)", (i, name))
            await db.commit()

        # seed rounds if empty (ids 1 & 2 to match criteria mapping)
        cur = await db.execute("SELECT COUNT(*) FROM rounds")
        (rcnt,) = await cur.fetchone()
        if rcnt == 0:
            await db.execute("INSERT INTO rounds(id, name) VALUES(1, ?)", ("Раунд 1",))
            await db.execute("INSERT INTO rounds(id, name) VALUES(2, ?)", ("Раунд 2",))
            await db.commit()

        # seed alias words if empty
        cur = await db.execute("SELECT COUNT(*) FROM alias_words")
        (wcnt,) = await cur.fetchone()
        if wcnt == 0:
            seed_words = [
                "Мавка", "Петруцалек", "Шоллер", "Інструкція", "Патісон", "Артикул", "Вітрина",
                "Елітка", "Палета", "Стікер", "Чотири зустрічі", "Рол-кейдж", "Заморозка",
                "Глобальний переоблік", "Бонет", "Доступність", "Контрольний лист", "Чек",
                "Суламіф", "Люля-кебаб", "Вантажник", "Гарбуз", "Виторг", "Мурашник", "Морозиво",
                "Рампа", "Штрихкод", "Самокаса", "Бограч", "Протермін", "Сировина", "Пенетрація",
                "Регал", "Поверенння", "Багет", "Дашборд", "Касир", "Порей", "Втрати",
                "Кошик з кремом", "Пароконвектомат", "Завгосп", "Даркстор", "Рація", "Тара",
                "Журнал", "Затарка", "Стейк", "Торець", "Кур’єр", "Накладна", "Стелаж",
                "Адміністратор", "Віскі", "Плов", "Корегуючий переоблік", "Наполеон", "Цінник",
                "Скоропорт", "Айпод", "Картадор", "Задача", "Полиця", "Бейдж", "Клінінг",
                "Піцестанція", "Викладка", "Зарплата", "Рубікон", "Фотозвіт", "Куратор",
                "Перевершник", "Радість"
            ]
            for w in seed_words:
                try:
                    await db.execute("INSERT INTO alias_words(word) VALUES(?)", (w,))
                except Exception:
                    pass
            await db.commit()

async def load_team_rows():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT position, name FROM teams ORDER BY position ASC")
        return await cur.fetchall()

async def load_teams():
    rows = await load_team_rows()
    return [name for _, name in rows]

async def get_team_name_by_pos(pos: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT name FROM teams WHERE position=?", (pos,))
        row = await cur.fetchone()
        return row[0] if row else None

# ---- rounds DB helpers ----
async def load_rounds():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, name FROM rounds ORDER BY id ASC")
        return await cur.fetchall()  # [(id, name)]

async def add_round(name: str):
    name = name.strip()
    if not name:
        raise ValueError("Назва раунду не може бути порожньою.")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(id), 0) FROM rounds")
        (maxid,) = await cur.fetchone()
        await db.execute("INSERT INTO rounds(id, name) VALUES(?, ?)", (maxid + 1, name))
        await db.commit()

async def rename_round(rnd_id: int, new_name: str):
    new_name = new_name.strip()
    if not new_name:
        raise ValueError("Нова назва не може бути порожньою.")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM rounds WHERE id=?", (rnd_id,))
        (exists,) = await cur.fetchone()
        if not exists:
            raise ValueError("Раунд із таким id не знайдено.")
        await db.execute("UPDATE rounds SET name=? WHERE id=?", (new_name, rnd_id))
        await db.commit()

async def remove_round(rnd_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM rounds WHERE id=?", (rnd_id,))
        (exists,) = await cur.fetchone()
        if not exists:
            raise ValueError("Раунд із таким id не знайдено.")
        # Видаляємо також оцінки цього раунду
        await db.execute("DELETE FROM scores WHERE round=?", (rnd_id,))
        await db.execute("DELETE FROM scores_detailed WHERE round=?", (rnd_id,))
        await db.execute("DELETE FROM rounds WHERE id=?", (rnd_id,))
        await db.commit()

# ===== SCORE OPS =====
async def upsert_score_total(judge_id: int, judge_username: str, team: str, rnd: int, total_score: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO scores(judge_id, judge_username, team, round, score, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(judge_id, team, round)
            DO UPDATE SET score=excluded.score, ts=excluded.ts
        """, (judge_id, judge_username, team, rnd, total_score, datetime.utcnow().isoformat()))
        await db.commit()

async def upsert_score_detailed(judge_id: int, judge_username: str, team: str, rnd: int, criterion: str, raw_score: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO scores_detailed(judge_id, judge_username, team, round, criterion, raw_score, weight, ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(judge_id, team, round, criterion)
            DO UPDATE SET raw_score=excluded.raw_score, ts=excluded.ts
        """, (judge_id, judge_username, team, rnd, criterion, raw_score, None, datetime.utcnow().isoformat()))
        await db.commit()

async def get_scored_teams_for_judge_round(judge_id: int, rnd: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT team FROM scores WHERE judge_id=? AND round=?", (judge_id, rnd))
        rows = await cur.fetchall()
        return {r[0] for r in rows}

async def get_my_status(judge_id: int):
    teams = await load_teams()
    rounds = await load_rounds()
    round_ids = [rid for rid, _ in rounds]
    res = {r: set() for r in round_ids}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT team, round FROM scores WHERE judge_id=?", (judge_id,)) as cur:
            async for team, rnd in cur:
                res.setdefault(rnd, set()).add(team)
    return res, teams, rounds

async def get_leaderboard(desc: bool = False):
    teams = await load_teams()
    totals = {t: 0 for t in teams}
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT team, SUM(score) FROM scores GROUP BY team") as cur:
            async for team, s in cur:
                if team in totals and s is not None:
                    totals[team] = int(s)
    ordered = sorted(totals.items(), key=lambda x: x[1], reverse=desc)
    return ordered

async def export_csv_bytes():
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(["judge_id","judge_username","team","round","score","timestamp"])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT judge_id, judge_username, team, round, score, ts
            FROM scores
            ORDER BY team, round, judge_id
        """) as cur:
            async for row in cur:
                writer.writerow(row)
    return io.BytesIO(output.getvalue().encode('utf-8'))

async def reset_all():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM scores")
        await db.execute("DELETE FROM scores_detailed")
        await db.commit()

# ===== TEAMS HELPERS =====
async def set_teams_any(names):
    clean = [n.strip() for n in names if n.strip()]
    if len(clean) < 1 or len(clean) > 50:
        raise ValueError("Кількість команд має бути від 1 до 50.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM teams")
        for i, name in enumerate(clean, start=1):
            await db.execute("INSERT INTO teams(position, name) VALUES(?, ?)", (i, name))
        await db.commit()

async def add_team(name: str):
    name = name.strip()
    if not name:
        raise ValueError("Назва команди не може бути порожньою.")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(position), 0) FROM teams")
        (maxpos,) = await cur.fetchone()
        await db.execute("INSERT INTO teams(position, name) VALUES(?, ?)", (maxpos + 1, name))
        await db.commit()

async def remove_team(identifier: str):
    """Видалення за номером (position) або за точним ім'ям. Після видалення — переіндексація 1..N."""
    async with aiosqlite.connect(DB_PATH) as db:
        pos_to_remove = None
        try:
            pos_to_remove = int(identifier)
        except ValueError:
            cur = await db.execute("SELECT position FROM teams WHERE name=?", (identifier.strip(),))
            row = await cur.fetchone()
            if row:
                pos_to_remove = row[0]

        if pos_to_remove is None:
            raise ValueError("Команду не знайдено за цим індексом/назвою.")

        cur = await db.execute("SELECT name FROM teams ORDER BY position ASC")
        all_names = [r[0] for r in await cur.fetchall()]
        if pos_to_remove < 1 or pos_to_remove > len(all_names):
            raise ValueError("Неправильний індекс команди.")

        new_names = [nm for i, nm in enumerate(all_names, start=1) if i != pos_to_remove]

        await db.execute("DELETE FROM teams")
        for i, name in enumerate(new_names, start=1):
            await db.execute("INSERT INTO teams(position, name) VALUES(?, ?)", (i, name))
        await db.commit()

# ===== ALIAS HELPERS =====
async def list_alias_words():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, word FROM alias_words ORDER BY id ASC")
        return await cur.fetchall()

async def add_alias_word(word: str):
    w = word.strip()
    if not w:
        raise ValueError("Порожнє слово не допускається.")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO alias_words(word) VALUES(?)", (w,))
        await db.commit()

async def delete_alias_word(word_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM alias_words WHERE id=?", (word_id,))
        await db.execute("DELETE FROM alias_used WHERE word_id=?", (word_id,))
        await db.commit()

async def get_next_alias_word_for_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        # вибираємо ID всіх слів
        cur = await db.execute("SELECT id FROM alias_words")
        all_ids = [r[0] for r in await cur.fetchall()]
        if not all_ids:
            return None  # слів нема
        # які вже використав користувач
        cur = await db.execute("SELECT word_id FROM alias_used WHERE user_id=?", (user_id,))
        used = {r[0] for r in await cur.fetchall()}
        avail = [i for i in all_ids if i not in used]
        if not avail:
            # якщо все вичерпано — очищаємо використані та починаємо по-новій
            await db.execute("DELETE FROM alias_used WHERE user_id=?", (user_id,))
            await db.commit()
            avail = all_ids[:]
        wid = random.choice(avail)
        # позначимо як використане
        await db.execute("INSERT OR IGNORE INTO alias_used(user_id, word_id) VALUES(?, ?)", (user_id, wid))
        cur = await db.execute("SELECT word FROM alias_words WHERE id=?", (wid,))
        row = await cur.fetchone()
        await db.commit()
        if not row:
            return None
        return {"id": wid, "word": row[0]}

# ===== PARSING =====
def parse_teams_text(text: str):
    if "\n" in text and "," not in text:
        parts = [line.strip() for line in text.splitlines() if line.strip()]
    else:
        parts = [p.strip() for p in text.replace("\n", ",").split(",") if p.strip()]
    return parts

def split_once(cmd_text: str) -> str:
    parts = cmd_text.split(" ", 1)
    return parts[1].strip() if len(parts) > 1 else ""

# ===== UI =====
def main_menu_kb(is_admin_flag: bool):
    buttons = [
        [InlineKeyboardButton("Я журі", callback_data="role:judge")],
        [InlineKeyboardButton("Я гравець (Еліас)", callback_data="role:player")],
    ]
    if is_admin_flag:
        buttons.append([InlineKeyboardButton("Адмін-Боженька", callback_data="role:admin")])
    return InlineKeyboardMarkup(buttons)

def alias_next_kb():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("Генерувати наступне", callback_data="alias:next")],
         [InlineKeyboardButton("⬅ Назад", callback_data="role:menu")]]
    )

def rounds_menu_kb(rounds):
    return InlineKeyboardMarkup([[InlineKeyboardButton(name, callback_data=f"round:{rid}")]
                                for rid, name in rounds])

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    user = update.effective_user
    await ensure_user_in_db(user, phone=None)

    await update.message.reply_text(
        "Вітаю! Оберіть вашу роль:",
        reply_markup=main_menu_kb(is_admin(user))
    )

async def ensure_user_in_db(user, phone: str | None):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE user_id=?", (user.id,))
        row = await cur.fetchone()
        if row:
            if phone is not None:
                await db.execute("UPDATE users SET phone=?, username=?, full_name=? WHERE user_id=?",
                                 (phone, user.username, (user.full_name or ""), user.id))
                await db.commit()
            return
        await db.execute(
            "INSERT INTO users(user_id, username, full_name, phone) VALUES(?, ?, ?, ?)",
            (user.id, user.username, (user.full_name or ""), phone)
        )
        await db.commit()

async def request_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("Поділитись номером ☎️", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True
    )
    await update.message.reply_text(
        "Щоб голосувати як журі, поділіться, будь ласка, номером телефону:",
        reply_markup=kb
    )

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.contact:
        return
    contact = update.message.contact
    user = update.effective_user
    # приймаємо як від самого користувача (щоб не чужий контакт)
    if contact.user_id and contact.user_id != user.id:
        await update.message.reply_text(
            "Будь ласка, надішліть свій контакт через кнопку.",
            reply_markup=ReplyKeyboardRemove()
        )
        return
    phone = contact.phone_number
    await ensure_user_in_db(user, phone=phone)
    await update.message.reply_text(
        "Дякую! Доступ журі активовано.",
        reply_markup=ReplyKeyboardRemove()
    )
    # показати меню журі
    await show_judge_menu(update, context)

async def show_judge_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rounds = await load_rounds()
    if update.callback_query:
        q = update.callback_query
        await q.edit_message_text("Оберіть раунд:", reply_markup=rounds_menu_kb(rounds))
    else:
        await update.message.reply_text("Оберіть раунд:", reply_markup=rounds_menu_kb(rounds))

async def show_player_alias(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # одразу генеруємо слово
    user_id = update.effective_user.id
    wid_word = await get_next_alias_word_for_user(user_id)
    if not wid_word:
        txt = "Словник порожній. Зверніться до адміністратора, щоб додати слова."
    else:
        txt = f"🎲 Слово: *{wid_word['word']}*"
    if update.callback_query:
        q = update.callback_query
        await q.edit_message_text(txt, reply_markup=alias_next_kb(), parse_mode="Markdown")
    else:
        await update.message.reply_text(txt, reply_markup=alias_next_kb(), parse_mode="Markdown")

# ---- rounds admin commands ----
async def list_rounds_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може дивитись список раундів.")
        return
    rows = await load_rounds()
    if not rows:
        await update.message.reply_text("Раундів немає.")
        return
    lines = ["📋 Список раундів:"]
    for rid, name in rows:
        suffix = " (з критеріями)" if CRITERIA_BY_ROUND.get(rid) else " (проста оцінка)"
        lines.append(f"- ID {rid}: {name}{suffix}")
    await update.message.reply_text("\n".join(lines))

async def addrnd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може додавати раунди.")
        return
    rest = split_once(update.message.text)
    if not rest:
        await update.message.reply_text("Вкажіть назву: /addrnd Назва раунду")
        return
    try:
        await add_round(rest)
    except ValueError as e:
        await update.message.reply_text(f"Помилка: {e}")
        return
    await update.message.reply_text("Раунд додано ✅\nВикористайте /listrnds щоб перевірити.")

async def renamernd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може перейменовувати раунди.")
        return
    rest = split_once(update.message.text)
    if not rest:
        await update.message.reply_text("Формат: /renamernd <id> <нова назва>")
        return
    parts = rest.split(" ", 1)
    if len(parts) < 2:
        await update.message.reply_text("Формат: /renamernd <id> <нова назва>")
        return
    try:
        rid = int(parts[0])
        new_name = parts[1].strip()
        await rename_round(rid, new_name)
    except ValueError as e:
        await update.message.reply_text(f"Помилка: {e}")
        return
    await update.message.reply_text("Раунд перейменовано ✅")

async def removernd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може видаляти раунди.")
        return
    rest = split_once(update.message.text)
    if not rest:
        await update.message.reply_text("Формат: /removernd <id>")
        return
    try:
        rid = int(rest)
        await remove_round(rid)
    except ValueError as e:
        await update.message.reply_text(f"Помилка: {e}")
        return
    await update.message.reply_text("Раунд видалено ✅")

# ---- teams admin ----
async def setteams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може змінювати назви команд.")
        return

    provided = split_once(update.message.text)
    if provided:
        names = parse_teams_text(provided)
        try:
            await set_teams_any(names)
        except ValueError as e:
            await update.message.reply_text(f"Помилка: {e}\nНадішліть від 1 до 50 назв (через кому або кожна з нового рядка).")
            return
        await update.message.reply_text("Список команд оновлено ✅\nВикористайте /teams щоб перевірити.")
        return

    context.user_data["awaiting_teams_input"] = True
    await update.message.reply_text(
        "Надішліть новий список команд (1..50): або через кому в одному повідомленні, або 1 назва = 1 рядок.\n"
        "Після отримання я збережу їх як поточний список."
    )

async def addteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може додавати команди.")
        return
    rest = split_once(update.message.text)
    if not rest:
        await update.message.reply_text("Вкажіть назву: /addteam Назва команди")
        return
    try:
        await add_team(rest)
    except ValueError as e:
        await update.message.reply_text(f"Помилка: {e}")
        return
    await update.message.reply_text("Команду додано ✅\nВикористайте /teams щоб перевірити.")

async def removeteam_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може видаляти команди.")
        return
    arg = update.message.text.split(" ", 1)
    ident = arg[1].strip() if len(arg) > 1 else ""
    if not ident:
        await update.message.reply_text("Вкажіть № або точну назву: /removeteam 3  або  /removeteam Команда 3")
        return
    try:
        await remove_team(ident)
    except ValueError as e:
        await update.message.reply_text(f"Помилка: {e}")
        return
    await update.message.reply_text("Команду видалено ✅\nВикористайте /teams щоб перевірити.")

async def handle_text_when_awaiting(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_teams_input"):
        return
    if not is_admin(update.effective_user):
        await update.message.reply_text("Тільки адмін може задавати список команд.")
        return

    names = parse_teams_text(update.message.text)
    try:
        await set_teams_any(names)
    except ValueError as e:
        await update.message.reply_text(f"Помилка: {e}\nНадішліть від 1 до 50 назв (через кому або кожна з нового рядка).")
        return

    context.user_data["awaiting_teams_input"] = False
    await update.message.reply_text("Список команд оновлено ✅\nВикористайте /teams щоб перевірити.")

# ---- scoring entry ----
async def score_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await judge_allowed(update.effective_user):
        await request_phone(update, context)
        return
    await show_judge_menu(update, context)

# ===== NAVIGATION + CHECKMARKS + CRITERIA / SIMPLE =====
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = q.from_user.id
    username = q.from_user.username or ""

    # role menu
    if data == "role:menu":
        await q.edit_message_text("Оберіть роль:", reply_markup=main_menu_kb(is_admin(q.from_user)))
        return

    if data == "role:judge":
        # перевіримо номер
        if not await judge_allowed(q.from_user):
            await q.message.reply_text(
                "Щоб голосувати як журі, поділіться, будь ласка, номером телефону:",
                reply_markup=ReplyKeyboardMarkup(
                    [[KeyboardButton("Поділитись номером ☎️", request_contact=True)]],
                    resize_keyboard=True, one_time_keyboard=True
                )
            )
            return
        await show_judge_menu(update, context)
        return

    if data == "role:player":
        await show_player_alias(update, context)
        return

    if data == "role:admin":
        if not is_admin(q.from_user):
            await q.edit_message_text("Доступ заборонено.")
            return
        # показуємо коротку адмін-довідку
        help_lines = [
            "Адмін-режим:",
            "/teams – список команд",
            "/setteams, /addteam, /removeteam",
            "/list_rounds або /listrnds – раунди",
            "/addrnd, /renamernd, /removernd",
            "/export – CSV",
            "/reset – очистити всі оцінки",
            "",
            "Еліас:",
            "/addalias <слово> – додати слово",
            "/listalias – список слів",
            "/delalias <id> – видалити слово",
        ]
        await q.edit_message_text("\n".join(help_lines), reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Назад", callback_data="role:menu")]]
        ))
        return

    # ---- alias next ----
    if data == "alias:next":
        wid_word = await get_next_alias_word_for_user(user_id)
        if not wid_word:
            await q.edit_message_text(
                "Словник порожній. Зверніться до адміністратора, щоб додати слова.",
                reply_markup=alias_next_kb()
            )
            return
        await q.edit_message_text(f"🎲 Слово: *{wid_word['word']}*", parse_mode="Markdown", reply_markup=alias_next_kb())
        return

    # ---- rounds
    if data.startswith("round:"):
        arg = data.split(":", 1)[1]
        if arg == "menu":
            rounds = await load_rounds()
            await q.edit_message_text("Оберіть раунд:", reply_markup=rounds_menu_kb(rounds))
            return

        rnd = int(arg)
        rows = await load_team_rows()  # [(pos, name)]
        scored = await get_scored_teams_for_judge_round(user_id, rnd)  # set of names
        kb = [[InlineKeyboardButton(f"{name}{' ✅' if name in scored else ''}", callback_data=f"team:{rnd}:{pos}")]
              for (pos, name) in rows]
        kb.append([InlineKeyboardButton("⬅ Раунди", callback_data="round:menu")])
        await q.edit_message_text(f"Раунд {rnd}. Оберіть команду:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # ---- team
    if data.startswith("team:"):
        _, rnd_str, pos_str = data.split(":", 2)
        rnd = int(rnd_str)
        pos = int(pos_str)
        team = await get_team_name_by_pos(pos)
        if not team:
            await q.edit_message_text("Команду не знайдено (ймовірно, список змінено).")
            return

        crit_list = CRITERIA_BY_ROUND.get(rnd, [])
        if crit_list:
            # criteria flow
            crit_name = crit_list[0]
            rows, row = [], []
            for s in SCORES:
                row.append(InlineKeyboardButton(str(s), callback_data=f"crit:{rnd}:{pos}:0:{s}"))
                if len(row) == 5:
                    rows.append(row)
                    row = []
            if row: rows.append(row)
            rows.append([
                InlineKeyboardButton("⬅ Команди", callback_data=f"round:{rnd}"),
                InlineKeyboardButton("⬅ Раунди", callback_data="round:menu")
            ])
            await q.edit_message_text(f"Раунд {rnd}. {team}\nКритерій: {crit_name}\nОберіть бал:",
                                      reply_markup=InlineKeyboardMarkup(rows))
        else:
            # simple scoring for this round (no criteria)
            rows, row = [], []
            for s in SCORES:
                row.append(InlineKeyboardButton(str(s), callback_data=f"score_simple:{rnd}:{pos}:{s}"))
                if len(row) == 5:
                    rows.append(row)
                    row = []
            if row: rows.append(row)
            rows.append([
                InlineKeyboardButton("⬅ Команди", callback_data=f"round:{rnd}"),
                InlineKeyboardButton("⬅ Раунди", callback_data="round:menu")
            ])
            await q.edit_message_text(f"Раунд {rnd}. {team}\nОберіть бал:", reply_markup=InlineKeyboardMarkup(rows))
        return

    # ---- Criterion scoring (uses team position)
    if data.startswith("crit:"):
        _, rnd_str, pos_str, idx_str, score_str = data.split(":", 4)
        rnd = int(rnd_str)
        pos = int(pos_str)
        idx = int(idx_str)
        raw_score = int(score_str)
        if raw_score not in SCORES:
            await q.edit_message_text("Недопустимий бал. Дозволено лише 0–5.")
            return

        team = await get_team_name_by_pos(pos)
        if not team:
            await q.edit_message_text("Команду не знайдено (ймовірно, список змінено).")
            return

        crit_list = CRITERIA_BY_ROUND.get(rnd, [])
        if not crit_list or idx < 0 or idx >= len(crit_list):
            await q.edit_message_text("Некоректний критерій.")
            return

        crit_name = crit_list[idx]
        await upsert_score_detailed(
            judge_id=user_id,
            judge_username=username,
            team=team,
            rnd=rnd,
            criterion=crit_name,
            raw_score=raw_score
        )

        next_idx = idx + 1
        if next_idx < len(crit_list):
            next_name = crit_list[next_idx]
            rows, row = [], []
            for s in SCORES:
                row.append(InlineKeyboardButton(str(s), callback_data=f"crit:{rnd}:{pos}:{next_idx}:{s}"))
                if len(row) == 5:
                    rows.append(row)
                    row = []
            if row: rows.append(row)
            rows.append([
                InlineKeyboardButton("⬅ Команди", callback_data=f"round:{rnd}"),
                InlineKeyboardButton("⬅ Раунди", callback_data="round:menu")
            ])
            await q.edit_message_text(
                f"Раунд {rnd}. {team}\nКритерій: {next_name}\nОберіть бал:",
                reply_markup=InlineKeyboardMarkup(rows)
            )
            return
        else:
            # final SUM from DB (criteria rounds)
            total = 0
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("""
                    SELECT SUM(raw_score) FROM scores_detailed
                    WHERE judge_id=? AND team=? AND round=?
                """, (user_id, team, rnd)) as cur2:
                    row = await cur2.fetchone()
                    total = int(row[0] or 0)

            await upsert_score_total(
                judge_id=user_id,
                judge_username=username,
                team=team,
                rnd=rnd,
                total_score=total
            )

            rows = await load_team_rows()
            scored = await get_scored_teams_for_judge_round(user_id, rnd)
            kb = [[InlineKeyboardButton(f"{name}{' ✅' if name in scored else ''}", callback_data=f"team:{rnd}:{p}")]
                  for (p, name) in rows]
            kb.append([InlineKeyboardButton("⬅ Раунди", callback_data="round:menu")])

            await q.edit_message_text(
                f"✅ Зараховано: Раунд {rnd}, {team}\nПідсумок (сума за критерії): {total}.\nОбирайте наступну команду:",
                reply_markup=InlineKeyboardMarkup(kb)
            )
            return

    # ---- Simple scoring handler (no criteria)
    if data.startswith("score_simple:"):
        _, rnd_str, pos_str, score_str = data.split(":", 3)
        rnd = int(rnd_str)
        pos = int(pos_str)
        score = int(score_str)
        if score not in SCORES:
            await q.edit_message_text("Недопустимий бал. Дозволено лише 0–5.")
            return

        team = await get_team_name_by_pos(pos)
        if not team:
            await q.edit_message_text("Команду не знайдено (ймовірно, список змінено).")
            return

        await upsert_score_total(
            judge_id=user_id,
            judge_username=username,
            team=team,
            rnd=rnd,
            total_score=score
        )

        rows = await load_team_rows()
        scored = await get_scored_teams_for_judge_round(user_id, rnd)
        kb = [[InlineKeyboardButton(f"{name}{' ✅' if name in scored else ''}", callback_data=f"team:{rnd}:{p}")]
              for (p, name) in rows]
        kb.append([InlineKeyboardButton("⬅ Раунди", callback_data="round:menu")])

        await q.edit_message_text(
            f"✅ Зараховано: Раунд {rnd}, {team} — {score}.\nОбирайте наступну команду:",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        return

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ordered = await get_leaderboard(desc=False)
    lines = ["Рейтинг (від найменшої суми):"]
    for i, (team, total) in enumerate(ordered, 1):
        lines.append(f"{i}. {team} - {total}")
    await update.message.reply_text("\n".join(lines))

async def leaderboard_desc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ordered = await get_leaderboard(desc=True)
    lines = ["Рейтинг (від найбільшої суми):"]
    for i, (team, total) in enumerate(ordered, 1):
        lines.append(f"{i}. {team} - {total}")
    await update.message.reply_text("\n".join(lines))

async def mystatus(update: Update, context: ContextTypes.DEFAULT_TYPE):
    st, teams, rounds = await get_my_status(update.effective_user.id)
    lines = ["Ваш прогрес:"]
    for rid, rname in rounds:
        done = sorted(list(st.get(rid, set())))
        left = [t for t in teams if t not in st.get(rid, set())]
        lines.append(f"{rname}:")
        lines.append(f"Оцінено: {len(done)}/{len(teams)}")
        if done:
            lines.append(" - " + "; ".join(done))
        if left:
            lines.append("Залишилось: " + "; ".join(left))
        lines.append("")
    await update.message.reply_text("\n".join(lines).strip())

async def export_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може експортувати.")
        return
    b = await export_csv_bytes()
    await update.message.reply_document(InputFile(b, filename="scores.csv"), caption="Експорт оцінок")

async def reset_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може скидати результати.")
        return
    kb = [[
        InlineKeyboardButton("Так, очищаємо", callback_data="reset:yes"),
        InlineKeyboardButton("Ні", callback_data="reset:no")
    ]]
    await update.message.reply_text("Точно очистити всі оцінки?", reply_markup=InlineKeyboardMarkup(kb))

async def reset_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user):
        await q.message.reply_text("⛔ Лише адмін може скидати результати.")
        return
    if q.data == "reset:yes":
        try:
            await reset_all()
        except Exception as e:
            await q.edit_message_text(f"Помилка під час очищення: {e}")
            return
        await q.edit_message_text("✅ Базу очищено (усі бали видалені).")
    else:
        await q.edit_message_text("Скасовано.")

# ==== ALIAS admin commands
async def addalias_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може додавати слова.")
        return
    rest = split_once(update.message.text)
    if not rest:
        await update.message.reply_text("Формат: /addalias <слово>")
        return
    try:
        await add_alias_word(rest)
    except ValueError as e:
        await update.message.reply_text(f"Помилка: {e}")
        return
    await update.message.reply_text("Слово додано ✅")

async def list_alias_words_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може дивитись перелік слів.")
        return
    rows = await list_alias_words()
    if not rows:
        await update.message.reply_text("Перелік слів порожній.")
        return
    lines = ["Слова для еліасу:"]
    for wid, w in rows:
        lines.append(f"{wid}. {w}")
    await update.message.reply_text("\n".join(lines))

async def delalias_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user):
        await update.message.reply_text("Лише адмін може видаляти слова.")
        return
    rest = split_once(update.message.text)
    if not rest or not rest.isdigit():
        await update.message.reply_text("Формат: /delalias <id>")
        return
    await delete_alias_word(int(rest))
    await update.message.reply_text("Слово видалено ✅")

# ===== SYNC MAIN =====
def main():
    # Fresh loop for MainThread (stable on Windows)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Ensure DB exists
    loop.run_until_complete(init_db())

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # General
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("teams", show_teams := (lambda u, c: u.effective_message.reply_text(
        "Поточні команди:\n" + "\n".join(f"{i}. {name}" for i, name in enumerate(loop.run_until_complete(load_teams()), 1))
    )))  )  # (невеликий шорткат не чіпай, нижче є нормальний show_teams виклик у меню)
    # нормальний show_teams теж додамо:
    app.add_handler(CommandHandler("teams", show_teams, block=False))

    app.add_handler(CommandHandler("score", score_entry))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("leaderboard_desc", leaderboard_desc))
    app.add_handler(CommandHandler("mystatus", mystatus))
    app.add_handler(CommandHandler("export", export_csv))
    app.add_handler(CommandHandler("reset", reset_all_cmd))

    # Teams admin
    app.add_handler(CommandHandler("setteams", setteams))
    app.add_handler(CommandHandler("addteam", addteam_cmd))
    app.add_handler(CommandHandler("removeteam", removeteam_cmd))

    # Rounds admin
    app.add_handler(CommandHandler("list_rounds", list_rounds_cmd))
    app.add_handler(CommandHandler("listrnds", list_rounds_cmd))
    app.add_handler(CommandHandler("addrnd", addrnd_cmd))
    app.add_handler(CommandHandler("renamernd", renamernd_cmd))
    app.add_handler(CommandHandler("removernd", removernd_cmd))

    # Alias admin
    app.add_handler(CommandHandler("addalias", addalias_cmd))
    app.add_handler(CommandHandler("listalias", list_alias_words_cmd))
    app.add_handler(CommandHandler("delalias", delalias_cmd))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_handler, pattern=r"^(role|alias|round|team|crit|score_simple):"))
    app.add_handler(CallbackQueryHandler(reset_cb, pattern=r"^reset:(yes|no)$"))

    # Contact (phone share)
    app.add_handler(MessageHandler(filters.CONTACT, contact_handler))
    # Catch plain text after /setteams when awaiting
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_when_awaiting))

    # --- Start (polling or webhook) ---
    use_webhook = os.getenv("USE_WEBHOOK", "0") == "1"
    if use_webhook:
        webhook_url = os.getenv("WEBHOOK_URL")
        webhook_path = os.getenv("WEBHOOK_PATH", "/hook")
        port = int(os.getenv("PORT", "8080"))
        if not webhook_url:
            raise RuntimeError("WEBHOOK_URL is required when USE_WEBHOOK=1")
        print(f"Starting webhook on 0.0.0.0:{port}{webhook_path} -> {webhook_url}")
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path.lstrip("/"),
            webhook_url=webhook_url.rstrip("/") + webhook_path
        )
    else:
        print("Bot started (polling)")
        app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()