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
SERVICE_URL = os.environ.get("RENDER_EXTERNAL_URL", "https://swiggy-telegram-bot.onrender.com").rstrip("/")
WEBHOOK_URL = f"{SERVICE_URL}/webhook"

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
        """Health check endpoint for Render and uptime monitoring services."""
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
                update = telebot.types.Update.de_json(update_dict)
                if update:
                    threading.Thread(target=bot.process_new_updates, args=([update],), daemon=True).start()
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
    for attempt in range(1, 10):
        try:
            current_info = bot.get_webhook_info()
            if current_info.url != WEBHOOK_URL:
                logger.info(f"Setting Telegram Webhook to: {WEBHOOK_URL}")
                try:
                    bot.remove_webhook()
                except Exception:
                    pass
                time.sleep(1)
                success = bot.set_webhook(url=WEBHOOK_URL, max_connections=40, drop_pending_updates=False)
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
    """Pings the public web service every 2 minutes to keep Render container awake."""
    time.sleep(20)
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
        time.sleep(120)  # Ping every 2 minutes


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
