# -*- coding: utf-8 -*-
"""Offline unit tests: engine wrapper, elo profiles, template recognition."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image, ImageDraw

import main as M
import chess

def test_profiles():
    for elo in M.ELO_LEVELS:
        p = M.profile_for(elo)
        assert p["movetime"] >= 50, p
        assert p["skill"] is not None
    print("profiles OK:", {e: M.profile_for(e)["movetime"] for e in M.ELO_LEVELS})

def test_engine():
    eng = M.EngineWrapper(M.engine_path())
    for elo in (600, 1200, 1700, 2500):
        eng.set_profile(elo)
        b = chess.Board()
        b.push_uci("e2e4")
        t0 = time.time()
        lines = eng.analyse(b, M.profile_for(elo)["movetime"])
        ms = (time.time() - t0) * 1000
        assert lines and len(lines) <= 3, lines
        mv, sc, rank, cp = lines[0]
        assert len(mv) >= 4
        print(f"elo {elo}: {lines[:2]} in {ms:.0f} ms")
    eng.quit()
    print("engine OK")

def _make_board_image(detector, board, cell=80, theme=((238,238,210),(118,153,86))):
    """Render a python-chess board the way chess.com looks (rough)."""
    size = cell * 8
    img = Image.new("RGB", (size, size), theme[0])
    d = ImageDraw.Draw(img)
    for r in range(8):
        for c in range(8):
            if (r + c) % 2 == 1:
                d.rectangle([c*cell, r*cell, (c+1)*cell, (r+1)*cell], fill=theme[1])
    # pieces: paste template images
    for sq, piece in board.piece_map().items():
        name = chess.square_name(sq)
        file_idx = ord(name[0]) - ord('a')
        rank_idx = int(name[1]) - 1
        col, row = file_idx, 7 - rank_idx
        ch = piece.symbol()
        fn = {"P":"wp","N":"wn","B":"wb","R":"wr","Q":"wq","K":"wk",
              "p":"bp","n":"bn","b":"bb","r":"br","q":"bq","k":"bk"}[ch]
        tpl = Image.open(os.path.join(M.templates_dir(), fn + ".png")).convert("RGBA")
        tpl = tpl.resize((cell, cell), Image.LANCZOS)
        img.paste(tpl, (col*cell, row*cell), tpl)
    return np.asarray(img, dtype=np.uint8)[:, :, :3]

def _make_board_image_black_view(detector, board, cell=80, theme=((238,238,210),(118,153,86))):
    """Render the board as seen when playing black (flipped coords, upright pieces)."""
    size = cell * 8
    img = Image.new("RGB", (size, size), theme[0])
    d = ImageDraw.Draw(img)
    for r in range(8):
        for c in range(8):
            if (r + c) % 2 == 1:
                d.rectangle([c*cell, r*cell, (c+1)*cell, (r+1)*cell], fill=theme[1])
    for sq, piece in board.piece_map().items():
        name = chess.square_name(sq)
        # flipped visual position: file/ rank mirrored
        col = 7 - (ord(name[0]) - ord('a'))
        row = int(name[1]) - 1
        ch = piece.symbol()
        fn = {"P":"wp","N":"wn","B":"wb","R":"wr","Q":"wq","K":"wk",
              "p":"bp","n":"bn","b":"bb","r":"br","q":"bq","k":"bk"}[ch]
        tpl = Image.open(os.path.join(M.templates_dir(), fn + ".png")).convert("RGBA")
        tpl = tpl.resize((cell, cell), Image.LANCZOS)
        img.paste(tpl, (col*cell, row*cell), tpl)
    return np.asarray(img, dtype=np.uint8)[:, :, :3]

def test_recognition():
    det = M.BoardDetector()
    det.region = {"left": 0, "top": 0, "width": 640, "height": 640}
    det.cell = 80
    det.cw = det.ch = 80.0
    det.flip = False
    det._post_region = lambda: None  # skip screen grab

    board = chess.Board()
    img = _make_board_image(det, board)
    t0 = time.time()
    st, changed = det.recognize(img, M.BoardState(), full=True)
    ms = (time.time() - t0) * 1000
    got = st.by_name(False)
    want = {chess.square_name(sq): pc.symbol() for sq, pc in board.piece_map().items()}
    wrong = {sq: (got.get(sq), want.get(sq)) for sq in want
             if got.get(sq) != want.get(sq)}
    assert not wrong, f"mismatches: {list(wrong.items())[:8]}"
    assert not det.determine_flip(st), "startpos should be white-bottom (flip=False)"
    print(f"startpos recognition OK ({len(want)} pieces) in {ms:.0f} ms full scan")

    # incremental: white plays e2e4, opponent answers e7e5
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    img2 = _make_board_image(det, board)
    t0 = time.time()
    st2, changed2 = det.recognize(img2, st)
    ms2 = (time.time() - t0) * 1000
    got2 = st2.by_name(False)
    want2 = {chess.square_name(sq): pc.symbol() for sq, pc in board.piece_map().items()}
    wrong2 = {sq: (got2.get(sq), want2.get(sq)) for sq in want2
              if got2.get(sq) != want2.get(sq)}
    assert not wrong2, f"incr mismatches: {list(wrong2.items())[:8]}"
    print(f"incremental OK, changed squares: {len(changed2)} in {ms2:.0f} ms")

    # move matching: from pre-move board, one legal move leads to want2
    pre = chess.Board()
    pre.push_uci("e2e4")
    ok_move = None
    for mv in pre.legal_moves:
        pre.push(mv)
        if {chess.square_name(sq): pc.symbol() for sq, pc in pre.piece_map().items()} == want2:
            ok_move = mv
            pre.pop()
            break
        pre.pop()
    assert ok_move and ok_move.uci() == "e7e5", ok_move
    print("move match OK ->", ok_move.uci())

    # build_board round trip
    b = det.build_board(st2, our_turn=True)
    assert b is not None
    want_fen_board = chess.Board(); want_fen_board.push_uci("e2e4"); want_fen_board.push_uci("e7e5")
    assert b.piece_map() == want_fen_board.piece_map()
    print("build_board OK ->", b.fen())

    # flipped board view (playing black): white pieces on top -> flip True
    imgf = _make_board_image_black_view(det, chess.Board())
    stf, _ = det.recognize(imgf, M.BoardState(), full=True)
    det.flip = det.determine_flip(stf)
    assert det.flip, "black view should be flip=True"
    bld = det.build_board(stf, our_turn=True)
    assert bld is not None and bld.piece_map() == chess.Board().piece_map()
    print("flip detection OK (black view -> flip, board rebuilt correctly)")

def test_orientation():
    name = M.sq_name(0, 7, flip=False)
    assert name == "a1", name
    name = M.sq_name(0, 0, flip=False)
    assert name == "a8", name
    name = M.sq_name(7, 7, flip=False)
    assert name == "h1", name
    name = M.sq_name(0, 7, flip=True)
    assert name == "h8", name  # black view: bottom-left = h8
    name = M.sq_name(7, 0, flip=True)
    assert name == "a1", name
    print("orientation OK")

if __name__ == "__main__":
    test_profiles()
    test_orientation()
    test_engine()
    test_recognition()
    print("\nALL TESTS PASSED ✿")

