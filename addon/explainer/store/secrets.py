"""API key storage.

Preference order: the OS keyring (if the ``keyring`` package is importable and
has a working backend), otherwise ``user_files/secret.json`` with user-only
file permissions where the OS supports it. The key is never written to the
add-on config, the log, the cache, or error reports.
"""

from __future__ import annotations

import json
import os
import stat

from ..util.log import get_logger

SERVICE = "anki-explainer"
USERNAME = "openrouter"

log = get_logger("secrets")


def mask(key: str | None) -> str:
    """Display form: ``sk-or-…last4``."""
    if not key:
        return ""
    return f"sk-or-…{key[-4:]}" if len(key) > 8 else "…"


class SecretStore:
    def __init__(self, user_files_dir: str, use_keyring: bool = True) -> None:
        self.path = os.path.join(user_files_dir, "secret.json")
        self._dir = user_files_dir
        self._use_keyring = use_keyring
        self._cached: str | None = None
        self._loaded = False

    # -- keyring -----------------------------------------------------------
    def _keyring(self):  # type: ignore[no-untyped-def]
        if not self._use_keyring:
            return None
        try:
            import keyring  # type: ignore[import-not-found]
            from keyring.backends import fail  # type: ignore[import-not-found]

            if isinstance(keyring.get_keyring(), fail.Keyring):
                return None
            return keyring
        except Exception:
            return None

    # -- public API ----------------------------------------------------------
    def get(self) -> str | None:
        if self._loaded:
            return self._cached
        key: str | None = None
        kr = self._keyring()
        if kr is not None:
            try:
                key = kr.get_password(SERVICE, USERNAME)
            except Exception:
                log.info("keyring read failed; falling back to file")
        if not key:
            key = self._read_file()
        self._cached = key.strip() if key else None
        self._loaded = True
        return self._cached

    def set(self, key: str) -> None:
        key = "".join(key.split())  # API keys never contain whitespace
        if not key:
            self.remove()
            return
        stored = False
        kr = self._keyring()
        if kr is not None:
            try:
                kr.set_password(SERVICE, USERNAME, key)
                stored = True
                self._delete_file()
            except Exception:
                log.info("keyring write failed; falling back to file")
        if not stored:
            self._write_file(key)
        self._cached = key
        self._loaded = True

    def remove(self) -> None:
        kr = self._keyring()
        if kr is not None:
            try:
                kr.delete_password(SERVICE, USERNAME)
            except Exception:
                pass
        self._delete_file()
        self._cached = None
        self._loaded = True

    def has_key(self) -> bool:
        return bool(self.get())

    # -- file fallback -------------------------------------------------------
    def _read_file(self) -> str | None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            value = data.get("openrouter_api_key")
            return value if isinstance(value, str) else None
        except (OSError, ValueError):
            return None

    def _write_file(self, key: str) -> None:
        os.makedirs(self._dir, exist_ok=True)
        tmp = self.path + ".tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"openrouter_api_key": key}, f)
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    def _delete_file(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            log.info("could not delete secret file")
