#!/usr/bin/env python3
import os
import sys
import subprocess
import threading
import time
import shutil
import zipfile
import tarfile
import sqlite3
import signal
import ast
import importlib
import importlib.util
import html as html_lib
import logging
import json
import re

from flask import Flask, request, jsonify, render_template, render_template
from datetime import datetime, timezone

# -------------------- AUTOMATIC DEPENDENCY INSTALLATION --------------------
def install_requirements():
    requirements = [
        "pyTelegramBotAPI",
        "requests",
        "psutil"
    ]
    for package in requirements:
        try:
            if package == "pyTelegramBotAPI":
                import telebot
            elif package == "psutil":
                import psutil
            elif package == "requests":
                import requests
            print(f"✅ {package} is already installed")
        except ImportError:
            print(f"📦 {package} is being installed...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install", package])
                print(f"✅ {package} installed successfully")
            except Exception as e:
                print(f"❌ {package} failed to install: {e}")

install_requirements()

import psutil
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# -------------------- CONFIGURATION --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8805059380:AAETAMZ7Q8omqJPvJ2yfnsDymEOEJcXqzow")   # Set BOT_TOKEN in Vercel Environment Variables
ORIGINAL_ADMIN_IDS = [int(x.strip()) for x in os.environ.get("ADMIN_IDS", "8313599433").split(",") if x.strip().isdigit()]

CPU_THRESHOLD = float(os.environ.get("CPU_THRESHOLD", "90.0"))
MEMORY_THRESHOLD = float(os.environ.get("MEMORY_THRESHOLD", "90.0"))
MAX_RUNNING_PROCESSES = int(os.environ.get("MAX_RUNNING_PROCESSES", "15"))
MAX_FILES_PER_USER = int(os.environ.get("MAX_FILES_PER_USER", "10"))
SOURCE_CHANNEL = "@hiiiiiiiiiiiiiii77"
SOURCE_MESSAGE_ID = 6
CHANNEL_MESSAGE_ID = 5
CONTACT_MESSAGE_ID = 4
MAX_SECONDARY_ADMINS = 10

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WELCOME_VIDEO_FILE = os.path.join(BASE_DIR, "welcome_video.mp4")
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "metadata.db")
UPLOADS_DIR = os.path.join(DATA_DIR, "uploads")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
TEMP_DIR = os.path.join(DATA_DIR, "temp")

for d in [DATA_DIR, UPLOADS_DIR, LOGS_DIR, TEMP_DIR]:
    os.makedirs(d, exist_ok=True)

START_TIME = datetime.now(timezone.utc)

# -------------------- LOGGING --------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# -------------------- DATABASE --------------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
conn.row_factory = sqlite3.Row
db_lock = threading.Lock()

def init_db():
    with db_lock:
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                filename TEXT,
                orig_name TEXT,
                path TEXT,
                uploaded_at TEXT,
                file_type TEXT,
                pid INTEGER,
                status TEXT DEFAULT 'Stopped',
                env_vars TEXT,
                auto_restart INTEGER DEFAULT 0
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                started_at TEXT,
                finished_at TEXT,
                pid INTEGER,
                log_path TEXT,
                exit_code INTEGER
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                joined_at TEXT,
                last_seen TEXT
            )
        ''')
        cur.execute('''
            CREATE TABLE IF NOT EXISTS secondary_admins (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                nickname TEXT,
                added_at TEXT
            )
        ''')
        # Migrate older installations that do not yet have nickname.
        try:
            cur.execute("ALTER TABLE secondary_admins ADD COLUMN nickname TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
        conn.commit()

init_db()

# -------------------- DATABASE HELPERS --------------------
def add_file_record(user_id, username, filename, orig_name, path, file_type, env_vars=None):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO files (user_id, username, filename, orig_name, path, uploaded_at, file_type, env_vars) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, username, filename, orig_name, path, datetime.now(timezone.utc).isoformat(), file_type, json.dumps(env_vars) if env_vars else None)
        )
        conn.commit()
        return cur.lastrowid

def list_user_files(user_id):
    cur = conn.cursor()
    cur.execute(
        "SELECT id, filename, orig_name, uploaded_at, file_type, status, pid FROM files WHERE user_id=? ORDER BY id DESC",
        (user_id,)
    )
    return cur.fetchall()

def get_file_record(file_id):
    cur = conn.cursor()
    cur.execute("SELECT * FROM files WHERE id=?", (file_id,))
    return cur.fetchone()

def remove_file_record(file_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM files WHERE id=?", (file_id,))
        conn.commit()

def update_file_status(file_id, pid, status):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET pid=?, status=? WHERE id=?",
            (pid, status, file_id)
        )
        conn.commit()

def update_file_env(file_id, env_vars):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET env_vars=? WHERE id=?",
            (json.dumps(env_vars), file_id)
        )
        conn.commit()

def set_auto_restart(file_id, enabled):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE files SET auto_restart=? WHERE id=?",
            (1 if enabled else 0, file_id)
        )
        conn.commit()

def get_all_users():
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, joined_at, last_seen FROM users ORDER BY joined_at DESC")
    return cur.fetchall()

def record_run_start(file_id, pid, log_path):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO runs (file_id, started_at, pid, log_path) VALUES (?, ?, ?, ?)",
            (file_id, datetime.now(timezone.utc).isoformat(), pid, log_path)
        )
        conn.commit()
        return cur.lastrowid

def record_run_finish(run_id, exit_code):
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "UPDATE runs SET finished_at=?, exit_code=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), exit_code, run_id)
        )
        conn.commit()

# -------------------- PROCESS MANAGEMENT --------------------
processes = {}
proc_lock = threading.Lock()
auto_restart_flags = {}

def get_system_load():
    try:
        cpu = psutil.cpu_percent(interval=0.1)
        mem = psutil.virtual_memory().percent
        proc_count = len(processes)
        disk = psutil.disk_usage('/').percent
        return float(cpu), float(mem), proc_count, disk
    except:
        return 0.0, 0.0, 0, 0

def should_stop_due_to_load():
    cpu, mem, proc_count, _ = get_system_load()
    if proc_count >= MAX_RUNNING_PROCESSES:
        return True, f"Too many processes running ({proc_count}/{MAX_RUNNING_PROCESSES})"
    if cpu >= CPU_THRESHOLD:
        return True, f"CPU load is high ({cpu:.1f}%)"
    if mem >= MEMORY_THRESHOLD:
        return True, f"Memory usage is high ({mem:.1f}%)"
    return False, None

# -------------------- FILE UTILITIES --------------------
def get_file_type(filename):
    name = filename.lower()
    if name.endswith(".py"): return "python"
    if name.endswith(".js"): return "javascript"
    if name.endswith(".zip"): return "zip"
    if any(name.endswith(ext) for ext in [".tar", ".tar.gz", ".tgz"]): return "archive"
    if name.endswith(".env"): return "env"
    return "unknown"

def extract_archive(file_path, extract_dir):
    try:
        if file_path.lower().endswith(".zip"):
            with zipfile.ZipFile(file_path, 'r') as z:
                z.extractall(extract_dir)
        elif file_path.lower().endswith((".tar.gz", ".tgz")):
            with tarfile.open(file_path, 'r:gz') as t:
                t.extractall(extract_dir)
        elif file_path.lower().endswith(".tar"):
            with tarfile.open(file_path, 'r') as t:
                t.extractall(extract_dir)
        else:
            return False, "Unsupported archive format"
        return True, None
    except Exception as e:
        return False, str(e)

def find_main_file(directory):
    priority = ["main.py", "bot.py", "app.py", "server.py", "index.py", "script.py",
                "main.js", "bot.js", "app.js", "server.js", "index.js", "script.js"]
    for root, _, files in os.walk(directory):
        for f in priority:
            if f in files:
                return os.path.join(root, f)
    for root, _, files in os.walk(directory):
        for f in files:
            if f.endswith((".py", ".js")):
                return os.path.join(root, f)
    return None

def install_requirements_from_file(req_path, chat_id, file_name):
    try:
        if not os.path.exists(req_path):
            return True, "requirements.txt not found"
        with open(req_path, 'r') as f:
            reqs = [line.strip() for line in f if line.strip() and not line.startswith('#')]
        if not reqs:
            return True, "requirements.txt is empty"
        success = 0
        failed = []
        for pkg in reqs:
            try:
                subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                               capture_output=True, timeout=120, check=True)
                success += 1
            except Exception:
                failed.append(pkg)
        msg = f"{success} packages installed"
        if failed:
            msg += f", {len(failed)} failed: {', '.join(failed[:5])}"
        return len(failed) == 0, msg
    except Exception as e:
        return False, f"Error: {str(e)}"

def extract_imports(file_path):
    imports = set()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name.split('.')[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.level == 0:
                    imports.add(node.module.split('.')[0])
    except:
        pass
    return imports

def install_missing_imports(imports, chat_id, file_name):
    missing = []
    for mod in imports:
        try:
            importlib.import_module(mod)
        except ImportError:
            missing.append(mod)
    if not missing:
        return True, "All imports are available"
    pip_map = {'telebot': 'pyTelegramBotAPI', 'PIL': 'Pillow', 'cv2': 'opencv-python',
               'Crypto': 'pycryptodome', 'bs4': 'beautifulsoup4'}
    success = 0
    failed = []
    for mod in missing:
        pkg = pip_map.get(mod, mod)
        try:
            subprocess.run([sys.executable, "-m", "pip", "install", pkg],
                           capture_output=True, timeout=120, check=True)
            success += 1
        except Exception:
            failed.append(mod)
    msg = f"{success} modules installed"
    if failed:
        msg += f", {len(failed)} failed: {', '.join(failed)}"
    return len(failed) == 0, msg

# -------------------- TELEGRAM BOT --------------------
# ---------- STYLIZED TELEGRAM OUTPUT ----------
import re as _re
_FONT_MAP = str.maketrans({
    "a":"ᴀ","b":"ʙ","c":"ᴄ","d":"ᴅ","e":"ᴇ","f":"ꜰ","g":"ɢ","h":"ʜ","i":"ɪ","j":"ᴊ","k":"ᴋ","l":"ʟ","m":"ᴍ","n":"ɴ","o":"ᴏ","p":"ᴩ","q":"q","r":"ʀ","s":"ꜱ","t":"ᴛ","u":"ᴜ","v":"ᴠ","w":"ᴡ","x":"x","y":"ʏ","z":"ᴢ",
    "A":"ᴀ","B":"ʙ","C":"ᴄ","D":"ᴅ","E":"ᴇ","F":"ꜰ","G":"ɢ","H":"ʜ","I":"ɪ","J":"ᴊ","K":"ᴋ","L":"ʟ","M":"ᴍ","N":"ɴ","O":"ᴏ","P":"ᴩ","Q":"q","R":"ʀ","S":"ꜱ","T":"ᴛ","U":"ᴜ","V":"ᴠ","W":"ᴡ","X":"x","Y":"ʏ","Z":"ᴢ"
})
def sf(text):
    if text is None:
        return text
    # Keep HTML tags, URLs and code/placeholder fragments intact.
    parts = _re.split(r'(<[^>]+>|https?://\S+|@[A-Za-z0-9_]+)', str(text))
    for i in range(0, len(parts), 2):
        parts[i] = parts[i].translate(_FONT_MAP)
    return ''.join(parts)

class StyledTeleBot(telebot.TeleBot):
    def send_message(self, chat_id, text, *args, **kwargs):
        return super().send_message(chat_id, sf(text), *args, **kwargs)
    def send_document(self, chat_id, document, *args, **kwargs):
        if kwargs.get("caption") is not None:
            kwargs["caption"] = sf(kwargs["caption"])
        return super().send_document(chat_id, document, *args, **kwargs)
    def edit_message_text(self, text, *args, **kwargs):
        return super().edit_message_text(sf(text), *args, **kwargs)
    def answer_callback_query(self, callback_query_id, text=None, *args, **kwargs):
        return super().answer_callback_query(callback_query_id, sf(text) if text else text, *args, **kwargs)

bot = StyledTeleBot(BOT_TOKEN, parse_mode="HTML")

# ---------- VERCEL / FLASK WEBHOOK APP ----------
# Vercel runs this Flask app as a serverless function. Telegram sends updates
# to /telegram/webhook instead of using long-polling.
app = Flask(__name__)
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "redlucky-webhook")
LOCAL_HOST = os.environ.get("HOST", "0.0.0.0")
LOCAL_PORT = int(os.environ.get("PORT", "5000"))

@app.get("/")
def home():
    return render_template("index.html"), 200

@app.get("/health")
def health():
    return jsonify({"status": "ok", "telegram": "webhook"}), 200

@app.post("/telegram/webhook")
def telegram_webhook():
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return jsonify({"ok": False, "error": "Unauthorized"}), 401
    try:
        update = request.get_json(silent=True)
        if update:
            bot.process_new_updates([telebot.types.Update.de_json(update)])
        return jsonify({"ok": True}), 200
    except Exception as e:
        logger.exception("Webhook processing error")
        return jsonify({"ok": False, "error": str(e)}), 500

def configure_webhook():
    # Set WEBHOOK_URL explicitly for the most reliable Vercel setup.
    # Otherwise Vercel provides VERCEL_URL automatically.
    base_url = os.environ.get("WEBHOOK_URL") or os.environ.get("VERCEL_URL")
    if not base_url:
        return False
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    webhook_url = base_url.rstrip("/") + "/telegram/webhook"
    try:
        result = bot.set_webhook(url=webhook_url, secret_token=WEBHOOK_SECRET)
        logger.info("Telegram webhook configured: %s", result)
        return bool(result)
    except Exception as e:
        logger.error("Webhook setup failed: %s", e)
        return False

# Configure the webhook on module load when deployed.
if os.environ.get("VERCEL") or os.environ.get("WEBHOOK_URL"):
    configure_webhook()

# ---------- KEYBOARD ----------
def is_original_admin(user_id):
    return user_id in ORIGINAL_ADMIN_IDS

def is_admin(user_id):
    if user_id in ORIGINAL_ADMIN_IDS:
        return True
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM secondary_admins WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None

def add_secondary_admin(user_id, username="", nickname=""):
    now = datetime.now(timezone.utc).isoformat()
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO secondary_admins (user_id, username, nickname, added_at) VALUES (?, ?, ?, COALESCE((SELECT added_at FROM secondary_admins WHERE user_id=?), ?))",
            (user_id, username or "", nickname or "", user_id, now)
        )
        conn.commit()

def is_secondary_admin(user_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM secondary_admins WHERE user_id=?", (user_id,))
        return cur.fetchone() is not None

def stop_all_user_processes(user_id):
    """Stop every running project owned by a secondary admin.

    Auto-restart is disabled first so the monitor thread cannot bring a
    removed admin's projects back after they are terminated.
    """
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT id, pid FROM files WHERE user_id=?", (user_id,))
        rows = cur.fetchall()
        # Disable auto-restart before stopping anything to prevent a race
        # with the background monitor thread.
        cur.execute("UPDATE files SET auto_restart=0 WHERE user_id=?", (user_id,))
        conn.commit()

    stopped = 0
    for row in rows:
        file_id = row[0]
        pid = row[1]
        did_stop = False

        # Prefer the process object tracked by the bot.
        with proc_lock:
            proc_info = processes.get(file_id)

        if proc_info:
            proc = proc_info.get("process")
            try:
                if proc and proc.poll() is None:
                    # Terminate the whole child-process tree, not just the
                    # direct process, so spawned workers are stopped too.
                    try:
                        parent = psutil.Process(proc.pid)
                        children = parent.children(recursive=True)
                    except Exception:
                        children = []
                    for child in reversed(children):
                        try:
                            child.terminate()
                        except Exception:
                            pass
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                    gone, alive = psutil.wait_procs(children + [parent], timeout=3)
                    for child in alive:
                        try:
                            child.kill()
                        except Exception:
                            pass
                    try:
                        proc.wait(timeout=3)
                    except Exception:
                        pass
                    did_stop = True
            except Exception as e:
                logger.warning("Could not stop tracked process for file %s: %s", file_id, e)

            with proc_lock:
                processes.pop(file_id, None)
        elif pid:
            # Fallback for a process that is recorded in SQLite but is not
            # currently present in the in-memory process map.
            try:
                parent = psutil.Process(int(pid))
                children = parent.children(recursive=True)
                for child in reversed(children):
                    try:
                        child.terminate()
                    except Exception:
                        pass
                try:
                    parent.terminate()
                except Exception:
                    pass
                psutil.wait_procs(children + [parent], timeout=3)
                for proc in children + [parent]:
                    try:
                        if proc.is_running():
                            proc.kill()
                    except Exception:
                        pass
                did_stop = True
            except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, ValueError):
                pass
            except Exception as e:
                logger.warning("Could not stop recorded process %s for file %s: %s", pid, file_id, e)

        update_file_status(file_id, None, "Stopped")
        if did_stop:
            stopped += 1

    return stopped

def remove_secondary_admin(user_id):
    with db_lock:
        cur = conn.cursor()
        cur.execute("DELETE FROM secondary_admins WHERE user_id=?", (user_id,))
        changed = cur.rowcount > 0
        conn.commit()
    return changed

def get_secondary_admins():
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, nickname, added_at FROM secondary_admins ORDER BY added_at ASC")
        return cur.fetchall()

def secondary_admin_count():
    with db_lock:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM secondary_admins")
        return int(cur.fetchone()[0])

def admin_list_text():
    rows = get_secondary_admins()
    if not rows:
        return "{\n}\n"
    blocks = []
    for row in rows:
        user_id, username, nickname, added_at = row
        username_text = f"@{username}" if username else "N/A"
        nickname_text = nickname or "N/A"
        date_text = "00/00/0000"
        time_text = "00-00-00"
        try:
            dt = datetime.fromisoformat(added_at)
            local_dt = dt.astimezone()
            date_text = local_dt.strftime("%d/%m/%Y")
            time_text = local_dt.strftime("%H-%M-%S")
        except Exception:
            pass
        blocks.append(
            "{\n"
            "      {\n"
            f"       ᴜꜱᴇʀɴᴀᴍᴇ : {username_text}\n"
            f"       ᴜꜱᴇʀ ɪᴅ : {user_id}\n"
            f"       ɴɪᴄᴋɴᴀᴍᴇ : {nickname_text}\n"
            f"       ᴊᴏɪɴ ᴅᴀᴛᴇ : {date_text}\n"
            f"       ᴊᴏɪɴ ᴛɪᴍᴇ : {time_text}\n"
            "      }\n"
            "}"
        )
    return "\n".join(blocks) + "\n"

def main_menu_kb(user_id=None):
    # Hosting board is visible only to original/secondary admins.
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("🌌 ᴄʜᴀɴɴᴇʟ"), KeyboardButton("📂 ꜰɪʟᴇ ᴜᴩʟᴏᴀᴅ"))
    kb.add(KeyboardButton("📁 ᴍʏ ᴩʀᴏᴊᴇᴄᴛꜱ"), KeyboardButton("⚡ ꜱʏꜱᴛᴇᴍ ꜱᴛᴀᴛᴜꜱ"))
    kb.add(KeyboardButton("👑 ᴄᴏɴᴛᴀᴄᴛ ᴀᴅᴍɪɴ"))
    if user_id is not None and is_original_admin(user_id):
        # Keep admin-management buttons visible until the 10-admin limit is reached.
        # When 10 secondary admins exist, only REMOVE ADMIN remains.
        count = secondary_admin_count()
        if count < MAX_SECONDARY_ADMINS:
            kb.add(KeyboardButton("➕ ᴀᴅᴅ ᴀᴅᴍɪɴ"), KeyboardButton("➖ ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ"))
        else:
            kb.add(KeyboardButton("➖ ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ"))
    return kb

def management_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True)
    kb.add(KeyboardButton("❌ ᴇxɪᴛ"))
    return kb

def file_actions_kb(file_id, is_running, auto_restart):
    kb = InlineKeyboardMarkup(row_width=2)
    if is_running:
        kb.add(InlineKeyboardButton("⏹️ ꜱᴛᴏᴩ", callback_data=f"stop:{file_id}"),
               InlineKeyboardButton("🔄 ʀᴇꜱᴛᴀʀᴛ", callback_data=f"restart:{file_id}"))
    else:
        kb.add(InlineKeyboardButton("▶️ ᴛᴜʀɴ ᴏɴ", callback_data=f"start:{file_id}"),
               InlineKeyboardButton("🔄 ʀᴇꜱᴛᴀʀᴛ", callback_data=f"restart:{file_id}"))
    kb.add(InlineKeyboardButton("🗑️ ᴅᴇʟᴇᴛᴇ", callback_data=f"delete:{file_id}"),
           InlineKeyboardButton("📜 ᴠɪᴇᴡ ʟᴏɢꜱ", callback_data=f"logs:{file_id}"))
    kb.add(InlineKeyboardButton("⬇️ ᴅᴏᴡɴʟᴏᴀᴅ ʟᴏɢꜱ", callback_data=f"download_log:{file_id}"),
           InlineKeyboardButton("✏️ ʀᴇɴᴀᴍᴇ", callback_data=f"rename:{file_id}"))
    kb.add(InlineKeyboardButton("♻️ ᴀᴜᴛᴏ-ʀᴇꜱᴛᴀʀᴛ", callback_data=f"auto:{file_id}") if not auto_restart else
           InlineKeyboardButton("✅ ᴀᴜᴛᴏ-ʀᴇꜱᴛᴀʀᴛ ᴏɴ", callback_data=f"auto:{file_id}"))
    kb.add(InlineKeyboardButton("🔙 ʙᴀᴄᴋ", callback_data="back_to_files"))
    return kb

def confirm_kb(action, file_id):
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ ʏᴇꜱ", callback_data=f"confirm_{action}:{file_id}"),
           InlineKeyboardButton("❌ ɴᴏ", callback_data=f"cancel_{action}:{file_id}"))
    return kb

# ---------- ADMIN MANAGEMENT ----------
pending_admin_action = {}

@bot.message_handler(func=lambda m: m.text == "➕ ᴀᴅᴅ ᴀᴅᴍɪɴ")
def add_admin_button_handler(message):
    if not is_original_admin(message.from_user.id):
        return
    pending_admin_action[message.from_user.id] = "add"
    bot.send_message(message.chat.id, "➕ ᴀᴅᴅ ᴀᴅᴍɪɴ\n\nꜱᴇɴᴅ ᴛʜᴇ ᴜꜱᴇʀ ɪᴅ ᴛᴏ ᴀᴅᴅ ᴀꜱ ꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴ.", reply_markup=management_kb())

@bot.message_handler(func=lambda m: m.text == "➖ ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ")
def remove_admin_button_handler(message):
    if not is_original_admin(message.from_user.id):
        return
    pending_admin_action[message.from_user.id] = "remove"
    rows = get_secondary_admins()
    text = "➖ ʀᴇᴍᴏᴠᴇ ᴀᴅᴍɪɴ\n\nꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴ ʟɪꜱᴛ ɪꜱ ᴀᴛᴛᴀᴄʜᴇᴅ.\nꜱᴇɴᴅ ᴛʜᴇ ᴜꜱᴇʀ ɪᴅ ᴛᴏ ʀᴇᴍᴏᴠᴇ ɪᴛ."
    bot.send_message(message.chat.id, text, reply_markup=management_kb())
    content = admin_list_text()
    path = os.path.join(TEMP_DIR, "second_admin.txt")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content + "\n")
    with open(path, "rb") as f:
        bot.send_document(message.chat.id, f, caption="📄 ꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴ ʟɪꜱᴛ")

@bot.message_handler(func=lambda m: m.text == "❌ ᴇxɪᴛ")
def admin_management_exit(message):
    pending_admin_action.pop(message.from_user.id, None)
    if is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "✅ ᴍᴀɴᴀɢᴇᴍᴇɴᴛ ᴄʟᴏꜱᴇᴅ.", reply_markup=main_menu_kb(message.from_user.id))

@bot.message_handler(func=lambda m: m.from_user.id in pending_admin_action and bool(m.text))
def admin_management_input(message):
    user_id = message.from_user.id
    if not is_original_admin(user_id):
        pending_admin_action.pop(user_id, None)
        return
    action = pending_admin_action.get(user_id)
    text = (message.text or "").strip()
    if not text.isdigit():
        bot.send_message(message.chat.id, "❌ ᴘʟᴇᴀꜱᴇ ꜱᴇɴᴅ ᴀ ᴠᴀʟɪᴅ ᴜꜱᴇʀ ɪᴅ.", reply_markup=management_kb())
        return
    target_id = int(text)
    if action == "add":
        # Hard limit: maximum 10 secondary admins.
        if secondary_admin_count() >= MAX_SECONDARY_ADMINS:
            pending_admin_action.pop(user_id, None)
            bot.send_message(message.chat.id, "⚠️ ʏᴏᴜ ʜᴀᴠᴇ ʀᴇᴀᴄʜᴇᴅ ᴛʜᴇ ᴍᴀxɪᴍᴜᴍ ᴏꜰ 10 ꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴꜱ.", reply_markup=main_menu_kb(user_id))
            return
        if target_id in ORIGINAL_ADMIN_IDS:
            bot.send_message(message.chat.id, "⚠️ ᴛʜɪꜱ ᴜꜱᴇʀ ɪꜱ ᴀʟʀᴇᴀᴅʏ ᴀɴ ᴏʀɪɢɪɴᴀʟ ᴀᴅᴍɪɴ.", reply_markup=management_kb())
            return
        with db_lock:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM secondary_admins WHERE user_id=?", (target_id,))
            if cur.fetchone() is not None:
                bot.send_message(message.chat.id, "⚠️ ᴛʜɪꜱ ᴜꜱᴇʀ ɪꜱ ᴀʟʀᴇᴀᴅʏ ᴀ ꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴ.", reply_markup=management_kb())
                return
        username = ""
        nickname = ""
        try:
            chat = bot.get_chat(target_id)
            username = chat.username or ""
            nickname = getattr(chat, "first_name", "") or ""
            if getattr(chat, "last_name", None):
                nickname = f"{nickname} {chat.last_name}".strip()
        except Exception:
            pass
        # Fall back to information already known by the bot.
        if not nickname or not username:
            with db_lock:
                cur = conn.cursor()
                cur.execute("SELECT username, joined_at FROM users WHERE user_id=?", (target_id,))
                known = cur.fetchone()
            if known:
                username = username or known[0] or ""
        add_secondary_admin(target_id, username, nickname)
        pending_admin_action.pop(user_id, None)
        bot.send_message(message.chat.id, f"✅ ᴜꜱᴇʀ <code>{target_id}</code> ɪꜱ ɴᴏᴡ ᴀ ꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴ.", reply_markup=main_menu_kb(user_id))
        try:
            bot.send_message(target_id, "👑 ʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ᴀᴅᴅᴇᴅ ᴀꜱ ᴀ ꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴ.", reply_markup=main_menu_kb(target_id))
        except Exception as e:
            logger.info("Could not notify new secondary admin %s: %s", target_id, e)
    elif action == "remove":
        if target_id in ORIGINAL_ADMIN_IDS:
            bot.send_message(message.chat.id, "⛔ ᴛʜᴇ ᴏʀɪɢɪɴᴀʟ ᴀᴅᴍɪɴ ᴄᴀɴɴᴏᴛ ʙᴇ ʀᴇᴍᴏᴠᴇᴅ.", reply_markup=management_kb())
            return
        if is_secondary_admin(target_id):
            # Stop all of the secondary admin's hosted projects BEFORE
            # removing their admin record. This also disables auto-restart.
            stopped_count = stop_all_user_processes(target_id)
        else:
            stopped_count = 0

        if remove_secondary_admin(target_id):
            pending_admin_action.pop(user_id, None)
            bot.send_message(
                message.chat.id,
                f"✅ ᴜꜱᴇʀ <code>{target_id}</code> ʜᴀꜱ ʙᴇᴇɴ ʀᴇᴍᴏᴠᴇᴅ.\n"
                f"🛑 {stopped_count} ʀᴜɴɴɪɴɢ ᴘʀᴏᴊᴇᴄᴛ(ꜱ) ʜᴀᴠᴇ ʙᴇᴇɴ ꜱᴛᴏᴘᴘᴇᴅ.\n"
                f"♻️ ᴀᴜᴛᴏ-ʀᴇꜱᴛᴀʀᴛ ʜᴀꜱ ʙᴇᴇɴ ᴅɪꜱᴀʙʟᴇᴅ.",
                reply_markup=main_menu_kb(user_id)
            )
            try:
                bot.send_message(target_id, "⛔ ʏᴏᴜ ʜᴀᴠᴇ ʙᴇᴇɴ ʀᴇᴍᴏᴠᴇᴅ ꜰʀᴏᴍ ꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴꜱ.")
            except Exception as e:
                logger.info("Could not notify removed secondary admin %s: %s", target_id, e)
        else:
            bot.send_message(message.chat.id, "❌ ᴛʜᴀᴛ ᴜꜱᴇʀ ɪꜱ ɴᴏᴛ ᴀ ꜱᴇᴄᴏɴᴅᴀʀʏ ᴀᴅᴍɪɴ.", reply_markup=management_kb())

# ---------- HANDLERS ----------
@bot.message_handler(commands=['start', 'help'])
def start_handler(message):
    user = message.from_user
    user_id = user.id
    with db_lock:
        cur = conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO users (user_id, username, joined_at, last_seen) VALUES (?, ?, ?, ?)",
            (user_id, user.username or "", datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat())
        )
        conn.commit()

    # Only regular users receive the public start/welcome post.
    # Original Admins and Secondary Admins go directly to the hosting board.
    if not is_admin(user_id):
        try:
            bot.forward_message(message.chat.id, SOURCE_CHANNEL, SOURCE_MESSAGE_ID)
        except Exception as e:
            logger.warning("Could not forward channel post: %s", e)
            bot.send_message(message.chat.id, "⚠️ ᴡᴇʟᴄᴏᴍᴇ ᴩᴏꜱᴛ ᴄᴏᴜʟᴅ ɴᴏᴛ ʙᴇ ꜰᴏʀᴡᴀʀᴅᴇᴅ. ᴩʟᴇᴀꜱᴇ ᴄᴏɴᴛᴀᴄᴛ ᴀᴅᴍɪɴ.")
        return

    user_nickname = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip() or "User"
    username = f"@{user.username}" if user.username else "N/A"
    files = list_user_files(user_id)

    welcome = f"""🌌 ʜᴇʏ {html_lib.escape(user_nickname)} ᴡᴇʟᴄᴏᴍᴇ ᴛᴏ ʟᴜᴄᴋʏ ʜᴏꜱᴛɪɴɢ ʙᴏᴛ 🌌

╔═══════════════════════════╗
║                  ʀ ᴇ ᴅ     ʟ ᴜ ᴄ ᴋ ʏ     x ʏ ᴢ                     ║
╚═══════════════════════════╝

🚀 ᴡᴀʀɴɪɴɢ: ᴛʜɪꜱ ʙᴏᴛ ᴛᴀᴋᴇꜱ ʏᴏᴜʀ ᴄᴏᴅᴇ ᴅᴇᴇᴩ ɪɴᴛᴏ ᴛʜᴇ ɢᴀʟᴀxʏ ᴛᴀᴋᴇꜱ ʏᴏᴜʀ ᴄᴏᴅᴇ।
🌀 24/7 ʜᴏꜱᴛɪɴɢ ᴡɪᴛʜ ꜱᴜᴩᴇʀɴᴀᴛᴜʀᴀʟ ᴩᴏᴡᴇʀ

👤 ᴜꜱᴇʀ: {html_lib.escape(username)}
🆔 ɪᴅ: {user_id}
📂 ᴩʀᴏᴊᴇᴄᴛ: {len(files)} / {MAX_FILES_PER_USER}

🔮 ᴡʜᴀᴛ ʏᴏᴜ ᴄᴀɴ ᴅᴏ:•
🌌 ʀᴜɴ ᴩʏᴛʜᴏɴ ᴀɴᴅ ᴊꜱ ꜱᴄʀɪᴩᴛꜱ 24/7•
📦 ᴀᴜᴛᴏ-ɪɴꜱᴛᴀʟʟ ᴅᴇᴩᴇɴᴅᴇɴᴄɪᴇꜱ ᴀɴᴅ ɪᴍᴩᴏʀᴛꜱ•
📜 ʀᴇᴀʟ-ᴛɪᴍᴇ ʟᴏɢꜱ ᴀɴᴅ ᴩʀᴏᴄᴇꜱꜱ ᴄᴏɴᴛʀᴏʟ•
♻️ ᴀᴜᴛᴏ-ʀᴇꜱᴛᴀʀᴛ ᴏɴ ᴄʀᴀꜱʜ (ᴏᴩᴛɪᴏɴᴀʟ)•
🌐 ᴇɴᴠɪʀᴏɴᴍᴇɴᴛ ᴠᴀʀɪᴀʙʟᴇ ꜱᴜᴩᴩᴏʀᴛ•
💀 ɢʜᴏꜱᴛ ᴍᴏᴅᴇ – ʀᴜɴꜱ ɪɴ ᴛʜᴇ ʙᴀᴄᴋɢʀᴏᴜɴᴅ

👇 ᴜꜱᴇ ᴛʜᴇ ʙᴜᴛᴛᴏɴꜱ ʙᴇʟᴏᴡ ᴛᴏ ʀᴜɴ ʏᴏᴜʀ ꜱᴄʀɪᴩᴛꜱ!"""

    try:
        if os.path.isfile(WELCOME_VIDEO_FILE):
            with open(WELCOME_VIDEO_FILE, "rb") as video:
                bot.send_video(
                    message.chat.id,
                    video,
                    caption=welcome,
                    reply_markup=main_menu_kb(user_id),
                    supports_streaming=True,
                )
        else:
            bot.send_message(
                message.chat.id,
                welcome,
                reply_markup=main_menu_kb(user_id),
            )
    except Exception as e:
        logger.warning("Could not send welcome video: %s", e)
        bot.send_message(
            message.chat.id,
            welcome,
            reply_markup=main_menu_kb(user_id),
        )

@bot.message_handler(func=lambda m: m.text == "🌌 ᴄʜᴀɴɴᴇʟ")
def updates_handler(m):
    if not is_admin(m.from_user.id):
        return
    try:
        bot.forward_message(m.chat.id, SOURCE_CHANNEL, CHANNEL_MESSAGE_ID)
    except Exception as e:
        logger.warning("Could not forward channel post: %s", e)
        bot.send_message(m.chat.id, "⚠️ ᴄʜᴀɴɴᴇʟ ᴩᴏꜱᴛ ᴄᴏᴜʟᴅ ɴᴏᴛ ʙᴇ ꜰᴏʀᴡᴀʀᴅᴇᴅ.")

@bot.message_handler(func=lambda m: m.text == "👑 ᴄᴏɴᴛᴀᴄᴛ ᴀᴅᴍɪɴ")
def contact_handler(m):
    if not is_admin(m.from_user.id):
        return
    try:
        bot.forward_message(m.chat.id, SOURCE_CHANNEL, CONTACT_MESSAGE_ID)
    except Exception as e:
        logger.warning("Could not forward contact post: %s", e)
        bot.send_message(m.chat.id, "⚠️ ᴄᴏɴᴛᴀᴄᴛ ᴩᴏꜱᴛ ᴄᴏᴜʟᴅ ɴᴏᴛ ʙᴇ ꜰᴏʀᴡᴀʀᴅᴇᴅ.")

@bot.message_handler(func=lambda m: m.text == "⚡ ꜱʏꜱᴛᴇᴍ ꜱᴛᴀᴛᴜꜱ")
def status_handler(m):
    if not is_admin(m.from_user.id):
        return
    cpu, mem, proc_count, disk = get_system_load()
    uptime = datetime.now(timezone.utc) - START_TIME
    days, rem = divmod(uptime.total_seconds(), 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    uptime_str = f"{int(days)}d {int(hours)}h {int(minutes)}m"
    text = f"""
🌌 <b><u>System Status</u></b>

⚡ <b>CPU</b>: {cpu:.1f}%  💾 <b>Memory</b>: {mem:.1f}%
💿 <b>Disk</b>: {disk:.1f}%  📌 <b>Running</b>: {proc_count} / {MAX_RUNNING_PROCESSES}
⏳ <b>Uptime</b>: {uptime_str}

🌀 <i>System is running normally...</i>
"""
    bot.send_message(m.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📊 Statistics")
def stats_handler(m):
    if not is_admin(m.from_user.id):
        return
    cur = conn.cursor()
    cur.execute("SELECT COUNT(DISTINCT user_id) FROM files")
    users = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files")
    total_files = cur.fetchone()[0] or 0
    cur.execute("SELECT COUNT(*) FROM files WHERE status='Running'")
    running = cur.fetchone()[0] or 0
    cpu, mem, _, _ = get_system_load()
    text = f"""
🌌 <b><u>Statistics</u></b>

👥 <b>Total Users</b>: {users}
📁 <b>Total Projects</b>: {total_files}
🚀 <b>Running</b>: {running}
⚡ <b>CPU</b>: {cpu:.1f}%
💾 <b>Memory</b>: {mem:.1f}%

🌀 <i>Many souls have found shelter here...</i>
"""
    bot.send_message(m.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "ℹ️ About Bot")
def info_handler(m):
    if not is_admin(m.from_user.id):
        return
    cpu, mem, proc_count, disk = get_system_load()
    uptime = datetime.now(timezone.utc) - START_TIME
    uptime_str = str(uptime).split('.')[0]
    text = f"""
🌌 <b><u>About Bot</u></b>

🤖 <b>Version</b>: 3.0 (Cosmic)
👨‍💻 <b>Developer</b>: RAFIN TxT
📅 <b>Uptime</b>: {uptime_str}
💻 <b>System</b>: {sys.platform}
🐍 <b>Python</b>: {sys.version.split()[0]}

⚙️ <b>Limits</b>:
• Maximum projects per user: {MAX_FILES_PER_USER}
• Maximum concurrent processes: {MAX_RUNNING_PROCESSES}
• CPU threshold: {CPU_THRESHOLD}%
• Memory threshold: {MEMORY_THRESHOLD}%

📊 <b>Current Load</b>:
• CPU: {cpu:.1f}%
• Memory: {mem:.1f}%
• Disk: {disk:.1f}%
• Active processes: {proc_count}

🌀 <i>This bot runs on pure cosmic energy.</i>
"""
    bot.send_message(m.chat.id, text)

@bot.message_handler(func=lambda m: m.text == "📁 ᴍʏ ᴩʀᴏᴊᴇᴄᴛꜱ")
def my_files_handler(m):
    if not is_admin(m.from_user.id):
        return
    send_files_list(m.chat.id, m.from_user.id)

def send_files_list(chat_id, user_id):
    files = list_user_files(user_id)
    if not files:
        bot.send_message(chat_id, "🌌 <b>Your Project</b>\n\nNo projects uploaded. Click the 'File Upload' button.")
        return
    text = "🌌 <b>Your Project</b>\n\nTap a project to manage it:"
    kb = InlineKeyboardMarkup()
    for f in files:
        file_id, filename, orig_name, uploaded, file_type, status, pid = f
        emoji = "🟢" if status == "Running" else "🔴"
        btn_text = f"{emoji} {orig_name} ({file_type})"
        kb.add(InlineKeyboardButton(btn_text, callback_data=f"manage:{file_id}"))
    bot.send_message(chat_id, text, reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📂 ꜰɪʟᴇ ᴜᴩʟᴏᴀᴅ")
def upload_handler(m):
    if not is_admin(m.from_user.id):
        return
    bot.send_message(m.chat.id,
        "🌌 <b>Upload a file</b>\n\n"
        "Send me <b>Python</b> (.py), <b>JavaScript</b> (.js) or a <b>ZIP/TAR</b> archive.\n\n"
        "✅ If it is an archive, I will auto-extract it, find the main file, install dependencies, and run it.\n"
        "✅ If you want, you can upload a <b>.env</b> file to set environment variables.।\n\n"
        "🌀 <i>I will handle the rest......</i>"
    )

@bot.message_handler(content_types=['document'])
def document_handler(message):
    user = message.from_user
    user_id = user.id
    if not is_admin(user_id):
        bot.reply_to(message, "⛔ ᴀᴅᴍɪɴ ᴏɴʟʏ\nᴛʜɪꜱ ʜᴏꜱᴛɪɴɢ ᴩᴀɴᴇʟ ɪꜱ ᴀᴠᴀɪʟᴀʙʟᴇ ᴏɴʟʏ ᴛᴏ ᴀᴅᴍɪɴꜱ.")
        return
    if len(list_user_files(user_id)) >= MAX_FILES_PER_USER:
        bot.reply_to(message, f"❌ Project limit exceeded ({MAX_FILES_PER_USER})। Delete some files first।")
        return

    doc = message.document
    orig_name = doc.file_name or "unknown"
    file_type = get_file_type(orig_name)

    if file_type == "env":
        handle_env_upload(message, user_id, orig_name)
        return

    try:
        file_info = bot.get_file(doc.file_id)
        file_bytes = bot.download_file(file_info.file_path)
    except Exception as e:
        bot.reply_to(message, f"❌ Download failed: {str(e)}")
        return

    user_dir = os.path.join(UPLOADS_DIR, str(user_id))
    os.makedirs(user_dir, exist_ok=True)
    safe_name = f"{int(time.time())}_{orig_name}"
    file_path = os.path.join(user_dir, safe_name)
    with open(file_path, 'wb') as f:
        f.write(file_bytes)

    final_path = file_path
    extracted_dir = None
    main_file = None

    if file_type in ("zip", "archive"):
        bot.reply_to(message, "🌀 Extracting archive...")
        extracted_dir = os.path.join(TEMP_DIR, f"extracted_{user_id}_{int(time.time())}")
        os.makedirs(extracted_dir, exist_ok=True)
        ok, err = extract_archive(file_path, extracted_dir)
        if not ok:
            bot.reply_to(message, f"❌ Extraction error: {err}")
            os.remove(file_path)
            return
        main_file = find_main_file(extracted_dir)
        if not main_file:
            bot.reply_to(message, "❌ No main Python/JS file found in the archive।")
            shutil.rmtree(extracted_dir, ignore_errors=True)
            os.remove(file_path)
            return
        final_path = extracted_dir
        file_type = get_file_type(main_file)

    env_vars = None
    if extracted_dir:
        env_path = os.path.join(extracted_dir, ".env")
        if os.path.exists(env_path):
            env_vars = parse_env_file(env_path)

    file_id = add_file_record(user_id, user.username, safe_name, orig_name, final_path, file_type, env_vars)

    if file_type in ("python", "javascript") and extracted_dir is None:
        bot.reply_to(message, f"✅ File <b>{html_lib.escape(orig_name)}</b> uploaded! Starting automatically...")
        start_file_process(file_id, message.chat.id)
    else:
        bot.reply_to(message, f"✅ File <b>{html_lib.escape(orig_name)}</b> uploaded! to manage 'My Projects' go to।")

def handle_env_upload(message, user_id, orig_name):
    files = list_user_files(user_id)
    if not files:
        bot.reply_to(message, "❌ You have no projects। Upload a script first, then provide the .env file।")
        return
    latest = files[0]
    file_id = latest['id']
    try:
        file_info = bot.get_file(message.document.file_id)
        content = bot.download_file(file_info.file_path).decode('utf-8')
        env_vars = parse_env_content(content)
        update_file_env(file_id, env_vars)
        bot.reply_to(message, f"✅ Environment variables updated <b>{html_lib.escape(latest['orig_name'])}</b> for")
    except Exception as e:
        bot.reply_to(message, f"❌ .env Failed to parse: {str(e)}")

def parse_env_file(path):
    try:
        with open(path, 'r') as f:
            return parse_env_content(f.read())
    except:
        return {}

def parse_env_content(content):
    env = {}
    for line in content.splitlines():
        line = line.strip()
        if line and not line.startswith('#'):
            if '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

# -------------------- START/STOP PROCESSES --------------------
def start_file_process(file_id, chat_id):
    should_stop, reason = should_stop_due_to_load()
    if should_stop:
        bot.send_message(chat_id, f"⚠️ Cannot start: {reason}")
        return

    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ Project not found")
        return

    path = file_record["path"]
    orig_name = file_record["orig_name"]
    file_type = file_record["file_type"]
    env_vars = json.loads(file_record["env_vars"]) if file_record["env_vars"] else {}

    target_file = None
    working_dir = None
    if os.path.isdir(path):
        target_file = find_main_file(path)
        if not target_file:
            bot.send_message(chat_id, "❌ No main file in the directory")
            return
        working_dir = os.path.dirname(target_file)
    else:
        if not os.path.exists(path):
            bot.send_message(chat_id, f"❌ File not found: {path}")
            return
        target_file = path
        working_dir = os.path.dirname(path)

    ext = os.path.splitext(target_file)[1].lower()

    if ext == ".py":
        req_path = os.path.join(working_dir, "requirements.txt")
        if os.path.exists(req_path):
            bot.send_message(chat_id, "🌀 Installing requirements.txt...")
            ok, msg = install_requirements_from_file(req_path, chat_id, orig_name)
            bot.send_message(chat_id, f"📦 {msg}")
        bot.send_message(chat_id, "🌀 Checking missing imports...")
        imports = extract_imports(target_file)
        if imports:
            ok, msg = install_missing_imports(imports, chat_id, orig_name)
            bot.send_message(chat_id, f"📦 {msg}")

    if ext == ".py":
        cmd = [sys.executable, target_file]
    elif ext == ".js":
        cmd = ["node", target_file]
    else:
        bot.send_message(chat_id, f"❌ Unsupported file type: {ext}")
        return

    log_name = f"file_{file_id}_{int(time.time())}.log"
    log_path = os.path.join(LOGS_DIR, log_name)

    try:
        env = os.environ.copy()
        env.update(env_vars)
        with open(log_path, 'w') as log_file:
            process = subprocess.Popen(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=working_dir,
                env=env,
                text=True
            )

        run_id = record_run_start(file_id, process.pid, log_path)
        update_file_status(file_id, process.pid, "Running")

        with proc_lock:
            processes[file_id] = {
                'process': process,
                'run_id': run_id,
                'log_path': log_path,
                'started_at': datetime.now(timezone.utc).isoformat()
            }

        bot.send_message(chat_id,
            f"✅ <b>{html_lib.escape(orig_name)}</b> started!\n"
            f"📝 PID: <code>{process.pid}</code>\n"
            f"📁 Log: <code>{log_name}</code>\n"
            f"🌀 <i>Your script is now under my control...</i>"
        )

        def monitor():
            try:
                exit_code = process.wait()
            except:
                exit_code = -1
            finally:
                update_file_status(file_id, None, "Stopped")
                record_run_finish(run_id, exit_code)
                with proc_lock:
                    processes.pop(file_id, None)
                auto = get_file_record(file_id)
                if auto and auto['auto_restart']:
                    if exit_code != 0:
                        bot.send_message(chat_id, f"🔄 <b>{html_lib.escape(orig_name)}</b> crashed (exit {exit_code})। Restarting...")
                        time.sleep(2)
                        start_file_process(file_id, chat_id)
                    else:
                        bot.send_message(chat_id, f"✅ <b>{html_lib.escape(orig_name)}</b> finished successfully।")
                else:
                    if exit_code != 0:
                        bot.send_message(chat_id, f"⚠️ <b>{html_lib.escape(orig_name)}</b> stopped, exit code {exit_code}")

        threading.Thread(target=monitor, daemon=True).start()

    except Exception as e:
        bot.send_message(chat_id, f"❌ Failed to start: {str(e)}")

def stop_file_process(file_id):
    with proc_lock:
        if file_id not in processes:
            return False
        proc_info = processes[file_id]
        proc = proc_info['process']
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
        except:
            pass
        processes.pop(file_id, None)
    update_file_status(file_id, None, "Stopped")
    return True

def get_file_logs(file_id, lines=50):
    with proc_lock:
        if file_id in processes:
            log_path = processes[file_id]['log_path']
            if os.path.exists(log_path):
                with open(log_path, 'r') as f:
                    content = f.readlines()
                return ''.join(content[-lines:]) if content else "No logs yet"
    cur = conn.cursor()
    cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1", (file_id,))
    row = cur.fetchone()
    if row and row[0] and os.path.exists(row[0]):
        with open(row[0], 'r') as f:
            content = f.readlines()
        return ''.join(content[-lines:]) if content else "No logs"
    return "Log file not found"

# -------------------- CALLBACKS --------------------
pending_rename = {}

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "⛔ ᴀᴅᴍɪɴ ᴏɴʟʏ", show_alert=True)
        return
    data = call.data
    chat_id = call.message.chat.id
    user_id = call.from_user.id
    msg_id = call.message.message_id

    if data == "back_to_files":
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
        send_files_list(chat_id, user_id)
        return

    parts = data.split(":")
    action = parts[0]

    if action == "manage":
        file_id = int(parts[1])
        show_file_management(chat_id, file_id, user_id, msg_id)
    elif action == "start":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "Starting...")
        start_file_process(file_id, chat_id)
        time.sleep(1)
        show_file_management(chat_id, file_id, user_id, msg_id)
    elif action == "stop":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "Stopping...")
        stop_file_process(file_id)
        bot.send_message(chat_id, "⏹️ Process stopped")
        time.sleep(1)
        show_file_management(chat_id, file_id, user_id, msg_id)
    elif action == "restart":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "Restarting...")
        stop_file_process(file_id)
        time.sleep(2)
        start_file_process(file_id, chat_id)
        time.sleep(1)
        show_file_management(chat_id, file_id, user_id, msg_id)
    elif action == "delete":
        file_id = int(parts[1])
        kb = confirm_kb("delete", file_id)
        bot.edit_message_text("🗑️ Are you sure you want to delete this Project?", chat_id, msg_id, reply_markup=kb)
    elif action == "confirm_delete":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "Deleting...")
        file_record = get_file_record(file_id)
        if file_record:
            stop_file_process(file_id)
            fpath = file_record["path"]
            try:
                if os.path.isdir(fpath):
                    shutil.rmtree(fpath, ignore_errors=True)
                elif os.path.exists(fpath):
                    os.remove(fpath)
            except:
                pass
            remove_file_record(file_id)
        bot.send_message(chat_id, "🗑️ Project deleted")
        send_files_list(chat_id, user_id)
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
    elif action == "cancel_delete":
        try:
            bot.delete_message(chat_id, msg_id)
        except:
            pass
        show_file_management(chat_id, int(parts[1]), user_id, None)
    elif action == "logs":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "Fetching logs...")
        logs = get_file_logs(file_id)
        file_record = get_file_record(file_id)
        fname = file_record["orig_name"] if file_record else "Unknown"
        if len(logs) > 4000:
            logs = logs[-4000:]
            logs = "... (truncated) ...\n" + logs
        text = f"🌌 <b>{html_lib.escape(fname)} logs</b>\n\n<pre>{html_lib.escape(logs)}</pre>"
        bot.send_message(chat_id, text)
    elif action == "download_log":
        file_id = int(parts[1])
        bot.answer_callback_query(call.id, "Preparing logs...")
        with proc_lock:
            if file_id in processes:
                log_path = processes[file_id]['log_path']
            else:
                cur = conn.cursor()
                cur.execute("SELECT log_path FROM runs WHERE file_id=? ORDER BY started_at DESC LIMIT 1", (file_id,))
                row = cur.fetchone()
                log_path = row[0] if row and row[0] else None
        if log_path and os.path.exists(log_path):
            with open(log_path, 'rb') as f:
                bot.send_document(chat_id, f, caption="🌌 Log file")
        else:
            bot.send_message(chat_id, "❌ Log file not found")
    elif action == "rename":
        file_id = int(parts[1])
        pending_rename[user_id] = file_id
        bot.send_message(chat_id, "✏️ Send the new name for this project (including the extension):")
    elif action == "auto":
        file_id = int(parts[1])
        file_record = get_file_record(file_id)
        if not file_record:
            bot.answer_callback_query(call.id, "Project not found")
            return
        current = file_record['auto_restart']
        new_val = 0 if current else 1
        set_auto_restart(file_id, new_val)
        bot.answer_callback_query(call.id, f"Auto-restart {'ON' if new_val else 'OFF'} completed")
        show_file_management(chat_id, file_id, user_id, msg_id)

@bot.message_handler(func=lambda m: m.from_user.id in pending_rename)
def rename_handler(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        pending_rename.pop(user_id, None)
        return
    file_id = pending_rename.pop(user_id, None)
    if not file_id:
        return
    new_name = message.text.strip()
    if not new_name:
        bot.reply_to(message, "❌ Invalid name")
        return
    with db_lock:
        cur = conn.cursor()
        cur.execute("UPDATE files SET orig_name=? WHERE id=?", (new_name, file_id))
        conn.commit()
    bot.reply_to(message, f"✅ Project renamed to <b>{html_lib.escape(new_name)}</b> set")
    send_files_list(message.chat.id, user_id)

@bot.message_handler(func=lambda m: m.text == "⏹️ Stop All")
def stop_all_handler(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        return
    files = list_user_files(user_id)
    stopped = 0
    for f in files:
        if f['status'] == 'Running':
            if stop_file_process(f['id']):
                stopped += 1
    bot.send_message(message.chat.id, f"⏹️ {stopped} running processes have been stopped.")

# -------------------- ADMIN COMMANDS --------------------
@bot.message_handler(commands=['admin'])
def admin_handler(message):
    if not is_original_admin(message.from_user.id):
        bot.reply_to(message, "⛔ ᴏɴʟʏ ᴛʜᴇ ᴏʀɪɢɪɴᴀʟ ᴀᴅᴍɪɴ ᴄᴀɴ ᴜꜱᴇ ᴛʜɪꜱ.")
        return
    cur = conn.cursor()
    cur.execute("SELECT user_id, username, COUNT(*) as file_count FROM files GROUP BY user_id")
    users = cur.fetchall()
    text = "👑 <b>Admin Panel</b>\n\n"
    for u in users:
        text += f"👤 {u['username'] or 'Unknown'} (ID: {u['user_id']}) – {u['file_count']} projects\n"
    with proc_lock:
        running = list(processes.keys())
    text += f"\n🚀 Running processes: {len(running)}"
    bot.send_message(message.chat.id, text)

# -------------------- SHOW PROJECT MANAGEMENT --------------------
def show_file_management(chat_id, file_id, user_id, message_id=None):
    file_record = get_file_record(file_id)
    if not file_record:
        bot.send_message(chat_id, "❌ Project not found")
        return
    if file_record["user_id"] != user_id and not is_admin(user_id):
        bot.send_message(chat_id, "❌ Access denied")
        return

    is_running = file_id in processes
    auto_restart = bool(file_record['auto_restart'])

    status = "🟢 Running" if is_running else "🔴 OFF"
    pid = f"\nPID: {file_record['pid']}" if file_record['pid'] else ""
    env = f"\nENV: {len(json.loads(file_record['env_vars']) if file_record['env_vars'] else {})} variables" if file_record['env_vars'] else ""

    text = f"""
🌌 <b>Project Management</b>

📁 <b>Project</b>: {html_lib.escape(file_record['orig_name'])}
📊 <b>Type</b>: {file_record['file_type']}
📈 <b>Status</b>: {status}{pid}{env}
⏰ <b>Uploaded</b>: {file_record['uploaded_at'][:16]}
♻️ <b>Auto-restart</b>: {'✅ ON' if auto_restart else '❌ OFF'}

🌀 <i>Manage carefully - this project is now under my control.</i>
"""
    kb = file_actions_kb(file_id, is_running, auto_restart)
    if message_id:
        try:
            bot.edit_message_text(text, chat_id, message_id, reply_markup=kb)
        except:
            bot.send_message(chat_id, text, reply_markup=kb)
    else:
        bot.send_message(chat_id, text, reply_markup=kb)

# -------------------- START BOT (GHOST MODE) --------------------
def run_daemon():
    try:
        if os.fork() > 0:
            sys.exit(0)
        os.setsid()
        if os.fork() > 0:
            sys.exit(0)
        sys.stdout = open('/dev/null', 'w')
        sys.stderr = open('/dev/null', 'w')
        os.chdir('/')
    except:
        pass

def start_bot():
    logger.info("🌌 RED LUCKY XYZ BOT is starting in polling mode...")
    while True:
        try:
            bot.infinity_polling(timeout=60, long_polling_timeout=50)
        except Exception as e:
            logger.error(f"Polling error: {e}")
            time.sleep(5)


def run_local():
    """Run Flask website and Telegram bot polling together locally."""
    import threading

    def start_polling():
        try:
            start_bot()
        except Exception:
            logger.exception("Telegram polling stopped")

    print("")
    print("=" * 60)
    print("🌐 RED LUCKY XYZ LOCAL WEBSITE")
    print(f"🔗 Local URL: http://127.0.0.1:{LOCAL_PORT}")
    print(f"🔗 Localhost: http://localhost:{LOCAL_PORT}")
    print("🤖 Telegram bot: polling mode")
    print("=" * 60)
    print("Open the URL above in your browser.")
    print("Press CTRL+C to stop.")
    print("")

    threading.Thread(target=start_polling, daemon=True).start()
    app.run(host=LOCAL_HOST, port=LOCAL_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    if os.environ.get("VERCEL") or os.environ.get("WEBHOOK_URL"):
        pass
    else:
        run_local()
