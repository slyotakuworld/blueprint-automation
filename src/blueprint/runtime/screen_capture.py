import os

import cv2
import numpy as np
from PIL import ImageGrab



class ScreenCapture:
    """独立的屏幕截图服务，BGR格式输出。"""

    def __init__(self, hwnd=None):
        self.hwnd = hwnd

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
