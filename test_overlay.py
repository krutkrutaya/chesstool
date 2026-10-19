# -*- coding: utf-8 -*-
"""Smoke test: the overlay actually paints visible arrows on the screen.
Real tk window, fake board region in the middle, then mss screenshot must
contain the arrow color pixels. WDA capture-exclusion is disabled for the
screenshot check (users see the window; mss would not)."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import tkinter as tk
import mss
import chesstool.overlay as OV

GREEN = np.array([57, 211, 83])      # #39d353
YELLOW = np.array([247, 201, 72])    # #f7c948

def run():
    # disable WDA for this test only (so mss can see the window)
    orig = OV.make_clickthrough_and_hidden
    OV.make_clickthrough_and_hidden = lambda hwnd: None

    root = tk.Tk()
    root.withdraw()  # hidden control window
    ov = OV.Overlay(root)

    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    cell = 90
    side = cell * 8
    region = {"left": (sw - side) // 2, "top": (sh - side) // 2,
              "width": side, "height": side}
    ov.set_region(region)

    moves = [
        {"uci": "e2e4", "score": "+0.25", "rank": 1, "pct": 100.0},
        {"uci": "g1f3", "score": "+0.22", "rank": 2, "pct": 94.0},
        {"uci": "d2d4", "score": "+0.04", "rank": 3, "pct": 71.0},
    ]
    ov.show_moves(moves, flip=False, cell=cell)
    for _ in range(20):
        root.update()
        time.sleep(0.02)

    # window state checks
    assert ov.root.state() == "normal", f"window state: {ov.root.state()}"
    assert ov.root.winfo_ismapped(), "overlay not mapped"
    assert ov.root.winfo_width() >= sw - 40, f"width: {ov.root.winfo_width()}"
    items = ov.canvas.find_all()
    assert len(items) > 20, f"too few canvas items: {len(items)}"

    # pixel check via mss
    time.sleep(0.1)
    for _ in range(5):
        root.update()
    with mss.mss() as sct:
        shot = np.asarray(sct.grab(sct.monitors[1]), dtype=np.uint8)[:, :, :3]
    # whole screen must NOT be a giant black/magenta opaque sheet
    # (regression: rewriting exstyle used to kill the color key)
    for name, rgb in (("green", GREEN), ("yellow", YELLOW)):
        hits = int((np.abs(shot.astype(int) - rgb[None, None, :]).max(axis=2) < 60).sum())
        assert hits > 30, f"no {name} pixels anywhere on screen: {hits}"
    # arrow from e2 (col4,row6) to e4 (col4,row4): vertical line area
    x0 = region["left"] + 4 * cell + cell // 2
    y0 = region["top"] + 4 * cell
    y1 = region["top"] + 7 * cell
    strip = shot[y0:y1, x0 - 4:x0 + 5]
    d = np.abs(strip.astype(int) - GREEN[None, None, :]).max(axis=2)
    green_hits = int((d < 60).sum())
    assert green_hits > 50, f"no green arrow pixels on screen: {green_hits}"
    # the overlay must not cover the whole screen with one solid color
    corners = [shot[5, 5], shot[5, -5], shot[-5, 5], shot[-5, -5]]
    solid = all(np.abs(c.astype(int) - shot.mean(axis=(0, 1)).astype(int)).max() < 12
                for c in corners)
    assert not solid, "screen looks fully covered by an opaque overlay sheet"

    ov.clear()
    root.update()
    assert len(ov.canvas.find_all()) == 0
    root.destroy()
    OV.make_clickthrough_and_hidden = orig
    print(f"OVERLAY SMOKE OK — window mapped, {len(items)} items, "
          f"arrow pixels {green_hits}, badges & colors visible ✿")

if __name__ == "__main__":
    run()

