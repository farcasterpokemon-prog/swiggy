"""
Swiggy Bot Cloud Runner for Render.com (Telegram Webhook Mode + 24/7 Always Awake)
"""
import http.server
import json
import logging
import os
import socketserver
import sys
import threading
import time
import urllib.request
import telebot

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

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("SwiggyWebhookRunner")

PORT = int(os.environ.get("PORT", 10000))
SERVICE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://swiggy-telegram-bot-9bya.onrender.com").rstrip("/")
WEBHOOK_URL = f"{SERVICE_URL}/webhook"
RECENT_UPDATES = []

import swiggy_bot

cfg = swiggy_bot.load_bot_config()
token = os.environ.get("TELEGRAM_BOT_TOKEN") or cfg.get("bot_token", "")
if not token or ":" not in token:
    logger.error("No valid TELEGRAM_BOT_TOKEN configured!")
    sys.exit(1)

bot = telebot.TeleBot(token, threaded=True)

# Register all bot command handlers
swiggy_bot.register_handlers(bot, cfg)


class WebhookServerHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        """Health check and diagnostic endpoint for Render."""
        if self.path.startswith("/check_diag"):
            try:
                import urllib.parse
                qs = urllib.parse.urlparse(self.path).query
                params = urllib.parse.parse_qs(qs)
                phone = params.get("phone", ["9876543210"])[0]

                import swiggy_signup as ss
                import swiggy_api_signup as api

                diag_result = {
                    "phone": phone,
                    "proxy_url": ss.get_proxy_url(),
                }

                # Primary Production Checker Flow
                t_chk = time.time()
                reg, resp = ss.check_swiggy_registered(phone)
                diag_result["check_swiggy_registered"] = {
                    "registered": reg,
                    "resp": resp,
                    "latency_sec": round(time.time() - t_chk, 2)
                }

                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(diag_result, indent=2).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        if self.path.startswith("/bot_diag"):
            try:
                import swiggy_bot
                import swiggy_signup as ss
                mem_info = {}
                try:
                    for p in ["/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory/memory.usage_in_bytes"]:
                        if os.path.exists(p):
                            with open(p) as f:
                                mem_info["cgroup_usage_mb"] = round(int(f.read().strip()) / (1024 * 1024), 2)
                            break
                    for p in ["/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"]:
                        if os.path.exists(p):
                            with open(p) as f:
                                val = f.read().strip()
                                if val != "max":
                                    mem_info["cgroup_limit_mb"] = round(int(val) / (1024 * 1024), 2)
                            break
                    if os.path.exists("/proc/meminfo"):
                        with open("/proc/meminfo") as f:
                            for line in f:
                                parts = line.split(":")
                                k = parts[0].strip()
                                if k in ["MemTotal", "MemFree", "MemAvailable"]:
                                    v = int(parts[1].strip().split()[0])
                                    mem_info[k.lower() + "_mb"] = round(v / 1024, 2)
                    st = os.statvfs("/")
                    mem_info["disk_free_gb"] = round((st.f_bavail * st.f_frsize) / (1024 ** 3), 2)
                    mem_info["disk_total_gb"] = round((st.f_blocks * st.f_frsize) / (1024 ** 3), 2)
                    mem_info["accounts_stored"] = len(ss.load_accounts())
                except Exception as ex:
                    mem_info["error"] = str(ex)

                diag = {
                    "running": swiggy_bot.RUNNING,
                    "active_orders": ss.load_active_orders(),
                    "recent_updates": RECENT_UPDATES,
                    "provider_balance": ss.get_provider_balance(),
                    "memory_and_storage": mem_info,
                }
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(diag, indent=2).encode("utf-8"))
                return
            except Exception as e:
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))
                return

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        response = {
            "status": "ok",
            "service": "Swiggy Telegram Bot",
            "mode": "webhook",
            "webhook_url": WEBHOOK_URL,
            "live": True,
        }
        self.wfile.write(json.dumps(response).encode("utf-8"))

    def do_POST(self):
        """Receives incoming updates from Telegram Webhook."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            post_data = self.rfile.read(content_length)

            # Send 200 OK immediately so Telegram doesn't timeout or retry
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")

            # Process Telegram Update in background thread
            if post_data:
                update_dict = json.loads(post_data.decode("utf-8"))
                RECENT_UPDATES.append({"time": time.time(), "update": update_dict})
                if len(RECENT_UPDATES) > 20:
                    RECENT_UPDATES.pop(0)
                update = telebot.types.Update.de_json(update_dict)
                if update:
                    def _handle(u):
                        try:
                            msg = getattr(u, "message", None)
                            if msg and msg.text:
                                logger.info(f"Incoming command: '{msg.text}' from chat {msg.chat.id}")
                            bot.process_new_updates([u])
                        except Exception as ex:
                            logger.error(f"Error in process_new_updates: {ex}", exc_info=True)
                    threading.Thread(target=_handle, args=(update,), daemon=True).start()
        except Exception as e:
            logger.error(f"Error handling webhook POST: {e}")

    def log_message(self, format, *args):
        # Silence routine request logging
        pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def setup_webhook():
    """Sets or verifies the Telegram Webhook with automatic retries."""
    desired_updates = ["message", "edited_message", "channel_post", "edited_channel_post", "callback_query"]
    for attempt in range(1, 10):
        try:
            current_info = bot.get_webhook_info()
            allowed = current_info.allowed_updates or []
            if current_info.url != WEBHOOK_URL or "callback_query" not in allowed:
                logger.info(f"Setting Telegram Webhook to: {WEBHOOK_URL} with callback_query support")
                try:
                    bot.remove_webhook()
                except Exception:
                    pass
                time.sleep(1)
                success = bot.set_webhook(
                    url=WEBHOOK_URL,
                    max_connections=40,
                    drop_pending_updates=False,
                    allowed_updates=desired_updates,
                )
                if success:
                    logger.info(f"Telegram Webhook successfully set to {WEBHOOK_URL}")
                    return True
            else:
                logger.info(f"Telegram Webhook already active at {WEBHOOK_URL}")
                return True
        except Exception as e:
            logger.warning(f"Webhook setup attempt {attempt} failed: {e}. Retrying in 3s...")
            time.sleep(3)
    return False


def keep_alive_worker():
    """Pings the public web service and enforces Telegram webhook continuously."""
    time.sleep(10)
    desired_updates = ["message", "edited_message", "channel_post", "edited_channel_post", "callback_query"]
    while True:
        try:
            req = urllib.request.Request(
                SERVICE_URL,
                headers={"User-Agent": "RenderKeepAlive/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                logger.info(f"Keep-alive ping sent to {SERVICE_URL} (HTTP {resp.getcode()})")
        except Exception as e:
            logger.debug(f"Keep-alive ping notice: {e}")

        # Webhook Auto-Heal: Check and enforce Telegram webhook on every keepalive cycle
        try:
            current_info = bot.get_webhook_info()
            allowed = current_info.allowed_updates or []
            if current_info.url != WEBHOOK_URL or "callback_query" not in allowed:
                logger.warning(f"Webhook URL or allowed_updates deviated. Auto-restoring to {WEBHOOK_URL}...")
                try:
                    bot.remove_webhook()
                except Exception:
                    pass
                time.sleep(0.5)
                success = bot.set_webhook(
                    url=WEBHOOK_URL,
                    max_connections=40,
                    drop_pending_updates=False,
                    allowed_updates=desired_updates,
                )
                if success:
                    logger.info(f"Telegram Webhook successfully restored to {WEBHOOK_URL}")
        except Exception as e:
            logger.debug(f"Webhook check notice: {e}")

        time.sleep(60)  # Ping and enforce every 60 seconds


if __name__ == "__main__":
    logger.info(f"Starting Swiggy Telegram Webhook Server on port {PORT}...")

    # 1. Start HTTP Webhook Server on main port
    httpd = ThreadedHTTPServer(("0.0.0.0", PORT), WebhookServerHandler)
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    logger.info(f"HTTP Server listening on 0.0.0.0:{PORT}")

    # 2. Register Webhook with Telegram
    t_wh = threading.Thread(target=setup_webhook, daemon=True)
    t_wh.start()

    # 3. Start Keep-Alive Pinger
    t_ka = threading.Thread(target=keep_alive_worker, daemon=True)
    t_ka.start()

    logger.info("Swiggy Bot 24/7 Webhook Engine is fully running!")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        logger.info("Stopping Swiggy Bot...")
        try:
            bot.remove_webhook()
        except Exception:
            pass
        httpd.shutdown()
