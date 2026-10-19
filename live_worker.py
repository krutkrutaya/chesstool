# -*- coding: utf-8 -*-
"""Live end-to-end test on the REAL screen with a real chess.com tab:
worker + engine + overlay. Waits for the enemy bot reply, draws arrows,
screenshots the screen and writes a verdict file."""
import sys, os, time, queue, threading
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import tkinter as tk

from chesstool.worker import Worker
from chesstool.config import DEFAULT_CONFIG, engine_path
from chesstool.overlay import Overlay
import mss

REPORT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "live_report.txt")
SIGNAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "start.txt")

def main():
    out = open(REPORT, "w", encoding="utf-8")
    def log(s):
        print(s)
        out.write(s + "\n")
        out.flush()

    # wait for the driver signal (browser move will be made first)
    t0 = time.time()
    while not os.path.exists(SIGNAL) and time.time() - t0 < 30:
        time.sleep(0.2)
    if not os.path.exists(SIGNAL):
        log("FAIL: no start signal")
        return

    root = tk.Tk()
    root.withdraw()
    ov = Overlay(root)

    cfg = dict(DEFAULT_CONFIG)
    cfg.update({"human_delay_on": False, "show_top3": True, "elo": 1700})
    ui = queue.Queue()
    w = Worker(cfg, ui)
    if not w.detector.auto_detect():
        log("FAIL: auto_detect")
        return
    r = w.detector.region
    log(f"region: {r} cell={r['width']/8}")
    w.our_color = False  # we are white
    from chesstool.engine import EngineWrapper
    w.engine = EngineWrapper(engine_path())
    w.engine.set_profile(1700)
    ov.set_region(dict(r))

    got_moves = None
    deadline = time.time() + 45
    steps = 0
    while time.time() < deadline:
        try:
            w._step()
        except Exception as e:
            log(f"step error: {e}")
        steps += 1
        # drain queue
        while not ui.empty():
            kind, data = ui.get()
            if kind == "log":
                log("worker: " + data)
            elif kind == "region":
                ov.set_region(data)
            elif kind == "moves":
                got_moves = data
                ov.show_moves(data["moves"], data["flip"], data["cell"])
                log("MOVES: " + str([(m["uci"], m["score"], round(m["pct"]))
                                     for m in data["moves"]]))
            elif kind == "status":
                log("status: " + data)
            elif kind == "clear":
                ov.clear()
        root.update()
        if got_moves:
            break
        time.sleep(0.15)

    if not got_moves:
        log("FAIL: no moves within timeout")
        w.engine.quit()
        return

    # arrows should be on screen now: screenshot and verify green pixels
    time.sleep(0.4)
    root.update()
    with mss.mss() as sct:
        shot = np.asarray(sct.grab(sct.monitors[1]), dtype=np.uint8)[:, :, :3]
    green = np.array([57, 211, 83])
    dg = np.abs(shot.astype(int) - green[None, None, :]).max(axis=2)
    hits = int((dg < 60).sum())
    log(f"green arrow pixels on live screen: {hits}")
    # save a cropped screenshot of the board area for the report
    from PIL import Image
    Image.fromarray(shot).crop((r["left"], r["top"],
                                r["left"] + r["width"], r["top"] + r["height"])).save(
        os.path.join(os.path.dirname(REPORT), "live_board.png"))
    if hits > 30:
        log("LIVE TEST PASSED ✿ arrows visible on real screen")
    else:
        log("FAIL: arrows not visible")
    w.engine.quit()
    root.destroy()

if __name__ == "__main__":
    main()
