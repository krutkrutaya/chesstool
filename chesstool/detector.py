# -*- coding: utf-8 -*-
"""Board detection on screen, piece recognition, grid refinement."""
import os, math
import numpy as np
from PIL import Image
import mss
import chess

from .config import templates_dir
from .win32 import user32

# chess.com standard themes (light square, dark square)
THEMES = [
    ((238, 238, 210), (118, 153,  86)),   # green
    ((240, 217, 181), (181, 136,  99)),   # brown
    ((227, 237, 249), (116, 127, 146)),   # blue
    ((238, 238, 238), (118, 118, 118)),   # grey / newspaper
    ((233, 231, 200), (120, 128,  89)),   # muted green
    ((247, 245, 209), ( 89, 122,  71)),   # walnut
    ((208, 236, 235), ( 82, 149, 115)),   # mint/teal
    ((222, 227, 230), ( 47,  61,  85)),   # dark navy
    ((235, 236, 209), (110, 139,  87)),   # green v2
    ((236, 236, 208), (114, 149,  81)),   # user-reported green (chess.com)
]
TOL = 26
USER_THEMES = []  # runtime-measured pairs, appended on calibration


def all_themes():
    return THEMES + [tuple(tuple(c) for c in t) for t in USER_THEMES]


def _rgb_close(px, ref, tol=TOL):
    return (abs(int(px[0]) - ref[0]) <= tol and
            abs(int(px[1]) - ref[1]) <= tol and
            abs(int(px[2]) - ref[2]) <= tol)


def sq_name(col: int, row: int, flip: bool) -> str:
    """row 0 = top of the captured region."""
    if not flip:
        return chr(ord('a') + col) + str(8 - row)
    return chr(ord('h') - col) + str(row + 1)


class BoardState:
    """Incremental square cache for fast re-recognition."""
    __slots__ = ("squares", "sign", "ok")

    def __init__(self):
        self.squares = {}      # (col, row) -> piece char or None
        self.sign = {}         # (col, row) -> (mean, std) fingerprint
        self.ok = False

    def by_name(self, flip: bool) -> dict:
        return {sq_name(c, r, flip): pc for (c, r), pc in self.squares.items()
                if pc}


class BoardDetector:
    def __init__(self):
        self.region = None     # dict left, top, width, height
        self.flip = False
        self.cw = 0.0          # fractional cell width
        self.ch = 0.0          # fractional cell height
        self.cell = 0          # int cell for callers
        self.templates = {}    # char -> PIL Image
        self._tpl_cache = {}   # (char, size) -> (rgb array, mask)
        self._load_templates()

    # ------------------------------------------------------------------
    def _load_templates(self):
        names = {"P": "wp", "N": "wn", "B": "wb", "R": "wr", "Q": "wq", "K": "wk",
                 "p": "bp", "n": "bn", "b": "bb", "r": "br", "q": "bq", "k": "bk"}
        for ch, fn in names.items():
            p = os.path.join(templates_dir(), fn + ".png")
            if os.path.exists(p):
                self.templates[ch] = Image.open(p).convert("RGBA")

    def _tpl(self, ch: str, size: int):
        key = (ch, size)
        if key not in self._tpl_cache:
            img = self.templates[ch].resize((size, size), Image.LANCZOS)
            arr = np.asarray(img, dtype=np.float32)
            rgb = arr[:, :, :3]
            alpha = arr[:, :, 3] / 255.0
            mask = (alpha > 0.55).astype(np.float32)
            # erode mask ~2px to drop anti-aliased edges
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
                if er.sum() > size * size * 0.08:
                    mask = er.astype(np.float32)
            self._tpl_cache[key] = (rgb, mask)
        return self._tpl_cache[key]

    # ------------------------------------------------------------------
    def auto_detect(self) -> bool:
        """Scan all monitors for an 8x8 light/dark grid."""
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
        small = img[::2, ::2]
        sm_h, sm_w = small.shape[:2]
        board_mask = self._board_mask(small)
        if board_mask.sum() < 400:
            return None
        win = max(20, min(sm_h, sm_w) // 12)
        row_sum = board_mask.sum(axis=1)
        row_win = np.convolve(row_sum, np.ones(win, dtype=np.int64), mode="same")
        y_peak = int(np.argmax(row_win))
        ys = np.where(row_win >= row_win[y_peak] * 0.35)[0]
        if len(ys) == 0:
            return None
        y0, y1 = int(ys[0]), int(ys[-1])
        band = board_mask[y0:y1 + 1]
        col_band = band.sum(axis=0)
        col_win = np.convolve(col_band, np.ones(win, dtype=np.int64), mode="same")
        x_peak = int(np.argmax(col_win))
        xs = np.where(col_win >= col_win[x_peak] * 0.35)[0]
        if len(xs) == 0:
            return None
        x0, x1 = int(xs[0]), int(xs[-1])
        bw = (x1 - x0) * 2
        bh = (y1 - y0) * 2
        if bw < 300 or bh < 300:
            return None
        if abs(bw - bh) > max(bw, bh) * 0.18:
            return None
        inner = board_mask[y0:y1 + 1, x0:x1 + 1]
        if inner.size == 0 or inner.mean() < 0.55:
            return None
        side = (min(bw, bh) // 8) * 8
        # checkerboard verification on a coarse grid
        side_s = side // 2
        cs = side_s // 8
        if cs < 4:
            return None
        ok_cells = 0
        total = 0
        for r in range(8):
            for c in range(8):
                y = y0 + r * cs + cs // 2
                x = x0 + c * cs + cs // 2
                if y >= sm_h or x >= sm_w:
                    continue
                total += 1
                expect_light = (r + c) % 2 == 0
                px = small[y, x]
                for lt, dk in all_themes():
                    if _rgb_close(px, lt if expect_light else dk):
                        ok_cells += 1
                        break
        if total == 0 or ok_cells / total < 0.60:
            return None
        return (x0 * 2, y0 * 2, side, side)

    # ------------------------------------------------------------------
    def set_region(self, left, top, width, height) -> bool:
        self.region = {"left": int(left), "top": int(top),
                       "width": int(width), "height": int(height)}
        self._post_region()
        return True

    def _board_mask(self, img: np.ndarray) -> np.ndarray:
        sm = img.astype(np.int16)
        m = np.zeros(img.shape[:2], dtype=bool)
        for lt, dk in all_themes():
            m |= (np.abs(sm - np.int16(lt)).max(axis=2) <= TOL)
            m |= (np.abs(sm - np.int16(dk)).max(axis=2) <= TOL)
        return m

    def measure_theme(self, img: np.ndarray) -> tuple | None:
        """Measure the actual board colors from empty corner-ish cells.
        Returns (light, dark) or None. Appends to USER_THEMES on success."""
        m = self._board_mask(img)
        if m.mean() < 0.3:
            return None
        h, w = img.shape[:2]
        cw = w / 8.0
        ch = h / 8.0
        # sample cell centres where the mask is board-colored (empty cells)
        lights, darks = [], []
        for r in range(8):
            for c in range(8):
                y = int((r + 0.5) * ch)
                x = int((c + 0.5) * cw)
                if not m[y, x]:
                    continue
                expect_light = (r + c) % 2 == 0
                patch = img[y - 3:y + 3, x - 3:x + 3]
                if patch.size == 0:
                    continue
                mean = patch.reshape(-1, 3).mean(axis=0)
                if expect_light:
                    lights.append(mean)
                else:
                    darks.append(mean)
        if len(lights) < 6 or len(darks) < 6:
            return None
        light = tuple(int(v) for v in np.median(np.array(lights), axis=0))
        dark = tuple(int(v) for v in np.median(np.array(darks), axis=0))
        # only register if not already covered by tolerance
        covered = any(_rgb_close(light, lt, 14) and _rgb_close(dark, dk, 14)
                      for lt, dk in all_themes())
        if not covered:
            USER_THEMES.append((light, dark))
        return light, dark

    # ------------------------------------------------------------------
    def _post_region(self):
        r = self.region
        orig = dict(r)
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
        # measure real board colors first (helps refine on unknown themes)
        self.measure_theme(img)
        rf = self._refine_grid(img)
        if rf is not None:
            gx, gy, gside = rf
            r["left"] += gx
            r["top"] += gy
            r["width"] = gside
            r["height"] = gside
        else:
            dx, dy, sw, sh = self._snap_to_checker(img)
            side = int(round((sw + sh) / 2.0))
            cx = dx + sw / 2.0
            cy = dy + sh / 2.0
            left = int(round(cx - side / 2.0))
            top = int(round(cy - side / 2.0))
            left = max(0, min(left, img.shape[1] - side))
            top = max(0, min(top, img.shape[0] - side))
            r["left"] += left
            r["top"] += top
            r["width"] = side
            r["height"] = side
        self.cw = r["width"] / 8.0
        self.ch = r["height"] / 8.0
        self.cell = int(round(r["width"] / 8.0))

    def _refine_grid(self, img: np.ndarray):
        """Subpixel board alignment from light<->dark transitions.
        Returns (left, top, side) inside img, or None."""
        sm = img.astype(np.int16)
        light = np.zeros(img.shape[:2], dtype=bool)
        dark = np.zeros(img.shape[:2], dtype=bool)
        for lt, dk in all_themes():
            light |= (np.abs(sm - np.int16(lt)).max(axis=2) <= TOL)
            dark |= (np.abs(sm - np.int16(dk)).max(axis=2) <= TOL)
        h, w = img.shape[:2]
        est_c = w / 8.0

        def transitions_line(is_row: bool, idx: int):
            if is_row:
                a, b = light[idx], dark[idx]
            else:
                a, b = light[:, idx], dark[:, idx]
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
            for frac in (0.22, 0.28, 0.34, 0.46, 0.52, 0.58,
                         0.64, 0.72, 0.78, 0.84):
                idx = int(frac * (h if is_row else w))
                idx = min(idx, (h if is_row else w) - 1)
                xs = transitions_line(is_row, idx)
                if len(xs) == 7:
                    diffs = [xs[i + 1] - xs[i] for i in range(6)]
                    if all(0.7 * est_c <= d <= 1.3 * est_c for d in diffs):
                        spans.append(xs)
            if not spans:
                return None
            first = float(np.median([s[0] for s in spans]))
            last = float(np.median([s[-1] for s in spans]))
            cw = (last - first) / 6.0
            return first - cw, 8.0 * cw

        fx = fit_axis(True)
        fy = fit_axis(False)
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
        """Trim outer frame/rails around the checkerboard."""
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
        left = max(0, left - strip)
        top = max(0, top - strip)
        right = min(w, right + strip)
        bottom = min(h, bottom + strip)
        if right - left < 300 or bottom - top < 300:
            return 0, 0, w, h
        return left, top, right - left, bottom - top

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
        pad = max(3, c // 10)
        ps = max(3, c // 14)
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
                expect_light = (r + cc) % 2 == 0
                for (yy, xx) in corners:
                    if (yy + ps <= img.shape[0] and xx + ps <= img.shape[1]
                            and yy >= 0 and xx >= 0):
                        patch = img[yy:yy + ps, xx:xx + ps].reshape(-1, 3).mean(axis=0)
                        for lt, dk in all_themes():
                            if _rgb_close(patch, lt if expect_light else dk):
                                cell_ok = True
                                break
                    if cell_ok:
                        break
                total += 1
                if cell_ok:
                    hits += 1
        return hits >= 6

    # ------------------------------------------------------------------
    def determine_flip(self, st: BoardState) -> bool:
        """True = we play black (white pieces are at the TOP)."""
        wp_rows = [row for (col, row), pc in st.squares.items() if pc == "P"]
        if wp_rows:
            bottom = sum(1 for r in wp_rows if r >= 4)
            return bottom < len(wp_rows) / 2.0
        wk = [row for (col, row), pc in st.squares.items() if pc == "K"]
        if wk:
            return sum(wk) / len(wk) < 4.0
        return False

    def _cell_bg(self, cell_img: np.ndarray):
        """Modal color of the cell = square background."""
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
        dmax = np.abs(f[:, :, :3] - bg).max(axis=2)
        frac = float((dmax > 22).mean())
        if frac < 0.10:
            return None, None
        piece_px = dmax > 22
        if piece_px.sum() < 4:
            return None, None
        brightness = f[:, :, :3][piece_px].mean()
        if brightness >= 125.0:
            candidates = "PNBRQK"
        elif brightness <= 110.0:
            candidates = "pnbrqk"
        else:
            candidates = "PNBRQKpnbrqk"
        best_ch, best_mse, second = None, 1e9, 1e9
        for ch in self.templates:
            if ch not in candidates:
                continue
            tpl, mask = self._tpl(ch, size)
            if mask.sum() < 1:
                continue
            diff = (f[:, :, :3] - tpl) * mask[:, :, None]
            mse = float((diff * diff).sum() / (mask.sum() * 3.0))
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
        return "?", best_ch

    def recognize(self, img: np.ndarray, prev: BoardState, full: bool = False):
        """Returns (new BoardState, set_of_changed (col,row))."""
        st = BoardState()
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
                    if (old is not None and abs(fp[0] - old[0]) < 4.0
                            and abs(fp[1] - old[1]) < 5.0):
                        st.squares[(col, row)] = prev.squares.get((col, row))
                        st.sign[(col, row)] = old
                        continue
                changed.add((col, row))
                bg = self._cell_bg(cell)
                if (cw, chh) != (tsize, tsize):
                    ci = Image.fromarray(cell).resize((tsize, tsize),
                                                      Image.BILINEAR)
                    cell = np.asarray(ci, dtype=np.uint8)
                piece, guess = self._classify_cell(cell, bg, tsize)
                if piece == "?" and guess is not None:
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
                return None
            pieces[sq_name(col, row, self.flip)] = pc
        placement = []
        for rank in range(8, 0, -1):
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
