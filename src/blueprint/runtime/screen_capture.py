import os
import ctypes

import cv2
import numpy as np
from PIL import ImageGrab

# PrintWindow 标志：Win8.1+ 支持，强制 DWM 重新合成窗口内容，
# 对 Chrome/Electron/DirectX 渲染的窗口也能拿到真实画面（不带它只会截到白屏）
PW_RENDERFULLCONTENT = 0x00000002


def capture_window_bgr(hwnd):
    """后台截取指定窗口内容（不前置、不还原、不抢焦点）。

    原理：PrintWindow + PW_RENDERFULLCONTENT 让系统把窗口内容直接渲染进
    内存位图——窗口被其它窗口遮挡时也能拿到实时画面（实测 Electron/Chrome
    窗口有效，含 toast 等动态内容）。

    限制与回退约定：
      - 最小化的窗口渲染器停摆，只能截到纯色空帧 → 返回 None，
        调用方应回退「还原+置顶+全屏截取」路径；
      - 输出已按 DWM 可见边界对齐裁剪，与 get_window_rect_dwm /
        「全屏截图+窗口矩形裁剪」路径的 ROI 坐标基准完全一致
        （编辑器/运行时口径一致原则）。

    返回 BGR ndarray；失败返回 None。
    """
    if not hwnd:
        return None
    try:
        import win32gui
        import win32ui
        from src.tools.window_utils import get_window_rect_dwm

        gl, gt, gr, gb = win32gui.GetWindowRect(hwnd)
        w, h = int(gr - gl), int(gb - gt)
        if w <= 0 or h <= 0:
            return None

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        try:
            mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
            save_dc = mfc_dc.CreateCompatibleDC()
            try:
                bmp = win32ui.CreateBitmap()
                bmp.CreateCompatibleBitmap(mfc_dc, w, h)
                save_dc.SelectObject(bmp)
                res = ctypes.windll.user32.PrintWindow(
                    hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
                data = bmp.GetBitmapBits(True)
                win32gui.DeleteObject(bmp.GetHandle())
            finally:
                save_dc.DeleteDC()
                mfc_dc.DeleteDC()
        finally:
            win32gui.ReleaseDC(hwnd, hwnd_dc)

        if not res or not data:
            return None
        img = np.frombuffer(data, dtype=np.uint8).reshape(h, w, 4)
        bgr = img[:, :, :3].copy()

        # 空帧检测：最小化/拒绝渲染的窗口会得到纯色图（全白或全黑）→ 交给调用方回退
        if float(bgr.std()) < 1.0:
            return None

        # 对齐 DWM 可见边界：GetWindowRect 含 Win10/11 不可见边框（左/上约 7~8px），
        # 框选 ROI / 拾取窗口的坐标基准是 DWM 可见边界，必须裁齐口径
        try:
            dwm = get_window_rect_dwm(hwnd)  # (x, y, w, h)
            if dwm:
                cx, cy = int(dwm[0] - gl), int(dwm[1] - gt)
                cw, ch = int(dwm[2]), int(dwm[3])
                if cx < 0:
                    cw += cx; cx = 0
                if cy < 0:
                    ch += cy; cy = 0
                cx, cy = max(0, cx), max(0, cy)
                cw = max(0, min(cw, w - cx))
                ch = max(0, min(ch, h - cy))
                if cw > 5 and ch > 5:
                    bgr = bgr[cy:cy + ch, cx:cx + cw].copy()
        except Exception:
            pass
        return bgr
    except Exception:
        return None


class ScreenCapture:
    """独立的屏幕截图服务，BGR格式输出。

    只做整屏/区域截图；「截某个窗口」用模块级 capture_window_bgr(hwnd)。
    （旧版这里有个 hwnd 字段给「全局目标窗口」用，已随该机制一起删除。）
    """

    def capture_full_bgr(self):
        try:
            screen = ImageGrab.grab(all_screens=True)
            return cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
        except Exception:
            return None

    def capture_region_bgr(self, region):
        try:
            x, y, w, h = region
            bbox = (int(x), int(y), int(x + w), int(y + h))
            screen = ImageGrab.grab(bbox=bbox, all_screens=True)
            return cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
        except Exception:
            return None

    @staticmethod
    def save_png(image_bgr, file_path):
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            ok, buf = cv2.imencode(".png", image_bgr)
            if ok:
                buf.tofile(file_path)
                return True
        except Exception:
            pass
        return False
