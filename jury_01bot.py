import os
import asyncio
import csv
import io
from datetime import datetime

# Windows: policy for Python 3.11+ (fix "no current event loop")
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
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
BOT_TOKEN = "8415156689:AAEC_fUy-vhzBvmuv1pxmB7PANNDdpPK104"
ADMIN = "@Vladz97"  # можна вказати @username або numeric id як рядок

# Голосує будь-хто (залишаємо як було)
ALLOWED_JUDGES = []

DB_PATH = "scores.db"
SCORES = list(range(0, 6))  # 0..5

# Пер-раундні критерії (сума), з емоджі для 1 і 2 раунду
CRITERIA_BY_ROUND = {
    1: ["💡 Креативність", "🧩 Концепт", "🎨 Візуальна привабливість"],
    2: ["😂 Гумор", "🎭 Артистизм", "🌟 Оригінальність ідеї"],
}

# --- Seed words for Alias (можеш редагувати) ---
SEED_ALIAS_WORDS = [
    "Мавка","Петруцалек","Шоллер","Інструкція","Патісон","Артикул","Вітрина","Елітка","Палета","Стікер",
    "«Чотири зустрічі»","Рол-кейдж","Заморозка","Глобальний переоблік","Бонет","Доступність","Контрольний лист",
    "Чек","Суламіф","Люля-кебаб","Вантажник","Гарбуз","Виторг","«Мурашник»","Морозиво","Рампа","Штрихкод",
    "Самокаса","Бограч","Протермін","Сировина","Пенетрація","Регал","Поверенння","Багет","Дашборд","Касир",
    "Порей","Втрати","Кошик з кремом","Пароконвектомат","Завгосп","Даркстор","Рація","Тара","Журнал","Затарка",
    "Стейк","Торець","Кур’єр","Накладна","Стелаж","Адміністратор","Віскі","Плов","Корегуючий переоблік","Наполеон",
    "Цінник","Скоропорт","Айпод","Картадор","Задача","Полиця","Бейдж","Клінінг","Касир","Піцестанція","Пенетрація",
    "Викладка","Зарплата","Сировина","Рубікон","Фотозвіт","Куратор","Перевершник","Радість"
]

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

def judge_allowed(user) -> bool:
    # Порожній список -> голосує будь-хто
    if not ALLOWED_JUDGES:
        return True
    uid = str(user.id)
    uname = f"@{user.username}" if user.username else None
    return uid in ALLOWED_JUDGES or (uname and uname in ALLOWED_JUDGES)

# ===== ALIAS MIGRATIONS =====
async def _migrate_alias_schema():
    """Міграції для alias_words та alias_used, якщо вони були створені у старому форматі."""
    async with aiosqlite.connect(DB_PATH) as db:
        # ---- alias_words: мусить мати id INTEGER PRIMARY KEY, word UNIQUE ----
        cur = await db.execute("PRAGMA table_info(alias_words)")
        rows = await cur.fetchall()
        cols = [r[1] for r in rows]  # імена колонок
        if rows and ("id" not in cols or "word" not in cols or len(cols) == 1):
            await db.execute("""
                CREATE TABLE IF NOT EXISTS alias_words_new (
                    id INTEGER PRIMARY KEY,
                    word TEXT UNIQUE NOT NULL
                )
            """)
            if "word" in cols:
                await db.execute("INSERT OR IGNORE INTO alias_words_new(word) SELECT word FROM alias_words")
            await db.execute("DROP TABLE alias_words")
            await db.execute("ALTER TABLE alias_words_new RENAME TO alias_words")
            await db.commit()

        # ---- alias_used: мусить мати (user_id, word_id, ts) з PK(user_id, word_id) ----
        cur = await db.execute("PRAGMA table_info(alias_used)")
        rows = await cur.fetchall()
        ucols = [r[1] for r in rows]
        if rows and "word_id" not in ucols:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS alias_used_new (
                    user_id INTEGER,
                    word_id INTEGER,
                    ts TEXT,
                    PRIMARY KEY (user_id, word_id)
                )
            """)
            if "word" in ucols:
                # перенесемо історію використань з текстового слова на його id
                await db.execute("""
                    INSERT OR IGNORE INTO alias_used_new(user_id, word_id, ts)
                    SELECT au.user_id, aw.id, au.ts
                    FROM alias_used au
                    JOIN alias_words aw ON aw.word = au.word
                """)
            await db.execute("DROP TABLE alias_used")
            await db.execute("ALTER TABLE alias_used_new RENAME TO alias_used")
            await db.commit()

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
        # alias tables (актуальна схема)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alias_words (
                id INTEGER PRIMARY KEY,
                word TEXT UNIQUE NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS alias_used (
                user_id INTEGER,
                word_id INTEGER,
                ts TEXT,
                PRIMARY KEY (user_id, word_id)
            )
        """)
        await db.commit()

    # 🔧 проганяємо міграції, якщо таблиці вже існували у застарілому вигляді
    await _migrate_alias_schema()

    # ---- seed даних
    async with aiosqlite.connect(DB_PATH) as db:
        # teams
        cur = await db.execute("SELECT COUNT(*) FROM teams")
        (cnt,) = await cur.fetchone()
        if cnt == 0:
            defaults = [f"Команда {i}" for i in range(1, 11)]
            for i, name in enumerate(defaults, start=1):
                await db.execute("INSERT INTO teams(position, name) VALUES(?, ?)", (i, name))
            await db.commit()

        # rounds
        cur = await db.execute("SELECT COUNT(*) FROM rounds")
        (rcnt,) = await cur.fetchone()
        if rcnt == 0:
            await db.execute("INSERT INTO rounds(id, name) VALUES(1, ?)", ("Раунд 1",))
            await db.execute("INSERT INTO rounds(id, name) VALUES(2, ?)", ("Раунд 2",))
            await db.execute("INSERT INTO rounds(id, name) VALUES(3, ?)", ("Еліас",))
            await db.commit()

        # alias words
        cur = await db.execute("SELECT COUNT(*) FROM alias_words")
        (wcnt,) = await cur.fetchone()
        if wcnt == 0:
            for w in SEED_ALIAS_WORDS:
                try:
                    await db.execute("INSERT OR IGNORE INTO alias_words(word) VALUES(?)", (w.strip(),))
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

# ===== TEAMS HELPERS (щоб /removeteam працювала гарантовано) =====
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

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    teams = await load_teams()
    rounds = await load_rounds()
    lines = []
    lines.append("Вітаю! Це бот для оцінювання команд.")
    lines.append(f"Команд: {len(teams)}, Раундів: {len(rounds)}, Шкала: 0-5.")
    for rid, rname in rounds:
        crits = CRITERIA_BY_ROUND.get(rid)
        if crits:
            lines.append(f"{rname}: " + ", ".join(crits))
        else:
            lines.append(f"{rname}: (проста оцінка)")
    lines.append("")
    lines.append("Команди:")
    lines.extend([f"{i}. {t}" for i, t in enumerate(teams, 1)])
    lines.append("")
    lines.append("Команди бота:")
    lines.append("/score - поставити оцінку")
    lines.append("/mystatus - що вже оцінили")
    lines.append("/leaderboard - рейтинг (зростання)")
    lines.append("/leaderboard_desc - рейтинг (спадання)")
    lines.append("/teams - показати назви команд")
    if is_admin(update.effective_user):
        lines.append("")
        lines.append("Команди адміністратора:")
        lines.append("/setteams <список> - задати повний список (1..50)")
        lines.append("/addteam <назва> - додати команду")
        lines.append("/removeteam <№|назва> - видалити команду")
        lines.append("/listrnds або /list_rounds - показати раунди")
        lines.append("/addrnd <назва> - додати раунд")
        lines.append("/renamernd <id> <нова назва> - перейменувати раунд")
        lines.append("/removernd <id> - видалити раунд")
        lines.append("/export - CSV")
        lines.append("/reset - очистити всі оцінки")
    await update.message.reply_text("\n".join(lines))

async def show_teams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teams = await load_teams()
    txt = "Поточні команди:\n" + "\n".join(f"{i}. {name}" for i, name in enumerate(teams, 1))
    await update.message.reply_text(txt)

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
    # Підтримка двох форматів:
    # /removeteam 3
    # /removeteam Назва команди
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
    if not judge_allowed(update.effective_user):
        await update.message.reply_text("Вибачте, ви не у списку суддів.")
        return
    rounds = await load_rounds()
    if not rounds:
        await update.message.reply_text("Немає жодного раунду. Зверніться до адміністратора.")
        return
    kb = [[InlineKeyboardButton(name, callback_data=f"round:{rid}")]
          for rid, name in rounds]
    await update.message.reply_text("Оберіть раунд:", reply_markup=InlineKeyboardMarkup(kb))

# ===== NAVIGATION + CHECKMARKS + CRITERIA / SIMPLE =====
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = q.from_user.id
    username = q.from_user.username or ""

    # ---- Menu of rounds or open a specific round ----
    if data.startswith("round:"):
        arg = data.split(":", 1)[1]
        if arg == "menu":
            rounds = await load_rounds()
            kb = [[InlineKeyboardButton(name, callback_data=f"round:{rid}")]
                  for rid, name in rounds]
            await q.edit_message_text("Оберіть раунд:", reply_markup=InlineKeyboardMarkup(kb))
            return

        rnd = int(arg)
        rows = await load_team_rows()  # [(pos, name)]
        scored = await get_scored_teams_for_judge_round(user_id, rnd)  # set of names
        kb = [[InlineKeyboardButton(f"{name}{' ✅' if name in scored else ''}", callback_data=f"team:{rnd}:{pos}")]
              for (pos, name) in rows]
        kb.append([InlineKeyboardButton("⬅ Раунди", callback_data="round:menu")])
        await q.edit_message_text(f"Раунд {rnd}. Оберіть команду:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # ---- Choose team -> criteria flow OR simple scoring, depending on round config ----
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

    # ---- Criterion scoring (uses team position) ----
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
            # final SUM from DB for reliability (criteria rounds)
            total = 0
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("""
                    SELECT SUM(raw_score) FROM scores_detailed
                    WHERE judge_id=? AND team=? AND round=?
                """, (user_id, team, rnd)) as cur:
                    row = await cur.fetchone()
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

    # ---- Simple scoring handler (no criteria) ----
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
        # FIX: було q.reply_text(...) — у v21 треба звертатись через message
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

# ===== Error Handler (щоб бачити причину аварій) =====
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    print("Exception:", context.error)

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
    app.add_handler(CommandHandler("teams", show_teams))
    app.add_handler(CommandHandler("score", score_entry))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(CommandHandler("leaderboard_desc", leaderboard_desc))
    app.add_handler(CommandHandler("mystatus", mystatus))
    app.add_handler(CommandHandler("export", export_csv))
    app.add_handler(CommandHandler("reset", reset_all_cmd))  # FIX: реєстрація /reset

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

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_handler, pattern=r"^(round|team|crit|score_simple):"))
    app.add_handler(CallbackQueryHandler(reset_cb, pattern=r"^reset:(yes|no)$"))

    # Catch plain text after /setteams when awaiting
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_when_awaiting))

    # Error handler
    app.add_error_handler(on_error)

    print("Bot started")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
Після заміни файлу просто запусти:
python jury_01bot.py

Це підніме міграції, збереже твої дані і прибере помилку sqlite3.OperationalError: no such column: id в Еліасі.







Ви сказали:
не запускається файл


Програма ChatGPT сказала:

Обробка тривала 1m 5s
Зрозумів. Швидше за все файл “не стартує” через одне з типових: відсутні залежності, некоректний токен, нема логів у консолі, або ви розгортаєте на хостингу з вебхуком, а код працює тільки в poll-режимі. Я додав у код:

детальне логування;

перевірку BOT_TOKEN з поясненням, якщо його нема/помилковий;

підтримку polling і webhook через змінні середовища (USE_WEBHOOK, WEBHOOK_URL, WEBHOOK_PATH, PORT);

акуратний error-handler.

Скопіюй цей файл повністю та запусти з консолі — помилки тепер будуть зрозуміло видно.

python
Копіювати код
import os
import asyncio
import csv
import io
import logging
from datetime import datetime

# Windows: policy for Python 3.11+ (fix "no current event loop")
if os.name == "nt":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
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

# ===== LOGGING =====
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("jury-bot")

# ===== SETTINGS =====
# Можна задати токен через змінну середовища BOT_TOKEN; якщо не задано — використовується значення нижче.
BOT_TOKEN = os.getenv("BOT_TOKEN") or "8415156689:AAEC_fUy-vhzBvmuv1pxmB7PANNDdpPK104"
ADMIN = "@Vladz97"  # можна вказати @username або numeric id як рядок

# Режим розгортання: POLLING (локально) або WEBHOOK (хостинг)
USE_WEBHOOK = os.getenv("USE_WEBHOOK", "0") == "1"
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()       # напр. https://your-domain.com
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/hook").strip()  # напр. /hook-рандом
PORT = int(os.getenv("PORT", "8000"))

# Голосує будь-хто (залишаємо як було)
ALLOWED_JUDGES = []

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

def judge_allowed(user) -> bool:
    # Порожній список -> голосує будь-хто
    if not ALLOWED_JUDGES:
        return True
    uid = str(user.id)
    uname = f"@{user.username}" if user.username else None
    return uid in ALLOWED_JUDGES or (uname and uname in ALLOWED_JUDGES)

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

# ===== TEAMS HELPERS (щоб /removeteam працювала гарантовано) =====
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

# ===== HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await init_db()
    teams = await load_teams()
    rounds = await load_rounds()
    lines = []
    lines.append("Вітаю! Це бот для оцінювання команд.")
    lines.append(f"Команд: {len(teams)}, Раундів: {len(rounds)}, Шкала: 0-5.")
    for rid, rname in rounds:
        crits = CRITERIA_BY_ROUND.get(rid)
        if crits:
            lines.append(f"{rname}: " + ", ".join(crits))
        else:
            lines.append(f"{rname}: (проста оцінка)")
    lines.append("")
    lines.append("Команди:")
    lines.extend([f"{i}. {t}" for i, t in enumerate(teams, 1)])
    lines.append("")
    lines.append("Команди бота:")
    lines.append("/score - поставити оцінку")
    lines.append("/mystatus - що вже оцінили")
    lines.append("/leaderboard - рейтинг (зростання)")
    lines.append("/leaderboard_desc - рейтинг (спадання)")
    lines.append("/teams - показати назви команд")
    if is_admin(update.effective_user):
        lines.append("")
        lines.append("Команди адміністратора:")
        lines.append("/setteams <список> - задати повний список (1..50)")
        lines.append("/addteam <назва> - додати команду")
        lines.append("/removeteam <№|назва> - видалити команду")
        lines.append("/listrnds або /list_rounds - показати раунди")
        lines.append("/addrnd <назва> - додати раунд")
        lines.append("/renamernd <id> <нова назва> - перейменувати раунд")
        lines.append("/removernd <id> - видалити раунд")
        lines.append("/export - CSV")
        lines.append("/reset - очистити всі оцінки")
    await update.message.reply_text("\n".join(lines))

async def show_teams(update: Update, context: ContextTypes.DEFAULT_TYPE):
    teams = await load_teams()
    txt = "Поточні команди:\n" + "\n".join(f"{i}. {name}" for i, name in enumerate(teams, 1))
    await update.message.reply_text(txt)

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
    # Підтримка двох форматів:
    # /removeteam 3
    # /removeteam Назва команди
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
    if not judge_allowed(update.effective_user):
        await update.message.reply_text("Вибачте, ви не у списку суддів.")
        return
    rounds = await load_rounds()
    if not rounds:
        await update.message.reply_text("Немає жодного раунду. Зверніться до адміністратора.")
        return
    kb = [[InlineKeyboardButton(name, callback_data=f"round:{rid}")]
          for rid, name in rounds]
    await update.message.reply_text("Оберіть раунд:", reply_markup=InlineKeyboardMarkup(kb))

# ===== NAVIGATION + CHECKMARKS + CRITERIA / SIMPLE =====
async def cb_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.callback_query:
        return
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = q.from_user.id
    username = q.from_user.username or ""

    # ---- Menu of rounds or open a specific round ----
    if data.startswith("round:"):
        arg = data.split(":", 1)[1]
        if arg == "menu":
            rounds = await load_rounds()
            kb = [[InlineKeyboardButton(name, callback_data=f"round:{rid}")]
                  for rid, name in rounds]
            await q.edit_message_text("Оберіть раунд:", reply_markup=InlineKeyboardMarkup(kb))
            return

        rnd = int(arg)
        rows = await load_team_rows()  # [(pos, name)]
        scored = await get_scored_teams_for_judge_round(user_id, rnd)  # set of names
        kb = [[InlineKeyboardButton(f"{name}{' ✅' if name in scored else ''}", callback_data=f"team:{rnd}:{pos}")]
              for (pos, name) in rows]
        kb.append([InlineKeyboardButton("⬅ Раунди", callback_data="round:menu")])
        await q.edit_message_text(f"Раунд {rnd}. Оберіть команду:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # ---- Choose team -> criteria flow OR simple scoring, depending on round config ----
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

    # ---- Criterion scoring (uses team position) ----
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
            # final SUM from DB for reliability (criteria rounds)
            total = 0
            async with aiosqlite.connect(DB_PATH) as db:
                async with db.execute("""
                    SELECT SUM(raw_score) FROM scores_detailed
                    WHERE judge_id=? AND team=? AND round=?
                """, (user_id, team, rnd)) as cur:
                    row = await cur.fetchone()
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

    # ---- Simple scoring handler (no criteria) ----
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

# ===== Error Handler =====
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Unhandled exception: %s", context.error)

# ===== RUN APP (polling/webhook) =====
def run_app(app):
    if not BOT_TOKEN or len(BOT_TOKEN.split(":")) != 2:
        logger.error("BOT_TOKEN не задано або виглядає некоректним. Задай змінну середовища BOT_TOKEN або впиши дійсний токен у коді.")
        raise SystemExit(1)

    if USE_WEBHOOK:
        if not WEBHOOK_URL:
            logger.error("USE_WEBHOOK=1, але WEBHOOK_URL не заданий")
            raise SystemExit(1)
        full_webhook_url = WEBHOOK_URL.rstrip("/") + WEBHOOK_PATH
        logger.info("Starting in WEBHOOK mode on port %s, path %s", PORT, WEBHOOK_PATH)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=WEBHOOK_PATH.lstrip("/"),
            webhook_url=full_webhook_url,
        )
    else:
        logger.info("Starting in POLLING mode")
        app.run_polling(close_loop=False)

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
    app.add_handler(CommandHandler("teams", show_teams))
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

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_handler, pattern=r"^(round|team|crit|score_simple):"))
    app.add_handler(CallbackQueryHandler(reset_cb, pattern=r"^reset:(yes|no)$"))

    # Catch plain text after /setteams when awaiting
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_when_awaiting))

    # Error handler
    app.add_error_handler(on_error)

    logger.info("Bot app built. Launching...")
    run_app(app)

if __name__ == "__main__":
    main()