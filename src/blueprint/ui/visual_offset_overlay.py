import io
import base64
from typing import Any, Optional, Tuple

from PIL import Image, ImageGrab
from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication

from .fullscreen_overlay import FullscreenOverlayBase


class VisualOffsetOverlay(FullscreenOverlayBase):
    """基准图可视化偏移微调。

    交互流程：
      1. 主窗隐藏后 0.3s 延迟（不做黑遮罩，让屏幕保持可见）
      2. 先截一张「完整屏幕静态截图」，全屏贴到无边框全屏 Widget 上（用户看到"静止真实屏幕"，方便点击不会误操作真实窗口）
      3. 立即在这张静态图上跑一次「模板匹配」：
           • 找到 → 绿色 #4caf50 粗线 2px 矩形框住匹配区域，匹配中心画红色十字(+/-10px 2px)
           • 没找到 → 外部调用者负责弹 QMessageBox 错误；本组件 finished.emit(None) + 恢复主窗
      4. 鼠标移动：从红色十字中心 → 鼠标光标 画蓝色虚线（dash=4,4 宽 1px）；光标右下角 10px 跟随白底蓝字实时显示 Offset: +23, -15
      5. 左键点击 → finished.emit( (dx, dy) )，dx/dy 为相对模板中心的像素偏移
      6. 右键/Esc → 取消 finished.emit(None)

    Args:
        reference_image: 基准图（PIL.Image / 文件路径 / bytes / base64字符串），同 RelativeClickPicker 接受格式
        threshold: 匹配阈值 0.0~1.0，默认 0.9
        hwnd: 可选绑定窗口句柄（如果有，限定匹配只在窗口范围内，性能更快 + 结果更准）
    """

    def __init__(
        self,
        reference_image,
        threshold: float = 0.9,
        hwnd: Optional[int] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, False)
        self._threshold = max(0.0, min(1.0, float(threshold)))
        self._hwnd = hwnd if isinstance(hwnd, int) and hwnd > 0 else None
        self._ref_pil: Optional[Image.Image] = self._load_ref_image(reference_image)
        self._fullscreen_pil: Optional[Image.Image] = None
        self._fullscreen_pixmap: Optional[QPixmap] = None
        self._matched_bbox: Optional[Tuple[int, int, int, int]] = None  # x1,y1,x2,y2 屏幕绝对坐标
        self._match_center: Optional[Tuple[int, int]] = None
        self._cursor_pos: Optional[QPoint] = None

    # ---------- 公共入口（比 prepare_and_run 延迟更短）----------
    @classmethod
    def prepare_and_run(
        cls,
        app_window,
        reference_image,
        threshold: float = 0.9,
        hwnd: Optional[int] = None,
        *args,
        delay_ms: int = 300,
        on_not_found=None,
        **kwargs,
    ) -> "VisualOffsetOverlay":
        """入口：隐藏主窗 → 延迟300ms（不做黑遮罩）。

        Args:
            on_not_found: 可选回调 callable(message_str)，如果模板没在屏幕上找到会被调用。
        """
        overlay = cls.__new__(cls)
        overlay.__init__(reference_image, threshold=threshold, hwnd=hwnd, *args, **kwargs)
        overlay._app_window = app_window
        overlay._on_not_found_cb = on_not_found

        def _on_finished(_res):
            from PySide6.QtCore import QTimer

            QTimer.singleShot(100, overlay._restore_app_window)

        overlay.finished.connect(_on_finished)
        from PySide6.QtCore import QTimer

        QTimer.singleShot(delay_ms, overlay._start_flow)
        return overlay

    def _start_flow(self):
        # 1. 先全屏截图（捕获"静止真实屏幕"）
        try:
            if self._hwnd is not None:
                try:
                    import win32gui

                    l, t, r, b = win32gui.GetWindowRect(self._hwnd)
                    self._fullscreen_pil = ImageGrab.grab(
                        bbox=(int(l), int(t), int(r), int(b)), all_screens=True
                    )
                    self._offset_topleft = (int(l), int(t))
                except Exception:
                    self._fullscreen_pil = ImageGrab.grab(all_screens=True)
                    self._offset_topleft = (0, 0)
            else:
                self._fullscreen_pil = ImageGrab.grab(all_screens=True)
                self._offset_topleft = (0, 0)
        except Exception:
            self._fullscreen_pil = None
            self._offset_topleft = (0, 0)
        # 2. 跨多屏设置几何（和基类一样逻辑）
        app = QApplication.instance()
        screens = app.screens() if app else []
        if screens:
            geo = screens[0].geometry()
            for s in screens[1:]:
                geo = geo.united(s.geometry())
            self.setGeometry(geo)
        # 3. 把 PIL 全屏图转 QPixmap
        if self._fullscreen_pil is not None:
            buf = io.BytesIO()
            self._fullscreen_pil.save(buf, format="PNG")
            qimg = QImage.fromData(buf.getvalue(), "PNG")
            if not qimg.isNull():
                self._fullscreen_pixmap = QPixmap.fromImage(qimg)
        # 4. 尝试匹配模板
        self._do_match()
        if self._match_center is None:
            # 没匹配到 → 通知外部 + 取消
            if callable(getattr(self, "_on_not_found_cb", None)):
                try:
                    self._on_not_found_cb(
                        "屏幕上没找到基准图，请确认游戏/目标界面是否正确显示。"
                    )
                except Exception:
                    pass
            self.finished.emit(None)
            self._restore_app_window()
            return
        # 5. 显示全屏遮罩（上面铺了静态图，用户看不到底下真实窗口，不会误点）
        self.show()

    # ---------- 加载参考图 ----------
    @staticmethod
    def _load_ref_image(source) -> Optional[Image.Image]:
        if source is None:
            return None
        try:
            if isinstance(source, Image.Image):
                return source.convert("RGB")
            if isinstance(source, (bytes, bytearray)):
                return Image.open(io.BytesIO(bytes(source))).convert("RGB")
            if isinstance(source, str):
                s = source.strip()
                if not s:
                    return None
                # base64 猜测
                if (
                    s.startswith("data:image")
                    or ("," not in s[:80] and "/" not in s[:80] and "\\" not in s[:80] and len(s) > 100)
                ):
                    raw = s
                    if "," in raw and raw.startswith("data:"):
                        raw = raw.split(",", 1)[1]
                    try:
                        data = base64.b64decode(raw)
                        return Image.open(io.BytesIO(data)).convert("RGB")
                    except Exception:
                        return None
                # 文件路径
                return Image.open(s).convert("RGB")
        except Exception:
            return None
        return None

    # ---------- 匹配 ----------
    def _do_match(self):
        if self._ref_pil is None or self._fullscreen_pil is None:
            return
        # 优先 OpenCV
        try:
            import cv2  # type: ignore
            import numpy as np  # type: ignore

            ref_arr = np.array(self._ref_pil)
            fs_arr = np.array(self._fullscreen_pil)
            ref_gray = cv2.cvtColor(ref_arr, cv2.COLOR_RGB2GRAY)
            fs_gray = cv2.cvtColor(fs_arr, cv2.COLOR_RGB2GRAY)
            res = cv2.matchTemplate(fs_gray, ref_gray, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= self._threshold:
                x, y = max_loc
                rh, rw = ref_gray.shape[:2]
                x1, y1 = int(x), int(y)
                x2, y2 = x1 + int(rw), y1 + int(rh)
                if self._offset_topleft:
                    x1 += self._offset_topleft[0]
                    y1 += self._offset_topleft[1]
                    x2 += self._offset_topleft[0]
                    y2 += self._offset_topleft[1]
                self._matched_bbox = (x1, y1, x2, y2)
                self._match_center = ((x1 + x2) // 2, (y1 + y2) // 2)
                return
        except Exception:
            pass
        # 兜底：PIL 粗匹配（性能差只做最后兜底）
        try:
            from PIL import ImageChops

            # 简单滑动窗口可能超时，跳过
        except Exception:
            pass

    # ---------- 绘制 ----------
    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            # 1. 铺全屏静态截图（让用户看到"静止真实屏幕"）
            if self._fullscreen_pixmap is not None:
                if self._offset_topleft != (0, 0):
                    x, y = self._offset_topleft
                    painter.drawPixmap(x, y, self._fullscreen_pixmap)
                else:
                    painter.drawPixmap(self.rect(), self._fullscreen_pixmap)
            else:
                painter.fillRect(self.rect(), QColor(0, 0, 0, 77))
            # 2. 匹配矩形（绿色粗线 2px）
            if self._matched_bbox is not None:
                x1, y1, x2, y2 = self._matched_bbox
                rect = QRect(int(x1), int(y1), int(x2 - x1), int(y2 - y1))
                pen = QPen(QColor(76, 175, 80), 2)
                painter.setPen(pen)
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(rect)
                # 中心红色十字 +/- 10px
                cx, cy = self._match_center
                pen2 = QPen(QColor(244, 67, 54), 2)
                painter.setPen(pen2)
                painter.drawLine(int(cx) - 10, int(cy), int(cx) + 10, int(cy))
                painter.drawLine(int(cx), int(cy) - 10, int(cx), int(cy) + 10)
                # 3. 蓝色虚线从十字到当前鼠标 + 跟随文字
                if self._cursor_pos is not None and self._match_center is not None:
                    mx = self._cursor_pos.x()
                    my = self._cursor_pos.y()
                    pen3 = QPen(QColor(33, 150, 243), 1, Qt.DashLine)
                    pen3.setDashPattern([4, 4])
                    painter.setPen(pen3)
                    painter.drawLine(int(cx), int(cy), int(mx), int(my))
                    # Offset 文字
                    dx = int(mx) - int(cx)
                    dy = int(my) - int(cy)
                    sign_x = "+" if dx >= 0 else ""
                    sign_y = "+" if dy >= 0 else ""
                    txt = f"Offset: {sign_x}{dx}, {sign_y}{dy}"
                    font = QFont("Consolas", 11, QFont.Bold)
                    painter.setFont(font)
                    fm = painter.fontMetrics()
                    tw = fm.horizontalAdvance(txt)
                    th = fm.height()
                    pad = 6
                    bx = int(mx) + 14
                    by = int(my) + 14
                    if bx + tw + pad * 2 > self.width():
                        bx = int(mx) - tw - pad * 2 - 14
                    if by + th + pad * 2 > self.height():
                        by = int(my) - th - pad * 2 - 14
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(255, 255, 255, 230))
                    painter.drawRect(QRect(bx, by, tw + pad * 2, th + pad))
                    painter.setPen(QColor(33, 150, 243))
                    painter.drawText(bx + pad, by + fm.ascent() + pad // 2, txt)
            # 4. 顶部操作提示
            painter.setPen(QColor(0, 0, 0, 180))
            painter.setBrush(QColor(255, 255, 255, 210))
            tip = "🎯 移动鼠标到最终要点击的位置 → 左键确认偏移  |  右键/Esc 取消"
            font = QFont("Microsoft YaHei", 12, QFont.Bold)
            painter.setFont(font)
            fm = painter.fontMetrics()
            tw = fm.horizontalAdvance(tip)
            th = fm.height()
            bx = (self.width() - tw) // 2 - 14
            by = 20
            painter.drawRect(bx - 8, by - 8, tw + 28, th + 20)
            painter.setPen(QColor(0, 0, 0))
            painter.drawText(bx, by + fm.ascent(), tip)
        finally:
            painter.end()

    # ---------- 鼠标 ----------
    def mouseMoveEvent(self, event):
        self._cursor_pos = event.pos()
        self.update()

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finished.emit(None)
            self._restore_app_window()
            return
        if event.button() != Qt.LeftButton:
            return
        if self._match_center is None:
            return
        mx = event.pos().x()
        my = event.pos().y()
        dx = int(mx) - int(self._match_center[0])
        dy = int(my) - int(self._match_center[1])
        self.finished.emit((dx, dy))
        self._restore_app_window()
