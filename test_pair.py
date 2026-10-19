# -*- coding: utf-8 -*-
"""Mock for the LIVE failure scenario: enemy replies instantly after our move
(both land in the SAME poll tick) — two-step pair matching must catch it."""
import sys, os, time, queue, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import chess

import chesstool.detector as D
import chesstool.worker as W
import chesstool.config as C
from test_game import FakeSct, MockEngine, render, drain, fen_core, make_worker
import test_game


def test_instant_reply_same_tick():
    """We move and the enemy's instant reply lands in the same _step() call:
    pair matching (our move + enemy reply) must trigger engine moves."""
    w, ui = make_worker()
    board = chess.Board()
    render(board)
    w._step()
    assert not w.full_rescan_needed

    # our move d2d4 + instant enemy reply d7d5 — BOTH applied before _step
    board.push_uci("d2d4")
    board.push_uci("d7d5")
    render(board)
    w._step()
    msgs = drain(ui)
    assert fen_core(w.board.fen()) == fen_core(board.fen()), \
        f"pair mismatch: {w.board.fen()} vs {board.fen()}"
    mv = [d for k, d in msgs if k == "moves"]
    assert mv, f"pair matching failed, kinds={[k for k,_ in msgs]}"
    moves = mv[-1]["moves"]
    assert abs(moves[0]["pct"] - 100.0) < 1e-6
    print("instant-reply pair matching OK ->",
          [(m["uci"], m["score"], round(m["pct"])) for m in moves])


def test_sequential_enemy_reply():
    """Normal case: our move one tick, enemy reply next tick."""
    w, ui = make_worker()
    board = chess.Board()
    render(board)
    w._step()
    board.push_uci("e2e4")
    render(board)
    w._step()
    kinds = [k for k, _ in drain(ui)]
    assert "clear" in kinds
    board.push_uci("e7e5")
    render(board)
    w._step()
    mv = [d for k, d in drain(ui) if k == "moves"]
    assert mv, "normal enemy reply lost"
    print("sequential enemy reply OK")


if __name__ == "__main__":
    random.seed(7)
    test_instant_reply_same_tick()
    test_sequential_enemy_reply()
    print("\nINSTANT-REPLY MOCKS PASSED ✿")
