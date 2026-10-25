# Mi Lamp Sync

Make a **Mi Bedside Lamp 2** (`yeelink.light.bslamp2`) react to the music playing on your Windows PC.
It controls the lamp locally over your Wi-Fi (no cloud, no phone), and it never touches your audio.

- **Beats style:** the lamp's color steps around the color wheel on every detected beat.
- **Blink on beat:** optionally dip the lamp to near-dark on every beat (or every other beat) and bring the new color up, for a punchier effect.
- **Classic style:** bass drives brightness, the pitch of the music drives the hue.
- Works with anything you play (Spotify, YouTube, local files, games), through any output (speakers, Bluetooth, HDMI).

> **Unofficial project.** Not affiliated with or endorsed by Xiaomi or Yeelight. Tested only on the
> Mi Bedside Lamp 2 and Windows 11. Other Yeelight/Xiaomi lamps may work but are untested.

## How it works

The app reads a copy of the sound your PC is playing (Windows WASAPI loopback), splits it into
frequency bands with an FFT, detects bass hits, and sends color commands to the lamp using the
miIO protocol ([python-miio](https://github.com/rytilahti/python-miio)).

- The lamp has **no microphone**. All listening happens on your PC.
- Loopback only *reads* the output signal. It doesn't change your volume, device or playback.
- The lamp accepts roughly 8-20 commands a second over Wi-Fi, so color changes can trail the beat
  by around 100-150 ms. It feels rhythmic, but it isn't frame-accurate.
- The "dark" part of a blink is a dip to 5% brightness, not a power-off. The app never toggles the
  lamp's power for effects (and this lamp ignores color changes while powered off anyway).

## Safety and guardrails

> **Photosensitivity warning.** Rapid flashing can be uncomfortable or unsafe for people with
> photosensitivity. Use gentle mode and moderate brightness near bed. Blink-on-beat is **off by default**.

All lamp commands go through one safety layer (`lamp_safety.py`), so these limits can't be bypassed
by accident. They're configurable in `lamp_config.ini` (see below):

- **Brightness cap: 80%**, always, applied to every brightness value. Near-white colors (all channels
  high) are scaled down, since sustained full-power white produces the most heat.
- **Rate limit:** at most `max_updates_per_sec` commands per second (default 4, set to about 70% of
  your lamp's measured rate after the speed test). Newer commands replace older pending ones; nothing
  is queued. If the lamp stops answering, the rate is halved on each failure and the session ends after
  5 failures in a row, with a hint to check the lamp's IP.
- **Flash limit:** at most 3 beat color changes or blinks per second. Brightness changes between
  updates are capped at 25 points; the blink dip is the one opt-in exception.
- **Power:** the lamp's power is never toggled for effects, and the lamp is **left on** when a session
  ends. It's only turned off if you pass `--off-at-exit`.
- **Saved state:** while a session runs, "save state on change" is switched **off** on the lamp, so the
  lamp's saved default isn't rewritten on every color change. It is not switched back on afterwards
  (re-enable it from Python with `Yeelight(ip, token).set_save_state_on_change(True)` if you want).
- **Restore on exit:** quit, `Ctrl+C`, an error, closing the window or the console, or the time limit
  all put the lamp back to its previous color/temperature and brightness (or warm white with
  `--restore warm`). Restored brightness is also capped at 80%.
- **Gentle mode** (`--gentle` / checkbox): slower smoothing, a 50% brightness ceiling, half the update
  rate, fewer flashes, and no blinking.
- **Runs only when you start it.** No background service, no autostart, no scheduled task. One instance
  at a time (lock file), a hard runtime cap (default 120 min), auto-stop after 10 min of silence.

## Requirements

- Windows 10/11
- Python 3.9+ (only needed if you run from source)
- A Mi Bedside Lamp 2 already set up in the **Mi Home / Xiaomi Home** app, on **2.4 GHz** Wi-Fi
- Your PC and the lamp on the **same network/subnet**

## 1. Get your lamp's IP and token

Local control needs the lamp's IP address and its 32-character token.

1. Use the [Xiaomi Cloud Tokens Extractor](https://github.com/PiotrMachowski/Xiaomi-cloud-tokens-extractor)
   and log in with your Xiaomi account. Pick the server region your account uses.
2. Find your lamp in the list (model `yeelink.light.bslamp2`) and note its `IP` and `TOKEN`.

Notes:
- The **token lets anyone on your network control the lamp**. Keep it private and never commit it.
- Re-pairing or factory-resetting the lamp (including moving it to a new Wi-Fi network via Mi Home)
  generates a **new token**; run the extractor again.
- Give the lamp a DHCP reservation in your router so its IP doesn't change.

## 2. Install

### Option A: the packaged app (no Python needed)

Download `MiLampSync.exe` from the [Releases](../../releases) page and run it. Windows SmartScreen may
warn because the file is unsigned: click **More info**, then **Run anyway**. The app stores your
settings in a `.env` and `settings.json` next to the `.exe`.

### Option B: from source

```powershell
git clone https://github.com/davidj22me/mi-lamp-music-sync.git
cd mi-lamp-music-sync
python -m pip install --upgrade pip
python -m pip install --no-deps python-miio==0.5.12
python -m pip install -r requirements.txt
python app.py
```

Why `--no-deps`? `python-miio` depends on `netifaces`, which has no Windows wheel and needs the
Microsoft C++ Build Tools to compile. It is only used for device *discovery*, which this project
doesn't use (it connects by IP), so installing without it works fine. `pip` may print a harmless
warning about it.

## 3. Use it

### Window app

1. Run `python app.py` (or `MiLampSync.exe`).
2. Enter the lamp **IP** and **token** (saved to a local `.env`).
3. Pick a style and blink option, start some music and click **Start**.
4. Click **Stop** (or close the window) to end it and restore the lamp.

Options: **Style** (beats or classic), **Blink on beat** (none by default; every or alternate beat),
**Gentle mode**, **Beat sensitivity** (works live), **Auto-stop after (min)**, **When stopped**
(restore the previous state or set warm white) and **Preview only** (analyse audio without touching
the lamp).

### Command line

```powershell
copy .env.example .env      # then edit .env with your IP and token

python lamp_test.py test           # sanity check: red -> green -> blue -> warm white
python lamp_test.py speed          # measure commands/sec and record a safe rate in lamp_config.ini

python music_sync.py                          # beats style, up to 120 min
python music_sync.py --gentle                 # slower, dimmer, no blinking
python music_sync.py --blink every            # opt-in: dip to 5% on every beat
python music_sync.py --blink alternate        # ...on every other beat
python music_sync.py --mode classic           # bass -> brightness, pitch -> hue
python music_sync.py --max-runtime 30         # custom time limit (minutes)
python music_sync.py --restore warm           # finish on warm white instead of the previous state
python music_sync.py --off-at-exit            # turn the lamp off when done (default: leave it on)
python music_sync.py --dry-run                # analyse audio only, never touch the lamp
```

Press `q`, `Esc` or `Ctrl+C` to quit.

## Configuration and tuning

- **Safety limits** are in `lamp_config.ini` (created from `lamp_config.example.ini` on first run;
  every setting is commented). Values are clamped to safe ranges, for example the flash limit can't
  exceed 3 per second. Run `python lamp_test.py speed` once: it records your lamp's measured rate in the
  file and sets the update limit to about 70% of it.
- **Look and feel** constants are at the top of `music_sync.py`: the hue step per beat
  (`BEAT_HUE_STEP`), beat sensitivity, smoothing, blink timing (`BLINK_DIP_MS`, `BLINK_RISE_MS`).

## Tests

The safety rules have automated tests that use a fake lamp and fake audio (no hardware needed):

```powershell
python -m unittest discover -s tests -v
```

They check, among other things, that brightness never exceeds the cap, that a burst of commands is
rate limited with the latest value winning, that `off()` is only called with the explicit flag, that
the lamp is restored on `Ctrl+C`, errors and the runtime cap, and that no token appears in tracked
files or logs.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `No response from the device` / timeouts | Wrong IP, **wrong or outdated token** (re-pairing generates a new one), PC on a different network/VLAN, a firewall blocking UDP port 54321, or router client isolation. |
| Ping works but control doesn't | Almost always the token. Re-run the token extractor. |
| `pip install python-miio` fails building `netifaces` | Use the `--no-deps` install in the steps above. |
| Lamp doesn't react to music | Check the app shows the right output device. Loopback follows your *default* playback device. |
| Lamp is stuck in a color after a crash | Set it from Mi Home, or run `python lamp_test.py test`. |
| "already running" | Another copy is open. Close it, or delete `.music_sync.lock` if it crashed. |

## Build the .exe yourself

```powershell
python -m pip install pyinstaller
python -m PyInstaller --noconfirm --onefile --windowed --name MiLampSync --collect-all soundcard --collect-all miio --hidden-import netifaces app.py
```

The result is `dist\MiLampSync.exe`. Put your `.env` next to it (or enter the IP/token in the app).
`lamp_config.ini` is created next to the `.exe` on first run; run the speed test from source
(`python lamp_test.py speed`) and copy the resulting file over if you want a measured rate.

## Limitations

- Windows only (uses WASAPI loopback and `msvcrt`).
- Tested only on the Mi Bedside Lamp 2. The Yeelight LAN control protocol is not available on this
  model, which is why this uses miIO.
- Color updates are limited by the lamp's Wi-Fi command rate (see above).

## License

MIT. See [LICENSE](LICENSE).
