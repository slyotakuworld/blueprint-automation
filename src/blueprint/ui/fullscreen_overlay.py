from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget


class FullscreenOverlayBase(QWidget):
    """全屏半透明遮罩基类。

    流程：
      1. 调用方（blueprint_app）调用 prepare_and_run(app)：隐藏主窗，延迟400ms
      2. showFullScreen() 覆盖所有显示器，背景黑半透
      3. 子类重写 paintEvent / mousePressEvent / mouseMoveEvent / mouseReleaseEvent
      4. 完成/取消后 emit finished(result)，caller恢复主窗

    子类可选开关：
      _need_bg_snapshot = True → show() 之前先抓一张整块虚拟桌面的快照，存到
      self._bg_img（PIL.Image）。用途见 SnipOverlay：控件上 alpha=0 的区域，
      系统做命中测试时会当"透明"处理，鼠标事件会穿透到下层窗口，所以那块
      区域不能用"挖空"，必须用不透明快照填充。
    """

    finished = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setCursor(Qt.CrossCursor)
        self._app_window: Optional[QWidget] = None
        self._result: Any = None
        # 显示遮罩之前是否抓屏幕快照（默认关，由子类按需开启）
        self._need_bg_snapshot: bool = False
        self._bg_img = None  # PIL.Image：遮罩显示前的整块虚拟桌面快照

    @classmethod
    def prepare_and_run(
        cls,
        app_window: QWidget,
        *args,
        delay_ms: int = 400,
        **kwargs,
    ) -> "FullscreenOverlayBase":
        """统一入口：隐藏主窗 → 延迟 → show全屏遮罩

        Args:
            app_window: 主窗口对象，完成后会恢复显示
            delay_ms: 隐藏主窗后延迟多少毫秒再显示遮罩（让目标窗口重绘）
            *args, **kwargs: 透传给子类构造函数

        Returns:
            已创建的 overlay 实例；结果通过 finished signal 异步返回
        """
        app_window.hide()
        overlay = cls(*args, **kwargs)
        overlay._app_window = app_window

        def _on_finished(_res):
            QTimer.singleShot(100, overlay._restore_app_window)

        overlay.finished.connect(_on_finished)
        QTimer.singleShot(delay_ms, overlay._show_fullscreen_all_screens)
        return overlay

    def _restore_app_window(self):
        """恢复主窗口显示（安全兜底，任何异常都不影响）。"""
        try:
            if self._app_window is not None:
                self._app_window.show()
                self._app_window.raise_()
                self._app_window.activateWindow()
        except Exception:
            pass
        try:
            self.close()
        except Exception:
            pass

    # ---------------- 快照 / 置顶 ----------------
    def _grab_virtual_screen(self):
        """抓取整块虚拟桌面（多屏并集）的快照；失败返回 None。"""
        try:
            from PIL import ImageGrab
            return ImageGrab.grab(all_screens=True)
        except Exception:
            return None

    def _force_topmost_and_focus(self):
        """Win32 强制置顶 + 尽量取回焦点。

        为什么需要：框选之前会把「临时目标窗口」SetForegroundWindow 提到最前。
        遮罩虽然带 WindowStaysOnTopHint，但个别情况下仍可能被目标窗口压住，
        那样鼠标事件会被目标窗口吃掉（表现为"只能在目标窗口里操作、框选不动"）。
        这里用 SetWindowPos(HWND_TOPMOST) 再兜一道底。
        """
        try:
            import win32con
            import win32gui
            hwnd = int(self.winId())
            win32gui.SetWindowPos(
                hwnd,
                win32con.HWND_TOPMOST,
                0, 0, 0, 0,
                win32con.SWP_NOMOVE | win32con.SWP_NOSIZE | win32con.SWP_SHOWWINDOW,
            )
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            # 诊断：把遮罩自身的窗口样式记一条，便于排查"事件被谁吃了"
            try:
                ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                msg = (
                    f"[Overlay] 遮罩已置顶 hwnd=0x{hwnd:08X} exstyle=0x{ex:08X} "
                    f"(TOPMOST={'Y' if ex & win32con.WS_EX_TOPMOST else 'N'}, "
                    f"TRANSPARENT={'Y' if ex & win32con.WS_EX_TRANSPARENT else 'N'})"
                )
                log = getattr(self._app_window, "_log", None)
                if log is not None and hasattr(log, "append_log"):
                    log.append_log(msg, "DEBUG")
                else:
                    print(msg, flush=True)
            except Exception:
                pass
        except Exception:
            try:
                self.raise_()
                self.activateWindow()
            except Exception:
                pass

    def _show_fullscreen_all_screens(self):
        """跨多屏：把 widget 几何设为所有屏幕的并集。"""
        app = QApplication.instance()
        screens = app.screens() if app else []
        geo = None
        if screens:
            geo = screens[0].geometry()
            for s in screens[1:]:
                geo = geo.united(s.geometry())
        # 快照必须在 show() 之前抓：show() 之后再抓，抓到的就是遮罩自己了
        if self._need_bg_snapshot:
            self._bg_img = self._grab_virtual_screen()
        if geo is None:
            self.showFullScreen()
        else:
            self.setGeometry(geo)
            self.show()
        self._force_topmost_and_focus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.finished.emit(None)
            self._restore_app_window()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        """默认背景：30% 黑色半透明遮罩。"""
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor(0, 0, 0, 77))
        finally:
            painter.end()

    def cancel(self):
        """外部可调用的取消接口。"""
        self.finished.emit(None)
        self._restore_app_window()
