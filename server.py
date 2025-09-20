# server.py

import asyncio
import scratchattach as sa
import aiohttp
import threading
import time
import logging
from flask import Flask
from datetime import datetime

# -------------------- Logging Setup --------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger(__name__)

# -------------------- Proxy Service --------------------
class ProxyService:
    def __init__(self, log_request_callback, log_error_callback):
        self.log_request = log_request_callback
        self.log_error = log_error_callback
        self.running = False
        self.loop = None
        self.thread = None

        # Scratch cloud configuration
        self.PROJECT_ID = "1197011359"
        self.cloud = None
        self.events = None

        # Service state
        self.updating = False
        self.MAX_CLOUD_LENGTH = 100_000
        self.REQUEST_ID_LEN = 9

    def digits_to_ascii(self, digits):
        try:
            return ''.join(chr(int(digits[i:i+5])) for i in range(0, len(digits), 5))
        except Exception as e:
            error_msg = f"Failed to decode digits to ASCII: {e}"
            logger.error(error_msg)
            self.log_error("ASCII decode error", error_msg)
            return ""

    def ascii_to_digits(self, text):
        return ''.join(f"{ord(c):05}" for c in text)

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
                error_msg = f"HTTP request failed: {str(e)}"
                logger.error(error_msg)
                self.log_error("HTTP request error", f"{method} {url}: {error_msg}")
                return f"Error: {str(e)}", duration_ms

    def on_set(self, event):
        if event.var != "CLOUD1" or self.updating:
            return

        raw = str(event.value)
        if not raw.startswith("9") or len(raw) < 20:
            logger.debug("Ignored: doesn't start with '9' or too short")
            return

        request_id = raw[1:10]
        try:
            header_len = int(raw[10:15])
            body_len = int(raw[15:20])
        except ValueError:
            error_msg = "Invalid header/body lengths"
            logger.error(error_msg)
            self.log_error("Parse error", f"Request {request_id}: {error_msg}")
            return

        total_needed_len = 20 + header_len + body_len
        if len(raw) < total_needed_len:
            error_msg = f"Incomplete message. Got {len(raw)} chars, need {total_needed_len}"
            logger.error(error_msg)
            self.log_error("Parse error", f"Request {request_id}: {error_msg}")
            return

        header_digits = raw[20:20 + header_len]
        body_digits = raw[20 + header_len:20 + header_len + body_len]
        header = self.digits_to_ascii(header_digits)
        body = self.digits_to_ascii(body_digits)
        logger.info(f"New Request - ID: {request_id}, Header: '{header}', Body length: {len(body)}")

        try:
            method, url = header.strip().split(" ", 1)
        except ValueError:
            error_msg = "Header format should be 'METHOD URL'"
            logger.error(error_msg)
            self.log_error("Parse error", f"Request {request_id}: {error_msg}")
            return

        asyncio.run_coroutine_threadsafe(
            self.handle_request(request_id, method, url, body),
            self.loop
        )

    async def handle_request(self, request_id, method, url, body):
        response_text, duration_ms = await self.fetch_from_web(method, url, body)

        max_response_chars = (self.MAX_CLOUD_LENGTH - self.REQUEST_ID_LEN) // 5
        response_text = response_text[:max_response_chars]

        result = request_id + self.ascii_to_digits(response_text)
        self.updating = True
        try:
            self.cloud.set_var("CLOUD1", result[:self.MAX_CLOUD_LENGTH])
            logger.info(f"Response sent for request {request_id} ({len(result)} chars)")
            self.log_request(request_id, method, url, body, len(response_text), duration_ms)
        except Exception as e:
            error_msg = f"Failed to set cloud variable: {str(e)}"
            logger.error(error_msg)
            self.log_error("Cloud variable error", f"Request {request_id}: {error_msg}")
        finally:
            await asyncio.sleep(0.5)
            self.updating = False

    def run_event_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.cloud = sa.TwCloud(project_id=self.PROJECT_ID)
            self.events = self.cloud.events()
            self.events.event(self.on_set)
            logger.info(f"Connected to Scratch project {self.PROJECT_ID}")
            self.events.start()
            self.loop.run_forever()
        except Exception as e:
            error_msg = f"Failed to start event loop: {str(e)}"
            logger.error(error_msg)
            self.log_error("Service startup error", error_msg)
        finally:
            self.loop.close()

    def start(self):
        if self.running:
            return
        self.running = True
        logger.info("Starting Scratch cloud variable HTTP proxy service...")
        try:
            self.thread = threading.Thread(target=self.run_event_loop, daemon=True)
            self.thread.start()
            logger.info("Proxy service started successfully")
        except Exception as e:
            error_msg = f"Failed to start proxy service: {str(e)}"
            logger.error(error_msg)
            self.log_error("Service startup error", error_msg)
            self.running = False
            raise

    def stop(self):
        if not self.running:
            return
        logger.info("Stopping proxy service...")
        self.running = False
        try:
            if self.events:
                self.events.stop()
            if self.loop and self.loop.is_running():
                self.loop.call_soon_threadsafe(self.loop.stop)
            logger.info("Proxy service stopped")
        except Exception as e:
            error_msg = f"Error stopping proxy service: {str(e)}"
            logger.error(error_msg)
            self.log_error("Service shutdown error", error_msg)

# -------------------- Flask Server --------------------
app = Flask(__name__)
proxy_service = ProxyService(
    log_request_callback=lambda *args: print("Request", args),
    log_error_callback=lambda *args: print("Error", args)
)
proxy_service.start()

@app.route("/")
def index():
    return "Scratch Cloud Proxy is running."

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
