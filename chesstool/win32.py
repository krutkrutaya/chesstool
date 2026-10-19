# -*- coding: utf-8 -*-
"""Win32 helpers via ctypes (no pywin32 dependency)."""
import ctypes

user32 = ctypes.windll.user32
GWL_EXSTYLE        = -20
WS_EX_LAYERED      = 0x00080000
WS_EX_TRANSPARENT  = 0x00000020
WS_EX_NOACTIVATE   = 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011


def make_clickthrough_and_hidden(hwnd: int) -> None:
    """Overlay: click-through, no activate, invisible to screenshots/OBS.

    NOTE: never add WS_EX_LAYERED here — tk already uses it for
    -transparentcolor; rewriting the style drops the color key and the
    fullscreen window turns into an opaque black sheet.
    """
    try:
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                 style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
    except Exception:
        try:
            style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongW(hwnd, GWL_EXSTYLE,
                                  style | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE)
        except Exception:
            pass
    try:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except Exception:
        pass


def set_dpi_aware() -> None:
    """Make tk coordinates match physical screen pixels (mss).

    Without this, clicks are shifted by the Windows scale factor
    (e.g. 125%: physical 100px becomes logical 80px).
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
