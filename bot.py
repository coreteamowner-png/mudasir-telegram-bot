#!/usr/bin/env python3
# MuDaSiR VIP Allocator — multi-select UI + multi-allocate
# Copy-paste exactly. Requires env: BOT_TOKEN, LOGIN_FORM_RAW, UPSTREAM_BASE (optional), LOG_CHAT_ID (optional)

import os, re, time, logging, sqlite3
from urllib.parse import unquote
import requests
from bs4 import BeautifulSoup

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
)
from telegram.ext import (
    Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler
)

# ---------------- config ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
LOGIN_FORM_RAW = os.getenv("LOGIN_FORM_RAW", "")
UPSTREAM_BASE = os.getenv("UPSTREAM_BASE", "http://mysmsportal.com")
LOGIN_PATH = "/index.php?login=1"
ALL_PATH = "/index.php?opt=shw_all_v2"
TODAY_PATH = "/index.php?opt=shw_sts_today"

HEADERS = {
    "User-Agent": "MuDaSiRBot/1.0",
    "Content-Type": "application/x-www-form-urlencoded",
    "Referer": UPSTREAM_BASE,
}

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mudasir")

# ---------------- state (in-memory per chat) ----------------
# structure: selections[chat_id] = {"clients": set(), "ranges": set(), "pending_custom_qty": None}
selections = {}

# ---------------- sqlite history ----------------
DBFILE = "mudasir_history.db"
conn = sqlite3.connect(DBFILE, check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT,
    client_external_id TEXT,
    range_code TEXT,
    quantity INTEGER,
    status TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.commit()

def save_history(chat_id, client_id, selrng, qty, status):
    try:
        cur.execute("INSERT INTO history (chat_id, client_external_id, range_code, quantity, status) VALUES (?,?,?,?,?)",
                    (str(chat_id), client_id, selrng, qty, status))
        conn.commit()
    except Exception as e:
        log.warning("save_history failed: %s", e)

# ---------------- helpers ----------------
def parse_form(raw):
    parts = [p for p in raw.split("&") if "=" in p]
    return {k: unquote(v) for k, v in (p.split("=", 1) for p in parts)}

def get_session():
    s = requests.Session()
    if LOGIN_FORM_RAW.strip():
        try:
            data = parse_form(LOGIN_FORM_RAW)
            s.post(UPSTREAM_BASE + LOGIN_PATH, data=data, headers=HEADERS, timeout=15, allow_redirects=True)
            log.info("Attempted upstream login (LOGIN_FORM_RAW provided).")
        except Exception as e:
            log.warning("Upstream login attempt failed: %s", e)
    return s

def looks_like_html(txt):
    if not txt: return False
    return bool(re.search(r'<!doctype|<html|<head', txt, re.I))

def brand(text):
    hdr = "💠 *MuDaSiR VIP Allocator*  \n_Powered by MuDaSiR_\n\n"
    return hdr + text

def safe_text(s): return re.sub(r'[`*_]', '', str(s))[:2000]

# ---------------- parsers ----------------
def extract_clients(html):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for opt in soup.select("select[name=selidd] option"):
        val = (opt.get("value") or "").strip()
        if val:
            out.append({"name": opt.get_text(" ", strip=True), "external_id": val})
    # dedupe
    seen = set(); uniq=[]
    for c in out:
        if c["external_id"] not in seen:
            seen.add(c["external_id"]); uniq.append(c)
    return uniq

def parse_ranges(html):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if not tds: continue
        rng_text = tds[0].get_text(" ", strip=True)
        hidden = tr.find("input", {"name":"selrng"})
        selrng = hidden.get("value") if hidden and hidden.get("value") else ""
        if rng_text:
            rows.append({"text": rng_text, "selrng": selrng})
    return rows

# ---------------- UI keyboards ----------------
def main_menu_kb():
    kb = [
        [InlineKeyboardButton("📋 Clients", callback_data="menu_clients"),
         InlineKeyboardButton("📊 Today Stats", callback_data="menu_today")],
        [InlineKeyboardButton("📁 History", callback_data="menu_history"),
         InlineKeyboardButton("📥 Bulk CSV", callback_data="menu_csv")],
        [InlineKeyboardButton("⚙️ Advanced", callback_data="menu_advanced"),
         InlineKeyboardButton("❓ Help", callback_data="menu_help")],
    ]
    return InlineKeyboardMarkup(kb)

def clients_kb(clients, chat_id):
    # show clients in two-column grid, selected ones prefixed with ✅
    kb=[]
    sel = selections.get(chat_id, {}).get("clients", set())
    row=[]
    for c in clients:
        label = ("✅ " if c['external_id'] in sel else "") + (c['name'] if len(c['name'])<25 else c['name'][:22]+"...")
        row.append(InlineKeyboardButton(label, callback_data=f"toggle_client|{c['external_id']}"))
        if len(row)==2:
            kb.append(row); row=[]
    if row: kb.append(row)
    # actions
    kb.append([InlineKeyboardButton("🔄 Load Ranges", callback_data="load_ranges"),
               InlineKeyboardButton("🧾 Select All", callback_data="select_all_clients")])
    kb.append([InlineKeyboardButton("🚀 Allocate (multi)", callback_data="goto_allocate"),
               InlineKeyboardButton("🗑️ Clear", callback_data="clear_selection")])
    kb.append([InlineKeyboardButton("↩️ Back", callback_data="menu_back")])
    return InlineKeyboardMarkup(kb)

def ranges_kb(ranges, chat_id):
    kb=[]
    sel = selections.get(chat_id, {}).get("ranges", set())
    row=[]
    for r in ranges:
        lbl = ("✅ " if r['selrng'] in sel else "") + (r['text'] if len(r['text'])<25 else r['text'][:22]+"...")
        row.append(InlineKeyboardButton(lbl, callback_data=f"toggle_range|{r['selrng']}"))
        if len(row)==2:
            kb.append(row); row=[]
    if row: kb.append(row)
    kb.append([InlineKeyboardButton("⬅️ Back Clients", callback_data="back_clients"),
               InlineKeyboardButton("🧮 Allocate Now", callback_data="alloc_qty_choice")])
    kb.append([InlineKeyboardButton("↩️ Main Menu", callback_data="menu_back")])
    return InlineKeyboardMarkup(kb)

def qty_kb():
    kb = [
        [InlineKeyboardButton("1", callback_data="qty|1"),
         InlineKeyboardButton("5", callback_data="qty|5"),
         InlineKeyboardButton("10", callback_data="qty|10")],
        [InlineKeyboardButton("25", callback_data="qty|25"),
         InlineKeyboardButton("Custom", callback_data="qty|custom")],
        [InlineKeyboardButton("↩️ Cancel", callback_data="menu_back")]
    ]
    return InlineKeyboardMarkup(kb)

# ---------------- handlers ----------------
def start(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id if update.effective_chat else None
    # initialize selection
    selections[chat_id] = {"clients": set(), "ranges": set(), "pending_custom_qty": None}
    txt = ("*Welcome to MuDaSiR VIP Allocator*\n\nUse the menu below to operate.\n\n— *Love ❤ from MuDaSiR*")
    target = update.message if update.message else update.callback_query.message
    target.reply_text(brand(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())

def help_cmd(update: Update, context: CallbackContext):
    txt = ("*Commands*\n"
           "/clients - open clients selector\n"
           "/today - today stats\n"
           "/history - allocation history\n\nUse the menu for multi-select allocate.")
    send_simple_reply(update, context, brand(txt), parse_mode=ParseMode.MARKDOWN)

def send_simple_reply(update, context, text, **kwargs):
    # prefer callback_query.message (edit) if present, else simple reply
    if getattr(update, "callback_query", None) and update.callback_query.message:
        try:
            return update.callback_query.message.reply_text(text, **kwargs)
        except Exception:
            pass
    if getattr(update, "message", None):
        return update.message.reply_text(text, **kwargs)
    # fallback to LOG_CHAT_ID
    log_chat = os.getenv("LOG_CHAT_ID")
    if log_chat:
        return context.bot.send_message(chat_id=log_chat, text=text, **kwargs)
    return None

def menu_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    if not q: return
    q.answer()
    data = q.data or ""
    chat_id = q.message.chat_id
    if data == "menu_clients":
        # fetch clients and show selector
        s = get_session()
        try:
            r = s.get(UPSTREAM_BASE + ALL_PATH, headers=HEADERS, timeout=20)
        except Exception as e:
            q.message.reply_text("Network error: "+str(e)); return
        if looks_like_html(r.text) and "login" in r.text.lower():
            q.message.reply_text("⚠️ Upstream returned HTML (login required). Check LOGIN_FORM_RAW."); return
        clients = extract_clients(r.text)
        if not clients:
            q.message.reply_text("No clients found."); return
        # init selection store
        selections[chat_id] = {"clients": set(), "ranges": set(), "pending_custom_qty": None}
        q.message.edit_text(brand("*Select Clients*"), parse_mode=ParseMode.MARKDOWN, reply_markup=clients_kb(clients, chat_id))
        return

    # toggle select client
    if data.startswith("toggle_client|"):
        _, cid = data.split("|",1)
        sel = selections.setdefault(chat_id, {"clients": set(), "ranges": set(), "pending_custom_qty": None})
        if cid in sel["clients"]:
            sel["clients"].remove(cid)
        else:
            sel["clients"].add(cid)
        # refresh client list (re-fetch names to rebuild labels)
        s = get_session()
        try:
            r = s.get(UPSTREAM_BASE + ALL_PATH, headers=HEADERS, timeout=20)
        except Exception as e:
            q.message.reply_text("Network error: "+str(e)); return
        clients = extract_clients(r.text)
        q.message.edit_text(brand("*Select Clients*"), parse_mode=ParseMode.MARKDOWN, reply_markup=clients_kb(clients, chat_id))
        return

    if data == "select_all_clients":
        s = get_session()
        r = s.get(UPSTREAM_BASE + ALL_PATH, headers=HEADERS, timeout=20)
        clients = extract_clients(r.text)
        sel = selections.setdefault(chat_id, {"clients": set(), "ranges": set(), "pending_custom_qty": None})
        for c in clients: sel["clients"].add(c["external_id"])
        q.message.edit_text(brand("*Select Clients* (all selected)"), parse_mode=ParseMode.MARKDOWN, reply_markup=clients_kb(clients, chat_id))
        return

    if data == "clear_selection":
        selections[chat_id] = {"clients": set(), "ranges": set(), "pending_custom_qty": None}
        q.message.edit_text(brand("*Selections cleared*"), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())
        return

    if data == "load_ranges":
        sel = selections.get(chat_id)
        if not sel or not sel.get("clients"):
            q.message.answer("⚠️ No clients selected. Select clients first."); return
        # we'll load ranges for FIRST selected client and display ranges to choose
        first = next(iter(sel["clients"]))
        s = get_session()
        try:
            r = s.post(UPSTREAM_BASE + ALL_PATH, data={"selidd": first, "selected2":"1"}, headers=HEADERS, timeout=20)
        except Exception as e:
            q.message.reply_text("Network error: "+str(e)); return
        if looks_like_html(r.text) and "login" in r.text.lower():
            q.message.reply_text("⚠️ Upstream returned HTML (login required). Check LOGIN_FORM_RAW."); return
        ranges = parse_ranges(r.text)
        if not ranges:
            q.message.reply_text("No ranges found for client "+first); return
        # store available ranges temporarily
        selections[chat_id].setdefault("available_ranges", ranges)
        q.message.edit_text(brand(f"*Select Ranges* (showing for client {first})"), parse_mode=ParseMode.MARKDOWN, reply_markup=ranges_kb(ranges, chat_id))
        return

    if data.startswith("toggle_range|"):
        _, selrng = data.split("|",1)
        sel = selections.setdefault(chat_id, {"clients": set(), "ranges": set(), "pending_custom_qty": None})
        if selrng in sel["ranges"]:
            sel["ranges"].remove(selrng)
        else:
            sel["ranges"].add(selrng)
        ranges = sel.get("available_ranges", [])
        q.message.edit_text(brand("*Select Ranges*"), parse_mode=ParseMode.MARKDOWN, reply_markup=ranges_kb(ranges, chat_id))
        return

    if data == "back_clients":
        # reconstruct clients list
        s = get_session()
        r = s.get(UPSTREAM_BASE + ALL_PATH, headers=HEADERS, timeout=20)
        clients = extract_clients(r.text)
        q.message.edit_text(brand("*Select Clients*"), parse_mode=ParseMode.MARKDOWN, reply_markup=clients_kb(clients, chat_id))
        return

    if data == "goto_allocate" or data == "alloc_qty_choice":
        sel = selections.get(chat_id)
        if not sel or not sel.get("clients"):
            q.message.answer("⚠️ No clients selected. Select clients first."); return
        if not sel.get("ranges"):
            q.message.answer("⚠️ No ranges selected. Load ranges and select at least one."); return
        q.message.edit_text(brand("*Choose quantity to allocate*"), parse_mode=ParseMode.MARKDOWN, reply_markup=qty_kb())
        return

    if data.startswith("qty|"):
        _, val = data.split("|",1)
        if val == "custom":
            # set pending flag and ask user to type number
            selections.setdefault(chat_id, {"clients": set(), "ranges": set(), "pending_custom_qty": None})["pending_custom_qty"] = True
            q.message.edit_text(brand("✍️ Please *type* the quantity number now (one message)."), parse_mode=ParseMode.MARKDOWN)
            return
        # numeric quantity chosen -> run allocate
        try:
            qty = int(val)
        except:
            q.message.answer("Invalid quantity."); return
        q.message.edit_text("⏳ Allocating... Please wait.")
        perform_multi_allocate(chat_id, context, q.message, qty)
        return

    if data == "menu_today":
        # today stats
        s = get_session()
        try:
            r = s.get(UPSTREAM_BASE + TODAY_PATH, headers=HEADERS, timeout=20)
        except Exception as e:
            q.message.reply_text("Network error: "+str(e)); return
        if looks_like_html(r.text):
            q.message.reply_text("⚠️ Upstream returned HTML (login required). Check LOGIN_FORM_RAW."); return
        soup = BeautifulSoup(r.text, "lxml")
        tbl = soup.find("table")
        if not tbl:
            q.message.reply_text("No today stats table found."); return
        msg="*Today Stats*\n\n"
        for tr in tbl.select("tr"):
            tds = tr.find_all("td")
            if len(tds)>=3:
                client = tds[0].get_text(" ", strip=True)
                msg += f"• *{client}*\n"
        q.message.edit_text(brand(msg), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "menu_history":
        rows = cur.execute("SELECT client_external_id,range_code,quantity,status,created_at FROM history ORDER BY id DESC LIMIT 30").fetchall()
        if not rows:
            q.message.edit_text(brand("No history yet.")); return
        msg="*Allocation History (last 30)*\n\n"
        for r in rows:
            msg += f"{r[4]} — `{r[0]}` — {r[1]} — {r[2]} — {r[3]}\n"
        q.message.edit_text(brand(msg), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "menu_csv":
        q.message.edit_text(brand("📥 Send a CSV file as document with columns: client_external_id,selrng,quantity"), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "menu_advanced":
        q.message.edit_text(brand("*Advanced*\nUse /ranges <client_id> or /allocate <client> <selrng> <qty>"), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "menu_help":
        q.message.edit_text(brand("*Help*\nUse menu to select clients, load ranges, select ranges, then allocate."), parse_mode=ParseMode.MARKDOWN)
        return

    if data == "menu_back":
        q.message.edit_text(brand("Back to menu"), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())
        return

    # unknown
    q.message.answer("Unknown action.")

# ---------------- allocate logic ----------------
def perform_multi_allocate(chat_id, context, message_obj, qty):
    sel = selections.get(chat_id)
    if not sel:
        message_obj.edit_text("No selections found.")
        return
    clients = list(sel.get("clients", []))
    ranges = list(sel.get("ranges", []))
    if not clients or not ranges:
        message_obj.edit_text("No clients or ranges selected.")
        return

    s = get_session()
    results=[]
    for c in clients:
        for rcode in ranges:
            try:
                res = s.post(UPSTREAM_BASE + ALL_PATH, data={"quantity":str(qty),"selidd":c,"selrng":rcode,"allocate":"1"}, headers=HEADERS, timeout=30)
                if looks_like_html(res.text):
                    status = "failed_html"
                elif res.status_code==200:
                    status = "success"
                else:
                    status = f"failed_http_{res.status_code}"
            except Exception as e:
                status = "error"
            save_history(chat_id, c, rcode, qty, status)
            results.append((c, rcode, status))
            time.sleep(0.2)
    # prepare message and edit original (same message)
    msg = "*Allocation Results*\n\n"
    succ = 0
    for r in results:
        emoji = "✅" if r[2]=="success" else "❌"
        if r[2]=="success": succ += 1
        msg += f"{emoji} `{r[0]}` — `{r[1]}` => {r[2]}\n"
    msg += f"\nTotal operations: {len(results)}  Successful: {succ}\n\n— *Love ❤ from MuDaSiR*"
    try:
        message_obj.edit_text(brand(msg), parse_mode=ParseMode.MARKDOWN)
    except Exception:
        # fallback send
        message_obj.reply_text(brand(msg), parse_mode=ParseMode.MARKDOWN)

# ---------------- message handler for custom qty and documents ----------------
def message_handler(update: Update, context: CallbackContext):
    chat_id = update.effective_chat.id
    text = update.message.text.strip() if update.message and update.message.text else ""
    sel = selections.get(chat_id)
    if sel and sel.get("pending_custom_qty"):
        # try parse one number and run allocate
        try:
            qty = int(re.sub(r"[^\d]", "", text))
            sel["pending_custom_qty"] = None
            # run allocate using the message that triggered this handler
            update.message.reply_text("⏳ Allocating... Please wait.")
            perform_multi_allocate(chat_id, context, update.message, qty)
        except Exception:
            update.message.reply_text("Invalid number. Cancelled.")
        return

def handle_document(update: Update, context: CallbackContext):
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".csv"):
        update.message.reply_text("Please upload a .csv file (client_external_id,selrng,quantity)."); return
    f = doc.get_file()
    fname = "/tmp/" + doc.file_name
    f.download(custom_path=fname)
    update.message.reply_text("CSV downloaded, processing...")
    processed=success=failed=0
    with open(fname, "r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"): continue
            parts = [p.strip() for p in ln.split(",")]
            if len(parts)<3: failed+=1; continue
            selidd, selrng, qtys = parts[0], parts[1], parts[2]
            try:
                q = int(re.sub(r"[^\d]","",qtys))
            except:
                failed+=1; continue
            processed+=1
            s = get_session()
            try:
                r = s.post(UPSTREAM_BASE + ALL_PATH, data={"quantity":str(q),"selidd":selidd,"selrng":selrng,"allocate":"1"}, headers=HEADERS, timeout=30)
                if looks_like_html(r.text):
                    failed+=1; save_history(update.effective_chat.id, selidd, selrng, q, "failed_html")
                elif r.status_code==200:
                    success+=1; save_history(update.effective_chat.id, selidd, selrng, q, "success")
                else:
                    failed+=1; save_history(update.effective_chat.id, selidd, selrng, q, f"failed_http_{r.status_code}")
            except Exception:
                failed+=1
            time.sleep(0.2)
    update.message.reply_text(f"CSV processed: total={processed}, success={success}, failed={failed}")

def unknown(update: Update, context: CallbackContext):
    send_simple_reply(update, context, "Unknown command. Use menu or /help")

# ---------------- start bot ----------------
def main():
    log.info("Starting MuDaSiR Bot...")
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set. Exiting."); return
    updater = Updater(BOT_TOKEN, use_context=True)
    dp = updater.dispatcher

    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CallbackQueryHandler(menu_callback))
    dp.add_handler(CommandHandler("clients", lambda u,c: menu_callback(u,c) if False else start(u,c)))  # keep simple
    dp.add_handler(CommandHandler("today", lambda u,c: menu_callback(u,c) if False else start(u,c)))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, message_handler))
    dp.add_handler(MessageHandler(Filters.document, handle_document))
    dp.add_handler(MessageHandler(Filters.command, unknown))

    updater.start_polling()
    log.info("MuDaSiR Bot started (polling)")
    updater.idle()

if __name__ == "__main__":
    main()
