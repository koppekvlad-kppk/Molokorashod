import logging
import sqlite3
import os
import threading
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

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
CATEGORY, AMOUNT, COMMENT = range(3)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT NOT NULL,
        category TEXT NOT NULL,
        amount REAL NOT NULL,
        comment TEXT,
        user_id INTEGER
    )""")
    conn.commit()
    conn.close()

def add_expense(date, category, amount, comment, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO expenses (date, category, amount, comment, user_id) VALUES (?, ?, ?, ?, ?)",
              (date, category, amount, comment, user_id))
    conn.commit()
    conn.close()

def get_last_expenses(limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, date, category, amount, comment FROM expenses ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_month_expenses(year, month):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    date_prefix = f"{year}-{month:02d}"
    c.execute("SELECT id, date, category, amount, comment FROM expenses WHERE date LIKE ? ORDER BY date DESC",
              (f"{date_prefix}%",))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_expense(expense_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE id = ?", (expense_id,))
    conn.commit()
    conn.close()

# ==================== КОМАНДЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"Получена команда /start от {update.effective_user.id}")
    text = (
        "Привет! Я бот для учёта расходов на студию 💸\n\n"
        "Команды:\n"
        "/add — добавить расход\n"
        "/list — последние расходы\n"
        "/stats — статистика за месяц\n"
        "/help — помощь"
    )
    await update.message.reply_text(text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Команда /add")
    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in CATEGORIES]
    await update.message.reply_text("Выбери категорию:", reply_markup=InlineKeyboardMarkup(keyboard))
    return CATEGORY

async def category_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    category = query.data.replace("cat_", "")
    context.user_data["category"] = category
    await query.edit_message_text(f"Категория: {category}\n\nНапиши сумму:")
    return AMOUNT

async def amount_received(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.replace(",", ".").replace(" ", "").strip()
    try:
        amount = float(text)
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Напиши нормальную сумму (например 1500)")
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
    await update.message.reply_text(f"✅ Добавлено\n{date} | {category} | {amount:.2f} ₽\n{comment or '—'}")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отменил.")
    return ConversationHandler.END

async def list_expenses(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Команда /list")
    rows = get_last_expenses(12)
    if not rows:
        await update.message.reply_text("Пока расходов нет.")
        return
    text = "Последние расходы:\n\n"
    keyboard = []
    for eid, date, category, amount, comment in rows:
        short = f" | {comment}" if comment else ""
        text += f"#{eid} • {date} | {category} | {amount:.2f} ₽{short}\n"
        keyboard.append([InlineKeyboardButton(f"🗑 #{eid}", callback_data=f"del_{eid}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None)

async def delete_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    eid = int(query.data.replace("del_", ""))
    delete_expense(eid)
    await query.edit_message_text(f"🗑 Удалено #{eid}")

async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("Команда /stats")
    now = datetime.now()
    rows = get_month_expenses(now.year, now.month)
    if not rows:
        await update.message.reply_text("В этом месяце пока ничего не потратили.")
        return
    totals = {cat: 0.0 for cat in CATEGORIES}
    grand = 0.0
    for eid, date, category, amount, comment in rows:
        if category in totals:
            totals[category] += amount
        else:
            totals["Прочее"] += amount
        grand += amount
    text = f"📊 Статистика за {now.strftime('%B %Y')}\n\n"
    for cat in CATEGORIES:
        if totals[cat] > 0:
            text += f"{cat}: {totals[cat]:.2f} ₽\n"
    text += f"\nИтого: {grand:.2f} ₽"
    await update.message.reply_text(text)

# ==================== ЗАПУСК ====================
def run_bot():
    logger.info("Запуск бота...")
    init_db()
    application = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            CATEGORY: [CallbackQueryHandler(category_chosen, pattern="^cat_")],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_received)],
            COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("list", list_expenses))
    application.add_handler(CommandHandler("stats", stats))
    application.add_handler(CallbackQueryHandler(delete_callback, pattern="^del_"))
    application.add_handler(conv)

    logger.info("Бот готов, начинаю polling")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

class DummyHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

if __name__ == "__main__":
    logger.info("Старт приложения")
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()

    port = int(os.getenv("PORT", 10000))
    logger.info(f"Запуск HTTP сервера на порту {port}")
    server = HTTPServer(("0.0.0.0", port), DummyHandler)
    server.serve_forever()
