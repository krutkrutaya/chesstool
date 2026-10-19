# -*- coding: utf-8 -*-
"""Mock: incremental matching misses the enemy move (any reason — highlight
shift, timing) and the worker rescans. The rescan-recovery path must
recognize the enemy move from the tracked board and still produce moves."""
import sys, os, time, queue, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chess

import chesstool.detector as D
import chesstool.worker as W
import chesstool.config as C
from test_game import MockEngine, render, drain, fen_core, make_worker


def test_rescan_recovery():
    w, ui = make_worker()
    board = chess.Board()
    render(board)
    w._step()   # initial scan
    # our move
    board.push_uci("e2e4")
    render(board)
    w._step()
    kinds = [k for k, _ in drain(ui)]
    assert "clear" in kinds, kinds
    # enemy replies, but we FORCE the incremental matcher to miss it:
    # simulate by marking full_rescan_needed directly (as if no candidate
    # matched), exactly like the live failure signature
    board.push_uci("g8f6")
    render(board)
    w.full_rescan_needed = True
    w._step()
    msgs = drain(ui)
    assert fen_core(w.board.fen()) == fen_core(board.fen()), \
        f"{w.board.fen()} vs {board.fen()}"
    mv = [d for k, d in msgs if k == "moves"]
    assert mv, f"rescan recovery failed: {[k for k, _ in msgs]}"
    moves = mv[-1]["moves"]
    assert abs(moves[0]["pct"] - 100.0) < 1e-6
    print("rescan-recovery OK ->",
          [(m["uci"], m["score"], round(m["pct"])) for m in moves])


def test_rescan_after_two_moves():
    """Double miss (our move + enemy reply never seen): rescan just
    re-syncs the position; the NEXT enemy move is caught normally."""
    w, ui = make_worker()
    board = chess.Board()
    render(board)
    w._step()
    board.push_uci("d2d4")
    board.push_uci("d7d5")
    render(board)
    w.full_rescan_needed = True
    w._step()
    msgs = drain(ui)
    assert fen_core(w.board.fen()) == fen_core(board.fen())
    # our move, then the next enemy move is caught normally
    board.push_uci("c2c4")
    render(board)
    w._step()
    drain(ui)
    board.push_uci("g8f6")
    render(board)
    w._step()
    msgs = drain(ui)
    assert fen_core(w.board.fen()) == fen_core(board.fen())
    mv = [d for k, d in msgs if k == "moves"]
    assert mv, f"tracking lost after rescan: {[k for k, _ in msgs]}"
    print("double-miss resync OK — next enemy move caught")


if __name__ == "__main__":
    random.seed(11)
    test_rescan_recovery()
    test_rescan_after_two_moves()
    print("\nRESCAN RECOVERY MOCKS PASSED ✿")
