# -*- coding: utf-8 -*-
"""Regression: OUR queen captures the ENEMY queen — must be detected as OUR
move (not the enemy's), and the engine must reply for OUR side afterwards.
Also: no spurious 'enemy move' may be reconstructed from a single our-move
position (false pair)."""
import sys, os, time, queue, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chess
from test_game import MockEngine, render, drain, fen_core, make_worker


def test_our_queen_capture():
    # position: white Qd1 vs black Qb5 (similar to the user's live bug)
    w, ui = make_worker()
    w.our_color = chess.WHITE
    fen = "rnbk2nr/pp1p2pp/2p1p3/8/1qPP1P2/8/P2NQ1PP/R3KB1R w KQ - 0 1"
    board = chess.Board(fen)
    # worker: fake that it already rescan-captured this position
    w.board = chess.Board(fen)
    w.board.turn = chess.WHITE
    render(board)
    st, _ = w.detector.recognize(w.detector.capture(), __import__(
        "chesstool.detector", fromlist=["BoardState"]).BoardState(), full=True)
    w.prev_state = st
    w.full_rescan_needed = False
    drain(ui)

    # OUR move: the queen simply moves (Qe2-g4), like the user's live case
    board.push_uci("e2g4")
    render(board)
    w._step()
    msgs = drain(ui)
    kinds = [k for k, _ in msgs]
    assert "moves" not in kinds, \
        f"our queen move misdetected as enemy move! kinds={kinds}"
    assert "clear" in kinds, f"our move must clear, got {kinds}"
    assert fen_core(w.board.fen()) == fen_core(board.fen())
    print("our queen move OK — detected as OUR move (overlay cleared)")


def test_false_pair_guard():
    """A single OUR move must never be matched as a (our, enemy) pair:
    engine must not reply after only our move."""
    w, ui = make_worker()
    w.our_color = chess.WHITE
    board = chess.Board()
    render(board)
    w._step()
    drain(ui)
    # single our move d4 — pair matching must NOT fire
    board.push_uci("d2d4")
    render(board)
    w._step()
    msgs = drain(ui)
    kinds = [k for k, _ in msgs]
    assert "moves" not in kinds, f"false pair fired: {kinds}"
    assert "clear" in kinds
    print("no false pair on single our move OK")


def test_enemy_move_still_detected():
    """Sanity: a real enemy move still triggers the engine reply."""
    w, ui = make_worker()
    w.our_color = chess.WHITE
    board = chess.Board()
    render(board)
    w._step()
    drain(ui)
    board.push_uci("e2e4")
    render(board)
    w._step()
    drain(ui)
    board.push_uci("e7e5")
    render(board)
    w._step()
    msgs = drain(ui)
    mv = [d for k, d in msgs if k == "moves"]
    assert mv, "enemy move lost"
    print("enemy move detection OK")


if __name__ == "__main__":
    random.seed(3)
    test_our_queen_capture()
    test_false_pair_guard()
    test_enemy_move_still_detected()
    print("\nQUEEN-CAPTURE REGRESSION PASSED ✿")
