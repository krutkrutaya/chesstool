#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ChessTool — chess.com board assistant.

- Auto/manual board capture (any chess.com theme, colors auto-measured)
- Piece recognition via template matching (neo piece set)
- Stockfish 19, elo-capped 600..2500, TT-warmed instant replies
- Human-like display delay, stream-proof overlay, 1-3 ranked moves
  with arrows, score and quality percent
- Hotkeys: F1 start/stop, F2 calibrate, F3 hide overlay, F4 1/3, F5 debug

The tool never clicks anything — you move yourself.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from chesstool.config import engine_path
from chesstool.win32 import set_dpi_aware
from chesstool.gui import ControlPanel


def main():
    # DPI awareness: tk coordinates must match physical screen pixels (mss)
    set_dpi_aware()
    if not os.path.exists(engine_path()):
        print("stockfish.exe not found in engines/ — see README")
        return
    app = ControlPanel()
    app.run()


if __name__ == "__main__":
    main()
