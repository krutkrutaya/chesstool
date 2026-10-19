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
        """Returns list of (move, score_string, rank) — up to 3 lines."""
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
                            sc = int(sc_part[1])
                            score = f"{sc/100:+.2f}"
                        elif sc_part[0] == "mate":
                            score = f"M{sc_part[1]}"
                        else:
                            score = "?"
                        pvmove = line.split(" pv ")[1].split()[0]
                        lines[multipv] = (pvmove, score)
                elif line.startswith("bestmove"):
                    best = line.split()[1] if len(line.split()) > 1 else None
                    break
            result = []
            for k in sorted(lines.keys())[:3]:
                mv, sc = lines[k]
                if mv != "(none)":
                    result.append((mv, sc, k))
            if not result and best and best != "(none)":
                result.append((best, "?", 1))
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
            mask = (alpha > 0.35).astype(np.float32)
            # normalize template brightness away — match shape, not shade
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
        self.cell = min(r["width"], r["height"]) // 8
        # NOTE: board orientation (flip) is NOT determined by corner color —
        # a1 and h8 are both dark. Flip is detected from piece placement
        # (white pawns) after the first full scan.

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
        for r in range(0, 8, 2):
            for cc in range(0, 8, 2):
                y = r * c + c // 2
                x = cc * c + c // 2
                if y >= img.shape[0] or x >= img.shape[1]:
                    continue
                px = img[y, x]
                for lt, dk in THEMES:
                    if _rgb_close(px, lt) or _rgb_close(px, dk):
                        hits += 1
                        break
        return hits >= 10  # 16 sampled, allow a few covered by pieces

    # ------------------------------------------------------------------
    def _cell_bg(self, cell_img: np.ndarray):
        h, w = cell_img.shape[:2]
        pad = max(3, h // 12)
        patches = np.concatenate([
            cell_img[:pad, :pad].reshape(-1, 3),
            cell_img[:pad, -pad:].reshape(-1, 3),
            cell_img[-pad:, :pad].reshape(-1, 3),
            cell_img[-pad:, -pad:].reshape(-1, 3),
        ])
        return np.median(patches, axis=0)

    def _fingerprint(self, cell_img: np.ndarray):
        g = cell_img.astype(np.float32)
        return float(g.mean()), float(g.std())

    def _classify_cell(self, cell_img: np.ndarray, bg: np.ndarray, size: int):
        """returns piece char, None (empty) or '?' (uncertain)"""
        f = cell_img.astype(np.float32)
        # empty check: central region close to background color
        m = size // 6
        center = f[m:size - m, m:size - m]
        d = np.abs(center - bg)
        if d.mean() < 14.0 and d.std() < 12.0:
            return None
        # template match
        best_ch, best_mse = None, 1e9
        second = 1e9
        for ch in self.templates:
            tpl, mask = self._tpl(ch, size)
            diff = (f[:, :, :3] - tpl) * mask[:, :, None]
            mse = float((diff * diff).sum() / (mask.sum() * 3.0))
            if mse < best_mse:
                second = best_mse
                best_ch, best_mse = ch, mse
            elif mse < second:
                second = mse
        if best_mse < 650.0:
            return best_ch
        if best_mse < 1500.0 and second - best_mse > 40.0:
            return best_ch
        return "?"

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
        c = self.cell
        # square size actually used for template matching
        tsize = c if c <= 96 else 96
        changed = set()
        for row in range(8):
            for col in range(8):
                y0, x0 = row * c, col * c
                cell = img[y0:y0 + c, x0:x0 + c]
                if cell.size == 0:
                    continue
                if not full:
                    m = c // 8
                    core = cell[m:c - m, m:c - m]
                    fp = self._fingerprint(core)
                    old = prev.sign.get((col, row))
                    if old is not None and abs(fp[0] - old[0]) < 4.0 and abs(fp[1] - old[1]) < 5.0:
                        st.squares[(col, row)] = prev.squares.get((col, row))
                        st.sign[(col, row)] = old
                        continue
                changed.add((col, row))
                bg = self._cell_bg(cell)
                if c != tsize:
                    ci = Image.fromarray(cell).resize((tsize, tsize), Image.BILINEAR)
                    cell = np.asarray(ci, dtype=np.uint8)
                piece = self._classify_cell(cell, bg, tsize)
                st.squares[(col, row)] = piece
                m = c // 8
                core = img[y0 + m:y0 + c - m, x0 + m:x0 + c - m]
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

    def set_region(self, region: dict):
        self._region = region

    def show_moves(self, moves, flip: bool, cell: int):
        """moves: list of (uci_str, score, rank) with rank 1..3"""
        if self._region is None or not moves:
            return
        self._moves = (list(moves), flip, cell)
        if self.hidden:
            return
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
        for uci, score, rank in moves:
            if len(uci) < 4:
                continue
            frm, to = uci[:2], uci[2:4]
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
            self.canvas.create_line(x2 + max(0, -d), y2 + c, x2 + c + min(0, d), y2,
                                    fill=color, width=max(1, w // 3))
            # arrow center-to-center
            cx1, cy1 = x1 + c // 2, y1 + c // 2
            cx2, cy2 = x2 + c // 2, y2 + c // 2
            dx, dy = cx2 - cx1, cy2 - cy1
            L = math.hypot(dx, dy) or 1.0
            ux, uy = dx / L, dy / L
            # trim arrow to not cover the badge too much
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
            self.canvas.create_text(bx, by, text=str(rank), fill="#0a0a0a",
                                    font=("Segoe UI", max(8, c // 7), "bold"))

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
        if not self.hidden:
            self._redraw()

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
            if {sq: pc for sq, pc in board.piece_map().items()} == piece_map_new:
                candidate = mv
                board.pop()
                break
            board.pop()

        if candidate is None:
            # maybe two moves happened quickly (ours + theirs) or glitch — rescan
            self.full_rescan_needed = True
            return

        mover_is_opponent = (board.turn != self.our_color)
        self.board.push(candidate)
        self.prev_state = st

        if mover_is_opponent:
            # recompute san on the pre-move board
            pre = self.board.copy(stack=False)
            pre.pop()
            try:
                san = pre.san(candidate)
            except Exception:
                san = candidate.uci()
            self.log(f"противник: {san}")
            self.status("думаю...")
            t0 = time.time()
            lines = self.engine.analyse(self.board,
                                        profile_for(int(self.cfg.get("elo", 1700)))["movetime"])
            engine_ms = int((time.time() - t0) * 1000)
            if not lines:
                self.status("движок не дал ход")
                return
            # low-elo: randomized pick with weights
            prof = profile_for(int(self.cfg.get("elo", 1700)))
            picks = lines[:]
            if prof["weights"] and len(picks) > 1:
                ws = prof["weights"][:len(picks)]
                s = sum(ws)
                r = random.random() * s
                acc = 0
                chosen = picks[0]
                for i, w in enumerate(ws):
                    acc += w
                    if r <= acc:
                        chosen = picks[i]
                        break
                # reorder so the displayed ranks stay by quality, mark chosen first
                picks = sorted(picks, key=lambda t: (t is not chosen, t[2]))
                picks = [((t[0], t[1] + (" ★" if t is chosen else ""), t[2])) for t in picks]
            show = picks[:3 if self.cfg.get("show_top3", True) else 1]
            # human delay — keeps reaction time statistically human
            dmin = int(self.cfg.get("human_delay_min_ms", 300)) / 1000.0
            dmax = int(self.cfg.get("human_delay_max_ms", 1400)) / 1000.0
            delay = random.uniform(dmin, max(dmax, dmin + 0.01))
            time.sleep(delay)
            self.ui.put(("region", dict(self.detector.region)))
            self.moves({
                "moves": show,
                "flip": self.detector.flip,
                "cell": self.detector.cell,
                "fen": self.board.fen(),
            })
            self.log(f"ходы: " + "  ".join(
                f"{'⌗' + str(m[2])} {m[0]} ({m[1]})" for m in show) +
                f"  [движок {engine_ms}мс, показ +{int(delay*1000)}мс]")
            self.awaiting_our_move = True
            self.status("ход показан — сходи и жди ответа")
        else:
            # we moved — clear overlay, keep waiting for opponent
            self.ui.put(("clear", None))
            self.status("твой ход принят, жду противника...")

# ----------------------------------------------------------------------------
# control panel GUI
# ----------------------------------------------------------------------------
BG      = "#1c1f26"
BG2     = "#22262f"
FG      = "#e6e6e6"
ACCENT  = "#81b64c"
ACCENT2 = "#39d353"

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
        style.configure("Horizontal.TScale", background=BG2, troughcolor=BG)

        title = tk.Label(self.root, text="♟ ChessTool", fg=ACCENT2, bg=BG,
                         font=("Segoe UI", 18, "bold"))
        title.pack(pady=(14, 4))
        sub = tk.Label(self.root, text="chess.com assistant · elo-capped engine",
                       fg="#9aa3af", bg=BG, font=("Segoe UI", 9))
        sub.pack(pady=(0, 10))

        frm = tk.Frame(self.root, bg=BG)
        frm.pack(fill=tk.X, padx=16)

        tk.Label(frm, text="Elo движка:", fg=FG, bg=BG,
                 font=("Segoe UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        self.elo_var = tk.StringVar(value=str(self.cfg.get("elo", 1700)))
        cb = ttk.Combobox(frm, textvariable=self.elo_var, values=[str(e) for e in ELO_LEVELS],
                          state="readonly", width=10, font=("Segoe UI", 11))
        cb.grid(row=0, column=1, sticky="e", pady=4)
        cb.bind("<<ComboboxSelected>>", self._on_elo)

        self.top3_var = tk.BooleanVar(value=bool(self.cfg.get("show_top3", True)))
        tk.Checkbutton(frm, text="показывать 3 варианта (F4 — переключить)",
                       variable=self.top3_var, command=self._on_top3,
                       fg=FG, bg=BG, selectcolor=BG2, activebackground=BG,
                       activeforeground=FG, font=("Segoe UI", 9)).grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

        tk.Label(frm, text="Задержка показа, сек", fg=FG, bg=BG,
                 font=("Segoe UI", 9)).grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.delay_var = tk.DoubleVar(value=self.cfg.get("human_delay_min_ms", 300) / 1000.0)
        self.delay_max_var = tk.DoubleVar(value=self.cfg.get("human_delay_max_ms", 1400) / 1000.0)
        s1 = ttk.Scale(frm, from_=0.2, to=3.0, variable=self.delay_var)
        s1.grid(row=3, column=0, sticky="ew", pady=(0, 2))
        s2 = ttk.Scale(frm, from_=0.3, to=5.0, variable=self.delay_max_var)
        s2.grid(row=4, column=0, sticky="ew")

        btns = tk.Frame(self.root, bg=BG)
        btns.pack(fill=tk.X, padx=16, pady=12)
        self.start_btn = tk.Button(btns, text="▶  Начать анализ (F1)", command=self._toggle,
                                   bg=ACCENT, fg="#101608", activebackground=ACCENT2,
                                   activeforeground="#101608", relief=tk.FLAT,
                                   font=("Segoe UI", 11, "bold"), pady=8, cursor="hand2")
        self.start_btn.pack(fill=tk.X)
        tk.Button(btns, text="⬚  Калибровать доску (F2)", command=self._calibrate,
                  bg=BG2, fg=FG, activebackground="#2d323d", activeforeground=FG,
                  relief=tk.FLAT, font=("Segoe UI", 10), pady=6, cursor="hand2").pack(
            fill=tk.X, pady=(6, 0))

        self.status_var = tk.StringVar(value="● стоп — нажми F1")
        self.status_lbl = tk.Label(self.root, textvariable=self.status_var, fg="#9aa3af",
                                   bg=BG2, font=("Segoe UI", 9, "bold"), pady=5)
        self.status_lbl.pack(fill=tk.X, padx=16)

        tk.Label(self.root, text="лог", fg="#707a86", bg=BG,
                 font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=18, pady=(8, 2))
        self.log = scrolledtext.ScrolledText(self.root, bg="#14171c", fg="#b9c2cc",
                                             font=("Consolas", 9), relief=tk.FLAT,
                                             state=tk.DISABLED, height=14)
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
        except Exception:
            pass  # keyboard lib may need admin; GUI buttons still work

    def _flip_top3(self):
        self.top3_var.set(not self.top3_var.get())
        self._on_top3()

        frm = tk.Frame(self.root, bg=BG)
        frm.pack(fill=tk.X, padx=16)

    def _on_top3(self):
        self.cfg["show_top3"] = bool(self.top3_var.get())
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
        save_config(self.cfg)
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
    if not os.path.exists(engine_path()):
        print("stockfish.exe not found in engines/ — see README")
        return
    app = ControlPanel()
    app.run()

if __name__ == "__main__":
    main()
