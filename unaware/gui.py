"""Frameless React desktop shell using the Windows Edge WebView2 runtime."""
from pathlib import Path

from . import __version__
from .backend import serve


class WindowApi:
    def __init__(self, service):
        self._service = service
        self._window = None
        self._close_pending = False
        self._maximized = False
        self._restore_geometry = None

    def minimize(self):
        if self._window:
            self._window.minimize()

    def fullscreen(self):
        """Fill the monitor's work area (taskbar stays visible) or restore.

        A frameless window maximized by WinForms covers the taskbar, so the
        work area is applied explicitly instead of using true fullscreen.
        """
        window = self._window
        if not window:
            return False
        if self._maximized and self._restore_geometry:
            x, y, width, height = self._restore_geometry
            window.resize(width, height)
            window.move(x, y)
            self._maximized = False
            return False
        self._restore_geometry = (window.x, window.y, window.width, window.height)
        # Monitor coordinates are physical pixels; pywebview moves in logical ones.
        scale = float(getattr(getattr(window, "native", None), "_scale", 1) or 1)
        left, top, right, bottom = work_area(int((window.x + window.width / 2) * scale),
                                             int((window.y + window.height / 2) * scale))
        window.move(left / scale, top / scale)
        window.resize(int((right - left) / scale), int((bottom - top) / scale))
        self._maximized = True
        return True

    def close(self):
        self._close_pending = True
        self._service.close()
        self._finish_close()
        return {"waiting_for_battle": False}

    def _finish_close(self):
        if self._window:
            self._window.destroy()


def work_area(x: int, y: int) -> tuple[int, int, int, int]:
    """Usable rectangle (without the taskbar) of the monitor containing a point."""
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("monitor", wintypes.RECT),
                    ("work", wintypes.RECT), ("flags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    user32.MonitorFromPoint.restype = wintypes.HMONITOR
    user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
    monitor = user32.MonitorFromPoint(wintypes.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
    info = MonitorInfo()
    info.size = ctypes.sizeof(MonitorInfo)
    if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        return 0, 0, 1280, 800
    rect = info.work
    return rect.left, rect.top, rect.right, rect.bottom



def initial_geometry() -> tuple[int, int, int, int]:
    """Window size and position in logical pixels: up to 1440x900, never more than
    90% of the primary monitor's work area, centered. The UI scales itself."""
    try:
        import ctypes
        left, top, right, bottom = work_area(0, 0)
        scale = ctypes.windll.user32.GetDpiForSystem() / 96 or 1
    except (AttributeError, OSError):
        return 1280, 800, None, None
    usable_w, usable_h = (right - left) / scale, (bottom - top) / scale
    width = int(max(960, min(1440, usable_w * .9)))
    height = int(max(540, min(900, usable_h * .9)))
    return width, height, int(left / scale + (usable_w - width) / 2), int(top / scale + (usable_h - height) / 2)


def launch(base_dir: Path):
    import webview
    base_dir = Path(base_dir).resolve()
    if not (base_dir / "frontend/dist/index.html").is_file():
        raise RuntimeError("Interface missing. Extract every folder of the package, including frontend/dist.")
    service, server = serve(base_dir)
    api = WindowApi(service)
    try:
        webview.settings["ALLOW_FILE_URLS"] = False
        width, height, x, y = initial_geometry()
        api._window = webview.create_window(
            "Unaware v" + __version__, server.origin, js_api=api,
            width=width, height=height, x=x, y=y, min_size=(960, 540), frameless=True,
            easy_drag=False, shadow=True, background_color="#050506", text_select=True,
        )

        def closing():
            service.close()

        api._window.events.closing += closing

        icon = base_dir / "assets" / "icon.ico"
        try:
            # Own taskbar identity, so Windows shows the bot icon, not Python's.
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Unaware.Launcher")
        except (AttributeError, OSError):
            pass
        webview.start(gui="edgechromium", private_mode=True, debug=False,
                      icon=str(icon) if icon.is_file() else None)
    finally:
        service.close()
        server.shutdown()
        server.server_close()


def startup_error(base_dir: Path, error: Exception):
    """Keep Python-windowed launch failures visible and diagnosable."""
    import os
    import sys
    import traceback
    log = Path(base_dir).resolve() / "logs" / "startup-error.log"
    details = "".join(traceback.format_exception(error))
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(details, encoding="utf-8")
        message = f"Unaware could not start.\n\n{error}\n\nDetails: {log}"
    except OSError:
        message = f"Unaware could not start.\n\n{error}"
    if sys.stderr is not None:
        print(details, file=sys.stderr)
    else:
        if os.name == "nt":
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, "Unaware: startup failed", 0x10)
