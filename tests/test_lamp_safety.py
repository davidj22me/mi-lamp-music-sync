"""
Acceptance tests for the lamp safety rules (see GUARDRAILS.md). They use a fake lamp and fake audio,
so no real lamp, network or sound device is needed.

Run from the repo folder:   python -m unittest discover -s tests -v
"""
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import replace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lamp_safety as ls  # noqa: E402
import music_sync as ms  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAST = replace(ls.Limits(), max_updates_per_sec=20.0)   # fast pacing keeps the tests quick


class FakeLamp:
    """Records every call; stands in for miio.Yeelight."""

    def __init__(self, on=True, mode=1, rgb=(204, 102, 255), ct=None, bri=100, fail=False):
        self._status = types.SimpleNamespace(is_on=on, color_mode=mode, rgb=rgb, color_temp=ct, brightness=bri)
        self.fail = fail
        self.calls = []
        self.lock = threading.Lock()

    def _rec(self, name, *args):
        with self.lock:
            self.calls.append((time.monotonic(), name, args))
        if self.fail and name != "status":
            raise RuntimeError("simulated timeout")

    def status(self): return self._status
    def set_save_state_on_change(self, v): self._rec("save_state", v)
    def on(self): self._rec("on")
    def off(self): self._rec("off")
    def set_rgb(self, rgb): self._rec("set_rgb", tuple(rgb))
    def set_brightness(self, v): self._rec("set_brightness", v)
    def set_color_temp(self, v): self._rec("set_color_temp", v)
    def raw_command(self, cmd, params): self._rec(cmd, tuple(params))

    def names(self): return [c[1] for c in self.calls]

    def brightness_values(self):
        """Every brightness the lamp was told to use (explicit calls + color-flow steps)."""
        out = []
        for _, name, args in self.calls:
            if name == "set_brightness":
                out.append(args[0])
            elif name == "start_cf":
                steps = args[0][2].split(",")           # dur,mode,value,bright, dur,mode,value,bright
                out += [int(steps[3]), int(steps[7])]
        return out


class FakeAnalyzer:
    def __init__(self, stop, boom=None, beats=0):
        self.device = types.SimpleNamespace(name="Fake output")
        self.hue = self.beat_hue = 0.1
        self.silent_since = None
        self.error = None
        self.beats = beats
        self._boom = boom

    @property
    def level(self):
        if self._boom:
            raise self._boom
        return 0.5

    def start(self):
        pass


def make_engine(lamp, cfg=None, boom=None, token="TESTTOKEN", limits=FAST, lines=None):
    cfg = cfg or ms.Config(minutes=1)
    lines = lines if lines is not None else []
    return ms.Engine("192.0.2.1", token, cfg, log=lines.append, limits=limits,
                     raw_factory=lambda: lamp, analyzer_factory=lambda stop: FakeAnalyzer(stop, boom))


class EngineTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_lock = ms.LOCK_PATH
        ms.LOCK_PATH = os.path.join(self._tmp.name, "lock")

    def tearDown(self):
        ms.LOCK_PATH = self._old_lock
        self._tmp.cleanup()

    def run_engine(self, engine, seconds=0.6):
        engine.start()
        time.sleep(seconds)
        engine.stop()
        self.assertFalse(engine.running)


class BrightnessCap(unittest.TestCase):
    """1. No code path sets brightness above the cap."""

    def check(self, limits):
        lamp = FakeLamp(bri=100)       # previous brightness above the cap must be clamped on restore
        sl = ls.SafeLamp(lamp, limits)
        sl.start_session()
        for pct in (100, 500, 81, 79.6, 0, -20):
            sl.set_brightness(pct)
        sl.submit_blink((255, 0, 0))
        time.sleep(0.3)
        sl.finish("previous")
        sl2 = ls.SafeLamp(FakeLamp(), limits)
        sl2.start_session()
        sl2.finish("warm")
        values = lamp.brightness_values()
        self.assertTrue(values)
        self.assertLessEqual(max(values), limits.max_brightness)
        self.assertGreaterEqual(min(values), 1)

    def test_default_cap_is_80(self):
        self.assertEqual(ls.Limits().max_brightness, 80)
        self.check(FAST)

    def test_gentle_cap(self):
        self.check(FAST.gentle())

    def test_no_time_of_day_dependence(self):
        self.assertEqual(ls.clamp_brightness(100, FAST), 80)   # same answer at any hour

    def test_blink_dip_floor_is_5(self):
        lamp = FakeLamp()
        sl = ls.SafeLamp(lamp, FAST)
        sl.start_session()
        sl.submit_blink((0, 255, 0))
        time.sleep(0.3)
        sl.close()
        flow = [a for _, n, a in lamp.calls if n == "start_cf"][0][0][2].split(",")
        self.assertEqual((int(flow[3]), int(flow[7])), (5, 80))

    def test_near_white_is_scaled_down(self):
        r, g, b = ls.clamp_rgb((255, 255, 255), FAST)
        self.assertLessEqual(max(r, g, b), int(0.85 * 255))


class RateLimit(unittest.TestCase):
    """2. A burst of 100 commands in one second -> no more than the limit, latest value wins."""

    def test_burst_is_limited_and_latest_wins(self):
        limits = replace(ls.Limits(), max_updates_per_sec=10.0)
        lamp = FakeLamp()
        sl = ls.SafeLamp(lamp, limits)
        sl.start_session()
        lamp.calls.clear()
        t0 = time.monotonic()
        for i in range(100):
            sl.submit_color((i, 0, 0))
        time.sleep(max(0, 1.0 - (time.monotonic() - t0)))
        sends_in_one_second = [c for c in lamp.calls if c[1] == "set_rgb" and c[0] - t0 <= 1.0]
        self.assertLessEqual(len(sends_in_one_second), 10 + 1)
        time.sleep(0.4)
        sl.close()
        rgb_sent = [c[2][0] for c in lamp.calls if c[1] == "set_rgb"]
        self.assertEqual(rgb_sent[-1], (99, 0, 0))             # latest value won
        self.assertGreater(sl.dropped, 80)                      # older ones were dropped, not queued

    def test_blinks_limited_to_3_per_second(self):
        lamp = FakeLamp()
        sl = ls.SafeLamp(lamp, FAST)
        sl.start_session()
        accepted = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 1.0:
            accepted += sl.submit_blink((255, 0, 0))
            time.sleep(0.02)
        sl.close()
        self.assertLessEqual(accepted, 4)   # 3/s, plus the one at t=0 of the next window

    def test_backoff_then_stop_after_5_failures(self):
        lamp = FakeLamp()
        sl = ls.SafeLamp(lamp, FAST, log=lambda m: None)
        sl.start_session()
        lamp.fail = True
        t0 = time.monotonic()
        while sl.failed is None and time.monotonic() - t0 < 8:
            sl.submit_color((1, 2, 3))
            time.sleep(0.02)
        sl.close()
        self.assertIsNotNone(sl.failed)
        self.assertIn("DHCP", sl.failed)
        self.assertEqual(sl.failures, 5)
        self.assertLess(sl.rate, FAST.max_updates_per_sec)     # rate was halved on failures


class PowerRules(EngineTestCase):
    """3. No script calls off() except through the explicit flag."""

    def test_off_is_never_called_by_default_and_lamp_is_left_on(self):
        lamp = FakeLamp(on=False)                                # lamp was OFF before the session
        self.run_engine(make_engine(lamp))
        self.assertNotIn("off", lamp.names())
        self.assertIn("on", lamp.names())                        # left on, per decision

    def test_off_only_with_explicit_flag(self):
        lamp = FakeLamp()
        self.run_engine(make_engine(lamp, ms.Config(minutes=1, off_at_exit=True)))
        self.assertEqual(lamp.names().count("off"), 1)
        self.assertEqual(lamp.names()[-1], "off")

    def test_no_off_calls_in_source(self):
        for name in ("app.py", "music_sync.py", "lamp_test.py"):
            with open(os.path.join(REPO, name), encoding="utf-8") as f:
                self.assertIsNone(re.search(r"\.off\(", f.read()), f"{name} calls off()")

    def test_on_called_once_only_if_off_and_saved_state_disabled(self):
        lamp = FakeLamp(on=True)
        sl = ls.SafeLamp(lamp, FAST)
        sl.start_session()
        sl.close()
        self.assertEqual(lamp.names().count("on"), 0)
        self.assertIn("save_state", lamp.names())
        self.assertEqual([a for _, n, a in lamp.calls if n == "save_state"][0], (False,))


class RestoreOnExit(EngineTestCase):
    """4 & 5. Ctrl+C, an exception, a normal stop and the runtime cap all restore the lamp."""

    def assert_restored(self, lamp):
        names = lamp.names()
        self.assertIn(("set_rgb", ((204, 102, 255),)), [(c[1], c[2]) for c in lamp.calls])
        self.assertEqual(names[-1], "set_brightness")
        self.assertEqual(lamp.calls[-1][2], (80,))               # previous 100 clamped to the cap

    def test_exception_restores(self):
        lamp = FakeLamp()
        engine = make_engine(lamp, boom=RuntimeError("boom"))
        self.run_engine(engine, 0.4)
        self.assertTrue(engine.finish_reason.startswith("error"))
        self.assert_restored(lamp)

    def test_keyboard_interrupt_restores(self):
        lamp = FakeLamp()
        engine = make_engine(lamp, boom=KeyboardInterrupt())
        self.run_engine(engine, 0.4)
        self.assertEqual(engine.finish_reason, "interrupted")
        self.assert_restored(lamp)

    def test_normal_stop_restores(self):
        lamp = FakeLamp()
        engine = make_engine(lamp)
        self.run_engine(engine, 0.5)
        self.assertEqual(engine.finish_reason, "stopped")
        self.assert_restored(lamp)

    def test_restore_warm(self):
        lamp = FakeLamp()
        self.run_engine(make_engine(lamp, ms.Config(minutes=1, restore="warm")), 0.4)
        self.assertIn(("set_color_temp", (2700,)), [(c[1], c[2]) for c in lamp.calls])

    def test_max_runtime_stops_and_restores(self):
        lamp = FakeLamp()
        engine = make_engine(lamp, ms.Config(minutes=0.01))      # 0.6 s
        engine.start()
        engine.thread.join(10)
        self.assertFalse(engine.running)
        self.assertEqual(engine.finish_reason, "runtime cap reached")
        self.assert_restored(lamp)

    def test_default_runtime_is_120_minutes(self):
        self.assertEqual(ls.Limits().max_runtime_minutes, 120)
        self.assertIsNone(ms.Config().minutes)                  # None -> falls back to the limit


class Secrets(EngineTestCase):
    """6. The token does not appear in any tracked file or log output."""

    def tracked_files(self):
        try:
            out = subprocess.run(["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True).stdout
            return [os.path.join(REPO, p) for p in out.splitlines() if p]
        except Exception:  # noqa: BLE001 - not a git checkout (e.g. exported zip): scan everything
            skip = {".git", "dist", "build", "__pycache__"}
            return [os.path.join(d, f) for d, ds, fs in os.walk(REPO) if not (set(d.split(os.sep)) & skip)
                    for f in fs if f not in (".env", "settings.json")]

    def test_no_token_shaped_strings_in_tracked_files(self):
        hex32 = re.compile(rb"(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])")
        real_token = ms.load_env().get("LAMP_TOKEN", "").encode()
        for path in self.tracked_files():
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as f:
                data = f.read()
            if real_token:
                self.assertNotIn(real_token, data, path)
            for m in hex32.findall(data):
                self.assertEqual(len(set(m.lower())), 1, f"token-shaped string in {path}")  # allow 000..0 placeholders

    def test_token_not_in_logs(self):
        token = "deadbeefdeadbeefdeadbeefdeadbeef"
        records = []

        class H(logging.Handler):
            def emit(self, record): records.append(record.getMessage())
        handler, logger = H(), logging.getLogger("lamp_safety")
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        try:
            lines = []
            lamp = FakeLamp()
            self.run_engine(make_engine(lamp, token=token, lines=lines), 0.4)
            lamp2 = FakeLamp()
            lamp2.fail = True
            self.run_engine(make_engine(lamp2, token=token, lines=lines), 0.6)
        finally:
            logger.removeHandler(handler)
        blob = "\n".join(lines + records)
        self.assertTrue(blob)
        self.assertNotIn(token, blob)


class Config(unittest.TestCase):
    def test_values_are_clamped_to_safe_ranges(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.ini")
            with open(p, "w") as f:
                f.write("[limits]\nmax_brightness = 500\nmax_flashes_per_sec = 10\n"
                        "max_updates_per_sec = 999\ndip_brightness = oops\n")
            lim = ls.load_limits(p)
            self.assertEqual(lim.max_brightness, 100)
            self.assertEqual(lim.max_flashes_per_sec, 3)
            self.assertEqual(lim.max_updates_per_sec, 20)
            self.assertEqual(lim.dip_brightness, 5)            # invalid -> default

    def test_missing_file_gives_defaults(self):
        self.assertEqual(ls.load_limits(os.path.join(tempfile.gettempdir(), "nope.ini")), ls.Limits())

    def test_measured_rate_is_recorded_at_70_percent(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.ini")
            value = ls.write_measured_rate(10.0, p)
            self.assertEqual(value, 7.0)
            self.assertEqual(ls.load_limits(p).max_updates_per_sec, 7.0)
            with open(p) as f:
                self.assertIn("# Measured: 10.0 updates/sec", f.read())

    def test_shipped_example_config_matches_defaults(self):
        self.assertEqual(ls.load_limits(os.path.join(REPO, "lamp_config.example.ini")), ls.Limits())
        with open(os.path.join(REPO, "lamp_config.example.ini"), encoding="utf-8") as f:
            self.assertEqual(f.read(), ls.DEFAULT_CONFIG_TEXT)


if __name__ == "__main__":
    unittest.main()
