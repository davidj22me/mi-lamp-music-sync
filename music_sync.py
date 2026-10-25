"""
Mi Bedside Lamp 2 - music reactive lighting (engine + command line).

The window app (app.py / MiLampSync.exe) uses the same Engine. To use the CLI:
    python music_sync.py                    # runs until you quit (max 120 min by default)
    python music_sync.py --max-runtime 30   # auto-stop after 30 minutes
    python music_sync.py --gentle           # slower, dimmer, no blinking
    python music_sync.py --mode classic     # bass->brightness, pitch->hue instead of beat color steps
    python music_sync.py --blink every      # opt-in: dip to dark on each beat, new color comes up
    python music_sync.py --blink alternate  # same, every other beat
    python music_sync.py --restore warm     # on exit set warm white instead of the previous state
    python music_sync.py --off-at-exit      # turn the lamp off on exit (default: leave it on)
    python music_sync.py --dry-run          # analyse audio and print values, never touch the lamp

Stop it any time with:  q  (or Esc)  or  Ctrl+C.

Safety limits (brightness cap, update rate, flash rate, ...) live in lamp_config.ini and are enforced
in lamp_safety.py. Other guardrails:
  * Foreground only. No background service, no autostart, no scheduled task.
  * One instance at a time (lock file). A second copy refuses to start.
  * Hard runtime cap, plus auto-stop after a long stretch of silence or repeated lamp errors.
  * On exit (quit, Ctrl+C, error, window closed) the lamp is restored; it is never switched off
    unless --off-at-exit is given.
  * Audio is only *read* through WASAPI loopback (a copy of the output signal). It never
    changes your volume, device, or what you hear.
"""
import argparse
import colorsys
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import soundcard as sc

import lamp_safety as ls

# When packaged with PyInstaller the .env / lock / settings live next to the .exe.
HERE = ls.HERE
LOCK_PATH = os.path.join(HERE, ".music_sync.lock")
ENV_PATH = os.path.join(HERE, ".env")

# ---- audio / look tuning (safety limits are in lamp_config.ini) ----------------------------------
SAMPLE_RATE = 44100
CHUNK = 2048                 # samples per analysis window (~46 ms)
MIN_BRIGHTNESS = 0.08        # keeps the lamp from going fully dark between beats (fraction of the cap)
HUE_RANGE = (0.60, 0.00)     # low-pitched sound -> blue (0.60), bright sound -> red (0.0)
SMOOTH_HUE = 0.15            # 0..1, higher = faster color changes
SMOOTH_LEVEL_UP = 0.6        # fast attack
SMOOTH_LEVEL_DOWN = 0.15     # slower release
GENTLE_LEVEL_UP = 0.25       # gentle mode: slower attack ...
GENTLE_LEVEL_DOWN = 0.06     # ... and release
BEAT_HUE_STEP = 0.14         # hue jump per detected beat (0.14 ~ 50 degrees round the wheel)
BEAT_SENSITIVITY = 1.3       # bass must exceed its recent average by this factor (lower = more beats)
BEAT_DRIFT = 0.0015          # slow hue drift per audio chunk, scaled by loudness
BEAT_BRIGHTNESS_FLOOR = 0.55 # in beats mode the lamp stays at least this bright (fraction of the cap)
BLINK_DIP_MS = 70            # how long the lamp sits at the dip brightness before the new color rises
BLINK_RISE_MS = 130          # how long the new color takes to come up
POLL_SECONDS = 0.01          # main loop poll, so a beat is acted on within ~10 ms
SILENCE_RMS = 1e-4
SILENCE_STOP_MIN = 10        # quit after this many minutes of continuous silence
# ---------------------------------------------------------------------------------------------------


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
    blink: str = "none"            # "none" | "every" | "alternate"  (beats mode, opt-in, not in gentle)
    minutes: Optional[float] = None  # runtime cap; None = max_runtime_minutes from lamp_config.ini
    threshold: float = BEAT_SENSITIVITY
    dry_run: bool = False          # analyse audio only, never touch the lamp
    gentle: bool = False           # slower smoothing, lower brightness/rate, no blinking
    restore: str = "previous"      # "previous" | "warm"  (what the lamp does on exit)
    off_at_exit: bool = False      # only way the script ever calls off()


class Analyzer(threading.Thread):
    """Reads loopback audio and keeps the latest (level, hue) in shared state."""

    def __init__(self, stop, cfg, limits):
        super().__init__(daemon=True)
        self.stop = stop
        self.cfg = cfg
        self.min_beat_gap = 1 / limits.max_flashes_per_sec   # beat color changes count as flashes
        self.up, self.down = ((GENTLE_LEVEL_UP, GENTLE_LEVEL_DOWN) if cfg.gentle
                              else (SMOOTH_LEVEL_UP, SMOOTH_LEVEL_DOWN))
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
            self.level += (0 - self.level) * self.down
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
                and now - self._last_beat > self.min_beat_gap):
            self._last_beat = now
            self.beats += 1
            self.beat_hue = (self.beat_hue + BEAT_HUE_STEP) % 1.0
        self._bass_avg += (bass - self._bass_avg) * 0.1
        self.beat_hue = (self.beat_hue + BEAT_DRIFT * self.level) % 1.0

        # level: bass-weighted energy, auto-gained against a slowly decaying peak
        raw = bass * 0.7 + total * 0.3
        self._peak = max(self._peak * 0.999, raw)
        target = min(1.0, raw / self._peak)
        k = self.up if target > self.level else self.down
        self.level += (target - self.level) * k

        # hue: spectral centroid on a log scale (200 Hz .. 4 kHz)
        band = (f >= 20) & (f < 8000)
        centroid = float((f[band] * spec[band]).sum() / (spec[band].sum() + 1e-9))
        pos = np.clip((np.log10(max(centroid, 200)) - np.log10(200)) / (np.log10(4000) - np.log10(200)), 0, 1)
        target_hue = HUE_RANGE[0] + (HUE_RANGE[1] - HUE_RANGE[0]) * pos
        self.hue += (target_hue - self.hue) * SMOOTH_HUE


class Engine:
    """Runs one music-sync session on a background thread. Create a new Engine per session.

    `raw_factory` / `analyzer_factory` exist so tests can substitute a fake lamp and fake audio.
    """

    def __init__(self, ip, token, cfg, log=print, limits=None, raw_factory=None, analyzer_factory=None):
        self.ip, self.token, self.cfg, self.log = ip, token, cfg, log
        self.limits = limits or ls.load_limits()
        if cfg.gentle:
            self.limits = self.limits.gentle()
        self.raw_factory = raw_factory or self._default_raw
        self.analyzer_factory = analyzer_factory or (lambda stop: Analyzer(stop, self.cfg, self.limits))
        self.stop_event = threading.Event()
        self.thread = None
        self.analyzer = None
        self.lamp = None
        self.running = False
        self.finish_reason = None   # set when the session ends
        # live status for the UI
        self.level, self.beats, self.blinks, self.rgb = 0.0, 0, 0, (0, 0, 0)
        self.device_name = ""

    def _default_raw(self):
        from miio import Yeelight
        return Yeelight(self.ip, self.token)

    def start(self):
        acquire_lock()  # raises AlreadyRunning
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self, timeout=15):
        """Ask the session to end and wait until the lamp has been restored."""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout)

    def _run(self):
        reason = "stopped"
        try:
            self.log(ls.PHOTOSENSITIVITY_NOTICE)
            self.log(f"Limits: {self.limits.describe()}" + (" (gentle mode)" if self.cfg.gentle else ""))
            if not self.cfg.dry_run:
                self.log(f"Connecting to lamp at {self.ip} ...")
                self.lamp = ls.SafeLamp(self.raw_factory(), self.limits, log=self.log)
                self.lamp.start_session()

            self.analyzer = self.analyzer_factory(self.stop_event)
            self.analyzer.start()
            self.device_name = self.analyzer.device.name
            self.log(f"Capturing: {self.device_name}")
            reason = self._loop()
        except BaseException as e:  # noqa: BLE001 - incl. KeyboardInterrupt: always fall through to restore
            reason = "interrupted" if isinstance(e, KeyboardInterrupt) else f"error: {e}"
        finally:
            self.stop_event.set()
            if self.lamp:
                self.lamp.finish(self.cfg.restore, self.cfg.off_at_exit)
            release_lock()
            self.finish_reason = reason
            self.running = False
            self.log(f"Stopped ({reason}).")

    def _loop(self):
        an, cfg, lamp, lim = self.analyzer, self.cfg, self.lamp, self.limits
        minutes = cfg.minutes or lim.max_runtime_minutes
        deadline = time.time() + minutes * 60
        interval = 1 / lim.max_updates_per_sec
        step = lim.brightness_step / 100
        last_send, hold_until, seen_beats, last_v = 0.0, 0.0, 0, None
        blink_allowed = cfg.mode == "beats" and cfg.blink != "none" and not cfg.gentle
        cap = lim.max_brightness / 100
        while not self.stop_event.is_set():
            t0 = time.time()
            if t0 >= deadline:
                return "runtime cap reached"
            if an.error:
                return f"audio capture failed: {an.error}"
            if lamp and lamp.failed:
                return lamp.failed
            if an.silent_since and t0 - an.silent_since > SILENCE_STOP_MIN * 60:
                return f"no audio for {SILENCE_STOP_MIN} min"

            if cfg.mode == "beats":
                floor = BEAT_BRIGHTNESS_FLOOR if an.silent_since is None else MIN_BRIGHTNESS
                v = floor + (1 - floor) * an.level
                hue = an.beat_hue
            else:
                v = MIN_BRIGHTNESS + (1 - MIN_BRIGHTNESS) * an.level
                hue = an.hue
            self.level, self.beats = an.level, an.beats

            new_beat = an.beats != seen_beats
            seen_beats = an.beats
            blink = (new_beat and blink_allowed
                     and (cfg.blink == "every" or an.beats % 2 == 0))

            if blink:
                # blink: dip to the dip brightness and rise in the new color. The dip is the one
                # explicit exception to the brightness-step rule; it is opt-in and rate limited.
                r, g, b = (max(int(c * 255), 1) for c in colorsys.hsv_to_rgb(hue % 1.0, 1.0, v))
                self.rgb = (r, g, b)
                if lamp is None or lamp.submit_blink((r, g, b), BLINK_DIP_MS, BLINK_RISE_MS):
                    self.blinks += 1
                    hold_until = t0 + (BLINK_DIP_MS + BLINK_RISE_MS) / 1000 + 0.05
                    last_send = t0
                    last_v = v
            elif t0 - last_send >= interval and t0 >= hold_until:
                last_send = t0
                if last_v is not None:   # no instant full-range jumps
                    v = last_v + max(-step / cap, min(step / cap, v - last_v))
                last_v = v
                r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue % 1.0, 1.0, v))
                r = max(r, 1)
                self.rgb = (r, g, b)
                if lamp:
                    lamp.submit_color((r, g, b))
            time.sleep(POLL_SECONDS)
        return "stopped"


def quit_pressed():
    import msvcrt
    while msvcrt.kbhit():
        if msvcrt.getwch().lower() in ("q", "\x1b"):
            return True
    return False


def _install_exit_handlers(engine):
    """Restore the lamp when the console is closed or the process is terminated."""
    def _terminate(*_):
        raise KeyboardInterrupt
    for name in ("SIGTERM", "SIGBREAK"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _terminate)
    try:
        import ctypes
        handler_t = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

        def _on_console_event(event):
            if event in (2, 5, 6):  # CLOSE, LOGOFF, SHUTDOWN
                engine.stop()
            return 0
        engine._ctrl_handler = handler_t(_on_console_event)  # keep a reference
        ctypes.windll.kernel32.SetConsoleCtrlHandler(engine._ctrl_handler, True)
    except Exception:  # noqa: BLE001 - best effort only
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-runtime", "--minutes", dest="minutes", type=float, default=None,
                    help="hard runtime cap in minutes (default: max_runtime_minutes in lamp_config.ini, 120)")
    ap.add_argument("--mode", choices=["beats", "classic"], default="beats",
                    help="beats: color steps on each beat (default); classic: bass->brightness, pitch->hue")
    ap.add_argument("--blink", choices=["none", "every", "alternate"], default="none",
                    help="OPT-IN, beats mode only, ignored in --gentle: dip the lamp to dark on every beat / "
                         "every other beat, then bring the new color up (default none)")
    ap.add_argument("--gentle", action="store_true",
                    help="slower smoothing, lower brightness cap and update rate, no blinking")
    ap.add_argument("--restore", choices=["previous", "warm"], default="previous",
                    help="on exit: put the lamp back as it was (default) or set warm white")
    ap.add_argument("--off-at-exit", action="store_true",
                    help="turn the lamp off when the script ends (default: leave it on)")
    ap.add_argument("--dry-run", action="store_true", help="analyse audio only, never send to lamp")
    args = ap.parse_args()

    ls.ensure_config_file()
    env = load_env()
    cfg = Config(mode=args.mode, blink=args.blink, minutes=args.minutes, dry_run=args.dry_run,
                 gentle=args.gentle, restore=args.restore, off_at_exit=args.off_at_exit)
    engine = Engine(env.get("LAMP_IP", ""), env.get("LAMP_TOKEN", ""), cfg)
    try:
        engine.start()
    except AlreadyRunning as e:
        sys.exit(str(e))
    _install_exit_handlers(engine)

    print("Press q or Esc to quit, or Ctrl+C.")
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
