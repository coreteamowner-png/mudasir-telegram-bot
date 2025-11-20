# temporary helper to patch bot.py menu_callback
python - <<'PYCODE'
from pathlib import Path
p = Path('bot.py')
s = p.read_text()

old = """def menu_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    q.answer()
    data = q.data
    if data == "menu_clients":
        q.message.reply_text("Fetching clients..."); clients_cmd(update, context)
    elif data == "menu_today":
        q.message.reply_text("Fetching today stats..."); today_cmd(update, context)
    elif data == "menu_history":
        q.message.reply_text("Loading history..."); history_cmd(update, context)
    elif data == "menu_csv":
        q.message.reply_text("Send CSV file (client_external_id,selrng,quantity) as attachment.")
    elif data == "menu_advanced":
        txt = ("*Advanced*\\nUse /ranges <client_id> and /allocate <client_id> <selrng> <qty>")
        q.message.reply_text(brand(txt), parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_help":
        help_cmd(update, context)
    else:
        q.message.reply_text("Unknown menu action.")"""
new = """def menu_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    q.answer()
    data = q.data
    # ensure update.message exists for the called handlers (they expect update.message.reply_text)
    try:
        update.message = q.message
    except Exception:
        pass

    if data == "menu_clients":
        q.message.reply_text("Fetching clients...")
        clients_cmd(update, context)
    elif data == "menu_today":
        q.message.reply_text("Fetching today stats...")
        today_cmd(update, context)
    elif data == "menu_history":
        q.message.reply_text("Loading history...")
        history_cmd(update, context)
    elif data == "menu_csv":
        q.message.reply_text("Send CSV file (client_external_id,selrng,quantity) as attachment.")
    elif data == "menu_advanced":
        txt = (\"*Advanced*\\nUse /ranges <client_id> and /allocate <client_id> <selrng> <qty>\")
        q.message.reply_text(brand(txt), parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_help":
        help_cmd(update, context)
    else:
        q.message.reply_text("Unknown menu action.")"""
if old in s:
    s = s.replace(old, new)
    p.write_text(s)
    print("Patched bot.py menu_callback.")
else:
    print("Could not find exact old menu_callback block; please update bot.py manually.")
PYCODE
