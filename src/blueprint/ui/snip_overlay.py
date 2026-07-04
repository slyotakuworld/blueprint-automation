from typing import Any, Optional, Tuple

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PIL import Image, ImageGrab

from .fullscreen_overlay import FullscreenOverlayBase


class SnipOverlay(FullscreenOverlayBase):
    """全屏半透明遮罩下的矩形框选截图（对齐 Qflow do_snip）。

    交互：
      - 鼠标按下（左键）→ 开始拖拽画框
      - 鼠标移动 → 实时绘制红色矩形（边线宽2px，半透填充）
      - 鼠标松开 → 完成，emit finished({bbox_screen, pil_img})
      - ESC/右键 → 取消，emit finished(None)
      - 选区宽<5 或 高<5 → 视为误按，取消
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_start: Optional[QPoint] = None
        self._drag_end: Optional[QPoint] = None
        self._current_rect: Optional[QRect] = None

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finished.emit(None)
            self._restore_app_window()
            return
        if event.button() != Qt.LeftButton:
            return
        self._drag_start = event.pos()
        self._drag_end = event.pos()
        self._current_rect = QRect(self._drag_start, self._drag_end).normalized()
        self.update()

    def mouseMoveEvent(self, event):
        if self._drag_start is None:
            return
        self._drag_end = event.pos()
        self._current_rect = QRect(self._drag_start, self._drag_end).normalized()
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._current_rect is None:
            return
        w = self._current_rect.width()
        h = self._current_rect.height()
        if w < 5 or h < 5:
            self.finished.emit(None)
            self._restore_app_window()
            return
        # 转屏幕坐标（widget geometry 已覆盖所有屏幕，pos() 即全局绝对坐标）
        r = self._current_rect
        x1, y1 = r.left(), r.top()
        x2, y2 = r.right(), r.bottom()
        bbox_screen: Tuple[int, int, int, int] = (int(x1), int(y1), int(x2), int(y2))
        try:
            pil_img = ImageGrab.grab(bbox=bbox_screen, all_screens=True)
        except Exception:
            try:
                pil_img = ImageGrab.grab(bbox=bbox_screen)
            except Exception:
                pil_img = None
        self.finished.emit(
            {"bbox_screen": bbox_screen, "pil_img": pil_img}
        )
        self._restore_app_window()

    def paintEvent(self, event):
        # 1. 先画默认半透明黑背景
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 77))
            # 2. 如果正在拖拽：挖空选区 + 画红框
            if self._current_rect is not None:
                r = self._current_rect
                # 挖空（选区恢复全透明，让用户看清真实画面）
                painter.setCompositionMode(QPainter.CompositionMode_Clear)
                painter.fillRect(r, Qt.transparent)
                painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                # 红色边框 2px
                pen = QPen(QColor(244, 67, 54), 2)
                painter.setPen(pen)
                painter.setBrush(QColor(244, 67, 54, 17))  # 10% 红
                painter.drawRect(r)
                # 右下角尺寸文字
                painter.setPen(QColor(255, 255, 255))
                font = QFont("Consolas", 10)
                painter.setFont(font)
                txt = f"{r.width()} × {r.height()}"
                margin = 6
                tx = r.right() - painter.fontMetrics().horizontalAdvance(txt) - margin
                ty = r.bottom() + margin + painter.fontMetrics().height()
                if ty > self.height() - 4:
                    ty = r.top() - margin
                painter.drawText(tx, ty, txt)
            # 3. 底部操作提示
            painter.setPen(QColor(255, 255, 255))
            font = QFont("Microsoft YaHei", 11)
            painter.setFont(font)
            tip = "按住鼠标左键拖拽画框截取区域  |  按 ESC 或点右键取消返回"
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(tip)
            th = fm.height()
            tx = (self.width() - tw) // 2
            ty = self.height() - 40
            painter.drawText(tx, ty, tip)
        finally:
            painter.end()
