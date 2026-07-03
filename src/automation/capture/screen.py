import os
import time
import threading

import cv2
import numpy as np
from PIL import ImageGrab


def get_screenshot_dir(app_dir):
    shot_dir = os.path.join(app_dir, "screenshots")
    os.makedirs(shot_dir, exist_ok=True)
    return shot_dir


def generate_screenshot_path(app_dir, prefix="shot"):
    shot_dir = get_screenshot_dir(app_dir)
    ts = time.strftime("%Y%m%d_%H%M%S")
    ms = int(time.time() * 1000) % 1000
    filename = f"{prefix}_{ts}_{ms:03d}.png"
    return os.path.join(shot_dir, filename)


def save_screenshot(image_bgr, file_path):
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        ok, buf = cv2.imencode(".png", image_bgr)
        if ok:
            buf.tofile(file_path)
            return True
    except Exception:
        pass
    return False


def capture_full_screen_bgr():
    try:
        screen = ImageGrab.grab(all_screens=True)
        return cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def capture_region_bgr(region):
    try:
        x, y, w, h = region
        bbox = (int(x), int(y), int(x + w), int(y + h))
        screen = ImageGrab.grab(bbox=bbox, all_screens=True)
        return cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def pil_to_bgr(pil_img):
    return cv2.cvtColor(np.array(pil_img), cv2.COLOR_RGB2BGR)
