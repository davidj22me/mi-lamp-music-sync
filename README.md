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
- The "dark" part of a blink is a dip to about 1% brightness, not a true power-off. This lamp ignores
  color changes while powered off, and a dip can be sent as a single command.

## Safety and guardrails

- **Runs only when you start it.** No background service, no autostart, no scheduled task.
- **Stop any time:** the Stop button, closing the window, or `q`/`Esc`/`Ctrl+C` in the CLI.
- **The lamp is restored** to its previous power, color/temperature and brightness whenever a session ends.
- **Hard runtime cap** (default 120 min), auto-stop after 10 min of silence, and auto-stop after 5
  failed lamp commands in a row.
- **One instance at a time** (lock file).

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

Options: **Style** (beats or classic), **Blink on beat** (none, every, alternate), **Beat sensitivity**
(works live), **Auto-stop after (min)**, and **Preview only** (analyse audio without touching the lamp).

### Command line

```powershell
copy .env.example .env      # then edit .env with your IP and token

python lamp_test.py test           # sanity check: red -> green -> blue -> warm white
python lamp_test.py speed          # measure how many commands/sec your lamp accepts

python music_sync.py                          # beats style, up to 120 min
python music_sync.py --blink every            # dip to dark on every beat
python music_sync.py --blink alternate        # ...on every other beat
python music_sync.py --mode classic           # bass -> brightness, pitch -> hue
python music_sync.py --minutes 30             # custom time limit
python music_sync.py --dry-run                # analyse audio only, never touch the lamp
```

Press `q`, `Esc` or `Ctrl+C` to quit.

## Tuning

Tunable constants are at the top of `music_sync.py`: the hue step per beat (`BEAT_HUE_STEP`),
beat sensitivity (`BEAT_SENSITIVITY`), minimum gap between beats (`BEAT_MIN_GAP`), brightness floor,
blink timing (`BLINK_DIP_MS`, `BLINK_RISE_MS`), and the safety limits.

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

## Limitations

- Windows only (uses WASAPI loopback and `msvcrt`).
- Tested only on the Mi Bedside Lamp 2. The Yeelight LAN control protocol is not available on this
  model, which is why this uses miIO.
- Color updates are limited by the lamp's Wi-Fi command rate (see above).

## License

MIT. See [LICENSE](LICENSE).
