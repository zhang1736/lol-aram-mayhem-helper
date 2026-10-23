"""Debug 页面：所有排查所需的运行时信息。"""

from __future__ import annotations

from src.core.state_hub import HUB
from src.utils.logger import get_logger

logger = get_logger(__name__)

try:  # pragma: no cover
    from PySide6 import QtCore, QtWidgets

    PYSIDE6_AVAILABLE = True
except ImportError:  # pragma: no cover
    QtCore = QtWidgets = None
    PYSIDE6_AVAILABLE = False


if PYSIDE6_AVAILABLE:  # pragma: no cover

    class DebugWindow(QtWidgets.QDialog):
        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self.setWindowTitle("Debug")
            self.resize(520, 560)
            self.text = QtWidgets.QPlainTextEdit()
            self.text.setReadOnly(True)
            self.text.setStyleSheet("font-family:Consolas,monospace;font-size:12px;")
            layout = QtWidgets.QVBoxLayout(self)
            layout.addWidget(self.text)
            self._timer = QtCore.QTimer(self)
            self._timer.timeout.connect(self.refresh)
            self._timer.start(500)
            self.refresh()

        def refresh(self) -> None:
            snap = HUB.snapshot()
            offers = "\n".join(
                "    slot%d id=%-6s %-14s 强度=%-7s 欢迎度=%-8s 数据=%-7s%s"
                % (i, o.get("augment_id"), o.get("name_cn") or "?",
                   o.get("performance"), o.get("popular"),
                   "PENDING" if o.get("pending") else o.get("data_state", "-"),
                   "  ★" if o.get("recommended") else "")
                for i, o in enumerate(snap.offers)) or "    -"
            self.text.setPlainText("\n".join([
                "== LCU ==",
                "  PID      : %s" % (snap.lcu_pid or "-"),
                "  Port     : %s" % (snap.lcu_port or "-"),
                "  Region   : %s" % (snap.region or "-"),
                "  Connected: %s" % snap.client_connected,
                "  Gameflow : %s" % snap.gameflow,
                "",
                "== Champion ==",
                "  ChampionId: %s" % (snap.champion_id or "-"),
                "  Alias     : %s" % (snap.champion_alias or "-"),
                "  Name      : %s" % (snap.champion_name or "-"),
                "  Candidate : %s" % (snap.candidate_champion_id or "-"),
                "  Final     : %s" % (snap.final_champion_id or "-"),
                "  Level     : %s" % (snap.level if snap.level is not None else "-"),
                "  Stage     : %s" % (snap.stage if snap.stage is not None else "-"),
                "  AugRound  : %s" % snap.augment_round,
                "",
                "== OCR ==",
                "  scan latency: %.0f ms" % snap.ocr_latency_ms,
                "  slot0/1/2   : %s" % (snap.ocr_texts or "-"),
                "  scan mode   : %s" % snap.scan_mode,
                "  ocr calls   : %s" % snap.ocr_calls,
                "",
                "== OP.GG ==",
                "  cache status: %s" % snap.cache_status,
                "  patch       : %s" % (snap.data_patch or "-"),
                "  scope       : %s" % snap.augment_scope,
                "  hero scope  : %s" % snap.hero_scope,
                "  request     : %.0f ms" % snap.perf.opgg_ms,
                "",
                "== Flow ==",
                "  offer_version : %s" % snap.offer_version,
                "  changed_slots : %s" % (snap.changed_slots or []),
                "  rerolled_slots: %s" % (snap.rerolled_slots or []),
                "  recommended   : %s" % (snap.recommended or []),
                "",
                "== Offers ==",
                offers,
                "",
                "== Perf ==",
                "  %s" % snap.perf.describe(),
                "  trace        : %s" % (
                    snap.perf_trace.describe() if snap.perf_trace else "-"),
                "  overlay show : %.1f ms" % snap.perf.overlay_visible_ms,
            ]))

else:  # pragma: no cover

    class DebugWindow:  # type: ignore[no-redef]
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("PySide6 is not installed.")


__all__ = ["DebugWindow"]
