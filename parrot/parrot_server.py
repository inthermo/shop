"""Parrot transcription server — runs on a fast always-on PC (inthermopc) so slow
client PCs can dictate with Parrot without doing whisper locally.

POST /transcribe   body = raw little-endian float32 mono PCM at 16 kHz
                   header X-Sample-Rate (optional, default 16000)
                   -> {"ok": true, "text": "...", "seconds": 2.3, "ms": 640}
GET  /healthz      -> {"ok": true, "model": "small.en"}

Binds 127.0.0.1:8810 only; reached from the tailnet via Shop Server's /parrot/*
forward (https://inthermopc.tail881969.ts.net/parrot/transcribe). Autostart on
inthermopc: Startup\\ParrotServer.vbs (pythonw). Model override: PARROT_MODEL.
Log: parrot_server.log next to this file.
"""
import json
import logging
import os
import socket
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

PORT = int(os.environ.get("PARROT_SERVER_PORT", "8810"))
MODEL_NAME = os.environ.get("PARROT_MODEL", "base.en")  # inthermopc i5-9500T: base.en ~1.1 s per 8 s clip, small.en ~3 s (10/23 bench)
HERE = os.path.dirname(os.path.abspath(__file__))
logging.basicConfig(
    filename=os.path.join(HERE, "parrot_server.log"), level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("parrot_server")
logging.getLogger("faster_whisper").setLevel(logging.WARNING)  # drop per-request VAD chatter
MODEL = None


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
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
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
            segments, _ = MODEL.transcribe(audio, language="en", beam_size=1, vad_filter=True)
            text = " ".join(s.text.strip() for s in segments).strip()
        except Exception as e:  # noqa: BLE001
            log.exception("transcribe failed")
            return self._json({"ok": False, "error": str(e)}, 500)
        ms = int((time.time() - t0) * 1000)
        log.info("%s %.1fs audio -> %d ms: %r", self.client_address[0], seconds, ms, text)
        self._json({"ok": True, "text": text, "seconds": round(seconds, 1), "ms": ms})

    def log_message(self, fmt, *args):  # quiet; we log transcriptions ourselves
        pass


def main():
    global MODEL
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError as e:
        if e.errno in (98, 10048) or isinstance(e, socket.error) and "10048" in str(e):
            log.info("already running on %d; exiting", PORT)
            return
        raise
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
