# -*- coding: utf-8 -*-
"""Offline unit tests: elo profiles, engine, template recognition."""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import numpy as np
from PIL import Image, ImageDraw

from chesstool import detector as D
from chesstool import engine as E
from chesstool.config import engine_path, templates_dir
import chess


def test_profiles():
    for elo in E.ELO_LEVELS:
        p = E.profile_for(elo)
        assert p["movetime"] >= 50, p
        assert p["skill"] is not None
    print("profiles OK:", {e: E.profile_for(e)["movetime"] for e in E.ELO_LEVELS})


def test_orientation():
    assert D.sq_name(0, 7, flip=False) == "a1"
    assert D.sq_name(0, 0, flip=False) == "a8"
    assert D.sq_name(7, 7, flip=False) == "h1"
    assert D.sq_name(0, 7, flip=True) == "h8"   # black view: bottom-left = h8
    assert D.sq_name(7, 0, flip=True) == "a1"
    print("orientation OK")


def test_engine():
    eng = E.EngineWrapper(engine_path())
    for elo in (600, 1200, 1700, 2500):
        eng.set_profile(elo)
        b = chess.Board()
        b.push_uci("e2e4")
        t0 = time.time()
        lines = eng.analyse(b, E.profile_for(elo)["movetime"])
        ms = (time.time() - t0) * 1000
        assert lines and len(lines) <= 3, lines
        mv, sc, rank, cp = lines[0]
        assert len(mv) >= 4
        print(f"elo {elo}: {lines[:2]} in {ms:.0f} ms")
    # warmup smoke: must not hang
    b = chess.Board()
    b.push_uci("e2e4")
    t0 = time.time()
    eng.warmup(b)
    print(f"warmup OK in {(time.time()-t0)*1000:.0f} ms")
    eng.quit()
    print("engine OK")


def _make_board_image(board, cell=80, theme=((238, 238, 210), (118, 153, 86))):
    size = cell * 8
    img = Image.new("RGB", (size, size), theme[0])
    d = ImageDraw.Draw(img)
    for r in range(8):
        for c in range(8):
            if (r + c) % 2 == 1:
                d.rectangle([c * cell, r * cell, (c + 1) * cell, (r + 1) * cell],
                            fill=theme[1])
    for sq, piece in board.piece_map().items():
        name = chess.square_name(sq)
        file_idx = ord(name[0]) - ord('a')
        rank_idx = int(name[1]) - 1
        col, row = file_idx, 7 - rank_idx
        ch = piece.symbol()
        fn = {"P": "wp", "N": "wn", "B": "wb", "R": "wr", "Q": "wq", "K": "wk",
              "p": "bp", "n": "bn", "b": "bb", "r": "br", "q": "bq",
              "k": "bk"}[ch]
        tpl = Image.open(os.path.join(templates_dir(), fn + ".png")).convert("RGBA")
        tpl = tpl.resize((cell, cell), Image.LANCZOS)
        img.paste(tpl, (col * cell, row * cell), tpl)
    return np.asarray(img, dtype=np.uint8)[:, :, :3]


def _make_board_image_black_view(board, cell=80,
                                 theme=((238, 238, 210), (118, 153, 86))):
    size = cell * 8
    img = Image.new("RGB", (size, size), theme[0])
    d = ImageDraw.Draw(img)
    for r in range(8):
        for c in range(8):
            if (r + c) % 2 == 1:
                d.rectangle([c * cell, r * cell, (c + 1) * cell, (r + 1) * cell],
                            fill=theme[1])
    for sq, piece in board.piece_map().items():
        name = chess.square_name(sq)
        col = 7 - (ord(name[0]) - ord('a'))
        row = int(name[1]) - 1
        ch = piece.symbol()
        fn = {"P": "wp", "N": "wn", "B": "wb", "R": "wr", "Q": "wq", "K": "wk",
              "p": "bp", "n": "bn", "b": "bb", "r": "br", "q": "bq",
              "k": "bk"}[ch]
        tpl = Image.open(os.path.join(templates_dir(), fn + ".png")).convert("RGBA")
        tpl = tpl.resize((cell, cell), Image.LANCZOS)
        img.paste(tpl, (col * cell, row * cell), tpl)
    return np.asarray(img, dtype=np.uint8)[:, :, :3]


def _fake_detector(cell=80):
    det = D.BoardDetector()
    det.region = {"left": 0, "top": 0, "width": cell * 8, "height": cell * 8}
    det.cw = det.ch = float(cell)
    det.cell = cell
    det.flip = False
    return det


def test_recognition():
    det = _fake_detector()
    board = chess.Board()
    img = _make_board_image(board)
    t0 = time.time()
    st, _ = det.recognize(img, D.BoardState(), full=True)
    ms = (time.time() - t0) * 1000
    got = st.by_name(False)
    want = {chess.square_name(sq): pc.symbol()
            for sq, pc in board.piece_map().items()}
    wrong = {sq: (got.get(sq), w) for sq, w in want.items()
             if got.get(sq) != w}
    assert not wrong, f"mismatches: {list(wrong.items())[:8]}"
    assert not det.determine_flip(st), "startpos should be white-bottom"
    print(f"startpos recognition OK ({len(want)} pieces) in {ms:.0f} ms")

    # incremental: white e2e4, opponent e7e5
    board.push_uci("e2e4")
    board.push_uci("e7e5")
    img2 = _make_board_image(board)
    t0 = time.time()
    st2, changed2 = det.recognize(img2, st)
    ms2 = (time.time() - t0) * 1000
    got2 = st2.by_name(False)
    want2 = {chess.square_name(sq): pc.symbol()
             for sq, pc in board.piece_map().items()}
    wrong2 = {sq: (got2.get(sq), w) for sq, w in want2.items()
              if got2.get(sq) != w}
    assert not wrong2, f"incr mismatches: {list(wrong2.items())[:8]}"
    print(f"incremental OK, changed squares: {len(changed2)} in {ms2:.0f} ms")

    b = det.build_board(st2, our_turn=True)
    assert b is not None
    ref = chess.Board()
    ref.push_uci("e2e4")
    ref.push_uci("e7e5")
    assert b.piece_map() == ref.piece_map()
    print("build_board OK ->", b.fen())

    # highlighted last-move cells + coordinate letters
    board3 = chess.Board()
    board3.push_uci("e2e4")
    img3 = Image.fromarray(_make_board_image(board3))
    dd = ImageDraw.Draw(img3)
    for (r, c) in ((6, 4), (4, 4)):
        dd.rectangle([c * 80, r * 80, (c + 1) * 80, (r + 1) * 80],
                     fill=(247, 236, 116))
    tpl = Image.open(os.path.join(templates_dir(), "wp.png")).convert("RGBA")
    tpl = tpl.resize((80, 80), Image.LANCZOS)
    img3.paste(tpl, (4 * 80, 4 * 80), tpl)
    try:
        font = ImageFont_load()
    except Exception:
        font = None
    if font:
        for i, ch in enumerate('abcdefgh'):
            dd.text((i * 80 + 2, 7 * 80 + 62), ch, fill=(20, 20, 20), font=font)
        for i in range(8):
            dd.text((2, i * 80 + 2), str(8 - i), fill=(20, 20, 20), font=font)
    img3 = np.asarray(img3, dtype=np.uint8)[:, :, :3]
    st3, _ = det.recognize(img3, D.BoardState(), full=True)
    got3 = st3.by_name(False)
    want3 = {chess.square_name(sq): pc.symbol()
             for sq, pc in board3.piece_map().items()}
    wrong3 = {sq: (got3.get(sq), w) for sq, w in want3.items()
              if got3.get(sq) != w}
    assert not wrong3, f"highlight mismatches: {list(wrong3.items())[:8]}"
    print("highlight + coords recognition OK")

    # black view -> flip
    imgf = _make_board_image_black_view(chess.Board())
    stf, _ = det.recognize(imgf, D.BoardState(), full=True)
    det.flip = det.determine_flip(stf)
    assert det.flip, "black view should be flip=True"
    bld = det.build_board(stf, our_turn=True)
    assert bld is not None and bld.piece_map() == chess.Board().piece_map()
    print("flip detection OK (black view -> flip, board rebuilt correctly)")


def ImageFont_load():
    from PIL import ImageFont
    try:
        return ImageFont.load_default(16)
    except Exception:
        return ImageFont.load_default()


def test_theme_measure():
    # an unknown theme should be auto-measured and registered
    unknown = ((214, 222, 232), (96, 118, 138))
    det = D.BoardDetector()
    det.USER = None
    img = _make_board_image(chess.Board(), theme=unknown)
    before = len(D.USER_THEMES)
    pair = det.measure_theme(img)
    assert pair is not None, "theme measurement failed"
    assert len(D.USER_THEMES) == before + 1
    # and now the mask must see the board
    m = det._board_mask(img)
    assert m.mean() > 0.5, f"mask still blind after theme registration: {m.mean()}"
    print("auto theme measurement OK ->", pair)


if __name__ == "__main__":
    test_profiles()
    test_orientation()
    test_engine()
    test_recognition()
    test_theme_measure()
    print("\nALL TESTS PASSED ✿")
