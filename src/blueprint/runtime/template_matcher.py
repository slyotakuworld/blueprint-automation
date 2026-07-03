import os
import time

import cv2
import numpy as np
import pyautogui
from PIL import ImageGrab

from ...config.app_paths import (
    APP_DIR,
    INTERNAL_DIR,
    get_images_dir,
    get_img_path,
)
from ...config.recognition_profiles import DEFAULT_RECOGNITION_PROFILES

MATCH_THRESHOLD = 0.8


class TemplateMatcher:
    """独立的模板匹配服务。蓝图 Runtime 直接实例化使用。"""

    def __init__(self, log_fn=None, is_running_fn=None):
        self._log = log_fn or (lambda *a, **k: None)
        self._is_running = is_running_fn or (lambda: True)
        self.regions = {}
        self.last_positions = {}
        self.template_cache = {}
        self.template_gray_cache = {}
        self.scaled_template_cache = {}
        self.file_template_cache = {}
        self.match_calibration = {}
        self.config = {}
        self.use_split_candidates = False
        self.watched_diagnostic_templates = []

    def log(self, msg, level="INFO"):
        self._log(msg, level)

    @property
    def is_running(self):
        return bool(self._is_running())

    def set_calibration(self, preferred_scale=None, gray_threshold_offset=None):
        if preferred_scale is not None:
            self.match_calibration["preferred_scale"] = float(preferred_scale)
        if gray_threshold_offset is not None:
            self.match_calibration["gray_threshold_offset"] = float(gray_threshold_offset)

    def set_region(self, name, rect):
        if name and rect and len(rect) == 4:
            self.regions[name] = tuple(int(v) for v in rect)

    def set_fullscreen_region(self, rect):
        self.set_region("全界面", rect)

    # ---------- 模板加载 + 缓存 ----------
    def load_template(self, template_path):
        actual_path = get_img_path(template_path)
        cache_key = actual_path
        if cache_key in self.template_cache:
            return self.template_cache[cache_key], actual_path
        tpl = cv2.imread(actual_path, cv2.IMREAD_COLOR)
        if tpl is not None:
            self.template_cache[cache_key] = tpl
        return tpl, actual_path

    def load_template_gray(self, template_path):
        actual_path = get_img_path(template_path)
        cache_key = ("gray", actual_path)
        if cache_key in self.template_gray_cache:
            return self.template_gray_cache[cache_key]
        tpl = cv2.imread(actual_path, cv2.IMREAD_GRAYSCALE)
        if tpl is not None:
            self.template_gray_cache[cache_key] = tpl
        return tpl

    def get_images_root_dir(self):
        ext_dir = get_images_dir(APP_DIR)
        if ext_dir:
            return ext_dir
        int_dir = get_images_dir(INTERNAL_DIR)
        if int_dir:
            return int_dir
        return None

    def capture_region(self, region=None, mask_areas=None):
        try:
            if region:
                x, y, w, h = region
                bbox = (int(x), int(y), int(x + w), int(y + h))
                screen = ImageGrab.grab(bbox=bbox, all_screens=True)
            else:
                screen = ImageGrab.grab(all_screens=True)
        except Exception:
            screen = pyautogui.screenshot(region=region)
        screen_bgr = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
        if mask_areas:
            for rect in mask_areas:
                try:
                    mx1, my1, mx2, my2 = rect
                    mx1 = max(0, int(mx1))
                    my1 = max(0, int(my1))
                    mx2 = min(screen_bgr.shape[1], int(mx2))
                    my2 = min(screen_bgr.shape[0], int(my2))
                    if mx2 > mx1 and my2 > my1:
                        screen_bgr[my1:my2, mx1:mx2] = 0
                except Exception:
                    pass
        return screen_bgr

    def get_scales_to_try(self, fast_mode=True):
        full_region = self.regions.get("全界面")
        curr_w = full_region[2] if full_region else pyautogui.size()[0]
        scales = []
        calib = self.match_calibration or {}
        preferred_scale = float(calib.get("preferred_scale", 1.0) or 1.0)
        hinted_scales = []
        for base_width in [1600, 1920, 2560]:
            hinted_scale = curr_w / base_width
            hinted_scales.extend([hinted_scale, hinted_scale * 0.99, hinted_scale * 1.01])

        def add_scale(s):
            s = round(float(s), 3)
            if 0.45 <= s <= 1.8 and s not in scales:
                scales.append(s)

        for s in [
            preferred_scale, *hinted_scales,
            preferred_scale * 0.995, preferred_scale * 1.005,
            preferred_scale * 0.99, preferred_scale * 1.01,
            preferred_scale * 0.985, preferred_scale * 1.015,
            1.0, 0.99, 1.01, 0.985, 1.015, 0.97, 1.03, 0.95, 1.05,
        ]:
            add_scale(s)
        for s in [0.92, 1.08, 0.9, 1.1, 0.88, 1.12, 0.85, 1.15, 0.8, 0.75, 0.7]:
            add_scale(s)
        if fast_mode:
            return scales[:16]
        return scales

    def get_calibrated_gray_threshold(self, threshold):
        calib = self.match_calibration or {}
        offset = float(calib.get("gray_threshold_offset", 0.0) or 0.0)
        adjusted = float(threshold) + offset
        return max(0.50, min(0.98, adjusted))

    def get_scaled_template(self, template_path, scale):
        actual_path = get_img_path(template_path)
        images_dir = self.get_images_root_dir()
        if images_dir and os.path.exists(actual_path):
            try:
                rel_key = os.path.relpath(actual_path, images_dir).replace("\\", "/")
            except Exception:
                rel_key = os.path.basename(actual_path)
        else:
            rel_key = os.path.basename(actual_path)

        mem_key = (actual_path, round(scale, 3))
        if mem_key in self.scaled_template_cache:
            return self.scaled_template_cache[mem_key], actual_path

        scale_key = str(round(scale, 3))
        if rel_key in self.file_template_cache:
            tpl = self.file_template_cache[rel_key].get(scale_key)
            if tpl is not None:
                self.scaled_template_cache[mem_key] = tpl
                return tpl, actual_path

        template_orig, actual_path = self.load_template(template_path)
        if template_orig is None:
            return None, actual_path
        try:
            if scale == 1.0:
                tpl = template_orig.copy()
            else:
                tpl = cv2.resize(template_orig, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
            self.scaled_template_cache[mem_key] = tpl
            return tpl, actual_path
        except Exception:
            return None, actual_path

    def find_image_in_screen(self, screen_bgr, template_path, region=None, threshold=0.75, fast_mode=True):
        try:
            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)
            for scale in scales_to_try:
                tpl_c, _ = self.get_scaled_template(template_path, scale)
                if tpl_c is None:
                    continue
                h, w = tpl_c.shape[:2]
                if h < 5 or w < 5:
                    continue
                if h > screen_bgr.shape[0] or w > screen_bgr.shape[1]:
                    continue
                res = cv2.matchTemplate(screen_bgr, tpl_c, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= threshold:
                    pos = (
                        max_loc[0] + w // 2 + (region[0] if region else 0),
                        max_loc[1] + h // 2 + (region[1] if region else 0),
                    )
                    self.last_positions[template_path] = pos
                    self.log(
                        f"[ImageMatch] 命中: {template_path} | "
                        f"得分: {max_val:.3f} (阈值 {threshold}) | 缩放: {scale:.3f}"
                    )
                    return pos
            return None
        except Exception as e:
            self.log(f"find_image_in_screen 异常: {e}", "WARN")
            return None

    def find_image(self, template_path, region=None, threshold=0.75, fast_mode=True):
        if not self.is_running:
            return None
        try:
            screen_bgr = self.capture_region(region)
            return self.find_image_in_screen(
                screen_bgr, template_path, region=region,
                threshold=threshold, fast_mode=fast_mode,
            )
        except Exception as e:
            self.log(f"查找图片时发生异常: {e}", "WARN")
            return None

    def find_any_image(self, image_list, region=None, threshold=MATCH_THRESHOLD, fast_mode=True):
        if not self.is_running:
            return None
        try:
            screen_bgr = self.capture_region(region)
            for img_path in image_list:
                pos = self.find_image_in_screen(
                    screen_bgr, img_path, region=region,
                    threshold=threshold, fast_mode=fast_mode,
                )
                if pos:
                    return pos
            return None
        except Exception as e:
            self.log(f"find_any_image 异常: {e}", "WARN")
            return None

    def find_image_gray(self, template_path, region=None, threshold=0.75, fast_mode=True, invert_mode=False):
        if not self.is_running:
            return None
        try:
            screen_bgr = self.capture_region(region)
            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)
            effective_threshold = self.get_calibrated_gray_threshold(threshold)
            tpl_gray_raw = self.load_template_gray(template_path)
            if tpl_gray_raw is None:
                return None
            best_score = 0.0
            best_position = None
            for scale in scales_to_try:
                tpl_gray = tpl_gray_raw
                if scale != 1.0:
                    tpl_gray = cv2.resize(tpl_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                h, w = tpl_gray.shape[:2]
                if h < 5 or w < 5 or h > screen_gray.shape[0] or w > screen_gray.shape[1]:
                    continue
                res = cv2.matchTemplate(screen_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val > best_score:
                    best_score = float(max_val)
                    best_position = (
                        max_loc[0] + w // 2 + (region[0] if region else 0),
                        max_loc[1] + h // 2 + (region[1] if region else 0),
                    )
                if max_val >= effective_threshold:
                    self.log(
                        f"[GrayMatch] 命中: {template_path} | 得分: {max_val:.3f} "
                        f"(阈值 {effective_threshold}) | 缩放: {scale:.3f}"
                    )
                    return (
                        max_loc[0] + w // 2 + (region[0] if region else 0),
                        max_loc[1] + h // 2 + (region[1] if region else 0),
                    )
                if invert_mode:
                    tpl_inv = 255 - tpl_gray
                    res_inv = cv2.matchTemplate(screen_gray, tpl_inv, cv2.TM_CCOEFF_NORMED)
                    _, max_val_inv, _, max_loc_inv = cv2.minMaxLoc(res_inv)
                    if max_val_inv >= effective_threshold:
                        self.log(
                            f"[GrayMatch] 命中(反相): {template_path} | 得分: {max_val_inv:.3f} "
                            f"(阈值 {effective_threshold}) | 缩放: {scale:.3f}"
                        )
                        return (
                            max_loc_inv[0] + w // 2 + (region[0] if region else 0),
                            max_loc_inv[1] + h // 2 + (region[1] if region else 0),
                        )
            return None
        except Exception as e:
            self.log(f"find_image_gray 异常: {e}", "WARN")
            return None

    def find_any_image_gray(self, image_list, region=None, threshold=0.75, fast_mode=True, invert_mode=False):
        if not self.is_running:
            return None
        try:
            for img_path in image_list:
                pos = self.find_image_gray(
                    img_path, region=region, threshold=threshold,
                    fast_mode=fast_mode, invert_mode=invert_mode,
                )
                if pos:
                    return pos
            return None
        except Exception as e:
            self.log(f"find_any_image_gray 异常: {e}", "WARN")
            return None

    # ---------- 等待版 ----------
    def wait_for_image(self, template_path, region=None, threshold=0.75, timeout=30, interval=0.30, fast_mode=True):
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_image(template_path, region=region, threshold=threshold, fast_mode=fast_mode)
            if pos:
                return pos
            time.sleep(interval)
        return None

    def wait_for_any_image(self, image_list, region=None, threshold=0.80, timeout=30, interval=0.30, fast_mode=True):
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_any_image(image_list, region=region, threshold=threshold, fast_mode=fast_mode)
            if pos:
                return pos
            time.sleep(interval)
        return None

    def wait_for_image_gray(self, template_path, region=None, threshold=0.75, timeout=30, interval=0.30, fast_mode=True, invert_mode=False):
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_image_gray(
                template_path, region=region, threshold=threshold,
                fast_mode=fast_mode, invert_mode=invert_mode,
            )
            if pos:
                return pos
            time.sleep(interval)
        return None

    def wait_for_any_image_gray(self, image_list, region=None, threshold=0.75, timeout=30, interval=0.30, fast_mode=True, invert_mode=False):
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_any_image_gray(
                image_list, region=region, threshold=threshold,
                fast_mode=fast_mode, invert_mode=invert_mode,
            )
            if pos:
                return pos
            time.sleep(interval)
        return None

    @staticmethod
    def get_profile(key, **overrides):
        base = dict(DEFAULT_RECOGNITION_PROFILES.get(key, {}))
        if overrides:
            base.update({k: v for k, v in overrides.items() if v is not None})
        return base
