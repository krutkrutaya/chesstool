# -*- coding: utf-8 -*-
"""Worker thread: capture -> recognize -> detect move -> engine -> UI queue.

Adds engine warmup after OUR move: stockfish pre-fills its TT for the
position, so the reply after the enemy move lands in ~10-30 ms.
"""
import time, random, math, threading, queue, os
import chess
import mss

from .detector import BoardDetector, BoardState
from .engine import EngineWrapper, profile_for, ELO_LEVELS
from .config import engine_path, user_data_dir


class Worker(threading.Thread):
    def __init__(self, cfg: dict, ui_queue: queue.Queue):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.ui = ui_queue
        self.detector = BoardDetector()
        self.engine = None
        self.stop_event = threading.Event()
        self.board = chess.Board()
        self.prev_state = BoardState()
        self.our_color = chess.WHITE
        self.full_rescan_needed = True

    # -- plumbing -----------------------------------------------------------
    def log(self, text):
        self.ui.put(("log", text))

    def status(self, text):
        self.ui.put(("status", text))

    def moves(self, data):
        self.ui.put(("moves", data))

    def dump_debug(self):
        """F5: save board screenshot + recognized squares for diagnosis."""
        try:
            from PIL import Image
            dbg = os.path.join(user_data_dir(), "debug")
            os.makedirs(dbg, exist_ok=True)
            img = self.detector.capture()
            Image.fromarray(img).save(os.path.join(dbg, "board.png"))
            lines = []
            for row in range(8):
                lines.append(" ".join(
                    (self.prev_state.squares.get((col, row)) or ".")
                    for col in range(8)))
            with open(os.path.join(dbg, "squares.txt"), "w",
                      encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n\n" + self.board.fen() + "\n")
            self.log(f"debug сохранён: {dbg}\\board.png + squares.txt")
        except Exception as e:
            self.log("debug ошибка: " + str(e))

    # -- loop ---------------------------------------------------------------
    def run(self):
        self.status("поиск доски...")
        saved = self.cfg.get("region")
        if saved and all(k in saved for k in ("left", "top", "width", "height")):
            try:
                self.detector.set_region(saved["left"], saved["top"],
                                         saved["width"], saved["height"])
                self.status("использую сохранённую область доски")
            except Exception:
                self.detector.region = None
        if self.detector.region is None and not self.detector.auto_detect():
            self.status("доска не найдена — нажми F2 и выдели рамкой")
            while not self.stop_event.is_set() and self.detector.region is None:
                time.sleep(0.3)
            if self.stop_event.is_set():
                return
        r = self.detector.region
        self.our_color = chess.BLACK if self.detector.flip else chess.WHITE
        self.ui.put(("region", dict(r)))
        self.status(f"доска: {r['width']}x{r['height']} @ ({r['left']},{r['top']})")
        try:
            self.engine = EngineWrapper(engine_path())
        except Exception as e:
            self.status("ошибка движка: " + str(e))
            return
        prof = self.engine.set_profile(int(self.cfg.get("elo", 1700)))
        self.log(f"движок: Stockfish 19, эло {self.cfg.get('elo')}, "
                 f"skill {prof['skill']}, movetime {prof['movetime']}мс")

        poll = int(self.cfg.get("poll_ms", 120)) / 1000.0
        while not self.stop_event.is_set():
            try:
                self._step()
            except Exception as e:
                self.status("ошибка: " + str(e))
                time.sleep(1.0)
            time.sleep(poll)

    # -- one iteration --------------------------------------------------------
    def _step(self):
        img = self.detector.capture()
        if not self.detector.board_looks_valid(img):
            self.status("доска не видна (перекрыта?)")
            return
        if self.full_rescan_needed:
            st, _ = self.detector.recognize(img, BoardState(), full=True)
            self.detector.flip = self.detector.determine_flip(st)
            self.our_color = chess.BLACK if self.detector.flip else chess.WHITE
            b = self.detector.build_board(st, our_turn=True)
            if b is None:
                self.status("позиция не распознана, пробую ещё...")
                return
            b.turn = chess.WHITE
            # rescan recovery: if the new position differs from our tracked
            # board by exactly ONE enemy move — that move just happened!
            # (incremental matching can miss it: highlight shifts, timing).
            # Treat it as a detected enemy move and analyse immediately.
            recovered = None
            try:
                if self.board is not None and self.board.piece_map():
                    enemy_turn = not self.our_color
                    for mv in self.board.legal_moves:
                        if chess.square_name(mv.from_square) == "" :
                            continue
                        piece = self.board.piece_at(mv.from_square)
                        if piece is None or piece.color != enemy_turn:
                            continue
                        probe = self.board.copy(stack=False)
                        probe.push(mv)
                        if {chess.square_name(sq): pc.symbol()
                                for sq, pc in probe.piece_map().items()} == \
                                st.by_name(self.detector.flip):
                            recovered = mv
                            break
            except Exception:
                pass
            self.board = b
            self.prev_state = st
            self.full_rescan_needed = False
            if recovered is not None:
                self.log(f"ход противника восстановлен из рескана: "
                         f"{recovered.uci()}")
                self.status("думаю...")
                t0 = time.time()
                lines = self.engine.analyse(
                    self.board, profile_for(int(self.cfg.get("elo", 1700)))["movetime"])
                engine_ms = int((time.time() - t0) * 1000)
                if lines:
                    best_cp = max(l[3] for l in lines)
                    show = []
                    for mv, sc, rank, cp in lines:
                        pct = 100.0 * math.exp(-(best_cp - cp) / 80.0)
                        pct = max(0.0, min(100.0, pct))
                        show.append({"uci": mv, "score": sc, "rank": rank,
                                     "pct": pct})
                    prof = profile_for(int(self.cfg.get("elo", 1700)))
                    if prof["weights"] and len(show) > 1:
                        ws = prof["weights"][:len(show)]
                        ssum = sum(ws)
                        rr = random.random() * ssum
                        acc = 0
                        chosen = show[0]
                        for i, wv in enumerate(ws):
                            acc += wv
                            if rr <= acc:
                                chosen = show[i]
                                break
                        chosen["star"] = True
                    if not self.cfg.get("show_top3", True):
                        show = show[:1]
                    if self.cfg.get("human_delay_on", True):
                        dmin = int(self.cfg.get("human_delay_min_ms", 300)) / 1000.0
                        dmax = int(self.cfg.get("human_delay_max_ms", 1400)) / 1000.0
                        delay = random.uniform(dmin, max(dmax, dmin + 0.01))
                    else:
                        delay = 0.0
                    time.sleep(delay)
                    self.ui.put(("region", dict(self.detector.region)))
                    self.moves({
                        "moves": show,
                        "flip": self.detector.flip,
                        "cell": self.detector.cell,
                        "fen": self.board.fen(),
                    })
                    self.log("ходы: " + "  ".join(
                        f"{'★' if m.get('star') else '⌗' + str(m['rank'])} "
                        f"{m['uci'][:2]}→{m['uci'][2:4]} {m['score']} "
                        f"{m['pct']:.0f}%" for m in show) +
                        f"  [движок {engine_ms}мс, показ +{int(delay * 1000)}мс]")
                    self.status("ход показан — сходи и жди ответа")
                    return
            self.status(f"позиция захвачена, играем "
                        f"{'чёрными' if self.detector.flip else 'белыми'} — "
                        f"слежу за доской")
            self.log(f"fen: {self.board.fen()}")
            self.engine.warmup(self.board)
            return

        st, changed = self.detector.recognize(img, self.prev_state)
        if not changed:
            return

        # two-sided move matching: piece_map doesn't depend on side-to-move,
        # so we try moves of both colors (fixes playing as black)
        base = self.board.copy(stack=False)
        candidate = None
        mover_color = None
        piece_map_new = st.by_name(self.detector.flip)
        for turn_color in (chess.WHITE, chess.BLACK):
            b2 = base.copy(stack=False)
            b2.turn = turn_color
            for mv in b2.legal_moves:
                b2.push(mv)
                if {chess.square_name(sq): pc.symbol()
                        for sq, pc in b2.piece_map().items()} == piece_map_new:
                    candidate = mv
                    mover_color = turn_color
                    b2.pop()
                    break
                b2.pop()
            if candidate is not None:
                break

        if candidate is None:
            # diagnostic: dump the mismatch to understand live failures
            best_diff = None
            for turn_color in (chess.WHITE, chess.BLACK):
                b2 = base.copy(stack=False)
                b2.turn = turn_color
                for mv in b2.legal_moves:
                    b2.push(mv)
                    m2 = {chess.square_name(sq): pc.symbol()
                          for sq, pc in b2.piece_map().items()}
                    if m2.keys() != piece_map_new.keys():
                        b2.pop()
                        continue
                    diff = {k: (m2[k], piece_map_new[k])
                            for k in m2 if m2[k] != piece_map_new.get(k)}
                    if diff and (best_diff is None or len(diff) < len(best_diff[2])):
                        best_diff = (turn_color, mv.uci(), diff)
                    b2.pop()
            if best_diff:
                self.log(f"матчинг: нет хода; ближайший {best_diff[1]} "
                         f"расходится в {best_diff[2]}")
            # fast bots reply within one poll tick: both our move and the
            # enemy reply land in the same frame. Match the PAIR of moves.
            pair = None
            for our_m_color in (chess.WHITE, chess.BLACK):
                b1 = base.copy(stack=False)
                b1.turn = our_m_color
                for mv1 in b1.legal_moves:
                    b1.push(mv1)
                    b2 = b1.copy(stack=False)
                    b2.turn = not our_m_color
                    for mv2 in b2.legal_moves:
                        b2.push(mv2)
                        if {chess.square_name(sq): pc.symbol()
                                for sq, pc in b2.piece_map().items()} == piece_map_new:
                            pair = (our_m_color, mv1, mv2)
                            b2.pop()
                            break
                        b2.pop()
                    if pair:
                        b1.pop()
                        break
                    b1.pop()
                if pair:
                    break
            if pair is not None:
                our_m_color, mv1, mv2 = pair
                if our_m_color == self.our_color:
                    # our move + enemy reply: process the enemy move fully
                    san1 = mv1.uci()
                    try:
                        b_san = base.copy(stack=False)
                        b_san.turn = our_m_color
                        san1 = b_san.san(mv1)
                    except Exception:
                        pass
                    san2 = mv2.uci()
                    try:
                        b_san2 = base.copy(stack=False)
                        b_san2.turn = our_m_color
                        b_san2.push(mv1)
                        san2 = b_san2.san(mv2)
                    except Exception:
                        pass
                    self.board.push(mv1)
                    self.board.push(mv2)
                    self.prev_state = st
                    self.log(f"я: {san1}, противник: {san2}")
                    self.status("думаю...")
                    t0 = time.time()
                    lines = self.engine.analyse(
                        self.board,
                        profile_for(int(self.cfg.get("elo", 1700)))["movetime"])
                    engine_ms = int((time.time() - t0) * 1000)
                    if not lines:
                        self.status("движок не дал ход")
                        return
                    best_cp = max(l[3] for l in lines)
                    show = []
                    for mv, sc, rank, cp in lines:
                        pct = 100.0 * math.exp(-(best_cp - cp) / 80.0)
                        pct = max(0.0, min(100.0, pct))
                        show.append({"uci": mv, "score": sc, "rank": rank,
                                     "pct": pct})
                    prof = profile_for(int(self.cfg.get("elo", 1700)))
                    if prof["weights"] and len(show) > 1:
                        ws = prof["weights"][:len(show)]
                        ssum = sum(ws)
                        rr = random.random() * ssum
                        acc = 0
                        chosen = show[0]
                        for i, wv in enumerate(ws):
                            acc += wv
                            if rr <= acc:
                                chosen = show[i]
                                break
                        chosen["star"] = True
                    if not self.cfg.get("show_top3", True):
                        show = show[:1]
                    if self.cfg.get("human_delay_on", True):
                        dmin = int(self.cfg.get("human_delay_min_ms", 300)) / 1000.0
                        dmax = int(self.cfg.get("human_delay_max_ms", 1400)) / 1000.0
                        delay = random.uniform(dmin, max(dmax, dmin + 0.01))
                    else:
                        delay = 0.0
                    time.sleep(delay)
                    self.ui.put(("region", dict(self.detector.region)))
                    self.moves({
                        "moves": show,
                        "flip": self.detector.flip,
                        "cell": self.detector.cell,
                        "fen": self.board.fen(),
                    })
                    self.log("ходы: " + "  ".join(
                        f"{'★' if m.get('star') else '⌗' + str(m['rank'])} "
                        f"{m['uci'][:2]}→{m['uci'][2:4]} {m['score']} "
                        f"{m['pct']:.0f}%" for m in show) +
                        f"  [движок {engine_ms}мс, показ +{int(delay * 1000)}мс]")
                    self.status("ход показан — сходи и жди ответа")
                    return
            # real glitch — rescan
            self.full_rescan_needed = True
            return

        mover_is_opponent = (mover_color != self.our_color)
        san = candidate.uci()
        try:
            b_san = base.copy(stack=False)
            b_san.turn = mover_color
            san = b_san.san(candidate)
        except Exception:
            pass

        if mover_is_opponent:
            self.board.push(candidate)
            self.prev_state = st
            self.log(f"противник: {san}")
            self.status("думаю...")
            t0 = time.time()
            lines = self.engine.analyse(
                self.board,
                profile_for(int(self.cfg.get("elo", 1700)))["movetime"])
            engine_ms = int((time.time() - t0) * 1000)
            if not lines:
                self.status("движок не дал ход")
                return
            best_cp = max(l[3] for l in lines)
            show = []
            for mv, sc, rank, cp in lines:
                pct = 100.0 * math.exp(-(best_cp - cp) / 80.0)
                pct = max(0.0, min(100.0, pct))
                show.append({"uci": mv, "score": sc, "rank": rank, "pct": pct})
            prof = profile_for(int(self.cfg.get("elo", 1700)))
            if prof["weights"] and len(show) > 1:
                ws = prof["weights"][:len(show)]
                ssum = sum(ws)
                rr = random.random() * ssum
                acc = 0
                chosen = show[0]
                for i, wv in enumerate(ws):
                    acc += wv
                    if rr <= acc:
                        chosen = show[i]
                        break
                chosen["star"] = True
            if not self.cfg.get("show_top3", True):
                show = show[:1]
            if self.cfg.get("human_delay_on", True):
                dmin = int(self.cfg.get("human_delay_min_ms", 300)) / 1000.0
                dmax = int(self.cfg.get("human_delay_max_ms", 1400)) / 1000.0
                delay = random.uniform(dmin, max(dmax, dmin + 0.01))
            else:
                delay = 0.0
            time.sleep(delay)
            self.ui.put(("region", dict(self.detector.region)))
            self.moves({
                "moves": show,
                "flip": self.detector.flip,
                "cell": self.detector.cell,
                "fen": self.board.fen(),
            })
            self.log("ходы: " + "  ".join(
                f"{'★' if m.get('star') else '⌗' + str(m['rank'])} "
                f"{m['uci'][:2]}→{m['uci'][2:4]} {m['score']} {m['pct']:.0f}%"
                for m in show) +
                f"  [движок {engine_ms}мс, показ +{int(delay * 1000)}мс]")
            self.status("ход показан — сходи и жди ответа")
        else:
            # we moved: clear overlay, warm the TT for the reply to come
            self.board.push(candidate)
            self.prev_state = st
            self.ui.put(("clear", None))
            self.engine.warmup(self.board)
            self.status("твой ход принят, жду противника...")
