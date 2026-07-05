# -*- coding: utf-8 -*-
"""快速聊天小气泡（悬浮球周围弹出，赫萝风格 · 纯 UI Stub）。

按 UI 规范 §4.2 改造：
  · 顶部：圆形赫萝小头像 + 「🐺 赫萝想跟你说~」看板娘标题 + [x]关闭
  · 中部：聊天气泡（复用 FullChatDialog 的 ChatBubble 类）
  · 快捷栏：4 个赫萝风格快速功能按钮
      [📷 截图问赫萝 icon_screenshot]
      [🌾 推荐话题 icon_chat]
      [📋 上次蓝图 icon_blueprint]
      [🍎 大聊窗口 icon_send]
  · 底部：输入框 + 苹果红圆形发送按钮
  · 配色：赫萝奶茶白日 / 深夜狼瞳紫（_holo_theme）
  · 欢迎语：赫萝口吻（唔姆 / 苹果商人~）
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

# ---- 共享主题 & 图标 ----
from ._holo_theme import build_qss, get_theme
from ._icon_loader import circular_pixmap, icon, pixmap

# ---- 共享 ChatBubble（尖角气泡 + 赫萝圆形头像）----
from .full_chat_dialog import ChatBubble

_THEME_NAME = os.environ.get("BA_HOLO_THEME", "day")
_THEME = get_theme(_THEME_NAME)
_QSS = build_qss(_THEME_NAME)


class QuickChatBubble(QDialog):
    """悬浮球旁弹出的迷你聊天气泡窗口。"""

    open_full_chat_requested = Signal()
    message_sent = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("QuickChatBubble")
        self.setWindowTitle("赫萝 · 快速聊天")
        self.resize(420, 480)
        self.setMinimumWidth(380)
        self.setStyleSheet(_QSS)
        self._anchor: Optional[QPoint] = None
        self._build_ui()
        self._append_welcome()

    # =================================================================
    # UI
    # =================================================================
    def _build_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # 外框卡片（带阴影 + 赫萝描边）
        card = QFrame()
        card.setObjectName("holoCard")
        card.setStyleSheet(
            f"#holoCard {{ background:{_THEME['bg_root']}; "
            f"border:1px solid {_THEME['primary']}; border-radius:20px; "
            f"padding:0; }}"
        )
        outer.addWidget(card, 1)
        v = QVBoxLayout(card)
        v.setContentsMargins(10, 8, 10, 10)
        v.setSpacing(8)

        # ---- 顶部：看板娘标题 + 关闭 ----
        head = QHBoxLayout(); head.setSpacing(8)
        pm = circular_pixmap("avatar_holo_small.png", 28)
        if not pm.isNull():
            a = QLabel(); a.setPixmap(pm); a.setFixedSize(28, 28); head.addWidget(a)
        title = QLabel("🐺 赫萝想跟你说~")
        title.setStyleSheet(
            f"background:transparent; color:{_THEME['primary_dark']}; font-size:14px; font-weight:800;"
        )
        head.addWidget(title, 1)

        btn_close = QPushButton()
        btn_close.setFixedSize(26, 26); btn_close.setCursor(Qt.PointingHandCursor)
        btn_close.setIcon(icon("icon_close.png", 14)); btn_close.setIconSize(QSize(14, 14))
        btn_close.setToolTip("收起来")
        btn_close.setStyleSheet(
            f"QPushButton {{ border-radius:13px; background:transparent; }}"
            f"QPushButton:hover {{ background:{_THEME['bg_hover']}; }}"
            f"QPushButton:pressed {{ background:{_THEME['accent']}; }}"
        )
        btn_close.clicked.connect(self.close)
        head.addWidget(btn_close)
        v.addLayout(head)

        # ---- 聊天列表（复用 ChatBubble 尖角气泡） ----
        self.list_msg = QListWidget()
        self.list_msg.setWordWrap(True); self.list_msg.setSpacing(2)
        v.addWidget(self.list_msg, 1)

        # ---- 输入框 ----
        self.ed_input = QPlainTextEdit()
        self.ed_input.setPlaceholderText("（赫萝：唔？跟咱说两句？🍎  Enter发 / Ctrl+Enter换行）")
        self.ed_input.setFixedHeight(56)
        self.ed_input.installEventFilter(self)
        v.addWidget(self.ed_input)

        # ---- 底部 4 赫萝快速功能按钮（一行 4 连，最右是🍎苹果红圆形发送） ----
        quick = QHBoxLayout(); quick.setSpacing(8)
        def _quick(icn: str, txt: str, handler) -> QPushButton:
            b = QPushButton(f" {txt}")
            b.setIcon(icon(icn, 18)); b.setIconSize(QSize(18, 18))
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumHeight(34)
            b.setStyleSheet(
                f"QPushButton {{ border-radius:17px; background:{_THEME['bg_card']};"
                f" border:1px solid {_THEME['border']}; color:{_THEME['text_main']};"
                f" padding: 5px 10px; font-weight:600; }}"
                f"QPushButton:hover {{ background:{_THEME['bg_checked']};"
                f" border-color:{_THEME['primary']}; color:{_THEME['primary_dark']}; }}"
            )
            b.clicked.connect(handler)
            return b
        # 1. 📷 截图
        q1 = _quick("icon_screenshot.png", "截图", lambda: QMessageBox.information(
            self, "📷 截图（Stub）",
            "赫萝：要截图给咱看看吗？\n（Stub：未来调 Windows.GraphicsCapture → 直接转成蓝图找图节点。）🍎"
        ))
        # 2. 📋 蓝图
        q2 = _quick("icon_blueprint.png", "蓝图", lambda: QMessageBox.information(
            self, "📋 蓝图生成（Stub）",
            "赫萝：唔姆~ 要写蓝图是吗？先切到「蓝图生成」Tab 哦~\n（Stub：未来直接跳 FullChatDialog 的蓝图 Tab。）"
        ))
        # 3. 🎮 宏
        q3 = _quick("icon_game.png", "宏", lambda: QMessageBox.information(
            self, "🎮 宏（Stub）",
            "赫萝：要连点宏还是找色模板？\n（Stub：未来跳 FullChatDialog 游戏辅助 Tab 连点宏生成器。）🍎"
        ))
        quick.addWidget(q1); quick.addWidget(q2); quick.addWidget(q3)
        quick.addStretch(1)
        # 4. 🍎 发送（苹果红圆形主按钮 40x40）
        self.btn_send = QPushButton()
        self.btn_send.setObjectName("holoAppleSend")
        self.btn_send.setFixedSize(40, 40); self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.setToolTip("发送给赫萝 🍎")
        self.btn_send.setIcon(icon("icon_send.png", 22)); self.btn_send.setIconSize(QSize(22, 22))
        self.btn_send.clicked.connect(self._on_send_clicked)
        quick.addWidget(self.btn_send)
        v.addLayout(quick)

    # =================================================================
    # 行为
    # =================================================================
    def popup_at(self, anchor_global: QPoint, offset_right: int = 30, offset_down: int = 60):
        self._anchor = QPoint(anchor_global)
        dp = self.screen().devicePixelRatio() if self.screen() else 1
        x = int(anchor_global.x() + offset_right / dp)
        y = int(anchor_global.y() + offset_down / dp)
        self.move(x, y)
        self.show()
        QTimer.singleShot(20, self.focus_input)

    def _append_welcome(self):
        self._append_msg(
            "assistant",
            "唔姆！苹果商人，叫咱啥事呀~\n\n"
            "快速问题直接在下面输入，聊嗨了点「大聊窗口」，赫萝女仆全程陪你 🐺🍎"
        )

    def _append_msg(self, role: str, text: str):
        h = ChatBubble.estimate_height(text, 320)
        item = QListWidgetItem()
        item.setSizeHint(QSize(max(340, self.list_msg.width() - 30), h))
        self.list_msg.addItem(item)
        self.list_msg.setItemWidget(item, ChatBubble(role, text))
        QTimer.singleShot(20, lambda: self.list_msg.scrollToBottom())

    def _insert_recommend_topic(self):
        topics = [
            "帮我写个自动启动记事本并输入 Hello 的蓝图",
            "告诉我怎样让蓝图每秒点击左键 10 次",
            "分析一下刚才截的图片里哪个是确定按钮",
            "把这段 Python 代码转成蓝图节点",
        ]
        import random
        self.ed_input.setPlainText(random.choice(topics))
        self.focus_input()

    def _on_go_full(self):
        self.open_full_chat_requested.emit()
        self.close()

    def _on_send_clicked(self):
        t = self.ed_input.toPlainText().strip()
        if not t:
            return
        self._append_msg("user", t)
        self.ed_input.clear()
        self.message_sent.emit(t)
        self.btn_send.setEnabled(False)
        QTimer.singleShot(380, lambda: self._mock_reply(t))

    def _mock_reply(self, t: str):
        self.btn_send.setEnabled(True)
        self._append_msg(
            "assistant",
            f"唔姆~ 收到：『{t[:24]}{'…' if len(t)>24 else ''}』\n"
            f"点顶部「大聊窗口」看详细草稿哦 🍎"
        )

    def eventFilter(self, obj, event):
        if obj is self.ed_input and event.type() == QEvent.KeyPress:
            k = event.key()
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ControlModifier:
                    return False
                self._on_send_clicked(); return True
            if k == Qt.Key_Escape:
                self.close(); return True
        return super().eventFilter(obj, event)

    def focus_input(self):
        try: self.ed_input.setFocus(Qt.OtherFocusReason)
        except Exception: pass

    def closeEvent(self, e):
        try:
            # 避免 Qt 清理时 list_msg 已析构引起警告
            if hasattr(self, "list_msg"): self.list_msg.clear()
        except Exception: pass
        super().closeEvent(e)
