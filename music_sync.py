"""
Mi Bedside Lamp 2 - music reactive lighting (engine + command line).

The window app (app.py / MiLampSync.exe) uses the same Engine. To use the CLI:
    python music_sync.py                 # runs until you quit (max 120 min by default)
    python music_sync.py --minutes 30    # auto-stop after 30 minutes
    python music_sync.py --mode classic  # bass->brightness, pitch->hue instead of beat color steps
    python music_sync.py --blink every   # dip to dark on each beat, new color comes up
    python music_sync.py --blink alternate  # same, every other beat
    python music_sync.py --dry-run       # analyse audio and print values, never touch the lamp

Stop it any time with:  q  (or Esc)  or  Ctrl+C.

Guardrails:
  * Foreground only. No background service, no autostart, no scheduled task.
  * One instance at a time (lock file). A second copy refuses to start.
  * Hard runtime cap (--minutes, default 120) so it can never loop forever.
  * Stops by itself after repeated lamp errors, or after a long stretch of silence.
  * On exit (quit, Ctrl+C, error, window closed) the lamp is restored to the state it
    had before the script started.
  * Audio is only *read* through WASAPI loopback (a copy of the output signal). It never
    changes your volume, device, or what you hear.
"""
import argparse
import colorsys
import os
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np
import soundcard as sc

# When packaged with PyInstaller the .env / lock / settings live next to the .exe.
HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
LOCK_PATH = os.path.join(HERE, ".music_sync.lock")
ENV_PATH = os.path.join(HERE, ".env")

# ---- tuning ---------------------------------------------------------------
SAMPLE_RATE = 44100
CHUNK = 2048                 # samples per analysis window (~46 ms)
MAX_SEND_HZ = 8.0            # lamp update rate ceiling (measured safe range: ~9-20/s)
MIN_BRIGHTNESS = 0.08        # keeps the lamp from going fully dark between beats
HUE_RANGE = (0.60, 0.00)     # low-pitched sound -> blue (0.60), bright sound -> red (0.0)
SMOOTH_HUE = 0.15            # 0..1, higher = faster color changes
SMOOTH_LEVEL_UP = 0.6        # fast attack
SMOOTH_LEVEL_DOWN = 0.15     # slower release
BEAT_HUE_STEP = 0.14         # hue jump per detected beat (0.14 ~ 50 degrees round the wheel)
BEAT_SENSITIVITY = 1.3       # bass must exceed its recent average by this factor (lower = more beats)
BEAT_MIN_GAP = 0.25          # seconds between beats (caps detection at 240 bpm)
BEAT_DRIFT = 0.0015          # slow hue drift per audio chunk, scaled by loudness
BEAT_BRIGHTNESS_FLOOR = 0.55 # in beats mode the lamp stays at least this bright
BLINK_DIP_MS = 70            # how long the lamp sits at ~1% (near dark) before the new color rises
BLINK_RISE_MS = 130          # how long the new color takes to come up to full
POLL_SECONDS = 0.01          # main loop poll, so a beat is acted on within ~10 ms
SILENCE_RMS = 1e-4
SILENCE_STOP_MIN = 10        # quit after this many minutes of continuous silence
MAX_CONSECUTIVE_LAMP_ERRORS = 5
# ---------------------------------------------------------------------------


class AlreadyRunning(Exception):
    pass


def load_env():
    """Return {LAMP_IP, LAMP_TOKEN, ...} from the .env next to the script/exe (empty if missing)."""
    values = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            for line in f:
                if "=" in line and not line.lstrip().startswith("#"):
                    k, v = line.strip().split("=", 1)
                    values[k] = v
    return values


def save_env(ip, token):
    with open(ENV_PATH, "w") as f:
        f.write(f"LAMP_IP={ip}\nLAMP_TOKEN={token}\n")


def pid_alive(pid):
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def acquire_lock():
    if os.path.exists(LOCK_PATH):
        try:
            pid = int(open(LOCK_PATH).read().strip())
        except ValueError:
            pid = 0
        if pid and pid != os.getpid() and pid_alive(pid):
            raise AlreadyRunning(f"music sync is already running (PID {pid}). Quit it first.")
    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))


def release_lock():
    try:
        os.remove(LOCK_PATH)
    except OSError:
        pass


@dataclass
class Config:
    """Settings the Engine reads live, so the window can change them while running."""
    mode: str = "beats"            # "beats" | "classic"
    blink: str = "none"            # "none" | "every" | "alternate"  (beats mode only)
    minutes: float = 120.0         # hard runtime cap
    threshold: float = BEAT_SENSITIVITY
    dry_run: bool = False          # analyse audio only, never touch the lamp


class Analyzer(threading.Thread):
    """Reads loopback audio and keeps the latest (level, hue) in shared state."""

    def __init__(self, stop, cfg):
        super().__init__(daemon=True)
        self.stop = stop
        self.cfg = cfg
        self.level = 0.0      # 0..1 smoothed loudness, bass-weighted
        self.hue = HUE_RANGE[0]
        self.silent_since = None
        self.error = None
        self.device = sc.get_microphone(sc.default_speaker().name, include_loopback=True)
        self.beat_hue = 0.60  # hue that steps forward on every beat (beats mode)
        self.beats = 0
        self._bass_avg = 0.0
        self._bass_peak = 1e-3
        self._last_beat = 0.0
        self._peak = 1e-3     # auto-gain reference, decays slowly
        self._freqs = np.fft.rfftfreq(CHUNK, 1 / SAMPLE_RATE)
        self._window = np.hanning(CHUNK)

    def run(self):
        try:
            with self.device.recorder(samplerate=SAMPLE_RATE, channels=2, blocksize=CHUNK) as rec:
                while not self.stop.is_set():
                    data = rec.record(numframes=CHUNK)
                    self._process(data.mean(axis=1))
        except Exception as e:  # noqa: BLE001 - surface any capture failure to the engine
            self.error = e

    def _process(self, mono):
        rms = float(np.sqrt(np.mean(mono ** 2)))
        if rms < SILENCE_RMS:
            self.silent_since = self.silent_since or time.time()
            self.level += (0 - self.level) * SMOOTH_LEVEL_DOWN
            return
        self.silent_since = None

        spec = np.abs(np.fft.rfft(mono * self._window))
        f = self._freqs
        bass = spec[(f >= 20) & (f < 150)].sum()
        total = spec[(f >= 20) & (f < 8000)].sum() + 1e-9

        # beat detection: bass spike over its recent average, with a refractory gap
        now = time.time()
        self._bass_peak = max(self._bass_peak * 0.999, bass)
        if (bass > self._bass_avg * self.cfg.threshold and bass > 0.25 * self._bass_peak
                and now - self._last_beat > BEAT_MIN_GAP):
            self._last_beat = now
            self.beats += 1
            self.beat_hue = (self.beat_hue + BEAT_HUE_STEP) % 1.0
        self._bass_avg += (bass - self._bass_avg) * 0.1
        self.beat_hue = (self.beat_hue + BEAT_DRIFT * self.level) % 1.0

        # level: bass-weighted energy, auto-gained against a slowly decaying peak
        raw = bass * 0.7 + total * 0.3
        self._peak = max(self._peak * 0.999, raw)
        target = min(1.0, raw / self._peak)
        k = SMOOTH_LEVEL_UP if target > self.level else SMOOTH_LEVEL_DOWN
        self.level += (target - self.level) * k

        # hue: spectral centroid on a log scale (200 Hz .. 4 kHz)
        band = (f >= 20) & (f < 8000)
        centroid = float((f[band] * spec[band]).sum() / (spec[band].sum() + 1e-9))
        pos = np.clip((np.log10(max(centroid, 200)) - np.log10(200)) / (np.log10(4000) - np.log10(200)), 0, 1)
        target_hue = HUE_RANGE[0] + (HUE_RANGE[1] - HUE_RANGE[0]) * pos
        self.hue += (target_hue - self.hue) * SMOOTH_HUE


def snapshot(lamp):
    s = lamp.status()
    return {"on": s.is_on, "mode": s.color_mode, "rgb": s.rgb, "ct": s.color_temp, "bri": s.brightness}


def restore(lamp, snap, log):
    try:
        if not snap["on"]:
            lamp.off()
            return
        lamp.on()
        if snap["mode"] == 2 and snap["ct"]:
            lamp.set_color_temp(snap["ct"])
        elif snap["rgb"]:
            lamp.set_rgb(tuple(snap["rgb"]))
        else:
            lamp.set_color_temp(3000)
        lamp.set_brightness(snap["bri"])
        log("Lamp restored to its previous state.")
    except Exception as e:  # noqa: BLE001
        log(f"Could not restore lamp ({e}). Set it from Mi Home if needed.")


class Engine:
    """Runs one music-sync session on a background thread. Create a new Engine per session."""

    def __init__(self, ip, token, cfg, log=print):
        self.ip, self.token, self.cfg, self.log = ip, token, cfg, log
        self.stop_event = threading.Event()
        self.thread = None
        self.analyzer = None
        self.running = False
        self.finish_reason = None   # set when the session ends
        # live status for the UI
        self.level, self.beats, self.blinks, self.rgb = 0.0, 0, 0, (0, 0, 0)
        self.device_name = ""

    def start(self):
        acquire_lock()  # raises AlreadyRunning
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self, timeout=10):
        """Ask the session to end and wait until the lamp has been restored."""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)

    def _run(self):
        lamp = snap = None
        reason = "stopped"
        try:
            if not self.cfg.dry_run:
                from miio import Yeelight
                self.log(f"Connecting to lamp at {self.ip} ...")
                lamp = Yeelight(self.ip, self.token)
                snap = snapshot(lamp)
                lamp.on()
                lamp.set_brightness(100)

            self.analyzer = Analyzer(self.stop_event, self.cfg)
            self.analyzer.start()
            self.device_name = self.analyzer.device.name
            self.log(f"Capturing: {self.device_name}")
            reason = self._loop(lamp)
        except Exception as e:  # noqa: BLE001
            reason = f"error: {e}"
        finally:
            self.stop_event.set()
            if lamp and snap:
                restore(lamp, snap, self.log)
            release_lock()
            self.finish_reason = reason
            self.running = False
            self.log(f"Stopped ({reason}).")

    def _loop(self, lamp):
        an, cfg = self.analyzer, self.cfg
        deadline = time.time() + cfg.minutes * 60
        interval = 1 / MAX_SEND_HZ
        errors, last_send, hold_until, seen_beats = 0, 0.0, 0.0, 0
        while not self.stop_event.is_set():
            t0 = time.time()
            if t0 >= deadline:
                return "runtime cap reached"
            if an.error:
                return f"audio capture failed: {an.error}"
            if an.silent_since and t0 - an.silent_since > SILENCE_STOP_MIN * 60:
                return f"no audio for {SILENCE_STOP_MIN} min"

            if cfg.mode == "beats":
                floor = BEAT_BRIGHTNESS_FLOOR if an.silent_since is None else MIN_BRIGHTNESS
                v = floor + (1 - floor) * an.level
                hue = an.beat_hue
            else:
                v = MIN_BRIGHTNESS + (1 - MIN_BRIGHTNESS) * an.level
                hue = an.hue
            r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue % 1.0, 1.0, v))
            r = max(r, 1)
            self.level, self.beats, self.rgb = an.level, an.beats, (r, g, b)

            # a new beat that should blink: dip the lamp to ~dark and rise in the new color
            # with ONE on-device flow command, so there are no extra network round trips.
            new_beat = an.beats != seen_beats
            seen_beats = an.beats
            blink = (new_beat and cfg.mode == "beats" and cfg.blink != "none"
                     and (cfg.blink == "every" or an.beats % 2 == 0))

            if blink or (t0 - last_send >= interval and t0 >= hold_until):
                last_send = t0
                if blink:
                    self.blinks += 1
                if lamp:
                    try:
                        if blink:
                            c = (r << 16) | (g << 8) | b
                            lamp.raw_command("start_cf", [2, 1, f"{BLINK_DIP_MS},1,{c},1,{BLINK_RISE_MS},1,{c},100"])
                            hold_until = t0 + (BLINK_DIP_MS + BLINK_RISE_MS) / 1000 + 0.05
                        else:
                            lamp.set_rgb((r, g, b))
                        errors = 0
                    except Exception as e:  # noqa: BLE001
                        errors += 1
                        if errors >= MAX_CONSECUTIVE_LAMP_ERRORS:
                            return f"lamp unreachable ({e})"
            time.sleep(POLL_SECONDS)
        return "stopped"


def quit_pressed():
    import msvcrt
    while msvcrt.kbhit():
        if msvcrt.getwch().lower() in ("q", "\x1b"):
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=120, help="hard runtime cap (default 120)")
    ap.add_argument("--mode", choices=["beats", "classic"], default="beats",
                    help="beats: color steps on each beat (default); classic: bass->brightness, pitch->hue")
    ap.add_argument("--blink", choices=["none", "every", "alternate"], default="none",
                    help="beats mode only: dip the lamp to dark on every beat / every other beat, "
                         "then bring the new color up (default none)")
    ap.add_argument("--dry-run", action="store_true", help="analyse audio only, never send to lamp")
    args = ap.parse_args()

    env = load_env()
    cfg = Config(mode=args.mode, blink=args.blink, minutes=args.minutes, dry_run=args.dry_run)
    engine = Engine(env.get("LAMP_IP", ""), env.get("LAMP_TOKEN", ""), cfg)
    try:
        engine.start()
    except AlreadyRunning as e:
        sys.exit(str(e))

    print(f"Running (max {args.minutes:g} min). Press q or Esc to quit, or Ctrl+C.")
    last_print = 0.0
    try:
        while engine.running:
            if quit_pressed():
                print("Quit requested.")
                break
            if args.dry_run and time.time() - last_print > 0.25:
                print(f"level={engine.level:4.2f} beats={engine.beats} blinks={engine.blinks} rgb={engine.rgb}")
                last_print = time.time()
            time.sleep(0.05)
    except KeyboardInterrupt:
        print("\nInterrupted.")
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
