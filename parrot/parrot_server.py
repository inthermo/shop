"""Parrot transcription server — runs on a fast always-on PC (inthermopc) so slow
client PCs can dictate with Parrot without doing whisper locally.

POST /transcribe   body = raw little-endian float32 mono PCM at 16 kHz
                   header X-Sample-Rate (optional, default 16000)
                   -> {"ok": true, "text": "...", "seconds": 2.3, "ms": 640}
GET  /healthz      -> {"ok": true, "model": "small.en"}

GET  /vocab        -> vocab.txt as text/plain (clients cache it for local fallback)

POST /type         body = {"text": "...", "token": "..."} -> types text into this
                   PC's foreground window (unicode SendInput). Used when the client
                   is dictating into a Chrome Remote Desktop window on this PC:
                   keys injected through CRD dropped the session, and CRD's
                   clipboard sync is focus-driven so paste got stale text. token
                   must match type_token.txt next to this file (made on first run).

Custom words: vocab.txt next to this file (see Vocab); edits apply on the next
dictation, no restart. Kept out of the public repo (customer names).

Binds 127.0.0.1:8810 only; reached from the tailnet via Shop Server's /parrot/*
forward (https://inthermopc.tail881969.ts.net/parrot/transcribe). Autostart on
inthermopc: Startup\\ParrotServer.vbs (pythonw). Model override: PARROT_MODEL.
Log: parrot_server.log next to this file.
"""
import ctypes
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
from ctypes import wintypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

PORT = int(os.environ.get("PARROT_SERVER_PORT", "8810"))
MODEL_NAME = os.environ.get("PARROT_MODEL", "small.en")  # i5-9500T, 7 s clip: base.en 1.3 s, small.en 4.5 s; base.en drops short words (10/30)
HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(
    filename=os.path.join(HERE, "parrot_server.log"), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("parrot_server")
logging.getLogger("faster_whisper").setLevel(logging.WARNING)  # drop per-request VAD chatter
MODEL = None
TYPE_TOKEN = None


class Vocab:
    """Custom words from vocab.txt (next to this script), re-read when it changes.

    One term per line ("Inthermo", "Cybex") -> fed to whisper as initial_prompt
    so it spells them right. "heard => Right" lines are whole-word,
    case-insensitive fixes applied to the transcript for words it still
    mishears ("in thermal => Inthermo"). "#" starts a comment.
    """

    def __init__(self, path):
        self.path, self.mtime, self.prompt, self.fixes = path, None, None, []

    def _load(self):
        try:
            mtime = os.path.getmtime(self.path)
        except OSError:
            mtime = None
        if mtime == self.mtime:
            return
        self.mtime, terms, fixes = mtime, [], []
        if mtime is not None:
            with open(self.path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.split("#", 1)[0].strip()
                    if "=>" in line:
                        heard, right = (s.strip() for s in line.split("=>", 1))
                        if heard and right:
                            fixes.append((re.compile(r"\b%s\b" % r"\s*".join(map(re.escape, heard.split())), re.I), right))
                            terms.append(right)
                    elif line:
                        terms.append(line)
        terms = list(dict.fromkeys(terms))
        self.prompt = (", ".join(terms) + ".") if terms else None
        self.fixes = fixes
        log.info("vocab: %d terms, %d fixes", len(terms), len(fixes))

    def initial_prompt(self):
        try:
            self._load()
        except Exception:  # noqa: BLE001
            log.exception("vocab load failed")
        return self.prompt

    def fix(self, text):
        for rx, right in self.fixes:
            text = rx.sub(right, text)
        return text


VOCAB = Vocab(os.path.join(HERE, "vocab.txt"))


TOKEN_PATH = os.path.join(HERE, "type_token.txt")


def type_token():
    try:
        with open(TOKEN_PATH, encoding="utf-8") as f:
            tok = f.read().strip()
        if tok:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(24)
    with open(TOKEN_PATH, "w", encoding="utf-8") as f:
        f.write(tok)
    log.info("created %s", TOKEN_PATH)
    return tok


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):  # only here so the union has its real (largest) size
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


INPUT_KEYBOARD, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE, VK_RETURN = 1, 0x0002, 0x0004, 0x0D
MODIFIER_VKS = (0x10, 0x11, 0x12, 0x5B, 0x5C)  # shift, ctrl, alt, lwin, rwin
TYPE_LOCK = threading.Lock()


def _key_inputs(ch):
    if ch == "\n":
        return [INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wVk=VK_RETURN, dwFlags=f)) for f in (0, KEYEVENTF_KEYUP)]
    out = []
    for unit in struct_utf16(ch):
        out += [INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(wScan=unit, dwFlags=KEYEVENTF_UNICODE | f))
                for f in (0, KEYEVENTF_KEYUP)]
    return out


def struct_utf16(ch):
    b = ch.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def type_here(text):
    """Type text into this PC's foreground window, 40 chars per SendInput batch."""
    u32 = ctypes.windll.user32
    # don't type while a modifier is still down on the host (would fire shortcuts)
    deadline = time.time() + 1.0
    while time.time() < deadline and any(u32.GetAsyncKeyState(vk) & 0x8000 for vk in MODIFIER_VKS):
        time.sleep(0.02)
    text = text.replace("\r\n", "\n")
    with TYPE_LOCK:
        for i in range(0, len(text), 40):
            inputs = [k for ch in text[i:i + 40] for k in _key_inputs(ch)]
            arr = (INPUT * len(inputs))(*inputs)
            if u32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT)) != len(inputs):
                raise OSError("SendInput blocked (locked screen or elevated window?)")
            time.sleep(0.005)


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") in ("/healthz", ""):
            return self._json({"ok": True, "model": MODEL_NAME})
        if self.path.rstrip("/") == "/vocab":
            try:
                with open(VOCAB.path, "rb") as f:
                    body = f.read()
            except OSError:
                body = b""
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            return self.wfile.write(body)
        self._json({"ok": False, "error": "not found"}, 404)

    def _type(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8")) if 0 < n < 1024 * 1024 else {}
        except ValueError:
            req = {}
        if not hmac.compare_digest(str(req.get("token") or ""), TYPE_TOKEN):
            log.warning("%s /type rejected: bad token", self.client_address[0])
            return self._json({"ok": False, "error": "bad token"}, 403)
        text = str(req.get("text") or "")
        t0 = time.time()
        try:
            type_here(text)
        except Exception as e:  # noqa: BLE001
            log.warning("type failed: %s", e)
            return self._json({"ok": False, "error": str(e)}, 500)
        log.info("typed %d chars in %d ms", len(text), (time.time() - t0) * 1000)
        self._json({"ok": True})

    def do_POST(self):
        if self.path.rstrip("/") == "/type":
            return self._type()
        if self.path.rstrip("/") != "/transcribe":
            return self._json({"ok": False, "error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 64 * 1024 * 1024:
            return self._json({"ok": False, "error": "bad length"}, 400)
        raw = self.rfile.read(n)
        sr = int(self.headers.get("X-Sample-Rate") or 16000)
        audio = np.frombuffer(raw[: len(raw) - len(raw) % 4], dtype=np.float32)
        if sr != 16000 and len(audio):
            idx = np.linspace(0, len(audio) - 1, int(len(audio) * 16000 / sr))
            audio = np.interp(idx, np.arange(len(audio)), audio).astype(np.float32)
        seconds = len(audio) / 16000.0
        t0 = time.time()
        try:
            segments, _ = MODEL.transcribe(audio, language="en", beam_size=1, vad_filter=True, vad_parameters=dict(min_silence_duration_ms=700, speech_pad_ms=400),
                                           initial_prompt=VOCAB.initial_prompt())
            text = VOCAB.fix(" ".join(s.text.strip() for s in segments).strip())
        except Exception as e:  # noqa: BLE001
            log.exception("transcribe failed")
            return self._json({"ok": False, "error": str(e)}, 500)
        ms = int((time.time() - t0) * 1000)
        log.info("%s %.1fs audio -> %d ms: %r", self.client_address[0], seconds, ms, text)
        self._json({"ok": True, "text": text, "seconds": round(seconds, 1), "ms": ms})

    def log_message(self, fmt, *args):  # quiet; we log transcriptions ourselves
        pass


def main():
    global MODEL, TYPE_TOKEN
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno in (98, 10048) or isinstance(e, socket.error) and "10048" in str(e):
            log.info("already running on %d; exiting", PORT)
            return
        raise
    TYPE_TOKEN = type_token()
    log.info("loading model %s", MODEL_NAME)
    from faster_whisper import WhisperModel  # slow import

    MODEL = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    MODEL.transcribe(np.zeros(16000, dtype=np.float32), language="en")  # warm up
    log.info("ready on 127.0.0.1:%d", PORT)
    srv.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("fatal")
        sys.exit(1)
