import logging
import sqlite3
import re
import os
from datetime import datetime
from io import BytesIO

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    ContextTypes,
)

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN")

CATEGORIES = [
    "Чай/печенье",
    "Химия",
    "Аренда",
    "Реклама",
    "ЗП рекламщику",
    "Коммуналка",
    "Прочее",
]

DB_FILE = "expenses.db"

# Состояния
CATEGORY, AMOUNT, COMMENT, EDIT_AMOUNT, EDIT_COMMENT = range(5)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# OCR (опционально). Если нет easyocr — фото будет просто просить сумму вручную
try:
    import easyocr
    OCR_READER = easyocr.Reader(["ru", "en"], gpu=False)
    OCR_AVAILABLE = True
except Exception:
    OCR_AVAILABLE = False
    logger.warning("easyocr не установлен. Распознавание чеков отключено.")


# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            category TEXT NOT NULL,
            amount REAL NOT NULL,
            comment TEXT,
            user_id INTEGER
        )"""
    )
    conn.commit()
    conn.close()


def add_expense(date, category, amount, comment, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "INSERT INTO expenses (date, category, amount, comment, user_id) VALUES (?, ?, ?, ?, ?)",
        (date, category, amount, comment, user_id),
    )
    conn.commit()
    conn.close()


def get_expense(expense_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, date, category, amount, comment FROM expenses WHERE id = ?", (expense_id,))
    row = c.fetchone()
    conn.close()
    return row


def update_expense(expense_id, category=None, amount=None, comment=None):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    if category is not None:
        c.execute("UPDATE expenses SET category = ? WHERE id = ?", (category, expense_id))
    if amount is not None:
        c.execute("UPDATE expenses SET amount = ? WHERE id = ?", (amount, expense_id))
    if comment is not None:
        c.execute("UPDATE expenses SET comment = ? WHERE id = ?", (comment, expense_id))
    conn.commit()
    conn.close()


def delete_expense(expense_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    conn.close()


def get_month_expenses(year, month):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    date_prefix = f"{year}-{month:02d}"
    c.execute(
        "SELECT id, date, category, amount, comment FROM expenses WHERE date LIKE ? ORDER BY date DESC",
        (f"{date_prefix}%",),
    )
    rows = c.fetchall()
    conn.close()
    return rows


def get_last_expenses(limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        "SELECT id, date, category, amount, comment FROM expenses ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = c.fetchall()
    conn.close()
    return rows


# ==================== КОМАНДЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Привет! Я бот для учёта расходов на студию 💸\n\n"
        "Команды:\n"
        "/add — добавить расход\n"
        "/list — последние расходы (с кнопками)\n"
        "/stats — статистика за месяц\n"
        "/help — помощь\n\n"
        "Можно просто скинуть фото чека — попробую вытащить сумму."
    )
    await update.message.reply_text(text)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)


# ---------- Добавление ----------
async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in CATEGORIES]
    await update.message.reply_text("Выбери категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CATEGORY


async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("cat_", "")
    context.user_data["category"] = category

    if context.user_data.get("from_photo"):
        amount = context.user_data.get("amount")
        await query.edit_message_text(
            f"Категория: {category}\nСумма с чека: {amount:.2f} ₽\n\nКомментарий (или /skip):"
        )
        return COMMENT

    await query.edit_message_text(f"Категория: {category}\n\nНапиши сумму:")
    return AMOUNT


async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace(",", ".").replace(" ", "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Напиши нормальную сумму (например 1500 или 1499.50)")
        return AMOUNT

    context.user_data["amount"] = amount
    await update.message.reply_text("Комментарий (или /skip):")
    return COMMENT


async def comment_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    if comment.lower() == "/skip":
        comment = ""

    category = context.user_data["category"]
    amount = context.user_data["amount"]
    date = datetime.now().strftime("%Y-%m-%d")
    user_id = update.effective_user.id

    add_expense(date, category, amount, comment, user_id)

    await update.message.reply_text(
        f"✅ Добавлено\n{date} | {category} | {amount:.2f} ₽\n{comment or '—'}"
    )
    context.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменил.")
    return ConversationHandler.END


# ---------- Список с кнопками ----------
async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_last_expenses(12)
    if not rows:
        await update.message.reply_text("Пока расходов нет.")
        return

    text = "Последние расходы:\n\n"
    keyboard = []

    for eid, date, category, amount, comment in rows:
        short_comment = f" | {comment}" if comment else ""
        text += f"#{eid} • {date} | {category} | {amount:.2f} ₽{short_comment}\n"
        keyboard.append([
            InlineKeyboardButton(f"✏️ #{eid}", callback_data=f"edit_{eid}"),
            InlineKeyboardButton(f"🗑 #{eid}", callback_data=f"del_{eid}"),
        ])

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    eid = int(query.data.replace("del_", ""))
    row = get_expense(eid)
    if not row:
        await query.edit_message_text("Запись не найдена.")
        return

    delete_expense(eid)
    await query.edit_message_text(f"🗑 Удалено: #{eid} | {row[2]} | {row[3]:.2f} ₽")


# ---------- Редактирование ----------
async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    eid = int(query.data.replace("edit_", ""))
    row = get_expense(eid)
    if not row:
        await query.edit_message_text("Запись не найдена.")
        return

    context.user_data["edit_id"] = eid
    context.user_data["old"] = row

    keyboard = [
        [InlineKeyboardButton("Изменить сумму", callback_data="ed_amount")],
        [InlineKeyboardButton("Изменить категорию", callback_data="ed_category")],
        [InlineKeyboardButton("Изменить комментарий", callback_data="ed_comment")],
        [InlineKeyboardButton("Отмена", callback_data="ed_cancel")],
    ]
    await query.edit_message_text(
        f"Редактирование #{eid}\n"
        f"{row[1]} | {row[2]} | {row[3]:.2f} ₽\n"
        f"{row[4] or '—'}\n\nЧто меняем?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return "EDIT_MENU"


async def edit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data

    if action == "ed_cancel":
        context.user_data.clear()
        await query.edit_message_text("Отменено.")
        return ConversationHandler.END

    eid = context.user_data["edit_id"]
    row = context.user_data["old"]

    if action == "ed_amount":
        await query.edit_message_text(f"Текущая сумма: {row[3]:.2f} ₽\nНапиши новую сумму:")
        return EDIT_AMOUNT

    if action == "ed_comment":
        await query.edit_message_text(f"Текущий комментарий: {row[4] or '—'}\nНапиши новый (или /skip чтобы очистить):")
        return EDIT_COMMENT

    if action == "ed_category":
        keyboard = [[InlineKeyboardButton(cat, callback_data=f"edcat_{cat}")] for cat in CATEGORIES]
        await query.edit_message_text("Выбери новую категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
        return "EDIT_CATEGORY"

    return "EDIT_MENU"


async def edit_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace(",", ".").replace(" ", "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Нормальную сумму напиши.")
        return EDIT_AMOUNT

    eid = context.user_data["edit_id"]
    update_expense(eid, amount=amount)
    await update.message.reply_text(f"✅ Сумма изменена на {amount:.2f} ₽")
    context.user_data.clear()
    return ConversationHandler.END


async def edit_comment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    comment = update.message.text.strip()
    if comment.lower() == "/skip":
        comment = ""

    eid = context.user_data["edit_id"]
    update_expense(eid, comment=comment)
    await update.message.reply_text("✅ Комментарий обновлён.")
    context.user_data.clear()
    return ConversationHandler.END


async def edit_category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("edcat_", "")
    eid = context.user_data["edit_id"]
    update_expense(eid, category=category)
    await query.edit_message_text(f"✅ Категория изменена на: {category}")
    context.user_data.clear()
    return ConversationHandler.END


# ---------- Фото чека ----------
def extract_amounts(text: str):
    # Ищем числа, похожие на суммы
    numbers = re.findall(r"\d[\d\s]*[.,]\d{2}|\d{3,}", text)
    amounts = []
    for n in numbers:
        cleaned = n.replace(" ", "").replace(",", ".")
        try:
            val = float(cleaned)
            if 10 < val < 500000:  # разумный диапазон
                amounts.append(val)
        except:
            pass
    # уникальные, по убыванию (часто итог самый большой)
    return sorted(set(amounts), reverse=True)[:5]


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not OCR_AVAILABLE:
        await update.message.reply_text(
            "Распознавание чеков не включено.\n"
            "Установи: pip install easyocr\n"
            "И перезапусти бота.\n\n"
            "Пока можешь добавить вручную через /add"
        )
        return

    photo = update.message.photo[-1]
    file = await photo.get_file()
    bio = BytesIO()
    await file.download_to_memory(bio)
    bio.seek(0)

    await update.message.reply_text("Смотрю чек...")

    try:
        result = OCR_READER.readtext(bio.getvalue(), detail=0)
        text = " ".join(result)
        amounts = extract_amounts(text)
    except Exception as e:
        logger.error(e)
        amounts = []

    if not amounts:
        await update.message.reply_text("Не смог вытащить сумму. Добавь вручную через /add")
        return

    context.user_data["from_photo"] = True
    context.user_data["possible_amounts"] = amounts

    keyboard = [[InlineKeyboardButton(f"{a:.2f} ₽", callback_data=f"photoamt_{a}")] for a in amounts]
    keyboard.append([InlineKeyboardButton("Ввести вручную", callback_data="photoamt_manual")])

    await update.message.reply_text(
        "Нашёл возможные суммы. Какая правильная?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def photo_amount_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data.replace("photoamt_", "")

    if data == "manual":
        await query.edit_message_text("Напиши сумму вручную:")
        context.user_data["from_photo"] = False
        return AMOUNT

    amount = float(data)
    context.user_data["amount"] = amount
    context.user_data["from_photo"] = True

    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in CATEGORIES]
    await query.edit_message_text(
        f"Сумма: {amount:.2f} ₽\nВыбери категорию:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return CATEGORY


# ---------- Статистика ----------
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    rows = get_month_expenses(now.year, now.month)

    if not rows:
        await update.message.reply_text("В этом месяце пока ничего не потратили.")
        return

    totals = {cat: 0.0 for cat in CATEGORIES}
    grand_total = 0.0

    for eid, date, category, amount, comment in rows:
        if category in totals:
            totals[category] += amount
        else:
            totals["Прочее"] += amount
        grand_total += amount

    text = f"📊 Статистика за {now.strftime('%B %Y')}\n\n"
    for cat in CATEGORIES:
        if totals[cat] > 0:
            text += f"{cat}: {totals[cat]:.2f} ₽\n"

    text += f"\n**Итого: {grand_total:.2f} ₽**"
    await update.message.reply_text(text, parse_mode="Markdown")


# ==================== ЗАПУСК ====================
def main():
    init_db()

    application = Application.builder().token(BOT_TOKEN).build()

    # Добавление
    add_conv = ConversationHandler(
        entry_points=[
            CommandHandler("add", add_start),
            CallbackQueryHandler(photo_amount_chosen, pattern="^photoamt_"),
        ],
        states={
            CATEGORY: [CallbackQueryHandler(category_chosen, pattern="^cat_")],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    # Редактирование
    edit_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(edit_callback, pattern="^edit_")],
        states={
            "EDIT_MENU": [CallbackQueryHandler(edit_menu, pattern="^ed_")],
            EDIT_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_amount)],
            EDIT_COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_comment)],
            "EDIT_CATEGORY": [CallbackQueryHandler(edit_category_chosen, pattern="^edcat_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_expenses))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CallbackQueryHandler(delete_callback, pattern="^del_"))
    application.add_handler(add_conv)
    application.add_handler(edit_conv)
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    print("Бот запущен...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
