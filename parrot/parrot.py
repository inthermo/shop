"""Parrot for Windows — push-to-talk local dictation.

Hold Ctrl+Win, speak, release: the transcription is typed at the cursor.
Fully on-device (faster-whisper small.en, CPU). No API keys, no cloud.
Slow PC? Set PARROT_SERVER to a parrot_server.py URL on a faster machine
on your tailnet and the audio is transcribed there instead.

Inspired by https://github.com/digimata/parrot (macOS). This is the
Windows equivalent: Raw Input key tracking + sounddevice + faster-whisper.
(Raw Input rather than a keyboard hook so the hotkey still works when a
full-screen Chrome Remote Desktop tab holds Chrome's keyboard lock.)

Run with pythonw.exe for no console window. Logs to parrot.log next to
this file. Beeps: high = recording, low = stopped, two rising = ready,
buzz = nothing transcribed.

Custom words (Inthermo, Cybex...): vocab.txt next to this file — see Vocab.
With PARROT_SERVER set, the server's vocab.txt is the master copy and is
downloaded here at startup for the local fallback.
"""

import ctypes
import logging
import os
import re
import sys
import threading
import time
from ctypes import wintypes

import numpy as np
import sounddevice as sd
import keyboard
import winsound
import json
import urllib.request

SAMPLE_RATE = 16000
MIN_SECONDS = 0.35          # ignore accidental taps
# Override per machine with a user env var PARROT_MODEL (e.g. "base.en" on slow PCs,
# ~3x faster than small.en with slightly lower accuracy; "tiny.en" faster still).
MODEL_NAME = os.environ.get("PARROT_MODEL", "small.en")  # small.en = ~250 MB download on first run
# Slow PCs: set user env var PARROT_SERVER to a parrot_server.py URL, e.g.
# https://inthermopc.tail881969.ts.net/parrot — audio is transcribed there
# instead of locally (local model loads lazily only if the server fails).
SERVER = os.environ.get("PARROT_SERVER", "").rstrip("/")
SERVER_TIMEOUT = float(os.environ.get("PARROT_SERVER_TIMEOUT", "20"))
# Mouse push-to-talk: hold a mouse button instead of Ctrl+Win. Needed in a
# full-screen Chrome Remote Desktop tab, where Chrome's keyboard lock swallows
# every key (even from Raw Input) but never mouse buttons. PARROT_MOUSE_BUTTON =
# comma list of 3 (middle/wheel click, default), 4 (back/side) and/or 5
# (forward/side); "0" disables. A low-level mouse hook intercepts the chosen
# button: a quick press (shorter than PARROT_MOUSE_HOLD_MS, default 250) is
# replayed as a normal click, so wheel-click still opens links in new tabs and
# closes tabs; holding it longer records. PARROT_MOUSE_HOLD_MS=0 = every press
# records and clicks never reach the app. PARROT_MOUSE_SWALLOW=0 removes the
# hook entirely (buttons tracked via Raw Input, clicks always reach the app).
_mb = os.environ.get("PARROT_MOUSE_BUTTON", "3")
MOUSE_BUTTONS = {int(x) for x in _mb.replace(";", ",").split(",") if x.strip().isdigit()} - {0}
MOUSE_SWALLOW = os.environ.get("PARROT_MOUSE_SWALLOW", "1") != "0"
MOUSE_HOLD = float(os.environ.get("PARROT_MOUSE_HOLD_MS", "250")) / 1000.0
# Seconds between typed keys: local apps vs. a remote-desktop window (which
# drops keys typed faster than ~30 ms apart).
TYPE_DELAY = float(os.environ.get("PARROT_TYPE_DELAY", "0.01"))
REMOTE_TYPE_DELAY = float(os.environ.get("PARROT_REMOTE_TYPE_DELAY", "0.035"))
# Foreground window titles (substring, case-insensitive) that mean "typing into
# a remote desktop" -> slow pacing. A full-screen Chrome Remote Desktop tab is
# titled after the remote machine ("InthermoPC - Google Chrome"), not "Remote Desktop".
REMOTE_TITLES = [t.strip().lower() for t in
                 os.environ.get("PARROT_REMOTE_TITLES", "remote desktop,inthermopc").split(",") if t.strip()]
# Remote typing goes out in word chunks with a breather between them. An
# unbroken burst of injected keys into a full-screen Chrome Remote Desktop tab
# dropped the CRD session (7 times on 10/29, each to the second as typing
# began); pausing lets the client's event queue drain.
REMOTE_CHUNK = int(os.environ.get("PARROT_REMOTE_CHUNK", "24"))
REMOTE_CHUNK_PAUSE = float(os.environ.get("PARROT_REMOTE_CHUNK_PAUSE_MS", "120")) / 1000.0
# Chunking did not stop the drops (again 10/30 05:30:43, mid-typing), and
# clipboard paste fails too (CRD only syncs the client clipboard when its tab
# gains focus, so Ctrl+V pasted stale text). So with PARROT_SERVER set, remote
# text goes to the server, which types it on the host itself -- no keystrokes
# cross CRD. PARROT_TYPE_TOKEN must match the server's type_token.txt.
# "type" = old chunked path (also the fallback if the server call fails).
REMOTE_MODE = os.environ.get("PARROT_REMOTE_MODE", "host").strip().lower()
TYPE_TOKEN = os.environ.get("PARROT_TYPE_TOKEN", "").strip()
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parrot.log")

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("parrot")


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


VOCAB = Vocab(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vocab.txt"))

# --- Raw Input key-state tracker ---------------------------------------
# Chrome's Keyboard Lock (used by Chrome Remote Desktop in full screen)
# installs a low-level keyboard hook that swallows Ctrl/Win before any
# other hook sees them, so keyboard.is_pressed() stops working the moment
# the CRD tab goes full screen. Raw Input (WM_INPUT) is delivered
# independently of low-level hooks, so we track modifier state from it.

_u32 = ctypes.windll.user32
_k32 = ctypes.windll.kernel32

WM_INPUT = 0x00FF
RIDEV_INPUTSINK = 0x00000100
RID_INPUT = 0x10000003
RIM_TYPEMOUSE = 0
RIM_TYPEKEYBOARD = 1
# RAWMOUSE.usButtonFlags bits: (down, up) per button number
MOUSE_FLAGS = {3: (0x0010, 0x0020), 4: (0x0040, 0x0080), 5: (0x0100, 0x0200)}
RI_KEY_BREAK = 1
VK_CTRL = {0x11, 0xA2, 0xA3}
VK_WIN = {0x5B, 0x5C}

WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                             wintypes.WPARAM, wintypes.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
WH_MOUSE_LL = 14
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_XBUTTONDOWN, WM_XBUTTONUP = 0x020B, 0x020C
LLMHF_INJECTED = 0x01
WM_PARROT_CLICK = 0x0400 + 7  # WM_USER+7: replay a swallowed short press as a click
# mouse_event flags for replaying a click: (down, up, mouseData) per button number
MOUSE_EVENT = {3: (0x0020, 0x0040, 0), 4: (0x0080, 0x0100, 1), 5: (0x0080, 0x0100, 2)}


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", wintypes.USHORT), ("usUsage", wintypes.USHORT),
                ("dwFlags", wintypes.DWORD), ("hwndTarget", wintypes.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", wintypes.DWORD), ("dwSize", wintypes.DWORD),
                ("hDevice", wintypes.HANDLE), ("wParam", wintypes.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", wintypes.USHORT), ("Flags", wintypes.USHORT),
                ("Reserved", wintypes.USHORT), ("VKey", wintypes.USHORT),
                ("Message", wintypes.UINT), ("ExtraInformation", wintypes.ULONG)]


class RAWMOUSE(ctypes.Structure):
    _fields_ = [("usFlags", wintypes.USHORT), ("_pad", wintypes.USHORT),
                ("usButtonFlags", wintypes.USHORT), ("usButtonData", wintypes.USHORT),
                ("ulRawButtons", wintypes.ULONG), ("lLastX", wintypes.LONG),
                ("lLastY", wintypes.LONG), ("ulExtraInformation", wintypes.ULONG)]


class RAWDATA(ctypes.Union):
    _fields_ = [("keyboard", RAWKEYBOARD), ("mouse", RAWMOUSE)]


class RAWINPUT(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("data", RAWDATA)]


_u32.DefWindowProcW.restype = ctypes.c_ssize_t
_u32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_u32.CreateWindowExW.restype = wintypes.HWND
_u32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                 wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_u32.GetRawInputData.argtypes = [wintypes.HANDLE, wintypes.UINT, wintypes.LPVOID,
                                 ctypes.POINTER(wintypes.UINT), wintypes.UINT]
_k32.GetModuleHandleW.restype = wintypes.HINSTANCE
_u32.SetWindowsHookExW.restype = wintypes.HHOOK
_u32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
_u32.CallNextHookEx.restype = ctypes.c_ssize_t
_u32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]


class RawKeys:
    """Tracks which virtual keys are physically down, via WM_INPUT."""

    def __init__(self):
        self.down = set()
        self.mouse_down = {}     # mouse button number (3/4/5) -> time.monotonic() when pressed
        self.hwnd = None
        self.ready = threading.Event()
        self.error = None
        self.hook = None          # WH_MOUSE_LL handle when swallowing is active
        self.hook_error = None
        self._proc = WNDPROC(self._wndproc)  # keep a reference alive
        self._mproc = HOOKPROC(self._mouseproc)
        threading.Thread(target=self._run, daemon=True, name="rawinput").start()

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_INPUT:
            size = wintypes.UINT(ctypes.sizeof(RAWINPUT))
            ri = RAWINPUT()
            got = _u32.GetRawInputData(lparam, RID_INPUT, ctypes.byref(ri),
                                       ctypes.byref(size), ctypes.sizeof(RAWINPUTHEADER))
            if got in (0, 0xFFFFFFFF):
                pass
            elif ri.header.dwType == RIM_TYPEKEYBOARD:
                vk = ri.data.keyboard.VKey
                if ri.data.keyboard.Flags & RI_KEY_BREAK:
                    self.down.discard(vk)
                else:
                    self.down.add(vk)
            elif ri.header.dwType == RIM_TYPEMOUSE and not self.hook:
                # Backup state source only when the hook isn't running (the
                # hook sees the same events, and raw input would also see the
                # replayed clicks).
                bf = ri.data.mouse.usButtonFlags
                for n, (dn, up) in MOUSE_FLAGS.items():
                    if bf & dn:
                        self.mouse_down.setdefault(n, time.monotonic())
                    if bf & up:
                        self.mouse_down.pop(n, None)
        elif msg == WM_PARROT_CLICK:
            # Short press: replay it as an injected click (the hook lets
            # injected events through), so the app gets a normal click.
            dn, up, data = MOUSE_EVENT[int(wparam)]
            _u32.mouse_event(dn, 0, 0, data, 0)
            _u32.mouse_event(up, 0, 0, data, 0)
            return 0
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _mouseproc(self, ncode, wparam, lparam):
        # Low-level mouse hook: track the push-to-talk button and swallow its
        # events; a press released before MOUSE_HOLD is replayed as a click
        # from the message loop. Keep this trivial - Windows drops hooks that
        # take too long.
        if ncode == 0 and wparam in (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_XBUTTONDOWN, WM_XBUTTONUP):
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if not info.flags & LLMHF_INJECTED:
                if wparam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                    n = 3
                else:
                    n = 3 + ((info.mouseData >> 16) & 0xFFFF)  # XBUTTON1 -> 4, XBUTTON2 -> 5
                if n in MOUSE_BUTTONS:
                    if wparam in (WM_MBUTTONDOWN, WM_XBUTTONDOWN):
                        self.mouse_down.setdefault(n, time.monotonic())
                    else:
                        t = self.mouse_down.pop(n, None)
                        if t is not None and MOUSE_HOLD and time.monotonic() - t < MOUSE_HOLD:
                            _u32.PostMessageW(self.hwnd, WM_PARROT_CLICK, n, 0)
                    return 1
        return _u32.CallNextHookEx(None, ncode, wparam, lparam)

    def _run(self):
        try:
            hinst = _k32.GetModuleHandleW(None)
            wc = WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = "ParrotRawInput"
            if not _u32.RegisterClassW(ctypes.byref(wc)):
                raise ctypes.WinError()
            hwnd = _u32.CreateWindowExW(0, wc.lpszClassName, "Parrot", 0,
                                        0, 0, 0, 0, None, None, hinst, None)
            if not hwnd:
                raise ctypes.WinError()
            self.hwnd = hwnd
            rids = (RAWINPUTDEVICE * 2)(RAWINPUTDEVICE(0x01, 0x06, RIDEV_INPUTSINK, hwnd),
                                        RAWINPUTDEVICE(0x01, 0x02, RIDEV_INPUTSINK, hwnd))
            if not _u32.RegisterRawInputDevices(rids, 2, ctypes.sizeof(RAWINPUTDEVICE)):
                raise ctypes.WinError()
        except Exception as e:  # fall back to keyboard.is_pressed
            self.error = e
            self.ready.set()
            return
        if MOUSE_BUTTONS and MOUSE_SWALLOW:
            self.hook = _u32.SetWindowsHookExW(WH_MOUSE_LL, self._mproc, _k32.GetModuleHandleW(None), 0)
            if not self.hook:
                self.hook_error = ctypes.WinError()
        self.ready.set()
        msg = wintypes.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))

    def ctrl(self):
        return bool(self.down & VK_CTRL)

    def win(self):
        return bool(self.down & VK_WIN)

    def mouse(self):
        # Held long enough to count as push-to-talk (short presses are clicks).
        now = time.monotonic()
        return any(now - t >= MOUSE_HOLD for n, t in list(self.mouse_down.items()) if n in MOUSE_BUTTONS)


WIN_KEYS = ("left windows", "right windows")
RAW = None  # set in main() once logging is up


def win_pressed():
    if RAW is not None:
        return RAW.win()
    for k in WIN_KEYS:
        try:
            if keyboard.is_pressed(k):
                return True
        except ValueError:
            pass
    return False


def ctrl_pressed():
    if RAW is not None:
        return RAW.ctrl()
    return keyboard.is_pressed("ctrl")


def mouse_pressed():
    return RAW is not None and RAW.mouse()


def combo_pressed():
    return (ctrl_pressed() and win_pressed()) or mouse_pressed()


def foreground_title():
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    buf = ctypes.create_unicode_buffer(512)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, 512)
    return buf.value


def type_text(text):
    # keyboard.write defaults to unicode VK_PACKET events on Windows, which
    # remote-desktop clients (Chrome Remote Desktop, RDP) do not forward -- the
    # remote session never sees them. Real virtual-key presses, paced so none
    # drop, work both locally and through any remote client, so use them for
    # all plain-ASCII text; fall back to unicode only for characters the US
    # layout cannot produce with key presses.
    title = foreground_title()
    # Remote-desktop clients drop keys that arrive too fast (10 ms pacing lost
    # letters through full-screen Chrome Remote Desktop), so pace slower there.
    remote = any(t in title.lower() for t in REMOTE_TITLES)
    delay = REMOTE_TYPE_DELAY if remote else TYPE_DELAY
    t0 = time.time()
    if remote and REMOTE_MODE == "host" and SERVER and TYPE_TOKEN:
        try:
            server_type(text)
            mode = "host"
        except Exception as e:
            log.warning("host typing failed (%s); typing through the remote client", e)
            mode = "vk %d ms, %d-char chunks (host failed)" % (round(delay * 1000), REMOTE_CHUNK)
            for chunk in word_chunks(text, REMOTE_CHUNK):
                keyboard.write(chunk, delay=delay, exact=False)
                time.sleep(REMOTE_CHUNK_PAUSE)
    elif not text.isascii():
        mode = "unicode"
        keyboard.write(text)
    elif remote:
        mode = "vk %d ms, %d-char chunks" % (round(delay * 1000), REMOTE_CHUNK)
        for chunk in word_chunks(text, REMOTE_CHUNK):
            keyboard.write(chunk, delay=delay, exact=False)
            time.sleep(REMOTE_CHUNK_PAUSE)
    else:
        mode = "vk %d ms" % round(delay * 1000)
        keyboard.write(text, delay=delay, exact=False)
    log.info("typed %d chars in %.1f s (%s) into %r",
             len(text), time.time() - t0, mode, title[:80])


def word_chunks(text, n):
    """Split text at word boundaries into pieces of at least n chars."""
    out, cur = [], ""
    for piece in re.split(r"(?<= )", text):
        cur += piece
        if len(cur) >= n:
            out.append(cur)
            cur = ""
    if cur:
        out.append(cur)
    return out


def beep(freq, ms):
    try:
        winsound.Beep(freq, ms)
    except Exception:
        pass


def server_transcribe(audio):
    """POST float32 PCM to the Parrot server; returns text or raises."""
    req = urllib.request.Request(
        SERVER + "/transcribe", data=audio.astype(np.float32).tobytes(), method="POST",
        headers={"Content-Type": "application/octet-stream", "X-Sample-Rate": str(SAMPLE_RATE)},
    )
    with urllib.request.urlopen(req, timeout=SERVER_TIMEOUT) as r:
        out = json.loads(r.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or "server error")
    return out.get("text", "").strip()


def server_type(text):
    """Ask the Parrot server to type text on its own desktop (the remote host)."""
    req = urllib.request.Request(
        SERVER + "/type", method="POST", headers={"Content-Type": "application/json"},
        # token in the body: Shop Server's /parrot/* forward drops custom headers
        data=json.dumps({"text": text, "token": TYPE_TOKEN}).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        out = json.loads(r.read().decode("utf-8"))
    if not out.get("ok"):
        raise RuntimeError(out.get("error") or "server error")


def main():
    global RAW
    log.info("starting; loading model %s", MODEL_NAME)
    rk = RawKeys()
    rk.ready.wait(5)
    if rk.error is None and rk.ready.is_set():
        RAW = rk
        log.info("raw input key tracking active; mouse push-to-talk buttons %s (%s)",
                 sorted(MOUSE_BUTTONS) or "off",
                 ("hold %d ms = record, shorter = normal click" % (MOUSE_HOLD * 1000)) if rk.hook and MOUSE_HOLD
                 else "clicks swallowed" if rk.hook else "clicks pass through")
        if rk.hook_error:
            log.warning("mouse hook failed (%s); button clicks will reach the focused app", rk.hook_error)
    else:
        log.warning("raw input unavailable (%s); using keyboard hook", rk.error)
    model = None

    def local_model():
        nonlocal model
        if model is None:
            from faster_whisper import WhisperModel  # slow import; after logging is up

            log.info("loading local model %s", MODEL_NAME)
            model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
            # Warm up so the first real dictation isn't slow.
            model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")
            log.info("model ready")
        return model

    if SERVER:
        try:
            with urllib.request.urlopen(SERVER + "/healthz", timeout=10) as r:
                log.info("using server %s: %s", SERVER, r.read(200).decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            log.warning("server %s unreachable (%s); loading local model", SERVER, e)
            local_model()
        try:
            with urllib.request.urlopen(SERVER + "/vocab", timeout=10) as r:
                body = r.read()
            if body.strip():  # keep the local copy if the server has none
                with open(VOCAB.path, "wb") as f:
                    f.write(body)
        except Exception as e:  # noqa: BLE001
            log.warning("vocab download failed (%s); using local copy", e)
    else:
        local_model()
    beep(700, 90)
    beep(1000, 90)

    while True:
        if not combo_pressed():
            time.sleep(0.02)
            continue

        via_mouse = mouse_pressed()
        if not via_mouse:
            # A key event while Win is held stops Windows opening the Start
            # menu when Win is released.
            keyboard.press_and_release("f24")
        log.info("recording (%s); foreground %r", "mouse" if via_mouse else "ctrl+win",
                 foreground_title()[:60])

        frames = []

        def cb(indata, frame_count, time_info, status):
            frames.append(indata.copy())

        try:
            stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32", callback=cb
            )
            stream.start()
        except Exception:
            log.exception("could not open microphone")
            beep(200, 400)
            time.sleep(1)
            continue

        beep(1200, 60)
        while combo_pressed():
            time.sleep(0.02)
        stream.stop()
        stream.close()
        beep(500, 60)

        if not frames:
            log.info("no audio frames captured")
            continue
        audio = np.concatenate(frames).flatten()
        seconds = len(audio) / SAMPLE_RATE
        if seconds < MIN_SECONDS:
            log.info("%.1fs audio too short; ignored", seconds)
            continue

        t0 = time.time()
        text = None
        if SERVER:
            try:
                text = server_transcribe(audio)
                log.info("%.1fs audio -> %.1fs server: %r", seconds, time.time() - t0, text)
            except Exception as e:  # noqa: BLE001
                log.warning("server transcribe failed (%s); falling back to local", e)
        if text is None:
            try:
                segments, _ = local_model().transcribe(
                    audio, language="en", beam_size=1, vad_filter=True, vad_parameters=dict(min_silence_duration_ms=700, speech_pad_ms=400),
                    initial_prompt=VOCAB.initial_prompt(),
                )
                text = VOCAB.fix(" ".join(s.text.strip() for s in segments).strip())
                log.info("%.1fs audio -> %.1fs transcribe: %r", seconds, time.time() - t0, text)
            except Exception:
                log.exception("transcription failed")
                beep(200, 400)
                continue

        if not text:
            log.info("empty transcript")
            beep(300, 150)
            continue

        # Never type while modifiers are still down — Ctrl+<letters> would
        # fire shortcuts in the focused app.
        t_wait = time.time()
        while (ctrl_pressed() or win_pressed()) and time.time() - t_wait < 3:
            time.sleep(0.02)
        time.sleep(0.12)
        type_text(text + " ")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("fatal")
        sys.exit(1)
