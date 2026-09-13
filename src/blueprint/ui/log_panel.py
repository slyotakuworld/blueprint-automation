from typing import List, Optional

from PySide6.QtCore import Signal
from PySide6.QtGui import QFont, QTextBlockUserData
from PySide6.QtWidgets import (
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


class _LogLineData(QTextBlockUserData):
    """挂在每个文本块上的附加信息：该行日志对应的执行节点 id（可为空）。"""

    __slots__ = ("node_id",)

    def __init__(self, node_id: str = ""):
        super().__init__()
        self.node_id = node_id


class _LogView(QPlainTextEdit):
    """日志正文：双击某一行时把该行的节点信息发出去（用于定位到画布节点）。"""

    line_double_clicked = Signal(str, str)   # (node_id, 行文本)

    def mouseDoubleClickEvent(self, event):
        try:
            cur = self.cursorForPosition(event.position().toPoint())
            blk = cur.block()
            data = blk.userData()
            nid = getattr(data, "node_id", "") or ""
            self.line_double_clicked.emit(nid, blk.text())
        except Exception:
            pass
        super().mouseDoubleClickEvent(event)


class LogPanel(QWidget):
    """底部日志面板：分级+过滤。双击日志行可定位到画布上的执行节点。"""

    request_clear = Signal()
    node_activated = Signal(str, str)   # (node_id, 行文本)：双击日志行

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(140)
        self._build_ui()
        self._min_level = 0
        self._keep: List[_LogLineData] = []   # 保活：块 userData 的 Python 侧引用

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

        self._text = _LogView()
        self._text.setReadOnly(True)
        self._text.setMaximumBlockCount(2000)
        self._text.setToolTip("双击某一行可跳到对应的画布节点（运行日志带节点信息时可用）")
        self._text.line_double_clicked.connect(self._on_line_double_clicked)
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

    def append_log(self, msg: str, level: str = "INFO", node_id: Optional[str] = None):
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
        # 给刚追加的块挂上节点 id（双击该行即可跳到画布节点）
        try:
            data = _LogLineData(str(node_id or ""))
            self._keep.append(data)
            self._text.document().lastBlock().setUserData(data)
            if len(self._keep) > 2500:
                del self._keep[:500]
        except Exception:
            pass
        sb = self._text.verticalScrollBar()
        sb.setValue(sb.maximum())

    # ---------------- 日志 ↔ 节点 双向定位 ----------------
    def _on_line_double_clicked(self, node_id: str, line_text: str):
        self.node_activated.emit(node_id or "", line_text or "")

    def highlight_node(self, node_id: str, nodes_hint: Optional[dict] = None) -> bool:
        """把日志滚动到最近一条属于 node_id 的日志并选中该行。命中返回 True。"""
        if not node_id:
            return False
        doc = self._text.document()
        target = None
        blk = doc.firstBlock()
        while blk.isValid():
            data = blk.userData()
            if getattr(data, "node_id", "") == node_id:
                target = blk
            blk = blk.next()
        if target is None:
            return False
        cur = self._text.textCursor()
        cur.setPosition(target.position())
        self._text.setTextCursor(cur)
        self._text.centerCursor()
        return True

    def log_runtime(self, msg: str, level: str = "INFO", node_id: Optional[str] = None):
        self.append_log(msg, level, node_id=node_id)

    def clear(self):
        self._keep.clear()
        self._text.clear()
        self.request_clear.emit()

    @staticmethod
    def _ts() -> str:
        from datetime import datetime
        return datetime.now().strftime("%H:%M:%S.%f")[:-3]

    @staticmethod
    def _escape(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
