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
"""

import ctypes
import logging
import os
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
# (forward/side); "0" disables. While Parrot runs, the chosen button's clicks are
# swallowed by a low-level mouse hook so the focused app (and the remote desktop
# behind it) never sees them; PARROT_MOUSE_SWALLOW=0 lets them through.
_mb = os.environ.get("PARROT_MOUSE_BUTTON", "3")
MOUSE_BUTTONS = {int(x) for x in _mb.replace(";", ",").split(",") if x.strip().isdigit()} - {0}
MOUSE_SWALLOW = os.environ.get("PARROT_MOUSE_SWALLOW", "1") != "0"
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parrot.log")

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("parrot")

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
        self.mouse_down = set()  # mouse button numbers (3/4/5) currently held
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
            elif ri.header.dwType == RIM_TYPEMOUSE:
                bf = ri.data.mouse.usButtonFlags
                for n, (dn, up) in MOUSE_FLAGS.items():
                    if bf & dn:
                        self.mouse_down.add(n)
                    if bf & up:
                        self.mouse_down.discard(n)
        return _u32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _mouseproc(self, ncode, wparam, lparam):
        # Low-level mouse hook: track the push-to-talk button and swallow its
        # clicks so the focused app never acts on them. Keep this trivial -
        # Windows drops hooks that take too long.
        if ncode == 0 and wparam in (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_XBUTTONDOWN, WM_XBUTTONUP):
            info = ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            if not info.flags & LLMHF_INJECTED:
                if wparam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                    n = 3
                else:
                    n = 3 + ((info.mouseData >> 16) & 0xFFFF)  # XBUTTON1 -> 4, XBUTTON2 -> 5
                if n in MOUSE_BUTTONS:
                    if wparam in (WM_MBUTTONDOWN, WM_XBUTTONDOWN):
                        self.mouse_down.add(n)
                    else:
                        self.mouse_down.discard(n)
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
        return bool(self.mouse_down & MOUSE_BUTTONS)


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
    if text.isascii():
        mode = "vk"
        keyboard.write(text, delay=0.01, exact=False)
    else:
        mode = "unicode"
        keyboard.write(text)
    log.info("typed (%s) into %r", mode, title[:80])


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


def main():
    global RAW
    log.info("starting; loading model %s", MODEL_NAME)
    rk = RawKeys()
    rk.ready.wait(5)
    if rk.error is None and rk.ready.is_set():
        RAW = rk
        log.info("raw input key tracking active; mouse push-to-talk buttons %s (%s)",
                 sorted(MOUSE_BUTTONS) or "off",
                 "clicks swallowed" if rk.hook else "clicks pass through")
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
                    audio, language="en", beam_size=1, vad_filter=True
                )
                text = " ".join(s.text.strip() for s in segments).strip()
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
