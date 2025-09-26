# server.py
import asyncio
import threading
import time
import os
import logging
from queue import Queue, Empty
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import scratchattach as sa
import aiohttp
from flask import Flask
import re

# ------------------------
# Inlined codec (final you approved)
# ------------------------
allowed_chars = [
    " ","!","\"","#","$","%","&","'","(",")","*","+",",","-",".","/",
    "0","1","2","3","4","5","6","7","8","9",":",";","<","=",">","?",
    "@","A","B","C","D","E","F","G","H","J","L","M","N","O","P","Q","R",
    "S","T","U","V","W","X","Y","Z","[","\\","]","^","_","`","a","b","c",
    "d","e","f","g","h","j","l","m","n","o","p","q","r","s","t","u","v",
    "w","x","y","z","{","|","}","~"
]

char_to_idx = {c: i+1 for i, c in enumerate(allowed_chars)}
idx_to_char = {i+1: c for i, c in enumerate(allowed_chars)}

def string_to_utf16_units(s: str) -> list[int]:
    b = s.encode("utf-16-be")
    return [(b[i]<<8) + b[i+1] for i in range(0, len(b), 2)]

def utf16_units_to_string(units: list[int]) -> str:
    b = bytearray()
    for u in units:
        if isinstance(u, str):  # number sequences stored directly as strings
            b.extend(u.encode('utf-16-be'))
        else:
            b.extend([(u >> 8) & 0xFF, u & 0xFF])
    return bytes(b).decode("utf-16-be")

def encode_string(s: str) -> str:
    result = []
    i = 0
    while i < len(s):
        match = re.match(r'\d{4,}', s[i:])
        if match:
            num_str = match.group(0)
            num_len = len(num_str)
            len_len = len(str(num_len))
            result.append(f"00{len_len}{num_len}{num_str}")
            i += num_len
            continue

        c = s[i]
        u = ord(c)
        if c in char_to_idx:
            result.append(f"{char_to_idx[c]:02}")
        else:
            code = u + 1
            s_code = str(code).zfill(5)
            result.append(f"{93 + int(s_code[0])}{s_code[1:]}")  # fallback using 93
        i += 1
    return "".join(result)

def decode_string(encoded: str) -> str:
    i = 0
    units = []
    while i < len(encoded):
        if encoded[i:i+2] == "00":
            len_len = int(encoded[i+2])
            num_len = int(encoded[i+3:i+3+len_len])
            start = i + 3 + len_len
            num_str = encoded[start:start+num_len]
            units.append(num_str)
            i = start + num_len
            continue

        prefix = int(encoded[i:i+2])
        if 1 <= prefix <= len(allowed_chars):
            units.append(idx_to_char[prefix])
            i += 2
        elif prefix >= 93:
            d = prefix - 93
            s_code = str(d) + encoded[i+2:i+6]
            u = int(s_code) - 1
            units.append(chr(u))
            i += 6
        else:
            raise ValueError(f"Invalid prefix at position {i}: {prefix}")
    return "".join(units)

# ------------------------
# Server config
# ------------------------
MAX_WORKERS = max(4, (os.cpu_count() or 4) * 2)
NUM_WORKER_THREADS = MAX_WORKERS
REQUEST_ID_DIGITS = 32
CLOUD_VAR = "CLOUD1"
PROJECT_ID = "1197011359"  # change if needed
REQUEST_TIMEOUT = 100  # seconds — cut requests that take longer

# ------------------------
# Logging & Flask
# ------------------------
logger = logging.getLogger("proxy")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
app = Flask(__name__)
@app.route("/")
def index():
    return "Proxy service running!"

# ------------------------
# ProxyService
# ------------------------
class ProxyService:
    def __init__(self):
        self.running = False
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.cloud = None
        self.events = None

        self.queue: Queue = Queue()
        self.executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        self.worker_threads = []

        # Guard to avoid reacting to our own writes
        self.writing_lock = threading.Lock()

    # ---------- Async HTTP fetch with aiohttp ----------
    async def fetch_from_web(self, method: str, url: str, body: Optional[str] = None) -> tuple[str, int]:
        t0 = time.time()
        try:
            timeout = aiohttp.ClientTimeout(total=None)  # we manage timeout via asyncio.wait_for
            async with aiohttp.ClientSession(timeout=timeout) as session:
                if method.upper() == "GET":
                    async with session.get(url) as resp:
                        text = await resp.text()
                elif method.upper() == "POST":
                    async with session.post(url, data=body) as resp:
                        text = await resp.text()
                else:
                    text = f"Invalid method: {method}"
            return text, int((time.time() - t0) * 1000)
        except Exception as e:
            logger.exception("HTTP fetch error")
            return f"Error: {e}", int((time.time() - t0) * 1000)

    # ---------- Worker thread: dequeue & schedule coroutine ----------
    def queue_worker(self):
        logger.info("Queue worker started")
        while self.running:
            try:
                item = self.queue.get(timeout=1.0)
            except Empty:
                continue

            req_id, first_char, decoded_payload = item
            try:
                # schedule the coroutine; do NOT block the worker waiting for result
                fut = asyncio.run_coroutine_threadsafe(self._handle_request_coroutine(req_id, first_char, decoded_payload), self.loop)
            except Exception:
                logger.exception("Failed to schedule coroutine")
            else:
                # add callback to catch exceptions inside coroutine
                def _cb(f):
                    try:
                        f.result()
                    except Exception:
                        logger.exception("Request coroutine raised exception")
                fut.add_done_callback(_cb)

            self.queue.task_done()

    # ---------- Coroutine that processes a single request ----------
    async def _handle_request_coroutine(self, req_id: str, first_char: str, decoded_payload: str):
        # only '9' requests are enqueued for full processing
        if first_char != "9":
            logger.warning("Non-9 reached processing: %s", first_char)
            return

        # parse METHOD URL [body] using first space = method/url, rest = body
        method = "GET"
        url = decoded_payload
        body = None
        try:
            parts = decoded_payload.split(" ", 2)  # split into at most 3 parts
            if len(parts) >= 2:
                method = parts[0].strip().upper()
                url = parts[1].strip()
                if len(parts) == 3:
                    body = parts[2]
            else:
                # fallback: entire payload is URL
                method = "GET"
                url = decoded_payload.strip()
        except Exception:
            logger.exception("Failed parsing decoded payload; treating whole as URL")
            method, url, body = "GET", decoded_payload, None

        # perform HTTP fetch with a timeout
        try:
            fetch_coro = self.fetch_from_web(method, url, body)
            response_text, duration_ms = await asyncio.wait_for(fetch_coro, timeout=REQUEST_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning("Request timed out for %s", req_id)
            response_text = "Request timed out"
            duration_ms = REQUEST_TIMEOUT * 1000
        except Exception:
            logger.exception("Exception during fetch for %s", req_id)
            response_text = "Error during fetch"
            duration_ms = 0

        # encode response off the loop (CPU work) using executor
        try:
            encoded_response = await self.loop.run_in_executor(self.executor, encode_string, response_text)
        except Exception:
            logger.exception("Encoding response failed")
            encoded_response = encode_string("Error encoding response")

        final_payload = "0" + first_char + req_id + encoded_response

        # write to cloud (guarded to avoid reacting to our own writes)
        with self.writing_lock:
            try:
                self.cloud.set_var(CLOUD_VAR, final_payload)
                logger.info("WROTE response for %s (%d chars, %d ms)", req_id, len(response_text), duration_ms)
            except Exception:
                logger.exception("Failed to write cloud response")

    # ---------- on_set callback invoked by scratchattach (synchronous) ----------
    def on_set(self, event):
        try:
            if not isinstance(event.value, (str, int)):
                return
            raw = str(event.value).strip()
            if raw == "":
                return

            # ignore events from our own writes
            if self.writing_lock.locked():
                return

            # transmissions must be numeric-only
            if not raw.isdigit():
                logger.info("Received non-numeric value -> immediate Invalid request reply")
                # Can't extract request id reliably; send zeros
                self._immediate_text_reply(first_char=(raw[0] if raw else "0"), req_id="0"*REQUEST_ID_DIGITS, text="Invalid request")
                return

            first_char = raw[0]

            # need at least 1 + REQUEST_ID_DIGITS
            if len(raw) < 1 + REQUEST_ID_DIGITS:
                logger.info("Too short (no 32-digit id) -> immediate Invalid request")
                self._immediate_text_reply(first_char=first_char, req_id="0"*REQUEST_ID_DIGITS, text="Invalid request")
                return

            request_id = raw[1:1+REQUEST_ID_DIGITS]
            if not request_id.isdigit() or len(request_id) != REQUEST_ID_DIGITS:
                logger.info("Malformed request id -> immediate Invalid request")
                self._immediate_text_reply(first_char=first_char, req_id="0"*REQUEST_ID_DIGITS, text="Invalid request")
                return

            if first_char == "9":
                # Full request: decode the rest and enqueue
                encoded_payload = raw[1+REQUEST_ID_DIGITS:]
                try:
                    decoded = decode_string(encoded_payload) if encoded_payload != "" else ""
                except Exception:
                    logger.exception("Failed to decode payload; replying Invalid request")
                    self._immediate_text_reply(first_char=first_char, req_id=request_id, text="Invalid request")
                    return

                # enqueue decoded payload; worker threads will schedule processing
                self.queue.put((request_id, first_char, decoded))
                logger.info("Enqueued request %s", request_id)
                return

            elif first_char == "8":
                # immediate work-in-progress reply (same request id)
                logger.info("Received '8' -> immediate WIP reply for %s", request_id)
                self._immediate_text_reply(first_char="8", req_id=request_id, text="work in progress")
                return

            else:
                logger.info("Unknown leading digit '%s' -> immediate Invalid request for %s", first_char, request_id)
                self._immediate_text_reply(first_char=first_char, req_id=request_id, text="Invalid request")
                return

        except Exception:
            logger.exception("Exception in on_set")

    # ---------- helper to encode and send immediate replies ----------
    def _immediate_text_reply(self, first_char: str, req_id: str, text: str):
        try:
            encoded = encode_string(text)
        except Exception:
            logger.exception("Encoding immediate reply failed")
            encoded = encode_string("Error encoding reply")
        payload = "0" + first_char + req_id + encoded
        with self.writing_lock:
            try:
                self.cloud.set_var(CLOUD_VAR, payload)
                logger.info("Immediate reply written for %s", req_id)
            except Exception:
                logger.exception("Failed to write immediate reply")

    # ---------- run event loop & worker threads ----------
    def run_event_loop(self):
        logger.info("Starting event loop thread")
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)

        # init cloud
        self.cloud = sa.TwCloud(project_id=PROJECT_ID)
        self.events = self.cloud.events()
        self.events.event(self.on_set)
        self.events.start()

        self.running = True
        for _ in range(NUM_WORKER_THREADS):
            t = threading.Thread(target=self.queue_worker, daemon=True)
            t.start()
            self.worker_threads.append(t)

        try:
            self.loop.run_forever()
        finally:
            logger.info("Event loop stopping")
            if self.events:
                self.events.stop()

    def start(self):
        if self.running:
            return
        t = threading.Thread(target=self.run_event_loop, daemon=True)
        t.start()
        logger.info("ProxyService started")

    def stop(self):
        logger.info("Stopping ProxyService")
        self.running = False
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.executor.shutdown(wait=True)

# ------------------------
# Entrypoint
# ------------------------
if __name__ == "__main__":
    svc = ProxyService()
    svc.start()
    port = int(os.environ.get("PORT", "10000"))
    # Flask only used for healthcheck in dev
    app.run(host="0.0.0.0", port=port, threaded=True)
