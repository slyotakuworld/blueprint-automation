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


# ============================================================
# HWND 截图（支持游戏前台无切后台）：双策略
#   A. 窗口在前台时 → BitBlt (能抓 D3D/Vulkan/OpenGL 前台帧，游戏不会黑屏)
#   B. 窗口在后台时 → PrintWindow (最小化/遮挡也能抓，但 D3D 后台可能是黑)
# ============================================================
def capture_hwnd_bgr(
    hwnd: int,
    prefer_front: bool = True,
) -> Optional[Tuple[Any, Any, Tuple[int, int]]]:
    """
    捕获指定 HWND 的窗口图像（ElementSplitDialog 截图与本函数行为一致）。

    策略：
      A. 若 prefer_front=True 且窗口当前是前台窗口 → PIL.ImageGrab.grab(bbox=窗口屏幕Rect)
         ⚠️ 游戏/视频/D3D/Vulkan 前台帧必须用这种方式！因为 D3D 前台缓冲区用 BitBlt/PrintWindow 抓都是黑的。
      B. 否则（后台/最小化/遮挡）→ PrintWindow(PW_RENDERFULLCONTENT=3)
         Win10+ 能抓到部分后台窗口的真实渲染内容，最小化窗口也能抓。

    返回：(bgr_ndarray shape=(H,W,3), pil_image_rgb, (width,height))
        失败返回 None。
    """
    if not hwnd:
        return None
    import numpy as np
    from PIL import Image, ImageGrab
    import win32gui
    import win32ui
    import win32con

    if not win32gui.IsWindow(hwnd):
        return None

    # 处理最小化窗口：GetWindowRect 会返回 (-32000, -32000) 等负值，
    # 需要用 GetWindowPlacement 的 rcNormalPosition 拿到还原后的真实屏幕Rect
    is_minimized = bool(win32gui.IsIconic(hwnd))
    if is_minimized:
        try:
            placement = win32gui.GetWindowPlacement(hwnd)
            # placement[4] == rcNormalPosition: (left, top, right, bottom)
            left, top, right, bottom = placement[4]
            w = right - left
            h = bottom - top
        except Exception:
            try:
                left, top, right, bottom = win32gui.GetClientRect(hwnd)
                w = right - left
                h = bottom - top
            except Exception:
                return None
    else:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        w = right - left
        h = bottom - top
    if w <= 0 or h <= 0:
        return None

    # 注意：只有非最小化 + 真正在前台（焦点窗口就是自己）时，策略A ImageGrab才有效
    is_foreground = (not is_minimized) and (win32gui.GetForegroundWindow() == hwnd)

    # ---- 策略A（推荐）：窗口在前台 → ImageGrab 抓屏幕区域（兼容游戏前台帧）----
    if prefer_front and is_foreground:
        try:
            # all_screens=True 兼容多显示器（窗口如果在副屏上也能抓对）
            pil_rgb = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
            bgr = np.array(pil_rgb.convert("RGB"))[:, :, ::-1].copy()
            return bgr, pil_rgb.convert("RGB"), (w, h)
        except Exception as e:
            print(f"[WindowUtils] capture_hwnd_bgr 策略A ImageGrab失败，回退策略B: {e}")
            # 失败回退到策略B

    # ---- 策略B：后台/最小化窗口 → PrintWindow 兜底 ----
    mfc_dc = None
    save_dc = None
    gdi_bitmap = None
    _hwnd_dc_handle = 0  # 如果通过 GetWindowDC 拿到的 DC，需在finally里Release
    try:
        try:
            # 0 = 屏幕DC；在部分受限会话（RDP/UAC弹窗后/非桌面会话）可能 CreateDCFromHandle(0) 失败
            mfc_dc = win32ui.CreateDCFromHandle(0)
        except Exception:
            # 兜底：直接用目标窗口自己的 WindowDC（不跨屏幕、但普通桌面窗口大概率可用）
            _hwnd_dc_handle = win32gui.GetWindowDC(hwnd)
            mfc_dc = win32ui.CreateDCFromHandle(_hwnd_dc_handle)
        save_dc = mfc_dc.CreateCompatibleDC()
        gdi_bitmap = win32ui.CreateBitmap()
        gdi_bitmap.CreateCompatibleBitmap(mfc_dc, w, h)
        save_dc.SelectObject(gdi_bitmap)
        try:
            _ctypes = __import__("ctypes")
            PW_RENDERFULLCONTENT = 3
            ok = _ctypes.windll.user32.PrintWindow(
                int(hwnd), int(save_dc.GetHandleOutput()), PW_RENDERFULLCONTENT,
            )
            if not ok:
                win32gui.PrintWindow(hwnd, save_dc.GetHandleOutput(), 0)
        except Exception:
            try:
                win32gui.PrintWindow(hwnd, save_dc.GetHandleOutput(), 0)
            except Exception:
                return None
        # 读取 bits：优先 GetBitmapBits(True) → 32 位 BGRA
        bgr = None
        try:
            bits = gdi_bitmap.GetBitmapBits(True)
            if isinstance(bits, (bytes, bytearray)) and len(bits) >= w * h * 4:
                arr = np.frombuffer(bits, dtype=np.uint8).reshape(h, w, 4)
                bgr = arr[:, :, :3].copy()
        except Exception:
            pass
        if bgr is None:
            stride = ((w * 3 + 3) // 4) * 4
            bgr = _getdibits_bgr(save_dc.GetHandleOutput(), gdi_bitmap.GetHandle(), w, h, stride)
        if bgr is None:
            return None
        rgb = bgr[:, :, ::-1].copy()
        pil_rgb = Image.fromarray(rgb, "RGB")
        return bgr, pil_rgb, (w, h)
    except Exception as e:
        print(f"[WindowUtils] capture_hwnd_bgr 策略B PrintWindow 失败: {e}")
        return None
    finally:
        try:
            if gdi_bitmap is not None:
                win32gui.DeleteObject(gdi_bitmap.GetHandle())
        except Exception:
            pass
        try:
            if save_dc is not None:
                save_dc.DeleteDC()
        except Exception:
            pass
        try:
            if mfc_dc is not None:
                mfc_dc.DeleteDC()
        except Exception:
            pass
        try:
            if _hwnd_dc_handle:
                # 通过 GetWindowDC 拿到的句柄，不能用 DeleteDC，必须用 ReleaseWindowDC
                win32gui.ReleaseDC(hwnd, _hwnd_dc_handle)
        except Exception:
            pass


def _getdibits_bgr(hdc_mem: int, hbmp: int, w: int, h: int, stride: int):
    """GetDIBits 兜底，返回 (H,W,3) BGR ndarray 或 None。"""
    try:
        import numpy as np
        import ctypes
        from ctypes import wintypes

        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [
                ("biSize", wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD),
            ]

        DIB_RGB_COLORS = 0
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth = w
        bih.biHeight = h  # top-down: 正数表示 bottom-up（我们读完再flip）
        bih.biPlanes = 1
        bih.biBitCount = 24
        bih.biCompression = 0  # BI_RGB

        buf_len = stride * h
        buf = (ctypes.c_ubyte * buf_len)()
        gdi32 = ctypes.windll.gdi32
        res = gdi32.GetDIBits(
            int(hdc_mem), int(hbmp), 0, h,
            ctypes.byref(buf), ctypes.byref(bih), DIB_RGB_COLORS,
        )
        if res == 0:
            return None
        arr = np.frombuffer(buf, dtype=np.uint8).reshape(h, stride)[:, :w * 3]
        bgr = arr.reshape(h, w, 3).copy()
        # DIB bottom-up → 翻转第一维
        bgr = bgr[::-1, :, :].copy()
        return bgr
    except Exception:
        return None


def write_png_cn(path: str, bgr_img) -> bool:
    """
    写入 PNG（兼容路径含中文）。
    bgr_img: np.ndarray shape=(H,W,3) BGR 顺序（cv2.imread 的默认顺序）
    """
    try:
        import os
        import numpy as np
        import cv2
        if bgr_img is None:
            return False
        if not isinstance(bgr_img, np.ndarray) or bgr_img.ndim not in (2, 3):
            return False
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        ok, buf = cv2.imencode(".png", bgr_img)
        if ok:
            buf.tofile(path)
            return True
    except Exception:
        pass
    return False
