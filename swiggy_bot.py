import io
import json
import os
import re
import sys
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import swiggy_api_signup as api
import swiggy_signup as ss

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BOT_CONFIG = os.path.join(BASE_DIR, "swiggy_bot.json")
ACCOUNTS_PATH = os.path.join(BASE_DIR, "accounts.json")

try:
    import telebot
except ImportError:
    telebot = None

RUN_LOCK = threading.Lock()
RUNNING = {"active": False, "done": 0, "total": 0, "cancel": False}


def log(msg):
    ts = time.strftime("%H:%M:%S")
    msg_str = str(msg)
    try:
        print("[%s] %s" % (ts, msg_str), flush=True)
    except Exception:
        try:
            print("[%s] %s" % (ts, msg_str.encode("ascii", "replace").decode("ascii")), flush=True)
        except Exception:
            pass


def safe_reply(bot, msg, text, parse_mode="Markdown", reply_markup=None):
    if not bot or not msg:
        return None
    try:
        return bot.reply_to(msg, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        try:
            return bot.reply_to(msg, text, reply_markup=reply_markup)
        except Exception as e:
            log(f"safe_reply failed: {e}")
            return None


def safe_send_message(bot, chat_id, text, parse_mode="Markdown", reply_markup=None):
    if not bot:
        return None
    try:
        return bot.send_message(chat_id, text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception:
        try:
            return bot.send_message(chat_id, text, reply_markup=reply_markup)
        except Exception as e:
            log(f"safe_send_message failed: {e}")
            return None


def get_control_keyboard():
    if telebot is None:
        return None
    markup = telebot.types.InlineKeyboardMarkup(row_width=2)
    markup.add(
        telebot.types.InlineKeyboardButton("🚫 Cancel All Numbers (Refund)", callback_data="cancel_all_rentals"),
        telebot.types.InlineKeyboardButton("💳 Check Balance", callback_data="check_balance"),
    )
    return markup


def load_bot_config():
    if os.path.exists(BOT_CONFIG):
        try:
            with open(BOT_CONFIG, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


def is_authorized(chat_id, cfg=None, bot=None, msg=None):
    cfg = load_bot_config() or cfg or {}
    allowed = cfg.get("allowed_users") or []
    if not allowed:
        return True
    try:
        cid = int(chat_id)
        if cid in [int(u) for u in allowed]:
            return True
    except Exception:
        pass
    log(f"Unauthorized request from chat_id: {chat_id}")
    if bot and msg:
        safe_reply(
            bot,
            msg,
            f"⚠️ Access Restricted.\nYour Telegram Chat ID is: `{chat_id}`\n\nAdd `{chat_id}` to `allowed_users` in `swiggy_bot.json` to grant access.",
            parse_mode="Markdown",
        )
    return False


def find_account_by_query(query, accounts):
    """Searches for an account by phone number, customerId, or name."""
    if not query or not accounts:
        return None
    q = str(query).strip()
    digits = re.sub(r"\D", "", q)
    if len(digits) > 10 and digits.startswith("91"):
        digits = digits[2:]

    # 1. Exact match by mobile / phoneNumber
    for a in reversed(accounts):
        mob = str(a.get("mobile") or a.get("phoneNumber") or "").strip()
        clean_mob = re.sub(r"\D", "", mob)
        if clean_mob and (clean_mob == digits or mob == q):
            return a

    # 2. Exact match by customerId
    for a in reversed(accounts):
        cid = str(a.get("customerId") or a.get("customer_id") or "").strip()
        if cid and cid == q:
            return a

    # 3. Partial match by name
    if len(q) >= 3:
        for a in reversed(accounts):
            name = str(a.get("name") or a.get("userName") or "").strip().lower()
            if q.lower() in name:
                return a

    return None


def ensure_account_fields(acct):
    """Ensures customerId, name, userName, phoneNumber, and valid live session are present."""
    if not acct:
        return acct

    # 1. Ensure name and userName are populated
    name = (
        acct.get("userName")
        or acct.get("name")
        or (ss.random_name() if hasattr(ss, "random_name") else "Swiggy User")
    )
    acct["name"] = name
    acct["userName"] = name

    # 2. Ensure mobile and phoneNumber are populated
    mob = str(acct.get("phoneNumber") or acct.get("mobile") or "").strip()
    acct["mobile"] = mob
    acct["phoneNumber"] = mob

    # 3. Ensure customerId is populated
    if not acct.get("customerId") or str(acct.get("customerId")) in ["", "?", "None", "0"]:
        acct["customerId"] = api.extract_customer_id_from_any(acct) or api.fetch_profile_customer_id(
            acct.get("tid", ""), acct.get("sid", ""), acct.get("deviceId", "")
        )

    # 4. Live profile refresh: fetch fresh session status and tokens
    ok, updated = api.verify_session_live(acct)
    if ok and updated:
        return updated
    return acct


def send_account_json(chat_id, acct, bot):
    """Sends individual account JSON file document cleanly to Telegram."""
    try:
        mobile = str(acct.get("mobile") or acct.get("phoneNumber") or "account")
        clean_mob = re.sub(r"[^0-9A-Za-z_-]", "_", mobile)
        json_bytes = json.dumps(acct, indent=2).encode("utf-8")
        filename = f"account_{clean_mob}.json"

        for attempt in range(3):
            try:
                bot.send_document(
                    chat_id,
                    (filename, json_bytes, "application/json"),
                )
                log(f"Sent JSON file for {mobile} to {chat_id}")
                return True
            except Exception as e:
                if "429" in str(e):
                    time.sleep(2)
                else:
                    log(f"send_document error: {e}")
                    time.sleep(0.5)
    except Exception as e:
        log(f"Failed to send account json: {e}")
    return False


def send_batch_zip(chat_id, accounts_list, bot, batch_title="10-Pack"):
    """Packages a list of accounts into an in-memory .zip and sends to Telegram."""
    if not accounts_list:
        return False

    cleaned_list = [ensure_account_fields(dict(a)) for a in accounts_list]
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    zip_filename = f"swiggy_accounts_{len(cleaned_list)}_pack_{timestamp}.zip"

    try:
        zip_buf = io.BytesIO()
        with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("accounts_summary.json", json.dumps(cleaned_list, indent=2))
            for i, acct in enumerate(cleaned_list):
                mob = str(acct.get("mobile") or acct.get("phoneNumber") or f"account_{i+1}")
                clean_mob = re.sub(r"[^0-9A-Za-z_-]", "_", mob)
                zf.writestr(f"account_{clean_mob}.json", json.dumps(acct, indent=2))

        zip_bytes = zip_buf.getvalue()

        for attempt in range(3):
            try:
                bot.send_document(
                    chat_id,
                    (zip_filename, zip_bytes, "application/zip"),
                )
                log(f"Sent batch zip ({len(cleaned_list)} accounts) to chat {chat_id}")
                return True
            except Exception as e:
                if "429" in str(e):
                    time.sleep(2)
                else:
                    log(f"send_batch_zip error: {e}")
                    time.sleep(1)
    except Exception as e:
        log(f"Failed to create/send batch zip: {e}")
    return False


def create_accounts(chat_id, count, bot):
    ss.reset_cancel()
    RUNNING["active"] = True
    RUNNING["done"] = 0
    RUNNING["total"] = count
    RUNNING["cancel"] = False

    last_chat_log_time = [0.0]
    def chat_logger(msg):
        if not msg:
            return
        m_str = str(msg).strip()
        # Only notify important milestones to avoid flooding Telegram chat and triggering 429
        key_words = ["Starting", "Rented", "OTP", "Registered", "Refund", "Created", "Checking", "Account", "cancel"]
        if not any(k.lower() in m_str.lower() for k in key_words):
            return
        now = time.time()
        if now - last_chat_log_time[0] < 1.5:
            return
        last_chat_log_time[0] = now
        try:
            bot.send_message(chat_id, m_str)
        except Exception:
            pass

    api.LOG_HOOK = chat_logger
    ss.LOG_HOOK = chat_logger

    created = 0
    newly_created_accounts = []
    cfg = ss.load_config(ss.CONFIG_PATH)

    try:
        workers = min(count, 100)
        bot.send_message(chat_id, f"🚀 Starting parallel creation of {count} Swiggy account(s) ({workers} concurrent workers)...")
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(api.create_api_account, cfg) for _ in range(count)]
            for f in as_completed(futs):
                if ss.is_cancelled() or RUNNING["cancel"]:
                    break
                try:
                    acct = f.result()
                except Exception as e:
                    log("Parallel account creation error: %s" % e)
                    acct = None

                if acct:
                    acct = ensure_account_fields(acct)
                    try:
                        ok, live_acct = api.verify_session_live(acct)
                        if ok and live_acct:
                            acct = live_acct
                    except Exception:
                        pass

                    created += 1
                    newly_created_accounts.append(acct)

                    # Send verified account JSON directly to chat
                    send_account_json(chat_id, acct, bot)
                    bot.send_message(chat_id, f"✅ Account {created}/{count} Ready: {acct.get('mobile')}")

                    # Send ZIP batch every 10 accounts if requested in larger runs
                    if len(newly_created_accounts) % 10 == 0:
                        batch_slice = newly_created_accounts[-10:]
                        send_batch_zip(chat_id, batch_slice, bot, batch_title="10-Pack")

        RUNNING["done"] = count
        bot.send_message(chat_id, f"🎉 Done! Created {created}/{count} account(s) successfully.")
        if len(newly_created_accounts) >= 2:
            send_batch_zip(chat_id, newly_created_accounts, bot, batch_title="All Created Accounts")

    finally:
        api.LOG_HOOK = None
        ss.LOG_HOOK = None
        RUNNING["active"] = False
    return created


def start_render_health_server():
    """Runs a lightweight HTTP health-check server if PORT is provided by Render."""
    port_str = os.environ.get("PORT")
    if not port_str:
        return
    try:
        from http.server import HTTPServer, BaseHTTPRequestHandler

        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"Swiggy Telegram Bot is Live & Running 24/7!")

            def log_message(self, format, *args):
                pass

        port = int(port_str)
        server = HTTPServer(("0.0.0.0", port), HealthHandler)
        log(f"Started Render Web Healthcheck server on port {port}")
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except Exception as e:
        log(f"Render health server notice: {e}")


def register_handlers(bot, cfg):
    """Registers all Telegram command and message handlers."""

    @bot.message_handler(commands=["start", "help"])
    def cmd_help(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        bot.reply_to(
            m,
            "Swiggy Account Bot\n\n"
            "/create N - create N accounts (1..100)\n"
            "/cancelall - 🚫 cancel all active numbers & instant refund\n"
            "/cancel - stop current run\n"
            "/status - view run status & active rentals\n"
            "/balance - check OTP provider balance\n"
            "/providers - list supported OTP APIs\n"
            "/setprovider <name> [key] - switch OTP provider\n"
            "/setkey <key> - set API key for active provider\n"
            "/otpconfig - view OTP configuration\n"
            "/proxy - check proxy status & live IP\n"
            "/proxy on|off - enable or disable proxy\n"
            "/setproxy <url> - configure new proxy URL\n"
            "/search <number/ID> - search account JSON\n"
            "/zip [N] - download ZIP of latest N accounts\n"
            "/check - check all accounts and get ZIP of active ones\n"
            "/clean - remove expired accounts from database\n"
            "/accounts - list accounts and get JSON\n"
            "/account N - get JSON for account N\n\n"
            "💡 Tip: Send any mobile number directly in chat to get its JSON!",
            reply_markup=get_control_keyboard(),
        )

    @bot.message_handler(commands=["create"])
    def cmd_create(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split()
        n = 1
        if len(parts) > 1:
            try:
                n = int(parts[1])
            except Exception:
                n = 1
        if n < 1 or n > 100:
            bot.reply_to(m, "Count must be between 1 and 100.")
            return
        with RUN_LOCK:
            if RUNNING["active"]:
                bot.reply_to(m, "A run is already active. Send /cancel to stop.")
                return
            t = threading.Thread(target=create_accounts, args=(m.chat.id, n, bot), daemon=True)
            t.start()

    @bot.message_handler(commands=["zip", "download"])
    def cmd_zip(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split()
        n = 10
        if len(parts) > 1:
            try:
                n = int(parts[1])
            except Exception:
                n = 10

        accs = ss.load_accounts()
        if not accs:
            bot.reply_to(m, "No accounts saved yet.")
            return

        target_accs = accs[-n:]
        send_batch_zip(m.chat.id, target_accs, bot, batch_title=f"Latest {len(target_accs)} Accounts")

    @bot.message_handler(commands=["check", "clean"])
    def cmd_check(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        is_clean_mode = "/clean" in (m.text or "")
        accs = ss.load_accounts()
        if not accs:
            bot.reply_to(m, "No accounts found to check.")
            return

        live_accs = []
        expired_accs = []

        for a in accs:
            a = ensure_account_fields(a)
            is_live, updated_a = api.verify_session_live(a)
            if is_live:
                live_accs.append(updated_a)
            else:
                expired_accs.append(a)

        if is_clean_mode:
            with ss.SAVE_LOCK:
                with open(ss.ACCOUNTS_PATH, "w", encoding="utf-8") as fh:
                    json.dump(live_accs, fh, indent=2)

        if live_accs:
            title = f"Verified Active Accounts ({len(live_accs)})" if not is_clean_mode else f"Cleaned Database - Active Accounts ({len(live_accs)})"
            send_batch_zip(m.chat.id, live_accs, bot, batch_title=title)

    @bot.message_handler(commands=["accounts"])
    def cmd_accounts(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        accs = ss.load_accounts()
        if not accs:
            bot.reply_to(m, "No accounts saved yet.")
            return
        markup = telebot.types.InlineKeyboardMarkup(row_width=2)
        btns = []
        for i, a in enumerate(accs[:50]):
            a = ensure_account_fields(a)
            label = f"{i + 1}. {a.get('mobile', '?')}"
            btns.append(telebot.types.InlineKeyboardButton(label, callback_data=f"acct:{i}"))
        markup.add(*btns)
        bot.reply_to(m, f"Accounts ({len(accs)}): tap to get JSON", reply_markup=markup)

    @bot.callback_query_handler(func=lambda c: c.data and c.data.startswith("acct:"))
    def cb_account(c):
        if not is_authorized(c.message.chat.id, cfg, bot, c.message):
            return
        try:
            idx = int(c.data.split(":")[1])
            accs = ss.load_accounts()
            a = ensure_account_fields(accs[idx])
            send_account_json(c.message.chat.id, a, bot)
            bot.answer_callback_query(c.id, "Sent")
        except Exception as e:
            bot.answer_callback_query(c.id, f"Error: {e}")

    @bot.message_handler(commands=["account"])
    def cmd_account(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split()
        accs = ss.load_accounts()
        if not accs:
            bot.reply_to(m, "No accounts found.")
            return
        if len(parts) < 2:
            bot.reply_to(m, "Usage: /account N")
            return
        try:
            idx = int(parts[1]) - 1
            a = ensure_account_fields(accs[idx])
            send_account_json(m.chat.id, a, bot)
        except Exception:
            bot.reply_to(m, "Invalid index.")

    @bot.message_handler(commands=["search", "find"])
    def cmd_search(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(m, "Usage: /search <phone_or_customer_id>")
            return
        query = parts[1].strip()
        accs = ss.load_accounts()
        target = find_account_by_query(query, accs)
        if target:
            target = ensure_account_fields(target)
            send_account_json(m.chat.id, target, bot)
        else:
            bot.reply_to(m, f"🔍 No saved account found matching '{query}'.")

    @bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
    def handle_text_search(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        text = (m.text or "").strip()
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 10:
            accs = ss.load_accounts()
            target = find_account_by_query(text, accs)
            if target:
                target = ensure_account_fields(target)
                send_account_json(m.chat.id, target, bot)
            else:
                bot.reply_to(m, f"🔍 No saved account found for {text}.")

    @bot.message_handler(commands=["balance"])
    def cmd_balance(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        try:
            parts = (m.text or "").split()
            target_prov = parts[1].lower().strip() if len(parts) > 1 else None

            cfg_data = ss.load_config(ss.CONFIG_PATH)
            op = cfg_data.get("otp_provider") or {}
            presets = cfg_data.get("otp_presets") or {}

            if target_prov:
                matched = presets.get(target_prov) or {"type": target_prov}
                ptype = target_prov.upper()
                bal = ss.get_provider_balance(matched)
                safe_reply(bot, m, f"💳 *{ptype} Live Balance:*\n`{bal}`", parse_mode="Markdown")
                return

            active_type = str(op.get("type", "unknown")).upper()
            active_bal = ss.get_provider_balance(op)

            lines = [f"💳 *Active OTP Provider:* `{active_type}`\n💰 *Live Balance:* `{active_bal}`\n"]
            lines.append("📊 *Configured Presets:*")
            for name, p_info in presets.items():
                k = str(p_info.get("api_key", ""))
                if k and not k.startswith("YOUR_") and len(k) > 5:
                    p_bal = ss.get_provider_balance(p_info)
                    marker = " 🟢 (ACTIVE)" if name.lower() == op.get("type", "").lower() else ""
                    lines.append(f"• *{name.upper()}*{marker}: `{p_bal}`")

            lines.append("\n💡 Use `/balance <name>` to check any specific provider or `/setprovider <name>` to switch.")
            safe_reply(bot, m, "\n".join(lines), parse_mode="Markdown")
        except Exception as e:
            safe_reply(bot, m, f"⚠️ Balance check note: {e}")

    @bot.message_handler(commands=["providers", "otpproviders"])
    def cmd_providers(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        cfg_data = ss.load_config(ss.CONFIG_PATH)
        op = cfg_data.get("otp_provider") or {}
        current_type = str(op.get("type", "nexnum")).lower()

        text = (
            "📱 *Supported OTP Providers:*\n\n"
            "1. `uotp` — UOTP.store (Service: swiggy, Operator: 3)\n"
            "2. `nexnum` — Nexnum ($0.08 / provider 4591)\n"
            "3. `grizzly` — GrizzlySMS (Service: hp)\n"
            "4. `smsactivate` — SMS-Activate (Service: cw)\n"
            "5. `tiger` — TigerSMS (Service: jx)\n"
            "6. `smshub` — SMSHub (Service: cw)\n"
            "7. `5sim` — 5SIM.net (Service: swiggy)\n"
            "8. `daisysms` — DaisySMS (Service: swiggy)\n"
            "9. `generic` — Any custom SMS-Activate API\n\n"
            f"🔹 *Active Provider:* `{current_type.upper()}`\n\n"
            "🔧 *Commands:*\n"
            "• `/setprovider <name>` — switch to preset\n"
            "• `/setprovider <name> <api_key>` — set provider & key\n"
            "• `/setkey <api_key>` — set key for current provider\n"
            "• `/otpconfig` — view full OTP config\n"
            "• `/balance` — check live balance"
        )
        safe_reply(bot, m, text, parse_mode="Markdown")

    @bot.message_handler(commands=["setprovider"])
    def cmd_setprovider(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split()
        if len(parts) < 2:
            safe_reply(bot, m, "Usage: `/setprovider <name> [api_key]`\nExample: `/setprovider uotp` or `/setprovider grizzly 804def...`", parse_mode="Markdown")
            return
        name = parts[1].lower().strip()
        new_key = parts[2].strip() if len(parts) > 2 else ""

        cfg_data = ss.load_config(ss.CONFIG_PATH)
        presets = cfg_data.get("otp_presets") or {}

        valid_names = ["uotp", "nexnum", "grizzly", "grizzlysms", "tiger", "tigersms", "smsactivate", "smshub", "5sim", "daisysms", "generic"]
        if name not in presets and name not in valid_names:
            safe_reply(bot, m, f"❌ Unknown provider '{name}'. Send `/providers` to view available options.", parse_mode="Markdown")
            return

        matched_preset = presets.get(name) or {}
        if not matched_preset:
            for k, v in presets.items():
                if name in k or k in name:
                    matched_preset = dict(v)
                    break

        op = cfg_data.get("otp_provider") or {}
        if matched_preset:
            op.update(matched_preset)
        else:
            op["type"] = name

        if new_key:
            op["api_key"] = new_key
            if name in presets:
                presets[name]["api_key"] = new_key

        op["enabled"] = True
        cfg_data["otp_provider"] = op
        cfg_data["otp_presets"] = presets

        with open(ss.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg_data, fh, indent=2)

        safe_reply(bot, m, f"🔄 Active OTP provider switched to *{op.get('type', name).upper()}*! Checking balance...", parse_mode="Markdown")
        bal = ss.get_provider_balance(op)
        safe_send_message(bot, m.chat.id, f"✅ Provider Configured: *{op.get('type').upper()}*\n💳 Live Balance: `{bal}`", parse_mode="Markdown")

    @bot.message_handler(commands=["setkey", "setotpkey"])
    def cmd_setkey(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            safe_reply(bot, m, "Usage: `/setkey <api_key>`", parse_mode="Markdown")
            return
        key = parts[1].strip()
        cfg_data = ss.load_config(ss.CONFIG_PATH)
        op = cfg_data.get("otp_provider") or {}
        op["api_key"] = key
        ptype = str(op.get("type", "nexnum")).lower()

        presets = cfg_data.get("otp_presets") or {}
        if ptype in presets:
            presets[ptype]["api_key"] = key

        cfg_data["otp_provider"] = op
        cfg_data["otp_presets"] = presets

        with open(ss.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg_data, fh, indent=2)

        safe_reply(bot, m, f"🔑 API Key updated for *{ptype.upper()}*! Testing balance...", parse_mode="Markdown")
        bal = ss.get_provider_balance(op)
        safe_send_message(bot, m.chat.id, f"💳 Live Balance: `{bal}`", parse_mode="Markdown")

    @bot.message_handler(commands=["setprice", "price", "maxprice"])
    def cmd_setprice(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            cfg_data = ss.load_config(ss.CONFIG_PATH)
            op = cfg_data.get("otp_provider") or {}
            cur_price = op.get("max_price", "N/A")
            safe_reply(bot, m, f"💲 *Current Max Price:* `${cur_price}`\n\nUsage: `/setprice <amount>` (e.g. `/setprice 0.07`)", parse_mode="Markdown")
            return
        try:
            val = float(parts[1].strip().replace("$", ""))
            cfg_data = ss.load_config(ss.CONFIG_PATH)
            op = cfg_data.get("otp_provider") or {}
            op["max_price"] = val
            op["price_usd"] = val
            presets = cfg_data.get("otp_presets") or {}
            ptype = str(op.get("type", "nexnum")).lower()
            if ptype in presets:
                presets[ptype]["max_price"] = val
                presets[ptype]["price_usd"] = val
            cfg_data["otp_provider"] = op
            cfg_data["otp_presets"] = presets
            with open(ss.CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg_data, fh, indent=2)
            safe_reply(bot, m, f"✅ *Max number buy price updated to:* `${val}`", parse_mode="Markdown")
        except Exception as e:
            safe_reply(bot, m, f"❌ Invalid price format: {e}", parse_mode="Markdown")

    @bot.message_handler(commands=["otpconfig"])
    def cmd_otpconfig(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        cfg_data = ss.load_config(ss.CONFIG_PATH)
        op = cfg_data.get("otp_provider") or {}
        ptype = str(op.get("type", "unknown")).upper()
        key = str(op.get("api_key", ""))
        masked_key = (key[:6] + "..." + key[-4:]) if len(key) > 10 else (key[:2] + "****" if key else "None")
        service = op.get("service", "default")
        base = op.get("base_url", "default")
        bal = ss.get_provider_balance(op)

        text = (
            f"⚙️ *Current OTP Configuration:*\n\n"
            f"• *Provider:* `{ptype}`\n"
            f"• *API Key:* `{masked_key}`\n"
            f"• *Service Code:* `{service}`\n"
            f"• *Country Code:* `{op.get('country', 22)}`\n"
            f"• *Endpoint:* `{base}`\n"
            f"• *Live Balance:* `{bal}`\n\n"
            "💡 Use `/providers` to see all supported services or `/setprovider <name>` to switch."
        )
        safe_reply(bot, m, text, parse_mode="Markdown")

    @bot.message_handler(commands=["cancelall", "refundall", "cancel_all", "refund_all"])
    def cmd_cancelall(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        RUNNING["cancel"] = True
        RUNNING["active"] = False
        ss.trigger_cancel()

        safe_reply(bot, m, "⏳ *Cancelling all active numbers & requesting refunds...*", parse_mode="Markdown")
        res = ss.cancel_all_active_numbers()

        total = res.get("total", 0)
        cancelled = res.get("cancelled", 0)
        bal = res.get("balance", "N/A")
        orders = res.get("orders", [])

        if total == 0:
            text = (
                f"✅ *No Active Numbers Found!*\n\n"
                f"All previous numbers are already completed or refunded.\n"
                f"💳 *Live Balance:* `{bal}`"
            )
        else:
            lines = [
                f"🚫 *1-Click Cancel Completed!*",
                f"",
                f"• *Active Numbers Processed:* `{total}`",
                f"• *Refunded / Queued:* `{cancelled}`",
                f"• *Updated Balance:* `{bal}`",
                f"",
                f"📋 *Order Breakdown:*",
            ]
            for o in orders[:10]:
                oid = o.get("order_id", "?")
                ph = o.get("phone", "?")
                st = o.get("status", "")
                if st == "queued_refund":
                    lines.append(f"• `+91 {ph}` (`{oid}`): ⏳ 2-min refund queued ({o.get('wait_sec')}s)")
                else:
                    lines.append(f"• `+91 {ph}` (`{oid}`): ✅ Cancelled & Refunded")
            if len(orders) > 10:
                lines.append(f"• ... and {len(orders)-10} more orders")
            text = "\n".join(lines)

        safe_reply(bot, m, text, parse_mode="Markdown", reply_markup=get_control_keyboard())

    @bot.callback_query_handler(func=lambda c: c.data == "cancel_all_rentals")
    def cb_cancel_all_rentals(c):
        if not is_authorized(c.message.chat.id, cfg, bot, c.message):
            return
        try:
            bot.answer_callback_query(c.id, "Cancelling all active numbers...")
        except Exception:
            pass
        RUNNING["cancel"] = True
        RUNNING["active"] = False
        ss.trigger_cancel()

        res = ss.cancel_all_active_numbers()
        total = res.get("total", 0)
        cancelled = res.get("cancelled", 0)
        bal = res.get("balance", "N/A")
        orders = res.get("orders", [])

        if total == 0:
            text = (
                f"✅ *No Active Numbers Found!*\n\n"
                f"All previous numbers are already completed or refunded.\n"
                f"💳 *Live Balance:* `{bal}`"
            )
        else:
            lines = [
                f"🚫 *1-Click Cancel Completed!*",
                f"",
                f"• *Active Numbers Processed:* `{total}`",
                f"• *Refunded / Queued:* `{cancelled}`",
                f"• *Updated Balance:* `{bal}`",
            ]
            for o in orders[:6]:
                oid = o.get("order_id", "?")
                ph = o.get("phone", "?")
                lines.append(f"• `+91 {ph}`: {o.get('status', 'refunded')}")
            text = "\n".join(lines)

        safe_send_message(bot, c.message.chat.id, text, parse_mode="Markdown", reply_markup=get_control_keyboard())

    @bot.callback_query_handler(func=lambda c: c.data == "check_balance")
    def cb_check_balance(c):
        if not is_authorized(c.message.chat.id, cfg, bot, c.message):
            return
        try:
            bot.answer_callback_query(c.id, "Checking live balance...")
        except Exception:
            pass
        cfg_data = ss.load_config(ss.CONFIG_PATH)
        op = cfg_data.get("otp_provider") or {}
        active_type = str(op.get("type", "unknown")).upper()
        bal = ss.get_provider_balance(op)
        safe_send_message(
            bot,
            c.message.chat.id,
            f"💳 *{active_type} Live Balance:* `{bal}`",
            parse_mode="Markdown",
            reply_markup=get_control_keyboard(),
        )

    @bot.callback_query_handler(func=lambda c: c.data == "create_single")
    def cb_create_single(c):
        if not is_authorized(c.message.chat.id, cfg, bot, c.message):
            return
        try:
            bot.answer_callback_query(c.id, "Starting account creation...")
        except Exception:
            pass
        with RUN_LOCK:
            if RUNNING["active"]:
                safe_send_message(bot, c.message.chat.id, "A run is already active. Send /cancel or tap Cancel All Numbers to stop.", reply_markup=get_control_keyboard())
                return
            t = threading.Thread(target=create_accounts, args=(c.message.chat.id, 1, bot), daemon=True)
            t.start()

    @bot.message_handler(commands=["cancel"])
    def cmd_cancel(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) > 1 and parts[1].strip():
            target_id = parts[1].strip()
            cfg_data = ss.load_config(ss.CONFIG_PATH)
            prov = ss.make_provider(cfg_data.get("otp_provider"))
            safe_reply(bot, m, f"⏳ *Attempting cancellation for order / number:* `{target_id}`...", parse_mode="Markdown")
            
            # Check active orders first for phone mapping
            active_orders = ss.load_active_orders()
            matched_oid = target_id
            for oid, oinfo in active_orders.items():
                if target_id in oid or target_id in str(oinfo.get("phone", "")):
                    matched_oid = oid
                    break
            
            try:
                res = prov.set_status(matched_oid, 8) if hasattr(prov, "set_status") else prov._call("setStatus", id=matched_oid, status=-1)
                res_str = str(res.get("_raw", "") if isinstance(res, dict) else res).strip()
                ss.unregister_active_order(matched_oid)
                bal = ss.get_provider_balance(cfg_data.get("otp_provider"))
                safe_reply(
                    bot,
                    m,
                    f"🚫 *Cancellation Result for `{matched_oid}`:*\n`{res_str or res}`\n\n💳 *Live Balance:* `{bal}`",
                    parse_mode="Markdown",
                    reply_markup=get_control_keyboard(),
                )
            except Exception as e:
                safe_reply(bot, m, f"⚠️ Failed to cancel `{matched_oid}`: {e}", parse_mode="Markdown")
            return

        RUNNING["cancel"] = True
        RUNNING["active"] = False
        ss.trigger_cancel()
        active_orders = ss.get_active_orders_list()
        count = len(active_orders)
        if count > 0:
            safe_reply(
                bot,
                m,
                f"🛑 *Run Cancelled!*\n\n⚠️ You have *{count} active rented number(s)* waiting.\nTap the button below to cancel them all and get refunds immediately.",
                parse_mode="Markdown",
                reply_markup=get_control_keyboard(),
            )
        else:
            safe_reply(bot, m, "🛑 *Run Cancelled.* No active numbers running in current session.\n\n💡 Tip: To cancel a specific order ID directly, use: `/cancel <order_id>`", parse_mode="Markdown", reply_markup=get_control_keyboard())

    @bot.message_handler(commands=["status"])
    def cmd_status(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        active_orders = ss.get_active_orders_list()
        count = len(active_orders)
        cfg_data = ss.load_config(ss.CONFIG_PATH)
        op = cfg_data.get("otp_provider") or {}
        bal = ss.get_provider_balance(op)
        text = (
            f"📊 *Bot Status Report:*\n\n"
            f"• *Active Run:* `{RUNNING['active']}`\n"
            f"• *Progress:* `{RUNNING['done']}/{RUNNING['total']}`\n"
            f"• *Cancelled Flag:* `{RUNNING['cancel']}`\n"
            f"• *Active Rented Numbers:* `{count}`\n"
            f"• *Live Balance:* `{bal}`"
        )
        safe_reply(bot, m, text, parse_mode="Markdown", reply_markup=get_control_keyboard())

    @bot.message_handler(commands=["proxy"])
    def cmd_proxy(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split()
        sub = parts[1].lower() if len(parts) > 1 else ""

        cfg_data = ss.load_config(ss.CONFIG_PATH)
        p_cfg = cfg_data.get("proxy") or {}

        if sub == "on":
            p_cfg["enabled"] = True
            cfg_data["proxy"] = p_cfg
            with open(ss.CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg_data, fh, indent=2)
            bot.reply_to(m, "⚡ Proxy Enabled. Testing connection...")
            ip_info = api.get_current_ip(p_cfg.get("url"))
            ip = ip_info.get("ip") or ip_info.get("query") or "unknown"
            country = ip_info.get("country") or ip_info.get("countryCode") or ""
            bot.send_message(m.chat.id, f"🌐 Active IP: `{ip}` ({country})\nProxy Status: LIVE", parse_mode="Markdown")
            return

        elif sub == "off":
            p_cfg["enabled"] = False
            cfg_data["proxy"] = p_cfg
            with open(ss.CONFIG_PATH, "w", encoding="utf-8") as fh:
                json.dump(cfg_data, fh, indent=2)
            bot.reply_to(m, "🔌 Proxy Disabled. Traffic now routing directly without proxy.")
            return

        # Default /proxy status & test
        enabled = p_cfg.get("enabled", False)
        url = p_cfg.get("url", "None")
        masked_url = re.sub(r":([^:@]+)@", r":****@", url) if "@" in url else url

        status_text = "ENABLED" if enabled else "DISABLED"
        msg_out = f"🛡️ Proxy Status: *{status_text}*\nURL: `{masked_url}`\n\nTesting proxy route..."
        bot.reply_to(m, msg_out, parse_mode="Markdown")

        if enabled and url:
            ip_info = api.get_current_ip(url)
            ip = ip_info.get("ip") or ip_info.get("query") or "unknown"
            country = ip_info.get("country") or ip_info.get("countryCode") or ""
            city = ip_info.get("city") or ""
            bot.send_message(m.chat.id, f"🌍 Routed Public IP: `{ip}`\n📍 Location: {city}, {country}\n✅ Proxy Test: SUCCESSFUL", parse_mode="Markdown")
        else:
            bot.send_message(m.chat.id, "Proxy is disabled. Use `/proxy on` or `/setproxy <url>` to activate.", parse_mode="Markdown")

    @bot.message_handler(commands=["setproxy"])
    def cmd_setproxy(m):
        if not is_authorized(m.chat.id, cfg, bot, m):
            return
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2:
            bot.reply_to(m, "Usage: `/setproxy http://user:pass@host:port`", parse_mode="Markdown")
            return
        new_url = parts[1].strip()
        if not (new_url.startswith("http://") or new_url.startswith("https://")):
            new_url = "http://" + new_url

        cfg_data = ss.load_config(ss.CONFIG_PATH)
        cfg_data["proxy"] = {"enabled": True, "url": new_url}
        with open(ss.CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(cfg_data, fh, indent=2)

        bot.reply_to(m, f"🔄 Proxy updated! Testing `{new_url[:30]}...`", parse_mode="Markdown")
        ip_info = api.get_current_ip(new_url)
        ip = ip_info.get("ip") or ip_info.get("query") or "unknown"
        country = ip_info.get("country") or ip_info.get("countryCode") or ""
        city = ip_info.get("city") or ""
        bot.send_message(m.chat.id, f"✅ Proxy is LIVE!\n🌍 Routed IP: `{ip}`\n📍 Location: {city}, {country}", parse_mode="Markdown")


def start_bot():
    if telebot is None:
        print("telebot not installed: pip install pyTelegramBotAPI")
        sys.exit(1)

    cfg = load_bot_config()
    token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("bot_token", "")
    if not token or ":" not in token:
        print("Bot token not configured in %s!" % BOT_CONFIG)
        sys.exit(1)

    bot = telebot.TeleBot(token)
    register_handlers(bot, cfg)

    # Clean any old webhook before starting local polling
    try:
        bot.remove_webhook()
    except Exception:
        pass

    # Start health check server if on Render/Cloud container
    start_render_health_server()

    log("Swiggy Bot polling cleanly (JSON delivery only, max 100)...")
    while True:
        try:
            bot.infinity_polling(timeout=20, long_polling_timeout=20, skip_pending=False)
        except Exception as e:
            log(f"Polling error: {e}. Reconnecting in 3 seconds...")
            time.sleep(3)


if __name__ == "__main__":
    start_bot()