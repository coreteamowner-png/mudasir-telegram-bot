#!/usr/bin/env python3
# MuDaSiR VIP Allocator - stable full file
# Requires: BOT_TOKEN, LOGIN_FORM_RAW, UPSTREAM_BASE, LOG_CHAT_ID (optional) as env vars

import os
import re
import logging
import sqlite3
import time
from time import sleep
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackContext, CallbackQueryHandler

# ----------------- config from env -----------------
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

# ---------------- sqlite history ----------------
DBFILE = "mudasir_history.db"
conn = sqlite3.connect(DBFILE, check_same_thread=False)
cur = conn.cursor()
cur.execute("""CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_external_id TEXT,
    range_code TEXT,
    quantity INTEGER,
    status TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
)""")
conn.commit()

def save_history(client_id, selrng, qty, status):
    try:
        cur.execute("INSERT INTO history (client_external_id, range_code, quantity, status) VALUES (?,?,?,?)",
                    (client_id, selrng, qty, str(status)))
        conn.commit()
    except Exception as e:
        log.warning("save_history failed: %s", e)

# ---------------- helper: safe reply ----------------
def send_reply(update: Update, context: CallbackContext, text, **kwargs):
    """
    Safely reply to user. Works for normal messages and callback_query.
    Falls back to LOG_CHAT_ID env var if no message object available.
    """
    try:
        if getattr(update, "message", None) is not None:
            return update.message.reply_text(text, **kwargs)
    except Exception:
        pass
    try:
        if getattr(update, "callback_query", None) is not None and update.callback_query.message is not None:
            return update.callback_query.message.reply_text(text, **kwargs)
    except Exception:
        pass
    try:
        em = getattr(update, "effective_message", None)
        if em is not None:
            return em.reply_text(text, **kwargs)
    except Exception:
        pass
    # fallback to LOG_CHAT_ID
    try:
        chat = os.getenv("LOG_CHAT_ID")
        if chat:
            return context.bot.send_message(chat_id=chat, text=text, **kwargs)
    except Exception:
        pass
    # last resort: try to use effective_chat if available
    try:
        chat = getattr(update, "effective_chat", None)
        if chat:
            return context.bot.send_message(chat_id=chat.id if hasattr(chat, "id") else chat, text=text, **kwargs)
    except Exception:
        pass
    return None

# ---------------- helpers for upstream ----------------
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
    if not txt:
        return False
    return bool(re.search(r'<!doctype|<html|<head', txt, re.I))

# ---------------- parsers ----------------
def extract_clients(html):
    soup = BeautifulSoup(html, "lxml")
    out = []
    for opt in soup.select("select[name=selidd] option"):
        val = (opt.get("value") or "").strip()
        if val:
            out.append({"name": opt.get_text(" ", strip=True), "external_id": val})
    return out

def parse_ranges(html):
    soup = BeautifulSoup(html, "lxml")
    rows = []
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 1:
            continue
        rng_text = tds[0].get_text(" ", strip=True)
        if not rng_text:
            continue
        hidden = tr.find("input", {"name": "selrng"})
        selrng = hidden.get("value") if hidden and hidden.get("value") else ""
        rows.append({"text": rng_text, "selrng": selrng})
    return rows

# ---------------- UI / Brand ----------------
BRAND_HEADER = "💠 *MuDaSiR VIP Allocator*  \n_Powered by MuDaSiR_\n\n"
def brand(text):
    return BRAND_HEADER + text

def main_menu_kb():
    kb = [
        [InlineKeyboardButton("📋 Clients", callback_data="menu_clients"),
         InlineKeyboardButton("📊 Today Stats", callback_data="menu_today")],
        [InlineKeyboardButton("📁 History", callback_data="menu_history"),
         InlineKeyboardButton("📥 Bulk CSV", callback_data="menu_csv")],
        [InlineKeyboardButton("⚙️ Advanced", callback_data="menu_advanced"),
         InlineKeyboardButton("❓ Help", callback_data="menu_help")]
    ]
    return InlineKeyboardMarkup(kb)

# ---------------- handlers ----------------
def start(update: Update, context: CallbackContext):
    txt = ("*Welcome to MuDaSiR VIP Allocator*\n\n"
           "Use the menu below or /clients /today /history /allocate\n\n"
           "— *Love ❤ from MuDaSiR*")
    send_reply(update, context, brand(txt), parse_mode=ParseMode.MARKDOWN, reply_markup=main_menu_kb())

def help_cmd(update: Update, context: CallbackContext):
    txt = ("*Commands*\n"
           "/clients - list clients\n"
           "/ranges <client_id> - show ranges\n"
           "/allocate <client_id> <selrng> <qty> - allocate\n"
           "/today - today stats\n"
           "Upload CSV (client_external_id,selrng,quantity) for bulk run\n")
    send_reply(update, context, brand(txt), parse_mode=ParseMode.MARKDOWN)

def clients_cmd(update: Update, context: CallbackContext):
    s = get_session()
    try:
        r = s.get(UPSTREAM_BASE + ALL_PATH, headers=HEADERS, timeout=20)
    except Exception as e:
        send_reply(update, context, "⚠️ Network error: " + str(e))
        return
    if looks_like_html(r.text) and "login" in r.text.lower():
        send_reply(update, context, "⚠️ Upstream returned HTML (login required). Check LOGIN_FORM_RAW.")
        return
    try:
        cl = extract_clients(r.text)
        if not cl:
            send_reply(update, context, "No clients found.")
            return
        msg = "*Clients*\n\n"
        for c in cl:
            msg += f"• *{c['name']}* — `{c['external_id']}`\n"
        send_reply(update, context, brand(msg), parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        send_reply(update, context, "Parse error: " + str(e))

def ranges_cmd(update: Update, context: CallbackContext):
    args = context.args
    if not args:
        send_reply(update, context, "Usage: /ranges <client_external_id>")
        return
    cid = args[0]
    s = get_session()
    try:
        r = s.post(UPSTREAM_BASE + ALL_PATH, data={"selidd": cid, "selected2":"1"}, headers=HEADERS, timeout=20)
    except Exception as e:
        send_reply(update, context, "Network error: " + str(e))
        return
    if looks_like_html(r.text) and "login" in r.text.lower():
        send_reply(update, context, "⚠️ Upstream returned HTML (login required). Check LOGIN_FORM_RAW.")
        return
    rows = parse_ranges(r.text)
    if not rows:
        send_reply(update, context, "No ranges found.")
        return
    msg = f"*Ranges for {cid}*\n\n"
    for rrr in rows:
        sel = rrr['selrng'] or "(no selrng)"
        msg += f"• *{rrr['text']}* — `{sel}`\n"
    send_reply(update, context, brand(msg), parse_mode=ParseMode.MARKDOWN)

def allocate_cmd(update: Update, context: CallbackContext):
    args = context.args
    if len(args) < 3:
        send_reply(update, context, "Usage: /allocate <client_id> <selrng> <qty>")
        return
    selidd, selrng, qtys = args[0], args[1], args[2]
    try:
        qty = int(qtys)
    except:
        send_reply(update, context, "Quantity must be integer")
        return
    s = get_session()
    try:
        r = s.post(UPSTREAM_BASE + ALL_PATH, data={"quantity":str(qty),"selidd":selidd,"selrng":selrng,"allocate":"1"}, headers=HEADERS, timeout=30)
    except Exception as e:
        send_reply(update, context, "Network error: "+str(e))
        return
    if looks_like_html(r.text):
        send_reply(update, context, "⚠️ Upstream returned HTML (login required or error). Check LOGIN_FORM_RAW.")
        save_history(selidd, selrng, qty, "failed_html")
        return
    status = "success" if r.status_code==200 else f"failed_http_{r.status_code}"
    save_history(selidd, selrng, qty, status)
    if status=="success":
        txt = (f"✅ *Numbers Allocated Successful*\n\nClient: `{selidd}`\nRange: `{selrng}`\nQty: *{qty}*\n\n— *Love ❤ from MuDaSiR*")
    else:
        txt = f"❌ Failed: HTTP {r.status_code}"
    send_reply(update, context, brand(txt), parse_mode=ParseMode.MARKDOWN)

def today_cmd(update: Update, context: CallbackContext):
    s = get_session()
    try:
        r = s.get(UPSTREAM_BASE + TODAY_PATH, headers=HEADERS, timeout=20)
    except Exception as e:
        send_reply(update, context, "Network error: " + str(e)); return
    if looks_like_html(r.text):
        send_reply(update, context, "⚠️ Upstream returned HTML (login required). Check LOGIN_FORM_RAW."); return
    soup = BeautifulSoup(r.text, "lxml")
    tbl = soup.find("table")
    if not tbl:
        send_reply(update, context, "No stats table found."); return
    counts = {}
    for tr in tbl.select("tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        client = tds[0].get_text(" ", strip=True)
        try:
            tbp = int(re.sub(r"[^\d]", "", tds[1].get_text(" ", strip=True) or "0") or 0)
            ntb = int(re.sub(r"[^\d]", "", tds[2].get_text(" ", strip=True) or "0") or 0)
        except:
            tbp = ntb = 0
        counts[client] = (tbp, ntb)
    msg = "*Today stats*\n\n"
    for k,v in counts.items():
        msg += f"• *{k}* — TBP: `{v[0]}`  NTB: `{v[1]}`\n"
    send_reply(update, context, brand(msg), parse_mode=ParseMode.MARKDOWN)

def history_cmd(update: Update, context: CallbackContext):
    rows = cur.execute("SELECT client_external_id,range_code,quantity,status,created_at FROM history ORDER BY id DESC LIMIT 50").fetchall()
    if not rows:
        send_reply(update, context, "No history yet.")
        return
    msg = "*Allocation History (last 50)*\n\n"
    for r in rows:
        msg += f"{r[4]} — `{r[0]}` — {r[1]} — {r[2]} — {r[3]}\n"
    send_reply(update, context, brand(msg), parse_mode=ParseMode.MARKDOWN)

def handle_document(update: Update, context: CallbackContext):
    doc = update.message.document if getattr(update, "message", None) else None
    if not doc:
        send_reply(update, context, "Send a CSV file (client_external_id,selrng,quantity)."); return
    if not doc.file_name.lower().endswith(".csv"):
        send_reply(update, context, "Please upload a .csv file."); return
    f = doc.get_file()
    fname = "/tmp/" + doc.file_name
    f.download(custom_path=fname)
    send_reply(update, context, "CSV downloaded, processing...")
    processed = success = failed = 0
    with open(fname, "r", encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = [p.strip() for p in ln.split(",")]
            if len(parts) < 3:
                failed += 1; continue
            selidd, selrng, qtys = parts[0], parts[1], parts[2]
            try:
                q = int(qtys)
            except:
                failed += 1; continue
            processed += 1
            s = get_session()
            try:
                r = s.post(UPSTREAM_BASE + ALL_PATH, data={"quantity":str(q),"selidd":selidd,"selrng":selrng,"allocate":"1"}, headers=HEADERS, timeout=30)
                if looks_like_html(r.text):
                    failed += 1
                else:
                    if r.status_code==200:
                        success += 1
                        save_history(selidd, selrng, q, "success")
                    else:
                        failed += 1
            except Exception:
                failed += 1
            sleep(0.25)
    send_reply(update, context, f"CSV processed: total={processed}, success={success}, failed={failed}")

def menu_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    if not q:
        return
    q.answer()
    # ensure update.message exists for called handlers
    try:
        update.message = q.message
    except Exception:
        pass

    data = q.data
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
        txt = ("*Advanced*\nUse /ranges <client_id> and /allocate <client_id> <selrng> <qty>")
        q.message.reply_text(brand(txt), parse_mode=ParseMode.MARKDOWN)
    elif data == "menu_help":
        help_cmd(update, context)
    else:
        q.message.reply_text("Unknown menu action.")

def unknown(update: Update, context: CallbackContext):
    send_reply(update, context, "Unknown command. Use /help or the menu.")

def main():
    log.info("Starting MuDaSiR Bot...")
    if not BOT_TOKEN:
        log.error("BOT_TOKEN not set. Exiting.")
        return
    try:
        updater = Updater(BOT_TOKEN, use_context=True)
    except Exception as e:
        log.exception("Failed to create Updater: %s", e)
        return

    dp = updater.dispatcher
    dp.add_handler(CommandHandler("start", start))
    dp.add_handler(CommandHandler("help", help_cmd))
    dp.add_handler(CommandHandler("clients", clients_cmd))
    dp.add_handler(CommandHandler("ranges", ranges_cmd))
    dp.add_handler(CommandHandler("allocate", allocate_cmd))
    dp.add_handler(CommandHandler("today", today_cmd))
    dp.add_handler(CommandHandler("history", history_cmd))
    dp.add_handler(MessageHandler(Filters.document, handle_document))
    dp.add_handler(CallbackQueryHandler(menu_callback))
    dp.add_handler(MessageHandler(Filters.command, unknown))

    log.info("MuDaSiR Bot started (polling)")
    try:
        updater.start_polling()
        updater.idle()
    except Exception as e:
        log.exception("Polling stopped: %s", e)

if __name__ == "__main__":
    main()
