import logging
import sqlite3
import os
from datetime import datetime

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
from fastapi import FastAPI, Request
import uvicorn

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise ValueError("Не задана переменная окружения BOT_TOKEN")

WEBHOOK_URL = os.getenv("RENDER_EXTERNAL_URL", "")  # Render сам подставит
PORT = int(os.getenv("PORT", 10000))

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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== БАЗА ====================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, category TEXT, amount REAL, comment TEXT, user_id INTEGER
    )""")
    conn.commit()
    conn.close()

def add_expense(date, category, amount, comment, user_id):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO expenses (date, category, amount, comment, user_id) VALUES (?,?,?,?,?)",
              (date, category, amount, comment, user_id))
    conn.commit()
    conn.close()

def get_last(limit=10):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, date, category, amount, comment FROM expenses ORDER BY id DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return rows

def get_month(year, month):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, date, category, amount, comment FROM expenses WHERE date LIKE ? ORDER BY date DESC",
              (f"{year}-{month:02d}%",))
    rows = c.fetchall()
    conn.close()
    return rows

def delete_exp(eid):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("DELETE FROM expenses WHERE id=?", (eid,))
    conn.commit()
    conn.close()

# ==================== ХЕНДЛЕРЫ ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info("START from %s", update.effective_user.id)
    await update.message.reply_text(
        "Привет! Бот учёта расходов 💸\n\n"
        "/add — добавить\n"
        "/list — список\n"
        "/stats — статистика"
    )

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = [[InlineKeyboardButton(c, callback_data=f"cat_{c}")] for c in CATEGORIES]
    await update.message.reply_text("Категория:", reply_markup=InlineKeyboardMarkup(kb))
    return CATEGORY

async def cat_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["cat"] = q.data.replace("cat_", "")
    await q.edit_message_text(f"Категория: {context.user_data['cat']}\nСумма:")
    return AMOUNT

async def amount_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(update.message.text.replace(",", ".").replace(" ", ""))
        if amt <= 0:
            raise ValueError
    except:
        await update.message.reply_text("Нормальную сумму")
        return AMOUNT
    context.user_data["amt"] = amt
    await update.message.reply_text("Комментарий или /skip")
    return COMMENT

async def comment_got(update: Update, context: ContextTypes.DEFAULT_TYPE):
    com = update.message.text.strip()
    if com.lower() == "/skip":
        com = ""
    add_expense(datetime.now().strftime("%Y-%m-%d"),
                context.user_data["cat"],
                context.user_data["amt"],
                com,
                update.effective_user.id)
    await update.message.reply_text("✅ Добавлено")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Отмена")
    return ConversationHandler.END

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_last(10)
    if not rows:
        await update.message.reply_text("Пусто")
        return
    text = ""
    kb = []
    for eid, d, c, a, com in rows:
        text += f"#{eid} {d} | {c} | {a:.0f}₽\n"
        kb.append([InlineKeyboardButton(f"🗑{eid}", callback_data=f"del_{eid}")])
    await update.message.reply_text(text or "Пусто", reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def del_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    delete_exp(int(q.data.replace("del_", "")))
    await q.edit_message_text("Удалено")

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    rows = get_month(now.year, now.month)
    if not rows:
        await update.message.reply_text("В этом месяце пусто")
        return
    tot = {c: 0 for c in CATEGORIES}
    s = 0
    for _, _, cat, amt, _ in rows:
        tot[cat] = tot.get(cat, 0) + amt
        s += amt
    txt = "\n".join(f"{c}: {v:.0f}₽" for c, v in tot.items() if v)
    await update.message.reply_text(f"{txt}\n\nИтого: {s:.0f}₽")

# ==================== APP ====================
init_db()
application = Application.builder().token(BOT_TOKEN).build()

conv = ConversationHandler(
    entry_points=[CommandHandler("add", add_start)],
    states={
        CATEGORY: [CallbackQueryHandler(cat_chosen, pattern="^cat_")],
        AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, amount_got)],
        COMMENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, comment_got)],
    },
    fallbacks=[CommandHandler("cancel", cancel)],
)

application.add_handler(CommandHandler("start", start))
application.add_handler(CommandHandler("list", list_cmd))
application.add_handler(CommandHandler("stats", stats_cmd))
application.add_handler(CallbackQueryHandler(del_cb, pattern="^del_"))
application.add_handler(conv)

app = FastAPI()

@app.on_event("startup")
async def on_startup():
    await application.initialize()
    if WEBHOOK_URL:
        await application.bot.set_webhook(url=f"{WEBHOOK_URL}/webhook")
        logger.info("Webhook set to %s/webhook", WEBHOOK_URL)
    await application.start()

@app.on_event("shutdown")
async def on_shutdown():
    await application.stop()
    await application.shutdown()

@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return {"ok": True}

@app.get("/")
async def root():
    return {"status": "ok"}

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
