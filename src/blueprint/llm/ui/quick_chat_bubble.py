# -*- coding: utf-8 -*-
"""快速聊天小气泡（悬浮球周围弹出，赫萝风格 · 纯 UI Stub）。

结构：
  · 顶部：圆形赫萝动画头像（工作女仆赫萝.webp）+ 「🐺 赫萝想跟你说~」标题
  · 中部：聊天气泡（复用 FullChatDialog 的 ChatBubble 类）
  · 快捷栏：截图 / 语音 / 预设问题下拉（选择后填入输入框）
  · 底部：输入框 + 苹果红圆形发送按钮
  · 配色：固定奶茶白日赫萝风格（_holo_theme）
  · 欢迎语：赫萝口吻（唔姆 / 苹果商人~）
  · 弹出位置：紧贴悬浮球边缘（左/右自动判断），不遮挡球，自动避屏边界
  · 圆角透明：WA_TranslucentBackground=True，四角无纯色底
"""
from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QEvent, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# ---- 共享主题 & 图标 ----
from ._holo_theme import build_qss, get_theme
from ._icon_loader import icon

# ---- 共享 ChatBubble（尖角气泡 + 赫萝圆形头像）----
from .full_chat_dialog import ChatBubble

# 人设预设索引辅助函数
def _get_preset() -> int:
    try:
        from src.config.user_settings import get_active_preset
        return get_active_preset()
    except Exception:
        return 0


_THEME_NAME = "day"  # 固定奶茶白日主题
_THEME = get_theme(_THEME_NAME)
_QSS = build_qss(_THEME_NAME)


class QuickChatBubble(QDialog):
    """悬浮球旁弹出的迷你聊天气泡窗口。"""

    message_sent = Signal(str)
    thinking_changed = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setObjectName("QuickChatBubble")
        self.setWindowTitle("赫萝 · 快速聊天")
        self.resize(420, 480)
        self.setMinimumWidth(380)
        self.setStyleSheet(_QSS)
        self._anchor: Optional[QPoint] = None
        self._full_dlg = None       # FullChatDialog 引用，用于消息同步
        self._syncing = False       # sync_from 拉取中标志，避免 _append_msg 回写
        self._chat_worker = None
        self._streaming_bubble = None
        self._streaming_text = ""
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

        # ---- 顶部：动画赫萝头像 + 看板娘标题 ----
        head = QHBoxLayout(); head.setSpacing(8)
        from .full_chat_dialog import _CircularMovieAvatar
        _prj_root = os.path.normpath(os.path.join(
            os.path.abspath(os.path.dirname(__file__)), "..", "..", "..", ".."))
        _webp = os.path.join(_prj_root, "images", "工作女仆赫萝.webp")
        avatar = _CircularMovieAvatar(_webp, 32, "avatar_holo_tsundere.png")
        head.addWidget(avatar)
        title = QLabel("🐺 赫萝想跟你说~")
        title.setStyleSheet(
            f"background:transparent; color:{_THEME['primary_dark']}; font-size:14px; font-weight:800;"
        )
        head.addWidget(title, 1)
        head.addStretch(1)
        v.addLayout(head)

        # ---- 聊天列表（复用 ChatBubble 尖角气泡） ----
        self.list_msg = QListWidget()
        self.list_msg.setWordWrap(True); self.list_msg.setSpacing(2)
        self.list_msg.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list_msg.setUniformItemSizes(False)
        v.addWidget(self.list_msg, 1)

        # ---- 输入框 ----
        self.ed_input = QPlainTextEdit()
        self.ed_input.setPlaceholderText("（赫萝：唔？跟咱说两句？🍎  Enter发 / Ctrl+Enter换行）")
        self.ed_input.setFixedHeight(56)
        self.ed_input.installEventFilter(self)
        v.addWidget(self.ed_input)

        # ---- 底部快捷栏：截图 / 语音 / 预设问题下拉 + 🍎苹果红圆形发送 ----
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
            "赫萝：要截图给咱看看吗？\n（Stub：未来调 Windows.GraphicsCapture。）🍎"
        ))
        # 2. � 语音
        q2 = _quick("icon_voice.png", "语音", lambda: QMessageBox.information(
            self, "🎤 语音（Stub）",
            "赫萝：唔…现在咱还不会听你说话啦，下次给咱装个语音耳朵哦~\nStub：暂未接通。"
        ))
        quick.addWidget(q1); quick.addWidget(q2)

        # 3. 📋 预设常见问题下拉（选择后填入输入框）
        self.cmb_preset = QComboBox()
        self.cmb_preset.setMinimumHeight(34)
        self.cmb_preset.setCursor(Qt.PointingHandCursor)
        self.cmb_preset.setToolTip("选一个预设问题，填到输入框里")
        self.cmb_preset.setStyleSheet(
            f"QComboBox {{ border-radius:17px; background:{_THEME['bg_card']};"
            f" border:1px solid {_THEME['border']}; color:{_THEME['text_main']};"
            f" padding: 5px 12px 5px 10px; font-weight:600; }}"
            f"QComboBox:hover {{ border-color:{_THEME['primary']}; }}"
            f"QComboBox::drop-down {{ border:none; width:22px; }}"
            f"QComboBox QAbstractItemView {{ background:{_THEME['bg_card']};"
            f" border:1px solid {_THEME['border']}; selection-background-color:{_THEME['accent']}; }}"
        )
        self.cmb_preset.addItem("📋 常见问题")
        self.cmb_preset.addItem("你是谁呀？")
        self.cmb_preset.addItem("你能帮我做什么？")
        self.cmb_preset.addItem("帮我解释一段代码")
        self.cmb_preset.addItem("推荐一款好玩的游戏")
        self.cmb_preset.addItem("讲个笑话吧")
        self.cmb_preset.addItem("今天聊点什么？")
        self.cmb_preset.currentIndexChanged.connect(self._on_preset_selected)
        quick.addWidget(self.cmb_preset, 1)

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
    def popup_at(self, top_left_global: QPoint):
        """在屏幕全局坐标 top_left_global 处弹出气泡窗口左上角。"""
        self._anchor = QPoint(top_left_global)
        self.move(top_left_global.x(), top_left_global.y())
        self.show()
        QTimer.singleShot(20, self.focus_input)

    def sync_from(self, full_dialog):
        """从 FullChatDialog 的自由聊天同步消息记录（共享同一份记录）。

        - 清空自己的 list_msg，从 full_dialog 拉取所有 (role, text) 重建
        - 保存 full_dialog 引用，后续 _append_msg 时同步回写
        """
        self._full_dlg = full_dialog
        self._syncing = True  # 同步中，_append_msg 不回写 full_dialog
        try:
            self.list_msg.clear()
            for role, text in full_dialog.get_chat_messages():
                self._append_msg(role, text)
        finally:
            self._syncing = False

    def _append_welcome(self):
        self._append_msg(
            "assistant",
            "唔姆！苹果商人，叫咱啥事呀~\n\n"
            "快速问题直接在下面输入，也可以从「📋 常见问题」里挑一个让咱帮你 🐺🍎"
        )

    def _append_msg(self, role: str, text: str):
        # 用窗口宽度计算（resize 过，show 前就有值），避免首次 list_msg.width()==0
        # 窗口宽 - card 左右 margins(10*2) - item 间距(30) = 气泡宽
        win_w = self.width() if self.width() > 0 else 420
        bubble_w = max(340, win_w - 20 - 30)
        bubble = ChatBubble(role, text, mode="chat")
        bubble.update_bubble_width(bubble_w)
        bubble.adjustSize()
        sh = bubble.sizeHint()
        h = max(74, sh.height())
        item = QListWidgetItem()
        item.setSizeHint(QSize(bubble_w, h))
        self.list_msg.addItem(item)
        self.list_msg.setItemWidget(item, bubble)
        QTimer.singleShot(20, lambda: self.list_msg.scrollToBottom())
        # 同步回写到 FullChatDialog（如果不是从 sync_from 拉取的）
        if not getattr(self, "_syncing", False) and getattr(self, "_full_dlg", None) is not None:
            try:
                self._full_dlg._append_chat_msg(role, text)
            except Exception:
                pass

    def _on_preset_selected(self, idx: int):
        """预设下拉选择：第 0 项是占位标题，不填入；其他项填入输入框。"""
        if idx <= 0:
            return
        text = self.cmb_preset.itemText(idx)
        self.ed_input.setPlainText(text)
        self.ed_input.setFocus(Qt.OtherFocusReason)
        # 选完回到占位标题（idx=0 触发 currentIndexChanged 又会回到 idx<=0 直接 return），方便下次再选
        QTimer.singleShot(0, lambda: self.cmb_preset.setCurrentIndex(0))

    def _on_send_clicked(self):
        t = self.ed_input.toPlainText().strip()
        if not t: return
        self._append_msg("user", t)
        self.ed_input.clear()
        self.message_sent.emit(t)
        self.btn_send.setEnabled(False)
        self._start_llm(t)

    def _start_llm(self, user_text: str):
        """启动 LLM 调用。"""
        from src.blueprint.llm.llm_worker import ChatWorker
        from src.blueprint.llm import prompt_builder

        sys_prompt = prompt_builder.build_chat_prompt(_get_preset(), user_query=user_text)
        messages = [{"role": "system", "content": sys_prompt}]
        # 从 FullChatDialog 同步历史
        if self._full_dlg is not None:
            for role, text in self._full_dlg.get_chat_messages():
                if role == "user":
                    messages.append({"role": "user", "content": text})
                elif role == "assistant":
                    messages.append({"role": "assistant", "content": text})

        # 占位气泡
        self._streaming_text = "赫萝正在思考…"
        self._streaming_bubble = self._append_msg_ex("assistant", self._streaming_text)

        self.thinking_changed.emit("thinking")
        self._chat_worker = ChatWorker(messages, stream=True, parent=self)
        self._chat_worker.chunk_received.connect(self._on_chunk)
        self._chat_worker.finished.connect(self._on_finished)
        self._chat_worker.error.connect(self._on_error)
        self._chat_worker.start()

    def _on_chunk(self, chunk: str):
        if self._streaming_bubble is None:
            return
        if self._streaming_text == "赫萝正在思考…":
            self._streaming_text = chunk
        else:
            self._streaming_text += chunk
        self._streaming_bubble.update_text(self._streaming_text)

    def _on_finished(self, full_text: str):
        self.btn_send.setEnabled(True)
        self.thinking_changed.emit("idle")
        if self._streaming_bubble is not None:
            self._streaming_bubble.update_text(full_text)
            self._streaming_bubble = None
        self._streaming_text = ""
        # 回写助手回复到 FullChatDialog（设置 _syncing 避免递归回写）
        if getattr(self, "_full_dlg", None) is not None:
            self._syncing = True
            try:
                self._full_dlg._append_chat_msg("assistant", full_text)
            except Exception:
                pass
            finally:
                self._syncing = False

    def _on_error(self, err_msg: str):
        self.btn_send.setEnabled(True)
        self.thinking_changed.emit("idle")
        if self._streaming_bubble is not None:
            self._streaming_bubble.update_text(f"呜……出了点问题：\n\n{err_msg}")
            self._streaming_bubble = None
        self._streaming_text = ""

    def _append_msg_ex(self, role: str, text: str):
        """追加消息并返回 bubble 引用（用于流式更新）。"""
        from .full_chat_dialog import ChatBubble
        list_w = self.list_msg
        bubble_w = max(280, list_w.width() - 20)
        bubble = ChatBubble(role, text, mode="chat")
        bubble.update_bubble_width(bubble_w)
        bubble.adjustSize()
        sh = bubble.sizeHint()
        item = QListWidgetItem()
        item.setSizeHint(QSize(bubble_w, max(60, sh.height())))
        list_w.addItem(item)
        list_w.setItemWidget(item, bubble)
        QTimer.singleShot(20, lambda: list_w.scrollToBottom())
        return bubble

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
        # 不清空 list_msg：聊天记录要和 FullChatDialog 共享保留，下次打开还在
        super().closeEvent(e)
