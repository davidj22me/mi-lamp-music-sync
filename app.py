"""
Mi Lamp Sync - window app. Click Start to sync the lamp with the music on this PC, Stop (or close
the window) to end it and put the lamp back as it was. Nothing runs until you click Start.
"""
import json
import os
import tkinter as tk
from tkinter import messagebox, ttk

import music_sync as ms

SETTINGS_PATH = os.path.join(ms.HERE, "settings.json")
DEFAULTS = {"mode": "beats", "blink": "none", "minutes": 120, "sensitivity": 0.83}


def threshold_from_slider(s):
    """Slider 0..1 (right = more beats) -> beat threshold 1.8..1.2 (lower threshold = more beats)."""
    return 1.8 - 0.6 * s


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Mi Lamp Sync")
        self.resizable(False, False)
        self.engine = None
        self.settings = dict(DEFAULTS)
        try:
            with open(SETTINGS_PATH) as f:
                self.settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        env = ms.load_env()

        pad = {"padx": 10, "pady": 4}
        frm = ttk.Frame(self, padding=12)
        frm.grid()

        ttk.Label(frm, text="Lamp IP").grid(row=0, column=0, sticky="w", **pad)
        self.ip = tk.StringVar(value=env.get("LAMP_IP", ""))
        ttk.Entry(frm, textvariable=self.ip, width=24).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Lamp token").grid(row=1, column=0, sticky="w", **pad)
        self.token = tk.StringVar(value=env.get("LAMP_TOKEN", ""))
        ttk.Entry(frm, textvariable=self.token, width=34, show="*").grid(row=1, column=1, sticky="w", **pad)

        ttk.Separator(frm).grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)

        ttk.Label(frm, text="Style").grid(row=3, column=0, sticky="w", **pad)
        self.mode = tk.StringVar(value=self.settings["mode"])
        ttk.Combobox(frm, textvariable=self.mode, values=["beats", "classic"], state="readonly",
                     width=12).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Blink on beat").grid(row=4, column=0, sticky="w", **pad)
        self.blink = tk.StringVar(value=self.settings["blink"])
        ttk.Combobox(frm, textvariable=self.blink, values=["none", "every", "alternate"], state="readonly",
                     width=12).grid(row=4, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Beat sensitivity").grid(row=5, column=0, sticky="w", **pad)
        self.sens = tk.DoubleVar(value=self.settings["sensitivity"])
        ttk.Scale(frm, from_=0, to=1, variable=self.sens, length=190,
                  command=lambda _=None: self._push_live()).grid(row=5, column=1, sticky="w", **pad)

        ttk.Label(frm, text="Auto-stop after (min)").grid(row=6, column=0, sticky="w", **pad)
        self.minutes = tk.StringVar(value=str(self.settings["minutes"]))
        ttk.Spinbox(frm, from_=1, to=600, textvariable=self.minutes, width=8).grid(row=6, column=1, sticky="w", **pad)

        self.preview = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="Preview only (don't control the lamp)", variable=self.preview).grid(
            row=7, column=0, columnspan=2, sticky="w", **pad)

        self.button = ttk.Button(frm, text="Start", command=self.toggle, width=18)
        self.button.grid(row=8, column=0, columnspan=2, pady=(10, 4))

        self.swatch = tk.Canvas(frm, width=300, height=34, highlightthickness=1, highlightbackground="#888",
                                bg="#222")
        self.swatch.grid(row=9, column=0, columnspan=2, pady=4)

        self.status = tk.StringVar(value="Idle. Click Start to begin.")
        ttk.Label(frm, textvariable=self.status, wraplength=320, justify="left").grid(
            row=10, column=0, columnspan=2, sticky="w", **pad)

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.tick)

    # -- settings --------------------------------------------------------------------------------
    def _push_live(self):
        if self.engine:
            self.engine.cfg.threshold = threshold_from_slider(self.sens.get())

    def _save(self):
        if not self.preview.get():  # preview never touches saved lamp credentials
            ms.save_env(self.ip.get().strip(), self.token.get().strip())
        with open(SETTINGS_PATH, "w") as f:
            json.dump({"mode": self.mode.get(), "blink": self.blink.get(),
                       "minutes": self._minutes(), "sensitivity": round(self.sens.get(), 2)}, f)

    def _minutes(self):
        try:
            return max(1.0, min(600.0, float(self.minutes.get())))
        except ValueError:
            return 120.0

    # -- start / stop ----------------------------------------------------------------------------
    def toggle(self):
        if self.engine and self.engine.running:
            self.stop()
        else:
            self.start()

    def start(self):
        if not self.preview.get() and not (self.ip.get().strip() and self.token.get().strip()):
            messagebox.showwarning("Mi Lamp Sync", "Enter the lamp IP and token first (or tick Preview only).")
            return
        self._save()
        cfg = ms.Config(mode=self.mode.get(), blink=self.blink.get(), minutes=self._minutes(),
                        threshold=threshold_from_slider(self.sens.get()), dry_run=self.preview.get())
        self.engine = ms.Engine(self.ip.get().strip(), self.token.get().strip(), cfg, log=self._log)
        try:
            self.engine.start()
        except ms.AlreadyRunning as e:
            self.engine = None
            messagebox.showwarning("Mi Lamp Sync", str(e))
            return
        self.button.config(text="Stop")
        self.status.set("Starting ...")

    def stop(self):
        if self.engine:
            self.status.set("Stopping and restoring the lamp ...")
            self.update_idletasks()
            self.engine.stop()

    def _log(self, msg):
        # called from the engine thread; just remember the latest line, the UI thread displays it
        self._last_log = msg

    # -- UI refresh ------------------------------------------------------------------------------
    def tick(self):
        e = self.engine
        if e:
            if e.running:
                r, g, b = e.rgb
                self.swatch.config(bg=f"#{r:02x}{g:02x}{b:02x}")
                self.status.set(f"Running on {e.device_name or '...'}\n"
                                f"Beats: {e.beats}   Blinks: {e.blinks}")
            elif e.finish_reason is not None:
                self.button.config(text="Start")
                self.swatch.config(bg="#222")
                self.status.set(f"Stopped: {e.finish_reason}.")
                self.engine = None
        self.after(100, self.tick)

    def on_close(self):
        if self.engine and self.engine.running:
            self.status.set("Stopping and restoring the lamp ...")
            self.update_idletasks()
            self.engine.stop()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()
