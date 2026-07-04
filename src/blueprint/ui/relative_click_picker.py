import io
import base64
from typing import Optional, Tuple

from PIL import Image
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QWidget


class RelativeClickPicker(QWidget):
    """缩略图相对点击位置选择器（对齐 Qflow _draw_image_preview）。

    放置在 Inspector 参数区下方：
      - 黑底容器（#111），高度固定 150px
      - 参考图居中缩放展示（保持比例，不超过容器）
      - 图上叠加「红圈白边小圆」标记当前点击点（默认中心，直径 8px 红填充白边 2px）

    用户交互：
      - 鼠标移到图上 → 十字光标
      - 左键点击图片任意位置 → 计算 click_rel=(rx,ry) 并发出信号
      - rx/ry 强制夹在 0.0~1.0 之间

    Args:
        parent: 父 QWidget
    """

    click_rel_changed = Signal(float, float)

    FIXED_HEIGHT = 150

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setFixedHeight(self.FIXED_HEIGHT)
        self.setMinimumHeight(self.FIXED_HEIGHT)
        self.setCursor(Qt.ArrowCursor)
        self._pixmap: Optional[QPixmap] = None
        self._img_draw_rect: Optional[QRectF] = None  # 图在 widget 上的绘制区域
        self._click_rel: Tuple[float, float] = (0.5, 0.5)
        self._hover_on_image: bool = False

    # ---------------- 外部 API ----------------
    def set_image(self, image_source) -> bool:
        """设置参考图。

        Args:
            image_source: 支持以下类型：
                - str: 文件路径 (.png/.jpg 等)
                - bytes: 原始二进制数据
                - PIL.Image.Image: PIL 图
                - str: base64 字符串（无前缀 data:image/... 也行，自动识别）
                - None: 清空

        Returns:
            True 表示成功加载
        """
        if image_source is None:
            self._pixmap = None
            self._img_draw_rect = None
            self.update()
            return False
        ok = False
        try:
            if isinstance(image_source, Image.Image):
                buf = io.BytesIO()
                image_source.save(buf, format="PNG")
                qimg = QImage.fromData(buf.getvalue(), "PNG")
                self._pixmap = QPixmap.fromImage(qimg)
                ok = True
            elif isinstance(image_source, (bytes, bytearray)):
                qimg = QImage.fromData(bytes(image_source))
                if not qimg.isNull():
                    self._pixmap = QPixmap.fromImage(qimg)
                    ok = True
            elif isinstance(image_source, str):
                s = image_source.strip()
                if not s:
                    ok = False
                elif s.startswith("data:image") or (
                    len(s) > 100
                    and "," not in s[:100]
                    and "/" not in s[:100]
                    and "\\" not in s[:100]
                ):
                    # 尝试 base64
                    raw = s
                    if "," in raw and raw.startswith("data:"):
                        raw = raw.split(",", 1)[1]
                    try:
                        data = base64.b64decode(raw)
                        qimg = QImage.fromData(data)
                        if not qimg.isNull():
                            self._pixmap = QPixmap.fromImage(qimg)
                            ok = True
                    except Exception:
                        ok = False
                else:
                    # 文件路径
                    pm = QPixmap(s)
                    if not pm.isNull():
                        self._pixmap = pm
                        ok = True
        except Exception:
            ok = False
        if not ok:
            self._pixmap = None
        self._img_draw_rect = None
        self.update()
        return ok

    def clear_image(self):
        self.set_image(None)

    @property
    def click_rel(self) -> Tuple[float, float]:
        """当前相对点击点 (rx, ry)，范围均在 [0.0, 1.0]。"""
        return self._click_rel

    def set_click_rel(self, rx: float, ry: float):
        """设置当前相对点击点；超出范围自动夹紧。"""
        rx = max(0.0, min(1.0, float(rx)))
        ry = max(0.0, min(1.0, float(ry)))
        changed = (rx, ry) != self._click_rel
        self._click_rel = (rx, ry)
        self.update()
        if changed:
            self.click_rel_changed.emit(rx, ry)

    # ---------------- 绘制 ----------------
    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(17, 17, 17))
            if self._pixmap is None or self._pixmap.isNull():
                painter.setPen(QColor(120, 120, 120))
                painter.drawText(self.rect(), Qt.AlignCenter, "（暂无参考图）")
                return
            # 居中缩放，保持比例
            cw = self.width()
            ch = self.height()
            iw = self._pixmap.width()
            ih = self._pixmap.height()
            if iw <= 0 or ih <= 0:
                return
            margin = 8
            avail_w = cw - margin * 2
            avail_h = ch - margin * 2
            scale = min(avail_w / iw, avail_h / ih, 1.0)
            dw = int(iw * scale)
            dh = int(ih * scale)
            dx = margin + (avail_w - dw) // 2
            dy = margin + (avail_h - dh) // 2
            rect = QRectF(dx, dy, dw, dh)
            self._img_draw_rect = rect
            painter.drawPixmap(rect.toRect(), self._pixmap)
            # 绘制当前点击点：红圈白边，直径 8px + 2px 白边
            cx = dx + dw * self._click_rel[0]
            cy = dy + dh * self._click_rel[1]
            outer_r = 8.0
            inner_r = 5.0
            pen = QPen(QColor(255, 255, 255), 2)
            painter.setPen(pen)
            painter.setBrush(QColor(244, 67, 54))
            painter.drawEllipse(QPointF(cx, cy), outer_r, outer_r)
            pen2 = QPen(QColor(244, 67, 54), 1)
            painter.setPen(pen2)
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(QPointF(cx, cy), inner_r, inner_r)
            # 小坐标文字 (rx, ry)
            txt = f"点击: ({self._click_rel[0]:.3f}, {self._click_rel[1]:.3f})"
            painter.setPen(QColor(200, 200, 200))
            painter.drawText(dx, dy + ch + 18 if dy + ch + 18 < ch else dy - 4, txt)
        finally:
            painter.end()

    # ---------------- 鼠标事件 ----------------
    def mouseMoveEvent(self, event):
        on = self._is_on_image(event.position())
        if on != self._hover_on_image:
            self._hover_on_image = on
            self.setCursor(Qt.CrossCursor if on else Qt.ArrowCursor)

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        if self._img_draw_rect is None:
            return
        p = event.position()
        r = self._img_draw_rect
        if not r.contains(p):
            return
        rx = (p.x() - r.left()) / r.width() if r.width() > 0 else 0.5
        ry = (p.y() - r.top()) / r.height() if r.height() > 0 else 0.5
        self.set_click_rel(rx, ry)

    def leaveEvent(self, event):
        if self._hover_on_image:
            self._hover_on_image = False
            self.setCursor(Qt.ArrowCursor)

    def _is_on_image(self, p: QPointF) -> bool:
        return self._img_draw_rect is not None and self._img_draw_rect.contains(p)
