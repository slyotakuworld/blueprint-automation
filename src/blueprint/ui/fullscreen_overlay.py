from typing import Any, Optional

from PySide6.QtCore import QEventLoop, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QApplication, QWidget


class FullscreenOverlayBase(QWidget):
    """全屏半透明遮罩基类。

    流程：
      1. 调用方（blueprint_app）调用 prepare_and_run(app)：隐藏主窗 → 显示遮罩
      2. showFullScreen() 覆盖所有显示器，背景黑半透
      3. 子类重写 paintEvent / mousePressEvent / mouseMoveEvent / mouseReleaseEvent
      4. 完成/取消后 emit finished(result)，caller恢复主窗

    ⚠ 「隐藏主窗 → 遮罩接管输入」之间**不允许有等待**（见 prepare_and_run 的 ★）：
      这段时间遮罩不存在，点下去会归当前前台窗口。
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
            delay_ms: 隐藏主窗后延迟多少毫秒再显示遮罩。**0 = 同步立即显示**
                      （不经过事件循环），截图遮罩必须用 0，理由见下 ★。
            *args, **kwargs: 透传给子类构造函数

        ★ 为什么 delay 不能留窗口期（2026-10-04 实测确认的穿透）：
          delay 期间遮罩**还没上屏**，这段时间里的鼠标按下会归"当前前台窗口"。
          而截图流程会先把「临时目标窗口」SetForegroundWindow 提到最前，于是
          用户"点完 📸 按钮、手还没停又点了一下屏幕"的那一次按下就被目标窗口接走；
          Windows 还会把**鼠标捕获**一并给它，于是后续整段拖拽都在目标窗口里发生
          （表现为"截图时目标窗口里出现了框选/选中文本"）。
          所以 delay=0 时必须**同步** show：hide() 与 show() 之间不留任何事件循环
          往返，遮罩从第一帧起就用 alpha=1 接管命中测试（见 _show_fullscreen_all_screens
          的首帧冲刷）。真正的"等桌面重绘"挪到遮罩上屏之后做，见 SnipOverlay.set_limit。

        Returns:
            已创建的 overlay 实例；结果通过 finished signal 异步返回
        """
        app_window.hide()
        overlay = cls(*args, **kwargs)
        overlay._app_window = app_window

        def _on_finished(_res):
            QTimer.singleShot(100, overlay._restore_app_window)

        overlay.finished.connect(_on_finished)
        if delay_ms and delay_ms > 0:
            QTimer.singleShot(delay_ms, overlay._show_fullscreen_all_screens)
        else:
            overlay._show_fullscreen_all_screens()
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

    # ---------------- 置顶 ----------------
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
        if geo is None:
            self.showFullScreen()
        else:
            self.setGeometry(geo)
            self.show()
        self._force_topmost_and_focus()
        # ★ 首帧同步上屏（重要）：show() 只是把"显示"请求排进 Qt 的窗口系统事件队列，
        #   原生窗口真正映射 + 首帧内容合成上屏要等队列被冲刷。这段窗口期内窗口像素
        #   还全是 alpha=0，而分层窗口的命中测试会把 alpha==0 当成"不存在" ——
        #   于是"刚点开截图就拖拽"的那一次按下会漏给下层窗口（实测 58ms 后才可命中）。
        #   这里同步冲刷一次队列，把窗口期压到 0（实测 processEvents 一次即可命中）。
        #   ExcludeUserInputEvents：不处理用户输入事件，避免在计时器回调里重入输入处理；
        #   窗口映射/曝光类事件仍会被冲刷。
        app2 = QApplication.instance()
        if app2 is not None:
            try:
                app2.processEvents(QEventLoop.ExcludeUserInputEvents)
            except Exception:
                pass

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
