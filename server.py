import os
import asyncio
import threading
import time
import logging
from datetime import datetime
import scratchattach as sa
from flask import Flask, jsonify
import aiohttp

# ----------------------------
# Logging setup
# ----------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# ----------------------------
# Flask setup
# ----------------------------
app = Flask(__name__)

@app.route("/")
def index():
    return "Scratch Cloud Proxy is running.", 200

# ----------------------------
# Proxy Service
# ----------------------------
class ProxyService:
    def __init__(self, project_id, username=None, password=None):
        self.PROJECT_ID = project_id
        self.username = username
        self.password = password
        self.cloud = None
        self.events = None
        self.loop = None
        self.thread = None
        self.running = False
        self.updating = False
        self.MAX_CLOUD_LENGTH = 100_000
        self.REQUEST_ID_LEN = 9

    # ------------------------
    # ASCII encoding helpers
    # ------------------------
    def digits_to_ascii(self, digits):
        try:
            return ''.join(chr(int(digits[i:i+5])) for i in range(0, len(digits), 5))
        except Exception as e:
            logger.error(f"ASCII decode error: {e}")
            return ""

    def ascii_to_digits(self, text):
        return ''.join(f"{ord(c):05}" for c in text)

    # ------------------------
    # HTTP fetch
    # ------------------------
    async def fetch_from_web(self, method, url, body=None):
        start_time = time.time()
        async with aiohttp.ClientSession() as session:
            try:
                if method.upper() == "GET":
                    async with session.get(url) as resp:
                        response = await resp.text()
                elif method.upper() == "POST":
                    async with session.post(url, data=body) as resp:
                        response = await resp.text()
                else:
                    response = f"Invalid method: {method}"
                duration_ms = int((time.time() - start_time) * 1000)
                return response, duration_ms
            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                logger.error(f"HTTP request error: {method} {url}: {str(e)}")
                return f"Error: {str(e)}", duration_ms

    # ------------------------
    # Cloud variable event
    # ------------------------
    def on_set(self, event):
        if event.var != "CLOUD1" or self.updating:
            return

        raw = str(event.value)
        if not raw.startswith("9") or len(raw) < 20:
            return

        request_id = raw[1:10]
        try:
            header_len = int(raw[10:15])
            body_len = int(raw[15:20])
        except ValueError:
            logger.error(f"Parse error for request {request_id}")
            return

        total_needed_len = 20 + header_len + body_len
        if len(raw) < total_needed_len:
            logger.error(f"Incomplete message for request {request_id}")
            return

        header_digits = raw[20:20 + header_len]
        body_digits = raw[20 + header_len:20 + header_len + body_len]

        header = self.digits_to_ascii(header_digits)
        body = self.digits_to_ascii(body_digits)

        try:
            method, url = header.strip().split(" ", 1)
        except ValueError:
            logger.error(f"Header format error for request {request_id}")
            return

        # Async request
        asyncio.run_coroutine_threadsafe(
            self.handle_request(request_id, method, url, body),
            self.loop
        )

    # ------------------------
    # Handle request
    # ------------------------
    async def handle_request(self, request_id, method, url, body):
        response_text, duration_ms = await self.fetch_from_web(method, url, body)

        max_response_chars = (self.MAX_CLOUD_LENGTH - self.REQUEST_ID_LEN) // 5
        response_text = response_text[:max_response_chars]
        result = request_id + self.ascii_to_digits(response_text)

        self.updating = True
        try:
            self.cloud.set_var("CLOUD1", result[:self.MAX_CLOUD_LENGTH])
            logger.info(f"Response sent for request {request_id} ({len(result)} chars)")
        except Exception as e:
            logger.error(f"Cloud variable error for request {request_id}: {str(e)}")
        finally:
            await asyncio.sleep(0.5)
            self.updating = False

    # ------------------------
    # Event loop thread
    # ------------------------
    def run_event_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        try:
            self.cloud = sa.TwCloud(project_id=self.PROJECT_ID)
            self.events = self.cloud.events()
            self.events.event(self.on_set)
            self.events.start()
            logger.info(f"Connected to Scratch project {self.PROJECT_ID}")
            self.loop.run_forever()
        except Exception as e:
            logger.error(f"Failed to start event loop: {str(e)}")
        finally:
            self.loop.close()

    # ------------------------
    # Start/stop service
    # ------------------------
    def start(self):
        if self.running:
            return
        self.running = True
        self.thread = threading.Thread(target=self.run_event_loop, daemon=True)
        self.thread.start()
        logger.info("Proxy service started successfully")

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self.events:
            self.events.stop()
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        logger.info("Proxy service stopped")

# ----------------------------
# Initialize proxy using env variables
# ----------------------------
PROJECT_ID = os.getenv("SCRATCH_PROJECT_ID", "1197011359")
proxy_service = ProxyService(PROJECT_ID)
proxy_service.start()

# ----------------------------
# Run Flask (Render compatible)
# ----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, threaded=True)
