import ctypes
import os
from ctypes import wintypes
from typing import Any, Dict, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from .fullscreen_overlay import FullscreenOverlayBase

# Win32 常量
GW_OWNER = 4
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14

# 系统窗口类名（拾取时跳过）
_EXCLUDED_CLASSES = frozenset({
    "Progman", "Shell_TrayWnd", "Button", "Static", "WorkerW",
    "Windows.UI.Core.CoreWindow", "EdgeUiInputTopWnd", "ApplicationFrameWindow",
})

_user32 = ctypes.windll.user32
_dwmapi = ctypes.windll.dwmapi
_kernel32 = ctypes.windll.kernel32

# EnumWindows 回调签名（c_bool, c_int, c_int）
_WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)


def _get_cursor_pos() -> tuple:
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return (pt.x, pt.y)


def _get_window_rect_dwm(hwnd: int) -> Optional[tuple]:
    """获取窗口矩形，优先用 DWM 扩展边界（更准确），失败兜底 GetWindowRect。"""
    rect = wintypes.RECT()
    if _dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
        ctypes.byref(rect), ctypes.sizeof(rect),
    ) == 0:
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    if _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)
    return None


def _get_exe_name(pid: int) -> str:
    """通过 Toolhelp32 进程快照获取 exe 名。"""
    if not pid:
        return ""
    TH32CS_SNAPPROCESS = 0x00000002

    class PROCESSENTRY32(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_char * 260),
        ]

    try:
        hSnap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if hSnap == -1 or hSnap == 0:
            return ""
        try:
            pe32 = PROCESSENTRY32()
            pe32.dwSize = ctypes.sizeof(PROCESSENTRY32)
            if _kernel32.Process32First(hSnap, ctypes.byref(pe32)):
                while True:
                    if pe32.th32ProcessID == pid:
                        try:
                            return pe32.szExeFile.decode("gbk", "ignore")
                        except Exception:
                            try:
                                return pe32.szExeFile.decode("utf-8", "ignore")
                            except Exception:
                                return ""
                    if not _kernel32.Process32Next(hSnap, ctypes.byref(pe32)):
                        break
        finally:
            _kernel32.CloseHandle(hSnap)
    except Exception:
        return ""
    return ""


def _is_window_valid_target(hwnd: int, my_pid: int) -> bool:
    """过滤无效窗口的逻辑。"""
    if not _user32.IsWindowVisible(hwnd):
        return False
    if _user32.IsIconic(hwnd):
        return False
    if _user32.GetWindow(hwnd, GW_OWNER) != 0:
        return False
    is_cloaked = ctypes.c_int(0)
    _dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_CLOAKED,
        ctypes.byref(is_cloaked), ctypes.sizeof(is_cloaked),
    )
    if is_cloaked.value != 0:
        return False
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == my_pid:
        return False
    cls_buff = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, cls_buff, 256)
    if cls_buff.value in _EXCLUDED_CLASSES:
        return False
    rect = wintypes.RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w < 10 or h < 10:
        return False
    length = _user32.GetWindowTextLengthW(hwnd)
    if length == 0 and cls_buff.value == "ApplicationFrameWindow":
        return False
    return True


def _get_window_info(hwnd: int) -> Optional[Dict[str, Any]]:
    """获取窗口详细信息。"""
    length = _user32.GetWindowTextLengthW(hwnd)
    title = ""
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
    cls_buff = ctypes.create_unicode_buffer(256)
    _user32.GetClassNameW(hwnd, cls_buff, 256)
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    rect = _get_window_rect_dwm(hwnd)
    if rect is None:
        return None
    return {
        "hwnd": int(hwnd),
        "title": title,
        "class_name": cls_buff.value,
        "exe_name": _get_exe_name(pid.value),
        "pid": int(pid.value),
        "rect": (int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])),
    }


def enumerate_valid_windows() -> list:
    """枚举所有有效目标窗口。"""
    my_pid = os.getpid()
    results = []

    def _cb(hwnd, _lparam):
        if _is_window_valid_target(hwnd, my_pid):
            info = _get_window_info(hwnd)
            if info:
                results.append(info)
        return True

    # 保持回调引用避免被 GC（ctypes 已知坑）
    cb = _WNDENUMPROC(_cb)
    _user32.EnumWindows(cb, 0)
    return results


def get_top_window_at_mouse() -> Optional[Dict[str, Any]]:
    """获取光标下的顶层窗口。
    用 EnumWindows 枚举所有有效窗口，返回第一个矩形包含光标点的。"""
    cx, cy = _get_cursor_pos()
    for win in enumerate_valid_windows():
        l, t, w, h = win["rect"]
        if l <= cx < l + w and t <= cy < t + h:
            return win
    return None


class WindowPickerOverlay(FullscreenOverlayBase):
    """智能拾取窗口全屏遮罩。

    流程：
      1. 主窗隐藏 → 全屏半透明遮罩
      2. 定时器 + 鼠标移动 双机制实时检测光标下的顶层窗口
      3. 绿色实线 4px 高亮其矩形，顶部显示进程名 / 类名 / 标题
      4. 左键点击 → 拾取该窗口，emit finished({hwnd,title,exe_name,class_name,pid})
      5. ESC / 右键 → 取消，emit finished(None)

    注：全屏遮罩交互
    偶尔会让 mouseMoveEvent 不稳定，因此这里用 QTimer 轮询作为主检测机制，
    mouseMoveEvent 作为辅助刷新。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._hover_win: Optional[Dict[str, Any]] = None
        self.setMouseTracking(True)
        # 轮询定时器：每 60ms 检测一次光标下的窗口（主检测机制）
        self._poll_timer = QTimer(self)
        self._poll_timer.setInterval(60)
        self._poll_timer.timeout.connect(self._poll_cursor)
        # 调试日志开关（默认关，出问题时手动开）
        self._debug = bool(os.environ.get("BP_PICKER_DEBUG"))

    def _log(self, msg: str):
        if not self._debug:
            return
        try:
            log_path = os.path.join("debug", "window_picker.log")
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except Exception:
            pass

    def showEvent(self, event):
        super().showEvent(event)
        self.raise_()
        self.activateWindow()
        self.setFocus()
        self._log(f"[showEvent] geometry={self.geometry()}")
        # 启动轮询
        self._poll_timer.start()
        self._log("[showEvent] poll timer started")

    def hideEvent(self, event):
        self._poll_timer.stop()
        super().hideEvent(event)

    def _poll_cursor(self):
        """定时轮询光标位置，检测窗口（主检测机制，不依赖 Qt 鼠标事件）。"""
        try:
            win = get_top_window_at_mouse()
            if win != self._hover_win:
                self._log(
                    f"[poll] 切换窗口: "
                    f"{self._hover_win and self._hover_win.get('exe_name')} -> "
                    f"{win and win.get('exe_name')}"
                )
            self._hover_win = win
            self.update()
        except Exception as e:
            self._log(f"[poll] 异常: {e!r}")

    # ---------------- 鼠标事件（辅助刷新） ----------------
    def mouseMoveEvent(self, event):
        self._poll_cursor()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            win = self._hover_win or get_top_window_at_mouse()
            self._log(f"[click] 左键, win={win and win.get('exe_name')}")
            if win and win.get("hwnd"):
                self.finished.emit(win)
            else:
                self.finished.emit(None)
            self._restore_app_window()
            return
        if event.button() == Qt.RightButton:
            self._log("[click] 右键, 取消")
            self.finished.emit(None)
            self._restore_app_window()
            return

    # ---------------- 绘制 ----------------
    def _screen_to_local(self, x: int, y: int) -> tuple:
        """屏幕坐标 → 遮罩本地坐标（多屏时遮罩起点可能不在 0,0）。"""
        g = self.geometry()
        return (x - g.left(), y - g.top())

    def paintEvent(self, event):
        painter = QPainter(self)
        try:
            # 1. 半透明黑背景
            painter.fillRect(self.rect(), QColor(0, 0, 0, 77))

            # 2. 顶部提示
            self._draw_center_banner(
                painter,
                "🖱️  移动鼠标选择窗口，左键确认拾取  |  ESC / 右键取消",
                QColor(255, 213, 79),
                y_pos=50,
            )

            # 3. 高亮当前窗口
            win = self._hover_win
            if win and win.get("rect"):
                l, t, w, h = win["rect"]
                if w > 0 and h > 0:
                    # 屏幕坐标转本地坐标
                    lx, ly = self._screen_to_local(l, t)
                    # 绿色实线高亮 4px
                    pen = QPen(QColor(0, 255, 0), 4)
                    painter.setPen(pen)
                    painter.setBrush(Qt.NoBrush)
                    painter.drawRect(lx, ly, w, h)
                    # 窗口标题/进程/类名信息卡
                    title = win.get("title", "") or ""
                    exe = win.get("exe_name", "") or ""
                    cls = win.get("class_name", "") or ""
                    info_lines = [
                        f"📄 进程: {exe}",
                        f"🏷️ 类名: {cls}",
                        f"🪟 标题: {title}",
                    ]
                    font = QFont("Microsoft YaHei", 10)
                    painter.setFont(font)
                    fm = painter.fontMetrics()
                    line_h = fm.height() + 2
                    max_w = max(fm.horizontalAdvance(s) for s in info_lines)
                    pad = 8
                    card_w = max_w + pad * 2
                    card_h = line_h * len(info_lines) + pad * 2
                    card_x = lx + 6
                    card_y = ly + 6
                    # 防溢出
                    if card_x + card_w > self.width():
                        card_x = self.width() - card_w - 6
                    if card_y + card_h > self.height():
                        card_y = self.height() - card_h - 6
                    painter.fillRect(card_x, card_y, card_w, card_h, QColor(0, 0, 0, 200))
                    painter.setPen(QColor(255, 255, 255))
                    for i, s in enumerate(info_lines):
                        painter.drawText(
                            card_x + pad,
                            card_y + pad + fm.ascent() + i * line_h,
                            s,
                        )
            else:
                # 没检测到窗口时显示提示
                self._draw_center_banner(
                    painter,
                    "（未检测到光标下的窗口，请确认有可见窗口）",
                    QColor(200, 200, 200),
                    y_pos=100,
                )
        finally:
            painter.end()

    @staticmethod
    def _draw_center_banner(painter: QPainter, text: str, color: QColor, y_pos: int):
        font = QFont("Microsoft YaHei", 14, QFont.Bold)
        painter.setFont(font)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.height()
        x = (painter.device().width() - tw) // 2
        # 背景卡
        pad = 10
        painter.fillRect(
            x - pad, y_pos - pad, tw + pad * 2, th + pad,
            QColor(0, 0, 0, 180),
        )
        painter.setPen(color)
        painter.drawText(x, y_pos + fm.ascent(), text)
