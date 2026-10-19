# -*- coding: utf-8 -*-
"""Test region auto-grow + square fit on a synthetic screen that mimics
the real chess.com layout: dark panel above the board, user region clipped."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image
import main as M
import chess
from test_units import _make_board_image

def run():
    cell = 103
    board = chess.Board()
    board_img = _make_board_image(None, board, cell=cell)  # 824x824
    # dark panel above board (like his screenshot: mean 43,46,48)
    screen = np.full((1080, 1920, 3), 34, dtype=np.uint8)
    panel = np.full((68, 824, 3), (43, 46, 48), dtype=np.uint8)
    board_x, board_y = 265, 143
    screen[board_y - 68:board_y, board_x:board_x + 824] = panel
    screen[board_y:board_y + 824, board_x:board_x + 824] = board_img

    # his calibration: region starting at screen 265,75 (includes panel),
    # clipped right/bottom
    class FakeSct:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def grab(self, mon):
            x, y = mon["left"], mon["top"]
            w = min(mon["width"], screen.shape[1] - x)
            h = min(mon["height"], screen.shape[0] - y)
            return screen[y:y + h, x:x + w]
    M.mss.mss = lambda: FakeSct()
    M.user32.GetSystemMetrics = lambda i: 1920 if i == 0 else 1080

    det = M.BoardDetector()
    det.set_region(265, 75, 823, 817)
    r = det.region
    print("fixed region:", r, "cell:", r["width"] / 8)
    assert abs(r["width"] - 824) <= 8, f"square fit failed: {r}"
    assert abs(r["left"] - board_x) <= 8, f"left misaligned: {r['left']} vs {board_x}"
    assert abs(r["top"] - board_y) <= 8, f"top misaligned: {r['top']} vs {board_y}"

    img = det.capture()
    st, _ = det.recognize(img, M.BoardState(), full=True)
    got = st.by_name(det.flip)
    want = {chess.square_name(sq): pc.symbol() for sq, pc in board.piece_map().items()}
    wrong = {sq: (got.get(sq), w) for sq, w in want.items() if got.get(sq) != w}
    assert not wrong, f"mismatches: {list(wrong.items())[:8]}"
    b = det.build_board(st, True)
    assert b is not None and b.piece_map() == board.piece_map()
    print("ALL OK — auto-grow + square fit + recognition on 824px board ✿")

if __name__ == "__main__":
    run()
