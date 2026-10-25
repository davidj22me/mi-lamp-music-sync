"""
Safety layer for the Mi Bedside Lamp 2. Every script sends lamp commands through SafeLamp so the
limits below cannot be bypassed by accident. See lamp_config.ini for the (commented) limits.

Rules enforced here:
  * saved-state writes are switched off for the session (never re-enabled automatically)
  * every brightness value is clamped to max_brightness (one function: clamp_brightness)
  * near-white colors (all channels high) are scaled down
  * commands are rate limited, latest value wins, no queue
  * flashes (blinks) are limited per second
  * power is never toggled for effects; off() is only called with an explicit flag
  * the lamp's previous state is restored on exit; repeated failures stop the session
"""
import configparser
import logging
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, replace

dlog = logging.getLogger("lamp_safety")

HERE = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "lamp_config.ini")

PHOTOSENSITIVITY_NOTICE = ("Rapid flashing can be uncomfortable or unsafe for people with photosensitivity. "
                           "Use gentle mode and moderate brightness near bed.")
MAX_CONSECUTIVE_FAILURES = 5
WARM_WHITE_KELVIN = 2700

DEFAULT_CONFIG_TEXT = """\
# Mi Lamp Sync - safety limits. Edit, then restart the app. Missing or invalid values fall back
# to the defaults shown here, and values are clamped to safe ranges.

[limits]
# Hard brightness ceiling in percent (1-100). Applied to every brightness value the lamp is sent,
# at all times of day.
max_brightness = 80

# Brightness (percent) of the dark part of a blink-on-beat dip. Never goes below this.
dip_brightness = 5

# Measured: not yet recorded. Run `python lamp_test.py speed`; it records the measured rate here
# and sets the value below to about 70% of it, leaving headroom for the lamp.
# Maximum lamp commands per second (0.5-20). If a command arrives early, older pending ones are
# dropped and only the latest is sent. Nothing is ever queued.
max_updates_per_sec = 4

# Largest brightness change allowed between two consecutive updates, in percentage points.
# (The blink dip is the one explicit exception.)
brightness_step = 25

# Maximum color/brightness "flashes" per second (hard limit 3). Applies to beat color changes and blinks.
max_flashes_per_sec = 3

# Hard stop for a session, in minutes. The lamp is restored when it is reached.
max_runtime_minutes = 120

# Colors whose red, green and blue channels are ALL above this fraction are scaled down, to avoid
# sustained full-power white.
white_limit = 0.85

[gentle]
# Gentle mode (--gentle / "Gentle mode" checkbox): slower smoothing, lower ceiling, lower rate, no blinking.
max_brightness = 50
rate_factor = 0.5
max_flashes_per_sec = 2
"""


@dataclass(frozen=True)
class Limits:
    max_brightness: int = 80
    dip_brightness: int = 5
    max_updates_per_sec: float = 4.0
    brightness_step: int = 25
    max_flashes_per_sec: float = 3.0
    max_runtime_minutes: float = 120.0
    white_limit: float = 0.85
    gentle_max_brightness: int = 50
    gentle_rate_factor: float = 0.5
    gentle_max_flashes_per_sec: float = 2.0

    def gentle(self):
        mb = min(self.max_brightness, self.gentle_max_brightness)
        return replace(
            self,
            max_brightness=mb,
            dip_brightness=min(self.dip_brightness, mb),
            max_updates_per_sec=max(0.5, self.max_updates_per_sec * self.gentle_rate_factor),
            max_flashes_per_sec=min(self.max_flashes_per_sec, self.gentle_max_flashes_per_sec),
        )

    def describe(self):
        return (f"max brightness {self.max_brightness}%, dip {self.dip_brightness}%, "
                f"<= {self.max_updates_per_sec:g} updates/s, <= {self.max_flashes_per_sec:g} flashes/s, "
                f"step <= {self.brightness_step} pts, runtime <= {self.max_runtime_minutes:g} min")


def _num(cp, section, key, default, lo, hi):
    try:
        v = float(cp.get(section, key))
    except (configparser.Error, ValueError):
        return default
    return max(lo, min(hi, v))


def ensure_config_file(path=CONFIG_PATH):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(DEFAULT_CONFIG_TEXT)


def load_limits(path=CONFIG_PATH):
    """Read lamp_config.ini (defaults if missing/invalid), clamping every value to a safe range."""
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
    except configparser.Error as e:
        dlog.warning("could not parse %s (%s); using defaults", path, e)
        cp = configparser.ConfigParser()
    d = Limits()
    mb = int(_num(cp, "limits", "max_brightness", d.max_brightness, 1, 100))
    return Limits(
        max_brightness=mb,
        dip_brightness=min(int(_num(cp, "limits", "dip_brightness", d.dip_brightness, 1, 100)), mb),
        max_updates_per_sec=_num(cp, "limits", "max_updates_per_sec", d.max_updates_per_sec, 0.5, 20),
        brightness_step=int(_num(cp, "limits", "brightness_step", d.brightness_step, 1, 100)),
        max_flashes_per_sec=_num(cp, "limits", "max_flashes_per_sec", d.max_flashes_per_sec, 0.5, 3),
        max_runtime_minutes=_num(cp, "limits", "max_runtime_minutes", d.max_runtime_minutes, 1, 600),
        white_limit=_num(cp, "limits", "white_limit", d.white_limit, 0.5, 1),
        gentle_max_brightness=int(_num(cp, "gentle", "max_brightness", d.gentle_max_brightness, 1, 100)),
        gentle_rate_factor=_num(cp, "gentle", "rate_factor", d.gentle_rate_factor, 0.1, 1),
        gentle_max_flashes_per_sec=_num(cp, "gentle", "max_flashes_per_sec", d.gentle_max_flashes_per_sec, 0.5, 3),
    )


def write_measured_rate(measured, path=CONFIG_PATH):
    """Record a `lamp_test.py speed` result and set max_updates_per_sec to ~70% of it."""
    ensure_config_file(path)
    value = round(max(0.5, min(20.0, measured * 0.7)), 1)
    with open(path, encoding="utf-8") as f:
        text = f.read()
    note = f"# Measured: {measured:.1f} updates/sec on {time.strftime('%Y-%m-%d')}; using ~70% = {value:g} for headroom."
    text = re.sub(r"(?m)^# Measured:.*$", note, text, count=1)
    text = re.sub(r"(?m)^max_updates_per_sec\s*=.*$", f"max_updates_per_sec = {value:g}", text, count=1)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return value


def clamp_brightness(pct, limits):
    """The one place brightness is limited. Always returns an int in [1, max_brightness]."""
    return int(max(1, min(limits.max_brightness, round(pct))))


def clamp_rgb(rgb, limits):
    """Clamp channels to 0-255 and scale down colors whose R, G and B are all high (near white)."""
    r, g, b = (int(max(0, min(255, c))) for c in rgb)
    ceiling = limits.white_limit * 255
    if min(r, g, b) > ceiling:
        k = ceiling / max(r, g, b)
        r, g, b = int(r * k), int(g * k), int(b * k)
    return r, g, b


class SafeLamp:
    """Wraps a python-miio Yeelight (or a test double). All lamp I/O goes through here."""

    def __init__(self, raw, limits, log=print, clock=time.monotonic):
        self.raw, self.limits, self.say, self._clock = raw, limits, log, clock
        self.rate = limits.max_updates_per_sec
        self.sent = self.dropped = self.failures = 0
        self.failed = None            # set to a message when the session must stop
        self.snap = None
        self._cond = threading.Condition()
        self._io = threading.Lock()
        self._pending = None
        self._closed = False
        self._last_send = float("-inf")
        self._last_flash = float("-inf")
        self._thread = None

    # -- session ------------------------------------------------------------------------------
    def start_session(self):
        s = self.raw.status()
        self.snap = {"on": s.is_on, "mode": s.color_mode, "rgb": s.rgb, "ct": s.color_temp, "bri": s.brightness}
        try:
            self._paced(self.raw.set_save_state_on_change, False)
        except Exception as e:  # noqa: BLE001 - not supported on every model; carry on
            self.say(f"Warning: could not disable saved-state writes ({e}).")
        if not s.is_on:
            self._paced(self.raw.on)
        self.set_brightness(self.limits.max_brightness)
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()

    def finish(self, restore="previous", off_at_exit=False):
        """Stop sending and put the lamp back. Never turns it off unless off_at_exit is set."""
        self.close()
        if self.snap is None:
            return
        snap, lim = self.snap, self.limits
        self.rate = lim.max_updates_per_sec  # restore is paced at the configured rate
        steps = [("power on", self.raw.on)]
        if restore == "warm":
            steps.append(("warm white", lambda: self.raw.set_color_temp(WARM_WHITE_KELVIN)))
        elif snap["mode"] == 2 and snap["ct"]:
            steps.append(("color temperature", lambda: self.raw.set_color_temp(snap["ct"])))
        elif snap["rgb"]:
            steps.append(("color", lambda: self.raw.set_rgb(clamp_rgb(snap["rgb"], lim))))
        else:
            steps.append(("color temperature", lambda: self.raw.set_color_temp(3000)))
        steps.append(("brightness", lambda: self.raw.set_brightness(clamp_brightness(snap["bri"] or lim.max_brightness, lim))))
        if off_at_exit:
            steps.append(("power off", self.raw.off))
        ok = True
        for label, fn in steps:
            try:
                self._paced(fn)
            except Exception as e:  # noqa: BLE001
                ok = False
                self.say(f"Could not restore lamp ({label}: {e}). Set it from Mi Home if needed.")
        if ok:
            self.say("Lamp restored." if restore == "previous" else "Lamp set to warm white.")

    def close(self):
        with self._cond:
            self._closed = True
            self._pending = None
            self._cond.notify_all()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(10)

    # -- commands -----------------------------------------------------------------------------
    def set_brightness(self, pct):
        v = clamp_brightness(pct, self.limits)
        self._paced(self.raw.set_brightness, v)
        return v

    def submit_color(self, rgb):
        self._submit(("color", rgb))

    def submit_blink(self, rgb, dip_ms=70, rise_ms=130):
        """Dip to dip_brightness and rise in `rgb`. Dropped if it would exceed the flash rate."""
        now = self._clock()
        if now - self._last_flash < 1 / self.limits.max_flashes_per_sec:
            self.dropped += 1
            dlog.debug("blink dropped (flash rate)")
            return False
        self._last_flash = now
        self._submit(("blink", (rgb, dip_ms, rise_ms)))
        return True

    def measure_max_rate(self, n=100, max_seconds=20, on_progress=None):
        """Speed test: send colors as fast as the lamp answers. Returns achieved updates/sec."""
        import colorsys
        hue, count, start = 0.0, 0, self._clock()
        with self._io:
            while count < n and self._clock() - start < max_seconds:
                rgb = clamp_rgb([int(c * 255) for c in colorsys.hsv_to_rgb(hue, 1, 1)], self.limits)
                self.raw.set_rgb(rgb)
                hue, count = (hue + 0.02) % 1.0, count + 1
                if on_progress and count % 20 == 0:
                    on_progress(count / (self._clock() - start))
        return count / (self._clock() - start)

    # -- internals ----------------------------------------------------------------------------
    def _submit(self, item):
        with self._cond:
            if self._closed:
                return
            if self._pending is not None:
                if self._pending[0] == "blink" and item[0] == "color":
                    self.dropped += 1  # a pending blink carries its own color; keep it
                    return
                self.dropped += 1
                dlog.debug("replaced pending %s command", self._pending[0])
            self._pending = item
            self._cond.notify_all()

    def _paced(self, fn, *args):
        """Run one lamp call no sooner than 1/rate after the previous one."""
        with self._io:
            wait = self._last_send + 1 / self.rate - self._clock()
            if wait > 0:
                time.sleep(wait)
            try:
                return fn(*args)
            finally:
                self._last_send = self._clock()

    def _worker(self):
        while True:
            with self._cond:
                while self._pending is None and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
            # wait out the rate limit; newer commands may replace the pending one meanwhile
            while True:
                with self._cond:
                    if self._closed:
                        return
                    remaining = self._last_send + 1 / self.rate - self._clock()
                    if remaining <= 0:
                        item, self._pending = self._pending, None
                        break
                    self._cond.wait(remaining)
            if item is not None:
                self._send(item)

    def _send(self, item):
        kind, payload = item
        lim = self.limits
        try:
            with self._io:
                if kind == "color":
                    self.raw.set_rgb(clamp_rgb(payload, lim))
                else:
                    rgb, dip_ms, rise_ms = payload
                    r, g, b = clamp_rgb(rgb, lim)
                    c = (r << 16) | (g << 8) | b
                    flow = f"{dip_ms},1,{c},{lim.dip_brightness},{rise_ms},1,{c},{lim.max_brightness}"
                    self.raw.raw_command("start_cf", [2, 1, flow])
            self.sent += 1
            self.failures = 0
        except Exception as e:  # noqa: BLE001 - never crash mid-flow; back off instead
            self.failures += 1
            self.rate = max(0.25, self.rate / 2)
            dlog.debug("send failed (%s); rate now %.2f/s", e, self.rate)
            self.say(f"Lamp not answering ({e}); slowing down.")
            if self.failures >= MAX_CONSECUTIVE_FAILURES:
                self.failed = (f"lamp unreachable after {self.failures} failures ({e}). If the lamp's IP may "
                               "have changed, check your router's device list or set a DHCP reservation")
                with self._cond:
                    self._closed = True
        finally:
            self._last_send = self._clock()
