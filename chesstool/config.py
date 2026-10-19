# -*- coding: utf-8 -*-
"""Paths, persistent config, resource helpers."""
import sys, os, json

APP_NAME = "ChessTool"

DEFAULT_CONFIG = {
    "elo": 1700,
    "show_top3": True,
    "human_delay_on": True,
    "human_delay_min_ms": 300,
    "human_delay_max_ms": 1400,
    "poll_ms": 120,
    "autodetect": True,
    "region": None,
    "themes_extra": [],       # user-measured (light, dark) pairs
}


def base_dir() -> str:
    if getattr(sys, "frozen", False):
        return sys._MEIPASS
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def user_data_dir() -> str:
    if getattr(sys, "frozen", False):
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP_NAME)
    else:
        d = base_dir()
    os.makedirs(d, exist_ok=True)
    return d


def engine_path() -> str:
    bundled = os.path.join(base_dir(), "engines", "stockfish.exe")
    if not getattr(sys, "frozen", False):
        return bundled
    dest_dir = os.path.join(os.environ.get("TEMP", os.path.expanduser("~")),
                            "chesstool_engine")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "stockfish.exe")
    if not os.path.exists(dest):
        import shutil
        shutil.copyfile(bundled, dest)
    return dest


def templates_dir() -> str:
    return os.path.join(base_dir(), "templates")


def config_path() -> str:
    return os.path.join(user_data_dir(), "config.json")


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(config_path(), "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> None:
    try:
        with open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass
