from typing import Any, Optional, Tuple

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen

from .fullscreen_overlay import FullscreenOverlayBase


class CoordOverlay(FullscreenOverlayBase):
    """坐标点选全屏遮罩（对齐 Qflow _coord_overlay）。

    两种模式（构造时传入 hwnd 自动判定）：
      - 绑定窗口模式（hwnd 有效）：
          * 蓝色虚线 3px 描整个绑定窗口矩形
          * 左上角 10,20 写小字 "⚓ 绑定窗口范围：xxx.exe"
          * 顶部正中央 50px 写大字绿色 "[窗口绑定模式] 坐标已自动扣除窗口左上角偏移（保存为相对坐标）"
          * 点击后保存 相对坐标 rel_x = click_abs_x - win_rect.left
      - 绝对坐标模式（hwnd 无效/None）：
          * 顶部正中央 50px 写大字黄色 "[绝对坐标模式] 未检测到绑定窗口，将保存为绝对屏幕坐标"
          * 点击后保存 绝对坐标

    Args:
        hwnd: 可选的已绑定窗口句柄；提供且有效时自动进入绑定模式
        window_rect: 绑定窗口矩形 (l, t, w, h)；若未提供但 hwnd 有效，会 win32gui 再查
        exe_name: 绑定窗口所属进程名，提示用
    """

    def __init__(
        self,
        hwnd: Optional[int] = None,
        window_rect: Optional[Tuple[int, int, int, int]] = None,
        exe_name: str = "",
        parent=None,
    ):
        super().__init__(parent)
        self._hwnd = hwnd if isinstance(hwnd, int) and hwnd > 0 else None
        self._bound_rect: Optional[QRect] = None
        self._exe_name = exe_name or ""
        self._hover_pos: Optional[QPoint] = None

        if window_rect and len(window_rect) == 4:
            l, t, w, h = window_rect
            self._bound_rect = QRect(int(l), int(t), int(w), int(h))
        elif self._hwnd is not None:
            try:
                import win32gui

                l, t, r, b = win32gui.GetWindowRect(self._hwnd)
                self._bound_rect = QRect(int(l), int(t), int(r - l), int(b - t))
                if not self._exe_name:
                    try:
                        from src.tools.window_utils import get_exe_name_by_hwnd

                        self._exe_name = get_exe_name_by_hwnd(self._hwnd) or ""
                    except Exception:
                        pass
            except Exception:
                self._bound_rect = None

    @property
    def is_bound_mode(self) -> bool:
        return self._bound_rect is not None

    def mouseMoveEvent(self, event):
        self._hover_pos = event.pos()
        self.update()

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            if event.button() == Qt.RightButton:
                self.finished.emit(None)
                self._restore_app_window()
            return
        abs_x = event.pos().x()
        abs_y = event.pos().y()
        if self.is_bound_mode:
            r = self._bound_rect
            rel_x = abs_x - r.left()
            rel_y = abs_y - r.top()
            result = {
                "mode": "bound",
                "abs": (abs_x, abs_y),
                "rel": (rel_x, rel_y),
                "bound_rect": (r.left(), r.top(), r.width(), r.height()),
            }
        else:
            result = {
                "mode": "absolute",
                "abs": (abs_x, abs_y),
                "rel": (abs_x, abs_y),
            }
        self.finished.emit(result)
        self._restore_app_window()

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            # 1. 默认半透明黑背景
            painter.fillRect(self.rect(), QColor(0, 0, 0, 77))
            # 2. 绑定模式：画蓝色虚线窗口框 + 挖空该区域
            if self.is_bound_mode:
                r = self._bound_rect
                # 挖空（让用户看清真实窗口）
                painter.setCompositionMode(QPainter.CompositionMode_Clear)
                painter.fillRect(r, Qt.transparent)
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                # 蓝色虚线 3px
                pen = QPen(QColor(33, 150, 243), 3, Qt.DashLine)
                pen.setDashPattern([4, 4])
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(r)
                # 左上角小字
                painter.setPen(QColor(100, 181, 246))
                font = QFont("Microsoft YaHei", 10)
                painter.setFont(font)
                tip1 = f"⚓ 绑定窗口范围：{self._exe_name or '目标程序'}"
                painter.drawText(r.left() + 10, r.top() + 20, tip1)
                # 顶部中央大字 绿色
                self._draw_center_banner(
                    painter,
                    "[窗口绑定模式] 坐标已自动扣除窗口左上角偏移（保存为相对坐标）",
                    QColor(129, 199, 132),
                    y_pos=50,
                )
            else:
                # 绝对模式：顶部中央 大字黄色
                self._draw_center_banner(
                    painter,
                    "[绝对坐标模式] 未检测到绑定窗口，将保存为绝对屏幕坐标",
                    QColor(255, 213, 79),
                    y_pos=50,
                )
            # 3. 十字光标跟随：当前坐标值提示
            if self._hover_pos is not None:
                px = self._hover_pos.x()
                py = self._hover_pos.y()
                if self.is_bound_mode:
                    r = self._bound_rect
                    rx = px - r.left()
                    ry = py - r.top()
                    coord_txt = f"相对: ({rx}, {ry})    绝对: ({px}, {py})"
                else:
                    coord_txt = f"绝对: ({px}, {py})"
                painter.setPen(QColor(255, 255, 255))
                font = QFont("Consolas", 11)
                painter.setFont(font)
                fm = painter.fontMetrics()
                tw = fm.horizontalAdvance(coord_txt)
                th = fm.height()
                pad = 8
                bx = px + 20
                by = py + 20
                if bx + tw + pad > self.width():
                    bx = px - tw - pad - 20
                if by + th + pad > self.height():
                    by = py - th - pad - 20
                painter.fillRect(bx, by, tw + pad * 2, th + pad, QColor(0, 0, 0, 200))
                painter.drawText(bx + pad, by + pad + fm.ascent(), coord_txt)
            # 4. 底部操作提示
            painter.setPen(QColor(255, 255, 255))
            font = QFont("Microsoft YaHei", 11)
            painter.setFont(font)
            tip = "点击鼠标左键确认拾取  |  按 ESC 或点右键取消返回"
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(tip)
            tx = (self.width() - tw) // 2
            ty = self.height() - 40
            painter.drawText(tx, ty, tip)
        finally:
            painter.end()

    @staticmethod
    def _draw_center_banner(painter: QPainter, text: str, color: QColor, y_pos: int):
        font = QFont("Microsoft YaHei", 16, QFont.Bold)
        painter.setFont(font)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        pad = 14
        bx = (painter.device().width() - tw) // 2 - pad
        by = y_pos
        painter.fillRect(bx, by, tw + pad * 2, th + pad * 2, QColor(0, 0, 0, 210))
        painter.setPen(color)
        painter.drawText(bx + pad, by + pad + fm.ascent(), text)
