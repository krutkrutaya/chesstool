# -*- coding: utf-8 -*-
"""Control panel GUI: dark theme, iOS switches, log, status."""
import time, threading, queue
import tkinter as tk
from tkinter import ttk, scrolledtext
import chess
import keyboard as kb

from .config import load_config, save_config
from .engine import ELO_LEVELS, profile_for
from .detector import BoardDetector
from .overlay import Overlay, Calibrator
from .worker import Worker

BG      = "#1c1f26"
BG2     = "#22262f"
BG3     = "#2a2f3a"
FG      = "#e6e6e6"
FGDIM   = "#9aa3af"
ACCENT  = "#81b64c"
ACCENT2 = "#39d353"


class Switch(tk.Canvas):
    """iOS-style toggle switch, animated."""
    def __init__(self, master, width=52, height=26, bg=BG,
                 on_color=ACCENT2, off_color="#3a4150", command=None):
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self.w, self.h = width, height
        self.on_color, self.off_color = on_color, off_color
        self.command = command
        self._state = False
        self._pos = 2.0
        self._tag_move = None
        self.bind("<Button-1>", lambda e: self.toggle())
        self._draw()

    def _draw(self):
        self.delete("all")
        pad = 2
        track_color = self.on_color if self._state else self.off_color
        r = self.h / 2 - pad
        x0, y0 = pad, pad
        x1, y1 = self.w - pad, self.h - pad
        self.create_rectangle(x0 + r, y0, x1 - r, y1,
                              fill=track_color, outline=track_color)
        self.create_oval(x0, y0, x0 + 2 * r, y1,
                         fill=track_color, outline=track_color)
        self.create_oval(x1 - 2 * r, y0, x1, y1,
                         fill=track_color, outline=track_color)
        kx = pad + self._pos
        knob_d = self.h - 6
        self.create_oval(kx, 3, kx + knob_d, 3 + knob_d,
                         fill="#f4f6f8", outline="#d5d9de")
        if self._state:
            self.create_oval(kx + knob_d - 10, 3 + knob_d / 2 - 4,
                             kx + knob_d - 4, 3 + knob_d / 2 + 2,
                             fill="#2f6f31", outline="#2f6f31")

    @property
    def state(self):
        return self._state

    def set(self, value: bool, fire: bool = False):
        self._state = bool(value)
        target = (self.w - 6) - (self.h - 6) if self._state else 2.0
        self._animate_to(target)
        if fire and self.command:
            self.command()

    def toggle(self):
        self._state = not self._state
        target = (self.w - 6) - (self.h - 6) if self._state else 2.0
        self._animate_to(target)
        if self.command:
            self.command()

    def _animate_to(self, target: float):
        if self._tag_move:
            try:
                self.after_cancel(self._tag_move)
            except Exception:
                pass
        self._animate_step(target, 0)

    def _animate_step(self, target: float, step: int):
        speed = (target - self._pos) * 0.35
        if abs(target - self._pos) < 0.8 or step > 12:
            self._pos = target
        else:
            self._pos += speed
        self._draw()
        if abs(target - self._pos) >= 0.8:
            self._tag_move = self.after(
                16, lambda: self._animate_step(target, step + 1))


class ControlPanel:
    def __init__(self):
        self.cfg = load_config()
        self.ui_queue = queue.Queue()
        self.worker = None

        self.root = tk.Tk()
        self.root.title("ChessTool")
        self.root.configure(bg=BG)
        self.root.geometry("400x680")
        self.root.minsize(380, 620)

        self.overlay = Overlay(self.root)
        self.calibrator = Calibrator(self.root)
        self._build()
        self._bind_hotkeys()
        self.root.after(60, self._poll)

    # -- layout --------------------------------------------------------------
    def _build(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=BG2, background=BG2,
                        foreground=FG, arrowcolor=ACCENT)
        style.configure("Horizontal.TScale", background=BG2, troughcolor=BG3)

        tk.Label(self.root, text="♟ ChessTool", fg=ACCENT2, bg=BG,
                 font=("Segoe UI", 18, "bold")).pack(pady=(14, 2))
        tk.Label(self.root, text="chess.com assistant · elo-capped engine",
                 fg=FGDIM, bg=BG, font=("Segoe UI", 9)).pack(pady=(0, 8))

        def section(text):
            box = tk.Frame(self.root, bg=BG2,
                           highlightbackground="#14161b", highlightthickness=1)
            box.pack(fill=tk.X, padx=16, pady=6)
            tk.Label(box, text=text, fg=ACCENT, bg=BG2,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10,
                                                        pady=(6, 2))
            return box

        def switch_row(parent, label, initial, command, hint=""):
            row = tk.Frame(parent, bg=BG2)
            row.pack(fill=tk.X, padx=10, pady=(2, 4))
            tk.Label(row, text=label, fg=FG, bg=BG2,
                     font=("Segoe UI", 10)).pack(side=tk.LEFT)
            sw = Switch(row, command=command, bg=BG2)
            sw.pack(side=tk.RIGHT)
            sw.set(bool(initial))
            if hint:
                tk.Label(parent, text=hint, fg="#6b7480", bg=BG2,
                         font=("Segoe UI", 8)).pack(anchor="w", padx=10)
            return sw

        # engine ---------------------------------------------------------
        sec1 = section("ENGINE")
        row = tk.Frame(sec1, bg=BG2)
        row.pack(fill=tk.X, padx=10, pady=(0, 4))
        tk.Label(row, text="Elo:", fg=FG, bg=BG2,
                 font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self.elo_var = tk.StringVar(value=str(self.cfg.get("elo", 1700)))
        cb = ttk.Combobox(row, textvariable=self.elo_var,
                          values=[str(e) for e in ELO_LEVELS],
                          state="readonly", width=8, font=("Segoe UI", 11))
        cb.pack(side=tk.RIGHT)
        cb.bind("<<ComboboxSelected>>", self._on_elo)
        tk.Label(sec1, text="engine plays at the selected level — "
                            "1700 moves like a 1700 human",
                 fg="#6b7480", bg=BG2,
                 font=("Segoe UI", 8)).pack(anchor="w", padx=10)
        self.sw_top3 = switch_row(sec1, "3 move options  (F4)",
                                  self.cfg.get("show_top3", True), self._on_top3,
                                  "green = best, yellow/orange = "
                                  "alternatives")
        self.sw_delay = switch_row(sec1, "Human-delay",
                                   self.cfg.get("human_delay_on", True),
                                   self._on_delay_switch,
                                   "shows the move after random 0.3–1.4s — "
                                   "reaction time looks human")
        drow = tk.Frame(sec1, bg=BG2)
        drow.pack(fill=tk.X, padx=10, pady=(2, 6))
        tk.Label(drow, text="min", fg=FGDIM, bg=BG2,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self.delay_var = tk.DoubleVar(
            value=self.cfg.get("human_delay_min_ms", 300) / 1000.0)
        ttk.Scale(drow, from_=0.2, to=3.0, variable=self.delay_var,
                  command=self._on_delay_slide).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        tk.Label(drow, text="max", fg=FGDIM, bg=BG2,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self.delay_max_var = tk.DoubleVar(
            value=self.cfg.get("human_delay_max_ms", 1400) / 1000.0)
        ttk.Scale(drow, from_=0.3, to=5.0, variable=self.delay_max_var,
                  command=self._on_delay_slide).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        # overlay --------------------------------------------------------
        sec2 = section("OVERLAY")
        self.sw_overlay = switch_row(sec2, "Show overlay  (F3)",
                                     True, self._on_overlay_switch,
                                     "highlights over the board; invisible to OBS "
                                     "and screenshots")
        self.sw_arrows = switch_row(sec2, "Move arrows", True,
                                    self._on_arrow_switch,
                                    "line from square to square + rank badge")

        # board ----------------------------------------------------------
        sec3 = section("BOARD")
        self.sw_autodetect = switch_row(sec3, "Auto-detect board",
                                        self.cfg.get("autodetect", True),
                                        self._on_autodetect_switch,
                                        "finds the board itself + measures its colors; "
                                        "off = manual calibration only")
        brow = tk.Frame(sec3, bg=BG2)
        brow.pack(fill=tk.X, padx=10, pady=(2, 8))
        tk.Button(brow, text="⬚  Calibrate board  (F2)",
                  command=self._calibrate,
                  bg=BG3, fg=FG, activebackground="#353b48",
                  activeforeground=FG, relief=tk.FLAT,
                  font=("Segoe UI", 9), pady=5, cursor="hand2").pack(fill=tk.X)

        # main button + status -------------------------------------------
        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill=tk.X, padx=16, pady=(8, 4))
        self.start_btn = tk.Button(btns, text="▶  Start analysis  (F1)",
                                   command=self._toggle, bg=ACCENT,
                                   fg="#101608", activebackground=ACCENT2,
                                   activeforeground="#101608", relief=tk.FLAT,
                                   font=("Segoe UI", 11, "bold"), pady=8,
                                   cursor="hand2")
        self.start_btn.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="● stopped — press F1 or the button")
        tk.Label(self.root, textvariable=self.status_var, fg=FGDIM, bg=BG2,
                 font=("Segoe UI", 9, "bold"), pady=5).pack(
            fill=tk.X, padx=16, pady=(4, 0))
        tk.Label(self.root, text="log", fg="#707a86", bg=BG,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=18,
                                                    pady=(8, 2))
        self.log = scrolledtext.ScrolledText(self.root, bg="#14171c",
                                             fg="#b9c2cc",
                                             font=("Consolas", 9),
                                             relief=tk.FLAT, state=tk.DISABLED,
                                             height=9)
        self.log.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 4))
        tk.Label(self.root,
                 text="F1 start/stop · F2 calibrate · F3 hide overlay · "
                      "F4 1/3 moves · F5 debug dump",
                 fg="#5a6370", bg=BG, font=("Segoe UI", 8)).pack(pady=(2, 8))

    # -- hotkeys ----------------------------------------------------------------
    def _bind_hotkeys(self):
        def safe(fn):
            def wrapper(*a, **k):
                try:
                    fn()
                except Exception:
                    pass
            return wrapper
        try:
            kb.add_hotkey("F1", safe(self._toggle))
            kb.add_hotkey("F2", safe(self._calibrate))
            kb.add_hotkey("F3", safe(lambda: self.overlay.toggle_hidden()))
            kb.add_hotkey("F4", safe(lambda: self.sw_top3.toggle()))
            kb.add_hotkey("F5", safe(self._dump_debug))
        except Exception:
            pass  # keyboard lib may need admin; GUI switches still work

    def _dump_debug(self):
        if self.worker and self.worker.is_alive():
            self.worker.dump_debug()
        else:
            self._log("start analysis (F1) — then F5 saves the dump")

    # -- handlers -----------------------------------------------------------------
    def _on_top3(self):
        self.cfg["show_top3"] = bool(self.sw_top3.state)
        save_config(self.cfg)

    def _on_delay_switch(self):
        self.cfg["human_delay_on"] = bool(self.sw_delay.state)
        save_config(self.cfg)

    def _on_delay_slide(self, _v=None):
        lo, hi = float(self.delay_var.get()), float(self.delay_max_var.get())
        if hi < lo + 0.1:
            hi = lo + 0.1
            self.delay_max_var.set(hi)
        self.cfg["human_delay_min_ms"] = int(lo * 1000)
        self.cfg["human_delay_max_ms"] = int(hi * 1000)
        save_config(self.cfg)

    def _on_overlay_switch(self):
        want_hidden = not self.sw_overlay.state
        if self.overlay.hidden != want_hidden:
            self.overlay.toggle_hidden()

    def _on_arrow_switch(self):
        self.overlay.show_arrows = bool(self.sw_arrows.state)

    def _on_autodetect_switch(self):
        self.cfg["autodetect"] = bool(self.sw_autodetect.state)
        save_config(self.cfg)

    def _on_elo(self, _e=None):
        try:
            elo = int(self.elo_var.get())
        except ValueError:
            return
        self.cfg["elo"] = elo
        save_config(self.cfg)
        if self.worker and self.worker.engine:
            prof = self.worker.engine.set_profile(elo)
            self._log(f"elo -> {elo} "
                      f"(skill {prof['skill']}, movetime {prof['movetime']}ms)")

    def _toggle(self):
        if self.worker and self.worker.is_alive():
            self._stop()
        else:
            self._start()

    def _start(self):
        self.cfg["human_delay_min_ms"] = int(float(self.delay_var.get()) * 1000)
        self.cfg["human_delay_max_ms"] = int(float(self.delay_max_var.get()) * 1000)
        self.cfg["human_delay_on"] = bool(self.sw_delay.state)
        self.cfg["show_top3"] = bool(self.sw_top3.state)
        save_config(self.cfg)
        self.overlay.show_arrows = bool(self.sw_arrows.state)
        self.worker = Worker(self.cfg, self.ui_queue)
        self.worker.start()
        self.start_btn.config(text="■  Stop (F1)", bg="#c0453b",
                              activebackground="#d95b50")
        self.status_var.set("● starting...")

    def _stop(self):
        if self.worker:
            self.worker.stop_event.set()
            if self.worker.engine:
                try:
                    self.worker.engine.quit()
                except Exception:
                    pass
        self.worker = None
        self.overlay.clear()
        self.start_btn.config(text="▶  Start analysis (F1)", bg=ACCENT,
                              activebackground=ACCENT2)
        self.status_var.set("● stopped")
        self._log("analysis stopped")

    def _calibrate(self):
        box = self.calibrator.select()
        if not box:
            return
        if self.worker and self.worker.is_alive():
            self.worker.detector.set_region(*box)
            self.worker.our_color = (chess.BLACK if self.worker.detector.flip
                                     else chess.WHITE)
            self.worker.full_rescan_needed = True
            self.overlay.set_region(self.worker.detector.region)
            self.cfg["region"] = dict(self.worker.detector.region)
            save_config(self.cfg)
            self._log(f"board recalibrated: {box[2]}x{box[3]} "
                      f"@ ({box[0]},{box[1]})")
        else:
            d = BoardDetector()
            d.set_region(*box)
            self.overlay.set_region(d.region)
            self.cfg["region"] = d.region
            save_config(self.cfg)
            self._log(f"board saved: {box[2]}x{box[3]} @ ({box[0]},{box[1]}) "
                      f"(picked up on start)")

    def _log(self, text):
        stamp = time.strftime("%H:%M:%S")
        self.log.config(state=tk.NORMAL)
        self.log.insert(tk.END, f"[{stamp}] {text}\n")
        self.log.see(tk.END)
        self.log.config(state=tk.DISABLED)

    def _poll(self):
        try:
            while True:
                kind, data = self.ui_queue.get_nowait()
                if kind == "log":
                    self._log(data)
                elif kind == "status":
                    self.status_var.set("● " + data)
                elif kind == "region":
                    self.overlay.set_region(data)
                elif kind == "moves":
                    self.overlay.show_moves(data["moves"], data["flip"],
                                            data["cell"])
                    fen = data.get("fen", "")
                    if fen:
                        self._log(f"fen: {fen.split(' ')[0]}")
                elif kind == "clear":
                    self.overlay.clear()
        except queue.Empty:
            pass
        self._poll_count = getattr(self, "_poll_count", 0) + 1
        if self._poll_count % 32 == 0:  # ~ every 2s keep the overlay on top
            if not self.overlay.hidden:
                self.overlay.root.attributes("-topmost", True)
                self.overlay.root.lift()
        self.root.after(60, self._poll)

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        try:
            self._stop()
        finally:
            self.root.destroy()
