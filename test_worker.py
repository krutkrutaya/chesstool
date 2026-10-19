# -*- coding: utf-8 -*-
"""Integration test: full worker loop on a fake screen with the real engine.
Verifies: auto-detect, our move, opponent move -> engine lines with pct."""
import sys, os, time, queue
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image
import main as M
import chess
from test_units import _make_board_image

def run():
    cell = 103
    board = chess.Board()
    screen = np.full((1080, 1920, 3), 34, dtype=np.uint8)
    board_x, board_y = 265, 143
    state = {"screen": screen.copy(), "board": board}

    def render(board_obj):
        sc = np.full((1080, 1920, 3), 34, dtype=np.uint8)
        sc[board_y:board_y + 824, board_x:board_x + 824] = _make_board_image(None, board_obj, cell=cell)
        state["screen"] = sc

    render(board)

    class FakeSct:
        monitors = [{"left": 0, "top": 0, "width": 1920, "height": 1080},
                    {"left": 0, "top": 0, "width": 1920, "height": 1080}]
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def grab(self, mon):
            x, y = mon["left"], mon["top"]
            w = min(mon["width"], 1920 - x)
            h = min(mon["height"], 1080 - y)
            return state["screen"][y:y + h, x:x + w]
    M.mss.mss = lambda: FakeSct()

    cfg = dict(M.DEFAULT_CONFIG)
    cfg["human_delay_on"] = False      # no delay in tests
    cfg["show_top3"] = True
    cfg["elo"] = 1700
    ui = queue.Queue()
    w = M.Worker(cfg, ui)
    w.stop_event.set()  # don't start the loop; drive _step manually

    # --- simulate run() startup: auto-detect + engine
    assert w.detector.auto_detect(), "auto-detect failed"
    r = w.detector.region
    assert abs(r["left"] - board_x) <= 6 and abs(r["width"] - 824) <= 6, r
    w.our_color = chess.BLACK if w.detector.flip else chess.WHITE
    w.engine = M.EngineWrapper(M.engine_path())
    w.engine.set_profile(1700)

    # --- first full scan
    w._step()
    assert not w.full_rescan_needed, "initial scan failed"
    assert w.board.piece_map() == chess.Board().piece_map(), "startpos mismatch"

    # --- our move e2e4 (we are white)
    board.push_uci("e2e4")
    render(board)
    w._step()
    assert w.board.fen().split()[0] == "rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR"
    # queue should have a clear message
    kinds = []
    while not ui.empty():
        kinds.append(ui.get()[0])
    assert "clear" in kinds, kinds

    # --- opponent plays e7e5 -> engine must answer
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
        assert len(m["uci"]) >= 4
    assert moves[0]["pct"] == 100.0 or abs(moves[0]["pct"] - 100.0) < 1e-6
    best_uci = moves[0]["uci"]
    assert chess.Board(board.fen()).parse_uci(best_uci)  # legal move
    print("engine reply:", [(m["uci"], m["score"], round(m["pct"])) for m in moves])
    w.engine.quit()
    print("WORKER INTEGRATION OK ✿")

if __name__ == "__main__":
    run()
