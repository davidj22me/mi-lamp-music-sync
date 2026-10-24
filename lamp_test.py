"""
Mi Bedside Lamp 2 - local control test.

Setup:
    see README.md (install python-miio), then copy .env.example to .env and fill in
    LAMP_IP and LAMP_TOKEN (token from the Xiaomi Cloud Tokens Extractor).

Run:
    python lamp_test.py test      # red -> green -> blue, then back to warm white
    python lamp_test.py speed     # fast rainbow loop, prints how many updates/sec the lamp handled
Press Ctrl+C to stop the speed test.
"""
import colorsys
import os
import sys
import time

from miio import Yeelight

def _load_env(path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
    with open(path) as f:
        for line in f:
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.strip().split("=", 1)
                os.environ.setdefault(k, v)


_load_env()
LAMP_IP = os.environ["LAMP_IP"]
LAMP_TOKEN = os.environ["LAMP_TOKEN"]

lamp = Yeelight(LAMP_IP, LAMP_TOKEN)


def basic_test():
    print("Status:", lamp.status())
    lamp.on()
    lamp.set_brightness(60)
    for name, rgb in [("red", (255, 0, 0)), ("green", (0, 255, 0)), ("blue", (0, 0, 255))]:
        print("Setting", name)
        lamp.set_rgb(rgb)
        time.sleep(1.5)
    lamp.set_color_temp(3000)
    print("Done - lamp should be warm white.")


def speed_test():
    print("Rainbow loop running. Ctrl+C to stop.")
    hue, sent, start = 0.0, 0, time.time()
    try:
        while True:
            if sent >= 100 and "--long" not in sys.argv:
                break
            r, g, b = (int(c * 255) for c in colorsys.hsv_to_rgb(hue, 1, 1))
            lamp.set_rgb((r, g, b))
            hue = (hue + 0.02) % 1.0
            sent += 1
            if sent % 20 == 0:
                print(f"{sent / (time.time() - start):.1f} updates/sec")
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "test"
    speed_test() if mode == "speed" else basic_test()
