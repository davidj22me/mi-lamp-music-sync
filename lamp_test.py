"""
Mi Bedside Lamp 2 - local control test (goes through the safety layer in lamp_safety.py).

Setup:
    see README.md (install python-miio), then copy .env.example to .env and fill in
    LAMP_IP and LAMP_TOKEN (token from the Xiaomi Cloud Tokens Extractor).

Run:
    python lamp_test.py test      # red -> green -> blue, then warm white
    python lamp_test.py speed     # sends 100 rainbow updates as fast as the lamp answers, records the
                                  # measured updates/sec in lamp_config.ini (and sets the limit to ~70%)
The lamp is restored to its previous state afterwards (it is left on).
"""
import os
import sys
import time

from miio import Yeelight

import lamp_safety as ls

HERE = ls.HERE


def load_env(path=os.path.join(HERE, ".env")):
    with open(path) as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


def make_lamp():
    load_env()
    ls.ensure_config_file()
    limits = ls.load_limits()
    print("Safety limits:", limits.describe())
    return ls.SafeLamp(Yeelight(os.environ["LAMP_IP"], os.environ["LAMP_TOKEN"]), limits)


def basic_test(lamp):
    lamp.start_session()
    try:
        for name, rgb in [("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255))]:
            print("Setting", name)
            lamp.submit_color(rgb)
            time.sleep(1.5)
    finally:
        lamp.finish(restore="warm")
    print("Done - lamp should be warm white.")


def speed_test(lamp):
    print(ls.PHOTOSENSITIVITY_NOTICE)
    print("Rainbow speed test (100 updates, Ctrl+C to abort).")
    lamp.start_session()
    try:
        rate = lamp.measure_max_rate(100, on_progress=lambda r: print(f"{r:.1f} updates/sec"))
        value = ls.write_measured_rate(rate)
        print(f"Measured {rate:.1f} updates/sec. Wrote max_updates_per_sec = {value:g} (~70%) to lamp_config.ini")
    except KeyboardInterrupt:
        print("\nAborted.")
    finally:
        lamp.finish(restore="previous")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    lamp = make_lamp()
    speed_test(lamp) if mode == "speed" else basic_test(lamp)
