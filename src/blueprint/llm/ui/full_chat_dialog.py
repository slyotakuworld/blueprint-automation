# -*- coding: utf-8 -*-
"""完整对话大窗口（赫萝风格通用助手版 · 纯 UI Stub）。

结构：
  ┌──────────────────────────────────────────────────────────────┐
  │ [左 220px 侧栏 holoSideFrame]  │ [右 主区]                      │
  │ ┌─看板娘 64x64 avatar_holo ─┐ │ 顶部操作条（新建/历史会话2赫萝Icon）  │
  │ │ 赫萝·女仆助理 Lv.99 ♡     │ │ ───────────────────────────── │
  │ ├─────────────────────────┤ │ 聊天消息列表（ChatBubble 重写）│
  │ │ [icon_chat]    自由聊天  │ │ ───────────────────────────── │
  │ │ [icon_blueprint] 蓝图生成│ │ 蓝图输入区（含状态卡片）       │
  │ │ [icon_image]    图像分析│ │ ───────────────────────────── │
  │ │ [icon_game]    游戏辅助 │ │ 输入卡片：                    │
  │ │ [icon_code]    代码助手│ │  [📷][📂][🎤] [输入] [🍎发送圆]│
  │ │ [icon_history] 对话历史│ │                                │
  │ │ [icon_settings]助手设置│ │                                │
  │ ├─────────────────────────┤ │                                │
  │ │ [icon_sleep] 休息模式 ⏱│ │                                │
  │ └─────────────────────────┘ │                                │
  └──────────────────────────────────────────────────────────────┘

打桩保留：
  · 发送消息 → 自己气泡 + 模拟打桩回复气泡
  · 新建会话 / 历史会话 → QMessageBox
  · 7 Tab 除「自由聊天 / 蓝图生成」外，点击显示对应 Stub 面板（赫萝 Icon + 占位文字）

注：7 Tab = 自由聊天 / 蓝图生成 / 图像分析 / 游戏辅助 / 代码助手 / 对话历史 / 助手设置
   （记忆库 Tab 已移除，相关配置统一在 LLM 设置面板管理）
   （自由聊天 / 蓝图生成 / 代码助手 使用各自独立的系统提示词，互不干扰）
"""
from __future__ import annotations

import os
from typing import List, Optional

from PySide6.QtCore import QByteArray, QEvent, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
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
    QAbstractItemView,
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
    QMenu,
)

# ---- 主题 + 图标加载器 ----
from ._holo_theme import build_qss, get_theme
from ._icon_loader import circular_pixmap, icon, icon_path, pixmap

_THEME_NAME = "day"  # 固定奶茶白日主题
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
        self._holo_avatar_pm = circular_pixmap("avatar_holo_tsundere.png", 32)
        # 用于 sizeHint 计算的"假设分配宽度"（≈ paintEvent 里的 self.width()）。
        # 构造时按角色给合理默认，update_bubble_width 时会同步外部真实宽度。
        self._hint_width = 500 if role == "user" else 520
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        self.setMinimumHeight(68)
        # 水平：可扩展（填满气泡条）；垂直：Minimum——sizeHint 就是底线，不许布局往下压
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

    def set_hint_width(self, width: int):
        """同步外部实际分配给 self 的宽度上限，让 sizeHint 与 paintEvent 共用同一宽度基准。"""
        self._hint_width = max(220, int(width))
        self.updateGeometry()

    def sizeHint(self) -> QSize:
        font = QFont()
        font.setPixelSize(13)
        fm = QFontMetrics(font)
        # 跟 paintEvent 完全一致的几何推导（用 _hint_width 代替 self.width()）
        pad, avatar_size, gap, text_pad = 10, 36, 10, 14
        w = self._hint_width
        is_user = self._role == "user"
        if is_user:
            ax = w - pad - avatar_size
            bw_max = ax - pad - gap
            bw = max(180, min(bw_max, 500))
        else:
            ax = pad + 10
            bx = ax + avatar_size + gap
            bw_max = w - bx - pad
            bw = max(220, min(bw_max, 520))
        tw = bw - text_pad * 2
        WRAP = Qt.TextWordWrap | getattr(Qt, "TextWrapAnywhere", 0x08)
        rect = fm.boundingRect(0, 0, max(80, tw), 9999,
                                WRAP | Qt.AlignLeft | Qt.AlignTop, self._text or "")
        # 尺寸链：by(8) + bh(=rect.h + 48) + sh_off_y(5) + 安全(2) = rect.h + 63
        h = max(83, rect.height() + 63)
        return QSize(w, h)

    def minimumSizeHint(self) -> QSize:
        return QSize(220, 68)

    def paintEvent(self, _evt):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)
        w, widget_h = self.width(), self.height()
        pad = 10
        avatar_size = 36
        gap = 10
        is_user = self._role == "user"
        r = 22
        text_pad = 14

        # 字体 & 文本高度（共用）
        tf = QFont()
        tf.setPixelSize(13)
        tf.setFamily("Microsoft YaHei UI")
        fm = QFontMetrics(tf)
        pen_w = 1.0

        # 装饰元素最大占用（避免底边被裁）
        sh_off_y = 5   # 阴影向下偏移

        if is_user:
            # ===== 气泡（4 圆角胶囊，先算气泡框 bx/by/bw/bh 再让头像跟它居中） =====
            ax = w - pad - avatar_size
            bw_max = ax - pad - gap
            bw = max(180, min(bw_max, 500))
            bx = ax - gap - bw
            by = 8
            tw = bw - text_pad * 2
            WRAP = Qt.TextWordWrap | getattr(Qt, "TextWrapAnywhere", 0x08)
            trect = fm.boundingRect(0, 0, max(80, tw), 9999, WRAP, self._text or "")
            text_need_h = max(20, trect.height())
            bh = max(68, text_need_h + 48)
            # ===== 超长文本安全网：bh 绝对不能超出 widget 实际分配高度 =====
            bh_max = max(68, widget_h - by - sh_off_y - 2)
            if bh > bh_max:
                bh = bh_max
                text_need_h = max(20, bh - 48)

            # 头像：跟气泡矩形 by/bh 垂直居中（不再跟 widget_h）
            avatar_y = by + (bh - avatar_size) // 2
            if avatar_y < 4: avatar_y = 4
            # 柔光环（发色柔光）
            halo = QPainterPath()
            halo.addEllipse(ax - 2, avatar_y - 2, avatar_size + 4, avatar_size + 4)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 216, 154, 90))
            p.drawPath(halo)
            # 圆形头像底（发色渐变）
            user_circle = QPainterPath()
            user_circle.addEllipse(ax, avatar_y, avatar_size, avatar_size)
            ug = QLinearGradient(ax, avatar_y, ax, avatar_y + avatar_size)
            ug.setColorAt(0, _qcolor("bubble_user_0"))
            ug.setColorAt(1, _qcolor("bubble_user_1"))
            p.setBrush(QBrush(ug))
            p.drawPath(user_circle)
            p.setPen(QPen(_qcolor("text_on_primary")))
            f = QFont(); f.setPixelSize(18); f.setBold(True); p.setFont(f)
            p.drawText(ax, avatar_y, avatar_size, avatar_size, Qt.AlignCenter, "👤")
            p.setPen(Qt.NoPen)

            # 超柔和单层投影（无硬线）
            sh = QPainterPath()
            sh.addRoundedRect(bx + 1, by + sh_off_y, bw, bh, r + 1, r + 1)
            p.setBrush(QColor(201, 127, 53, 20))
            p.drawPath(sh)

            # 气泡本体：纯渐变填充（完全不画任何描边线条！彻底去线框）
            path = QPainterPath()
            path.addRoundedRect(bx, by, bw, bh, r, r)
            grad = QLinearGradient(bx, by, bx, by + bh)
            grad.setColorAt(0, _qcolor("bubble_user_0"))
            grad.setColorAt(1, _qcolor("bubble_user_1"))
            text_c = _qcolor("maid_black")
            p.setPen(Qt.NoPen)
            p.fillPath(path, QBrush(grad))

            # 内发光（柔和 1 圈柔光，仅填充无描边）
            glow_pen_c = QColor(255, 255, 255, 50)
            glow_path = QPainterPath()
            glow_path.addRoundedRect(bx + 3.5, by + 3.5, bw - 7, bh - 7, r - 3, r - 3)
            p.setBrush(glow_pen_c)
            p.drawPath(glow_path)

            # 顶部高光
            p.setBrush(QColor(255, 255, 255, 55))
            hl = QPainterPath(); hl.addRoundedRect(bx + 12, by + 5, bw - 24, 9, 6, 6)
            p.drawPath(hl)
        else:
            # ===== 赫萝气泡：先算气泡框，让头像跟 by/bh 气泡矩形垂直居中 =====
            ax = pad + 10       # x=20（pad=10）
            bx = ax + avatar_size + gap
            bw_max = w - bx - pad
            bw = max(220, min(bw_max, 520))
            by = 8
            tw = bw - text_pad * 2
            WRAP = Qt.TextWordWrap | getattr(Qt, "TextWrapAnywhere", 0x08)
            trect = fm.boundingRect(0, 0, max(80, tw), 9999, WRAP, self._text or "")
            text_need_h = max(20, trect.height())
            bh = max(68, text_need_h + 48)
            # ===== 超长文本安全网：bh 绝对不能超出 widget 实际分配高度 =====
            bh_max = max(68, widget_h - by - sh_off_y - 2)
            if bh > bh_max:
                bh = bh_max
                text_need_h = max(20, bh - 48)

            # 头像：跟气泡矩形 (by, bh) 垂直居中（不是跟 widget_h）
            avatar_y = by + (bh - avatar_size) // 2
            if avatar_y < 4: avatar_y = 4
            pm_ok = not self._holo_avatar_pm.isNull()
            if not pm_ok:
                fallback_circle = QPainterPath()
                fallback_circle.addEllipse(ax, avatar_y, avatar_size, avatar_size)
                p.setBrush(QColor(255, 222, 236, 200))
                p.drawPath(fallback_circle)
                p.setPen(QPen(_qcolor("maid_white")))
                ff = QFont(); ff.setPixelSize(18); p.setFont(ff)
                p.drawText(ax, avatar_y, avatar_size, avatar_size, Qt.AlignCenter, "🐺")
                p.setPen(Qt.NoPen)
            else:
                pw = self._holo_avatar_pm.size().width()
                if pw >= avatar_size:
                    p.drawPixmap(ax, avatar_y, avatar_size, avatar_size, self._holo_avatar_pm)
                else:
                    inner = (avatar_size - pw) // 2
                    thin_ring = QPainterPath()
                    thin_ring.addEllipse(ax + inner - 1, avatar_y + inner - 1, pw + 2, pw + 2)
                    p.setBrush(QColor(255, 200, 220, 55))
                    p.drawPath(thin_ring)
                    p.drawPixmap(ax + inner, avatar_y + inner, self._holo_avatar_pm)

            # 气泡形状：3 大圆角 + 棒棒糖尖角（指向头像）
            tip = 10
            path = QPainterPath()
            path.moveTo(bx + r + tip, by)
            path.lineTo(bx + bw - r, by)
            path.quadTo(bx + bw, by, bx + bw, by + r)
            path.lineTo(bx + bw, by + bh - r)
            path.quadTo(bx + bw, by + bh, bx + bw - r, by + bh)
            path.lineTo(bx + r + tip + 2, by + bh)
            path.quadTo(bx + tip, by + bh, bx + tip, by + bh - r - 2)
            mid_y = by + bh * 0.42
            path.lineTo(bx + tip, mid_y + 10)
            path.quadTo(bx + tip, mid_y + 14, bx + tip - 3, mid_y + 6)
            path.quadTo(bx - 6, mid_y, bx + tip - 3, mid_y - 6)
            path.quadTo(bx + tip, mid_y - 14, bx + tip, mid_y - 10)
            path.lineTo(bx + tip, by + r)
            path.quadTo(bx + tip, by, bx + r + tip, by)
            path.closeSubpath()

            # 3 色渐变填充（完全不画任何描边/线条！彻底去左侧线框）
            grad = QLinearGradient(bx, by, bx, by + bh)
            grad.setColorAt(0.0, QColor("#FFF9FC"))
            grad.setColorAt(0.55, QColor("#FFE8F3"))
            grad.setColorAt(1.0, QColor("#FFD5E7"))
            text_c = _qcolor("text_main")
            p.setPen(Qt.NoPen)
            p.fillPath(path, QBrush(grad))

            # 内发光：1 圈柔光（仅填充，无线）
            glow_c = QColor(255, 255, 255, 70)
            # 内发光路径简化：用 painterPath 跟气泡形状一致，再整体缩小 3.5px
            s = 3.5
            ir2, ibw2, ibh2 = r - 2, bw - s * 2, bh - s * 2
            iix, iiy = bx + s + tip, by + s
            iimid = iiy + ibh2 * 0.42
            glow = QPainterPath()
            glow.moveTo(iix + ir2, iiy)
            glow.lineTo(iix + ibw2 - ir2 - tip, iiy)
            glow.quadTo(iix + ibw2 - tip, iiy, iix + ibw2 - tip, iiy + ir2)
            glow.lineTo(iix + ibw2 - tip, iiy + ibh2 - ir2)
            glow.quadTo(iix + ibw2 - tip, iiy + ibh2, iix + ibw2 - ir2 - tip, iiy + ibh2)
            glow.lineTo(bx + s + tip + ir2 + 2, iiy + ibh2)
            glow.quadTo(bx + s + tip, iiy + ibh2, bx + s + tip, iiy + ibh2 - ir2 - 2)
            glow.lineTo(bx + s + tip, iimid + 8)
            glow.quadTo(bx + s + tip, iimid + 11, bx + s + tip - 2, iimid + 5)
            glow.quadTo(bx + s + tip - 5, iimid, bx + s + tip - 2, iimid - 5)
            glow.quadTo(bx + s + tip, iimid - 11, bx + s + tip, iimid - 8)
            glow.lineTo(bx + s + tip, iiy + ir2)
            glow.quadTo(bx + s + tip, iiy, bx + s + tip + ir2, iiy)
            glow.closeSubpath()
            p.setBrush(glow_c)
            p.drawPath(glow)

            # 左上奶油高光泡泡（保持）
            dot = QColor(255, 255, 255, 155)
            p.setBrush(dot)
            dot1 = QPainterPath(); dot1.addEllipse(bx + 20, by + 10, 11, 8)
            p.drawPath(dot1)
            dot2 = QPainterPath(); dot2.addEllipse(bx + 38, by + 15, 6, 5)
            p.drawPath(dot2)

            # 小云（保持，半透粉）
            cloud_c = QColor(255, 205, 225, 75)
            p.setBrush(cloud_c)
            cx, cy = bx + bw - 74, by - 6
            cloud = QPainterPath()
            cloud.addEllipse(cx,      cy + 3, 15, 10)
            cloud.addEllipse(cx + 10, cy,      14, 11)
            cloud.addEllipse(cx + 22, cy + 4,  12, 9)
            p.drawPath(cloud)

            # 右上角小爱心（保持）
            heart_c = QColor(255, 120, 165, 190)
            p.setBrush(heart_c)
            hx, hy = bx + bw - 22, by + 9
            hp = QPainterPath()
            hp.moveTo(hx, hy + 4)
            hp.quadTo(hx,      hy,     hx + 4,  hy)
            hp.quadTo(hx + 7,  hy,     hx + 7,  hy + 3)
            hp.quadTo(hx + 7,  hy,     hx + 10, hy)
            hp.quadTo(hx + 14, hy,     hx + 14, hy + 4)
            hp.quadTo(hx + 14, hy + 7, hx + 7,  hy + 11)
            hp.quadTo(hx,      hy + 7, hx,      hy + 4)
            hp.closeSubpath()
            p.drawPath(hp)

        # ===== 文本（共用）—— 短文本垂直居中，长文本顶边对齐，底部给 4px 呼吸空间 =====
        p.setFont(tf)
        p.setPen(text_c)
        tx = bx + text_pad
        avail_h = bh - text_pad * 2
        # 长文本判定：实际文本高度 ≥ 可用高度 - 4px（占满大部分空间）→ 顶边对齐，底部留呼吸空间
        if text_need_h >= avail_h - 4:
            ty = by + text_pad
            th = avail_h + 4          # 多给 4px，最后一行抗锯齿像素不被贴边裁
        else:
            ty = by + max(text_pad, (bh - text_need_h) // 2)
            th = text_need_h + 4
        if ty + th > by + bh - 2:
            th = max(16, (by + bh - 2) - ty)
        WRAP = Qt.TextWordWrap | getattr(Qt, "TextWrapAnywhere", 0x08)
        p.drawText(QRect(tx, ty, max(40, tw), max(16, th)), WRAP, self._text or "")
        p.end()


# =====================================================================
# ChatBubble（外框：气泡内容 + 赫萝气泡右下角 3 操作按钮）
# =====================================================================
class ChatBubble(QWidget):
    """单条消息气泡：
    role='user'(右尖角/金棕) / 'assistant'(左尖角/奶白+赫萝头像+操作按钮)
    mode='chat'(自由聊天：assistant 只显示复制) / 'blueprint'(蓝图生成：assistant 显示重写/导入/复制)
    """

    retry_clicked = Signal()
    import_clicked = Signal()
    copy_clicked = Signal()

    def __init__(self, role: str, text: str, mode: str = "chat", parent=None):
        super().__init__(parent)
        self._role = role
        self._mode = mode
        self._text = text
        self._content = _BubbleContent(role, text)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAutoFillBackground(False)
        # 水平：可扩展；垂直：Minimum——超长文本时按 sizeHint 给足高度，不许布局压缩
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)
        self.setMinimumHeight(74 if role == "user" else 104)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        outer.addWidget(self._content, 1)

        if role == "assistant":
            # ---- 赫萝气泡右下角操作按钮（根据 mode 决定显示几个）：
            #   - chat 模式：只显示「复制」（通用聊天场景只需要把回复复制走）
            #   - blueprint 模式：显示「重写 / 导入 / 复制」三个按钮
            bar_wrap = QHBoxLayout()
            bar_wrap.setContentsMargins(0, 0, 14, 0)
            bar_wrap.addStretch(1)
            _style = QApplication.style()
            from PySide6.QtWidgets import QStyle as _QStyle

            def _mini_btn(std_icon, label: str, tip: str) -> QPushButton:
                b = QPushButton(f" {label}")
                b.setMinimumHeight(28)
                b.setCursor(Qt.PointingHandCursor)
                b.setToolTip(tip)
                try:
                    b.setIcon(_style.standardIcon(std_icon))
                    b.setIconSize(QSize(14, 14))
                except Exception:
                    pass
                b.setStyleSheet(
                    f"QPushButton {{ padding:2px 8px; border-radius:6px; background:transparent;"
                    f" border:1px solid transparent; color:{_THEME['text_sub']}; font-weight:600; }}"
                    f"QPushButton:hover {{ background:{_THEME['bg_hover']};"
                    f" border:1px solid {_THEME['border']}; color:{_THEME['primary_dark']}; }}"
                    f"QPushButton:pressed {{ background:{_THEME['bg_checked']}; }}"
                )
                return b

            if self._mode == "blueprint":
                # 蓝图模式：3 个按钮
                self.btn_retry = _mini_btn(_QStyle.SP_BrowserReload, "重写", "🔄 重新生成（赫萝再写一版蓝图）")
                self.btn_retry.clicked.connect(self.retry_clicked.emit)
                self.btn_import = _mini_btn(_QStyle.SP_DialogOpenButton, "导入", "➕ 导入生成结果到画布")
                self.btn_import.clicked.connect(self.import_clicked.emit)
                self.btn_copy = _mini_btn(_QStyle.SP_DialogSaveButton, "复制", "📋 复制回复文本")
                self.btn_copy.clicked.connect(lambda: (
                    QApplication.clipboard().setText(self._text),
                    self._flash_copy_done(self.btn_copy),
                ))
                bar_wrap.addWidget(self.btn_retry)
                bar_wrap.addWidget(self.btn_import)
                bar_wrap.addWidget(self.btn_copy)
            else:
                # 聊天模式：只有复制
                self.btn_copy = _mini_btn(_QStyle.SP_DialogSaveButton, "复制", "📋 复制回复文本")
                self.btn_copy.clicked.connect(lambda: (
                    QApplication.clipboard().setText(self._text),
                    self._flash_copy_done(self.btn_copy),
                ))
                bar_wrap.addWidget(self.btn_copy)

            outer.addLayout(bar_wrap)
            self.setMinimumHeight(96)

    def sizeHint(self) -> QSize:
        csh = self._content.sizeHint()
        if self._role == "assistant":
            # 按钮条（最小 28，留 padding）+ VBox spacing(2)
            return QSize(csh.width(), csh.height() + 34 + 2)
        return csh

    def minimumSizeHint(self) -> QSize:
        cmsh = self._content.minimumSizeHint()
        if self._role == "assistant":
            return QSize(cmsh.width(), cmsh.height() + 34 + 2)
        return cmsh

    def _flash_copy_done(self, btn: QPushButton):
        original_tip = btn.toolTip()
        btn.setToolTip("✅ 已复制")
        QTimer.singleShot(1200, lambda: btn.setToolTip(original_tip))

    def update_bubble_width(self, new_bubble_width: int):
        """窗口/列表宽度变化时，重新调整气泡内部内容最大宽度并 updateGeometry。"""
        if new_bubble_width <= 60:
            return
        # 给 _content 的最大宽度（含头像+padding，保证 paint 区域完整）
        safe_max = max(260, new_bubble_width - 20)  # 左右各预留 10 padding
        self._content.setMaximumWidth(safe_max)
        # 同步 sizeHint 的"假设分配宽度"，让 sizeHint 和 paintEvent 共用同一宽度基准
        self._content.set_hint_width(safe_max)
        self._content.updateGeometry()
        self.updateGeometry()

    @staticmethod
    def estimate_height(text: str, bubble_width: int = 520, role: str = "user") -> int:
        font = QFont()
        font.setPixelSize(13)
        fm = QFontMetrics(font)
        # 按角色区分装饰占用与气泡边界（与 paintEvent/sizeHint 完全对齐）
        if role == "user":
            deco_w = 66          # pad*2 + avatar_size + gap = 10*2 + 36 + 10
            min_bw, max_bw = 180, 500
        else:
            deco_w = 76          # (pad+10) + avatar_size + gap + pad = 20 + 36 + 10 + 10
            min_bw, max_bw = 220, 520
        bw = max(min_bw, min(bubble_width - deco_w, max_bw))
        tw = bw - 28  # = text_pad(14) * 2
        WRAP = Qt.TextWordWrap | getattr(Qt, "TextWrapAnywhere", 0x08)
        rect = fm.boundingRect(
            0, 0, max(80, tw), 9999,
            WRAP | Qt.AlignLeft | Qt.AlignTop,
            text or "",
        )
        # 尺寸链与 _BubbleContent.sizeHint / paintEvent 完全一致：
        # by(8) + bh(=rect.h + 48) + sh_off_y(5) + 安全(2) = rect.h + 63
        bubble_body = max(83, rect.height() + 63)
        if role == "assistant":
            # ChatBubble 外层 VBox spacing(2) + 操作按钮条(34)
            return bubble_body + 34 + 2
        return bubble_body


# =====================================================================
# 7 Tab 的右侧主工作区 Stub 面板（除聊天/蓝图外的 5 个占位页）
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
# Tab 4: 游戏辅助专属页（4个卡片：攻略问答/问题诊断/资讯速递/游戏推荐）
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
        st = QLabel("攻略问答、问题诊断、资讯速递、游戏推荐——赫萝陪你通关 🐺🍎"); st.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']};")
        col.addWidget(st); hl.addLayout(col, 1)
        root.addLayout(hl)

        # 4 卡片（2x2 Grid）
        grid = QGridLayout(); grid.setSpacing(14); grid.setContentsMargins(0, 4, 0, 0)
        cards = [
            ("icon_history.png", "📜  游戏攻略问答",
             "问赫萝任何游戏攻略，她会整理成「操作步骤清单」",
             "攻略问答",
             "赫萝：唔姆~ 把你想问的游戏攻略告诉咱，咱帮你整理成清晰的步骤清单~\n\n（Stub：下一阶段接 LLM，攻略问答直接在聊天页展开。）🍎"),
            ("icon_code.png",    "🔧  游戏问题诊断",
             "游戏卡顿/崩溃/黑屏/网络延迟？赫萝帮你排查",
             "问题诊断",
             "赫萝：唔…游戏出问题了？把现象告诉咱，咱帮你找原因~\n\n（Stub：下一阶段接 LLM，根据描述给出排查建议。）🍎"),
            ("icon_chat.png",    "📰  游戏资讯速递",
             "版本更新/活动公告/新闻速报，赫萝帮你整理",
             "资讯速递",
             "赫萝：唔姆~ 想知道最近有什么游戏新闻？咱帮你整理~\n\n（Stub：下一阶段接资讯源，自动推送最新消息。）🍎"),
            ("icon_game.png",    "🎲  游戏推荐",
             "告诉赫萝你的喜好，她帮你挑下一款玩什么",
             "游戏推荐",
             "赫萝：唔姆~ 不知道玩什么好？告诉咱你喜欢什么类型的，咱给你推荐~\n\n（Stub：下一阶段接 LLM，根据喜好推荐游戏。）🍎"),
        ]
        for i, (icn, title, desc, key, stub) in enumerate(cards):
            r, c = divmod(i, 2)
            grid.addWidget(self._make_card(icn, title, desc, key, stub), r, c)
        root.addLayout(grid, 1)

    def _make_card(self, icn: str, title: str, desc: str, key: str, stub: str) -> QFrame:
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
        go.clicked.connect(lambda _=False, k=key, s=stub: QMessageBox.information(
            self._parent_dialog, f"Stub：{k} ✨", s
        ))
        v.addWidget(go)
        return f


# =====================================================================
# Tab 5: 代码助手专属页（上代码框/下输出 + 顶部3按钮）
#   注：代码助手使用独立的系统提示词（与自由聊天/蓝图生成隔离），
#       核心定位是排查代码 Bug，不涉及蓝图生成。
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
        st = QLabel("贴代码，赫萝帮你找 Bug / 解释 / 重构 🐺🍎"); st.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']};")
        col.addWidget(st); hl.addLayout(col, 1)
        root.addLayout(hl)

        # 3 个赫萝按钮
        btn_row = QHBoxLayout(); btn_row.setSpacing(10)
        b_bug = QPushButton("  �  查找 Bug"); b_bug.setObjectName("holoPrimary"); b_bug.setCursor(Qt.PointingHandCursor); b_bug.setMinimumHeight(34)
        b_bug.setIcon(icon("icon_code.png", 18)); b_bug.setIconSize(QSize(18,18))
        b_exp = QPushButton("  💡  解释代码"); b_exp.setCursor(Qt.PointingHandCursor); b_exp.setMinimumHeight(34)
        b_exp.setIcon(icon("icon_chat.png", 18)); b_exp.setIconSize(QSize(18,18))
        b_ref = QPushButton("  🪄  重构风格"); b_ref.setCursor(Qt.PointingHandCursor); b_ref.setMinimumHeight(34)
        b_ref.setIcon(icon("icon_retry.png", 18)); b_ref.setIconSize(QSize(18,18))
        for b in (b_bug, b_exp, b_ref):
            b.clicked.connect(lambda _=False, x=b.text(): QMessageBox.information(
                self._parent_dialog, f"Stub：{x.strip()}",
                f"赫萝：唔姆~ 代码分析功能马上就好啦~\n\n（Stub：下一阶段接 AST，{x.strip()}结果会写在下面输出框。）🍎"
            ))
        btn_row.addWidget(b_bug); btn_row.addWidget(b_exp); btn_row.addWidget(b_ref); btn_row.addStretch(1)
        root.addLayout(btn_row)

        # 代码框
        self._code = QPlainTextEdit()
        self._code.setPlaceholderText(
            "// 在这里粘贴 Python / JSON / 任何代码…\n"
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
            # 原图人物在画布内偏左，水平向右偏移 6px 让人物视觉居中
            qp.drawPixmap(10, 0, apm); qp.setClipping(False)
            qp.setPen(QPen(_qcolor("primary"), 2)); qp.setBrush(Qt.NoBrush)
            qp.drawEllipse(0,0,95,95); qp.end()
            big.setPixmap(out); big.setFixedSize(96,96)
        root.addWidget(big, 0, Qt.AlignHCenter)

        t = QLabel("⚙  助手设置"); t.setAlignment(Qt.AlignCenter)
        t.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:24px; font-weight:900;")
        root.addWidget(t)
        s = QLabel("设置面板里有：模型服务商 / 赫萝人设 / 通用参数 / 使用说明\n——所有选项在这里集中配置，改完点保存就生效啦 🍎")
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

        # 7 Tab
        tabs = [
            ("chat",      "icon_chat.png",      "自由聊天"),
            ("blueprint", "icon_blueprint.png", "蓝图生成"),
            ("image",     "icon_image.png",     "图像分析"),
            ("game",      "icon_game.png",      "游戏辅助"),
            ("code",      "icon_code.png",      "代码助手"),
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
        # ---- Page 5: 对话历史（专属页：QListWidget+每行时间+标题+继续/删除） ----
        self._stack.addWidget(_HistoryPage(self))
        # ---- Page 6: 助手设置（专属页：大图+跳转按钮直接打开LLMSettingsDialog） ----
        self._stack.addWidget(_SettingsPage(self))

        # 默认选中「自由聊天」
        self._side_buttons["chat"].setChecked(True)
        self._switch_tab("chat")

    # =================================================================
    # 左侧 Tab 切换
    # =================================================================
    TAB_INDEX = {
        "chat": 0, "blueprint": 1, "image": 2, "game": 3,
        "code": 4, "history": 5, "settings": 6,
    }

    def _switch_tab(self, key: str):
        idx = self.TAB_INDEX.get(key, 0)
        self._stack.setCurrentIndex(idx)
        # 切 Tab 后自动聚焦对应页面的输入框（用户不用再点一下才能打字）
        if key == "chat" and getattr(self, "ed_input", None) is not None:
            try: self.ed_input.setFocus(Qt.OtherFocusReason)
            except Exception: pass
        elif key == "blueprint" and getattr(self, "ed_bp_input", None) is not None:
            try: self.ed_bp_input.setFocus(Qt.OtherFocusReason)
            except Exception: pass
        # 注意：「助手设置」侧栏已经有 Stub 面板（Tab 7），不再额外弹窗——
        # 真正的设置对话框应通过主窗口的按钮打开（避免 Tab 切换时误弹模态窗阻塞）。

    # =================================================================
    # Page 0: 聊天
    # =================================================================
    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page); v.setContentsMargins(0,0,0,0); v.setSpacing(8)

        # ---- 顶部操作条（2 个赫萝 Icon 按钮 + 状态胶囊） ----
        topbar = QFrame(); topbar.setObjectName("holoCard")
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(12, 8, 12, 8); tl.setSpacing(8)

        # 状态胶囊（赫萝拿对话框 + 文字）
        st_wrap = QHBoxLayout(); st_wrap.setSpacing(8)
        si_lbl = QLabel(); si_lbl.setPixmap(pixmap("icon_chat.png", 18)); si_lbl.setFixedSize(20,20)
        st_wrap.addWidget(si_lbl)
        status_lbl = QLabel("<b>赫萝待命</b> 🍎")
        status_lbl.setObjectName("holoStatus")
        status_lbl.setStyleSheet(
            f"QLabel#holoStatus {{ background:{_THEME['bg_checked']}; "
            f"color:{_THEME['primary_dark']}; border:1px solid {_THEME['border']}; }}"
        )
        st_wrap.addWidget(status_lbl)
        tl.addLayout(st_wrap)
        tl.addStretch(1)

        # 新建会话 / 历史会话（会话管理入口）——「清空」按钮已删除，新建会话等同"开新对话"
        self.btn_new_chat = QPushButton(" 新建会话")
        self.btn_new_chat.setIcon(icon("icon_send.png", 16))
        self.btn_new_chat.setIconSize(QSize(16, 16))
        self.btn_new_chat.setCursor(Qt.PointingHandCursor)
        self.btn_new_chat.setToolTip("♻ 开启一个全新的对话（当前上下文自动存档到历史）")
        self.btn_new_chat.clicked.connect(self._on_new_session_clicked)
        tl.addWidget(self.btn_new_chat)

        # 历史会话：下拉菜单展示"自由聊天类型"的历史会话（不再跳转到 history 侧栏页）
        self.btn_history = QPushButton(" 历史会话")
        self.btn_history.setIcon(icon("icon_chat.png", 16))
        self.btn_history.setIconSize(QSize(16, 16))
        self.btn_history.setCursor(Qt.PointingHandCursor)
        self.btn_history.setToolTip("📚 切换到一条自由聊天的历史会话（下拉选择，不跳转）")
        self._menu_chat_history = QMenu(self.btn_history)
        self._build_chat_history_menu(self._menu_chat_history)
        self.btn_history.setMenu(self._menu_chat_history)
        tl.addWidget(self.btn_history)

        v.addWidget(topbar)

        # ---- 聊天列表 ----
        self.list_msg = QListWidget()
        self.list_msg.setWordWrap(True)
        self.list_msg.setSpacing(2)
        # 关键：按像素滚动而非按 item 滚动，否则超长气泡（比 viewport 还高）
        # 滚到底时只能对齐到气泡顶部，下半部分被裁且无法继续滚。
        self.list_msg.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list_msg.setUniformItemSizes(False)
        # 安装 eventFilter：对话框拖拽宽度时统一刷新所有气泡尺寸
        self.list_msg.installEventFilter(self)
        v.addWidget(self.list_msg, 1)

        # ---- 结果卡片（打桩：赫萝回复的通用摘要：代码片段 / 文案润色结果 / 草稿等） ----
        self.result_card = QFrame(); self.result_card.setObjectName("holoCard")
        self.result_card.setStyleSheet(
            f"#holoCard {{ background:{_THEME['maid_white']}; "
            f"border:2px dashed {_THEME['success']}; border-radius:16px; }}"
        )
        rc = QVBoxLayout(self.result_card)
        rc.setContentsMargins(14, 10, 14, 10); rc.setSpacing(4)
        rc.addWidget(QLabel(f"<b style='color:{_THEME['success']};'>📝 赫萝回复摘要（打桩示例）</b>"))
        detail = QLabel(
            "🗒 类型：代码解释 &nbsp; 字数：约 300 字 &nbsp; 语言：混合（中文 + Python）<br>"
            f"<span style='color:{_THEME['text_sub']};'>💡 提示：真实接入后这里会显示可折叠的代码块 / Markdown / 表格等富内容。</span>"
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
            "示例：解释下 QThread 的 moveToThread 怎么用？"
        )
        self.ed_input.setFixedHeight(110)
        self.ed_input.installEventFilter(self)
        icl.addWidget(self.ed_input)

        send_row = QHBoxLayout(); send_row.setSpacing(8)
        # 4 个快捷按钮：左侧 icon + 右侧文字（不再是纯圆形赫萝风格），用户一眼知道功能
        def _icon_btn(fn: str, label: str, tip: str) -> QPushButton:
            b = QPushButton(f" {label}"); b.setCursor(Qt.PointingHandCursor)
            b.setIcon(icon(fn, 18)); b.setIconSize(QSize(18, 18))
            b.setMinimumHeight(36)
            b.setToolTip(tip)
            b.setStyleSheet(
                f"QPushButton {{ padding:4px 12px 4px 8px; border-radius:10px;"
                f" background:{_THEME['bg_hover']}; border:1px solid {_THEME['border']};"
                f" color:{_THEME['text_main']}; font-weight:600; }}"
                f"QPushButton:hover {{ background:{_THEME['bg_checked']};"
                f" border-color:{_THEME['primary']}; color:{_THEME['primary_dark']}; }}"
            )
            return b
        self.btn_scr = _icon_btn("icon_screenshot.png", "截图", "📷 截图并发送给赫萝")
        self.btn_scr.clicked.connect(lambda: QMessageBox.information(
            self, "📷 截图（Stub）",
            "赫萝：要截图吗？未来咱会调 Windows GraphicsCapture 帮你框选哦~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_scr)

        self.btn_up = _icon_btn("icon_upload.png", "上传", "📂 上传文件 / 图片")
        self.btn_up.clicked.connect(lambda: QMessageBox.information(
            self, "📂 上传文件（Stub）",
            "赫萝：要给咱看什么文件呀？.py/.json/.txt 咱都能读~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_up)

        self.btn_voice = _icon_btn("icon_voice.png", "语音", "🎤 语音输入")
        self.btn_voice.clicked.connect(lambda: QMessageBox.information(
            self, "🎤 语音（Stub）",
            "赫萝：唔…现在咱还不会听你说话啦，下次给咱装个语音耳朵哦~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_voice)

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
    # Page 1: 蓝图生成（完全独立对话界面：顶栏+聊天列表+输入卡，和自由聊天互不干扰）
    # - assistant 消息显示：重写 / 导入画布 / 复制 三个操作按钮（自由聊天只有复制）
    # - 顶栏按钮：新建会话 / 历史会话（蓝图模式独立会话管理，不与自由聊天共用记录）
    # =================================================================
    def _build_blueprint_page(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page); v.setContentsMargins(0, 0, 0, 0); v.setSpacing(8)

        # ---- 顶部操作条（状态胶囊 + 新建/历史会话） ----
        topbar = QFrame(); topbar.setObjectName("holoCard")
        tl = QHBoxLayout(topbar)
        tl.setContentsMargins(12, 8, 12, 8); tl.setSpacing(8)

        # 状态胶囊（蓝图 icon + 专属文字，体现独立的蓝图专用 system prompt）
        st_wrap = QHBoxLayout(); st_wrap.setSpacing(8)
        si_lbl = QLabel(); si_lbl.setPixmap(pixmap("icon_blueprint.png", 18)); si_lbl.setFixedSize(20, 20)
        st_wrap.addWidget(si_lbl)
        status_lbl = QLabel("<b>赫萝待命</b> 📋")
        status_lbl.setObjectName("holoStatus")
        status_lbl.setStyleSheet(
            f"QLabel#holoStatus {{ background:{_THEME['primary_light']}; "
            f"color:{_THEME['primary_dark']}; border:1px solid {_THEME['primary']}; }}"
        )
        st_wrap.addWidget(status_lbl)
        tl.addLayout(st_wrap)
        tl.addStretch(1)

        # 新建会话（会话管理，不与气泡按钮重复）
        self.btn_bp_new = QPushButton(" 新建会话")
        self.btn_bp_new.setIcon(icon("icon_send.png", 16))
        self.btn_bp_new.setIconSize(QSize(16, 16))
        self.btn_bp_new.setCursor(Qt.PointingHandCursor)
        self.btn_bp_new.setToolTip("♻ 开启一条全新的蓝图会话（当前草稿自动存入历史）")
        self.btn_bp_new.clicked.connect(self._on_bp_new_session_clicked)
        tl.addWidget(self.btn_bp_new)

        self.btn_bp_history = QPushButton(" 历史会话")
        self.btn_bp_history.setIcon(icon("icon_chat.png", 16))
        self.btn_bp_history.setIconSize(QSize(16, 16))
        self.btn_bp_history.setCursor(Qt.PointingHandCursor)
        self.btn_bp_history.setToolTip("📚 切换到一条蓝图类型的历史会话（下拉选择，不跳转）")
        self._menu_bp_history = QMenu(self.btn_bp_history)
        self._build_bp_history_menu(self._menu_bp_history)
        self.btn_bp_history.setMenu(self._menu_bp_history)
        tl.addWidget(self.btn_bp_history)

        v.addWidget(topbar)

        # ---- 蓝图聊天列表（独立：和自由聊天 list_msg 完全隔离） ----
        self.list_bp = QListWidget()
        self.list_bp.setWordWrap(True)
        self.list_bp.setSpacing(2)
        # 关键：按像素滚动而非按 item 滚动，否则超长气泡（比 viewport 还高）
        # 滚到底时只能对齐到气泡顶部，下半部分被裁且无法继续滚。
        self.list_bp.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list_bp.setUniformItemSizes(False)
        # 安装 eventFilter：拖拽窗口宽度时自适应气泡尺寸
        self.list_bp.installEventFilter(self)
        v.addWidget(self.list_bp, 1)

        # ---- 结果卡片（蓝图模式：生成完显示节点统计） ----
        self.result_bp_card = QFrame(); self.result_bp_card.setObjectName("holoCard")
        self.result_bp_card.setStyleSheet(
            f"#holoCard {{ background:{_THEME['maid_white']}; "
            f"border:2px dashed {_THEME['primary']}; border-radius:16px; }}"
        )
        rc = QVBoxLayout(self.result_bp_card)
        rc.setContentsMargins(14, 10, 14, 10); rc.setSpacing(4)
        rc.addWidget(QLabel(f"<b style='color:{_THEME['primary_dark']};'>📋 蓝图草稿（打桩示例）</b>"))
        detail = QLabel(
            "📊 节点数：5 个 &nbsp; 连线数：4 条 &nbsp; 覆盖：OpenApp ×1 / Wait ×1 / PressKey ×1<br>"
            f"<span style='color:{_THEME['text_sub']};'>⚠ Stub：FindImage 节点未指定图路径，导入后需手动选择。</span>"
        )
        detail.setWordWrap(True); detail.setStyleSheet(f"background:transparent;color:{_THEME['text_main']};")
        rc.addWidget(detail)
        self.result_bp_card.hide()
        v.addWidget(self.result_bp_card)

        # ---- 输入卡片 ----
        input_card = QFrame(); input_card.setObjectName("holoCard")
        icl = QVBoxLayout(input_card)
        icl.setContentsMargins(10, 10, 10, 10); icl.setSpacing(8)

        self.ed_bp_input = QPlainTextEdit()
        self.ed_bp_input.setPlaceholderText(
            "（赫萝：这里只用蓝图专用 prompt，不会夹带闲聊内容~ 🍎）\n"
            "Enter 发送 · Ctrl+Enter 换行\n"
            "示例：打开记事本，等 2 秒，输入 Hello，关闭窗口"
        )
        self.ed_bp_input.setFixedHeight(110)
        self.ed_bp_input.installEventFilter(self)  # Enter 发送 / Ctrl+Enter 换行
        icl.addWidget(self.ed_bp_input)

        send_row = QHBoxLayout(); send_row.setSpacing(8)

        def _icon_btn(fn: str, label: str, tip: str) -> QPushButton:
            b = QPushButton(f" {label}"); b.setCursor(Qt.PointingHandCursor)
            b.setIcon(icon(fn, 18)); b.setIconSize(QSize(18, 18))
            b.setMinimumHeight(36)
            b.setToolTip(tip)
            b.setStyleSheet(
                f"QPushButton {{ padding:4px 12px 4px 8px; border-radius:10px;"
                f" background:{_THEME['bg_hover']}; border:1px solid {_THEME['border']};"
                f" color:{_THEME['text_main']}; font-weight:600; }}"
                f"QPushButton:hover {{ background:{_THEME['bg_checked']};"
                f" border-color:{_THEME['primary']}; color:{_THEME['primary_dark']}; }}"
            )
            return b

        self.btn_bp_scr = _icon_btn("icon_screenshot.png", "截图", "📷 截图并发送给赫萝（蓝图识别）")
        self.btn_bp_scr.clicked.connect(lambda: QMessageBox.information(
            self, "📷 截图（Stub）",
            "赫萝：要识别蓝图里的按钮吗？未来咱会调 Windows GraphicsCapture 帮你框选哦~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_bp_scr)

        self.btn_bp_up = _icon_btn("icon_upload.png", "上传", "📂 上传 JSON/蓝图草稿 / 图片")
        self.btn_bp_up.clicked.connect(lambda: QMessageBox.information(
            self, "📂 上传蓝图（Stub）",
            "赫萝：要给咱看什么蓝图草稿呀？.bp.json / .txt 咱都能读~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_bp_up)

        self.btn_bp_voice = _icon_btn("icon_voice.png", "语音", "🎤 语音输入蓝图需求")
        self.btn_bp_voice.clicked.connect(lambda: QMessageBox.information(
            self, "🎤 语音（Stub）",
            "赫萝：唔…现在咱还不会听你说话啦，下次给咱装个语音耳朵哦~\nStub：暂未接通。"
        ))
        send_row.addWidget(self.btn_bp_voice)

        send_row.addStretch(1)
        lbl_hint = QLabel("Enter 发送 · Ctrl+Enter 换行")
        lbl_hint.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:12px;")
        send_row.addWidget(lbl_hint)

        self.btn_bp_send = QPushButton()
        self.btn_bp_send.setObjectName("holoAppleSend")
        self.btn_bp_send.setFixedSize(44, 44)
        self.btn_bp_send.setCursor(Qt.PointingHandCursor)
        self.btn_bp_send.setToolTip("发送蓝图需求给赫萝 📋")
        self.btn_bp_send.setIcon(icon("icon_send.png", 26))
        self.btn_bp_send.setIconSize(QSize(26, 26))
        self.btn_bp_send.clicked.connect(self._on_bp_send_clicked)
        send_row.addWidget(self.btn_bp_send)
        icl.addLayout(send_row)

        v.addWidget(input_card)

        # 初始化：追加一条蓝图专属欢迎语（和自由聊天欢迎语完全独立）
        QTimer.singleShot(20, self._append_bp_welcome)
        return page

    def _append_bp_welcome(self):
        """蓝图 Tab 专属欢迎语：强调独立的 system prompt，不会和自由聊天混在一起。"""
        self._append_bp_msg(
            "assistant",
            "唔姆~ 这里是赫萝的【蓝图生成专用区】📋\n\n"
            "本页会自动加载【蓝图专用 system prompt】（节点规范 / JSON 模板 / 指令说明），\n"
            "和「自由聊天」Tab 完全独立——闲聊的内容不会影响蓝图生成，蓝图需求也不会带乱人设。\n\n"
            "· 用自然语言描述：「打开记事本等 2 秒，输入 Hello，再关掉」\n"
            "· 多轮修改：「把等待时间改 5 秒，再加 3 次循环」\n"
            "· 生成完点气泡右下角「➕ 导入」按钮，直接把草稿写入当前打开的蓝图画布。\n\n"
            "（Stub：这里会走蓝图专用请求通道；真实接入后会显示可折叠的 JSON 树预览。）"
        )

    # =================================================================
    # 窗口拖拽宽度自适应：刷新所有已存在气泡的宽度 / 高度
    # =================================================================
    def _refresh_all_bubble_sizes(self, list_widget):
        """按指定聊天列表的当前宽度，重新计算其所有 item 的 sizeHint + 气泡内部内容宽度。"""
        if list_widget is None or list_widget not in (getattr(self, "list_msg", None), getattr(self, "list_bp", None)):
            return
        new_bubble_w = max(400, list_widget.width() - 30)
        total = list_widget.count()
        for i in range(total):
            item = list_widget.item(i)
            widget = list_widget.itemWidget(item)
            if isinstance(widget, ChatBubble):
                widget.update_bubble_width(new_bubble_w)
                widget.adjustSize()
                sh = widget.sizeHint()
                item.setSizeHint(QSize(new_bubble_w, max(74, sh.height())))
        list_widget.scheduleDelayedItemsLayout()

    def _queue_resize_refresh(self, list_widget):
        """合并 Resize 事件：25ms 内的多次 Resize 合并为一次刷新，保证拖拽流畅。"""
        if list_widget is None:
            return
        if not hasattr(self, "_pending_resize") or self._pending_resize is None:
            self._pending_resize: dict = {}
        self._pending_resize[list_widget] = True
        if not hasattr(self, "_resize_timer") or self._resize_timer is None:
            self._resize_timer = QTimer(self)
            self._resize_timer.setSingleShot(True)
            def _do_refresh():
                pending = getattr(self, "_pending_resize", {})
                for lw, need in list(pending.items()):
                    if need:
                        self._refresh_all_bubble_sizes(lw)
                pending.clear()
            self._resize_timer.timeout.connect(_do_refresh)
        if not self._resize_timer.isActive():
            self._resize_timer.start(25)

    # =================================================================
    # 消息/欢迎
    # =================================================================
    def _append_welcome(self):
        """自由聊天 Tab 专属欢迎语（只有蓝图生成 Tab 才有蓝图谱系相关提示）。"""
        self._append_chat_msg(
            "assistant",
            "唔姆，苹果商人汝可算来了~ 咱是赫萝，你的女仆助理 🐺🍎\n\n"
            "有什么事想聊都可以直接说：\n"
            "  · 闲聊吐槽、问问题\n"
            "  · 解释一段代码、查 Bug、润色文案\n\n"
            "（Stub：发送后会显示模拟回复，真实 LLM 功能后续接入。）"
        )

    def _append_chat_msg(self, role: str, text: str):
        """自由聊天 Tab 追加消息（mode=chat：assistant 只显示「复制」按钮）。"""
        list_w = self.list_msg
        bubble_w = max(400, list_w.width() - 30)
        bubble = ChatBubble(role, text, mode="chat")
        bubble.update_bubble_width(bubble_w)
        bubble.adjustSize()
        sh = bubble.sizeHint()
        item = QListWidgetItem()
        item.setSizeHint(QSize(bubble_w, max(74, sh.height())))
        list_w.addItem(item)
        list_w.setItemWidget(item, bubble)
        QTimer.singleShot(20, lambda: list_w.scrollToBottom())

    def get_chat_messages(self):
        """返回自由聊天 Tab 的所有 (role, text) 对，供外部同步（如 QuickChatBubble）。"""
        out = []
        for i in range(self.list_msg.count()):
            item = self.list_msg.item(i)
            w = self.list_msg.itemWidget(item)
            if w is not None and hasattr(w, "_role") and hasattr(w, "_text"):
                out.append((w._role, w._text))
        return out

    def _append_bp_msg(self, role: str, text: str):
        """蓝图生成 Tab 追加消息（mode=blueprint：assistant 显示重写/导入/复制三按钮）。"""
        list_w = self.list_bp
        bubble_w = max(400, list_w.width() - 30)
        bubble = ChatBubble(role, text, mode="blueprint")
        bubble.update_bubble_width(bubble_w)
        bubble.adjustSize()
        sh = bubble.sizeHint()
        item = QListWidgetItem()
        item.setSizeHint(QSize(bubble_w, max(74, sh.height())))
        list_w.addItem(item)
        list_w.setItemWidget(item, bubble)
        QTimer.singleShot(20, lambda: list_w.scrollToBottom())

    # =================================================================
    # 打桩事件
    # =================================================================
    def _on_send_clicked(self):
        text = self.ed_input.toPlainText().strip()
        if not text:
            return
        self._append_chat_msg("user", text)
        self.ed_input.clear()
        self.message_sent.emit(text)
        self.btn_send.setEnabled(False)
        self.btn_send.setToolTip("赫萝正在思考…")
        QTimer.singleShot(450, lambda: self._mock_reply(text))

    def _mock_reply(self, user_text: str):
        self.btn_send.setEnabled(True)
        self.btn_send.setToolTip("发送给赫萝 🍎")
        reply = (
            f"唔姆~ 收到汝的留言啦：『{user_text[:24]}{'…' if len(user_text)>24 else ''}』\n\n"
            f"咱先给汝打个草稿示例：\n"
            f"  · 要是这是代码问题：咱会列出关键点 + 给出可运行的小片段\n"
            f"  · 要是这是闲聊：咱就跟汝唠两句，顺便要一个苹果 🍎\n"
            f"  · 要是这是润色文案：咱会改出 2~3 个版本供汝挑。\n\n"
            f"（Stub：真实接入后这里会显示真正的 LLM 回复，支持代码块/Markdown）"
        )
        self._append_chat_msg("assistant", reply)
        # 结果卡片更新一下字数/类型（打桩：不依赖任何蓝图字段）
        try:
            card_layout = self.result_card.findChild(QVBoxLayout)
            if card_layout is not None and card_layout.count() >= 2:
                w = card_layout.itemAt(1).widget()
                if isinstance(w, QLabel):
                    n = len(user_text)
                    w.setText(
                        f"🗒 类型：自由回复 &nbsp; 输入字数：{n} 字 &nbsp; 回复字数：{len(reply)} 字<br>"
                        f"<span style='color:{_THEME['text_sub']};'>💡 提示：真实接入后这里会显示可折叠的代码块 / Markdown / 表格等富内容。</span>"
                    )
        except Exception:
            pass
        self.result_card.show()

    # =================================================================
    # 蓝图生成 Tab 打桩事件（独立的，和自由聊天完全分离）
    # =================================================================
    def _on_bp_send_clicked(self):
        text = self.ed_bp_input.toPlainText().strip()
        if not text:
            return
        self._append_bp_msg("user", text)
        self.ed_bp_input.clear()
        self.btn_bp_send.setEnabled(False)
        self.btn_bp_send.setToolTip("赫萝正在生成蓝图草稿…")
        QTimer.singleShot(500, lambda: self._mock_bp_reply(text))

    def _mock_bp_reply(self, user_text: str):
        self.btn_bp_send.setEnabled(True)
        self.btn_bp_send.setToolTip("发送蓝图需求给赫萝 📋")
        self._append_bp_msg(
            "assistant",
            f"唔姆~ 你的蓝图需求收到啦：『{user_text[:24]}{'…' if len(user_text)>24 else ''}』\n\n"
            f"咱按蓝图专用规范生成草稿：\n"
            f"  · 节点 1（Start）：入口\n"
            f"  · 节点 2（OpenApp）：打开记事本 notepad.exe\n"
            f"  · 节点 3（Wait）：等待 2 秒\n"
            f"  · 节点 4（PressKey）：输入 Hello 并回车\n"
            f"  · 节点 5（CloseApp）：关闭记事本窗口\n\n"
            f"（Stub：真实接入后这里会显示可折叠的节点树 + JSON 预览，支持逐节点修改。）"
        )
        self.result_bp_card.show()

    def _on_bp_clear_clicked(self):
        r = QMessageBox.question(
            self, "清空蓝图对话？", "确定要把当前蓝图会话全部丢掉吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if r == QMessageBox.Yes:
            self.list_bp.clear()
            self.result_bp_card.hide()
            self._append_bp_welcome()

    def _on_clear_clicked(self):
        r = QMessageBox.question(
            self, "清空对话？", "确定要把当前对话历史全部丢掉吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if r == QMessageBox.Yes:
            self.list_msg.clear()
            self.result_card.hide()
            self._append_welcome()

    def _on_new_session_clicked(self):
        """「新建会话」：保留当前会话在历史里（真实接入会持久化），
        然后清空当前聊天列表并重新显示欢迎语，相当于开一个新会话。"""
        r = QMessageBox.question(
            self, "新开一个对话？",
            "当前对话会保留到历史会话里，确认开启一个全新的会话吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if r == QMessageBox.Yes:
            self.list_msg.clear()
            self.result_card.hide()
            self._append_welcome()

    # =================================================================
    # 会话管理：Mock 历史会话数据 + 下拉菜单 + 切换加载
    # （Stup：真实接入后这些数据应从 LLMConversationStore / 数据库读取）
    # =================================================================

    # 自由聊天类型的 mock 历史会话
    _MOCK_CHAT_HISTORY: dict = {
        "chat_1": {
            "title": "🍎 赫萝的苹果闲聊 (7/5)",
            "messages": [
                ("user", "赫萝，你今天吃了几个苹果呀？"),
                ("assistant", "唔姆…咱今天已经吃了 3 颗了，汝不介意和咱一起分享吗～"),
                ("user", "那我给你带两斤来，下次不许抢我的零食了啊。"),
                ("assistant", "好~ 咱不抢就是了，两斤苹果换汝的零食，算啦算啦 🐺🍎"),
            ],
        },
        "chat_2": {
            "title": "🎮 游戏宅赫萝·今日吐槽 (7/4)",
            "messages": [
                ("user", "今天肝到哪了？"),
                ("assistant", "唔…原神深渊第 12 层第三间又没满星，最后差 8 秒！好气哦…"),
                ("user", "明天加油，我陪你凹。"),
                ("assistant", "哼，汝说的，不许跑路！凹满星报酬要给咱 3 颗苹果 ♡"),
            ],
        },
        "chat_3": {
            "title": "😌 贤狼赫萝·人生相谈 (7/3)",
            "messages": [
                ("user", "赫萝，最近工作压力好大，怎么办？"),
                ("assistant", "呼，咱活了几百岁，这种事见多啦——放下手上的事，去吃点好的、散散步、睡个好觉。"),
                ("user", "明天我就请假去旅游。"),
                ("assistant", "这就对了嘛，汝不是机器，要对自己好一点~ 咱陪汝一起去。"),
            ],
        },
    }

    # 蓝图生成类型的 mock 历史会话
    _MOCK_BP_HISTORY: dict = {
        "bp_1": {
            "title": "📋 记事本自动输入 (7/4)",
            "messages": [
                ("user", "打开记事本，等 2 秒，输入 Hello 再关掉。"),
                ("assistant",
                 "好，咱生成的蓝图草稿：\n"
                 "  · Start → OpenApp(notepad.exe)\n"
                 "  · Wait(2s) → PressKey(Hello, ENTER)\n"
                 "  · CloseApp(notepad) → End\n\n"
                 "共 5 节点 4 连线，支持直接导入画布。"),
            ],
        },
        "bp_2": {
            "title": "⚡ 游戏连点宏·每秒 10 次 (7/2)",
            "messages": [
                ("user", "给我写一个连点宏，每秒点鼠标左键 10 次，按 F8 开始、F9 停止。"),
                ("assistant",
                 "好，蓝图草稿：\n"
                 "  · Start → HotkeyListener(F8)\n"
                 "  · Loop(Counter=∞) → MouseClick(Left, Repeat=10, Interval=100ms)\n"
                 "  · HotkeyListener(F9) → End\n\n"
                 "注：实际使用时请确认不违反游戏用户协议哦~"),
            ],
        },
        "bp_3": {
            "title": "🚀 每日工作启动器·批量启动 (6/30)",
            "messages": [
                ("user", "帮我一键启动：微信、VSCode、Chrome、钉钉，再打开 G:\\work\\BlueprintAutomation 工作目录。"),
                ("assistant",
                 "好，蓝图草稿：\n"
                 "  · Start → Parallel{ OpenApp(WeChat) / OpenApp(VSCode) / OpenApp(Chrome) / OpenApp(DingTalk) }\n"
                 "  · OpenDir(G:\\work\\BlueprintAutomation) → End\n\n"
                 "可设置为开机自启，点『导入画布』后按你机器的实际路径微调即可。"),
            ],
        },
    }

    def _build_chat_history_menu(self, menu: QMenu):
        """构造「自由聊天」Tab 的历史会话下拉菜单（点击直接切换，不跳转到 history 页）。"""
        menu.clear()
        # 菜单头
        header = menu.addAction("📚 自由聊天·历史会话")
        header.setEnabled(False)
        menu.addSeparator()
        # 每个会话一个 Action
        for sess_id, data in self._MOCK_CHAT_HISTORY.items():
            action = QAction(data.get("title", sess_id), menu)
            action.setToolTip("点击直接在当前自由聊天 Tab 加载该会话")
            action.triggered.connect(lambda _=False, s=sess_id: self._load_history_session("chat", s))
            menu.addAction(action)
        menu.addSeparator()
        tip = menu.addAction("💡 提示：真实接入后这里会读取 LLM 历史会话库")
        tip.setEnabled(False)

    def _build_bp_history_menu(self, menu: QMenu):
        """构造「蓝图生成」Tab 的历史会话下拉菜单（点击直接切换，不跳转到 history 页）。"""
        menu.clear()
        header = menu.addAction("📚 蓝图生成·历史会话")
        header.setEnabled(False)
        menu.addSeparator()
        for sess_id, data in self._MOCK_BP_HISTORY.items():
            action = QAction(data.get("title", sess_id), menu)
            action.setToolTip("点击直接在当前蓝图 Tab 加载该会话草稿")
            action.triggered.connect(lambda _=False, s=sess_id: self._load_history_session("blueprint", s))
            menu.addAction(action)
        menu.addSeparator()
        tip = menu.addAction("💡 提示：选择后可点气泡右下角「➕ 导入」直接写入画布")
        tip.setEnabled(False)

    def _load_history_session(self, hist_type: str, sess_id: str):
        """加载一条 mock 历史会话到对应的 Tab 聊天列表里。
        hist_type: 'chat' -> 自由聊天 list_msg；'blueprint' -> 蓝图生成 list_bp
        """
        hist = (self._MOCK_CHAT_HISTORY if hist_type == "chat" else self._MOCK_BP_HISTORY).get(sess_id)
        if not hist or "messages" not in hist:
            return
        # 切到对应 Tab（历史会话可能从别的 Tab 点，但菜单只在对应 Tab 上显示，这里加个保险）
        if hist_type == "chat":
            self._side_buttons["chat"].setChecked(True)
            self._switch_tab("chat")
            list_w = self.list_msg
            append_fn = self._append_chat_msg
            self.result_card.hide()
        else:
            self._side_buttons["blueprint"].setChecked(True)
            self._switch_tab("blueprint")
            list_w = self.list_bp
            append_fn = self._append_bp_msg
            self.result_bp_card.hide()
        # 先清空当前列表，再逐条追加历史消息
        list_w.clear()
        for role, text in hist["messages"]:
            append_fn(role, text)

    # =================================================================
    # 蓝图 Tab 新建会话（和自由聊天的「新建会话」行为一致）
    # =================================================================
    def _on_bp_new_session_clicked(self):
        r = QMessageBox.question(
            self, "新开一条蓝图会话？",
            "当前蓝图草稿会保留到历史会话里，确认开启一个全新的会话吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if r == QMessageBox.Yes:
            self.list_bp.clear()
            self.result_bp_card.hide()
            self._append_bp_welcome()

    # =================================================================
    # Enter 发送 / Ctrl+Enter 换行 + 聊天列表 Resize 自适应（合并，避免两个 eventFilter 相互覆盖）
    # =================================================================
    def eventFilter(self, obj, event):
        # 1) 自由聊天：输入框 Enter 发送 / Ctrl+Enter 换行
        if obj is getattr(self, "ed_input", None) and event.type() == QEvent.KeyPress:
            from PySide6.QtGui import QKeyEvent
            k = event.key()
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ControlModifier:
                    return False
                self._on_send_clicked()
                return True
        # 1b) 蓝图生成：输入框 Enter 发送 / Ctrl+Enter 换行
        if obj is getattr(self, "ed_bp_input", None) and event.type() == QEvent.KeyPress:
            from PySide6.QtGui import QKeyEvent
            k = event.key()
            if k in (Qt.Key_Return, Qt.Key_Enter):
                if event.modifiers() & Qt.ControlModifier:
                    return False
                self._on_bp_send_clicked()
                return True
        # 2) Resize 事件：自由聊天 list_msg / 蓝图生成 list_bp 宽度自适应气泡
        if (obj is getattr(self, "list_msg", None) or obj is getattr(self, "list_bp", None)) \
                and event.type() == QEvent.Resize:
            self._queue_resize_refresh(obj)
        return super().eventFilter(obj, event)

    # =================================================================
    # 外部：打开后聚焦输入框
    # =================================================================
    def focus_input(self):
        try:
            self.ed_input.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass
