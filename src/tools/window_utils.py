import os
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import win32con


def _enumerate_windows_win32() -> List[Dict[str, Any]]:
    """枚举所有可见顶层窗口，返回 [{hwnd, title, class, pid, exe}] 列表。"""
    import win32gui
    import win32process

    results: List[Dict[str, Any]] = []

    def _enum_cb(hwnd: int, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return
            title = win32gui.GetWindowText(hwnd) or ""
            if not title:
                return
            if title in ("Program Manager", "Windows Input Experience", "MSCTFIME UI"):
                return
            style_ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
            if style_ex & win32con.WS_EX_TOOLWINDOW:
                return
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
            except Exception:
                pid = 0
            try:
                cls_name = win32gui.GetClassName(hwnd) or ""
            except Exception:
                cls_name = ""
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                rect = (l, t, r - l, b - t)
                if rect[2] < 32 or rect[3] < 32:
                    return
            except Exception:
                rect = (0, 0, 0, 0)
            results.append({
                "hwnd": int(hwnd),
                "title": title,
                "class": cls_name,
                "pid": int(pid),
                "rect": rect,
                "exe": "",
            })
        except Exception:
            pass

    win32gui.EnumWindows(_enum_cb, None)
    results.sort(key=lambda r: r["title"].lower())
    return results


def list_visible_windows() -> List[Dict[str, Any]]:
    """跨平台安全的窗口枚举接口，非Win32返回空列表。"""
    try:
        return _enumerate_windows_win32()
    except Exception:
        return []


def start_exe(
    exe_path: str,
    args: Optional[List[str]] = None,
    working_dir: Optional[str] = None,
    wait_sec_after_launch: float = 0.0,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, Optional[int], str]:
    """
    启动外部程序。

    返回 (ok, pid_or_None, message)。
    """
    log = log_fn or (lambda m, l="INFO": None)
    if not exe_path:
        return False, None, "exe_path 为空"
    if not os.path.isfile(exe_path):
        # 尝试用 PATH 查找
        if not exe_path.lower().endswith(".exe"):
            candidate = exe_path + ".exe"
        else:
            candidate = None
        if candidate and os.path.isfile(candidate):
            exe_path = candidate
        else:
            import shutil
            if shutil.which(exe_path) is None:
                return False, None, f"未找到可执行文件: {exe_path}"
    cwd = working_dir if working_dir and os.path.isdir(working_dir) else os.path.dirname(exe_path) or None
    cmd = [exe_path] + (args or [])
    log(f"[WindowUtils] 启动: {' '.join(cmd)}", "INFO")
    try:
        if cwd:
            proc = subprocess.Popen(cmd, cwd=cwd)
        else:
            proc = subprocess.Popen(cmd)
    except Exception as e:
        return False, None, f"启动失败: {e}"
    pid = proc.pid
    if wait_sec_after_launch > 0:
        log(f"[WindowUtils] 等待 {wait_sec_after_launch}s 让窗口出现...", "INFO")
        time.sleep(wait_sec_after_launch)
    return True, pid, f"已启动 PID={pid}"


def bring_window_to_front(hwnd: int, log_fn: Optional[Callable[[str, str], None]] = None) -> bool:
    log = log_fn or (lambda m, l="INFO": None)
    try:
        import win32gui
        if not win32gui.IsWindow(hwnd):
            log(f"[WindowUtils] HWND {hwnd} 已失效", "WARN")
            return False
        # 只在窗口最小化时才 SW_RESTORE（恢复到之前的状态：最大化或普通）
        # 最大化的窗口保持最大化不动，避免尺寸变化导致坐标错位
        try:
            if win32gui.IsIconic(hwnd):
                win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
                time.sleep(0.05)
        except Exception:
            pass
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            # 回退：Alt 键切换焦点
            try:
                import pydirectinput
                pydirectinput.keyDown("alt")
                win32gui.SetForegroundWindow(hwnd)
                pydirectinput.keyUp("alt")
            except Exception:
                return False
        time.sleep(0.15)
        return True
    except Exception as e:
        log(f"[WindowUtils] bring_to_front 失败: {e}", "WARN")
        return False


def get_window_rect(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    try:
        import win32gui
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        return (l, t, r - l, b - t)
    except Exception:
        return None


def get_window_rect_dwm(hwnd: int) -> Optional[Tuple[int, int, int, int]]:
    """获取窗口可见矩形 (left, top, right, bottom)：DWM 扩展边界优先，GetWindowRect 兜底。

    「编辑器 / 运行时口径一致」的关键落点之一：
      - 编辑器侧框选 ROI / 拾取窗口的基准是 enumerate_valid_windows() 的 rect，
        它走 DWMWA_EXTENDED_FRAME_BOUNDS（用户看得见的窗口边界）；
      - Win10/11 的裸 GetWindowRect 比可见边界大——左右各含约 7px 不可见边框、
        顶部含不可见边框，若运行时仍用它裁窗口，ROI 会整体偏移几个像素。
    所以运行时裁窗口（OCR/Screenshot/Mouse 相对坐标/FindImage 搜索区）一律用本函数。
    """
    try:
        import ctypes
        from ctypes import wintypes

        DWMWA_EXTENDED_FRAME_BOUNDS = 9
        rect = wintypes.RECT()
        dwmapi = ctypes.windll.dwmapi
        if dwmapi.DwmGetWindowAttribute(
            int(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(rect), ctypes.sizeof(rect),
        ) == 0:
            return (rect.left, rect.top, rect.right, rect.bottom)
    except Exception:
        pass
    try:
        import win32gui
        return tuple(win32gui.GetWindowRect(hwnd))
    except Exception:
        return None


def find_hwnd_by_title_substring(
    title_sub: str,
    pid: Optional[int] = None,
    log_fn: Optional[Callable[[str, str], None]] = None,
) -> Optional[int]:
    """
    按标题子串+可选PID过滤找窗口。
    体验教训（ID: 1043761）：必须句柄优先 + PID二次校验，避免误选相同标题的其他窗口。
    """
    log = log_fn or (lambda m, l="INFO": None)
    if not title_sub and pid is None:
        return None
    wins = list_visible_windows()
    candidates = []
    for w in wins:
        if pid is not None and w["pid"] != pid:
            continue
        if title_sub and title_sub.lower() not in w["title"].lower():
            continue
        candidates.append(w)
    if not candidates:
        log(f"[WindowUtils] 未找到匹配窗口: title_sub='{title_sub}' pid={pid}", "WARN")
        return None
    # 优先 PID 完全匹配的
    if pid is not None:
        for c in candidates:
            if c["pid"] == pid:
                return c["hwnd"]
    return candidates[0]["hwnd"]
