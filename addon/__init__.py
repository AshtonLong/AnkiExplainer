"""Anki Explainer: press X for a concise explanation of the card on screen.

Entry point: checks the Anki version, then registers hooks. Everything heavy
is imported lazily so add-on import stays well under the 50 ms budget.
"""

from __future__ import annotations

import time

_t0 = time.perf_counter()
MIN_POINT_VERSION = 250200  # Anki 25.02


def _anki_version() -> int:
    try:
        from anki.utils import int_version

        return int_version()
    except Exception:
        try:
            from anki.utils import point_version  # type: ignore[attr-defined]

            return int(point_version())
        except Exception:
            return 0


def _init() -> None:
    from aqt import mw

    if mw is None:  # imported outside Anki (e.g. tests)
        return
    if _anki_version() < MIN_POINT_VERSION:
        from aqt import gui_hooks
        from aqt.utils import showWarning

        def warn() -> None:
            showWarning(
                "Anki Explainer needs Anki 25.02 or newer. Please update Anki to use it.",
                title="Anki Explainer",
            )

        gui_hooks.main_window_did_init.append(warn)
        return
    from .explainer import hooks

    hooks.register(__name__)


_init()
__import_ms__ = (time.perf_counter() - _t0) * 1000
