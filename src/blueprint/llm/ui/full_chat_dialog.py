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
    QImage,
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
    QFileDialog,
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

    def update_text(self, new_text: str):
        """流式更新文本（触发重绘 + 重新计算 sizeHint）。"""
        self._text = new_text
        self.updateGeometry()
        self.update()

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

    def update_text(self, new_text: str):
        """流式更新文本内容（用于 LLM 流式输出时逐 chunk 刷新气泡）。"""
        self._text = new_text
        self._content.update_text(new_text)
        # 重新计算高度并更新 QListWidgetItem 的 sizeHint
        self.adjustSize()
        sh = self.sizeHint()
        list_w = self.parent()
        while list_w is not None and not hasattr(list_w, 'itemWidget'):
            list_w = list_w.parent()
        if list_w is not None:
            for i in range(list_w.count()):
                item = list_w.item(i)
                if list_w.itemWidget(item) is self:
                    item.setSizeHint(QSize(max(400, self.width()), max(self.minimumHeight(), sh.height())))
                    break
        QTimer.singleShot(10, lambda: list_w.scrollToBottom() if list_w else None)

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
        # 运行期状态
        self._img_worker = None
        self._img_streaming = ""
        self._current_pixmap: Optional[QPixmap] = None
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

        # 中央拖拽区（缩略图 + 提示 + 按钮）
        self._drop_zone = QFrame(); self._drop_zone.setObjectName("holoCard")
        self._drop_zone.setMinimumHeight(280)
        self._drop_zone.setStyleSheet(
            f"#holoCard {{ background:{_THEME['bg_root']};"
            f" border:2px dashed {_THEME['border']}; border-radius:20px; }}"
        )
        dz = QVBoxLayout(self._drop_zone); dz.setContentsMargins(20, 24, 20, 24); dz.setSpacing(12)
        # 缩略图标签：默认显示大图标，加载图片后显示缩略图
        self._thumb_label = QLabel(); self._thumb_label.setAlignment(Qt.AlignCenter)
        big_pm = pixmap("icon_image.png", 128)
        if not big_pm.isNull(): self._thumb_label.setPixmap(big_pm)
        dz.addWidget(self._thumb_label)
        self._drop_hint = QLabel("把截图拖进来，或点下面 📷 选图~"); self._drop_hint.setAlignment(Qt.AlignCenter)
        self._drop_hint.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-size:20px; font-weight:800;")
        dz.addWidget(self._drop_hint)
        sub_text = QLabel("（支持 PNG / JPG / BMP / WEBP，截图直接 Ctrl+V 粘贴也行哦）"); sub_text.setAlignment(Qt.AlignCenter)
        sub_text.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:13px;")
        dz.addWidget(sub_text)
        dz.addStretch(1)

        # 2 个快速按钮
        btns = QHBoxLayout(); btns.setSpacing(10); btns.setAlignment(Qt.AlignCenter)
        self._btn_clip = QPushButton("  📷  从剪贴板 / 截图")
        self._btn_clip.setIcon(icon("icon_screenshot.png", 20)); self._btn_clip.setIconSize(QSize(20, 20))
        self._btn_clip.setCursor(Qt.PointingHandCursor); self._btn_clip.setObjectName("holoPrimary"); self._btn_clip.setMinimumHeight(36)
        self._btn_clip.clicked.connect(self._load_from_clipboard)
        self._btn_file = QPushButton("  📂  选择图片文件")
        self._btn_file.setIcon(icon("icon_upload.png", 20)); self._btn_file.setIconSize(QSize(20, 20))
        self._btn_file.setCursor(Qt.PointingHandCursor); self._btn_file.setMinimumHeight(36)
        self._btn_file.clicked.connect(self._load_from_file)
        btns.addWidget(self._btn_clip); btns.addWidget(self._btn_file); dz.addLayout(btns)
        root.addWidget(self._drop_zone, 2)

        # 输出区：LLM 视觉分析结果
        ol = QHBoxLayout(); ol.setSpacing(8); ol.setAlignment(Qt.AlignVCenter)
        lt = QLabel("📤  分析结果："); lt.setStyleSheet(f"background:transparent;color:{_THEME['primary_dark']}; font-weight:800;")
        ol.addWidget(lt); ol.addStretch(1)
        cb = QPushButton("  📋  复制"); cb.setIcon(icon("icon_import.png", 14)); cb.setIconSize(QSize(14, 14))
        cb.setCursor(Qt.PointingHandCursor)
        cb.clicked.connect(lambda: (QApplication.clipboard().setText(self._out.toPlainText()),
                                    QMessageBox.information(self._parent_dialog, "✅", "赫萝：已复制到剪贴板 🍎")))
        ol.addWidget(cb); root.addLayout(ol)
        self._out = QPlainTextEdit()
        self._out.setReadOnly(True)
        self._out.setPlaceholderText("（把图片扔给赫萝，她的视觉分析结果会显示在这里哦~）")
        self._out.setMinimumHeight(150)
        root.addWidget(self._out, 1)

    # ---- 图片加载入口 ----
    def _set_image(self, qpm: QPixmap):
        """统一入口：接收 QPixmap → 显示缩略图 → 发送给 LLM 分析。"""
        if qpm.isNull():
            QMessageBox.warning(self._parent_dialog, "图片无效",
                                "赫萝：这张图片咱看不了呀~ 换一张试试？ 🍎")
            return
        self._current_pixmap = qpm
        # 缩略图等比缩放到拖拽区内（替换原来的大图标）
        thumb = qpm.scaled(360, 220, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self._thumb_label.setPixmap(thumb)
        self._drop_hint.setText("已加载图片，赫萝正在看哦~ 👀")
        self._run_image_llm()

    def _load_from_clipboard(self):
        """从系统剪贴板获取图片（截图 / 复制的图片）。"""
        cb = QApplication.clipboard()
        qpm = cb.pixmap()
        if qpm.isNull():
            # 部分平台 pixmap() 取不到，回退用 image()
            img = cb.image()
            if not img.isNull():
                qpm = QPixmap.fromImage(img)
        if qpm.isNull():
            QMessageBox.information(self._parent_dialog, "剪贴板无图片",
                                    "赫萝：剪贴板里没有图片呀~\n先截图（Win+Shift+S / PrintScreen）或复制一张图再来~ 🍎")
            return
        self._set_image(qpm)

    def _load_from_file(self):
        """文件对话框选择图片文件。"""
        path, _ = QFileDialog.getOpenFileName(
            self._parent_dialog, "选择图片", "",
            "图片文件 (*.png *.jpg *.jpeg *.bmp *.gif *.webp);;所有文件 (*.*)"
        )
        if not path:
            return
        qpm = QPixmap(path)
        if qpm.isNull():
            QMessageBox.warning(self._parent_dialog, "图片加载失败",
                                f"赫萝：这个文件打不开呀~\n{path}")
            return
        self._set_image(qpm)

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
        md = e.mimeData()
        qpm = QPixmap()
        # 优先取内嵌图像数据
        if md.hasImage():
            img = QImage(md.imageData())
            if not img.isNull():
                qpm = QPixmap.fromImage(img)
        # 再尝试 URL（本地文件拖入）
        if qpm.isNull() and md.hasUrls():
            for url in md.urls():
                if url.isLocalFile():
                    qpm = QPixmap(url.toLocalFile())
                    if not qpm.isNull():
                        break
        if qpm.isNull():
            QMessageBox.information(self._parent_dialog, "图片无效",
                                    "赫萝：拖进来的不是图片呀~ 支持 PNG / JPG / BMP 哦 🍎")
            return
        self._set_image(qpm)

    # ---- LLM 视觉分析 ----
    def _image_to_base64(self, qpm: QPixmap) -> str:
        """QPixmap → QImage → QByteArray → base64 字符串。"""
        from PySide6.QtCore import QBuffer, QIODevice
        img = qpm.toImage()  # QPixmap → QImage
        ba = QByteArray()    # QImage → QByteArray
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        buf.close()
        return bytes(ba.toBase64()).decode("ascii")  # → base64

    def _set_buttons_enabled(self, enabled: bool):
        self._btn_clip.setEnabled(enabled)
        self._btn_file.setEnabled(enabled)

    def _run_image_llm(self):
        """构造 vision messages 并启动 ChatWorker（流式输出）。"""
        from src.blueprint.llm.llm_worker import ChatWorker
        from src.blueprint.llm import prompt_builder

        qpm = self._current_pixmap
        if qpm is None or qpm.isNull():
            return

        # 上一次还在跑 → 断开旧信号并取消，避免串流
        if self._img_worker is not None and self._img_worker.isRunning():
            try:
                self._img_worker.chunk_received.disconnect()
                self._img_worker.finished.disconnect()
                self._img_worker.error.disconnect()
            except Exception:
                pass
            self._img_worker.cancel()

        # 缩放到合理尺寸，避免 base64 体积过大
        src = qpm
        max_side = 1024
        if src.width() > max_side or src.height() > max_side:
            src = src.scaled(max_side, max_side, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        try:
            b64 = self._image_to_base64(src)
        except Exception as exc:
            QMessageBox.warning(self._parent_dialog, "图片编码失败",
                                f"赫萝：图片转码失败啦~\n{exc}")
            return

        # 系统提示词：复用自由聊天人设 + 追加视觉分析指令
        sys_prompt = prompt_builder.build_chat_prompt(_get_preset())
        sys_prompt += (
            "\n\n附加指令：请分析这张截图，识别其中的按钮、文字、UI元素，"
            "帮助用户理解画面内容。"
        )
        # OpenAI vision content 列表格式：text + image_url
        user_content = [
            {"type": "text",
             "text": "请分析这张截图，识别其中的按钮、文字、UI元素，帮助用户理解画面内容。"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_content},
        ]

        self._img_streaming = "赫萝正在看图…"
        self._out.setPlainText(self._img_streaming)
        self._set_buttons_enabled(False)
        try:
            self._parent_dialog.thinking_changed.emit("thinking")
        except Exception:
            pass

        self._img_worker = ChatWorker(messages, stream=True, parent=self)
        self._img_worker.chunk_received.connect(self._on_img_chunk)
        self._img_worker.finished.connect(self._on_img_finished)
        self._img_worker.error.connect(self._on_img_error)
        self._img_worker.start()

    def _on_img_chunk(self, chunk: str):
        if self._img_streaming == "赫萝正在看图…":
            self._img_streaming = chunk
        else:
            self._img_streaming += chunk
        self._out.setPlainText(self._img_streaming)

    def _on_img_finished(self, full_text: str):
        self._set_buttons_enabled(True)
        try:
            self._parent_dialog.thinking_changed.emit("idle")
        except Exception:
            pass
        self._out.setPlainText(full_text)
        self._img_streaming = ""
        self._drop_hint.setText("分析完成~ 再扔一张图给咱看看？ 🍎")

    def _on_img_error(self, err_msg: str):
        self._set_buttons_enabled(True)
        try:
            self._parent_dialog.thinking_changed.emit("idle")
        except Exception:
            pass
        self._out.setPlainText(
            f"分析失败：{err_msg}\n\n"
            f"小提示：\n"
            f"· 如果是模型不支持视觉，请在「助手设置」中切换支持视觉的模型\n"
            f"  （如 GPT-4o / Claude 3.5 Sonnet / Gemini 等）后重试。\n"
            f"· 如果是图片格式问题，请换一张 PNG / JPG 截图试试。"
        )
        self._img_streaming = ""
        self._drop_hint.setText("分析出错了，再试试？ 🍎")


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
        # 每张卡片点击后切换到「自由聊天」Tab，并把预填文本塞进输入框；
        # prefill 为空串时仅切换并聚焦输入框，等用户手动输入（如「攻略问答」）。
        # 发送流程复用聊天 Tab 的 btn_send，本页不直接调 LLM。
        grid = QGridLayout(); grid.setSpacing(14); grid.setContentsMargins(0, 4, 0, 0)
        cards = [
            ("icon_history.png", "📜  游戏攻略问答",
             "问赫萝任何游戏攻略，她会整理成「操作步骤清单」",
             "攻略问答",
             ""),
            ("icon_code.png",    "🔧  游戏问题诊断",
             "游戏卡顿/崩溃/黑屏/网络延迟？赫萝帮你排查",
             "问题诊断",
             "我的游戏遇到了问题：[请描述你的问题现象，赫萝帮你排查]"),
            ("icon_chat.png",    "📰  游戏资讯速递",
             "版本更新/活动公告/新闻速报，赫萝帮你整理",
             "资讯速递",
             "最近有什么值得关注的游戏新闻、版本更新或活动公告吗？"),
            ("icon_game.png",    "🎲  游戏推荐",
             "告诉赫萝你的喜好，她帮你挑下一款玩什么",
             "游戏推荐",
             "我喜欢玩[请填写你喜欢的游戏类型]，帮我推荐几款类似的游戏"),
        ]
        for i, (icn, title, desc, key, prefill) in enumerate(cards):
            r, c = divmod(i, 2)
            grid.addWidget(self._make_card(icn, title, desc, key, prefill), r, c)
        root.addLayout(grid, 1)

    def _make_card(self, icn: str, title: str, desc: str, key: str, prefill: str) -> QFrame:
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
        go.clicked.connect(lambda _=False, p=prefill: self._open_in_chat(p))
        v.addWidget(go)
        return f

    def _open_in_chat(self, prefill: str = "") -> None:
        """切换到「自由聊天」Tab，按需预填问题文本并聚焦输入框。

        复用 FullChatDialog 的聊天发送流程：用户编辑/确认后按回车或点击
        btn_send 即可触发 LLM 调用，本页不直接调 LLM。
        """
        dlg = self._parent_dialog
        # 切换到「自由聊天」Tab（QStackedWidget index 0），并同步左侧侧栏选中态
        if hasattr(dlg, "_switch_tab"):
            dlg._switch_tab("chat")  # 内部会 setCurrentIndex(0) 并聚焦 ed_input
        elif getattr(dlg, "_stack", None) is not None:
            dlg._stack.setCurrentIndex(0)
        side = getattr(dlg, "_side_buttons", {}).get("chat")
        if side is not None:
            side.setChecked(True)
        # 预填问题文本（空串 = 仅切换并聚焦，等用户手动输入）
        ed = getattr(dlg, "ed_input", None)
        if ed is not None:
            ed.setPlainText(prefill or "")
            try:
                ed.setFocus(Qt.OtherFocusReason)
                # 光标移到末尾，方便用户接着编辑模板里的占位符
                ed.setTextCursor(ed.document().end())
            except Exception:
                pass


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
        self._btn_code_bug = QPushButton("  \U0001F41B  查找 Bug"); self._btn_code_bug.setObjectName("holoPrimary"); self._btn_code_bug.setCursor(Qt.PointingHandCursor); self._btn_code_bug.setMinimumHeight(34)
        self._btn_code_bug.setIcon(icon("icon_code.png", 18)); self._btn_code_bug.setIconSize(QSize(18,18))
        self._btn_code_exp = QPushButton("  \U0001F4A1  解释代码"); self._btn_code_exp.setCursor(Qt.PointingHandCursor); self._btn_code_exp.setMinimumHeight(34)
        self._btn_code_exp.setIcon(icon("icon_chat.png", 18)); self._btn_code_exp.setIconSize(QSize(18,18))
        self._btn_code_ref = QPushButton("  \U0001FA84  重构风格"); self._btn_code_ref.setCursor(Qt.PointingHandCursor); self._btn_code_ref.setMinimumHeight(34)
        self._btn_code_ref.setIcon(icon("icon_retry.png", 18)); self._btn_code_ref.setIconSize(QSize(18,18))
        self._btn_code_bug.clicked.connect(lambda: self._run_code_llm("bug"))
        self._btn_code_exp.clicked.connect(lambda: self._run_code_llm("explain"))
        self._btn_code_ref.clicked.connect(lambda: self._run_code_llm("refactor"))
        self._code_worker = None
        self._code_streaming = ""
        btn_row.addWidget(self._btn_code_bug); btn_row.addWidget(self._btn_code_exp); btn_row.addWidget(self._btn_code_ref); btn_row.addStretch(1)
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

    def _run_code_llm(self, action: str):
        """代码助手 LLM 调用。"""
        code = self._code.toPlainText().strip()
        if not code:
            QMessageBox.information(self._parent_dialog, "提示", "赫萝：先贴点代码给咱看看嘛~")
            return
        from src.blueprint.llm.llm_worker import ChatWorker
        from src.blueprint.llm import prompt_builder

        action_text = {"bug": "请检查以下代码中的 Bug 和潜在问题，逐条列出：",
                       "explain": "请解释以下代码的功能和逻辑：",
                       "refactor": "请重构以下代码，提升可读性和性能，并说明改动点："}
        user_msg = action_text.get(action, "请分析以下代码：") + "\n\n```\n" + code + "\n```"

        sys_prompt = prompt_builder.build_code_prompt(_get_preset())
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_msg},
        ]

        self._code_streaming = "赫萝正在分析代码…"
        self._out.setPlainText(self._code_streaming)
        for btn in (self._btn_code_bug, self._btn_code_exp, self._btn_code_ref):
            btn.setEnabled(False)

        try:
            self._parent_dialog.thinking_changed.emit("thinking")
        except Exception:
            pass

        self._code_worker = ChatWorker(messages, stream=True, parent=self)
        self._code_worker.chunk_received.connect(self._on_code_chunk)
        self._code_worker.finished.connect(self._on_code_finished)
        self._code_worker.error.connect(self._on_code_error)
        self._code_worker.start()

    def _on_code_chunk(self, chunk: str):
        if self._code_streaming == "赫萝正在分析代码…":
            self._code_streaming = chunk
        else:
            self._code_streaming += chunk
        self._out.setPlainText(self._code_streaming)

    def _on_code_finished(self, full_text: str):
        for btn in (self._btn_code_bug, self._btn_code_exp, self._btn_code_ref):
            btn.setEnabled(True)
        try:
            self._parent_dialog.thinking_changed.emit("idle")
        except Exception:
            pass
        self._out.setPlainText(full_text)
        self._code_streaming = ""

    def _on_code_error(self, err_msg: str):
        for btn in (self._btn_code_bug, self._btn_code_exp, self._btn_code_ref):
            btn.setEnabled(True)
        try:
            self._parent_dialog.thinking_changed.emit("idle")
        except Exception:
            pass
        self._out.setPlainText(f"分析失败：{err_msg}")
        self._code_streaming = ""


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
        clr.clicked.connect(self._clear_all_sessions)
        hl.addWidget(clr); root.addLayout(hl)

        # 会话列表
        self._list = QListWidget(); self._list.setSpacing(6)
        root.addWidget(self._list, 1)

        # 从 ConversationStore 加载真实会话
        from src.blueprint.llm.conversation_store import ConversationStore
        store = ConversationStore()
        all_sessions = store.list_sessions()
        if all_sessions:
            samples = [(s.get("title", "未命名"), s.get("type", "chat"),
                        s.get("updated_at", ""), s.get("id", ""))
                       for s in all_sessions[:20]]
        else:
            samples = []  # 空列表，不显示假数据
        for title, sess_type, time_str, sess_id in samples:
            self._add_row(time_str, title, sess_type, sess_id)

        # 底栏统计
        stat = QLabel(f"共 {len(samples)} 条会话 · 最近 7 天"); stat.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:12px;")
        root.addWidget(stat)

    def _add_row(self, time_str: str, title: str, sess_type: str = "chat",
                 sess_id: str = ""):
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
        type_tag = "蓝图生成" if sess_type == "blueprint" else "自由聊天"
        t1 = QLabel(f"[{type_tag}]  {time_str}"); t1.setStyleSheet(f"background:transparent;color:{_THEME['text_sub']}; font-size:11px;")
        t2 = QLabel(title); t2.setWordWrap(True)
        t2.setStyleSheet(f"background:transparent;color:{_THEME['text_main']}; font-size:13px; font-weight:600;")
        mid.addWidget(t1); mid.addWidget(t2); h.addLayout(mid, 1)
        # 右：两按钮
        go = QPushButton("继续对话"); go.setObjectName("holoSuccess"); go.setCursor(Qt.PointingHandCursor)
        go.setIcon(icon("icon_chat.png", 14)); go.setIconSize(QSize(14,14)); go.setMinimumHeight(28)
        go.clicked.connect(lambda _=False, st=sess_type, sid=sess_id: self._parent_dialog._load_history_session(st, sid))
        dl = QPushButton(); dl.setIcon(icon("icon_clear.png", 14)); dl.setIconSize(QSize(14,14))
        dl.setFixedSize(32, 28); dl.setCursor(Qt.PointingHandCursor)
        dl.setToolTip("删除这次会话")
        dl.clicked.connect(lambda _=False, sid=sess_id, it=item: self._on_delete_session(sid, it))
        h.addWidget(go); h.addWidget(dl)
        self._list.setItemWidget(item, w)

    def _on_delete_session(self, sess_id: str, item):
        """删除一条历史会话（先确认，再从磁盘和列表移除）。"""
        r = QMessageBox.question(
            self._parent_dialog, "删除会话？", "确定删除这次会话吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if r != QMessageBox.Yes:
            return
        from src.blueprint.llm.conversation_store import ConversationStore
        store = ConversationStore()
        store.delete_session(sess_id)
        row = self._list.row(item)
        if row >= 0:
            self._list.takeItem(row)

    def _clear_all_sessions(self):
        """清空全部历史会话。"""
        r = QMessageBox.question(
            self._parent_dialog, "⚠", "确定要清空所有历史会话吗？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if r != QMessageBox.Yes:
            return
        from src.blueprint.llm.conversation_store import ConversationStore
        store = ConversationStore()
        for s in store.list_sessions():
            store.delete_session(s.get("id", ""))
        self._list.clear()


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
    thinking_changed = Signal(str)  # "thinking"=开始思考, "idle"=结束
    blueprint_imported = Signal(dict)  # 蓝图 JSON 导入信号

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent, Qt.Window | Qt.WindowCloseButtonHint | Qt.WindowMinimizeButtonHint)
        self.setObjectName("FullChatDialog")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setWindowTitle("赫萝 · 你的全能女仆助理 🐺🍎")
        self.resize(980, 680)
        self.setMinimumSize(820, 540)
        self.setStyleSheet(_QSS)
        self._side_buttons: dict[str, QPushButton] = {}
        self._chat_worker = None
        self._bp_worker = None
        self._streaming_bubble = None  # 流式输出中的 assistant 气泡
        self._streaming_text = ""      # 流式累积文本
        self._bp_streaming_bubble = None
        self._bp_streaming_text = ""
        self._last_bp_user_text = ""  # 上次蓝图请求文本（重写用）
        from src.blueprint.llm.conversation_store import ConversationStore
        self._conv_store = ConversationStore()
        self._current_chat_sess_id = None
        self._current_bp_sess_id = None
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

        # -------- 右侧：QStackedWidget（7 个 Tab 对应 7 页） --------
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
        # 打开下拉前刷新，确保能看到最新存档的会话
        self._menu_bp_history.aboutToShow.connect(
            lambda m=self._menu_bp_history: self._build_bp_history_menu(m))
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

    def _append_chat_msg_ex(self, role: str, text: str) -> ChatBubble:
        """追加消息并返回 bubble 引用（用于流式更新）。"""
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
        return bubble

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

    def _append_bp_msg_ex(self, role: str, text: str) -> ChatBubble:
        """追加蓝图消息并返回 bubble 引用（用于流式更新）。"""
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
        # 接线导入/重写按钮
        bubble.import_clicked.connect(lambda b=bubble: self._on_bp_import(b))
        bubble.retry_clicked.connect(lambda b=bubble: self._on_bp_retry(b))
        QTimer.singleShot(20, lambda: list_w.scrollToBottom())
        return bubble

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
        self._start_chat_llm(text)

    def _start_chat_llm(self, user_text: str):
        """启动自由聊天 LLM 调用。"""
        from src.blueprint.llm.llm_worker import ChatWorker
        from src.blueprint.llm import prompt_builder

        # 构建消息列表
        sys_prompt = prompt_builder.build_chat_prompt(_get_preset(), user_query=user_text)
        history = self.get_chat_messages()
        messages = [{"role": "system", "content": sys_prompt}]
        for role, text in history:
            if role == "user":
                messages.append({"role": "user", "content": text})
            elif role == "assistant":
                messages.append({"role": "assistant", "content": text})

        # 创建占位气泡（流式更新）
        self._streaming_text = "赫萝正在思考…"
        self._streaming_bubble = self._append_chat_msg_ex("assistant", self._streaming_text)

        # 启动 worker
        self.thinking_changed.emit("thinking")
        self._chat_worker = ChatWorker(messages, stream=True, parent=self)
        self._chat_worker.chunk_received.connect(self._on_chat_chunk)
        self._chat_worker.finished.connect(self._on_chat_finished)
        self._chat_worker.error.connect(self._on_chat_error)
        self._chat_worker.start()

    def _on_chat_chunk(self, chunk: str):
        """流式回调：逐 chunk 更新气泡。"""
        if self._streaming_bubble is None:
            return
        if self._streaming_text == "赫萝正在思考…":
            self._streaming_text = chunk
        else:
            self._streaming_text += chunk
        self._streaming_bubble.update_text(self._streaming_text)

    def _on_chat_finished(self, full_text: str):
        """LLM 完成。"""
        self.btn_send.setEnabled(True)
        self.btn_send.setToolTip("发送给赫萝 🍎")
        self.thinking_changed.emit("idle")
        if self._streaming_bubble is not None:
            self._streaming_bubble.update_text(full_text)
            self._streaming_bubble = None
        self._streaming_text = ""
        # 结果卡片
        try:
            card_layout = self.result_card.findChild(QVBoxLayout)
            if card_layout is not None and card_layout.count() >= 2:
                w = card_layout.itemAt(1).widget()
                if isinstance(w, QLabel):
                    w.setText(
                        f"🗒 类型：自由回复 &nbsp; 回复字数：{len(full_text)} 字<br>"
                        f"<span style='color:{_THEME['text_sub']};'>💡 提示：可复制回复内容，或在下方继续对话。</span>"
                    )
        except Exception:
            pass
        self.result_card.show()
        # 自动保存会话（受 remember_ctx 记忆开关控制）
        try:
            from src.config.user_settings import get_llm_memory
            mem = get_llm_memory()
            if mem.get("remember_ctx", True):
                history = self.get_chat_messages()
                msgs = [{"role": r, "content": t} for r, t in history]
                self._current_chat_sess_id = self._conv_store.auto_save_current("chat", msgs)
        except Exception:
            pass

    def _on_chat_error(self, err_msg: str):
        """LLM 调用失败。"""
        self.btn_send.setEnabled(True)
        self.btn_send.setToolTip("发送给赫萝 🍎")
        self.thinking_changed.emit("idle")
        if self._streaming_bubble is not None:
            self._streaming_bubble.update_text(f"呜……出了点问题：\n\n{err_msg}")
            self._streaming_bubble = None
        self._streaming_text = ""

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
        self._start_bp_llm(text)

    def _start_bp_llm(self, user_text: str):
        """启动蓝图生成 LLM 调用。"""
        from src.blueprint.llm.llm_worker import ChatWorker
        from src.blueprint.llm import prompt_builder

        self._last_bp_user_text = user_text
        sys_prompt = prompt_builder.build_blueprint_prompt(_get_preset(), user_query=user_text)
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ]

        # 占位气泡
        self._bp_streaming_text = "赫萝正在生成蓝图…"
        self._bp_streaming_bubble = self._append_bp_msg_ex("assistant", self._bp_streaming_text)

        self.thinking_changed.emit("thinking")
        self._bp_worker = ChatWorker(messages, stream=True, parent=self)
        self._bp_worker.chunk_received.connect(self._on_bp_chunk)
        self._bp_worker.finished.connect(self._on_bp_finished)
        self._bp_worker.error.connect(self._on_bp_error)
        self._bp_worker.start()

    def _on_bp_chunk(self, chunk: str):
        if self._bp_streaming_bubble is None:
            return
        if self._bp_streaming_text == "赫萝正在生成蓝图…":
            self._bp_streaming_text = chunk
        else:
            self._bp_streaming_text += chunk
        self._bp_streaming_bubble.update_text(self._bp_streaming_text)

    def _on_bp_finished(self, full_text: str):
        self.btn_bp_send.setEnabled(True)
        self.btn_bp_send.setToolTip("发送蓝图需求给赫萝 📋")
        self.thinking_changed.emit("idle")
        if self._bp_streaming_bubble is not None:
            self._bp_streaming_bubble.update_text(full_text)
            self._bp_streaming_bubble = None
        self._bp_streaming_text = ""
        self.result_bp_card.show()

        # 蓝图校验闭环：解析 JSON → 校验 → 失败自动重试
        bp_json = self._extract_bp_json(full_text)
        if bp_json is not None:
            # R1 JSON 解析成功 → R2+R3 结构校验
            errors = self._validate_bp_json(bp_json)
            if errors:
                # 校验失败，尝试自动修复
                self._bp_attempt = getattr(self, "_bp_attempt", 0) + 1
                max_attempts = self._get_bp_max_attempts()
                if self._bp_attempt < max_attempts:
                    # 自动重试：把错误信息反馈给 LLM
                    self._retry_bp_with_errors(full_text, errors)
                    return
                else:
                    # 超过最大重试次数，告知用户
                    err_summary = "\n".join(f"  · {e}" for e in errors[:5])
                    self._append_bp_msg_ex(
                        "assistant",
                        f"⚠️ 蓝图校验未通过（已尝试 {self._bp_attempt} 次）：\n\n{err_summary}\n\n"
                        f"可以点击「重试」让赫萝再试一次，或修改需求描述后重新发送。"
                    )
            else:
                # 校验通过，绑定 JSON 到最后一条气泡
                self._bind_bp_json_to_last_bubble(bp_json)
        # 重置重试计数
        self._bp_attempt = 0

        # 自动保存会话（受 remember_ctx 记忆开关控制）
        try:
            from src.config.user_settings import get_llm_memory
            mem = get_llm_memory()
            if mem.get("remember_ctx", True):
                bp_msgs = []
                for i in range(self.list_bp.count()):
                    item = self.list_bp.item(i)
                    w = self.list_bp.itemWidget(item)
                    if w is not None and hasattr(w, "_role") and hasattr(w, "_text"):
                        bp_msgs.append({"role": w._role, "content": w._text})
                self._current_bp_sess_id = self._conv_store.auto_save_current("blueprint", bp_msgs)
        except Exception:
            pass

    def _get_bp_max_attempts(self) -> int:
        """从 llm_defaults.json 读取蓝图最大尝试次数。"""
        try:
            from src.blueprint.llm.prompt_builder import _load_llm_defaults
            defaults = _load_llm_defaults()
            return max(1, int(defaults.get("blueprint", {}).get("max_attempts", 3)))
        except Exception:
            return 3

    def _validate_bp_json(self, bp_json: dict) -> list:
        """校验蓝图 JSON 结构，返回错误列表（空列表=通过）。

        R1: JSON 语法（已在外层 _extract_bp_json 完成）
        R2: 节点类型 + 参数合法性
        R3: 连线合法性（exec→exec / Start 唯一）
        """
        errors = []

        nodes = bp_json.get("nodes", {})
        links = bp_json.get("links", [])

        if not isinstance(nodes, dict) or len(nodes) == 0:
            errors.append("nodes 为空或格式错误")
            return errors

        # R2: 检查节点类型合法性
        try:
            from src.blueprint.core.node_library import NODE_TYPES_ORDERED
            valid_types = set(NODE_TYPES_ORDERED)
        except Exception:
            valid_types = set()

        start_count = 0
        for nid, node in nodes.items():
            if not isinstance(node, dict):
                errors.append(f"节点 {nid} 格式错误")
                continue
            ntype = node.get("type", "")
            if valid_types and ntype not in valid_types:
                errors.append(f"节点 {nid} 类型 '{ntype}' 不存在")
            if ntype == "Start":
                start_count += 1
            # 检查 id 一致性
            if node.get("id", "") != nid:
                errors.append(f"节点 {nid} 的 id 字段不匹配")

        if start_count == 0:
            errors.append("缺少 Start 节点")
        elif start_count > 1:
            errors.append(f"存在 {start_count} 个 Start 节点（必须唯一）")

        # R3: 检查连线合法性
        for i, link in enumerate(links):
            if not isinstance(link, dict):
                errors.append(f"连线 #{i} 格式错误")
                continue
            from_node = link.get("from_node", "")
            to_node = link.get("to_node", "")
            if from_node not in nodes:
                errors.append(f"连线 #{i} 起始节点 '{from_node}' 不存在")
            if to_node not in nodes:
                errors.append(f"连线 #{i} 目标节点 '{to_node}' 不存在")
            ltype = link.get("type", "")
            if ltype not in ("exec", "data"):
                errors.append(f"连线 #{i} 类型 '{ltype}' 无效（只能 exec 或 data）")

        # 尝试用 runtime validator 做更深层校验
        try:
            from src.blueprint.core.models import BlueprintGraph
            from src.blueprint.runtime.validator import validate_graph
            graph = BlueprintGraph.from_dict(bp_json)
            issues = validate_graph(graph)
            for issue in issues:
                if issue.level == "ERROR":
                    errors.append(issue.message)
        except Exception:
            pass  # 深层校验失败不阻断

        return errors

    def _retry_bp_with_errors(self, prev_response: str, errors: list):
        """校验失败后，把错误信息反馈给 LLM 自动重试。"""
        from src.blueprint.llm.llm_worker import ChatWorker
        from src.blueprint.llm import prompt_builder

        err_text = "\n".join(f"  · {e}" for e in errors[:8])
        retry_msg = (
            f"你上次生成的蓝图有以下校验错误：\n\n{err_text}\n\n"
            f"请修正这些问题，重新生成完整的蓝图 JSON。只输出 JSON，不要附加解释。"
        )

        sys_prompt = prompt_builder.build_blueprint_prompt(_get_preset(), user_query=self._last_bp_user_text)
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": self._last_bp_user_text},
            {"role": "assistant", "content": prev_response},
            {"role": "user", "content": retry_msg},
        ]

        # 占位气泡
        self._bp_streaming_text = f"赫萝正在修正蓝图（第 {self._bp_attempt + 1} 次尝试）…"
        self._bp_streaming_bubble = self._append_bp_msg_ex("assistant", self._bp_streaming_text)

        self.thinking_changed.emit("thinking")
        self._bp_worker = ChatWorker(messages, stream=True, parent=self)
        self._bp_worker.chunk_received.connect(self._on_bp_chunk)
        self._bp_worker.finished.connect(self._on_bp_finished)
        self._bp_worker.error.connect(self._on_bp_error)
        self._bp_worker.start()

    def _bind_bp_json_to_last_bubble(self, bp_json: dict):
        """校验通过后，把 JSON 绑定到蓝图列表最后一条助手气泡。"""
        for i in range(self.list_bp.count() - 1, -1, -1):
            item = self.list_bp.item(i)
            w = self.list_bp.itemWidget(item)
            if w is not None and hasattr(w, "_role") and w._role == "assistant":
                w._bp_json = bp_json
                break

        # autosave_draft：自动保存校验通过的草稿
        try:
            from src.blueprint.llm.prompt_builder import _load_llm_defaults
            defaults = _load_llm_defaults()
            if defaults.get("blueprint", {}).get("autosave_draft", True):
                import json as _json
                import os
                import time
                draft_dir = os.path.join(os.path.expanduser("~"), ".blueprintautomation", "llm_bp_drafts")
                os.makedirs(draft_dir, exist_ok=True)
                draft_name = f"draft_{int(time.time())}.json"
                draft_path = os.path.join(draft_dir, draft_name)
                with open(draft_path, "w", encoding="utf-8") as f:
                    _json.dump(bp_json, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _extract_bp_json(self, text: str):
        """从 LLM 回复中提取蓝图 JSON，失败返回 None。"""
        import json as _json
        import re
        # 尝试从 ```json ... ``` 代码块提取
        m = re.search(r'```(?:json)?\s*\n(.*?)\n```', text, re.DOTALL)
        raw = m.group(1) if m else text.strip()
        try:
            d = _json.loads(raw)
            if isinstance(d, dict) and "nodes" in d:
                return d
        except _json.JSONDecodeError:
            pass
        # 尝试找第一个 { 到最后一个 } 的子串
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            try:
                d = _json.loads(raw[start:end+1])
                if isinstance(d, dict) and "nodes" in d:
                    return d
            except _json.JSONDecodeError:
                pass
        return None

    def _on_bp_import(self, bubble):
        """导入按钮：将蓝图 JSON 发送给主窗口加载到画布。"""
        bp_json = getattr(bubble, "_bp_json", None)
        if bp_json is None:
            QMessageBox.warning(self, "无法导入", "赫萝：这段回复里没有找到有效的蓝图 JSON 哦~\n可以让赫萝重新生成一次试试 🍎")
            return
        self.blueprint_imported.emit(bp_json)
        QMessageBox.information(self, "✅ 已导入", f"赫萝：蓝图已导入画布！\n节点数：{len(bp_json.get('nodes', {}))}\n连线数：{len(bp_json.get('links', []))}")

    def _on_bp_retry(self, bubble):
        """重写按钮：用上次的用户需求重新生成。"""
        if not self._last_bp_user_text:
            QMessageBox.information(self, "提示", "赫萝：没有找到上次的需求文本呢~")
            return
        self._start_bp_llm(self._last_bp_user_text)

    def _on_bp_error(self, err_msg: str):
        self.btn_bp_send.setEnabled(True)
        self.btn_bp_send.setToolTip("发送蓝图需求给赫萝 📋")
        self.thinking_changed.emit("idle")
        if self._bp_streaming_bubble is not None:
            self._bp_streaming_bubble.update_text(f"呜……蓝图生成失败：\n\n{err_msg}")
            self._bp_streaming_bubble = None
        self._bp_streaming_text = ""

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
    # 会话管理：对话历史持久化（ConversationStore）+ 下拉菜单 + 切换加载
    # 自由聊天 / 蓝图生成 的会话在 LLM 回复完成后自动存档到磁盘 JSON，
    # 历史页和下拉菜单都从 ConversationStore 实时读取。
    # =================================================================

    def _build_chat_history_menu(self, menu: QMenu):
        """构造「自由聊天」Tab 的历史会话下拉菜单（点击直接切换，不跳转到 history 页）。"""
        menu.clear()
        # 菜单头
        header = menu.addAction("📚 自由聊天·历史会话")
        header.setEnabled(False)
        menu.addSeparator()
        # 从 ConversationStore 读取自由聊天类型的历史会话
        sessions = self._conv_store.list_sessions("chat")
        for s in sessions[:30]:
            sess_id = s.get("id", "")
            title = s.get("title", "未命名")
            action = QAction(title, menu)
            action.setToolTip("点击直接在当前自由聊天 Tab 加载该会话")
            action.triggered.connect(lambda _=False, sid=sess_id: self._load_history_session("chat", sid))
            menu.addAction(action)
        if not sessions:
            empty = menu.addAction("（暂无历史会话）")
            empty.setEnabled(False)
        menu.addSeparator()
        tip = menu.addAction("💡 提示：发送消息后会自动存档到这里")
        tip.setEnabled(False)

    def _build_bp_history_menu(self, menu: QMenu):
        """构造「蓝图生成」Tab 的历史会话下拉菜单（点击直接切换，不跳转到 history 页）。"""
        menu.clear()
        header = menu.addAction("📚 蓝图生成·历史会话")
        header.setEnabled(False)
        menu.addSeparator()
        # 从 ConversationStore 读取蓝图类型的历史会话
        sessions = self._conv_store.list_sessions("blueprint")
        for s in sessions[:30]:
            sess_id = s.get("id", "")
            title = s.get("title", "未命名")
            action = QAction(title, menu)
            action.setToolTip("点击直接在当前蓝图 Tab 加载该会话草稿")
            action.triggered.connect(lambda _=False, sid=sess_id: self._load_history_session("blueprint", sid))
            menu.addAction(action)
        if not sessions:
            empty = menu.addAction("（暂无历史会话）")
            empty.setEnabled(False)
        menu.addSeparator()
        tip = menu.addAction("💡 提示：选择后可点气泡右下角「➕ 导入」直接写入画布")
        tip.setEnabled(False)

    def _load_history_session(self, hist_type: str, sess_id: str):
        """加载一条历史会话到对应的 Tab 聊天列表里。
        hist_type: 'chat' -> 自由聊天 list_msg；'blueprint' -> 蓝图生成 list_bp
        """
        from src.blueprint.llm.conversation_store import ConversationStore
        store = ConversationStore()
        data = store.load_session(sess_id)
        if not data or "messages" not in data:
            QMessageBox.information(
                self, "提示", "赫萝：找不到这个会话呢~ 🐺"
            )
            return
        # 切到对应 Tab（历史会话可能从别的 Tab 点，但菜单只在对应 Tab 上显示，这里加个保险）
        if hist_type == "chat":
            self._side_buttons["chat"].setChecked(True)
            self._switch_tab("chat")
            list_w = self.list_msg
            append_fn = self._append_chat_msg
            self.result_card.hide()
            self._current_chat_sess_id = sess_id
        else:
            self._side_buttons["blueprint"].setChecked(True)
            self._switch_tab("blueprint")
            list_w = self.list_bp
            append_fn = self._append_bp_msg
            self.result_bp_card.hide()
            self._current_bp_sess_id = sess_id
        # 先清空当前列表，再逐条追加历史消息
        list_w.clear()
        for m in data["messages"]:
            role = m.get("role", "user")
            text = m.get("content", "")
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

