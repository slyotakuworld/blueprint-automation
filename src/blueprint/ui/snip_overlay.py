from typing import Optional, Tuple

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPen
from PIL import ImageGrab

from .fullscreen_overlay import FullscreenOverlayBase


class SnipOverlay(FullscreenOverlayBase):
    """全屏半透明遮罩下的矩形框选截图。

    交互：
      - 鼠标按下（左键）→ 开始拖拽画框
      - 鼠标移动 → 实时绘制红色矩形（边线宽2px）
      - 鼠标松开 → 完成，emit finished({bbox_screen, pil_img})
      - ESC/右键 → 取消，emit finished(None)
      - 选区宽<5 或 高<5 → 视为误按，取消

    限定区域 limit_rect（可选，屏幕绝对坐标 (x, y, w, h)）：
      节点配置了「临时目标窗口」时传入该窗口矩形，框选被限制在窗口之内。
      目的：保证截出来的模板一定取自这个窗口的内容 —— 否则运行时按该窗口
      找图，模板却是从屏幕别处截的，必然匹配不上。

    ⚠ 关键实现约束（踩过坑，别改回去）：
      限定区**不能**用 CompositionMode_Clear 把遮罩"挖空"来显示真实画面。
      挖空后该区域 alpha=0，而 Qt 的半透明窗口在 Windows 上是分层窗口
      （WS_EX_LAYERED），系统做命中测试时会忽略完全透明的像素 —— 鼠标消息
      会穿透到底下的临时目标窗口。现象："框里能看见目标窗口，但鼠标点下去
      是在操作那个窗口，框选起不来"。
      正确做法：show() 之前先抓一张整屏快照（基类的 _bg_img），限定区用这张
      快照以**不透明**方式填充。既保证事件被遮罩接住，又保证用户框到的画面
      精确等于将要截下来的内容（顺带杜绝了"遮罩自身像素被截进模板"）。
    """

    def __init__(
        self,
        parent=None,
        limit_rect: Optional[Tuple[int, int, int, int]] = None,
        limit_label: str = "",
    ):
        super().__init__(parent)
        self._drag_start: Optional[QPoint] = None
        self._drag_end: Optional[QPoint] = None
        self._current_rect: Optional[QRect] = None
        # 限定框选范围的窗口矩形（屏幕绝对坐标；None=不限定，走原全屏逻辑）
        self._limit_screen: Optional[QRect] = None
        if limit_rect:
            try:
                lx, ly, lw, lh = (int(v) for v in limit_rect)
                if lw > 0 and lh > 0:
                    self._limit_screen = QRect(lx, ly, lw, lh)
            except Exception:
                self._limit_screen = None
        self._limit_label = str(limit_label or "")
        # 限定区要靠"不透明快照"填充，所以需要显示前抓一张屏幕快照
        self._need_bg_snapshot: bool = self._limit_screen is not None
        # 在限定区外按下时给个提示（否则用户只感觉"点了没反应"）
        self._outside_warn: bool = False

    # ---------------- 坐标换算 ----------------
    def _local_to_screen(self, x: int, y: int) -> Tuple[int, int]:
        """遮罩本地坐标 → 屏幕绝对坐标（多屏时遮罩起点不一定在 0,0）。"""
        g = self.geometry()
        return (int(x) + g.left(), int(y) + g.top())

    def _limit_local(self) -> Optional[QRect]:
        """限定区域换算成本地坐标（画遮罩 / 判边界用）。"""
        if self._limit_screen is None:
            return None
        g = self.geometry()
        return self._limit_screen.translated(-g.left(), -g.top())

    # ---------------- 快照绘制 ----------------
    def _paint_snapshot(self, painter: QPainter, rect_local: QRect) -> bool:
        """用"显示前的整屏快照"以不透明方式填充 rect_local。

        返回 True 表示已填充（该区域 alpha=255，事件不会穿透）；
        返回 False 表示快照不可用，调用方应退回原来的挖空逻辑。

        坐标关系：快照抓的是整块虚拟桌面，遮罩几何也是所有屏幕的并集，
        两者原点一致、且 1:1（无缩放），所以本地坐标可直接当快照像素坐标用。
        """
        bg = self._bg_img
        if bg is None:
            return False
        try:
            if tuple(bg.size) != (int(self.width()), int(self.height())):
                # 尺寸对不上（缩放/异常）→ 不敢乱贴，交回调用方兜底
                return False
            x1, y1 = int(rect_local.left()), int(rect_local.top())
            x2, y2 = int(rect_local.right()) + 1, int(rect_local.bottom()) + 1
            if x2 <= x1 or y2 <= y1:
                return False
            crop = bg.crop((x1, y1, x2, y2)).convert("RGB")
            data = crop.tobytes("raw", "RGB")
            qimg = QImage(
                data, crop.width, crop.height, crop.width * 3, QImage.Format_RGB888
            ).copy()  # 必须 copy：data 是临时 bytes，不 copy 会指向已释放内存
            painter.drawImage(x1, y1, qimg)
            return True
        except Exception:
            return False

    # ---------------- 鼠标事件 ----------------
    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.finished.emit(None)
            self._restore_app_window()
            return
        if event.button() != Qt.LeftButton:
            return
        lim = self._limit_local()
        if lim is not None and not lim.contains(event.pos()):
            # 限定区域内才能起框：点在外面不响应，避免框到临时目标窗口之外
            self._outside_warn = True
            self.update()
            return
        self._outside_warn = False
        self._drag_start = event.pos()
        self._drag_end = event.pos()
        self._current_rect = QRect(self._drag_start, self._drag_end).normalized()
        self.update()

    def mouseMoveEvent(self, event):
        if self._drag_start is None:
            return
        p = event.pos()
        lim = self._limit_local()
        if lim is not None:
            # 拖拽终点夹到限定区域内，光标跑出去也不会画出界
            p = QPoint(
                min(max(p.x(), lim.left()), lim.right()),
                min(max(p.y(), lim.top()), lim.bottom()),
            )
        self._drag_end = p
        self._current_rect = QRect(self._drag_start, self._drag_end).normalized()
        self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton or self._current_rect is None:
            return
        r = self._current_rect
        lim = self._limit_local()
        if lim is not None:
            r = r.intersected(lim)  # 双保险：结果一定落在限定区域内
        w = r.width()
        h = r.height()
        if w < 5 or h < 5:
            self.finished.emit(None)
            self._restore_app_window()
            return
        # 本地坐标 → 屏幕绝对坐标，再据此抓屏
        sx, sy = self._local_to_screen(r.left(), r.top())
        bbox_screen: Tuple[int, int, int, int] = (
            int(sx), int(sy), int(sx + w), int(sy + h)
        )
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

    # ---------------- 绘制 ----------------
    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            # 1. 先画默认半透明黑背景
            painter.fillRect(self.rect(), QColor(0, 0, 0, 77))

            # 2. 限定区域（临时目标窗口）
            #    不挖空（挖空=alpha 0=鼠标穿透，见类 docstring），改用显示前
            #    的快照不透明填充。快照不可用时才退回挖空（至少不崩）。
            lim = self._limit_local()
            if lim is not None:
                if not self._paint_snapshot(painter, lim):
                    painter.setCompositionMode(QPainter.CompositionMode_Clear)
                    painter.fillRect(lim, Qt.transparent)
                    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                # 紫色边框：外扩 2px 画在限定区“之外”（画在里面会被截进模板）
                painter.setPen(QPen(QColor(188, 140, 255), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(lim.adjusted(-2, -2, 2, 2))
                self._draw_limit_label(painter, lim)

            # 3. 正在拖拽：高亮选区 + 红色边框
            #
            # ⚠ 关键约束：选区内容必须保持"零绘制"或"用快照原样覆盖"。
            #   松手时 mouseReleaseEvent 里的 ImageGrab 抓的是"屏幕合成画面"，
            #   任何我们自己画进选区的像素都会被永久截进模板图：
            #     · 半透明填充 → 整张模板被染色（白底变 #FEF2F2）
            #     · 压在边界上的 2px 边框 → 模板上边/左边多出一条红线
            #   所以：有快照就用快照原样覆盖（内容=真实画面），
            #   没有快照就挖空，且边框一律向外扩 2px 画在选区"之外"。
            if self._current_rect is not None:
                r = self._current_rect
                if not self._paint_snapshot(painter, r):
                    painter.setCompositionMode(QPainter.CompositionMode_Clear)
                    painter.fillRect(r, Qt.transparent)
                    painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
                # 红色边框 2px，画在选区之外
                painter.setPen(QPen(QColor(244, 67, 54), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(r.adjusted(-2, -2, 2, 2))
                # 右下角尺寸文字（始终画在选区之外）
                painter.setPen(QColor(255, 255, 255))
                painter.setFont(QFont("Consolas", 10))
                txt = f"{r.width()} × {r.height()}"
                margin = 6
                tx = r.right() - painter.fontMetrics().horizontalAdvance(txt) - margin
                ty = r.bottom() + margin + painter.fontMetrics().height()
                if ty > self.height() - 4:
                    ty = r.top() - margin
                painter.drawText(tx, ty, txt)

            # 4. 底部操作提示
            painter.setPen(QColor(255, 255, 255))
            painter.setFont(QFont("Microsoft YaHei", 11))
            if self._outside_warn:
                tip = "请在上方紫色框（临时目标窗口）内部拖拽框选  ｜  ESC / 右键取消"
            elif lim is not None and self._limit_label:
                tip = f"框选已限定在临时目标窗口内 → {self._limit_label}  ｜  ESC / 右键取消"
            else:
                tip = "按住鼠标左键拖拽画框截取区域  |  按 ESC 或点右键取消返回"
            self._draw_hint_text(painter, tip, lim)
        finally:
            painter.end()

    def _draw_limit_label(self, painter, lim: QRect):
        """在限定区外侧标注这是哪个窗口（绝不画进限定区/选区内）。"""
        if not self._limit_label:
            return
        painter.setFont(QFont("Microsoft YaHei", 10))
        painter.setPen(QColor(188, 140, 255))
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(self._limit_label)
        x = min(max(4, lim.left()), max(4, self.width() - tw - 6))
        # 优先画在限定区上方，放不下则画在下方；上下都没空间就放弃（宁可不画）
        y = lim.top() - 8
        if y - fm.ascent() < 0:
            y = lim.bottom() + fm.ascent() + 8
            if y + 4 > self.height():
                return
        painter.drawText(x, int(y), self._limit_label)

    def _draw_hint_text(self, painter, text: str, lim: Optional[QRect]):
        """画底部提示；若提示会落进限定区或当前选区（可能被截进模板/参考图），换位置或干脆不画。"""
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        x = max(4, (self.width() - tw) // 2)
        # 选区也要避开：全屏模式（无限定区）下选区是"挖空"的，松手时 ImageGrab 抓的是
        # 屏幕实况，提示文字若压在选区里会被永久截进模板/参考图
        avoid = [r for r in (lim, self._current_rect) if r is not None]
        for y in (self.height() - 40, 26 + fm.ascent(), self.height() - 8):
            if y + 4 > self.height() or y - fm.ascent() < 0:
                continue
            box = QRect(x - 6, int(y) - fm.ascent() - 3, tw + 12, fm.height() + 8)
            if all(not box.intersects(r) for r in avoid):
                painter.drawText(x, int(y), text)
                return
