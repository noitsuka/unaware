"""Atomic recovery checkpoints; login cookies are protected by Windows DPAPI.

Showdown assertions are signed for one connection challenge and cannot serve
as reusable session tokens. Only the login-server cookie is retained, scoped
to the exact authentication URL and account. Passwords are never persisted.
"""
from __future__ import annotations

import base64
import asyncio
import ctypes
import json
import os
from pathlib import Path
import re
import threading
import time


async def background_call(function, *args, **kwargs):
    """Cancellable await for bounded blocking work, without executor exit waits.

Network authentication uses real async I/O. Legacy public-data readers have
their own short timeouts; their daemon thread may finish after cancellation,
but cannot keep application shutdown waiting for ThreadPoolExecutor.join().
"""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def deliver(result=None, error=None):
        if future.done():
            return
        future.set_exception(error) if error is not None else future.set_result(result)

    def work():
        try:
            result, error = function(*args, **kwargs), None
        except BaseException as exc:
            result, error = None, exc
        try:
            loop.call_soon_threadsafe(deliver, result, error)
        except RuntimeError:
            pass

    threading.Thread(target=work, name="showdown-bounded-work", daemon=True).start()
    return await future


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise OSError("DPAPI requires Windows")
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    destination = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(destination))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), "Unaware login", None, None, None, 1, ctypes.byref(destination))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(destination.data, destination.size)
    finally:
        kernel.LocalFree(destination.data)


class SessionStore:
    def __init__(self, base_dir: Path):
        self.directory = Path(base_dir) / "data"
        self.path = self.directory / "active-session.json"
        self.secret_path = self.directory / "login-session.dpapi"
        self._lock = threading.RLock()
        self._cookies = {}

    def _write(self, path: Path, value: dict):
        self.directory.mkdir(parents=True, exist_ok=True)
        # Unique name per write: on Windows, replacing a file another process
        # (the app reading the checkpoint, antivirus) holds open raises
        # PermissionError for a moment; retry briefly instead of failing.
        temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
            for attempt in range(5):
                try:
                    temporary.replace(path)
                    return
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(.02 * (attempt + 1))
        finally:
            temporary.unlink(missing_ok=True)

    def save(self, value: dict):
        tag = value.get("battle_tag", "")
        if not re.fullmatch(r"battle-[a-z0-9-]+", tag):
            raise ValueError("Invalid battle identifier.")
        # Whitelist prevents credentials entering an ordinary JSON checkpoint.
        allowed = {"battle_tag", "username", "format_id", "server", "websocket_url", "authentication_url",
                   "team", "archetype", "session_id", "turn", "request", "state", "trace", "sent_rqid"}
        payload = {key: item for key, item in value.items() if key in allowed}
        payload.update(schema=1, updated_at=time.time())
        with self._lock:
            self._write(self.path, payload)

    def load(self) -> dict | None:
        with self._lock:
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if value.get("schema") != 1 or not re.fullmatch(r"battle-[a-z0-9-]+", value.get("battle_tag", "")):
                    return None
                return value
            except (OSError, ValueError, TypeError):
                return None

    def clear(self, battle_tag: str):
        with self._lock:
            if (self.load() or {}).get("battle_tag") == battle_tag:
                self.path.unlink(missing_ok=True)

    # ---- Remembered account (until the user logs out) -------------------
    @property
    def account_path(self) -> Path:
        return self.directory / "account.dpapi"

    def save_account(self, username: str, password: str) -> bool:
        """Keep the login on this Windows user, encrypted with DPAPI."""
        payload = json.dumps({"username": username.casefold(), "password": password}).encode("utf-8")
        try:
            protected = _dpapi(payload)
        except OSError:
            return False
        with self._lock:
            self._write(self.account_path, {"schema": 1, "dpapi": base64.b64encode(protected).decode("ascii")})
        return True

    def account_password(self, username: str) -> str:
        with self._lock:
            try:
                saved = json.loads(self.account_path.read_text(encoding="utf-8"))
                value = json.loads(_dpapi(base64.b64decode(saved["dpapi"]), decrypt=True))
            except (OSError, ValueError, KeyError, TypeError):
                return ""
        return value.get("password", "") if value.get("username") == username.casefold() else ""

    def has_account(self, username: str) -> bool:
        return bool(username) and self.account_path.is_file() and bool(self.account_password(username))

    def logout(self):
        with self._lock:
            self._cookies = {}
            self.account_path.unlink(missing_ok=True)
            self.secret_path.unlink(missing_ok=True)

    def save_cookies(self, username: str, authentication_url: str, cookies: dict):
        payload = {"username": username.casefold(), "url": authentication_url, "cookies": cookies}
        with self._lock:
            self._cookies = payload
            try:
                protected = _dpapi(json.dumps(payload).encode("utf-8"))
            except OSError:
                return  # Other platforms keep tokens in memory only.
            self._write(self.secret_path, {"schema": 1, "dpapi": base64.b64encode(protected).decode("ascii")})

    def cookies(self, username: str, authentication_url: str) -> dict:
        with self._lock:
            value = self._cookies
            if not value:
                try:
                    saved = json.loads(self.secret_path.read_text(encoding="utf-8"))
                    value = json.loads(_dpapi(base64.b64decode(saved["dpapi"]), decrypt=True))
                except (OSError, ValueError, KeyError, TypeError):
                    return {}
            if value.get("username") != username.casefold() or value.get("url") != authentication_url:
                return {}
            return dict(value.get("cookies", {}))
