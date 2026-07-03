from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QComboBox,
    QVBoxLayout,
    QWidget,
)


LEVEL_COLORS = {
    "INFO": "#C9D1D9",
    "DEBUG": "#8B949E",
    "WARN": "#F0B429",
    "WARNING": "#F0B429",
    "ERROR": "#F85149",
    "SUCCESS": "#3FB950",
}


class LogPanel(QWidget):
    """底部日志面板：分级+过滤。"""

    request_clear = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(140)
        self._build_ui()
        self._min_level = 0

    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        lbl = QLabel("运行日志")
        f = QFont()
        f.setBold(True)
        f.setPointSize(10)
        lbl.setFont(f)
        lbl.setStyleSheet("color: #C9D1D9;")
        top.addWidget(lbl)

        top.addSpacing(12)
        self._level_combo = QComboBox()
        self._level_combo.addItems(["全部 (DEBUG+)", "INFO+", "WARN+", "仅 ERROR"])
        self._level_combo.setStyleSheet(self._combo_style())
        self._level_combo.currentIndexChanged.connect(self._on_level_changed)
        top.addWidget(self._level_combo, 0)

        top.addStretch(1)

        self._btn_clear = QPushButton("清空")
        self._btn_clear.setStyleSheet(self._btn_style())
        self._btn_clear.clicked.connect(self.clear)
        top.addWidget(self._btn_clear)
        outer.addLayout(top)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(2000)
        self._text.setStyleSheet(
            "QPlainTextEdit {"
            "  background: #1C2128; color: #C9D1D9;"
            "  border: 1px solid #2D333B; border-radius: 6px;"
            "  font-family: Consolas, 'Courier New', monospace;"
            "  font-size: 10px; padding: 4px;"
            "}"
        )
        outer.addWidget(self._text, 1)

    @staticmethod
    def _btn_style() -> str:
        return (
            "QPushButton { background: #2D333B; color: #C9D1D9;"
            " border: 1px solid #2D333B; border-radius: 4px;"
            " padding: 3px 12px; font-size: 10px; }"
            "QPushButton:hover { background: #3A3F4B; }"
        )

    @staticmethod
    def _combo_style() -> str:
        return (
            "QComboBox { background: #22272E; color: #C9D1D9;"
            " border: 1px solid #2D333B; border-radius: 4px;"
            " padding: 2px 6px; font-size: 10px; min-width: 100px; }"
        )

    def _on_level_changed(self, idx: int):
        self._min_level = idx

    @staticmethod
    def _level_rank(level: str) -> int:
        key = (level or "INFO").upper()
        if key.startswith("DEB"):
            return 0
        if key.startswith("INFO") or key.startswith("SUC"):
            return 1
        if key.startswith("WARN"):
            return 2
        if key.startswith("ERR"):
            return 3
        return 1

    def append_log(self, msg: str, level: str = "INFO"):
        level_key = (level or "INFO").upper()
        color = LEVEL_COLORS.get(level_key, LEVEL_COLORS["INFO"])
        ts = self._ts()
        if self._level_rank(level) < self._min_level:
            return
        prefix = {
            0: "DEBG", 1: "INFO", 2: "WARN", 3: "ERRO",
        }.get(self._level_rank(level), "INFO")
        html = (
            f'<span style="color:#8B949E;">{ts}</span>'
            f' <span style="color:{color};font-weight:bold;">[{prefix}]</span>'
            f' <span style="color:{color};">{self._escape(msg)}</span>'
        )
        self._text.appendHtml(html)
        sb = self._text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def log_runtime(self, msg: str, level: str = "INFO"):
        self.append_log(msg, level)

    def clear(self):
        self._text.clear()
        self.request_clear.emit()

    @staticmethod
    def _ts() -> str:
        from datetime import datetime
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    @staticmethod
    def _escape(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
