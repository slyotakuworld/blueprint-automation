from typing import Optional, Tuple
import time

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QApplication
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

    限定区域（临时目标窗口）：调用方在遮罩**显示之后**调 `set_limit(rect, label)` 传入，
      框选被限制在该窗口矩形内。目的：保证截出来的模板一定取自这个窗口的内容 ——
      否则运行时按该窗口找图，模板却是从屏幕别处截的，必然匹配不上。
      ⚠ 不要挪到构造函数里做（为什么必须"先上遮罩、后设限定区"，见 set_limit 的 docstring）。

    ⚠ 关键实现约束（踩过坑，别改回去）：
      限定区**不能**用 `CompositionMode_Clear` 把遮罩"挖空"来显示真实画面 ——
      挖空后该区域 alpha=0，而 Qt 的半透明窗口在 Windows 上是分层窗口
      （WS_EX_LAYERED），**系统命中测试会忽略 alpha==0 的像素**，鼠标消息直接
      穿透到底下的临时目标窗口。现象："框里能看见目标窗口，但鼠标点下去是在
      操作那个窗口，框选起不来"。

      ✅ 正确的"开洞"方式（2026-10-04 实测确定）：把该区域填成 **alpha=1**
         （`QColor(0,0,0,1)`，用 `CompositionMode_Source` 整块替换，别用
         SourceOver —— 那只会把 77 的半透明黑再叠一层，洞没开出来）。
         实测分层窗口的穿透阈值是 **alpha==0 才穿透**，alpha=1 就已经被遮罩
         稳稳接住；而 alpha=1 在屏幕上肉眼与全透明无异。
         这样开出来的洞：① 事件绝不穿透；② 显示的是**活的**桌面/目标窗口，
         所见即所得；③ 不再依赖"显示前快照"，即使快照抓取失败或尺寸对不上
         （多屏不同尺寸/DPI 缩放）也不会退化成穿透。

      → 显示用的「整屏快照」机制已**整段删除**：它只在 delay=0 之前有意义，而现在
         hide 主窗与 show 遮罩之间没有等待，快照必然抓到主窗残影，留着只会提供一个
         "看起来有兜底、实际内容错误"的假保险。抓屏失败就直接返回 None（上层提示）。

      ⚠ 另一条同样重要的约束：**松手时不能直接 ImageGrab**。
      拖拽走的是 update()（异步重绘），最后一次移动的重绘可能还排在事件队列里，
      而 mouseReleaseEvent 在同一轮事件循环就把屏抓了 —— 抓到的是**上一帧**，
      于是把那一帧的红色选区边框截进模板/参考图（现象：参考图上横贯一条红线）。
      正确做法见 _grab_selection：先撤装饰 + repaint() 同步重绘 + 空转几帧，再抓。
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._drag_start: Optional[QPoint] = None
        self._drag_end: Optional[QPoint] = None
        self._current_rect: Optional[QRect] = None
        # 限定框选范围的窗口矩形（屏幕绝对坐标；None=不限定，走原全屏逻辑）
        self._limit_screen: Optional[QRect] = None
        self._limit_label: str = ""     # 限定区的名字，画在紫框外侧
        # 在限定区外按下时给个提示（否则用户只感觉"点了没反应"）
        self._outside_warn: bool = False
        # 正在抓图：临时不画"装饰"（红色选区边框 / 尺寸文字），见 _grab_selection
        self._hide_decor: bool = False

    # ---------------- 限定区（临时目标窗口） ----------------
    def set_limit(self, limit_rect, limit_label: str = "") -> None:
        """遮罩**已经显示到屏幕上之后**，再设置/更新限定区。

        ★ 为什么必须晚一点设：解析目标窗口要做 SetForegroundWindow 把它提到最前，
          既耗时又会切换前台窗口。如果这步发生在遮罩上屏之前，那段窗口期里用户的
          点击会直接落到刚被提到最前的目标窗口上；Windows 还会把鼠标捕获一并给它，
          于是整段拖拽都变成在目标窗口里操作（用户实测反馈"截图时目标窗口出现了
          选择操作"）。所以顺序只能是：
            ① hide 主窗 + 同步 show 遮罩（delay_ms=0，遮罩立刻接管命中测试）
            ② 解析目标窗口（提窗 / 找 hwnd，慢一点没关系，遮罩已经挡在前面）
            ③ 调本方法把限定区回填给遮罩
        """
        self._limit_screen = None
        if limit_rect:
            try:
                lx, ly, lw, lh = (int(v) for v in limit_rect)
                if lw > 0 and lh > 0:
                    self._limit_screen = QRect(lx, ly, lw, lh)
            except Exception:
                self._limit_screen = None
        self._limit_label = str(limit_label or "")
        # 步骤②的提窗动作可能把遮罩压到后面，这里再兜一次置顶与焦点
        self._force_topmost_and_focus()
        self.update()

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

    # ---------------- "开洞" ----------------
    def _punch_hole(self, painter: QPainter, rect_local: QRect) -> None:
        """把 rect_local 开成一个"看得见桌面、但绝不穿透鼠标"的洞。

        做法：用 `CompositionMode_Source` 把该区域**整块替换**成 alpha=1 的像素。
          · alpha=1 → 分层窗口命中测试认为该像素不透明 → 事件不会漏给下层窗口；
          · alpha=1 在屏幕上与全透明无异 → 用户看到的是**活的**真实画面；
          · 必须用 Source（整块替换）。用默认的 SourceOver 只是把 alpha=1 的黑
            叠在已有的半透明黑之上，合成出来 alpha 仍然≈78，洞根本没开。
        """
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(rect_local, QColor(0, 0, 0, 1))
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

    # ---------------- 抓取选区 ----------------
    def _grab_selection(self, bbox_screen):
        """抓取最终选区。

        ⚠ 关键约束（踩过坑，别改回去）：松手瞬间屏幕上很可能还留着**上一帧**的遮罩 ——
          `mouseMoveEvent` 走的是 `update()`（异步），最后几次拖拽的重绘可能还排在
          事件队列里，而 `mouseReleaseEvent` 在同一轮事件循环里就把 `ImageGrab` 做掉了。
          于是抓到的画面 = 更早的那一帧，把那一帧的**红色选区边框**（画在选区外 2px）
          连同遮罩边界一起截进模板/参考图。现象：快速甩动鼠标后松手，
          参考图上横贯一条 #F44336 红线（实测偏移量 = 最终选区 − 最后上屏的那一帧，
          甩得快时能差 60px）。

        所以抓之前必须：
          1. 先撤掉"装饰"（红框 + 尺寸文字）—— 就算抓到本帧也不该有它们；
          2. `repaint()` 同步整屏重绘，让选区/遮罩回到"最终状态"；
          3. 空转几帧，等 DWM 真的把新画面合成上屏（分层窗口有一帧延迟）；
          4. 最后才 ImageGrab。
        """
        self._hide_decor = True
        try:
            self.repaint()
            app = QApplication.instance()
            if app is not None:
                for _ in range(4):
                    app.processEvents()
                    time.sleep(0.008)
            try:
                return ImageGrab.grab(bbox=bbox_screen, all_screens=True)
            except Exception:
                try:
                    return ImageGrab.grab(bbox=bbox_screen)
                except Exception:
                    # 不再有"快照兜底"（见类 docstring）：拿不到就是拿不到，
                    # 让上层提示"截图结果为空"，好过塞一张内容可疑的图进去。
                    return None
        finally:
            self._hide_decor = False

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
        pil_img = self._grab_selection(bbox_screen)
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
            #    开洞显示"活的"目标窗口（alpha=1，事件绝不穿透；见 _punch_hole）。
            lim = self._limit_local()
            if lim is not None:
                self._punch_hole(painter, lim)
                # 紫色边框：外扩 2px 画在限定区“之外”（画在里面会被截进模板）
                painter.setPen(QPen(QColor(188, 140, 255), 2))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(lim.adjusted(-2, -2, 2, 2))
                self._draw_limit_label(painter, lim)

            # 3. 正在拖拽：高亮选区 + 红色边框
            #
            # ⚠ 关键约束：选区内容必须保持"零绘制"（或原样开洞）。
            #   松手时 mouseReleaseEvent 里的 ImageGrab 抓的是"屏幕合成画面"，
            #   任何我们自己画进选区的像素都会被永久截进模板图：
            #     · 半透明填充 → 整张模板被染色（白底变 #FEF2F2）
            #     · 压在边界上的 2px 边框 → 模板上边/左边多出一条红线
            #   所以：用 _punch_hole 开洞（alpha=1，既通透又不穿透），
            #   边框一律向外扩 2px 画在选区"之外"。
            if self._current_rect is not None:
                r = self._current_rect
                self._punch_hole(painter, r)
                # 抓图的那一帧不画装饰：红框虽然画在选区之外，但只要抓屏发生
                # 在"上一帧还没被顶掉"的时机，它就会被留在选区内（见 _grab_selection）
                if not self._hide_decor:
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
