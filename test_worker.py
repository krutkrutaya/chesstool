# -*- coding: utf-8 -*-
"""Integration test: full worker loop on a fake screen with the real engine."""
import sys, os, time, queue
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import chess

import chesstool.worker as W
import chesstool.engine as E
import chesstool.config as C
from test_units import _make_board_image

CELL = 103
BOARD_X, BOARD_Y = 265, 143
STATE = {"screen": None}


def render(board_obj):
    sc = np.full((1080, 1920, 3), 34, dtype=np.uint8)
    sc[BOARD_Y:BOARD_Y + 824, BOARD_X:BOARD_X + 824] = _make_board_image(
        board_obj, cell=CELL)
    STATE["screen"] = sc


class FakeSct:
    monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080},
                {"left": 0, "top": 0, "width": 1920, "height": 1080}]
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def grab(self, mon):
        x, y = mon["left"], mon["top"]
        w = min(mon["width"], 1920 - x)
        h = min(mon["height"], 1080 - y)
        return STATE["screen"][y:y + h, x:x + w]


def run():
    import chesstool.detector as D
    D.mss.mss = lambda: FakeSct()
    D.user32.GetSystemMetrics = lambda i: 1920 if i == 0 else 1080

    board = chess.Board()
    render(board)

    cfg = dict(C.DEFAULT_CONFIG)
    cfg.update({"human_delay_on": False, "show_top3": True, "elo": 1700})
    ui = queue.Queue()
    w = W.Worker(cfg, ui)
    w.stop_event.set()  # manual stepping

    assert w.detector.auto_detect(), "auto-detect failed"
    r = w.detector.region
    assert abs(r["left"] - BOARD_X) <= 6 and abs(r["width"] - 824) <= 6, r
    w.our_color = chess.BLACK if w.detector.flip else chess.WHITE
    w.engine = E.EngineWrapper(C.engine_path())
    w.engine.set_profile(1700)

    w._step()
    assert not w.full_rescan_needed, "initial scan failed"
    assert w.board.piece_map() == chess.Board().piece_map()

    # our move e2e4
    board.push_uci("e2e4")
    render(board)
    w._step()
    assert w.board.fen().split()[0] == \
        "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR"
    kinds = []
    while not ui.empty():
        kinds.append(ui.get()[0])
    assert "clear" in kinds, kinds

    # opponent e7e5 -> engine must answer
    board.push_uci("e7e5")
    render(board)
    w._step()
    found = None
    while not ui.empty():
        kind, data = ui.get()
        if kind == "moves":
            found = data
    assert found is not None, "engine produced no moves"
    moves = found["moves"]
    assert 1 <= len(moves) <= 3
    for m in moves:
        assert set(("uci", "score", "rank", "pct")) <= set(m.keys()), m
        assert 0.0 <= m["pct"] <= 100.0
    assert abs(moves[0]["pct"] - 100.0) < 1e-6
    assert chess.Board(board.fen()).parse_uci(moves[0]["uci"])
    print("engine reply:", [(m["uci"], m["score"], round(m["pct"]))
                            for m in moves])

    # warmup speed check: reply after our move should be faster
    board.push_uci(moves[0]["uci"])
    render(board)
    w._step()
    board.push_uci("b8c6")
    render(board)
    t0 = time.time()
    w._step()
    warm_ms = (time.time() - t0) * 1000
    print(f"warm reply pipeline in {warm_ms:.0f} ms")
    w.engine.quit()
    print("WORKER INTEGRATION OK ✿")


if __name__ == "__main__":
    run()
