# ChessTool

Chess assistant for chess.com: reads the board straight from the screen and
suggests moves in the style of the selected Elo (600–2500). Single exe,
instant replies, invisible overlay with arrows.

## Features

- **Auto board detection** + automatic color measurement (any chess.com theme)
- Manual frame calibration (F2), region auto-grow and subpixel grid
  alignment (fractional cells under browser zoom)
- Piece recognition via templates (neo set), incremental (~10 ms per move)
- **Stockfish 19 with an Elo cap** + TT warmup: replies land in tens of ms
- 1–3 options: arrow from → to, score, quality percent
  (`e5 +0.25 100%`), ★ on the recommended pick (low Elo)
- Human-delay 0.3–1.4s: reaction time stays statistically human
- Robust enemy-move tracking: two-step pair matching (instant bot replies),
  rescan recovery (missed move reconstructed from the tracked board)
- Overlay: click-through, invisible to OBS/screenshots
  (WDA_EXCLUDEFROMCAPTURE), mover color determined by the piece itself
- Playing white or black, mates, captures, castling — all tracked
- The tool **never clicks for you** — you move yourself

## Hotkeys

| Key | Action |
|-----|--------|
| F1 | start / stop analysis |
| F2 | calibrate the board with a frame |
| F3 | hide / show overlay |
| F4 | 1 / 3 move options |
| F5 | debug dump (screenshot + recognized position) |

## Run (dev)

```
pip install -r requirements.txt
python main.py
```

Requires `engines/stockfish.exe` (Stockfish 19) — already in the folder.

## Build exe

```
build.bat
```

## Tests (mandatory before changes)

```
run_tests.bat          # all suites
python test_units.py   # profiles, engine, recognition, auto themes
python test_region.py  # region auto-grow, subpixel grid
python test_worker.py  # integration with live Stockfish
python test_game.py    # mocked games: Morphy's Opera (mate), Najdorf as
                      # black, occlusion recovery, noise tolerance
python test_pair.py    # instant enemy replies in a single poll tick
python test_rescan.py  # missed enemy move recovered from rescan
python test_capture.py # our queen move never misdetected as enemy
python test_overlay.py # overlay smoke test (pixels on the real screen)
```

## Structure

```
main.py                 — entry point
chesstool/
  config.py             — paths, config
  win32.py              — ctypes: click-through, DPI-aware
  engine.py             — Stockfish UCI + elo profiles + TT warmup
  detector.py           — board detection, themes, template matching
  overlay.py            — transparent overlay + calibrator
  worker.py             — loop: capture -> moves -> engine -> GUI queue
  gui.py                — panel: switches, elo, sliders, log
engines/stockfish.exe   — Stockfish 19
templates/*.png         — 12 piece templates (neo)
```

## Anti-detection design

- No clicks — highlighting only
- Elo cap: moves match the declared level
- Random display delay + light randomization at low Elo
- Overlay excluded from screen capture (OBS/screenshots)
