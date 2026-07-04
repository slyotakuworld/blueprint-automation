from typing import Any, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


class FullscreenOverlayBase(QWidget):
    """全屏半透明遮罩基类。

    流程：
      1. 调用方（blueprint_app）调用 prepare_and_run(app)：隐藏主窗，延迟400ms
      2. showFullScreen() 覆盖所有显示器，背景黑半透
      3. 子类重写 paintEvent / mousePressEvent / mouseMoveEvent / mouseReleaseEvent
      4. 完成/取消后 emit finished(result)，caller恢复主窗
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

    def _show_fullscreen_all_screens(self):
        """跨多屏：把 widget 几何设为所有屏幕的并集。"""
        app = QApplication.instance()
        screens = app.screens() if app else []
        if not screens:
            self.showFullScreen()
            return
        geo = screens[0].geometry()
        for s in screens[1:]:
            geo = geo.united(s.geometry())
        self.setGeometry(geo)
        self.show()

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
