#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChessTool — offline chess.com board assistant.

- Auto/manual board capture from screen (chess.com board themes)
- Piece recognition via template matching (neo piece set)
- Stockfish 19 with elo cap (600..2500) — plays like a human of that level
- Incremental engine analysis: instant replies after opponent move
- Human-like display delay (anti-statistical-detection)
- Stream-proof overlay (WDA_EXCLUDEFROMCAPTURE) + click-through
- Highlights top-3 moves: from-square -> to-square, color-ranked
- Hotkeys: F1 start/stop, F2 calibrate, F3 hide overlay, F4 toggle 1/3 moves

This tool never clicks anything. You move yourself — from the site's point
of view you are just a player looking at the board.
"""

import sys, os, json, time, math, random, threading, queue, subprocess, ctypes
import tkinter as tk
from tkinter import ttk, scrolledtext
import numpy as np
from PIL import Image
import mss
import chess
import keyboard as kb

# ----------------------------------------------------------------------------
# win32 helpers (ctypes, no pywin32 dependency)
# ----------------------------------------------------------------------------
user32 = ctypes.windll.user32
GWL_EXSTYLE        = -20
WS_EX_LAYERED      = 0x00080000
WS_EX_TRANSPARENT  = 0x00000020
WS_EX_NOACTIVATE   = 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

def make_clickthrough_and_hidden(hwnd: int):
    """Overlay: click-through, no activate, invisible to screenshots/OBS."""
    try:
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                 style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    except Exception:
        try:
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        except Exception:
            pass
    try:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass

# ----------------------------------------------------------------------------
# paths & config
# ----------------------------------------------------------------------------
APP_NAME = "ChessTool"

def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.abspath(__file__))

def user_data_dir() -> str:
    if getattr(sys, "frozen", False):
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
    else:
        d = base_dir()
    os.makedirs(d, exist_ok=True)
    return d

def engine_path() -> str:
    bundled = os.path.join(base_dir(), "engines", "stockfish.exe")
    if not getattr(sys, "frozen", False):
        return bundled
    # when frozen: extract engine to a writable temp location once
    dest_dir = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")), "chesstool_engine")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "stockfish.exe")
    if not os.path.exists(dest):
        import shutil
        shutil.copyfile(bundled, dest)
    return dest

def templates_dir() -> str:
    return os.path.join(base_dir(), "templates")

CONFIG_PATH = os.path.join(user_data_dir(), "config.json")

DEFAULT_CONFIG = {
    "elo": 1700,
    "show_top3": True,
    "human_delay_min_ms": 300,
    "human_delay_max_ms": 1400,
    "poll_ms": 120,
}

def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg

def save_config(cfg: dict):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass

# ----------------------------------------------------------------------------
# elo profiles
# ----------------------------------------------------------------------------
ELO_LEVELS = [600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2500]

# below 1600 stockfish UCI_Elo floor is 1320, so we emulate weakness:
# low skill + short movetime + randomized pick among top-N with weights.
_LOW_SKILL = {600: 0, 800: 1, 1000: 2, 1200: 4, 1400: 6}

def profile_for(elo: int) -> dict:
    if elo >= 1600:
        mt = {1600: 150, 1800: 200, 2000: 250, 2200: 320, 2500: 400}.get(elo, 200)
        return {"uci_elo": True, "elo": elo, "skill": 20, "movetime": mt, "weights": None}
    skill = _LOW_SKILL[elo]
    weights = {600: [30, 26, 22, 22], 800: [42, 30, 28], 1000: [55, 25, 20],
               1200: [60, 25, 15], 1400: [65, 22, 13]}[elo]
    mt = 60 + (elo - 600) // 12
    return {"uci_elo": False, "elo": elo, "skill": skill, "movetime": mt, "weights": weights}

# ----------------------------------------------------------------------------
# engine wrapper (raw UCI over pipes, thread-safe)
# ----------------------------------------------------------------------------
class EngineWrapper:
    def __init__(self, path: str):
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                  creationflags=creationflags)
        self.lock = threading.Lock()
        self._send("uci")
        self._wait_for("uciok")
        self.set_profile(1700)
        self._send("setoption name MultiPV value 3")
        self._send("isready")
        self._wait_for("readyok")

    def _send(self, line: str):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def _wait_for(self, token: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine died")
            if line.strip() == token:
                return
        raise RuntimeError("engine timeout waiting for " + token)

    def set_profile(self, elo: int):
        prof = profile_for(elo)
        with self.lock:
            if prof["uci_elo"]:
                try:
                    self._send("setoption name UCI_LimitRating value true")
                    self._send(f"setoption name UCI_Elo value {prof['elo']}")
                    self._send("setoption name Skill Level value 20")
                except Exception:
                    pass
            else:
                try:
                    self._send("setoption name UCI_LimitRating value false")
                except Exception:
                    pass
                self._send(f"setoption name Skill Level value {prof['skill']}")
            self._send("isready")
            self._wait_for("readyok")
        return prof

    def analyse(self, board: chess.Board, movetime_ms: int):
        """Returns list of (move, score_string, rank, centipawns) — up to 3 lines."""
        with self.lock:
            self._send("position fen " + board.fen())
            self._send(f"go movetime {int(movetime_ms)}")
            lines = {}
            best = None
            deadline = time.time() + 15.0
            while time.time() < deadline:
                raw = self.p.stdout.readline()
                if not raw:
                    break
                line = raw.strip()
                if line.startswith("info") and " pv " in line:
                    multipv = 1
                    if " multipv " in line:
                        multipv = int(line.split(" multipv ")[1].split()[0])
                    if " score " in line:
                        sc_part = line.split(" score ")[1].split()
                        if sc_part[0] == "cp":
                            cp = int(sc_part[1])
                            score = f"{cp/100:+.2f}"
                        elif sc_part[0] == "mate":
                            mt = int(sc_part[1])
                            cp = 10000 if mt > 0 else -10000
                            score = f"M{mt}"
                        else:
                            cp = 0
                            score = "?"
                        pvmove = line.split(" pv ")[1].split()[0]
                        lines[multipv] = (pvmove, score, cp)
                elif line.startswith("bestmove"):
                    best = line.split()[1] if len(line.split()) > 1 else None
                    break
            result = []
            for k in sorted(lines.keys())[:3]:
                mv, sc, cp = lines[k]
                if mv != "(none)":
                    result.append((mv, sc, k, cp))
            if not result and best and best != "(none)":
                result.append((best, "?", 1, 0))
            return result

    def quit(self):
        try:
            with self.lock:
                self._send("quit")
            self.p.terminate()
        except Exception:
            pass

# ----------------------------------------------------------------------------
# board detection & piece recognition
# ----------------------------------------------------------------------------
# chess.com standard themes (light square, dark square)
THEMES = [
    ((238, 238, 210), (118, 153,  86)),   # green
    ((240, 217, 181), (181, 136,  99)),   # brown
    ((227, 237, 249), (116, 127, 146)),   # blue
    ((238, 238, 238), (118, 118, 118)),   # grey / newspaper
    ((233, 231, 200), (120, 128,  89)),   # muted green-ish
    ((247, 245, 209), ( 89, 122,  71)),   # walnut-ish
    ((208, 236, 235), ( 82, 149, 115)),   # mint/teal (user's theme)
    ((222, 227, 230), ( 47,  61,  85)),   # dark navy-ish
    ((235, 236, 209), (110, 139,  87)),   # green v2
]
TOL = 26

def _rgb_close(px, ref, tol=TOL):
    return (abs(int(px[0]) - ref[0]) <= tol and
            abs(int(px[1]) - ref[1]) <= tol and
            abs(int(px[2]) - ref[2]) <= tol)

def sq_name(col: int, row: int, flip: bool) -> str:
    """row 0 = top of captured region."""
    if not flip:
        return chr(ord('a') + col) + str(8 - row)
    return chr(ord('h') - col) + str(row + 1)

class BoardState:
    """Incremental square cache for fast re-recognition."""
    def __init__(self):
        self.squares = {}      # (col, row) -> piece char or None
        self.sign = {}         # (col, row) -> (mean, std) fingerprint
        self.ok = False

    def by_name(self, flip: bool) -> dict:
        return {sq_name(c, r, flip): pc for (c, r), pc in self.squares.items() if pc}

class BoardDetector:
    def __init__(self):
        self.region = None     # dict left, top, width, height
        self.flip = False
        self.cell = 0
        self.templates = {}    # char -> PIL Image (resized lazily)
        self._tpl_cache = {}   # (char, size) -> (array, mask)
        self._load_templates()

    # ------------------------------------------------------------------
    def _load_templates(self):
        names = {"P": "wp", "N": "wn", "B": "wb", "R": "wr", "Q": "wq", "K": "wk",
                 "p": "bp", "n": "bn", "b": "bb", "r": "br", "q": "bq", "k": "bk"}
        for ch, fn in names.items():
            p = os.path.join(templates_dir(), fn + ".png")
            if os.path.exists(p):
                self.templates[ch] = Image.open(p).convert("RGBA")

    def _tpl(self, ch: int, size: int):
        key = (ch, size)
        if key not in self._tpl_cache:
            img = self.templates[ch].resize((size, size), Image.LANCZOS)
            arr = np.asarray(img, dtype=np.float32)
            rgb = arr[:, :, :3]
            alpha = arr[:, :, 3] / 255.0
            mask = (alpha > 0.55).astype(np.float32)
            # erode mask ~2px to drop anti-aliased edges (sum of 5x5 shifts)
            k = 2
            if size >= 40 and mask.shape[0] > 2 * k and mask.shape[1] > 2 * k:
                m = mask.astype(np.int32)
                h, w = m.shape
                win = 2 * k + 1
                s = np.zeros((h - 2 * k, w - 2 * k), dtype=np.int32)
                for dy in range(win):
                    for dx in range(win):
                        s += m[dy:dy + h - 2 * k, dx:dx + w - 2 * k]
                er = np.zeros_like(m)
                er[k:h - k, k:w - k] = (s == win * win)
                if er.sum() > size * size * 0.08:  # erosion didn't eat everything
                    mask = er.astype(np.float32)
            self._tpl_cache[key] = (rgb, mask)
        return self._tpl_cache[key]

    # ------------------------------------------------------------------
    def auto_detect(self) -> bool:
        """Scan all monitors for an 8x8 light/dark grid. Sets self.region."""
        with mss.mss() as sct:
            for mon in sct.monitors[1:]:
                img = np.asarray(sct.grab(mon), dtype=np.uint8)[:, :, :3]
                found = self._find_grid_in(img)
                if found:
                    x, y, w, h = found
                    self.region = {"left": mon["left"] + x, "top": mon["top"] + y,
                                   "width": w, "height": h}
                    self._post_region()
                    return True
        return False

    def _find_grid_in(self, img: np.ndarray):
        h, w = img.shape[:2]
        if w < 300 or h < 300:
            return None
        # downsample x2 for speed
        small = img[::2, ::2]
        sm_h, sm_w = small.shape[:2]
        light_mask = np.zeros((sm_h, sm_w), dtype=bool)
        dark_mask = np.zeros((sm_h, sm_w), dtype=bool)
        for lt, dk in THEMES:
            light_mask |= (np.abs(small.astype(np.int16) - np.int16(lt)).max(axis=2) <= TOL)
            dark_mask  |= (np.abs(small.astype(np.int16) - np.int16(dk)).max(axis=2) <= TOL)
        board_mask = light_mask | dark_mask
        # local window densities (window ~= max board size / 2)
        win = max(20, min(sm_h, sm_w) // 12)
        # column density within the best row-window, row density within best col-window
        row_sum = board_mask.sum(axis=1)  # per row
        col_sum = board_mask.sum(axis=0)  # per col
        # rows: a row belongs if col_sum in a +-win neighborhood of its peak region
        if board_mask.sum() < win * win:
            return None
        # find dense row band
        row_win = np.convolve(row_sum, np.ones(win, dtype=np.int64), mode="same")
        y_peak = int(np.argmax(row_win))
        y_lo, y_hi = y_peak - win, y_peak + win
        y0, y1 = 0, sm_h - 1
        thr = row_win[y_peak] * 0.35
        ys = np.where(row_win >= thr)[0]
        if len(ys) == 0:
            return None
        y0, y1 = int(ys[0]), int(ys[-1])
        # columns within that row band
        band = board_mask[y0:y1 + 1]
        col_band = band.sum(axis=0)
        col_win = np.convolve(col_band, np.ones(win, dtype=np.int64), mode="same")
        x_peak = int(np.argmax(col_win))
        thr2 = col_win[x_peak] * 0.35
        xs = np.where(col_win >= thr2)[0]
        if len(xs) == 0:
            return None
        x0, x1 = int(xs[0]), int(xs[-1])
        bw = (x1 - x0) * 2
        bh = (y1 - y0) * 2
        if bw < 300 or bh < 300:
            return None
        if abs(bw - bh) > max(bw, bh) * 0.18:
            return None
        # density check inside box
        inner = board_mask[y0:y1 + 1, x0:x1 + 1]
        if inner.size == 0 or inner.mean() < 0.55:
            return None
        # shrink to a multiple-of-8 box
        bw = (bw // 8) * 8
        bh = (bh // 8) * 8
        side = min(bw, bh)
        # verify checkerboard alternation on a coarse grid
        side_s = side // 2
        cs = side_s // 8
        if cs < 4:
            return None
        ok_cells = 0
        total_cells = 0
        for r in range(8):
            for c in range(8):
                y = y0 + r * cs + cs // 2
                x = x0 + c * cs + cs // 2
                if y >= sm_h or x >= sm_w:
                    continue
                total_cells += 1
                is_light_expected = (r + c) % 2 == 0
                px = small[y, x]
                if is_light_expected:
                    for lt, _ in THEMES:
                        if _rgb_close(px, lt):
                            ok_cells += 1
                            break
                else:
                    for _, dk in THEMES:
                        if _rgb_close(px, dk):
                            ok_cells += 1
                            break
        if total_cells == 0 or ok_cells / total_cells < 0.60:
            return None
        return (x0 * 2, y0 * 2, side, side)

    # ------------------------------------------------------------------
    def set_region(self, left, top, width, height) -> bool:
        self.region = {"left": int(left), "top": int(top),
                       "width": int(width), "height": int(height)}
        self._post_region()
        return True

    def _post_region(self):
        r = self.region
        orig = dict(r)
        # auto-grow: if the checkerboard mask touches a region edge, the user
        # frame clipped the board — expand (screen-limited), up to 3 rounds
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        grow = {"top": 0, "left": 0, "right": 0, "bottom": 0}
        for _ in range(3):
            with mss.mss() as sct:
                img = np.asarray(sct.grab({"left": r["left"], "top": r["top"],
                                           "width": r["width"], "height": r["height"]}),
                                 dtype=np.uint8)[:, :, :3]
            m = self._board_mask(img)
            t_t = m[0:3].mean() > 0.5
            t_b = m[-3:].mean() > 0.5
            t_l = m[:, 0:3].mean() > 0.5
            t_r = m[:, -3:].mean() > 0.5
            if not (t_t or t_b or t_l or t_r):
                break
            if t_t:
                grow["top"] += 80
            if t_b:
                grow["bottom"] += 80
            if t_l:
                grow["left"] += 80
            if t_r:
                grow["right"] += 80
            r["left"] = max(0, orig["left"] - grow["left"])
            r["top"] = max(0, orig["top"] - grow["top"])
            r["width"] = min(screen_w - r["left"],
                             orig["width"] + grow["left"] + grow["right"])
            r["height"] = min(screen_h - r["top"],
                              orig["height"] + grow["top"] + grow["bottom"])
        with mss.mss() as sct:
            img = np.asarray(sct.grab({"left": r["left"], "top": r["top"],
                                       "width": r["width"], "height": r["height"]}),
                             dtype=np.uint8)[:, :, :3]
        # refine grid: subpixel alignment from light/dark transitions
        rf = self._refine_grid(img)
        if rf is not None:
            gx, gy, gside = rf
            r["left"] += gx
            r["top"] += gy
            r["width"] = gside
            r["height"] = gside
        # fractional cell size: browser zoom gives non-integer boards
        self.cw = r["width"] / 8.0
        self.ch = r["height"] / 8.0
        self.cell = int(round(r["width"] / 8.0))
        # NOTE: flip is detected from piece placement after the first full scan.

    def _refine_grid(self, img: np.ndarray):
        """Subpixel board alignment from light<->dark transitions.
        Returns (left, top, side) inside img, or None."""
        sm = img.astype(np.int16)
        light = np.zeros(img.shape[:2], dtype=bool)
        dark = np.zeros(img.shape[:2], dtype=bool)
        for lt, dk in THEMES:
            light |= (np.abs(sm - np.int16(lt)).max(axis=2) <= TOL)
            dark |= (np.abs(sm - np.int16(dk)).max(axis=2) <= TOL)
        both = light | dark
        h, w = img.shape[:2]
        est_c = w / 8.0

        def transitions_line(is_row: bool, idx: int):
            if is_row:
                a = light[idx]; b = dark[idx]
            else:
                a = light[:, idx]; b = dark[:, idx]
            lab = np.zeros(a.shape[0], dtype=np.int8)
            lab[a] = 1
            lab[b] = 2
            ok = np.zeros(a.shape[0], dtype=bool)
            ok[a | b] = True
            xs = []
            for i in range(1, len(lab)):
                if ok[i] and ok[i - 1] and lab[i] != lab[i - 1]:
                    xs.append(i)
            return xs

        def fit_axis(is_row: bool):
            spans = []
            for frac_y in (0.22, 0.28, 0.34, 0.46, 0.52, 0.58, 0.64, 0.72, 0.78, 0.84):
                idx = int(frac_y * (h if is_row else w))
                idx = min(idx, (h if is_row else w) - 1)
                xs = transitions_line(is_row, idx)
                if len(xs) == 7:
                    diffs = [xs[i + 1] - xs[i] for i in range(6)]
                    if all(0.7 * est_c <= d <= 1.3 * est_c for d in diffs):
                        spans.append(xs)
            if not spans:
                return None
            # aggregate: first/last transition positions
            firsts = [s[0] for s in spans]
            lasts = [s[-1] for s in spans]
            first = float(np.median(firsts))
            last = float(np.median(lasts))
            cw = (last - first) / 6.0
            left = first - cw
            side = 8.0 * cw
            return left, side

        fx = fit_axis(True)   # rows -> vertical transitions give x-grid
        fy = fit_axis(False)  # columns -> horizontal transitions give y-grid
        if fx is None or fy is None:
            return None
        left, side_x = fx
        top, side_y = fy
        side = (side_x + side_y) / 2.0
        if side < 300 or side > max(h, w) + 8:
            return None
        left = int(round(max(0, min(left, w - side))))
        top = int(round(max(0, min(top, h - side))))
        return left, top, int(round(side))

    def _snap_to_checker(self, img: np.ndarray):
        """Trim outer frame/rails around the actual 8x8 checkerboard.
        Returns (dx, dy, w, h) of the board inside the image."""
        m = self._board_mask(img)
        h, w = m.shape
        strip = 4
        def dens_rows(y0, y1):
            return m[y0:y1].mean() if y1 > y0 else 0.0
        def dens_cols(x0, x1):
            return m[:, x0:x1].mean() if x1 > x0 else 0.0
        top = 0
        while top + strip <= h // 2 and dens_rows(top, top + strip) < 0.55:
            top += strip
        bottom = h
        while bottom - strip >= h // 2 and dens_rows(bottom - strip, bottom) < 0.55:
            bottom -= strip
        left = 0
        while left + strip <= w // 2 and dens_cols(left, left + strip) < 0.55:
            left += strip
        right = w
        while right - strip >= w // 2 and dens_cols(right - strip, right) < 0.55:
            right -= strip
        # expand back by one strip to not eat the outer row/col of cells
        left = max(0, left - strip)
        top = max(0, top - strip)
        right = min(w, right + strip)
        bottom = min(h, bottom + strip)
        if right - left < 300 or bottom - top < 300:
            return 0, 0, w, h
        return left, top, right - left, bottom - top

    def _board_mask(self, img: np.ndarray) -> np.ndarray:
        sm = img.astype(np.int16)
        m = np.zeros(img.shape[:2], dtype=bool)
        for lt, dk in THEMES:
            m |= (np.abs(sm - np.int16(lt)).max(axis=2) <= TOL)
            m |= (np.abs(sm - np.int16(dk)).max(axis=2) <= TOL)
        return m

    # ------------------------------------------------------------------
    def capture(self) -> np.ndarray:
        r = self.region
        with mss.mss() as sct:
            img = np.asarray(sct.grab({"left": r["left"], "top": r["top"],
                                       "width": r["width"], "height": r["height"]}),
                             dtype=np.uint8)[:, :, :3]
        return img

    # ------------------------------------------------------------------
    def board_looks_valid(self, img: np.ndarray) -> bool:
        c = self.cell
        if c < 8:
            return False
        hits = 0
        total = 0
        pad = max(3, c // 10)          # inset from cell border
        ps = max(3, c // 14)           # patch size
        for r in range(0, 8, 2):
            for cc in range(0, 8, 2):
                y = int(r * self.ch)
                x = int(cc * self.cw)
                cw = int((cc + 1) * self.cw) - x
                chh = int((r + 1) * self.ch) - y
                corners = [
                    (y + pad, x + pad),
                    (y + pad, x + cw - pad - ps),
                    (y + chh - pad - ps, x + pad),
                    (y + chh - pad - ps, x + cw - pad - ps),
                ]
                cell_ok = False
                for (yy, xx) in corners:
                    if (yy + ps <= img.shape[0] and xx + ps <= img.shape[1]
                            and yy >= 0 and xx >= 0):
                        patch = img[yy:yy + ps, xx:xx + ps].reshape(-1, 3).mean(axis=0)
                        for lt, dk in THEMES:
                            if _rgb_close(patch, lt) or _rgb_close(patch, dk):
                                cell_ok = True
                                break
                    if cell_ok:
                        break
                total += 1
                if cell_ok:
                    hits += 1
        return hits >= 6  # 16 sampled cells; pieces can cover several centres

    # ------------------------------------------------------------------
    def _cell_bg(self, cell_img: np.ndarray):
        """Modal (most frequent) color of the cell = square background.
        Robust to pieces (they cover <50%), coordinates letters and highlights."""
        q = (cell_img[:, :, :3] // 8).astype(np.int32)
        flat = (q[:, :, 0] << 16) | (q[:, :, 1] << 8) | q[:, :, 2]
        counts = np.bincount(flat.ravel(), minlength=(1 << 24))
        mode_code = int(np.argmax(counts))
        r = (mode_code >> 16) & 0xFF
        g = (mode_code >> 8) & 0xFF
        b = mode_code & 0xFF
        return np.array([r, g, b], dtype=np.float32) * 8.0 + 4.0

    def _fingerprint(self, cell_img: np.ndarray):
        g = cell_img.astype(np.float32)
        return float(g.mean()), float(g.std())

    def _classify_cell(self, cell_img: np.ndarray, bg: np.ndarray, size: int):
        """returns (piece | None | '?', best_guess_char)"""
        f = cell_img.astype(np.float32)
        # empty check: fraction of pixels far from modal background color
        dmax = np.abs(f[:, :, :3] - bg).max(axis=2)
        frac = float((dmax > 22).mean())
        if frac < 0.10:
            return None, None
        # template match over eroded piece masks (background not compared)
        # but split candidates by piece colour first:
        # piece pixels = far from bg; their brightness decides white vs black
        piece_px = dmax > 22
        if piece_px.sum() < 4:
            return None, None
        brightness = f[:, :, :3][piece_px].mean()
        # white pieces are bright (>=140), black pieces dark (<=110)
        if brightness >= 125.0:
            candidates = "PNBRQK"
        elif brightness <= 110.0:
            candidates = "pnbrqk"
        else:
            candidates = "PNBRQKpnbrqk"  # ambiguous (highlighted cells)
        best_ch, best_mse, second = None, 1e9, 1e9
        for ch in self.templates:
            if ch not in candidates:
                continue
            tpl, mask = self._tpl(ch, size)
            if mask.sum() < 1:
                continue
            diff = (f[:, :, :3] - tpl) * mask[:, :, None]
            mse = float((diff * diff).sum() / (mask.sum() * 3.0))
            # outside penalty: non-background pixels sticking out of the
            # template mask mean the piece is bigger than this template
            outside = (mask < 0.5) & piece_px
            if outside.sum() > 2:
                out_dev = ((f[:, :, :3] - bg) ** 2).sum(axis=2)[outside].mean() / 3.0
                mse += 0.05 * out_dev
            if mse < best_mse:
                second = best_mse
                best_ch, best_mse = ch, mse
            elif mse < second:
                second = mse
        if best_ch is None:
            return "?", None
        if best_mse < 900.0:
            return best_ch, best_ch
        if best_mse < 2800.0 and (second - best_mse) > 25.0:
            return best_ch, best_ch
        return "?", best_ch  # uncertain, but best-guess candidate attached

    def _classify_cell_compat(self, cell_img, bg, size):
        piece, _ = self._classify_cell(cell_img, bg, size)
        return piece

    def determine_flip(self, st: BoardState) -> bool:
        """True = we play black (white pieces are at the TOP of the screen)."""
        wp_rows = [row for (col, row), pc in st.squares.items() if pc == "P"]
        if wp_rows:
            bottom = sum(1 for r in wp_rows if r >= 4)
            return bottom < len(wp_rows) / 2.0  # majority on top -> flip
        wk = [row for (col, row), pc in st.squares.items() if pc == "K"]
        if wk:
            return sum(wk) / len(wk) < 4.0  # white king top -> flip
        return False

    def recognize(self, img: np.ndarray, prev: BoardState, full: bool = False):
        """Returns (new BoardState, set_of_changed (col,row)). Incremental when possible."""
        st = BoardState()
        # square size actually used for template matching
        tsize = self.cell if self.cell <= 96 else 96
        changed = set()
        for row in range(8):
            for col in range(8):
                y0 = int(row * self.ch)
                y1 = int((row + 1) * self.ch)
                x0 = int(col * self.cw)
                x1 = int((col + 1) * self.cw)
                cell = img[y0:y1, x0:x1]
                if cell.size == 0:
                    continue
                cw = x1 - x0
                chh = y1 - y0
                if not full:
                    m = max(3, min(cw, chh) // 8)
                    core = cell[m:chh - m, m:cw - m]
                    fp = self._fingerprint(core)
                    old = prev.sign.get((col, row))
                    if old is not None and abs(fp[0] - old[0]) < 4.0 and abs(fp[1] - old[1]) < 5.0:
                        st.squares[(col, row)] = prev.squares.get((col, row))
                        st.sign[(col, row)] = old
                        continue
                changed.add((col, row))
                bg = self._cell_bg(cell)
                if (cw, chh) != (tsize, tsize):
                    ci = Image.fromarray(cell).resize((tsize, tsize), Image.BILINEAR)
                    cell = np.asarray(ci, dtype=np.uint8)
                piece, guess = self._classify_cell(cell, bg, tsize)
                if piece == "?" and full and guess is not None:
                    # full scan: trust the best-guess candidate instead of
                    # blocking board assembly with a fatal '?'
                    piece = guess
                st.squares[(col, row)] = piece
                m = max(3, min(cw, chh) // 8)
                core = img[y0 + m:y1 - m, x0 + m:x1 - m]
                st.sign[(col, row)] = self._fingerprint(core)
        st.ok = True
        return st, changed

    def build_board(self, st: BoardState, our_turn: bool):
        """Construct a python-chess Board from recognized squares (flip-aware)."""
        pieces = {}
        for (col, row), pc in st.squares.items():
            if pc is None:
                continue
            if pc == "?":
                return None  # uncertain -> caller does a full rescan
            pieces[sq_name(col, row, self.flip)] = pc
        placement = []
        for rank in range(8, 0, -1):  # rank 8 down to 1
            rank_parts = []
            empty = 0
            for file in "abcdefgh":
                pc = pieces.get(file + str(rank))
                if pc is None:
                    empty += 1
                else:
                    if empty:
                        rank_parts.append(str(empty))
                        empty = 0
                    rank_parts.append(pc)
            if empty:
                rank_parts.append(str(empty))
            placement.append("".join(rank_parts))
        fen = "/".join(placement) + (" w " if our_turn else " b ") + "KQkq - 0 1"
        try:
            b = chess.Board(fen)
            if b.king(chess.WHITE) is None or b.king(chess.BLACK) is None:
                return None
            return b
        except Exception:
            return None

# ----------------------------------------------------------------------------
# overlay window (transparent, topmost, click-through, capture-excluded)
# ----------------------------------------------------------------------------
class Overlay:
    TRANSPARENT_KEY = "#ff00fe"
    COLORS = ["#39d353", "#f7c948", "#ff8c42"]  # rank 1..3

    def __init__(self, owner: tk.Tk):
        self.owner = owner
        self.root = tk.Toplevel(owner)
        self.root.title(APP_NAME + " overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", self.TRANSPARENT_KEY)
        self.root.config(bg=self.TRANSPARENT_KEY)
        self.canvas = tk.Canvas(self.root, bg=self.TRANSPARENT_KEY, highlightthickness=0)
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self.hidden = False
        self.show_arrows = True
        self._moves = None
        self._region = None
        self._layout()

    def _layout(self):
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{sw}x{sh}+0+0")
        self.canvas.config(width=sw, height=sh)
        self.root.update_idletasks()
        make_clickthrough_and_hidden(self.root.winfo_id())
        self.root.withdraw()
        self._shown = False

    def set_region(self, region: dict):
        self._region = region

    def show_moves(self, moves, flip: bool, cell: int):
        """moves: list of dicts {uci, score, rank, pct, star}"""
        if self._region is None or not moves:
            return
        self._moves = (list(moves), flip, cell)
        if self.hidden:
            return
        if not self._shown:
            self.root.deiconify()
            make_clickthrough_and_hidden(self.root.winfo_id())
            self._shown = True
        self._redraw()

    def _sq_rect(self, sq: str, flip: bool, cell: int):
        file_idx = ord(sq[0]) - ord('a')
        rank_idx = int(sq[1]) - 1
        if not flip:
            col, row = file_idx, 7 - rank_idx
        else:
            col, row = 7 - file_idx, rank_idx
        r = self._region
        x = r["left"] + col * cell
        y = r["top"] + row * cell
        return x, y, cell

    def _redraw(self):
        self.canvas.delete("all")
        if not self._moves:
            return
        moves, flip, cell = self._moves
        for m in moves:
            uci = m["uci"]
            if len(uci) < 4:
                continue
            frm, to = uci[:2], uci[2:4]
            rank = m.get("rank", 1)
            color = self.COLORS[min(rank - 1, 2)]
            x1, y1, c = self._sq_rect(frm, flip, cell)
            x2, y2, _ = self._sq_rect(to, flip, cell)
            pad = max(2, c // 16)
            w = max(3, c // 14)
            # from-square: thick outline
            self.canvas.create_rectangle(x1 + pad, y1 + pad, x1 + c - pad, y1 + c - pad,
                                         outline=color, width=w)
            # to-square: outline + hatching (semi-transparent illusion)
            self.canvas.create_rectangle(x2 + pad, y2 + pad, x2 + c - pad, y2 + c - pad,
                                         outline=color, width=w)
            step = max(6, c // 7)
            for d in range(-c, c, step):
                self.canvas.create_line(x2 + max(0, d), y2, x2 + d + c, y2 + c,
                                        fill=color, width=max(1, w // 3))
            # arrow center-to-center
            if self.show_arrows:
                cx1, cy1 = x1 + c // 2, y1 + c // 2
                cx2, cy2 = x2 + c // 2, y2 + c // 2
                dx, dy = cx2 - cx1, cy2 - cy1
                L = math.hypot(dx, dy) or 1.0
                ux, uy = dx / L, dy / L
                ex, ey = cx2 - ux * (c // 2.4), cy2 - uy * (c // 2.4)
                self.canvas.create_line(cx1 + ux * (c // 3.4), cy1 + uy * (c // 3.4), ex, ey,
                                        fill=color, width=w + 1)
                ah = max(8, c // 3)
                self.canvas.create_polygon(ex + ux * ah, ey + uy * ah,
                                           ex - uy * ah * 0.55 - ux * ah * 0.35,
                                           ey + ux * ah * 0.55 - uy * ah * 0.35,
                                           ex + uy * ah * 0.55 - ux * ah * 0.35,
                                           ey - ux * ah * 0.55 - uy * ah * 0.35,
                                           fill=color, outline=color)
            # rank badge
            bx = x1 + c - pad * 2
            by = y1 + pad * 2
            rr = max(7, c // 6)
            self.canvas.create_oval(bx - rr, by - rr, bx + rr, by + rr, fill=color,
                                    outline="#101010", width=1)
            badge = ("★" if m.get("star") else str(rank))
            self.canvas.create_text(bx, by, text=badge, fill="#0a0a0a",
                                    font=("Segoe UI", max(8, c // 7), "bold"))
            # score + pct label near the to-square
            label = f"{to}  {m.get('score','')}  {m.get('pct',0):.0f}%"
            fs = max(9, c // 6)
            tx = x2 + c // 2
            ty = y2 - pad * 2
            self.canvas.create_text(tx, ty, text=" " + label + " ", fill="#ffffff",
                                    font=("Segoe UI", fs, "bold"), anchor=tk.S)
            # dark chip behind the text for readability
            self.canvas.create_text(tx, ty, text=" " + label + " ", fill="#111111",
                                    font=("Segoe UI", fs, "bold"), anchor=tk.S,
                                    offset=1)
            self.canvas.tag_raise(None)

    def toggle_hidden(self) -> bool:
        self.hidden = not self.hidden
        if self.hidden:
            self.root.withdraw()
        else:
            self.root.deiconify()
            make_clickthrough_and_hidden(self.root.winfo_id())
            self._redraw()
        return self.hidden

    def clear(self):
        self._moves = None
        if self._shown and not self.hidden:
            self.root.withdraw()
            self._shown = False

# ----------------------------------------------------------------------------
# calibrator (fullscreen 2-click region selector)
# ----------------------------------------------------------------------------
class Calibrator:
    def __init__(self, root_owner: tk.Tk):
        self.owner = root_owner

    def select(self) -> tuple | None:
        """Returns (left, top, width, height) or None."""
        result = {"box": None}
        win = tk.Toplevel(self.owner)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.30)
        win.config(bg="#101418", cursor="crosshair")
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"{sw}x{sh}+0+0")
        lbl = tk.Label(win, text="Кликни два противоположных УГЛА ДОСКИ (по внешним краям клеток)\nEsc — отмена",
                       fg="#ffffff", bg="#1c1f26", font=("Segoe UI", 14, "bold"), pady=10)
        lbl.pack(side=tk.TOP, fill=tk.X)
        cv = tk.Canvas(win, bg="#101418", highlightthickness=0)
        cv.pack(fill=tk.BOTH, expand=True)
        state = {"p1": None, "p2": None, "rect": None}

        def on_click(e):
            if state["p1"] is None:
                state["p1"] = (e.x_root, e.y_root)
            else:
                state["p2"] = (e.x_root, e.y_root)
                x1, y1 = state["p1"]; x2, y2 = state["p2"]
                box = (min(x1, x2), min(y1, y2), abs(x2 - x1), abs(y2 - y1))
                if box[2] > 200 and box[3] > 200:
                    result["box"] = box
                win.destroy()

        def on_move(e):
            if state["p1"] is None:
                return
            cv.delete("all")
            x1, y1 = state["p1"]
            cv.create_rectangle(min(x1, e.x_root), min(y1, e.y_root),
                                max(x1, e.x_root), max(y1, e.y_root),
                                outline="#39d353", width=3)

        def on_esc(e):
            win.destroy()

        cv.bind("<Button-1>", on_click)
        cv.bind("<Motion>", on_move)
        win.bind("<Escape>", on_esc)
        win.focus_force()
        self.owner.wait_window(win)
        return result["box"]

# ----------------------------------------------------------------------------
# worker thread: capture -> recognize -> detect move -> engine -> queue
# ----------------------------------------------------------------------------
class Worker(threading.Thread):
    def __init__(self, cfg: dict, ui_queue: queue.Queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.ui = ui_queue
        self.detector = BoardDetector()
        self.engine = None
        self.stop_event = threading.Event()
        self.board = chess.Board()
        self.prev_state = BoardState()
        self.our_color = chess.WHITE
        self.full_rescan_needed = True
        self.awaiting_our_move = False

    def log(self, text):
        self.ui.put(("log", text))

    def status(self, text):
        self.ui.put(("status", text))

    def moves(self, data):
        self.ui.put(("moves", data))

    def dump_debug(self):
        """F5: save board screenshot + recognized squares for diagnosis."""
        try:
            dbg = os.path.join(user_data_dir(), "debug")
            os.makedirs(dbg, exist_ok=True)
            img = self.detector.capture()
            Image.fromarray(img).save(os.path.join(dbg, "board.png"))
            lines = []
            for row in range(8):
                lines.append(" ".join(
                    (self.prev_state.squares.get((col, row)) or ".")
                    for col in range(8)))
            with open(os.path.join(dbg, "squares.txt"), "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n\n" + self.board.fen() + "\n")
            self.log(f"debug сохранён: {dbg}\\board.png + squares.txt")
        except Exception as e:
            self.log("debug ошибка: " + str(e))

    # ------------------------------------------------------------------
    def run(self):
        self.status("поиск доски...")
        saved = self.cfg.get("region")
        if saved and all(k in saved for k in ("left", "top", "width", "height")):
            try:
                self.detector.set_region(saved["left"], saved["top"],
                                         saved["width"], saved["height"])
                self.status("использую сохранённую область доски")
            except Exception:
                self.detector.region = None
        if self.detector.region is None and not self.detector.auto_detect():
            self.status("доска не найдена — нажми F2 и выдели рамкой")
            while not self.stop_event.is_set() and self.detector.region is None:
                time.sleep(0.3)
            if self.stop_event.is_set():
                return
        r = self.detector.region
        self.our_color = chess.BLACK if self.detector.flip else chess.WHITE
        self.ui.put(("region", dict(r)))
        self.status(f"доска: {r['width']}x{r['height']} @ ({r['left']},{r['top']})")
        try:
            self.engine = EngineWrapper(engine_path())
        except Exception as e:
            self.status("ошибка движка: " + str(e))
            return
        prof = self.engine.set_profile(int(self.cfg.get("elo", 1700)))
        self.log(f"движок: Stockfish 19, эло {self.cfg.get('elo')}, "
                 f"skill {prof['skill']}, movetime {prof['movetime']}мс")

        poll = int(self.cfg.get("poll_ms", 120)) / 1000.0
        while not self.stop_event.is_set():
            try:
                self._step()
            except Exception as e:
                self.status("ошибка: " + str(e))
                time.sleep(1.0)
            time.sleep(poll)

    # ------------------------------------------------------------------
    def _step(self):
        img = self.detector.capture()
        if not self.detector.board_looks_valid(img):
            self.status("доска не видна (перекрыта?)")
            return
        if self.full_rescan_needed:
            st, _ = self.detector.recognize(img, BoardState(), full=True)
            self.detector.flip = self.detector.determine_flip(st)
            self.our_color = chess.BLACK if self.detector.flip else chess.WHITE
            b = self.detector.build_board(st, our_turn=True)
            if b is None:
                self.status("позиция не распознана, пробую ещё...")
                return
            # fix side-to-move: assume it's our move at rescan
            b.turn = self.our_color
            self.board = b
            self.prev_state = st
            self.full_rescan_needed = False
            self.awaiting_our_move = False
            self.status(f"позиция захвачена, играем "
                        f"{'чёрными' if self.detector.flip else 'белыми'} — слежу за доской")
            self.log(f"fen: {self.board.fen()}")
            return

        st, changed = self.detector.recognize(img, self.prev_state)
        if not changed:
            return  # nothing moved

        # figure out which legal move leads to recognized position
        board = self.board.copy(stack=False)
        candidate = None
        piece_map_new = st.by_name(self.detector.flip)
        for mv in board.legal_moves:
            board.push(mv)
            if {chess.square_name(sq): pc.symbol()
                    for sq, pc in board.piece_map().items()} == piece_map_new:
                candidate = mv
                board.pop()
                break
            board.pop()

        if candidate is None:
            # maybe two moves happened quickly (ours + theirs) or glitch — rescan
            self.full_rescan_needed = True
            return

        mover_is_opponent = (board.turn != self.our_color)

        if mover_is_opponent:
            # compute san BEFORE pushing (copy without stack can't pop)
            try:
                san = board.san(candidate)
            except Exception:
                san = candidate.uci()
            self.board.push(candidate)
            self.prev_state = st
            self.log(f"противник: {san}")
            self.status("думаю...")
            t0 = time.time()
            lines = self.engine.analyse(self.board,
                                        profile_for(int(self.cfg.get("elo", 1700)))["movetime"])
            engine_ms = int((time.time() - t0) * 1000)
            if not lines:
                self.status("движок не дал ход")
                return
            # percent scores: quality relative to the best line
            best_cp = max(l[3] for l in lines)
            show = []
            for mv, sc, rank, cp in lines:
                pct = 100.0 * math.exp(-(best_cp - cp) / 80.0)
                pct = max(0.0, min(100.0, pct))
                show.append({"uci": mv, "score": sc, "rank": rank, "pct": pct})
            # low-elo: randomized pick with weights (starred)
            prof = profile_for(int(self.cfg.get("elo", 1700)))
            if prof["weights"] and len(show) > 1:
                ws = prof["weights"][:len(show)]
                s = sum(ws)
                r = random.random() * s
                acc = 0
                chosen = show[0]
                for i, wv in enumerate(ws):
                    acc += wv
                    if r <= acc:
                        chosen = show[i]
                        break
                chosen["star"] = True
            if not self.cfg.get("show_top3", True):
                show = show[:1]
            # human delay — keeps reaction time statistically human
            if self.cfg.get("human_delay_on", True):
                dmin = int(self.cfg.get("human_delay_min_ms", 300)) / 1000.0
                dmax = int(self.cfg.get("human_delay_max_ms", 1400)) / 1000.0
                delay = random.uniform(dmin, max(dmax, dmin + 0.01))
            else:
                delay = 0.0
            time.sleep(delay)
            self.ui.put(("region", dict(self.detector.region)))
            self.moves({
                "moves": show,
                "flip": self.detector.flip,
                "cell": self.detector.cell,
                "fen": self.board.fen(),
            })
            self.log(f"ходы: " + "  ".join(
                f"{'★' if m.get('star') else '⌗' + str(m['rank'])} "
                f"{m['uci'][:2]}→{m['uci'][2:4]} {m['score']} {m['pct']:.0f}%"
                for m in show) +
                f"  [движок {engine_ms}мс, показ +{int(delay*1000)}мс]")
            self.awaiting_our_move = True
            self.status("ход показан — сходи и жди ответа")
        else:
            # we moved — clear overlay, keep waiting for opponent
            self.board.push(candidate)
            self.prev_state = st
            self.ui.put(("clear", None))
            self.status("твой ход принят, жду противника...")

# ----------------------------------------------------------------------------
# control panel GUI
# ----------------------------------------------------------------------------
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
        self._pos = 2.0  # knob x-offset
        self._knob_r = (height - 6) / 2
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
        # rounded track: rect + 2 circles
        self.create_rectangle(x0 + r, y0, x1 - r, y1, fill=track_color, outline=track_color)
        self.create_oval(x0, y0, x0 + 2 * r, y1, fill=track_color, outline=track_color)
        self.create_oval(x1 - 2 * r, y0, x1, y1, fill=track_color, outline=track_color)
        # knob
        kx = pad + r + self._pos * (self.w - 2 * pad - 2 * r) / (self.w - 2 * pad - 2 * r)
        kx = pad + self._pos
        knob_d = self.h - 6
        self.create_oval(kx, 3, kx + knob_d, 3 + knob_d, fill="#f4f6f8",
                         outline="#d5d9de")
        if self._state:
            self._dot = self.create_oval(kx + knob_d - 9, 3 + knob_d / 2 - 3,
                                         kx + knob_d - 5, 3 + knob_d / 2 + 1,
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
            self._tag_move = self.after(16, lambda: self._animate_step(target, step + 1))

class ControlPanel:
    def __init__(self):
        self.cfg = load_config()
        self.ui_queue = queue.Queue()
        self.worker = None
        self.worker_stop = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.configure(bg=BG)
        self.root.geometry("400x620")
        self.root.minsize(380, 560)
        try:
            self.root.call("tk", "scaling", 1.3)
        except Exception:
            pass

        self.overlay = Overlay(self.root)
        self.calibrator = Calibrator(self.root)
        self._build()
        self._bind_hotkeys()
        self.root.after(60, self._poll)

    # ------------------------------------------------------------------
    def _build(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("TCombobox", fieldbackground=BG2, background=BG2, foreground=FG,
                        arrowcolor=ACCENT)
        style.configure("Horizontal.TScale", background=BG2, troughcolor=BG3)

        # --- header ---------------------------------------------------
        title = tk.Label(self.root, text="♟ ChessTool", fg=ACCENT2, bg=BG,
                         font=("Segoe UI", 18, "bold"))
        title.pack(pady=(14, 2))
        tk.Label(self.root, text="chess.com assistant · elo-capped engine",
                 fg=FGDIM, bg=BG, font=("Segoe UI", 9)).pack(pady=(0, 8))

        def section(text):
            box = tk.Frame(self.root, bg=BG2, highlightbackground="#14161b",
                           highlightthickness=1)
            box.pack(fill=tk.X, padx=16, pady=6)
            tk.Label(box, text=text, fg=ACCENT, bg=BG2,
                     font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10,
                                                        pady=(6, 2))
            return box

        def switch_row(parent, label, initial, command, hint=""):
            row = tk.Frame(parent, bg=BG2)
            row.pack(fill=tk.X, padx=10, pady=(2, 4))
            txt = tk.Label(row, text=label, fg=FG, bg=BG2, font=("Segoe UI", 10))
            txt.pack(side=tk.LEFT)
            sw = Switch(row, command=command, bg=BG2)
            sw.pack(side=tk.RIGHT)
            sw.set(bool(initial))
            if hint:
                tk.Label(parent, text=hint, fg="#6b7480", bg=BG2,
                         font=("Segoe UI", 8)).pack(anchor="w", padx=10)
            return sw

        # --- engine section -------------------------------------------
        sec1 = section("ДВИЖОК")
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
        tk.Label(sec1, text="движок играет на выбранном уровне — 1700 ходит как человек 1700",
                 fg="#6b7480", bg=BG2, font=("Segoe UI", 8)).pack(anchor="w", padx=10)

        self.sw_top3 = switch_row(sec1, "3 варианта хода  (F4)",
                                  self.cfg.get("show_top3", True),
                                  self._on_top3,
                                  "зелёный = лучший, жёлтый и оранжевый = альтернативы")

        self.sw_delay = switch_row(sec1, "Human-delay задержка",
                                   self.cfg.get("human_delay_on", True),
                                   self._on_delay_switch,
                                   "показ хода через случайные 0.3–1.4с — время реакции выглядит живым")
        drow = tk.Frame(sec1, bg=BG2)
        drow.pack(fill=tk.X, padx=10, pady=(2, 6))
        tk.Label(drow, text="мин", fg=FGDIM, bg=BG2, font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self.delay_var = tk.DoubleVar(value=self.cfg.get("human_delay_min_ms", 300) / 1000.0)
        ttk.Scale(drow, from_=0.2, to=3.0, variable=self.delay_var,
                  command=self._on_delay_slide).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        tk.Label(drow, text="макс", fg=FGDIM, bg=BG2, font=("Segoe UI", 8)).pack(side=tk.LEFT)
        self.delay_max_var = tk.DoubleVar(value=self.cfg.get("human_delay_max_ms", 1400) / 1000.0)
        ttk.Scale(drow, from_=0.3, to=5.0, variable=self.delay_max_var,
                  command=self._on_delay_slide).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)

        # --- overlay section -------------------------------------------
        sec2 = section("ОВЕРЛЕЙ")
        self.sw_overlay = switch_row(sec2, "Показывать оверлей  (F3)",
                                     True, self._on_overlay_switch,
                                     "подсветка клеток поверх доски; невидим для OBS и скриншотов")
        self.sw_highlight_arrow = switch_row(sec2, "Стрелки хода", True,
                                             self._on_arrow_switch,
                                             "линия от клетки к клетке + бейдж ранга")

        # --- board section ---------------------------------------------
        sec3 = section("ДОСКА")
        self.sw_autodetect = switch_row(sec3, "Авто-детект доски",
                                        self.cfg.get("autodetect", True),
                                        self._on_autodetect_switch,
                                        "ищет доску chess.com сам; выключи — только ручная калибровка")
        brow = tk.Frame(sec3, bg=BG2)
        brow.pack(fill=tk.X, padx=10, pady=(2, 8))
        tk.Button(brow, text="⬚  Калибровать доску  (F2)", command=self._calibrate,
                  bg=BG3, fg=FG, activebackground="#353b48", activeforeground=FG,
                  relief=tk.FLAT, font=("Segoe UI", 9), pady=5, cursor="hand2").pack(
            fill=tk.X)

        # --- main button + status --------------------------------------
        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill=tk.X, padx=16, pady=(8, 4))
        self.start_btn = tk.Button(btns, text="▶  Начать анализ  (F1)", command=self._toggle,
                                   bg=ACCENT, fg="#101608", activebackground=ACCENT2,
                                   activeforeground="#101608", relief=tk.FLAT,
                                   font=("Segoe UI", 11, "bold"), pady=8, cursor="hand2")
        self.start_btn.pack(fill=tk.X)

        self.status_var = tk.StringVar(value="● стоп — нажми F1 или кнопку")
        self.status_lbl = tk.Label(self.root, textvariable=self.status_var, fg=FGDIM,
                                   bg=BG2, font=("Segoe UI", 9, "bold"), pady=5)
        self.status_lbl.pack(fill=tk.X, padx=16, pady=(4, 0))

        tk.Label(self.root, text="лог", fg="#707a86", bg=BG,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=18, pady=(8, 2))
        self.log = scrolledtext.ScrolledText(self.root, bg="#14171c", fg="#b9c2cc",
                                             font=("Consolas", 9), relief=tk.FLAT,
                                             state=tk.DISABLED, height=9)
        self.log.pack(fill=tk.BOTH, expand=True, padx=16, pady=(0, 4))
        foot = tk.Label(self.root, text="F1 старт/стоп · F2 калибровка · F3 скрыть оверлей · F4 1/3 хода",
                        fg="#5a6370", bg=BG, font=("Segoe UI", 8))
        foot.pack(pady=(2, 8))

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
            kb.add_hotkey("F4", safe(lambda: self._flip_top3()))
            kb.add_hotkey("F5", safe(self._dump_debug))
        except Exception:
            pass  # keyboard lib may need admin; GUI buttons still work

    def _dump_debug(self):
        if self.worker and self.worker.is_alive():
            self.worker.dump_debug()
        else:
            self._log("запусти анализ (F1) — потом F5 сохранит дамп")

    def _flip_top3(self):
        self.sw_top3.toggle()

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
        self.overlay.show_arrows = bool(self.sw_highlight_arrow.state)

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
            self._log(f"эло → {elo} (skill {prof['skill']}, movetime {prof['movetime']}мс)")

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
        self.overlay.show_arrows = bool(self.sw_highlight_arrow.state)
        self.worker = Worker(self.cfg, self.ui_queue)
        self.worker.start()
        self.start_btn.config(text="■  Остановить (F1)", bg="#c0453b",
                              activebackground="#d95b50")
        self.status_var.set("● запускаюсь...")

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
        self.start_btn.config(text="▶  Начать анализ (F1)", bg=ACCENT,
                              activebackground=ACCENT2)
        self.status_var.set("● стоп")
        self._log("анализ остановлен")

    def _calibrate(self):
        box = self.calibrator.select()
        if not box:
            return
        if self.worker and self.worker.is_alive():
            self.worker.detector.set_region(*box)
            self.worker.our_color = chess.BLACK if self.worker.detector.flip else chess.WHITE
            self.worker.full_rescan_needed = True
            self.overlay.set_region(self.worker.detector.region)
            self.cfg["region"] = dict(self.worker.detector.region)
            save_config(self.cfg)
            self._log(f"доска перекалибрована: {box[2]}x{box[3]} @ ({box[0]},{box[1]}) — "
                      f"{'чёрные' if self.worker.detector.flip else 'белые'}")
        else:
            d = BoardDetector()
            d.set_region(*box)
            self.overlay.set_region(d.region)
            self.cfg["region"] = d.region
            save_config(self.cfg)
            self._log(f"доска запомнена: {box[2]}x{box[3]} @ ({box[0]},{box[1]}) — "
                      f"{'чёрные' if d.flip else 'белые'} (при запуске подхватится)")

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
                    self.overlay.show_moves(data["moves"], data["flip"], data["cell"])
                    fen = data.get("fen", "")
                    if fen:
                        self._log(f"fen: {fen.split(' ')[0]}")
                elif kind == "clear":
                    self.overlay.clear()
        except queue.Empty:
            pass
        self.root.after(60, self._poll)

    def run(self):
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        try:
            self._stop()
        finally:
            self.root.destroy()

# ----------------------------------------------------------------------------
def main():
    # DPI awareness: tk coordinates must match physical screen pixels (mss),
    # otherwise calibration clicks and overlay positions are shifted by scale
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    if not os.path.exists(engine_path()):
        print("stockfish.exe not found in engines/ — see README")
        return
    app = ControlPanel()
    app.run()

if __name__ == "__main__":
    main()
