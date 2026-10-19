# -*- coding: utf-8 -*-
"""Region auto-grow + subpixel grid refinement on a synthetic screen."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import chess

import chesstool.detector as D
from test_units import _make_board_image


def run():
    cell = 103
    board = chess.Board()
    board_img = _make_board_image(board, cell=cell)  # 824x824
    screen = np.full((1080, 1920, 3), 34, dtype=np.uint8)
    panel = np.full((68, 824, 3), (43, 46, 48), dtype=np.uint8)
    board_x, board_y = 265, 143
    screen[board_y - 68:board_y, board_x:board_x + 824] = panel
    screen[board_y:board_y + 824, board_x:board_x + 824] = board_img

    class FakeSct:
        monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080},
                    {"left": 0, "top": 0, "width": 1920, "height": 1080}]
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def grab(self, mon):
            x, y = mon["left"], mon["top"]
            w = min(mon["width"], 1920 - x)
            h = min(mon["height"], 1080 - y)
            return screen[y:y + h, x:x + w]

    D.mss.mss = lambda: FakeSct()
    D.user32.GetSystemMetrics = lambda i: 1920 if i == 0 else 1080

    det = D.BoardDetector()
    det.set_region(265, 75, 823, 817)
    r = det.region
    print("fixed region:", r, "cell:", r["width"] / 8)
    assert abs(r["width"] - 824) <= 8, f"square fit failed: {r}"
    assert abs(r["left"] - board_x) <= 8, f"left misaligned: {r['left']}"
    assert abs(r["top"] - board_y) <= 8, f"top misaligned: {r['top']}"

    img = det.capture()
    st, _ = det.recognize(img, D.BoardState(), full=True)
    got = st.by_name(det.flip)
    want = {chess.square_name(sq): pc.symbol()
            for sq, pc in board.piece_map().items()}
    wrong = {sq: (got.get(sq), w) for sq, w in want.items() if got.get(sq) != w}
    assert not wrong, f"mismatches: {list(wrong.items())[:8]}"
    b = det.build_board(st, True)
    assert b is not None and b.piece_map() == board.piece_map()
    print("ALL OK — auto-grow + square fit + recognition on 824px board ✿")


if __name__ == "__main__":
    run()
