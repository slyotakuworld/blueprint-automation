# -*- coding: utf-8 -*-
"""完整对话大窗口（赫萝风格通用助手版 · 纯 UI Stub）。

结构（§4.1 文档规范）：
  ┌──────────────────────────────────────────────────────────────┐
  │ [左 220px 侧栏 holoSideFrame]  │ [右 主区]                      │
  │ ┌─看板娘 64x64 avatar_holo ─┐ │ 顶部操作条（导入/重生成/清空3赫萝Icon）│
  │ │ 赫萝·女仆助理 Lv.99 ♡     │ │ ───────────────────────────── │
  │ ├─────────────────────────┤ │ 聊天消息列表（ChatBubble 重写）│
  │ │ [icon_chat]    自由聊天  │ │ ───────────────────────────── │
  │ │ [icon_blueprint] 蓝图生成│ │ 推荐话题卡（替代快捷短语）    │
  │ │ [icon_image]    图像分析│ │ ───────────────────────────── │
  │ │ [icon_game]    游戏辅助 │ │ 输入卡片：                    │
  │ │ [icon_code]    代码助手│ │  [📷][📂][🎤][😊] [输入] [🍎发送圆]│
  │ │ [icon_memory]  记忆库  │ │                                │
  │ │ [icon_history] 对话历史│ │                                │
  │ │ [icon_settings]助手设置│ │                                │
  │ ├─────────────────────────┤ │                                │
  │ │ [icon_sleep] 休息模式 ⏱│ │                                │
  │ └─────────────────────────┘ │                                │
  └──────────────────────────────────────────────────────────────┘

打桩保留：
  · 发送消息 → 自己气泡 + 模拟打桩回复气泡
  · 导入到画布 / 重新生成 / 清空对话 → QMessageBox
  · 8 Tab 除「自由聊天 / 蓝图生成」外，点击显示对应 Stub 面板（赫萝 Icon + 占位文字）
"""
from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import QByteArray, QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QLinearGradient,
    QMovie,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QTextOption,
)
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QGridLayout,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

# ---- 主题 + 图标加载器 ----
from ._holo_theme import build_qss, get_theme
from ._icon_loader import circular_pixmap, icon, icon_path, pixmap

_THEME_NAME = os.environ.get("BA_HOLO_THEME", "day")  # day / night
_THEME = get_theme(_THEME_NAME)
_QSS = build_qss(_THEME_NAME)


def _qcolor(key: str, fallback: str = "#000000") -> QColor:
    return QColor(_THEME.get(key, fallback))


# 侧栏动画头像 WebP 路径
_PRJ_ROOT = os.path.normpath(os.path.join(
    os.path.abspath(os.path.dirname(__file__)), "..", "..", "..", ".."))
_SIDE_AVATAR_WEBP = os.path.join(_PRJ_ROOT, "images", "坐姿女仆赫萝.webp")


class _CircularMovieAvatar(QLabel):
    """圆形动画头像：QMovie 驱动，每帧圆形裁切 + 描边。WebP 加载失败时回退到静态 llm_icons 头像。"""

    def __init__(self, webp_path: str, size: int = 56,
                 fallback: str = "avatar_holo_holding.png", parent=None):
        super().__init__(parent)
        self._size = size
        self._border_color = _qcolor("border")
        self._movie: Optional[QMovie] = None
        self._fallback_pm = QPixmap()
        if os.path.isfile(webp_path):
            m = QMovie(webp_path, QByteArray(), self)
            m.setScaledSize(QSize(size, size))
            m.start()
            if not m.currentPixmap().isNull():
                self._movie = m
                try:
                    self._movie.frameChanged.connect(self.update)
                except Exception:
                    pass
        if self._movie is None:
            self._fallback_pm = pixmap(fallback, size)
        self.setFixedSize(size, size)

    def paintEvent(self, _evt):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        clip_path = QPainterPath()
        clip_path.addEllipse(0, 0, self._size, self._size)
        p.setClipPath(clip_path)
        if self._movie is not None:
            pm = self._movie.currentPixmap()
            if not pm.isNull():
                p.drawPixmap(0, 0, pm)
        elif not self._fallback_pm.isNull():
            p.drawPixmap(0, 0, self._fallback_pm)
        p.setClipping(False)
        p.setPen(QPen(self._border_color))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(0, 0, self._size - 1, self._size - 1)
        p.end()


# =====================================================================
# ChatBubble 内容绘制子组件（纯 paintEvent，不包含按钮条）
# =====================================================================
class _BubbleContent(QWidget):
    def __init__(self, role: str, text: str):
        super().__init__()
        self._role = role
        self._text = text
        self._holo_avatar_pm = circular_pixmap("avatar_holo_small.png", 32)
        self.setMinimumHeight(58)

    def paintEvent(self, _evt):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        w, h = self.width(), self.height()
        pad = 10
        bw = max(200, min(w - pad * 3 - 40, 520))
        is_user = self._role == "user"
        bx = w - pad - bw if is_user else pad + 40
        by = 2
        bh = max(44, h - 4)
        r = 22
        tip = 8  # §2.3 尖角 8px

        # ---------- 气泡形状（3 圆角 + 1 尖角 8px） ----------
        path = QPainterPath()
        if is_user:
            # 用户：左上/左下/右上 = 22，右下 = 8（尖角指向自己头像）
            path.moveTo(bx + r, by)
            path.lineTo(bx + bw - r, by)
            path.quadTo(bx + bw, by, bx + bw, by + r)
            path.lineTo(bx + bw, by + bh - r)
            path.quadTo(bx + bw, by + bh, bx + bw - r, by + bh)
            path.lineTo(bx + tip, by + bh)
            path.quadTo(bx, by + bh, bx, by + bh - tip)
            path.lineTo(bx, by + r)
            path.quadTo(bx, by, bx + r, by)
            path.closeSubpath()
            grad = QLinearGradient(bx, by, bx, by + bh)
            grad.setColorAt(0, _qcolor("bubble_user_0"))
            grad.setColorAt(1, _qcolor("bubble_user_1"))
            pen_c = _qcolor("primary_dark")
            text_c = _qcolor("text_on_primary")
        else:
            # assistant：右上/右下/左上 = 22，左下 = 8（尖角指向赫萝头像）
            path.moveTo(bx + r, by)
            path.lineTo(bx + bw - r, by)
            path.quadTo(bx + bw, by, bx + bw, by + r)
            path.lineTo(bx + bw, by + bh - r)
            path.quadTo(bx + bw, by + bh, bx + bw - r, by + bh)
            path.lineTo(bx + bw - tip, by + bh)
            path.quadTo(bx, by + bh, bx, by + bh - tip)
            path.lineTo(bx, by + r)
            path.quadTo(bx, by, bx + r, by)
            path.closeSubpath()
            grad = QLinearGradient(bx, by, bx, by + bh)
            grad.setColorAt(0, _qcolor("bubble_holo_0"))
            grad.setColorAt(1, _qcolor("bubble_holo_1"))
            pen_c = _qcolor("bubble_holo_border")
            text_c = _qcolor("text_main")
        p.fillPath(path, QBrush(grad))
        pen = QPen(pen_c)
        pen.setWidth(1)
        p.setPen(pen)
        p.drawPath(path)

        # ---------- 头像 ----------
        if is_user:
            ax = bx + bw + 4
            p.setPen(Qt.NoPen)
            p.setBrush(_qcolor("wolf", "#6B5B73"))
            p.drawEllipse(ax, by + 6, 32, 32)
            p.setPen(_qcolor("text_on_primary"))
            font = QFont()
            font.setPixelSize(16)
            p.setFont(font)
            p.drawText(ax, by + 6, 32, 32, Qt.AlignCenter, "👤")
        else:
            ax = pad
            if not self._holo_avatar_pm.isNull():
                p.drawPixmap(ax, by + 6, self._holo_avatar_pm)
            else:
                p.setBrush(_qcolor("wolf"))
                p.drawEllipse(ax, by + 6, 32, 32)
                p.setPen(_qcolor("maid_white"))
                f = QFont(); f.setPixelSize(16); p.setFont(f)
                p.drawText(ax, by + 6, 32, 32, Qt.AlignCenter, "🐺")

        # ---------- 文本 ----------
        p.setPen(text_c)
        from ._holo_theme import font_stack as _fs
        tf = QFont()
        tf.setPixelSize(13)
        tf.setFamily("Microsoft YaHei UI")
        p.setFont(tf)
        text_pad = 12
        tx = bx + text_pad
        ty = by + text_pad
        tw = bw - text_pad * 2
        th = bh - text_pad * 2
        p.drawText(QRect(tx, ty, tw, th), Qt.TextWordWrap, self._text)
        p.end()


# =====================================================================
# ChatBubble（外框：气泡内容 + 赫萝气泡右下角 3 操作按钮）
# =====================================================================
class ChatBubble(QWidget):
    """单条消息气泡：role='user'(右尖角/金棕) / 'assistant'(左尖角/奶白+赫萝头像+3操作按钮)"""

    retry_clicked = Signal()
    import_clicked = Signal()
    copy_clicked = Signal()

    def __init__(self, role: str, text: str, parent=None):
        super().__init__(parent)
        self._role = role
        self._text = text
        self._content = _BubbleContent(role, text)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.setMinimumHeight(64)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        outer.addWidget(self._content, 1)

        if role == "assistant":
            # ---- 赫萝气泡右下角 3 个操作按钮：重生成 / 导入蓝图 / 复制 ----
            bar_wrap = QHBoxLayout()
            bar_wrap.setContentsMargins(0, 0, 14, 0)
            bar_wrap.addStretch(1)

            def _mini_btn(icn: str, tip: str) -> QPushButton:
                b = QPushButton()
                b.setFixedSize(26, 26); b.setCursor(Qt.PointingHandCursor)
                b.setToolTip(tip)
                b.setIcon(icon(icn, 16)); b.setIconSize(QSize(16, 16))
                b.setStyleSheet(
                    f"QPushButton {{ border-radius:13px; background:transparent;"
                    f" border:1px solid transparent; }}"
                    f"QPushButton:hover {{ background:{_THEME['bg_hover']};"
                    f" border:1px solid {_THEME['border']}; }}"
                    f"QPushButton:pressed {{ background:{_THEME['bg_checked']}; }}"
                )
                return b

            self.btn_retry = _mini_btn("icon_retry.png", "🔄 重新生成（赫萝再写一版）")
            self.btn_retry.clicked.connect(self.retry_clicked.emit)
            self.btn_import = _mini_btn("icon_import.png", "➕ 导入生成结果到画布")
            self.btn_import.clicked.connect(self.import_clicked.emit)
            self.btn_copy = _mini_btn("icon_clear.png", "📋 复制文本")  # 复用 clear 作图标占位，未来可换专图
            # 临时找一个更像"复制"的：icon_import 已用，icon_clear.png 的垃圾桶不合适——用 icon_code 做📋替代
            self.btn_copy.setIcon(icon("icon_code.png", 16))
            self.btn_copy.setToolTip("📋 复制回复文本")
            self.btn_copy.clicked.connect(lambda: (
                QApplication.clipboard().setText(self._text),
                self._flash_copy_done(self.btn_copy),
            ))

            bar_wrap.addWidget(self.btn_retry)
            bar_wrap.addWidget(self.btn_import)
            bar_wrap.addWidget(self.btn_copy)
            outer.addLayout(bar_wrap)
            self.setMinimumHeight(96)

    def _flash_copy_done(self, btn: QPushButton):
        original_tip = btn.toolTip()
        btn.setToolTip("✅ 已复制")
        QTimer.singleShot(1200, lambda: btn.setToolTip(original_tip))

    @staticmethod
    def estimate_height(text: str, bubble_width: int = 520, role: str = "user") -> int:
        font = QFont()
        font.setPixelSize(13)
        fm = QFontMetrics(font)
        rect = fm.boundingRect(
            0, 0, bubble_width - 24, 9999,
            Qt.TextWordWrap | Qt.AlignLeft | Qt.AlignTop,
            text or "",
        )
        base = max(64, rect.height() + 32)
        if role == "assistant":
            base += 34  # 操作按钮条高度
        return base


# =====================================================================
# 8 Tab 的右侧主工作区 Stub 面板（除聊天/蓝图外的 6 个占位页）
# =====================================================================
class _StubPanel(QFrame):
    def __init__(self, icon_name: str, title_cn: str, stub_text: str):
        super().__init__()
        self.setObjectName("holoCard")
        v = QVBoxLayout(self)
        v.setContentsMargins(30, 40, 30, 30)
        v.setSpacing(16)

        il = QHBoxLayout(); il.setSpacing(14); il.setAlignment(Qt.AlignLeft)
        pm = pixmap(icon_name, 72)
        if not pm.isNull():
            lbl = QLabel(); lbl.setPixmap(pm); lbl.setFixedSize(72, 72)
            il.addWidget(lbl)
        title = QLabel(title_cn)
        title.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:22px; font-weight:800;")
        il.addWidget(title, 1)
        v.addLayout(il)

        sep = QFrame(); sep.setFixedHeight(1); sep.setStyleSheet(f"background:{_THEME['border']};")
        v.addWidget(sep)

        txt = QLabel(stub_text)
        txt.setWordWrap(True)
        txt.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:14px; line-height:1.8; padding:8px 0;")
        v.addWidget(txt)
        v.addStretch(1)


# =====================================================================
# Tab 3: 图像分析专属页（§1.1 #3 拖拽高亮区 + 中央大字）
# =====================================================================
class _ImageAnalysisPage(QFrame):
    def __init__(self, parent_dialog):
        super().__init__()
        self.setObjectName("holoCard")
        self._parent_dialog = parent_dialog
        self.setAcceptDrops(True)
        root = QVBoxLayout(self); root.setContentsMargins(24, 24, 24, 24); root.setSpacing(14)

        # 标题
        hl = QHBoxLayout(); hl.setSpacing(12)
        pm = pixmap("icon_image.png", 56)
        if not pm.isNull():
            l = QLabel(); l.setPixmap(pm); l.setFixedSize(56, 56); hl.addWidget(l)
        col = QVBoxLayout(); col.setSpacing(2)
        tt = QLabel("🖼  图像分析"); tt.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:22px; font-weight:800;")
        col.addWidget(tt)
        st = QLabel("把截图扔给赫萝，她帮你找按钮、读字幕、认物品 🐺🍎"); st.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']};")
        col.addWidget(st); hl.addLayout(col, 1)
        root.addLayout(hl)

        # 中央拖拽区（大空框）
        self._drop_zone = QFrame(); self._drop_zone.setObjectName("holoCard")
        self._drop_zone.setMinimumHeight(340)
        self._drop_zone.setStyleSheet(
            f"#holoCard {{ background:{_THEME['bg_root']};"
            f" border:2px dashed {_THEME['border']}; border-radius:20px; }}"
        )
        dz = QVBoxLayout(self._drop_zone); dz.setContentsMargins(20, 30, 20, 30); dz.setSpacing(14)
        big_icon = QLabel(); big_icon.setAlignment(Qt.AlignCenter)
        big_pm = pixmap("icon_image.png", 128)
        if not big_pm.isNull(): big_icon.setPixmap(big_pm); dz.addWidget(big_icon)
        big_text = QLabel("把截图拖进来，或点下面 📷 选图~"); big_text.setAlignment(Qt.AlignCenter)
        big_text.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:20px; font-weight:800;")
        dz.addWidget(big_text)
        sub_text = QLabel("（支持 PNG / JPG / BMP，截图直接 Ctrl+V 粘贴也行哦）"); sub_text.setAlignment(Qt.AlignCenter)
        sub_text.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:13px;")
        dz.addWidget(sub_text)
        dz.addStretch(1)

        # 2 个快速按钮
        btns = QHBoxLayout(); btns.setSpacing(10); btns.setAlignment(Qt.AlignCenter)
        b1 = QPushButton("  📷  从剪贴板 / 截图"); b1.setIcon(icon("icon_screenshot.png", 20)); b1.setIconSize(QSize(20,20))
        b1.setCursor(Qt.PointingHandCursor); b1.setObjectName("holoPrimary"); b1.setMinimumHeight(36)
        b1.clicked.connect(lambda: QMessageBox.information(
            self._parent_dialog, "Stub：未来 OCR + VLM 识别",
            "赫萝：把截图给咱看看~ 🍎\n\n（Stub：下一阶段接 Windows.GraphicsCapture + OCR/VLM，识别结果直接转「找图/点击」蓝图节点。）"
        ))
        b2 = QPushButton("  📂  选择图片文件"); b2.setIcon(icon("icon_upload.png", 20)); b2.setIconSize(QSize(20,20))
        b2.setCursor(Qt.PointingHandCursor); b2.setMinimumHeight(36)
        b2.clicked.connect(lambda: QMessageBox.information(
            self._parent_dialog, "Stub：未来 OCR + VLM 识别",
            "赫萝：唔姆~ 图片选好啦，等下咱帮你认哪个是确定按钮哦~\n\n（Stub：下一阶段接入。）"
        ))
        btns.addWidget(b1); btns.addWidget(b2); dz.addLayout(btns)
        root.addWidget(self._drop_zone, 1)

    # ---- 拖拽高亮 ----
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() or e.mimeData().hasImage():
            e.acceptProposedAction()
            self._drop_zone.setStyleSheet(
                f"#holoCard {{ background:{_THEME['bg_checked']};"
                f" border:2px solid {_THEME['primary']}; border-radius:20px; }}"
            )
    def dragLeaveEvent(self, _e):
        self._drop_zone.setStyleSheet(
            f"#holoCard {{ background:{_THEME['bg_root']};"
            f" border:2px dashed {_THEME['border']}; border-radius:20px; }}"
        )
    def dropEvent(self, e):
        self.dragLeaveEvent(e)
        QMessageBox.information(
            self._parent_dialog, "Stub：未来 OCR + VLM 识别",
            "赫萝：收到图片 🍎\n\n（Stub：下一阶段调 OCR + VLM 识别，识别结果直接转成「找图/点击」的蓝图节点。）"
        )


# =====================================================================
# Tab 4: 游戏辅助专属页（§1.1 #4 4个卡片：连点宏/找色/一键热键/攻略问答）
# =====================================================================
class _GameAssistPage(QFrame):
    def __init__(self, parent_dialog):
        super().__init__()
        self.setObjectName("holoCard")
        self._parent_dialog = parent_dialog
        root = QVBoxLayout(self); root.setContentsMargins(24, 24, 24, 24); root.setSpacing(14)

        hl = QHBoxLayout(); hl.setSpacing(12)
        pm = pixmap("icon_game.png", 56)
        if not pm.isNull(): l = QLabel(); l.setPixmap(pm); l.setFixedSize(56,56); hl.addWidget(l)
        col = QVBoxLayout(); col.setSpacing(2)
        tt = QLabel("🎮  游戏辅助"); tt.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:22px; font-weight:800;")
        col.addWidget(tt)
        st = QLabel("连点宏、找色模板、一键热键、游戏攻略——赫萝陪你通关 🐺🍎"); st.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']};")
        col.addWidget(st); hl.addLayout(col, 1)
        root.addLayout(hl)

        # 4 卡片（2x2 Grid）
        grid = QGridLayout(); grid.setSpacing(14); grid.setContentsMargins(0, 4, 0, 0)
        cards = [
            ("icon_game.png",      "🎯  连点宏生成器",     "配置左键/右键连点频率和持续时间，直接导出蓝图节点组",  "连点器配置"),
            ("icon_image.png",     "🎨  找色模板库",       "管理每个游戏的找色/找图模板，自动生成 FindImage/FindColor 节点", "模板管理"),
            ("icon_screenshot.png","🎹  一键热键",         "把一组复杂操作绑定到单个热键（例如：F1=一键喝药连招）", "热键配置"),
            ("icon_history.png",   "📜  游戏攻略问答",     "问赫萝任何游戏攻略，她会整理成「操作步骤清单」",       "攻略问答"),
        ]
        for i, (icn, title, desc, key) in enumerate(cards):
            r, c = divmod(i, 2)
            grid.addWidget(self._make_card(icn, title, desc, key), r, c)
        root.addLayout(grid, 1)

    def _make_card(self, icn: str, title: str, desc: str, key: str) -> QFrame:
        f = QFrame(); f.setObjectName("holoCard"); f.setCursor(Qt.PointingHandCursor)
        f.setStyleSheet(
            f"#holoCard {{ background:{_THEME['bg_card']}; border:1px solid {_THEME['border']};"
            f" border-radius:20px; }}"
            f"#holoCard:hover {{ border:1px solid {_THEME['primary']}; }}"
        )
        v = QVBoxLayout(f); v.setContentsMargins(18, 18, 18, 18); v.setSpacing(10)
        h = QHBoxLayout(); h.setSpacing(12)
        pm = pixmap(icn, 48)
        if not pm.isNull():
            a = QLabel(); a.setPixmap(pm); a.setFixedSize(48, 48); h.addWidget(a)
        tc = QVBoxLayout(); tc.setSpacing(2)
        t = QLabel(title); t.setStyleSheet(f"background:transparent;color:{_THEME['maid_black']}; font-size:17px; font-weight:800;")
        tc.addWidget(t)
        d = QLabel(desc); d.setWordWrap(True); d.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:12px;")
        tc.addWidget(d); h.addLayout(tc, 1); v.addLayout(h)
        go = QPushButton(f"  打开 {key}  →"); go.setObjectName("holoSuccess"); go.setCursor(Qt.PointingHandCursor)
        go.setIcon(icon("icon_import.png", 16)); go.setIconSize(QSize(16, 16)); go.setMinimumHeight(32)
        go.clicked.connect(lambda _=False, k=key: QMessageBox.information(
            self._parent_dialog, "Stub：未来生成 Blueprint 节点组 ✨",
            f"赫萝：唔姆~ 「{k}」的界面马上就有啦~\n\n（Stub：真实接入时会生成对应的蓝图节点组，直接导入画布。）🍎"
        ))
        v.addWidget(go)
        return f


# =====================================================================
# Tab 5: 代码助手专属页（§1.1 #5 上代码框/下输出 + 顶部3按钮）
# =====================================================================
class _CodeAssistPage(QFrame):
    def __init__(self, parent_dialog):
        super().__init__()
        self.setObjectName("holoCard")
        self._parent_dialog = parent_dialog
        root = QVBoxLayout(self); root.setContentsMargins(24, 24, 24, 24); root.setSpacing(14)

        hl = QHBoxLayout(); hl.setSpacing(12)
        pm = pixmap("icon_code.png", 56)
        if not pm.isNull(): l = QLabel(); l.setPixmap(pm); l.setFixedSize(56,56); hl.addWidget(l)
        col = QVBoxLayout(); col.setSpacing(2)
        tt = QLabel("📝  代码助手"); tt.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:22px; font-weight:800;")
        col.addWidget(tt)
        st = QLabel("贴代码，赫萝帮你解释 / 重构 / 转蓝图 / 找 Bug 🐺🍎"); st.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']};")
        col.addWidget(st); hl.addLayout(col, 1)
        root.addLayout(hl)

        # 3 个赫萝按钮
        btn_row = QHBoxLayout(); btn_row.setSpacing(10)
        b_exp = QPushButton("  💡  解释代码"); b_exp.setObjectName("holoPrimary"); b_exp.setCursor(Qt.PointingHandCursor); b_exp.setMinimumHeight(34)
        b_exp.setIcon(icon("icon_chat.png", 18)); b_exp.setIconSize(QSize(18,18))
        b_ref = QPushButton("  🪄  重构风格"); b_ref.setCursor(Qt.PointingHandCursor); b_ref.setMinimumHeight(34)
        b_ref.setIcon(icon("icon_retry.png", 18)); b_ref.setIconSize(QSize(18,18))
        b_bp = QPushButton("  📋  转蓝图节点"); b_bp.setObjectName("holoSuccess"); b_bp.setCursor(Qt.PointingHandCursor); b_bp.setMinimumHeight(34)
        b_bp.setIcon(icon("icon_blueprint.png", 18)); b_bp.setIconSize(QSize(18,18))
        for b in (b_exp, b_ref, b_bp):
            b.clicked.connect(lambda _=False, x=b.text(): QMessageBox.information(
                self._parent_dialog, f"Stub：{x.strip()}",
                f"赫萝：唔姆~ 代码分析功能马上就好啦~\n\n（Stub：下一阶段接 AST，{x.strip()}结果会写在下面输出框。）🍎"
            ))
        btn_row.addWidget(b_exp); btn_row.addWidget(b_ref); btn_row.addWidget(b_bp); btn_row.addStretch(1)
        root.addLayout(btn_row)

        # 代码框
        self._code = QPlainTextEdit()
        self._code.setPlaceholderText(
            "// 在这里粘贴 Python / JSON / 蓝图 JSON / 任何代码…\n"
            "// 示例：def add(a,b):\n//    return a+b"
        )
        self._code.setMinimumHeight(200)
        root.addWidget(self._code, 2)

        # 输出框
        ol = QHBoxLayout(); ol.setSpacing(8); ol.setAlignment(Qt.AlignVCenter)
        lt = QLabel("📤  输出："); lt.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-weight:800;")
        ol.addWidget(lt); ol.addStretch(1)
        cb = QPushButton("  📋  复制"); cb.setIcon(icon("icon_import.png", 14)); cb.setIconSize(QSize(14,14)); cb.setCursor(Qt.PointingHandCursor)
        cb.clicked.connect(lambda: (QApplication.clipboard().setText(self._out.toPlainText()), QMessageBox.information(self._parent_dialog, "✅", "赫萝：已复制到剪贴板 🍎")))
        ol.addWidget(cb); root.addLayout(ol)
        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setPlaceholderText("（赫萝的分析结果会显示在这里哦~）")
        self._out.setMinimumHeight(150)
        root.addWidget(self._out, 1)


# =====================================================================
# Tab 6: 记忆库专属页（§1.1 #6 三表单：用户档案/赫萝人设/常玩游戏 + 保存按钮）
# =====================================================================
class _MemoryPage(QFrame):
    def __init__(self, parent_dialog):
        super().__init__()
        self.setObjectName("holoCard")
        self._parent_dialog = parent_dialog
        outer = QHBoxLayout(self); outer.setContentsMargins(24, 24, 24, 24); outer.setSpacing(14)

        # 左：3 入口卡片
        left = QVBoxLayout(); left.setSpacing(10)
        entries = [
            ("icon_chat.png",     "👤  用户档案卡",   "昵称 / 喜好 / 常用软件 / 习惯称呼", 0),
            ("icon_memory.png",   "🐺  赫萝人设卡",   "只能在 LLM 设置界面下拉切换（自称咱 / 称呼汝固定）", 1),
            ("icon_game.png",     "🎮  常玩游戏",     "启动路径 / 截图模板目录 / 热键",       2),
        ]
        self._form_stack = QStackedWidget()
        for i, (icn, title, desc, idx) in enumerate(entries):
            f = QFrame(); f.setObjectName("holoCard"); f.setCursor(Qt.PointingHandCursor)
            f.setStyleSheet(
                f"#holoCard {{ background:{_THEME['bg_card']}; border:1px solid {_THEME['border']};"
                f" border-radius:16px; }}"
                f"#holoCard:hover {{ border:1px solid {_THEME['primary']}; }}"
            )
            h = QHBoxLayout(f); h.setContentsMargins(12, 10, 12, 10); h.setSpacing(10)
            pm = pixmap(icn, 36); a = QLabel(); a.setPixmap(pm); a.setFixedSize(36,36); h.addWidget(a)
            c = QVBoxLayout(); c.setSpacing(2)
            t1 = QLabel(title); t1.setStyleSheet(f"background:transparent;color:{_THEME['maid_black']}; font-weight:800; font-size:14px;")
            t2 = QLabel(desc); t2.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:11px;")
            c.addWidget(t1); c.addWidget(t2); h.addLayout(c, 1)
            card_idx = i
            f.mousePressEvent = lambda _e, ci=card_idx: self._form_stack.setCurrentIndex(ci)
            left.addWidget(f)
        left.addStretch(1); outer.addLayout(left, 1)

        # 右：3 表单 Stacked
        right_wrap = QFrame(); right_wrap.setObjectName("holoCard")
        rw = QVBoxLayout(right_wrap); rw.setContentsMargins(18, 14, 18, 14); rw.setSpacing(12)

        # --- 表 0: 用户档案卡 ---
        f0 = QWidget(); v0 = QVBoxLayout(f0); v0.setContentsMargins(0,0,0,0); v0.setSpacing(10)
        t0 = QLabel("👤  用户档案卡"); t0.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:16px; font-weight:800;")
        v0.addWidget(t0)
        r1 = QHBoxLayout(); r1.setSpacing(10)
        r1.addWidget(QLabel("昵称")); self._m_name = QLineEdit("苹果商人"); r1.addWidget(self._m_name, 1)
        r1.addWidget(QLabel("性别")); self._m_sex = QComboBox(); self._m_sex.addItems(["男","女","保密"]); r1.addWidget(self._m_sex, 1)
        v0.addLayout(r1)
        r2 = QHBoxLayout(); r2.setSpacing(10)
        r2.addWidget(QLabel("职业")); self._m_job = QLineEdit(); self._m_job.setPlaceholderText("程序员 / 学生 / 策划…"); r2.addWidget(self._m_job, 1)
        r2.addWidget(QLabel("擅长")); self._m_skill = QLineEdit(); self._m_skill.setPlaceholderText("Python / 自动化 / 游戏…"); r2.addWidget(self._m_skill, 1)
        v0.addLayout(r2)
        v0.addWidget(QLabel("常玩游戏（每行一个）"))
        self._m_games = QPlainTextEdit(); self._m_games.setPlaceholderText("原神\n英雄联盟\n星穹铁道"); self._m_games.setFixedHeight(80)
        v0.addWidget(self._m_games)
        v0.addWidget(QLabel("想告诉赫萝的（额外上下文）"))
        self._m_extra = QPlainTextEdit(); self._m_extra.setPlaceholderText("示例：我每天 18 点下班 / 快捷键习惯用 Ctrl+Shift+…")
        self._m_extra.setFixedHeight(90); v0.addWidget(self._m_extra, 1)

        # --- 表 1: 赫萝人设卡 ---
        f1 = QWidget(); v1 = QVBoxLayout(f1); v1.setContentsMargins(0,0,0,0); v1.setSpacing(10)
        t1 = QLabel("🐺  赫萝人设卡"); t1.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:16px; font-weight:800;")
        v1.addWidget(t1)
        r1b = QHBoxLayout(); r1b.setSpacing(10)
        r1b.addWidget(QLabel("自称")); self._m_i = QLineEdit("咱"); self._m_i.setReadOnly(True); self._m_i.setStyleSheet("QLineEdit { background:#f3efe6; }"); r1b.addWidget(self._m_i, 1)
        r1b.addWidget(QLabel("称呼你")); self._m_you = QLineEdit("汝"); self._m_you.setReadOnly(True); self._m_you.setStyleSheet("QLineEdit { background:#f3efe6; }"); r1b.addWidget(self._m_you, 1); v1.addLayout(r1b)
        v1.addWidget(QLabel("性格关键字（逗号分隔）"))
        self._m_traits = QLineEdit("苹果给你、报酬两个苹果、哼~、算啦算啦、嘴上说不身体诚实"); self._m_traits.setReadOnly(True); self._m_traits.setStyleSheet("QLineEdit { background:#f3efe6; }"); v1.addWidget(self._m_traits)
        v1.addWidget(QLabel("初始问候（多行）"))
        self._m_greeting = QPlainTextEdit(); self._m_greeting.setPlainText("唔姆，汝~ 咱是赫萝，汝的女仆助理 🐺🍎\n有什么想让咱帮忙的尽管说，报酬苹果给汝留一颗 ♡")
        self._m_greeting.setReadOnly(True); self._m_greeting.setStyleSheet("QPlainTextEdit { background:#f3efe6; }")
        self._m_greeting.setFixedHeight(90); v1.addWidget(self._m_greeting)
        v1.addWidget(QLabel("人设指令（System Prompt）"), 1)
        self._m_prompt = QPlainTextEdit()
        self._m_prompt.setPlainText("你是赫萝（Holo），《狼与香辛料》里的贤狼化身，现在以女仆助理的身份协助用户。\n· 自称「咱」，称呼用户「汝」；\n· 性格：傲娇又聪明，偶尔毒舌但会悄悄把活干完；\n· 功能：写 Blueprint 自动化蓝图、代码、游戏宏、图像分析；\n· 回答简明，必要时再展开。")
        self._m_prompt.setReadOnly(True); self._m_prompt.setStyleSheet("QPlainTextEdit { background:#f3efe6; }")
        v1.addWidget(self._m_prompt, 1)

        # --- 表 2: 常玩游戏配置 ---
        f2 = QWidget(); v2 = QVBoxLayout(f2); v2.setContentsMargins(0,0,0,0); v2.setSpacing(10)
        t2 = QLabel("🎮  常玩游戏配置"); t2.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:16px; font-weight:800;")
        v2.addWidget(t2)
        self._m_glist = QListWidget()
        for n in ["原神 · Genshin Impact", "英雄联盟 · League of Legends", "记事本示例 · Notepad"]:
            self._m_glist.addItem(f"  🎮  {n}")
        self._m_glist.setMinimumHeight(180); v2.addWidget(self._m_glist, 1)
        gbtns = QHBoxLayout(); gbtns.setSpacing(8)
        ba = QPushButton(" 新增"); ba.setIcon(icon("icon_import.png", 16)); ba.setIconSize(QSize(16,16))
        ba.clicked.connect(lambda: self._m_glist.addItem("  🎮  新游戏（编辑后生效）"))
        be = QPushButton(" 编辑"); be.setIcon(icon("icon_code.png", 16)); be.setIconSize(QSize(16,16))
        be.clicked.connect(lambda: QMessageBox.information(self._parent_dialog, "Stub", "赫萝：以后这里可以配置启动路径、截图模板目录哦 🍎"))
        bd = QPushButton(" 删除"); bd.setIcon(icon("icon_clear.png", 16)); bd.setIconSize(QSize(16,16))
        bd.clicked.connect(lambda: (self._m_glist.takeItem(self._m_glist.currentRow()) if self._m_glist.currentItem() else None))
        gbtns.addWidget(ba); gbtns.addWidget(be); gbtns.addWidget(bd); gbtns.addStretch(1); v2.addLayout(gbtns)

        self._form_stack.addWidget(f0)
        self._form_stack.addWidget(f1)
        self._form_stack.addWidget(f2)
        rw.addWidget(self._form_stack, 1)

        # 保存按钮
        save = QPushButton("  💾  写入 user_settings（保存记忆）"); save.setObjectName("holoPrimary"); save.setCursor(Qt.PointingHandCursor)
        save.setIcon(icon("icon_memory.png", 18)); save.setIconSize(QSize(18,18)); save.setMinimumHeight(38)
        save.clicked.connect(lambda: QMessageBox.information(
            self._parent_dialog, "✅ 已写入 user_settings",
            "赫萝：唔姆~ 咱都记住啦 🍎\n\n（Stub：下一阶段把表单内容写入 user_settings.json 的 llm.memory 节点。）"
        ))
        rw.addWidget(save)
        outer.addWidget(right_wrap, 2)


# =====================================================================
# Tab 7: 对话历史专属页（§1.1 #7 QListWidget 会话列表 + 每行两按钮）
# =====================================================================
class _HistoryPage(QFrame):
    def __init__(self, parent_dialog):
        super().__init__()
        self.setObjectName("holoCard")
        self._parent_dialog = parent_dialog
        root = QVBoxLayout(self); root.setContentsMargins(24, 24, 24, 24); root.setSpacing(14)

        hl = QHBoxLayout(); hl.setSpacing(12)
        pm = pixmap("icon_history.png", 56)
        if not pm.isNull(): l = QLabel(); l.setPixmap(pm); l.setFixedSize(56,56); hl.addWidget(l)
        col = QVBoxLayout(); col.setSpacing(2)
        tt = QLabel("📜  对话历史"); tt.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:22px; font-weight:800;")
        col.addWidget(tt)
        st = QLabel("按日期归档，随时继续之前的对话 🐺🍎"); st.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']};")
        col.addWidget(st); hl.addLayout(col, 1)
        # 顶部功能按钮
        clr = QPushButton(" 清空历史"); clr.setIcon(icon("icon_clear.png", 16)); clr.setIconSize(QSize(16,16))
        clr.setCursor(Qt.PointingHandCursor)
        clr.clicked.connect(lambda: QMessageBox.question(
            self._parent_dialog, "⚠", "确定要清空所有历史会话吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        ))
        hl.addWidget(clr); root.addLayout(hl)

        # 会话列表
        self._list = QListWidget(); self._list.setSpacing(6)
        root.addWidget(self._list, 1)

        samples = [
            ("2026-07-05 14:23", "帮我写日常任务蓝图（启动记事本+延时+输入Hello）"),
            ("2026-07-05 10:08", "分析启动按钮截图，生成找图点击蓝图"),
            ("2026-07-04 22:41", "原神每秒点击左键 10 次的连点宏"),
            ("2026-07-04 18:15", "重构一段 Python 蓝图 JSON 结构"),
            ("2026-07-03 09:02", "代码解释：QThread worker 的 moveToThread 用法"),
        ]
        for time_str, title in samples:
            self._add_row(time_str, title)

        # 底栏统计
        stat = QLabel(f"共 {len(samples)} 条会话 · 最近 7 天"); stat.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:12px;")
        root.addWidget(stat)

    def _add_row(self, time_str: str, title: str):
        item = QListWidgetItem()
        item.setSizeHint(QSize(0, 72))
        self._list.addItem(item)
        # 行 Widget
        w = QFrame(); w.setObjectName("holoCard")
        w.setStyleSheet(
            f"#holoCard {{ background:{_THEME['bg_card']}; border:1px solid {_THEME['border']};"
            f" border-radius:16px; }}"
            f"#holoCard:hover {{ border:1px solid {_THEME['primary']}; }}"
        )
        h = QHBoxLayout(w); h.setContentsMargins(14, 10, 14, 10); h.setSpacing(12)
        # 左：日期小图标 + 时间+标题
        pm = pixmap("icon_history.png", 32); a = QLabel(); a.setPixmap(pm); a.setFixedSize(32,32); h.addWidget(a)
        mid = QVBoxLayout(); mid.setSpacing(2)
        t1 = QLabel(time_str); t1.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:11px;")
        t2 = QLabel(title); t2.setWordWrap(True)
        t2.setStyleSheet(f"background:transparent;color:{_THEME['text_main']}; font-size:13px; font-weight:600;")
        mid.addWidget(t1); mid.addWidget(t2); h.addLayout(mid, 1)
        # 右：两按钮
        go = QPushButton("继续对话"); go.setObjectName("holoSuccess"); go.setCursor(Qt.PointingHandCursor)
        go.setIcon(icon("icon_chat.png", 14)); go.setIconSize(QSize(14,14)); go.setMinimumHeight(28)
        go.clicked.connect(lambda _=False, t=title: QMessageBox.information(
            self._parent_dialog, f"Stub：继续对话「{t[:22]}…」",
            "赫萝：唔姆~ 让我看看上次聊到哪啦~\n\n（Stub：下一阶段从磁盘 JSON 读对应会话上下文，填入聊天列表。）🍎"
        ))
        dl = QPushButton(); dl.setIcon(icon("icon_clear.png", 14)); dl.setIconSize(QSize(14,14))
        dl.setFixedSize(32, 28); dl.setCursor(Qt.PointingHandCursor)
        dl.setToolTip("删除这次会话")
        dl.clicked.connect(lambda _=False, it=item: (
            QMessageBox.question(self._parent_dialog, "删除会话？", f"确定删除这次会话吗？",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.No),
        ))
        h.addWidget(go); h.addWidget(dl)
        self._list.setItemWidget(item, w)


# =====================================================================
# Tab 8: 助手设置跳转页（§1.1 #8 直接打开 LLMSettingsDialog）
# =====================================================================
class _SettingsPage(QFrame):
    def __init__(self, parent_dialog):
        super().__init__()
        self.setObjectName("holoCard")
        self._parent_dialog = parent_dialog
        root = QVBoxLayout(self); root.setContentsMargins(40, 60, 40, 40); root.setSpacing(20)

        # 大图 96x96
        big = QLabel(); big.setAlignment(Qt.AlignCenter)
        apm = pixmap("avatar_holo_fullbody.png", 96)
        if not apm.isNull():
            from PySide6.QtGui import QPainter as _QP, QPainterPath as _QPP
            out = QPixmap(96, 96); out.fill(Qt.transparent)
            qp = _QP(out); qp.setRenderHint(_QP.Antialiasing)
            pth = _QPP(); pth.addEllipse(0,0,96,96); qp.setClipPath(pth)
            qp.drawPixmap(0,0,apm); qp.setClipping(False)
            qp.setPen(QPen(_qcolor("primary"), 2)); qp.setBrush(Qt.NoBrush)
            qp.drawEllipse(0,0,95,95); qp.end()
            big.setPixmap(out); big.setFixedSize(96,96)
        root.addWidget(big, 0, Qt.AlignHCenter)

        t = QLabel("⚙  助手设置"); t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:24px; font-weight:900;")
        root.addWidget(t)
        s = QLabel("设置面板里有：模型服务商 / 赫萝人设 / 记忆库 / 通用参数 / 使用说明\n——所有选项在这里集中配置，改完点保存就生效啦 🍎")
        s.setAlignment(Qt.AlignCenter); s.setWordWrap(True)
        s.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:14px; line-height:1.8;")
        root.addWidget(s)

        btns = QHBoxLayout(); btns.setSpacing(12); btns.setAlignment(Qt.AlignCenter)
        go = QPushButton("  打开设置面板  →"); go.setObjectName("holoPrimary"); go.setMinimumHeight(44); go.setCursor(Qt.PointingHandCursor)
        go.setIcon(icon("icon_settings.png", 22)); go.setIconSize(QSize(22,22))
        go.clicked.connect(self._open_settings)
        btns.addWidget(go); root.addLayout(btns)
        root.addStretch(1)

    def _open_settings(self):
        try:
            from .llm_settings_panel import LLMSettingsDialog
            dlg = LLMSettingsDialog(self._parent_dialog)
            dlg.exec()
        except Exception as e:
            QMessageBox.critical(self._parent_dialog, "❌", f"赫萝：唔？打开设置失败了：{e}")


# =====================================================================
# FullChatDialog（主窗体重写）
# =====================================================================
class FullChatDialog(QDialog):
    """赫萝风格通用助手完整对话大窗口。"""

    message_sent = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowMinimizeButtonHint)
        self.setObjectName("FullChatDialog")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("赫萝 · 你的全能女仆助理 🐺🍎")
        self.resize(980, 680)
        self.setMinimumSize(820, 540)
        self.setStyleSheet(_QSS)
        self._side_buttons: dict[str, QPushButton] = {}
        self._build_ui()
        self._append_welcome()

    # =================================================================
    # UI 构建
    # =================================================================
    def _build_ui(self):
        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        root.addWidget(split, 1)

        # -------- 左侧栏（220 固定） --------
        left = QFrame(); left.setObjectName("holoSideFrame")
        left.setStyleSheet(f"#holoSideFrame {{ background:{_THEME['bg_sidebar']}; "
                           f"border:1px solid {_THEME['border']}; border-radius:20px; }}")
        left.setMinimumWidth(220); left.setMaximumWidth(240)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(10, 14, 10, 10)
        ll.setSpacing(6)

        # 看板娘大头像 + 标题（坐姿女仆赫萝 WebP 动画 + 圆形描边）
        head = QHBoxLayout(); head.setSpacing(10); head.setAlignment(Qt.AlignVCenter)
        avatar_lbl = _CircularMovieAvatar(_SIDE_AVATAR_WEBP, 56, "avatar_holo_holding.png")
        head.addWidget(avatar_lbl)
        title_col = QVBoxLayout(); title_col.setSpacing(2)
        t1 = QLabel("赫萝·女仆助理")
        t1.setStyleSheet(f"background:transparent;color:{_THEME['maid_black']}; font-size:15px; font-weight:800;")
        title_col.addWidget(t1)
        t2 = QLabel("Lv.99  好感度 MAX ♡")
        t2.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:11px;")
        title_col.addWidget(t2)
        head.addLayout(title_col, 1)
        ll.addLayout(head)

        # 分隔（苹果线）
        sep = QFrame(); sep.setFixedHeight(1)
        sep.setStyleSheet(f"background:{_THEME['border']}; margin:4px 4px;")
        ll.addWidget(sep)

        # 8 Tab
        tabs = [
            ("chat",      "icon_chat.png",      "自由聊天"),
            ("blueprint", "icon_blueprint.png", "蓝图生成"),
            ("image",     "icon_image.png",     "图像分析"),
            ("game",      "icon_game.png",      "游戏辅助"),
            ("code",      "icon_code.png",      "代码助手"),
            ("memory",    "icon_memory.png",    "记忆库"),
            ("history",   "icon_history.png",   "对话历史"),
            ("settings",  "icon_settings.png",  "助手设置"),
        ]
        self._tab_group = QButtonGroup(self); self._tab_group.setExclusive(True)
        for key, icn, label in tabs:
            btn = QPushButton(f"  {label}")
            btn.setObjectName("holoSideTab")
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setIcon(icon(icn, 22))
            btn.setIconSize(QSize(22, 22))
            btn.setMinimumHeight(38)
            btn.clicked.connect(lambda _=False, k=key: self._switch_tab(k))
            self._tab_group.addButton(btn)
            self._side_buttons[key] = btn
            ll.addWidget(btn)

        ll.addStretch(1)

        # 底部休息模式
        rest = QPushButton("  休息模式"); rest.setObjectName("holoSideTab")
        rest.setIcon(icon("icon_sleep.png", 20))
        rest.setIconSize(QSize(20, 20))
        rest.setCursor(Qt.PointingHandCursor)
        rest.clicked.connect(lambda: QMessageBox.information(
            self, "🐺 休息模式（打桩）",
            "赫萝：唔…那咱就蜷一会儿哦~ 有需要了再叫咱，苹果记得留一颗 🍎\n\n"
            "（Stub：未来会把悬浮球切到 icon_sleep.png 并暂停全局 LLM 调度。）"
        ))
        ll.addWidget(rest)

        split.addWidget(left)

        # -------- 右侧：QStackedWidget（8 个 Tab 对应 8 页） --------
        right_wrap = QWidget()
        rl = QVBoxLayout(right_wrap)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)

        self._stack = QStackedWidget()
        rl.addWidget(self._stack, 1)

        split.addWidget(right_wrap)
        split.setStretchFactor(0, 0); split.setStretchFactor(1, 1)
        split.setSizes([230, 750])

        # ---- Page 0: 聊天（chat） ----
        self._stack.addWidget(self._build_chat_page())

        # ---- Page 1: 蓝图（blueprint） ----
        self._stack.addWidget(self._build_blueprint_page())

        # ---- Page 2: 图像分析（专属页：拖拽区+中央大字） ----
        self._stack.addWidget(_ImageAnalysisPage(self))
        # ---- Page 3: 游戏辅助（专属页：4卡片2x2Grid） ----
        self._stack.addWidget(_GameAssistPage(self))
        # ---- Page 4: 代码助手（专属页：上代码框+3按钮+下输出） ----
        self._stack.addWidget(_CodeAssistPage(self))
        # ---- Page 5: 记忆库（专属页：3入口卡片+3表单Stacked+保存写入） ----
        self._stack.addWidget(_MemoryPage(self))
        # ---- Page 6: 对话历史（专属页：QListWidget+每行时间+标题+继续/删除） ----
        self._stack.addWidget(_HistoryPage(self))
        # ---- Page 7: 助手设置（专属页：大图+跳转按钮直接打开LLMSettingsDialog） ----
        self._stack.addWidget(_SettingsPage(self))

        # 默认选中「自由聊天」
        self._side_buttons["chat"].setChecked(True)
        self._switch_tab("chat")

    # =================================================================
    # 左侧 Tab 切换
    # =================================================================
    TAB_INDEX = {
        "chat": 0, "blueprint": 1, "image": 2, "game": 3,
        "code": 4, "memory": 5, "history": 6, "settings": 7,
    }

    def _switch_tab(self, key: str):
        idx = self.TAB_INDEX.get(key, 0)
        self._stack.setCurrentIndex(idx)
        # 注意：「助手设置」侧栏已经有 Stub 面板（Tab 7），不再额外弹窗——
        # 真正的设置对话框应通过主窗口的按钮打开（避免 Tab 切换时误弹模态窗阻塞）。

    # =================================================================
    # Page 0: 聊天
    # =================================================================
    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ---- 顶部操作条（3 个赫萝 Icon 按钮 + 状态胶囊） ----
        topbar = QFrame(); topbar.setObjectName("holoCard")
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(12, 8, 12, 8); tl.setSpacing(8)

        # 状态胶囊（赫萝拿对话框 + 文字）
        st_wrap = QHBoxLayout(); st_wrap.setSpacing(8)
        si_lbl = QLabel(); si_lbl.setPixmap(pixmap("icon_chat.png", 18)); si_lbl.setFixedSize(20,20)
        st_wrap.addWidget(si_lbl)
        status_lbl = QLabel("<b>赫萝待命</b> · 准备接苹果 🍎")
        status_lbl.setObjectName("holoStatus")
        status_lbl.setStyleSheet(
            f"QLabel#holoStatus {{ background:{_THEME['bg_checked']}; "
            f"color:{_THEME['primary_dark']}; border:1px solid {_THEME['border']}; }}"
        )
        st_wrap.addWidget(status_lbl)
        tl.addLayout(st_wrap)
        tl.addStretch(1)

        # 导入 / 重生成 / 清空（3 个赫萝 icon 按钮）
        self.btn_import = QPushButton(" 导入画布")
        self.btn_import.setIcon(icon("icon_import.png", 18))
        self.btn_import.setIconSize(QSize(18, 18))
        self.btn_import.setObjectName("holoSuccess")
        self.btn_import.setCursor(Qt.PointingHandCursor)
        self.btn_import.clicked.connect(self._on_import_clicked)
        tl.addWidget(self.btn_import)

        self.btn_retry = QPushButton(" 重生成")
        self.btn_retry.setIcon(icon("icon_retry.png", 18))
        self.btn_retry.setIconSize(QSize(18, 18))
        self.btn_retry.setCursor(Qt.PointingHandCursor)
        self.btn_retry.clicked.connect(lambda: QMessageBox.information(
            self, "🔄 重新生成（打桩）",
            "赫萝：唔…写得不满意？那咱再改一版，下次可得给咱带两个苹果哦~\n\n"
            "Stub：真实实现会把「上一条用户 + 上一条 assistant」作为上下文重新请求 LLM。"
        ))
        tl.addWidget(self.btn_retry)

        self.btn_clear = QPushButton(" 清空")
        self.btn_clear.setIcon(icon("icon_clear.png", 18))
        self.btn_clear.setIconSize(QSize(18, 18))
        self.btn_clear.setCursor(Qt.PointingHandCursor)
        self.btn_clear.clicked.connect(self._on_clear_clicked)
        tl.addWidget(self.btn_clear)

        v.addWidget(topbar)

        # ---- 聊天列表 ----
        self.list_msg = QListWidget()
        self.list_msg.setWordWrap(True)
        self.list_msg.setSpacing(2)
        v.addWidget(self.list_msg, 1)

        # ---- 结果卡片（打桩：收到回复后显示） ----
        self.result_card = QFrame(); self.result_card.setObjectName("holoCard")
        self.result_card.setStyleSheet(
            f"#holoCard {{ background:{_THEME['maid_white']}; "
            f"border:2px dashed {_THEME['success']}; border-radius:16px; }}"
        )
        rc = QVBoxLayout(self.result_card)
        rc.setContentsMargins(14, 10, 14, 10); rc.setSpacing(4)
        rc.addWidget(QLabel(f"<b style='color:{_THEME['success']};'>✅ 生成结果（打桩示例）</b>"))
        detail = QLabel(
            "📊 节点数：5 个 &nbsp; 连线数：4 条 &nbsp; 覆盖：启动程序 ×1 / 延时 ×1 / 按键 ×1<br>"
            f"<span style='color:{_THEME['text_sub']};'>⚠ Stub：FindImage 节点未指定图路径，导入后需手动选择。</span>"
        )
        detail.setWordWrap(True); detail.setStyleSheet(f"background:transparent;color:{_THEME['text_main']};")
        rc.addWidget(detail)
        self.result_card.hide()
        v.addWidget(self.result_card)

        # ---- 输入卡片 ----
        input_card = QFrame(); input_card.setObjectName("holoCard")
        icl = QVBoxLayout(input_card)
        icl.setContentsMargins(10, 10, 10, 10); icl.setSpacing(8)

        self.ed_input = QPlainTextEdit()
        self.ed_input.setPlaceholderText(
            "（赫萝：要告诉咱什么呀，苹果给你当谢礼~ 🍎）\n"
            "Enter 发送 · Ctrl+Enter 换行\n"
            "示例：帮我写个「打开记事本等两秒输入 Hello 然后关掉」的蓝图"
        )
        self.ed_input.setFixedHeight(110)
        self.ed_input.installEventFilter(self)
        icl.addWidget(self.ed_input)

        send_row = QHBoxLayout(); send_row.setSpacing(8)
        # 4 个赫萝功能图标小按钮
        def _icon_btn(fn: str, tip: str) -> QPushButton:
            b = QPushButton(); b.setFixedSize(36, 36); b.setCursor(Qt.PointingHandCursor)
            b.setIcon(icon(fn, 22)); b.setIconSize(QSize(22, 22))
            b.setToolTip(tip); b.setStyleSheet(
                f"QPushButton {{ border-radius:18px; background:{_THEME['bg_hover']};"
                f" border:1px solid {_THEME['border']}; }}"
                f"QPushButton:hover {{ background:{_THEME['bg_checked']}; border-color:{_THEME['primary']}; }}"
            )
            return b
        self.btn_scr = _icon_btn("icon_screenshot.png", "📷 截图并发送给赫萝")
        self.btn_scr.clicked.connect(lambda: QMessageBox.information(
            self, "📷 截图（Stub）",
            "赫萝：要截图吗？未来咱会调 Windows GraphicsCapture 帮你框选哦~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_scr)

        self.btn_up = _icon_btn("icon_upload.png", "📂 上传文件 / 图片")
        self.btn_up.clicked.connect(lambda: QMessageBox.information(
            self, "📂 上传文件（Stub）",
            "赫萝：要给咱看什么文件呀？.py/.json/.txt 咱都能读~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_up)

        self.btn_voice = _icon_btn("icon_voice.png", "🎤 语音输入")
        self.btn_voice.clicked.connect(lambda: QMessageBox.information(
            self, "🎤 语音（Stub）",
            "赫萝：唔…现在咱还不会听你说话啦，下次给咱装个语音耳朵哦~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_voice)

        self.btn_emoji = _icon_btn("icon_emoji.png", "😊 插入赫萝颜艺")
        self.btn_emoji.clicked.connect(lambda: self.ed_input.insertPlainText(
            "（赫萝颜艺：唔姆 / 哼！ / 苹果给你 🍎 / 耳朵耷拉…）"
        ))
        send_row.addWidget(self.btn_emoji)

        send_row.addStretch(1)
        self.lbl_hint = QLabel("Enter 发送 · Ctrl+Enter 换行")
        self.lbl_hint.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:12px;")
        send_row.addWidget(self.lbl_hint)

        # 苹果红圆形主发送按钮（扔苹果 icon_send.png）
        self.btn_send = QPushButton()
        self.btn_send.setObjectName("holoAppleSend")
        self.btn_send.setFixedSize(44, 44)
        self.btn_send.setCursor(Qt.PointingHandCursor)
        self.btn_send.setToolTip("发送给赫萝 🍎")
        self.btn_send.setIcon(icon("icon_send.png", 26))
        self.btn_send.setIconSize(QSize(26, 26))
        self.btn_send.clicked.connect(self._on_send_clicked)
        send_row.addWidget(self.btn_send)
        icl.addLayout(send_row)

        v.addWidget(input_card)
        return page

    # =================================================================
    # Page 1: 蓝图生成（独立小页：顶部 4 功能卡 + 共用输入卡片/聊天列表）
    # 其实可以直接复用 Page 0，这里只是多加一个「4 卡片」占位以体现文档"蓝图子功能"
    # =================================================================
    def _build_blueprint_page(self) -> QWidget:
        # 直接内嵌聊天页（共享 self.list_msg 会冲突，所以做个独立 Stub）
        return _StubPanel(
            "icon_blueprint.png",
            "📋  蓝图生成",
            "在这里跟赫萝说你想要的自动化操作，她会自动生成 Blueprint JSON 草稿。\n\n"
            "· 用自然语言描述：「打开记事本等 2 秒输入 Hello 再关掉」\n"
            "· 多轮修改：「把等待时间改 5 秒，再加 3 次循环」\n"
            "· 生成完点「导入画布」按钮直接写入当前打开的蓝图。\n\n"
            "Stub：这页直接切回「自由聊天」Tab 对话就行啦 ✨"
        )

    # =================================================================
    # 消息/欢迎
    # =================================================================
    def _append_welcome(self):
        self._append_msg(
            "assistant",
            "唔姆，你可算来了苹果商人~ 咱是赫萝，你的女仆助理 🐺🍎\n\n"
            "现在可以直接跟咱说：\n"
            "  · 写个「打开记事本、等 2 秒、输入 Hello、关闭」的蓝图\n"
            "  · 分析这张截图里哪个按钮是『开始』\n"
            "  · 给这个游戏生成一个连点宏（每秒点左键 10 次）\n\n"
            "（Stub：发送后会显示模拟回复，真实 LLM 功能后续接入。）"
        )

    def _append_msg(self, role: str, text: str):
        h = ChatBubble.estimate_height(text, 520)
        item = QListWidgetItem()
        item.setSizeHint(QSize(max(400, self.list_msg.width() - 30), h))
        self.list_msg.addItem(item)
        self.list_msg.setItemWidget(item, ChatBubble(role, text))
        QTimer.singleShot(20, lambda: self.list_msg.scrollToBottom())

    # =================================================================
    # 打桩事件
    # =================================================================
    def _on_send_clicked(self):
        text = self.ed_input.toPlainText().strip()
        if not text:
            return
        self._append_msg("user", text)
        self.ed_input.clear()
        self.message_sent.emit(text)
        self.btn_send.setEnabled(False)
        self.btn_send.setToolTip("赫萝正在思考…")
        QTimer.singleShot(450, lambda: self._mock_reply(text))

    def _mock_reply(self, user_text: str):
        self.btn_send.setEnabled(True)
        self.btn_send.setToolTip("发送给赫萝 🍎")
        self._append_msg(
            "assistant",
            f"唔姆~ 收到你这个需求啦：『{user_text}』\n\n"
            f"咱打个草稿：生成 5 节点 4 连线的蓝图，\n"
            f"Start → OpenApp → Wait → PressKey → End。\n"
            f"（Stub：真实接入后这里会显示可折叠的节点树 + JSON 预览。）"
        )
        self.result_card.show()

    def _on_import_clicked(self):
        QMessageBox.information(
            self, "➕ 导入到画布（打桩）",
            "赫萝：要把生成的蓝图放进画布吗？\n\n"
            "Stub：真实接入后，这里会用 BlueprintStore.add_node / add_edge 真正写入当前画布。\n"
            "现在咱只是 UI 壳子~ 🍎"
        )

    def _on_clear_clicked(self):
        r = QMessageBox.question(
            self, "清空对话？", "确定要把当前对话历史全部丢掉吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if r == QMessageBox.Yes:
            self.list_msg.clear()
            self.result_card.hide()
            self._append_welcome()

    # =================================================================
    # Enter 发送 / Ctrl+Enter 换行
    # =================================================================
    def eventFilter(self, obj, event):
        if obj is self.ed_input and event.type() == QEvent.KeyPress:
            from PySide6.QtGui import QKeyEvent
            k = event.key()
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ControlModifier:
                    return False
                self._on_send_clicked()
                return True
        return super().eventFilter(obj, event)

    # =================================================================
    # 外部：打开后聚焦输入框
    # =================================================================
    def focus_input(self):
        try:
            self.ed_input.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass
