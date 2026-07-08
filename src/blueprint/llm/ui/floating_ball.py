"""
全局桌面悬浮球（系统级置顶，压住游戏窗口显示）。

设计要点（Windows 特调，保证主窗口最小化后仍显示 + 游戏全屏时压在上面）：

  1. **不设 parent** —— parent=None 才能成为独立顶层窗口。
     如果把主窗口当 parent，主窗口最小化/隐藏时 Qt 会连带隐藏 Tool 类型的子窗口，
     而且 Z-order 会跟着父窗口被游戏盖下去。

  2. **窗口 flags 组合 = Qt.Window（独立窗口） + FramelessWindowHint（无边框）
                 + WindowStaysOnTopHint（Qt 层 hint 置顶）
                 + Qt.Tool（在任务栏/Alt+Tab 默认不出现，但需要 WS_EX_TOOLWINDOW 兜底）。**

  3. **Win32 API 双保险** —— 直接调 user32.SetWindowPos(..., HWND_TOPMOST, ...)：
     - 初始化时设一次
     - QTimer 每 500ms 重刷一次（防游戏运行时引擎把自己切到 HWND_TOPMOST 盖过我们）。
     这是"压过游戏全屏窗口"最稳的做法——普通 WindowStaysOnTopHint 只是 Qt 的"建议"，
     游戏（尤其是 Unity/UE4/DirectX 独占全屏）会用 Win32 API 抢 TOPMOST，
     我们必须也用同等手段，且定时刷新。

  4. **WS_EX_TOOLWINDOW + WS_EX_NOACTIVATE** ——
     - WS_EX_TOOLWINDOW：让独立顶层窗口不出现在 Alt+Tab 列表、不占任务栏
     - WS_EX_NOACTIVATE：点悬浮球时不抢前台焦点（用户切回游戏/目标窗口不会被打断）

  5. **Linux/macOS 兼容**：没有 ctypes.windll 时走 Qt 默认 StaysOnTopHint，不会崩溃
     （只是达不到"压过游戏全屏"的效果，属于合理降级）。

交互：
  * 左键单击（无拖动） —— 打开完整 FullChatDialog
  * 右键单击 —— 在悬浮球旁弹出 QuickChatBubble（自动避屏边界，不遮挡球）
  * 左键拖动 —— 移动悬浮球（无吸附，停在哪就在哪）
"""
from __future__ import annotations

import sys
from typing import Optional

from PySide6.QtCore import (
    QByteArray,
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
    Property,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QLinearGradient,
    QMovie,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import QApplication, QToolTip, QWidget

import os

from .full_chat_dialog import FullChatDialog
from .quick_chat_bubble import QuickChatBubble
from ._holo_theme import get_theme
from ._icon_loader import pixmap as _icon_pixmap

_THEME = get_theme("day")  # 固定奶茶白日主题

# ====================================================================
# 人物动画资源路径（相对于项目根目录 images/）
#   仅使用 Animated WebP：文件更小、alpha 通道平滑（8-bit，边缘无锯齿）
#   加载失败 → fallback 到 🐺 emoji 自绘（赫萝贤狼）
# ====================================================================
def _resolve_img_dir() -> str:
    try:
        from src.config.app_paths import INTERNAL_DIR
        internal = os.path.normpath(os.path.join(INTERNAL_DIR, "images"))
        if os.path.isdir(internal):
            return internal
    except Exception:
        pass
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "..", "..", "images"))


_IMG_DIR = _resolve_img_dir()
_ANIM_AVATAR_WEBP = os.path.join(_IMG_DIR, "沉思女仆赫萝.webp")
_ANIM_THINKING_WEBP = os.path.join(_IMG_DIR, "搬砖女仆赫萝.webp")
# 动画人物在 56x56 球里的绘制尺寸（留 6px 的渐变球边缘，不要顶满）
_AVATAR_DRAW_SIZE = QSize(44, 44)


# ====================================================================
# Win32 常量（仅 Windows 下用 ctypes 调）
# ====================================================================
_HWND_TOPMOST = -1
_HWND_NOTOPMOST = -2

_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_SWP_SHOWWINDOW = 0x0040

_GWL_EXSTYLE = -20

_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_NOACTIVATE = 0x08000000
_WS_EX_LAYERED = 0x00080000
_WS_EX_TOPMOST = 0x00000008


def _is_windows() -> bool:
    return sys.platform.startswith("win") or sys.platform == "cygwin"


def _win32() -> Optional[object]:
    """拿到 user32.dll 句柄，非 Windows 返回 None。"""
    if not _is_windows():
        return None
    try:
        import ctypes
        return ctypes.windll.user32
    except Exception:
        return None


class FloatingBall(QWidget):
    """系统级置顶悬浮球。parent 必须是 None（见文件头说明）。"""

    user_message = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None):
        # ============================================================
        # 窗口 Flags —— 关键：独立 Window（不是 Tool），但配合 WS_EX_TOOLWINDOW 藏任务栏
        # ============================================================
        flags = (
            Qt.Window                  # ← 独立顶层窗口（不依附任何 parent）
            | Qt.FramelessWindowHint    # ← 无边框
            | Qt.WindowStaysOnTopHint   # ← Qt "建议"置顶（Linux/macOS 兜底）
            | Qt.WindowDoesNotAcceptFocus  # ← 自身不抢焦点（防游戏被切走）
            | Qt.NoDropShadowWindowHint
        )
        # 注意：**不使用 Qt.Tool**。Qt.Tool 在传 parent 时是"子工具窗口"，
        # 会跟随父窗口隐藏；parent=None 时 Tool 虽然能用，
        # 但跟 WS_EX_TOOLWINDOW 搭配起来用更纯的 Window 更稳定。
        super().__init__(parent, flags)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_AlwaysStackOnTop)
        try:
            # 告诉 Qt "我们不接收焦点"，配合 WS_EX_NOACTIVATE 双重保险
            self.setFocusPolicy(Qt.NoFocus)
        except Exception:
            pass
        self.setWindowTitle("BA AI FloatingBall")

        # ---- 尺寸/位置 ----
        self._size = 56
        self.setFixedSize(QSize(self._size, self._size))
        self._default_margin = 24
        self._move_to_default_bottom_right()

        # ---- 状态 ----
        self._drag_pos: Optional[QPoint] = None
        self._press_global_pos: Optional[QPoint] = None
        self._did_drag: bool = False
        self._hovered = False
        self._opacity_val = 0.0
        # ---- 赫萝 2 状态：idle / thinking
        self._state: str = "idle"
        self._bounce_offset: float = 0.0
        self._rot_tick: int = 0
        self._state_timer: Optional[QTimer] = None
        self._setup_state_anim()

        # ---- 子窗口引用（避免被 GC）----
        self._full_dlg: Optional[FullChatDialog] = None
        self._quick_bubble: Optional[QuickChatBubble] = None

        # ---- 动画人物（QMovie）—— 仅 Animated WebP，加载失败就 emoji 兜底 ----
        self._movie: Optional[QMovie] = None
        self._movie_thinking: Optional[QMovie] = None
        self._movie_fallback_emoji = False
        self._setup_avatar_movie()

        # ---- 动画 ----
        self._anim: Optional[QPropertyAnimation] = None
        # ★ Win32 句柄必须在 setVisible(True) 之前初始化，因为 setVisible 会触发 showEvent，
        #   showEvent 里调度了 _apply_win32_styles_once 的 QTimer，如果此时 _win32 还不存在就会崩溃
        self._win32 = _win32()
        self._hwnd: Optional[int] = None
        self.setVisible(True)
        QTimer.singleShot(60, self._fade_in)
        QToolTip.setFont(QFont("Microsoft YaHei", 9))
        self._refresh_holo_tip()

        # ============================================================
        # 【核心】Win32 层的 TOPMOST 双保险
        # ============================================================
        # _win32 / _hwnd 已在 setVisible(True) 之前初始化
        # QTimer：每 220ms 刷新一次 HWND_TOPMOST（防游戏/新打开程序抢置顶）
        self._topmost_timer = QTimer(self)
        self._topmost_timer.setInterval(220)
        self._topmost_timer.timeout.connect(self._ensure_topmost_win32)
        self._topmost_timer.start()

        # 窗口真正显示到屏幕上后（show 之后会 polish 产生真实 HWND），再调一次 Win32 API
        QTimer.singleShot(100, self._apply_win32_styles_once)
        QTimer.singleShot(220, self._ensure_topmost_win32)

        # 【焦点变化补钉】用户切换/新开窗口时，系统会重排 TOPMOST Z-order，趁刚切换完立刻补一次
        #   这是解决「后开的程序能盖在悬浮球上面」最直接的一击——用户一切过去就顶回来
        try:
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is not None and hasattr(app, "focusWindowChanged"):
                app.focusWindowChanged.connect(lambda *_: self._ensure_topmost_win32())
        except Exception:
            pass

    # =================================================================
    # Win32 样式 / TopMost 双保险核心函数
    # =================================================================
    def _win_hwnd(self) -> Optional[int]:
        """拿到当前窗口的 Win32 HWND（整数句柄）。"""
        try:
            if self._hwnd is None:
                hwnd_raw = int(self.winId())
                # PySide6 的 winId() 在 Windows 下直接就是 HWND
                # （WId -> HWND，Qt6 不再需要经过 QWindow::handle()）
                self._hwnd = hwnd_raw
            return self._hwnd
        except Exception:
            return None

    def _apply_win32_styles_once(self):
        """第一次显示后，SetWindowLong 追加扩展样式：
        WS_EX_TOOLWINDOW（不出现在 Alt+Tab）+ WS_EX_NOACTIVATE（点我不抢焦点）。
        """
        u32 = getattr(self, "_win32", None)
        hwnd = self._win_hwnd()
        if u32 is None or hwnd is None:
            return
        try:
            import ctypes
            GWL_EXSTYLE = _GWL_EXSTYLE
            # 1. 读取当前扩展样式
            old_style = int(
                ctypes.c_ssize_t(u32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)).value
                if hasattr(u32, "GetWindowLongPtrW")
                else u32.GetWindowLongW(hwnd, GWL_EXSTYLE)
            )
            new_style = (
                old_style
                | _WS_EX_TOPMOST        # ⭐ Win32 层硬标记「我是 TOPMOST 类窗口」—— 缺失这一位会导致新弹出的 TOPMOST 窗口能压到我上面
                | _WS_EX_TOOLWINDOW     # 不在 Alt+Tab / 任务栏出现
                | _WS_EX_NOACTIVATE     # 点击/拖动悬浮球时不抢焦点/激活
                | _WS_EX_LAYERED        # 配合 WA_TranslucentBackground 让透明像素不挡点击（可选）
            )
            # 2. 写回扩展样式
            if hasattr(u32, "SetWindowLongPtrW"):
                u32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE, ctypes.c_ssize_t(new_style))
            else:
                u32.SetWindowLongW(hwnd, GWL_EXSTYLE, new_style)
        except Exception:
            pass

    def _ensure_topmost_win32(self):
        """核心：SetWindowPos(HWND_TOPMOST) 把悬浮球钉在所有窗口（包括游戏全屏）之上。

        - 游戏运行时，Unity/UE4 等引擎、以及用户新开的任意程序，都可能把自己的主窗口设为 HWND_TOPMOST，
          我们必须以较高频率（每 220ms 就再钉一次）+ 焦点变化回调 才能"压得住"。
        - SWP_NOMOVE | SWP_NOSIZE：保持当前位置/大小不变，只改 Z-order。
        - SWP_NOACTIVATE：关键——不要让游戏失去焦点！
        - SWP_SHOWWINDOW：如果被系统藏了就再放出来。
        - Qt 层兜底：同步调 raise_()，并重新确认 WindowStaysOnTopHint flag（某些游戏的反作弊驱动会偷偷抹掉 Qt flag）。
        """
        u32 = getattr(self, "_win32", None)
        hwnd = self._win_hwnd()
        if u32 is None or hwnd is None:
            return
        if not self.isVisible():
            return
        try:
            # ① Qt 层：先确认 Flag 没被反作弊/其他程序抹掉（罕见，但遇到过）
            cur_flags = self.windowFlags()
            if not (cur_flags & Qt.WindowStaysOnTopHint):
                self.setWindowFlags(cur_flags | Qt.WindowStaysOnTopHint)
                # 改 flag 后必须重新 show（hide→show 会重造 HWND，这里只在缺失时调，频率极低）
                try: self.show(); self.raise_()
                except Exception: pass
            else:
                # ② Qt 层：普通 raise（不激活，只调 Z-order）
                try: self.raise_()
                except Exception: pass
            # ③ Win32 层：硬钉 TOPMOST（核心）
            u32.SetWindowPos(
                hwnd,
                _HWND_TOPMOST,
                0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE | _SWP_SHOWWINDOW,
            )
        except Exception:
            pass

    def _unset_topmost_win32(self):
        """显式撤销 TOPMOST（销毁时调用，避免句柄泄漏残留的置顶状态）。"""
        u32 = getattr(self, "_win32", None)
        hwnd = self._win_hwnd()
        if u32 is None or hwnd is None:
            return
        try:
            u32.SetWindowPos(
                hwnd,
                _HWND_NOTOPMOST,
                0, 0, 0, 0,
                _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE,
            )
        except Exception:
            pass

    # =================================================================
    # 生命周期
    # =================================================================
    def _setup_avatar_movie(self):
        """加载 Animated WebP 人物动画，失败 fallback 到 🐺 emoji 自绘（赫萝贤狼）。"""
        self._movie = self._load_webp_movie(_ANIM_AVATAR_WEBP)
        self._movie_thinking = self._load_webp_movie(_ANIM_THINKING_WEBP)
        if self._movie is None:
            self._movie_fallback_emoji = True

    def _load_webp_movie(self, path: str) -> Optional[QMovie]:
        """加载单个 WebP 动画，失败返回 None。"""
        if not os.path.isfile(path):
            return None
        try:
            m = QMovie(path, QByteArray(), self)
            m.start()
            if not m.currentPixmap().isNull():
                try:
                    m.frameChanged.connect(lambda _n: self.update())
                except Exception:
                    m.frameChanged.connect(self.update)
                return m
            m.stop()
            try:
                m.deleteLater()
            except Exception:
                pass
        except Exception:
            pass
        return None

    # =================================================================
    # 赫萝 2 状态伪动画 + ToolTip
    # =================================================================
    STATE_TIPS = {
        "idle":     "🐺 赫萝待命：唔姆，苹果商人~ 点左键聊天，右键弹快速气泡 ✨",
        "thinking": "🤔 赫萝搬砖：唔…让咱捋捋…（稍等哦，马上给你答复 🍎）",
    }
    STATE_PRIMARY = {  # 对应不同状态下圆球的发色
        "idle":     ("#FFD89A", "#C97F35"),   # 正常发色渐变（赫萝金棕）
        "thinking": ("#B9A8D4", "#6B5B73"),   # 狼尾灰紫（严肃思考/搬砖）
    }
    STATE_HOVER = {    # hover 时的渐变色（稍亮）
        "idle":     ("#FFE1C2", "#E8A95C"),
        "thinking": ("#D4C5EE", "#B9A8D4"),
    }

    def _setup_state_anim(self):
        """启动一个 80ms 的定时器，持续驱动轻微浮动 + 状态旋转（伪动画）。"""
        t = QTimer(self); t.setInterval(80); t.timeout.connect(self._tick_state_anim)
        t.start(); self._state_timer = t

    def _tick_state_anim(self):
        self._rot_tick += 1
        # 不做上下浮动动画，头像静止不动
        self._bounce_offset = 0.0
        self.update()

    def set_avatar_state(self, state: str):
        """外部调用：切赫萝 2 状态（idle / thinking）。
        未知名 → idle；切 ToolTip；立刻刷新。
        """
        if state not in self.STATE_TIPS:
            state = "idle"
        if state != self._state:
            self._state = state
            self._refresh_holo_tip()
        self.update()

    def _refresh_holo_tip(self):
        tip = self.STATE_TIPS.get(self._state, self.STATE_TIPS["idle"])
        tip += (
            "\n━━━━━━━━━━━━━━━━━━"
            "\n🐾 左键：完整对话大窗口"
            "\n🐾 右键：快速气泡（悬浮球旁）"
            "\n🐾 拖动：自由移动，拖到哪停在哪"
        )
        self.setToolTip(tip)

    def _fade_in(self):
        self._anim = QPropertyAnimation(self, b"opacity_val", self)
        self._anim.setDuration(280)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(1.0)
        self._anim.setEasingCurve(QEasingCurve.OutBack)
        self._anim.start()

    def _get_opacity(self) -> float:
        return self._opacity_val

    def _set_opacity(self, v: float):
        self._opacity_val = float(v)
        self.setWindowOpacity(max(0.05, min(1.0, v)))
        self.update()

    opacity_val = Property(float, _get_opacity, _set_opacity)

    # =================================================================
    # 位置：默认右下角（避开任务栏）
    # =================================================================
    def _screen_available(self) -> QRect:
        try:
            screen = (
                QGuiApplication.screenAt(self.geometry().center())
                or QGuiApplication.primaryScreen()
            )
            return screen.availableGeometry()
        except Exception:
            return QGuiApplication.primaryScreen().availableGeometry()

    def _move_to_default_bottom_right(self):
        geo = self._screen_available()
        self.move(
            geo.right() - self._size - self._default_margin,
            geo.bottom() - self._size - self._default_margin,
        )

    def _snap_to_edge(self):
        geo = self._screen_available()
        x, y = self.x(), self.y()
        mid_x = geo.left() + geo.width() // 2
        if x + self._size // 2 <= mid_x:
            x = geo.left() + self._default_margin // 2
        else:
            x = geo.right() - self._size - self._default_margin // 2
        y = max(geo.top() + 8, min(y, geo.bottom() - self._size - 8))
        a = QPropertyAnimation(self, b"pos", self)
        a.setDuration(180)
        a.setStartValue(self.pos())
        a.setEndValue(QPoint(x, y))
        a.setEasingCurve(QEasingCurve.OutCubic)
        a.start()

    # =================================================================
    # 绘制（赫萝发色渐变圆球 + 动画人物头像 / 🐺 兜底 + 2 状态浮动）
    # =================================================================
    def paintEvent(self, _evt):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setRenderHint(QPainter.SmoothPixmapTransform, True)
            rect = self.rect().adjusted(2, 2, -2, -2)

            # 1) 阴影
            sp = QPainterPath()
            sp.addEllipse(rect.adjusted(3, 4, -3, -2))
            ps = QPen(QColor(0, 0, 0, 90)); ps.setWidth(3)
            p.setPen(ps); p.setBrush(Qt.NoBrush); p.drawPath(sp)

            # 2) 主体渐变圆球（按 state 切发色渐变）
            path = QPainterPath()
            path.addRoundedRect(rect, self._size, self._size)
            grad = QLinearGradient(rect.topLeft(), rect.bottomRight())
            pri = self.STATE_PRIMARY.get(self._state, self.STATE_PRIMARY["idle"])
            hov = self.STATE_HOVER.get(self._state, self.STATE_HOVER["idle"])
            if self._hovered:
                grad.setColorAt(0.0, QColor(hov[0])); grad.setColorAt(1.0, QColor(hov[1]))
            else:
                grad.setColorAt(0.0, QColor(pri[0])); grad.setColorAt(1.0, QColor(pri[1]))
            p.fillPath(path, QBrush(grad))

            # 3) 高光 + 描边（⚠ 注意括号：QColor 只接收颜色字符串，线宽要放在 QPen 的第二个参数里！）
            p.setPen(QPen(QColor(_THEME.get("maid_white", "#FFFFFF")), 3))
            p.drawPath(path)
            p.setPen(QPen(QColor(_THEME.get("border", "#F2D4B0")), 1))
            p.drawPath(path)

            # 4) 中间头像 —— 先 setClipPath 圆形裁切，y 方向应用 _bounce_offset（伪动画）
            #    使用固定居中坐标（基于 _AVATAR_DRAW_SIZE），不依赖 scaled 实际尺寸，
            #    避免 WebP 各帧尺寸微小差异导致 y 跳动抖动。
            drawn_avatar = False
            y_shift = int(round(self._bounce_offset))
            # 固定居中坐标
            avatar_x = rect.x() + (rect.width() - _AVATAR_DRAW_SIZE.width()) // 2
            avatar_y = rect.y() + (rect.height() - _AVATAR_DRAW_SIZE.height()) // 2 + 1 + y_shift

            # thinking 状态：用搬砖女仆赫萝 WebP 动画
            if self._state == "thinking" and self._movie_thinking is not None:
                pm = self._movie_thinking.currentPixmap()
                if not pm.isNull():
                    p.save(); p.setClipPath(path)
                    scaled = pm.scaled(_AVATAR_DRAW_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    # 用固定坐标居中，不随 scaled 尺寸变化
                    p.drawPixmap(avatar_x, avatar_y, scaled); p.restore(); drawn_avatar = True

            # idle 状态：用沉思女仆赫萝 WebP 动画
            if not drawn_avatar and self._movie is not None:
                pm = self._movie.currentPixmap()
                if not pm.isNull():
                    p.save(); p.setClipPath(path)
                    scaled = pm.scaled(_AVATAR_DRAW_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                    p.drawPixmap(avatar_x, avatar_y, scaled); p.restore(); drawn_avatar = True

            # fallback：🐺 emoji（赫萝贤狼）
            if not drawn_avatar and self._movie_fallback_emoji:
                p.setBrush(Qt.NoBrush)
                font = QFont("Microsoft YaHei"); font.setBold(True); font.setPixelSize(22)
                p.setFont(font); p.setPen(QColor("#FFFFFF"))
                p.drawText(rect.adjusted(0, y_shift, 0, y_shift), Qt.AlignCenter, "\U0001F43A")
        finally:
            # ⚠ 无论 paintEvent 中间抛不抛异常，必须 end()，否则下次进入会报：
            #   QBackingStore::endPaint() called with active painter
            p.end()

    # =================================================================
    # 鼠标事件
    # =================================================================
    def enterEvent(self, e):
        self._hovered = True
        self.update()
        try:
            super().enterEvent(e)
        except Exception:
            pass

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        try:
            super().leaveEvent(e)
        except Exception:
            pass

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            # 记录按下时的全局坐标，用作「是否拖过」的基准
            self._press_global_pos = e.globalPosition().toPoint()
            # 拖动锚点：鼠标全局坐标 - 窗口左上角（按下瞬间的帧左上角）
            self._drag_pos = self._press_global_pos - self.frameGeometry().topLeft()
            self._did_drag = False
            e.accept()
            return
        if e.button() == Qt.RightButton:
            self._show_quick_bubble()
            e.accept()
            return
        try:
            super().mousePressEvent(e)
        except Exception:
            pass

    def mouseMoveEvent(self, e):
        if self._drag_pos is not None and e.buttons() & Qt.LeftButton:
            # 超过系统拖动阈值（默认 ~6-8px）就认为是拖动，后续 release 不再触发点击
            if (not self._did_drag) and self._press_global_pos is not None:
                cur = e.globalPosition().toPoint()
                dx = cur.x() - self._press_global_pos.x()
                dy = cur.y() - self._press_global_pos.y()
                threshold = QApplication.startDragDistance() + 2  # 再加 2px 防抖动
                if (dx * dx + dy * dy) > threshold * threshold:
                    self._did_drag = True
            # 计算目标位置，并限制在屏幕可用区域内（防止拖出屏幕）
            new_pos = e.globalPosition().toPoint() - self._drag_pos
            geo = self._screen_available()
            clamped_x = max(geo.left(), min(new_pos.x(), geo.right() - self._size))
            clamped_y = max(geo.top(), min(new_pos.y(), geo.bottom() - self._size))
            self.move(clamped_x, clamped_y)
            e.accept()
            return
        try:
            super().mouseMoveEvent(e)
        except Exception:
            pass

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton and self._drag_pos is not None:
            drag_pos_was = self._drag_pos
            self._drag_pos = None
            # 只要拖过（_did_drag = True）就保留当前位置（不吸附），绝不触发打开
            if self._did_drag:
                self._did_drag = False
                e.accept()
                return
            # 没拖过：再做一次兜底距离检查（防极短距离的误触），阈值比 startDragDistance 稍大
            if self._press_global_pos is not None:
                cur = e.globalPosition().toPoint()
                dx = cur.x() - self._press_global_pos.x()
                dy = cur.y() - self._press_global_pos.y()
                if (dx * dx + dy * dy) <= 36:  # 6px
                    self._open_full_chat()
            else:
                self._open_full_chat()
            self._press_global_pos = None
            e.accept()
            return
        try:
            super().mouseReleaseEvent(e)
        except Exception:
            pass

    # =================================================================
    # 打开完整对话 / 快速气泡
    # =================================================================
    def _open_full_chat(self):
        if self._full_dlg is None:
            self._full_dlg = FullChatDialog()
            self._full_dlg.message_sent.connect(self._on_user_message_from_full)
        self._full_dlg.show()
        # 尝试拉到前台（对话框是独立 Window，可以主动激活）
        try:
            self._full_dlg.raise_()
            QApplication.setActiveWindow(self._full_dlg)
            self._full_dlg.activateWindow()
            self._full_dlg.focus_input()
        except Exception:
            pass

    def _show_quick_bubble(self):
        """右键 → 在悬浮球旁弹出 QuickChatBubble（自动避屏边界 + 不遮挡球 + 共享自由聊天记录）。"""
        if self._quick_bubble is None:
            self._quick_bubble = QuickChatBubble(parent=None)
            self._quick_bubble.message_sent.connect(self._on_user_message_quick)
        # 确保 FullChatDialog 已创建（不 show），用于共享聊天记录
        if self._full_dlg is None:
            self._full_dlg = FullChatDialog()
            self._full_dlg.message_sent.connect(self._on_user_message_from_full)
        # 从 FullChatDialog 同步当前自由聊天会话记录（共享同一份记录）
        self._quick_bubble.sync_from(self._full_dlg)

        from PySide6.QtCore import QPoint as _QP
        geo = self._screen_available()
        ball_geo = self.geometry()           # 球的全局 rect
        ball_x, ball_y = ball_geo.x(), ball_geo.y()
        ball_w, ball_h = ball_geo.width(), ball_geo.height()
        ball_cx = ball_x + ball_w // 2

        bub_w = self._quick_bubble.width()    # 420
        bub_h = self._quick_bubble.height()   # 480
        gap = 8                                # 球与气泡间距

        # 水平：球在屏幕左半 → 气泡在右；球在右半 → 气泡在左
        if ball_cx < geo.center().x():
            x = ball_x + ball_w + gap          # 球右边 + 间距
        else:
            x = ball_x - bub_w - gap           # 球左边 - 间距 - 气泡宽

        # 垂直：默认让气泡顶部和球顶部对齐，再做边界保护
        y = ball_y
        if y + bub_h > geo.bottom() + 1:
            y = geo.bottom() - bub_h + 1
        if y < geo.top():
            y = geo.top()

        self._quick_bubble.popup_at(_QP(x, y))

    # =================================================================
    # 用户消息 —— 打桩：toast + 发信号
    # =================================================================
    def _on_user_message_from_full(self, text: str):
        self.user_message.emit(text)
        self._mock_toast(f"[完整对话] 发送：\n{text[:60]}")

    def _on_user_message_quick(self, text: str):
        self.user_message.emit(text)
        self._mock_toast(f"[快速气泡] 发送：\n{text[:60]}")

    def _mock_toast(self, msg: str):
        try:
            QToolTip.showText(QCursor.pos(), msg, self)
            QTimer.singleShot(2000, QToolTip.hideText)
        except Exception:
            pass

    # =================================================================
    # 生命周期：每次 show 立刻补打扩展样式 + 补钉 TOPMOST
    # =================================================================
    def showEvent(self, e):
        """每次显示（首次创建 / 最小化恢复 / 关闭后重开）立刻重钉一次。
        setWindowFlags() 会重建 HWND，扩展样式（WS_EX_TOPMOST/TOOLWINDOW）会丢失，
        所以这里用 60ms / 120ms 双 QTimer 补两道保险。"""
        try: super().showEvent(e)
        except Exception: pass
        QTimer.singleShot(60, self._apply_win32_styles_once)
        self._ensure_topmost_win32()
        QTimer.singleShot(120, self._ensure_topmost_win32)

    # =================================================================
    # 销毁前撤销 TOPMOST（避免句柄泄漏，且防止"关了程序桌面仍留一个置顶幽灵"）
    # =================================================================
    def closeEvent(self, e):
        # 1. 先停动画人物的 QMovie 解码器（释放 Animated WebP 内存和解码线程）
        try:
            if self._movie is not None:
                try:
                    self._movie.stop()
                except Exception:
                    pass
                try:
                    # 断 frameChanged -> update 信号，避免销毁中还收到帧
                    try:
                        self._movie.frameChanged.disconnect()
                    except Exception:
                        pass
                except Exception:
                    pass
        except Exception:
            pass
        # 2. 置顶定时器停掉
        try:
            if self._topmost_timer is not None:
                try:
                    self._topmost_timer.stop()
                except Exception:
                    pass
        except Exception:
            pass
        # 先关子窗口
        for attr in ("_full_dlg", "_quick_bubble"):
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                w.close()
            except Exception:
                pass
            try:
                w.deleteLater()
            except Exception:
                pass
        self._unset_topmost_win32()
        try:
            super().closeEvent(e)
        except Exception:
            e.accept()
