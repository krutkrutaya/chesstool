# -*- coding: utf-8 -*-
"""Stream-proof overlay: transparent topmost click-through canvas."""
import math
import tkinter as tk
from .win32 import make_clickthrough_and_hidden


class Overlay:
    TRANSPARENT_KEY = "#ff00fe"
    COLORS = ["#39d353", "#f7c948", "#ff8c42"]  # rank 1..3

    def __init__(self, owner: tk.Tk):
        self.owner = owner
        self.root = tk.Toplevel(owner)
        self.root.title("ChessTool overlay")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", self.TRANSPARENT_KEY)
        self.root.config(bg=self.TRANSPARENT_KEY)
        self.canvas = tk.Canvas(self.root, bg=self.TRANSPARENT_KEY,
                                highlightthickness=0)
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
        self.root.attributes("-topmost", True)
        # stay always visible but fully transparent when empty:
        # withdraw/deiconify on overrideredirect windows is unreliable on tk
        self.root.deiconify()
        self.root.update_idletasks()
        make_clickthrough_and_hidden(self.root.winfo_id())

    def set_region(self, region: dict):
        self._region = region

    def show_moves(self, moves, flip: bool, cell: int):
        """moves: list of dicts {uci, score, rank, pct, star}"""
        if self._region is None or not moves:
            return
        self._moves = (list(moves), flip, cell)
        if self.hidden:
            return
        self.root.attributes("-topmost", True)
        self.root.lift()
        self.root.update_idletasks()
        self._redraw()

    def _sq_rect(self, sq: str, flip: bool, cell: int):
        file_idx = ord(sq[0]) - ord('a')
        rank_idx = int(sq[1]) - 1
        if not flip:
            col, row = file_idx, 7 - rank_idx
        else:
            col, row = 7 - file_idx, rank_idx
        r = self._region
        cw = r["width"] / 8.0
        chh = r["height"] / 8.0
        x = r["left"] + col * cw
        y = r["top"] + row * chh
        return x, y, cw, chh

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
            x1, y1, cw1, ch1 = self._sq_rect(frm, flip, cell)
            x2, y2, cw2, ch2 = self._sq_rect(to, flip, cell)
            c = min(cw1, ch1)
            pad = max(2, c // 16)
            w = max(3, c // 14)
            # from-square: thick outline
            self.canvas.create_rectangle(x1 + pad, y1 + pad,
                                         x1 + cw1 - pad, y1 + ch1 - pad,
                                         outline=color, width=w)
            # to-square: outline + hatching
            self.canvas.create_rectangle(x2 + pad, y2 + pad,
                                         x2 + cw2 - pad, y2 + ch2 - pad,
                                         outline=color, width=w)
            step = max(6, int(c // 7))
            for d in range(-int(c), int(c), step):
                self.canvas.create_line(x2 + max(0, d), y2,
                                        x2 + d + cw2, y2 + ch2,
                                        fill=color, width=max(1, w // 3))
            # arrow
            if self.show_arrows:
                cx1, cy1 = x1 + cw1 / 2, y1 + ch1 / 2
                cx2, cy2 = x2 + cw2 / 2, y2 + ch2 / 2
                dx, dy = cx2 - cx1, cy2 - cy1
                L = math.hypot(dx, dy) or 1.0
                ux, uy = dx / L, dy / L
                trim = min(cw2, ch2) / 2.4
                ex, ey = cx2 - ux * trim, cy2 - uy * trim
                self.canvas.create_line(cx1 + ux * (c / 3.4),
                                        cy1 + uy * (c / 3.4), ex, ey,
                                        fill=color, width=w + 1)
                ah = max(8, c // 3)
                self.canvas.create_polygon(
                    ex + ux * ah, ey + uy * ah,
                    ex - uy * ah * 0.55 - ux * ah * 0.35,
                    ey + ux * ah * 0.55 - uy * ah * 0.35,
                    ex + uy * ah * 0.55 - ux * ah * 0.35,
                    ey - ux * ah * 0.55 - uy * ah * 0.35,
                    fill=color, outline=color)
            # rank badge
            bx = x1 + cw1 - pad * 2
            by = y1 + pad * 2
            rr = max(7, c // 6)
            self.canvas.create_oval(bx - rr, by - rr, bx + rr, by + rr,
                                    fill=color, outline="#101010", width=1)
            badge = ("★" if m.get("star") else str(rank))
            self.canvas.create_text(bx, by, text=badge, fill="#0a0a0a",
                                    font=("Segoe UI", max(8, int(c // 7)), "bold"))
            # score + pct label
            label = f"{to}  {m.get('score', '')}  {m.get('pct', 0):.0f}%"
            fs = max(9, int(c // 6))
            tx = x2 + cw2 / 2
            ty = y2 - pad * 2
            self.canvas.create_text(tx + 1, ty + 1, text=" " + label + " ",
                                    fill="#0d0d0d", font=("Segoe UI", fs, "bold"),
                                    anchor=tk.S)
            self.canvas.create_text(tx, ty, text=" " + label + " ",
                                    fill="#ffffff", font=("Segoe UI", fs, "bold"),
                                    anchor=tk.S)

    def clear(self):
        self._moves = None
        self.canvas.delete("all")

    def toggle_hidden(self) -> bool:
        self.hidden = not self.hidden
        if self.hidden:
            self.root.withdraw()
        else:
            self.root.deiconify()
            self.root.geometry(f"{self.root.winfo_screenwidth()}x"
                               f"{self.root.winfo_screenheight()}+0+0")
            make_clickthrough_and_hidden(self.root.winfo_id())
            self._redraw()
        return self.hidden


class Calibrator:
    """Fullscreen 2-click region selector."""
    def __init__(self, root_owner: tk.Tk):
        self.owner = root_owner

    def select(self):
        result = {"box": None}
        win = tk.Toplevel(self.owner)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.30)
        win.config(bg="#101418", cursor="crosshair")
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"{sw}x{sh}+0+0")
        lbl = tk.Label(win,
                       text="Click two OPPOSITE CORNERS of the board "
                            "(outer edges of the squares)\nEsc — cancel",
                       fg="#ffffff", bg="#1c1f26",
                       font=("Segoe UI", 14, "bold"), pady=10)
        lbl.pack(side=tk.TOP, fill=tk.X)
        cv = tk.Canvas(win, bg="#101418", highlightthickness=0)
        cv.pack(fill=tk.BOTH, expand=True)
        state = {"p1": None}

        def on_click(e):
            if state["p1"] is None:
                state["p1"] = (e.x_root, e.y_root)
            else:
                x1, y1 = state["p1"]
                x2, y2 = e.x_root, e.y_root
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

        cv.bind("<Button-1>", on_click)
        cv.bind("<Motion>", on_move)
        win.bind("<Escape>", lambda e: win.destroy())
        win.focus_force()
        self.owner.wait_window(win)
        return result["box"]
