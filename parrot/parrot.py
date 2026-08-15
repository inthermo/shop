"""Parrot for Windows — push-to-talk local dictation.

Hold Ctrl+Win, speak, release: the transcription is typed at the cursor.
Fully on-device (faster-whisper small.en, CPU). No API keys, no cloud.

Inspired by https://github.com/digimata/parrot (macOS). This is the
Windows equivalent: keyboard hook + sounddevice + faster-whisper.

Run with pythonw.exe for no console window. Logs to parrot.log next to
this file. Beeps: high = recording, low = stopped, two rising = ready,
buzz = nothing transcribed.
"""

import logging
import os
import sys
import time

import numpy as np
import sounddevice as sd
import keyboard
import winsound

SAMPLE_RATE = 16000
MIN_SECONDS = 0.35          # ignore accidental taps
# Override per machine with a user env var PARROT_MODEL (e.g. "base.en" on slow PCs,
# ~3x faster than small.en with slightly lower accuracy; "tiny.en" faster still).
MODEL_NAME = os.environ.get("PARROT_MODEL", "small.en")  # small.en = ~250 MB download on first run
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "parrot.log")

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("parrot")

WIN_KEYS = ("left windows", "right windows")


def win_pressed():
    for k in WIN_KEYS:
        try:
            if keyboard.is_pressed(k):
                return True
        except ValueError:
            pass
    return False


def combo_pressed():
    return keyboard.is_pressed("ctrl") and win_pressed()


def beep(freq, ms):
    try:
        winsound.Beep(freq, ms)
    except Exception:
        pass


def main():
    log.info("starting; loading model %s", MODEL_NAME)
    from faster_whisper import WhisperModel  # slow import; after logging is up

    model = WhisperModel(MODEL_NAME, device="cpu", compute_type="int8")
    # Warm up so the first real dictation isn't slow.
    model.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), language="en")
    log.info("model ready")
    beep(700, 90)
    beep(1000, 90)

    while True:
        if not combo_pressed():
            time.sleep(0.02)
            continue

        # A key event while Win is held stops Windows opening the Start
        # menu when Win is released.
        keyboard.press_and_release("f24")

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
            continue
        audio = np.concatenate(frames).flatten()
        seconds = len(audio) / SAMPLE_RATE
        if seconds < MIN_SECONDS:
            continue

        try:
            t0 = time.time()
            segments, _ = model.transcribe(
                audio, language="en", beam_size=1, vad_filter=True
            )
            text = " ".join(s.text.strip() for s in segments).strip()
            log.info("%.1fs audio -> %.1fs transcribe: %r", seconds, time.time() - t0, text)
        except Exception:
            log.exception("transcription failed")
            beep(200, 400)
            continue

        if not text:
            beep(300, 150)
            continue

        # Never type while modifiers are still down — Ctrl+<letters> would
        # fire shortcuts in the focused app.
        while combo_pressed() or keyboard.is_pressed("ctrl") or win_pressed():
            time.sleep(0.02)
        time.sleep(0.12)
        keyboard.write(text + " ")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log.exception("fatal")
        sys.exit(1)
