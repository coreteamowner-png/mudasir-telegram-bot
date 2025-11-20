#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MuDaSiR VIP Allocator Bot
Ready-to-use single-file bot for Telegram + upstream SMS portal automation.
Config via environment variables (see README below).
"""

import os, re, time, csv, io, logging, traceback
from urllib.parse import unquote
import requests
from bs4 import BeautifulSoup
from telegram import Bot, ParseMode, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler
from telegram.ext import CallbackContext
from datetime import datetime

# ---------------- logging ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("mudasir")

# ---------------- config from env ----------------
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
BOT_OWNER      = os.getenv("BOT_OWNER", "MuDaSiR")
LOG_CHAT_ID    = os.getenv("LOG_CHAT_ID", "")  # optional chat id for admin logs
UPSTREAM_BASE  = os.getenv("UPSTREAM_BASE", "http://mysmsportal.com")
LOGIN_FORM_RAW = os.getenv("LOGIN_FORM_RAW", "")  # e.g. user=7944&password=10-16-2025%40Swi
ALL_PATH       = os.getenv("ALL_PATH", "/index.php?opt=shw_all_v2")
TODAY_PATH     = os.getenv("TODAY_PATH", "/index.php?opt=shw_sts_today")
LOGIN_PATH     = os.getenv("LOGIN_PATH", "/index.php?login=1")

# ---------------- helpers ----------------
def parse_form_encoded(raw):
    parts = [p for p in raw.split("&") if "=" in p]
    return {k: unquote(v) for k,v in (p.split("=",1) for p in parts)}

def safe_reply(update: Update, text, **kw):
    """Reply safely whether update has message or callback_query."""
    try:
        if getattr(update, "message", None):
            return update.message.reply_text(text, **kw)
        if getattr(update, "callback_query", None) and update.callback_query.message:
            return update.callback_query.message.reply_text(text, **kw)
    except Exception as e:
        try:
            # send to admin log chat if available
            if LOG_CHAT_ID and bot:
                bot.send_message(chat_id=LOG_CHAT_ID, text=f"safe_reply failed: {e}\n{text}")
        except Exception:
            pass
    return None

# session helper (shared)
def new_session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (Mobile)"})
    return s

def do_login(session):
    """Perform upstream login using LOGIN_FORM_RAW if provided."""
    if not LOGIN_FORM_RAW:
        log.info("LOGIN_FORM_RAW not set — skipping login (may be public).")
        return None
    try:
        data = parse_form_encoded(LOGIN_FORM_RAW)
        hdr = {"Referer": UPSTREAM_BASE + "/index.php?opt=shw_allo"}
        r = session.post(UPSTREAM_BASE + LOGIN_PATH, data=data, headers=hdr, timeout=15)
        r.raise_for_status()
        log.info("Upstream login attempted.")
        return r
    except Exception as e:
        log.warning("Login failed: %s", e)
        return None

# ---------- parsers ----------
def parse_all_ranges_with_stats_and_value(html):
    soup = BeautifulSoup(html, "lxml")
    rows=[]
    def num_from_text(txt):
        s = (txt or "").strip().replace(",", "")
        m = re.search(r"\d+", s)
        return int(m.group(0)) if m else 0
    for tr in soup.select("table tr"):
        tds = tr.find_all("td")
        if len(tds) < 4: continue
        rng_text = tds[0].get_text(" ", strip=True)
        if not rng_text: continue
        up = rng_text.strip().upper()
        if up in ("RANGE","S/N"): continue
        all_num = num_from_text(tds[1].get_text(" ",strip=True))
        free    = num_from_text(tds[2].get_text(" ",strip=True))
        alloc   = num_from_text(tds[3].get_text(" ",strip=True))
        selrng_val = ""
        hidden = tr.find("input", attrs={"name":"selrng"})
        if hidden and hidden.get("value"):
            selrng_val = hidden["value"].strip()
        else:
            frm = tr.find("form")
            if frm:
                inp = frm.find("input", attrs={"name":"selrng"})
                if inp and inp.get("value"):
                    selrng_val = inp["value"].strip()
        rows.append({
            "text": rng_text,
            "all": all_num, "free": free, "allocated": alloc,
            "selrng": selrng_val,
            "allocatable": bool(selrng_val)
        })
    return rows

def extract_clients(html):
    soup = BeautifulSoup(html, "lxml")
    out=[]
    for opt in soup.select("select[name=selidd] option"):
        val = (opt.get("value") or "").strip()
        if val:
            out.append((opt.get_text(" ", strip=True), val))
    seen=set(); uniq=[]
    for nm,sid in out:
        if sid not in seen:
            seen.add(sid); uniq.append((nm,sid))
    return uniq

# today stats parser (simple)
def compute_today_counts(html):
    soup = BeautifulSoup(html, "lxml")
    counts = {}
    for tbl in soup.find_all("table"):
        txt = tbl.get_text(" ", strip=True).upper()
        if "CLIENT" in txt and ("MESSAGES" in txt or "NUMBER" in txt) and "STATUS" in txt:
            # parse rows
            headers = [th.get_text(" ",strip=True).upper() for th in (tbl.find("thead").find_all(["th","td"]) if tbl.find("thead") else tbl.find("tr").find_all(["th","td"]))]
            col_msg = None; col_client=None; col_status=None
            for i,h in enumerate(headers):
                hh=h.upper()
                if "MESSAGE" in hh or "NUMBER" in hh: col_msg=i
                if "CLIENT" in hh: col_client=i
                if "STATUS" in hh: col_status=i
            for tr in tbl.find_all("tr"):
                cells = tr.find_all(["td","th"])
                if not cells or len(cells)<2: continue
                client = ""
                if col_client is not None and col_client < len(cells):
                    client = cells[col_client].get_text(" ", strip=True)
                else:
                    # heuristic
                    for c in cells:
                        t = c.get_text(" ",strip=True)
                        if re.search(r"[A-Za-z]", t) and not re.match(r"^\+?\d+$", t):
                            client = t; break
                if not client: continue
                msg_val=0
                if col_msg is not None and col_msg < len(cells):
                    m = re.search(r"-?\d+", cells[col_msg].get_text(" ",strip=True) or "")
                    msg_val = int(m.group(0)) if m else 0
                status_raw = ""
                if col_status is not None and col_status < len(cells):
                    status_raw = cells[col_status].get_text(" ",strip=True)
                else:
                    status_raw = cells[-1].get_text(" ",strip=True)
                key = "TO BE PAID" if "TO BE PAID" in status_raw.upper() else ("NOT TO BE PAID" if "NOT" in status_raw.upper() and "PAID" in status_raw.upper() else None)
                if not key: continue
                counts.setdefault(client, {"TO BE PAID":0,"NOT TO BE PAID":0})
                counts[client][key] += msg_val
            return counts
    return counts

# ---------- allocation ----------
def allocate_one(session, selidd, selrng, quantity):
    payload = {"quantity": str(quantity), "selidd": str(selidd), "selrng": selrng, "allocate":"1"}
    hdr = {"Referer": UPSTREAM_BASE + ALL_PATH, "Content-Type":"application/x-www-form-urlencoded"}
    r = session.post(UPSTREAM_BASE + ALL_PATH, data=payload, headers=hdr, timeout=25)
    return r

# ---------- history store ----------
HISTORY_FILE = "alloc_history.csv"
def add_history(client_name, client_id, range_text, selrng, qty, ok):
    row = [datetime.utcnow().isoformat(), client_name, client_id, range_text, selrng, str(qty), "OK" if ok else "FAIL"]
    try:
        write_header = not os.path.exists(HISTORY_FILE)
        with open(HISTORY_FILE, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(["ts","client_name","client_id","range_text","selrng","quantity","status"])
            w.writerow(row)
    except Exception as e:
        log.exception("history save failed: %s", e)

def read_history_text(limit=50):
    if not os.path.exists(HISTORY_FILE):
        return "No history yet."
    out=[]
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            rows = list(csv.reader(f))
            for r in rows[-limit:]:
                out.append(" | ".join(r))
    except:
        return "Could not read history."
    return "\n".join(out[::-1])

# ---------- TELEGRAM HANDLERS ----------
def start_cmd(update: Update, context: CallbackContext):
    text = (
        "💎 *MuDaSiR VIP Allocator*\n"
        "_Powered by MuDaSiR_\n\n"
        "Welcome. Use the menu below or commands: /clients /today /history /allocate\n\n"
        "— Love ❤️ from MuDaSiR"
    )
    kb = [
        [InlineKeyboardButton("📋 Clients", callback_data="menu_clients"),
         InlineKeyboardButton("📈 Today Stats", callback_data="menu_today")],
        [InlineKeyboardButton("🕒 History", callback_data="menu_history"),
         InlineKeyboardButton("📤 Bulk CSV", callback_data="menu_csv")],
        [InlineKeyboardButton("⚙ Advanced", callback_data="menu_advanced"),
         InlineKeyboardButton("❓ Help", callback_data="menu_help")]
    ]
    update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)

def menu_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    if not q:
        safe_reply(update, "⚠ Invalid menu action.")
        return
    q.answer()
    data = q.data
    msg = q.message
    try:
        if data == "menu_clients":
            msg.edit_text("📁 Fetching clients…")
            clients_cmd(update, context)
        elif data == "menu_today":
            msg.edit_text("📊 Fetching today stats…")
            today_cmd(update, context)
        elif data == "menu_history":
            msg.edit_text("🕒 Loading history…")
            history_cmd(update, context)
        elif data == "menu_csv":
            msg.edit_text("📤 Send CSV file (client_external_id, selrng, quantity) as attachment.")
        elif data == "menu_advanced":
            txt = (
                "⚙ *Advanced Panel*\n"
                "Use:\n"
                "• /ranges — load ranges\n"
                "• /allocate — allocate numbers\n"
                "• /clients — load clients\n\n"
                "— Powered by MuDaSiR"
            )
            msg.edit_text(txt, parse_mode=ParseMode.MARKDOWN)
        elif data == "menu_help":
            help_txt = (
                "❓ *Help Menu*\n"
                "• Use /clients to load clients\n"
                "• Use /ranges to load ranges\n"
                "• Use /allocate to allocate\n"
                "• Use CSV upload for bulk\n\n"
                "— MuDaSiR"
            )
            msg.edit_text(help_txt, parse_mode=ParseMode.MARKDOWN)
        else:
            msg.edit_text("⚠ Unknown menu action.")
    except Exception as e:
        safe_reply(update, "⚠ Error handling menu.")

def clients_cmd(update: Update, context: CallbackContext):
    safe_reply(update, "Fetching clients from upstream...")
    sess = new_session()
    try:
        do_login(sess)
        r = sess.get(UPSTREAM_BASE + ALL_PATH, timeout=20)
        r.raise_for_status()
        clients = extract_clients(r.text)
        if not clients:
            safe_reply(update, "No clients parsed (login may be required).")
            return
        # build text
        lines=[]
        for name, cid in clients:
            lines.append(f"{name} [{cid}]")
        txt = "*Clients list:*\n" + "\n".join(lines[:200])
        safe_reply(update, txt)
    except Exception as e:
        log.exception("clients_cmd error")
        safe_reply(update, "Error fetching clients: " + str(e))

def today_cmd(update: Update, context: CallbackContext):
    safe_reply(update, "Fetching today stats...")
    sess = new_session()
    try:
        do_login(sess)
        r = sess.get(UPSTREAM_BASE + TODAY_PATH, timeout=20)
        r.raise_for_status()
        counts = compute_today_counts(r.text)
        if not counts:
            safe_reply(update, "No today stats parsed (login required or page changed).")
            return
        lines=["*Today stats per client:*"]
        for c, v in list(counts.items())[:100]:
            lines.append(f"{c} — TO_BE_PAID: {v.get('TO BE PAID',0)}  NOT_TO_BE_PAID: {v.get('NOT TO BE PAID',0)}")
        safe_reply(update, "\n".join(lines))
    except Exception as e:
        log.exception("today_cmd")
        safe_reply(update, "Error fetching today stats: " + str(e))

def history_cmd(update: Update, context: CallbackContext):
    txt = read_history_text(50)
    safe_reply(update, f"*Allocation History (latest)*\n{txt}")

def allocate_cmd(update: Update, context: CallbackContext):
    # expected usage: /allocate <client_id> <selrng> <qty>
    msg = update.message.text or ""
    parts = msg.split()
    if len(parts) >= 4:
        _, client_id, selrng, qty = parts[:4]
        try:
            qty = int(qty)
        except:
            safe_reply(update, "Quantity must be integer.")
            return
        sess = new_session()
        do_login(sess)
        r = allocate_one(sess, client_id, selrng, qty)
        ok = r.status_code == 200
        add_history("manual", client_id, selrng, selrng, qty, ok)
        if ok:
            safe_reply(update, f"✅ Numbers Allocated to client {client_id} range {selrng} qty {qty}")
        else:
            safe_reply(update, f"❌ Allocation failed (HTTP {r.status_code}).")
    else:
        safe_reply(update, "Usage: /allocate <client_id> <selrng> <quantity>")

def csv_file_handler(update: Update, context: CallbackContext):
    # handles file uploads for CSV bulk allocation
    f = update.message.document
    if not f:
        safe_reply(update, "Please send CSV file as document (columns: client_external_id,selrng,quantity).")
        return
    safe_reply(update, "Received CSV — processing...")
    bio = f.get_file().download_as_bytearray()
    txt = bio.decode('utf-8', errors='ignore')
    reader = csv.reader(io.StringIO(txt))
    sess = new_session()
    do_login(sess)
    results=[]
    for i,row in enumerate(reader, start=1):
        if not row or len(row)<3: continue
        client_id = row[0].strip()
        selrng = row[1].strip()
        try:
            qty = int(row[2])
        except:
            results.append(f"Row{i}: bad qty")
            continue
        try:
            r = allocate_one(sess, client_id, selrng, qty)
            ok = r.status_code==200
            add_history("csv", client_id, selrng, selrng, qty, ok)
            results.append(f"Row{i}: {client_id} {selrng} {qty} -> {'OK' if ok else 'FAIL '+str(r.status_code)}")
        except Exception as e:
            results.append(f"Row{i}: exception {e}")
    safe_reply(update, "CSV results:\n" + "\n".join(results[:1000]))

# fallback message
def unknown_handler(update: Update, context: CallbackContext):
    safe_reply(update, "Unknown command. Use /start to open menu.")

# ---------- bot setup ----------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("BOT_TOKEN not set. Set env var BOT_TOKEN and restart.")
        exit(1)

    updater = Updater(BOT_TOKEN, use_context=True)
    dispatcher = updater.dispatcher
    bot = updater.bot

    # handlers
    dispatcher.add_handler(CommandHandler("start", start_cmd))
    dispatcher.add_handler(CommandHandler("clients", clients_cmd))
    dispatcher.add_handler(CommandHandler("today", today_cmd))
    dispatcher.add_handler(CommandHandler("history", history_cmd))
    dispatcher.add_handler(CommandHandler("allocate", allocate_cmd))
    dispatcher.add_handler(MessageHandler(Filters.document.mime_type("text/csv") | Filters.document.mime_type("text/plain") | Filters.document.file_extension("csv"), csv_file_handler))
    dispatcher.add_handler(CallbackQueryHandler(menu_callback))
    dispatcher.add_handler(MessageHandler(Filters.command, unknown_handler))

    # start polling (worker style)
    log.info("Starting MuDaSiR bot - polling mode.")
    updater.start_polling()
    updater.idle()
