# -*- coding: utf-8 -*-
"""Stockfish UCI wrapper with elo-capped profiles."""
import subprocess, time, threading, os

ELO_LEVELS = [600, 800, 1000, 1200, 1400, 1600, 1800, 2000, 2200, 2500]

# below 1600 stockfish UCI_Elo floor is 1320, so we emulate weakness:
# low skill + short movetime + randomized pick among top-N with weights.
_LOW_SKILL = {600: 0, 800: 1, 1000: 2, 1200: 4, 1400: 6}
_LOW_WEIGHTS = {
    600: [30, 26, 22, 22],
    800: [42, 30, 28],
    1000: [55, 25, 20],
    1200: [60, 25, 15],
    1400: [65, 22, 13],
}


def profile_for(elo: int) -> dict:
    if elo >= 1600:
        mt = {1600: 150, 1800: 200, 2000: 250, 2200: 320, 2500: 400}.get(elo, 200)
        return {"uci_elo": True, "elo": elo, "skill": 20,
                "movetime": mt, "weights": None}
    skill = _LOW_SKILL[elo]
    weights = _LOW_WEIGHTS[elo]
    mt = 60 + (elo - 600) // 12
    return {"uci_elo": False, "elo": elo, "skill": skill,
            "movetime": mt, "weights": weights}


class EngineWrapper:
    """Raw UCI over pipes, thread-safe. Returns multipv=3 lines."""

    def __init__(self, path: str):
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.p = subprocess.Popen([path], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL, text=True, bufsize=1,
                                  creationflags=creationflags)
        self.lock = threading.Lock()
        self._send("uci")
        self._wait_for("uciok")
        self.set_profile(1700)
        self._send("setoption name MultiPV value 3")
        self._send("isready")
        self._wait_for("readyok")

    # -- low-level ----------------------------------------------------------
    def _send(self, line: str):
        self.p.stdin.write(line + "\n")
        self.p.stdin.flush()

    def _wait_for(self, token: str, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("engine died")
            if line.strip() == token:
                return
        raise RuntimeError("engine timeout waiting for " + token)

    # -- public -------------------------------------------------------------
    def set_profile(self, elo: int):
        prof = profile_for(elo)
        with self.lock:
            if prof["uci_elo"]:
                try:
                    self._send("setoption name UCI_LimitRating value true")
                    self._send(f"setoption name UCI_Elo value {prof['elo']}")
                    self._send("setoption name Skill Level value 20")
                except Exception:
                    pass
            else:
                try:
                    self._send("setoption name UCI_LimitRating value false")
                except Exception:
                    pass
                self._send(f"setoption name Skill Level value {prof['skill']}")
            self._send("isready")
            self._wait_for("readyok")
        return prof

    def warmup(self, board):
        """Pre-fill the TT for the position so the real reply is instant."""
        try:
            with self.lock:
                self._send("position fen " + board.fen())
                self._send("go movetime 120")
                deadline = time.time() + 2.0
                while time.time() < deadline:
                    raw = self.p.stdout.readline()
                    if not raw:
                        break
                    if raw.strip().startswith("bestmove"):
                        break
        except Exception:
            pass

    def analyse(self, board, movetime_ms: int):
        """Returns list of (move, score_string, rank, centipawns), up to 3."""
        import chess
        with self.lock:
            self._send("position fen " + board.fen())
            self._send(f"go movetime {int(movetime_ms)}")
            lines = {}
            best = None
            deadline = time.time() + 15.0
            while time.time() < deadline:
                raw = self.p.stdout.readline()
                if not raw:
                    break
                line = raw.strip()
                if line.startswith("info") and " pv " in line and " score " in line:
                    multipv = 1
                    if " multipv " in line:
                        multipv = int(line.split(" multipv ")[1].split()[0])
                    sc_part = line.split(" score ")[1].split()
                    if sc_part[0] == "cp":
                        cp = int(sc_part[1])
                        score = f"{cp/100:+.2f}"
                    elif sc_part[0] == "mate":
                        mt = int(sc_part[1])
                        cp = 10000 if mt > 0 else -10000
                        score = f"M{mt}"
                    else:
                        cp = 0
                        score = "?"
                    pvmove = line.split(" pv ")[1].split()[0]
                    lines[multipv] = (pvmove, score, cp)
                elif line.startswith("bestmove"):
                    parts = line.split()
                    best = parts[1] if len(parts) > 1 else None
                    break
            result = []
            for k in sorted(lines.keys())[:3]:
                mv, sc, cp = lines[k]
                if mv != "(none)":
                    result.append((mv, sc, k, cp))
            if not result and best and best != "(none)":
                result.append((best, "?", 1, 0))
            return result

    def quit(self):
        try:
            with self.lock:
                self._send("quit")
            self.p.terminate()
        except Exception:
            pass
