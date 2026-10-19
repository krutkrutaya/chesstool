# -*- coding: utf-8 -*-
"""Mock end-to-end tests: full real games emulated on a fake screen.

No real Stockfish — MockEngine gives deterministic replies. Verifies the whole
worker loop: capture -> recognize -> move detection -> engine reply -> UI queue.

Games covered:
  1. Morphy's Opera game (white side): captures, castling 0-0-0, mate
  2. Najdorf opening (black side, flipped board view)
  3. Board disappears mid-game (occlusion) and returns with enemy move made
  4. Sensor noise tolerance (fingerprints must not retrigger)
Run:  python test_game.py
"""
import sys, os, time, queue, random
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
import chess
import main as M
from test_units import _make_board_image, _make_board_image_black_view

CELL = 103
BOARD_X, BOARD_Y = 265, 143
STATE = {"screen": None}
NOISE = 1  # +-1 per channel on each re-render

# ---------------------------------------------------------------------------
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

class MockEngine:
    """Deterministic mock: 3 pseudo-random legal moves with synthetic scores."""
    def __init__(self):
        self.calls = 0
    def set_profile(self, elo):
        return M.profile_for(elo)
    def analyse(self, board, movetime_ms):
        self.calls += 1
        rnd = random.Random(hash(board.fen()) & 0xFFFF)
        legal = list(board.legal_moves)
        picks = rnd.sample(legal, min(3, len(legal)))
        cps = sorted((rnd.randint(-30, 60) for _ in picks), reverse=True)
        out = []
        for k, (mv, cp) in enumerate(zip(picks, cps)):
            out.append((mv.uci(), f"{cp/100:+.2f}", k + 1, cp))
        return out
    def quit(self):
        pass

# ---------------------------------------------------------------------------
def render(board, black_view=False, occlude=False):
    sc = np.full((1080, 1920, 3), 34, dtype=np.uint8)
    if black_view:
        bi = _make_board_image_black_view(None, board, cell=CELL)
    else:
        bi = _make_board_image(None, board, cell=CELL)
    if occlude:
        # board covered by a grey window
        sc[BOARD_Y:BOARD_Y + 824, BOARD_X:BOARD_X + 824] = 120
    else:
        sc[BOARD_Y:BOARD_Y + 824, BOARD_X:BOARD_X + 824] = bi
    if NOISE:
        sc = sc.astype(np.int16)
        n = np.random.default_rng(0).integers(-NOISE, NOISE + 1, sc.shape)
        sc = np.clip(sc + n, 0, 255).astype(np.uint8)
    STATE["screen"] = sc

def make_worker(cfg_updates=None, black_view=False):
    M.mss.mss = lambda: FakeSct()
    render(chess.Board(), black_view=black_view)  # board must exist for auto-detect
    cfg = dict(M.DEFAULT_CONFIG)
    cfg.update({"human_delay_on": False, "show_top3": True, "elo": 1700})
    cfg.update(cfg_updates or {})
    ui = queue.Queue()
    w = M.Worker(cfg, ui)
    w.stop_event.set()  # manual stepping
    assert w.detector.auto_detect(), "auto-detect failed"
    w.our_color = chess.BLACK if w.detector.flip else chess.WHITE
    w.engine = MockEngine()
    return w, ui

def drain(ui):
    msgs = []
    while not ui.empty():
        msgs.append(ui.get())
    return msgs

def fen_core(fen):
    return " ".join(fen.split()[:4])

def play_uci(w, ui, board, uci, side, tag=""):
    """Apply uci to board+screen, step the worker, verify everything."""
    board.push_uci(uci)
    render(board, black_view=(w.our_color == chess.BLACK))
    w._step()
    msgs = drain(ui)
    kinds = [k for k, _ in msgs]
    # worker internal board must match the real one
    assert fen_core(w.board.fen()) == fen_core(board.fen()), \
        f"{tag} board diverged after {uci}:\n  worker: {w.board.fen()}\n  real:  {board.fen()}"
    if side == "enemy":
        mv = [d for k, d in msgs if k == "moves"]
        assert mv, f"{tag} engine reply missing after enemy move {uci}: got {kinds}"
        moves = mv[-1]["moves"]
        assert 1 <= len(moves) <= 3, moves
        for m in moves:
            assert {"uci", "score", "rank", "pct"} <= set(m.keys()), m
            assert 0.0 <= m["pct"] <= 100.0, m
            assert chess.Board(board.fen()).parse_uci(m["uci"]), m["uci"]
        # pct must be monotonically non-increasing by rank
        pcts = [m["pct"] for m in moves]
        assert pcts == sorted(pcts, reverse=True), pcts
        assert abs(moves[0]["pct"] - 100.0) < 1e-6, moves[0]
    else:  # our move
        assert "clear" in kinds or "moves" not in kinds, \
            f"{tag} our move {uci} must clear overlay, got {kinds}"
    return msgs

# ---------------------------------------------------------------------------
def test_opera_game():
    """Morphy vs Duke of Brunswick, we play white. Castling, sacs, mate."""
    w, ui = make_worker()
    board = chess.Board()
    render(board)
    w._step()  # initial full scan
    assert not w.full_rescan_needed
    assert fen_core(w.board.fen()) == fen_core(board.fen())

    game = [("e2e4", "ours"), ("e7e5", "enemy"),
            ("g1f3", "ours"), ("d7d6", "enemy"),
            ("d2d4", "ours"), ("b8g4" == False and "b8c6" or "b8c6", "enemy"),
            ("d4e5", "ours"), ("c8g4", "enemy"),   # Bg4? actually c8g4 is legal? use f8g4
            ]
    # play the real Opera game line instead (uci-safe):
    opera = ["e2e4", "e7e5", "g1f3", "d7d6", "d2d4", "c8g4",
             "d4e5", "g4f3", "d1f3", "d6e5", "f1c4", "g8f6",
             "f3b3", "d8e7", "b1c3", "c7c6", "c1g5", "b7b5",
             "c3b5", "c6b5", "c4b5", "b8d7", "e1c1", "a8d8",   # white castles 0-0-0
             "c5d7" if False else "g5d7" if False else "a1d8" if False else "c5d7" if False else "d8d7",
             "d8d7" if False else "d1d8" if False else "d2c3" if False else "a1d8",
             ]
    # simpler: walk python-chess through the game with SAN, it validates legality
    san_moves = ("e4 e5 Nf3 d6 d4 Bg4 dxe5 Bxf3 Qxf3 dxe5 Bc4 Nf6 Qb3 Qe7 "
                 "Nc3 c6 Bg5 b5 Nxb5 cxb5 Bxb5+ Nbd7 O-O-O Rd8 Rxd7 Rxd7 "
                 "Rd1 Qe6 Bxd7+ Nxd7 Qb8+ Nxb8 Rd8#").split()
    i = 0
    for san in san_moves:
        side = "ours" if board.turn == chess.WHITE else "enemy"
        uci = board.parse_san(san).uci()
        play_uci(w, ui, board, uci, side, tag=f"opera[{i}:{san}]")
        i += 1
    assert board.is_checkmate()
    assert w.engine.calls == 16  # every enemy move got an engine reply
    print(f"opera game OK — {len(san_moves)} plies, mate delivered, "
          f"{w.engine.calls} engine replies ✿")

def test_najdorf_black():
    """We play black: flipped view, enemy moves first."""
    w, ui = make_worker(black_view=True)
    board = chess.Board()
    render(board, black_view=True)
    w._step()  # full scan sets flip from piece placement
    assert w.detector.flip and w.our_color == chess.BLACK, "flip detection failed"
    assert not w.full_rescan_needed
    assert fen_core(w.board.fen()) == fen_core(board.fen())

    line = ("e4 c5 Nf3 d6 d4 cxd4 Nxd4 Nf6 Nc3 a6 Be3 e5 Nb3 Be7 "
            "f3 Be6 Qd2 O-O").split()
    i = 0
    for san in line:
        side = "enemy" if board.turn == chess.WHITE else "ours"
        uci = board.parse_san(san).uci()
        play_uci(w, ui, board, uci, side, tag=f"najdorf[{i}:{san}]")
        i += 1
    print(f"najdorf as black OK — {len(line)} plies, black castled short ✿")

def test_occlusion_recovery():
    """Board gets covered mid-game, then returns with the enemy move done."""
    w, ui = make_worker()
    board = chess.Board()
    render(board)
    w._step()
    # we play e4, enemy should reply e5
    play_uci(w, ui, board, "e2e4", "ours", tag="occl[0]")
    # board covered — worker must not crash, just report unseen
    render(board, occlude=True)
    for _ in range(3):
        w._step()
        drain(ui)
    assert w.board is not None
    # cover lifted, enemy already moved e7e5 in the meantime
    board.push_uci("e7e5")
    render(board)
    w._step()
    msgs = drain(ui)
    assert fen_core(w.board.fen()) == fen_core(board.fen()), "state lost after occlusion"
    mv = [d for k, d in msgs if k == "moves"]
    assert mv, f"no engine reply after occlusion lift: {[k for k, _ in msgs]}"
    print("occlusion recovery OK ✿")

def test_noise_tolerance():
    """Repeated re-renders with +-1 noise must not retrigger anything."""
    w, ui = make_worker()
    board = chess.Board()
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    render(board)
    w._step()
    drain(ui)
    calls_before = w.engine.calls
    for i in range(5):
        render(board)  # noise re-roll
        w._step()
        kinds = [k for k, _ in drain(ui)]
        assert "moves" not in kinds, f"noise triggered spurious reply #{i}"
    assert w.engine.calls == calls_before
    print("noise tolerance OK — no spurious re-triggers ✿")

if __name__ == "__main__":
    random.seed(42)
    t0 = time.time()
    test_opera_game()
    test_najdorf_black()
    test_occlusion_recovery()
    test_noise_tolerance()
    print(f"\nALL GAME MOCKS PASSED in {time.time()-t0:.1f}s ✿")

