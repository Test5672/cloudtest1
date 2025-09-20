import asyncio
import scratchattach as sa
import aiohttp
import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)

# ---- Your Unicode encoding setup ----
allowed_chars = [" ","!","\"","#","$","%","&","'","(",")","*","+",",","-",".","/","0","1","2","3","4","5","6","7","8","9",
                 ":",";","<","=",">","?","@","A","B","C","D","E","F","G","H","J","L","M","N","O","P","Q","R","S","T","U",
                 "V","W","X","Y","Z","[","\\","]","^","_","","a","b","c","d","e","f","g","h","j","l","m","n","o","p",
                 "q","r","s","t","u","v","w","x","y","z","{","|","}","~"]
char_to_idx = {c: i+1 for i, c in enumerate(allowed_chars)}
idx_to_char = {i+1: c for i, c in enumerate(allowed_chars)}

def string_to_utf16_units(s):
    b = s.encode("utf-16-be")
    return [(b[i]<<8) + b[i+1] for i in range(0, len(b), 2)]

def utf16_units_to_string(units):
    b = bytearray()
    for u in units:
        b.extend([(u >> 8) & 0xFF, u & 0xFF])
    return bytes(b).decode("utf-16-be")

def encode_string(s):
    units = string_to_utf16_units(s)
    result = []
    for u in units:
        c = chr(u)
        if c in char_to_idx:
            result.append(f"{char_to_idx[c]:02}")
        else:
            code = u + 1
            s_code = str(code).zfill(5)
            result.append(f"{93 + int(s_code[0])}{s_code[1:]}")
    return "".join(result)

def decode_string(encoded):
    i = 0
    units = []
    while i < len(encoded):
        prefix = int(encoded[i:i+2])
        if 1 <= prefix <= len(allowed_chars):
            c = idx_to_char[prefix]
            units.append(ord(c))
            i += 2
        elif prefix >= 94:
            d = prefix - 93
            s_code = str(d) + encoded[i+2:i+6]
            u = int(s_code) - 1
            units.append(u)
            i += 6
        else:
            raise ValueError(f"Invalid prefix at position {i}: {prefix}")
    return utf16_units_to_string(units)

# ---- Proxy service ----
class ProxyService:
    def __init__(self, log_request_callback, log_error_callback):
        self.log_request = log_request_callback
        self.log_error = log_error_callback
        self.running = False
        self.loop = None
        self.thread = None
        self.cloud = None
        self.events = None
        self.updating = False
        self.MAX_CLOUD_LENGTH = 100_000
        self.REQUEST_ID_LEN = 9

        # ThreadPool for CPU-heavy encoding/decoding
        self.executor = ThreadPoolExecutor(max_workers=4)

    # Use your Unicode encoder/decoder
    def digits_to_text(self, digits):
        try:
            return decode_string(digits)
        except Exception as e:
            self.log_error("Decode error", str(e))
            return ""

    def text_to_digits(self, text):
        try:
            return encode_string(text)
        except Exception as e:
            self.log_error("Encode error", str(e))
            return ""

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
                self.log_error("HTTP request error", f"{method} {url}: {str(e)}")
                return f"Error: {str(e)}", duration_ms

    def on_set(self, event):
        if event.var != "CLOUD1" or self.updating: return
        raw = str(event.value)
        if not raw.startswith("9") or len(raw) < 20: return
        request_id = raw[1:10]
        try:
            header_len = int(raw[10:15])
            body_len = int(raw[15:20])
        except ValueError:
            self.log_error("Parse error", f"Request {request_id}: Invalid header/body lengths")
            return
        total_needed_len = 20 + header_len + body_len
        if len(raw) < total_needed_len:
            self.log_error("Parse error", f"Request {request_id}: Incomplete message")
            return

        header_digits = raw[20:20 + header_len]
        body_digits = raw[20 + header_len:20 + header_len + body_len]

        # Decode header/body using thread pool
        header = self.loop.run_in_executor(self.executor, self.digits_to_text, header_digits)
        body = self.loop.run_in_executor(self.executor, self.digits_to_text, body_digits)

        async def process():
            header_val = await header
            body_val = await body
            try:
                method, url = header_val.strip().split(" ", 1)
            except ValueError:
                self.log_error("Parse error", f"Request {request_id}: Header should be 'METHOD URL'")
                return
            await self.handle_request(request_id, method, url, body_val)
        asyncio.run_coroutine_threadsafe(process(), self.loop)

    async def handle_request(self, request_id, method, url, body):
        response_text, duration_ms = await self.fetch_from_web(method, url, body)
        max_response_chars = (self.MAX_CLOUD_LENGTH - self.REQUEST_ID_LEN) // 6
        response_text = response_text[:max_response_chars]

        # Encode response using thread pool
        response_encoded = await self.loop.run_in_executor(self.executor, self.text_to_digits, response_text)
        result = request_id + response_encoded
        self.updating = True
        try:
            self.cloud.set_var("CLOUD1", result[:self.MAX_CLOUD_LENGTH])
            self.log_request(request_id, method, url, body, len(response_text), duration_ms)
        finally:
            await asyncio.sleep(0.5)
            self.updating = False

    def run_event_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.cloud = sa.TwCloud(project_id="1197011359")
            self.events = self.cloud.events()
            self.events.event(self.on_set)
            self.events.start()
            self.loop.run_forever()
        finally:
            self.loop.close()

    def start(self):
        if self.running: return
        self.running = True
        self.thread = threading.Thread(target=self.run_event_loop, daemon=True)
        self.thread.start()

    def stop(self):
        if not self.running: return
        self.running = False
        if self.events: self.events.stop()
        if self.loop and self.loop.is_running(): self.loop.call_soon_threadsafe(self.loop.stop)
