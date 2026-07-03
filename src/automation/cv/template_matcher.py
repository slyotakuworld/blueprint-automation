import os
import time
import json
import pickle

import cv2
import numpy as np
import pyautogui
from PIL import ImageGrab

from ...config.app_paths import (
    APP_DIR,
    INTERNAL_DIR,
    cache_dir,
    template_cache_file,
    template_meta_file,
    get_images_dir,
    get_img_path,
)
from ...config.recognition_profiles import get_recognition_profile

try:
    from .splitter_integration import build_candidate_boxes_from_image
    _SPLITTER_AVAILABLE = True
except Exception:
    _SPLITTER_AVAILABLE = False
    build_candidate_boxes_from_image = None

MATCH_THRESHOLD = 0.8


class TemplateMatcherMixin:
    # ==========================================
    def should_capture_diagnostic_template(self, template_name):
        watched = set(getattr(self, "watched_diagnostic_templates", None) or [])
        return os.path.basename(str(template_name or "")) in watched

    def capture_diagnostic_template_miss(self, template_name, *, region=None, screen_bgr=None, best_score=None, threshold=None, extra=None):
        if not hasattr(self, "capture_diagnostic_snapshot"):
            return None
        if not self.should_capture_diagnostic_template(template_name):
            return None

        region_name = next((name for name, rect in self.regions.items() if rect == region), "custom") if region else "全界面"
        safe_name = os.path.basename(str(template_name or "template")).replace(".", "_")
        meta = {
            "template": template_name,
            "region": region_name,
            "best_score": round(float(best_score or 0.0), 4),
            "threshold": threshold,
        }
        if extra:
            meta["extra"] = extra

        return self.capture_diagnostic_snapshot(
            f"miss_{safe_name}",
            region=region,
            image_bgr=screen_bgr,
            reason=f"关键模板未命中: {template_name}",
            level="WARN",
            meta=meta,
            dedupe_key=f"miss:{template_name}:{region_name}",
        )

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
        if not hasattr(self, "template_gray_cache"):
            self.template_gray_cache = {}
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

    def get_template_meta(self):
        images_dir = self.get_images_root_dir()
        meta_data = {}
        if not images_dir:
            return meta_data

        for root, _, files in os.walk(images_dir):
            for file in files:
                if not file.lower().endswith((".png", ".jpg", ".jpeg", ".bmp")):
                    continue

                path = os.path.join(root, file)
                rel_path = os.path.relpath(path, images_dir).replace("\\", "/")

                try:
                    stat = os.stat(path)
                    meta_data[rel_path] = {
                        "mtime": stat.st_mtime,
                        "size": stat.st_size,
                    }
                except Exception:
                    pass

        return meta_data

    def is_template_cache_valid(self):
        cache_file = template_cache_file()
        meta_file = template_meta_file()
        if not os.path.exists(cache_file) or not os.path.exists(meta_file):
            return False

        try:
            with open(meta_file, "r", encoding="utf-8") as f:
                old_meta = json.load(f)
        except Exception:
            return False

        new_meta = self.get_template_meta()
        return old_meta == new_meta

    def build_template_file_cache(self):
        self.log("开始构建模板缓存文件...")
        cache_d = cache_dir()
        os.makedirs(cache_d, exist_ok=True)
        cache_file = template_cache_file()
        meta_file = template_meta_file()

        images_dir = self.get_images_root_dir()
        if not images_dir:
            self.log("未找到 images 目录，无法构建模板缓存。")
            return False

        cache_data = {}
        meta_data = self.get_template_meta()

        scales = self.get_scales_to_try(fast_mode=False)

        for rel_path in meta_data.keys():
            img_path = os.path.join(images_dir, rel_path)
            tpl = cv2.imread(img_path, cv2.IMREAD_COLOR)
            if tpl is None:
                continue

            cache_data[rel_path] = {}
            for scale in scales:
                try:
                    if scale == 1.0:
                        scaled = tpl.copy()
                    else:
                        scaled = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

                    cache_data[rel_path][str(round(scale, 3))] = scaled
                except Exception:
                    continue

        try:
            with open(cache_file, "wb") as f:
                pickle.dump(cache_data, f, protocol=pickle.HIGHEST_PROTOCOL)

            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(meta_data, f, ensure_ascii=False, indent=2)

            self.log("模板缓存文件构建完成。")
            return True
        except Exception as e:
            self.log(f"写入模板缓存失败: {e}")
            return False

    def load_template_file_cache(self):
        cache_file = template_cache_file()
        try:
            with open(cache_file, "rb") as f:
                self.file_template_cache = pickle.load(f)
            self.log("模板缓存文件加载成功。")
            return True
        except Exception as e:
            self.log(f"加载模板缓存失败: {e}")
            self.file_template_cache = {}
            return False

    def prepare_template_cache(self):
        os.makedirs(cache_dir(), exist_ok=True)

        if self.is_template_cache_valid():
            if self.load_template_file_cache():
                return

        self.log("模板缓存不存在或已失效，开始后台重建（这可能需要几秒钟）...")
        if self.build_template_file_cache():
            self.template_cache.clear()
            self.scaled_template_cache.clear()
            self.load_template_file_cache()

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

        # 对指定区域打黑块，避免重复识别同一个目标。
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
        calib = getattr(self, "match_calibration", {}) or {}
        preferred_scale = float(calib.get("preferred_scale", 1.0) or 1.0)
        hinted_scales = []
        for base_width in [1600, 1920, 2560]:
            hinted_scale = curr_w / base_width
            hinted_scales.extend([
                hinted_scale,
                hinted_scale * 0.99,
                hinted_scale * 1.01,
            ])

        def add_scale(s):
            s = round(float(s), 3)
            if 0.45 <= s <= 1.8 and s not in scales:
                scales.append(s)

        # 优先尝试校准结果和窗口提示比例，避免 fast_mode 只围绕 1.0 导致入口图漏识别。
        for s in [
            preferred_scale,
            *hinted_scales,
            preferred_scale * 0.995,
            preferred_scale * 1.005,
            preferred_scale * 0.99,
            preferred_scale * 1.01,
            preferred_scale * 0.985,
            preferred_scale * 1.015,
            1.0,
            0.99,
            1.01,
            0.985,
            1.015,
            0.97,
            1.03,
            0.95,
            1.05,
        ]:
            add_scale(s)

        # 最后保留一层兜底范围，兼容少量 UI 缩放或采样差异。
        for s in [0.92, 1.08, 0.9, 1.1, 0.88, 1.12, 0.85, 1.15, 0.8, 0.75, 0.7]:
            add_scale(s)

        if fast_mode:
            return scales[:16]
        return scales

    def get_calibrated_gray_threshold(self, threshold):
        calib = getattr(self, "match_calibration", {}) or {}
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
                tpl_c, actual_path = self.get_scaled_template(template_path, scale)
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
                    # 输出基础模板匹配日志。
                    self.log(f"[ImageMatch] 命中: {template_path} | 得分: {max_val:.3f} (阈值 {threshold}) | 缩放比: {scale:.3f}")
                    return pos

            return None

        except Exception as e:
            self.log(f"find_image_in_screen 异常: {e}")
            return None

    # ==========================================
    # --- 碎图候选框识别增强 ---
    # ==========================================
    def _split_enabled(self):
        flag = getattr(self, "use_split_candidates", None)
        if isinstance(flag, bool):
            return flag and _SPLITTER_AVAILABLE
        cfg = getattr(self, "config", None)
        if isinstance(cfg, dict):
            return bool(cfg.get("use_split_candidates", False)) and _SPLITTER_AVAILABLE
        return False

    def _get_split_candidates(self, screen_bgr):
        if not _SPLITTER_AVAILABLE:
            return None
        try:
            boxes = build_candidate_boxes_from_image(screen_bgr, APP_DIR, min_area=100)
            if not boxes:
                return None
            return boxes
        except Exception as e:
            self.log(f"[SplitCandidates] 构建候选框失败: {e}", level="WARN")
            return None

    @staticmethod
    def _filter_candidates_for_template(candidates, tpl_h, tpl_w, pad=10, size_ratio=0.55):
        if not candidates:
            return []
        tpl_area = max(1, tpl_w * tpl_h)
        min_box_w = max(8, int(tpl_w * size_ratio))
        min_box_h = max(8, int(tpl_h * size_ratio))
        out = []
        for c in candidates:
            cw = int(c["w"])
            ch = int(c["h"])
            if cw < min_box_w or ch < min_box_h:
                continue
            box_area = cw * ch
            if box_area < int(tpl_area * size_ratio * size_ratio):
                continue
            if box_area > tpl_area * 64:
                continue
            out.append((
                max(0, int(c["x"]) - pad),
                max(0, int(c["y"]) - pad),
                int(cw) + pad * 2,
                int(ch) + pad * 2,
            ))
        return out

    def _match_gray_in_candidates(self, screen_gray, tpl_gray, candidates, threshold, invert_mode=False):
        tpl_h, tpl_w = tpl_gray.shape[:2]
        boxes = self._filter_candidates_for_template(candidates, tpl_h, tpl_w)
        if not boxes:
            return None
        sh, sw = screen_gray.shape[:2]
        best_score = 0.0
        best_loc = None
        best_box = None
        for (bx, by, bw, bh) in boxes:
            if bx + bw > sw:
                bw = sw - bx
            if by + bh > sh:
                bh = sh - by
            if bw < tpl_w or bh < tpl_h:
                continue
            roi = screen_gray[by:by + bh, bx:bx + bw]
            res = cv2.matchTemplate(roi, tpl_gray, cv2.TM_CCOEFF_NORMED)
            _, mx, _, ml = cv2.minMaxLoc(res)
            if mx > best_score:
                best_score = float(mx)
                best_loc = (ml[0] + bx, ml[1] + by)
                best_box = (bx, by, bw, bh)
            if mx >= threshold:
                return {"score": float(mx), "loc": (ml[0] + bx, ml[1] + by), "tpl_w": tpl_w, "tpl_h": tpl_h, "box": (bx, by, bw, bh), "mode": "原图"}
            if invert_mode:
                tpl_inv = 255 - tpl_gray
                res_inv = cv2.matchTemplate(roi, tpl_inv, cv2.TM_CCOEFF_NORMED)
                _, mx_inv, _, ml_inv = cv2.minMaxLoc(res_inv)
                if mx_inv > best_score:
                    best_score = float(mx_inv)
                    best_loc = (ml_inv[0] + bx, ml_inv[1] + by)
                    best_box = (bx, by, bw, bh)
                if mx_inv >= threshold:
                    return {"score": float(mx_inv), "loc": (ml_inv[0] + bx, ml_inv[1] + by), "tpl_w": tpl_w, "tpl_h": tpl_h, "box": (bx, by, bw, bh), "mode": "反相"}
        if best_loc is None:
            return None
        return {"score": best_score, "loc": best_loc, "tpl_w": tpl_w, "tpl_h": tpl_h, "box": best_box, "mode": "原图"}

    def find_image_with_split_candidates(self, template_path, region=None, threshold=0.75, fast_mode=True, invert_mode=False, use_gray=True):
        """碎图候选框增强版模板匹配：先拆 UI 元素框，再在每个框内做匹配。找不到时返回 None。"""
        try:
            screen_bgr = self.capture_region(region)
            candidates = self._get_split_candidates(screen_bgr)
            if not candidates:
                return None, None
            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)
            effective_threshold = self.get_calibrated_gray_threshold(threshold)
            tpl_gray_raw = self.load_template_gray(template_path) if use_gray else None
            if use_gray and tpl_gray_raw is None:
                return None, candidates
            for scale in scales_to_try:
                if use_gray:
                    tpl = tpl_gray_raw
                    if scale != 1.0:
                        tpl = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                    h, w = tpl.shape[:2]
                    if h < 5 or w < 5 or h > screen_gray.shape[0] or w > screen_gray.shape[1]:
                        continue
                    hit = self._match_gray_in_candidates(screen_gray, tpl, candidates, effective_threshold, invert_mode=invert_mode)
                    if hit and hit["score"] >= effective_threshold:
                        pos = (
                            hit["loc"][0] + hit["tpl_w"] // 2 + (region[0] if region else 0),
                            hit["loc"][1] + hit["tpl_h"] // 2 + (region[1] if region else 0),
                        )
                        self.log(f"[SplitMatch] 命中: {template_path} | 模式: {hit['mode']} | 得分: {hit['score']:.3f} (阈值 {effective_threshold}) | 缩放: {scale:.3f}")
                        return pos, candidates
                else:
                    tpl_c, _ = self.get_scaled_template(template_path, scale)
                    if tpl_c is None:
                        continue
                    h, w = tpl_c.shape[:2]
                    if h < 5 or w < 5 or h > screen_bgr.shape[0] or w > screen_bgr.shape[1]:
                        continue
                    boxes = self._filter_candidates_for_template(candidates, h, w)
                    sh, sw = screen_bgr.shape[:2]
                    for (bx, by, bw, bh) in boxes:
                        if bx + bw > sw:
                            bw = sw - bx
                        if by + bh > sh:
                            bh = sh - by
                        if bw < w or bh < h:
                            continue
                        roi = screen_bgr[by:by + bh, bx:bx + bw]
                        res = cv2.matchTemplate(roi, tpl_c, cv2.TM_CCOEFF_NORMED)
                        _, mx, _, ml = cv2.minMaxLoc(res)
                        if mx >= effective_threshold:
                            pos = (
                                ml[0] + bx + w // 2 + (region[0] if region else 0),
                                ml[1] + by + h // 2 + (region[1] if region else 0),
                            )
                            self.log(f"[SplitMatchC] 命中: {template_path} | 得分: {mx:.3f} (阈值 {effective_threshold}) | 缩放比: {scale:.3f}")
                            return pos, candidates
            return None, candidates
        except Exception as e:
            self.log(f"find_image_with_split_candidates 异常: {e}")
            return None, None

    def find_image(self, template_path, region=None, threshold=0.75, fast_mode=True):
        if not self.is_running:
            return None

        try:
            if self._split_enabled():
                pos, _ = self.find_image_with_split_candidates(
                    template_path,
                    region=region,
                    threshold=threshold,
                    fast_mode=fast_mode,
                    invert_mode=False,
                    use_gray=False,
                )
                if pos is not None:
                    self.last_positions[template_path] = pos
                    return pos
            screen_bgr = self.capture_region(region)
            return self.find_image_in_screen(
                screen_bgr,
                template_path,
                region=region,
                threshold=threshold,
                fast_mode=fast_mode
            )
        except Exception as e:
            self.log(f"查找图片时发生异常: {e}")
            return None

    def find_any_image(self, image_list, region=None, threshold=MATCH_THRESHOLD, fast_mode=True):
        if not self.is_running:
            return None

        try:
            screen_bgr = self.capture_region(region)
            for img_path in image_list:
                pos = self.find_image_in_screen(
                    screen_bgr,
                    img_path,
                    region=region,
                    threshold=threshold,
                    fast_mode=fast_mode
                )
                if pos:
                    return pos
            return None
        except Exception as e:
            self.log(f"find_any_image 异常: {e}")
            return None

    def find_image_with_element(self, main_path, sub_path, region=None, threshold=0.85, fast_mode=True):
        if not self.is_running:
            return None
        try:
            screen_bgr = self.capture_region(region)
            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)
            for scale in scales_to_try:
                # 1. 直接读取缓存后的缩放模板。
                main_tpl_c, _ = self.get_scaled_template(main_path, scale)
                sub_tpl_c, _ = self.get_scaled_template(sub_path, scale)
                if main_tpl_c is None or sub_tpl_c is None:
                    continue
                h_m, w_m = main_tpl_c.shape[:2]
                if h_m < 5 or w_m < 5 or h_m > screen_bgr.shape[0] or w_m > screen_bgr.shape[1]:
                    continue
                # 2. 第一阶段匹配：先在全图中找主目标。
                res_main = cv2.matchTemplate(screen_bgr, main_tpl_c, cv2.TM_CCOEFF_NORMED)
                loc = np.where(res_main >= threshold)
                checked = set()  # 坐标去重，避免相邻结果重复验证。
                for pt in zip(*loc[::-1]):
                    x, y = pt
                    # 过滤 10 像素范围内的重复命中点。
                    key = (x // 10, y // 10)
                    if key in checked:
                        continue
                    checked.add(key)
                    # 3. 在主图周边略微扩展范围，再验证子元素。
                    sub_roi = screen_bgr[
                        max(0, y - 5):min(screen_bgr.shape[0], y + h_m + 5),
                        max(0, x - 5):min(screen_bgr.shape[1], x + w_m + 5),
                    ]
                    if sub_tpl_c.shape[0] > sub_roi.shape[0] or sub_tpl_c.shape[1] > sub_roi.shape[1]:
                        continue
                    # 4. 第二阶段匹配：确认扩展区域内确实包含子元素。
                    res_sub = cv2.matchTemplate(sub_roi, sub_tpl_c, cv2.TM_CCOEFF_NORMED)
                    sub_score = cv2.minMaxLoc(res_sub)[1]
                    if sub_score >= threshold:
                        # 输出组合匹配日志，方便诊断。
                        main_score = res_main[y, x]
                        self.log(f"[ComboMatch] 命中: {main_path}+{sub_path} | 主图得分: {main_score:.3f} | 元素得分: {sub_score:.3f} (阈值 {threshold}) | 缩放比: {scale:.3f}")
                        return (
                            x + w_m // 2 + (region[0] if region else 0),
                            y + h_m // 2 + (region[1] if region else 0),
                        )
            return None
        except Exception as e:
            self.log(f"find_image_with_element 异常: {e}")
            return None
    def find_image_with_element_stable(
        self,
        main_path,
        sub_path,
        region=None,
        main_threshold=0.60,
        verify_threshold=0.72,
        sub_threshold=0.70,
        max_candidates=15
    ):
        if not self.is_running:
            return None

        try:
            screen = pyautogui.screenshot(region=region)
            screen_gray = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2GRAY)

            main_tpl = self.load_template_gray(main_path)
            sub_tpl = self.load_template_gray(sub_path)

            if main_tpl is None or sub_tpl is None:
                return None

            h_m, w_m = main_tpl.shape[:2]
            h_s, w_s = sub_tpl.shape[:2]

            if h_m > screen_gray.shape[0] or w_m > screen_gray.shape[1]:
                return None

            res_main = cv2.matchTemplate(screen_gray, main_tpl, cv2.TM_CCOEFF_NORMED)
            ys, xs = np.where(res_main >= main_threshold)

            if len(xs) == 0:
                return None

            candidates = [(float(res_main[y, x]), x, y) for x, y in zip(xs, ys)]
            candidates.sort(key=lambda t: t[0], reverse=True)

            checked = set()
            checked_count = 0

            for main_score, x, y in candidates:
                key = (x // 8, y // 8)
                if key in checked:
                    continue
                checked.add(key)

                checked_count += 1
                if checked_count > max_candidates:
                    break

                pad = 8
                x1 = max(0, x - pad)
                y1 = max(0, y - pad)
                x2 = min(screen_gray.shape[1], x + w_m + pad)
                y2 = min(screen_gray.shape[0], y + h_m + pad)

                sub_roi = screen_gray[y1:y2, x1:x2]
                if sub_roi.shape[0] < h_s or sub_roi.shape[1] < w_s:
                    continue

                res_sub = cv2.matchTemplate(sub_roi, sub_tpl, cv2.TM_CCOEFF_NORMED)
                sub_score = cv2.minMaxLoc(res_sub)[1]

                if main_score >= verify_threshold and sub_score >= sub_threshold:
                    cx = x + w_m // 2
                    cy = y + h_m // 2
                    if region:
                        cx += region[0]
                        cy += region[1]
                    # 銆愭柊澧炪€戯細鎵撳嵃绋冲畾鐗堢粍鍚堝尮閰嶇殑璇︾粏寰楀垎
                    self.log(f"[StableMatch] 命中: {main_path}+{sub_path} | 主图: {main_score:.3f} (需>{verify_threshold}) | 元素: {sub_score:.3f} (需>{sub_threshold})")
                    return (cx, cy)

            return None

        except Exception as e:
            self.log(f"find_image_with_element_stable 识别报错: {e}")
            return None
    def find_image_with_element_multi(self, main_path, sub_path, region=None, fast_mode=True,
        main_threshold=0.60, like_threshold=0.75, final_threshold=0.72, mask_areas=None):
        if not self.is_running:
            return None

        try:
            screen_bgr = self.capture_region(region, mask_areas=mask_areas)
            screen_gray = self.to_gray_image(screen_bgr)
            screen_edge = self.to_edge_image(screen_bgr)

            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)

            for scale in scales_to_try:
                main_tpl_c, _ = self.get_scaled_template(main_path, scale)
                sub_tpl_c, _ = self.get_scaled_template(sub_path, scale)

                if main_tpl_c is None or sub_tpl_c is None:
                    continue

                main_tpl_gray = self.to_gray_image(main_tpl_c)
                main_tpl_edge = self.to_edge_image(main_tpl_c)

                h_m, w_m = main_tpl_c.shape[:2]
                if h_m < 5 or w_m < 5:
                    continue
                if h_m > screen_bgr.shape[0] or w_m > screen_bgr.shape[1]:
                    continue

                # 先用彩色主模板找候选点，初筛阈值放低一些。
                res_main = cv2.matchTemplate(screen_bgr, main_tpl_c, cv2.TM_CCOEFF_NORMED)
                # 不只依赖硬阈值，改为提取前 N 个高分候选。
                flat = res_main.ravel()
                if flat.size == 0:
                    continue
                top_k = min(80, flat.size)   # 可调，当前先取前 80 个候选。
                idxs = np.argpartition(flat, -top_k)[-top_k:]
                points = []
                for idx in idxs:
                    y, x = np.unravel_index(idx, res_main.shape)
                    score = res_main[y, x]
                    # 给一个较低的底线，避免噪点候选过多。
                    if score < max(0.55, main_threshold - 0.12):
                        continue
                    points.append((x, y, score))
                # 先按 y、x 排序，保证从上到下、从左到右处理。
                points.sort(key=lambda p: (p[1], p[0]))

                checked_points = set()

                for pt in points:
                    x, y, base_score = pt

                    # 去重，避免同一辆车被重复计算。
                    key = (x // 10, y // 10)
                    if key in checked_points:
                        continue
                    checked_points.add(key)

                    roi_bgr = screen_bgr[y:y + h_m, x:x + w_m]
                    roi_gray = screen_gray[y:y + h_m, x:x + w_m]
                    roi_edge = screen_edge[y:y + h_m, x:x + w_m]

                    if roi_bgr.shape[:2] != main_tpl_c.shape[:2]:
                        continue

                    # 四维打分：彩色、灰度、边缘、中心区域。
                    color_score = self.match_template_score(roi_bgr, main_tpl_c)
                    gray_score = self.match_template_score(roi_gray, main_tpl_gray)
                    edge_score = self.match_template_score(roi_edge, main_tpl_edge)

                    roi_center = self.crop_center_ratio(roi_bgr, ratio=0.6)
                    tpl_center = self.crop_center_ratio(main_tpl_c, ratio=0.6)
                    center_score = self.match_template_score(roi_center, tpl_center)

                    # 标签匹配：NEW 标签或作者点赞标签。
                    pad = 5
                    sub_roi = screen_bgr[
                        max(0, y - pad):min(screen_bgr.shape[0], y + h_m + pad),
                        max(0, x - pad):min(screen_bgr.shape[1], x + w_m + pad),
                    ]
                    like_score = self.match_template_score(sub_roi, sub_tpl_c)

                    if like_score < like_threshold:
                        continue

                    # 计算综合得分。
                    final_score = (
                        color_score * 0.35 +
                        gray_score * 0.25 +
                        edge_score * 0.05 +
                        center_score * 0.20 +
                        like_score * 0.15
                    )

                    curr_pos = (
                        x + w_m // 2 + (region[0] if region else 0),
                        y + h_m // 2 + (region[1] if region else 0),
                    )

                    # 只要综合得分达标，就直接返回当前最靠前的目标。
                    if final_score >= final_threshold:
                        self.log(
                            f"[MultiMatch] 锁定目标: {main_path}+{sub_path} | "
                            f"综合: {final_score:.3f} | 彩色: {color_score:.3f} | "
                            f"灰度: {gray_score:.3f} | 边缘: {edge_score:.3f} | "
                            f"中心: {center_score:.3f} | 标签: {like_score:.3f}"
                        )
                        return curr_pos

            return None

        except Exception as e:
            self.log(f"find_image_with_element_multi 异常: {e}")
            return None

    def find_image_with_element_fast(self, main_path, sub_path, region=None, threshold=0.70, sub_threshold=0.70):
        if not self.is_running:
            return None

        try:
            screen = pyautogui.screenshot(region=region)
            screen_gray = cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2GRAY)

            main_tpl = self.load_template_gray(main_path)
            sub_tpl = self.load_template_gray(sub_path)

            if main_tpl is None or sub_tpl is None:
                return None

            h_m, w_m = main_tpl.shape[:2]
            h_s, w_s = sub_tpl.shape[:2]

            if h_m > screen_gray.shape[0] or w_m > screen_gray.shape[1]:
                return None

            res_main = cv2.matchTemplate(screen_gray, main_tpl, cv2.TM_CCOEFF_NORMED)
            loc = np.where(res_main >= threshold)

            checked = set()

            for pt in zip(*loc[::-1]):
                x, y = pt

                # 鍘婚噸锛岄伩鍏嶇浉閭婚噸澶嶇偣澶
                key = (x // 10, y // 10)
                if key in checked:
                    continue
                checked.add(key)

                x1 = max(0, x - 5)
                y1 = max(0, y - 5)
                x2 = min(screen_gray.shape[1], x + w_m + 5)
                y2 = min(screen_gray.shape[0], y + h_m + 5)

                sub_roi = screen_gray[y1:y2, x1:x2]

                if sub_roi.shape[0] < h_s or sub_roi.shape[1] < w_s:
                    continue

                res_sub = cv2.matchTemplate(sub_roi, sub_tpl, cv2.TM_CCOEFF_NORMED)
                _, max_val_sub, _, _ = cv2.minMaxLoc(res_sub)

                if max_val_sub >= sub_threshold:
                    cx = x + w_m // 2
                    cy = y + h_m // 2
                    if region:
                        cx += region[0]
                        cy += region[1]
                    # 输出快速匹配日志。
                    main_score = res_main[y, x]
                    self.log(f"[FastMatch] 命中: {main_path}+{sub_path} | 主图: {main_score:.3f} (需>{threshold}) | 元素: {max_val_sub:.3f} (需>{sub_threshold})")
                    return (cx, cy)

            return None

        except Exception as e:
            self.log(f"find_image_with_element_fast 异常: {e}")
            return None

    def wait_for_image_with_element_multi(self, main_path, sub_path, region=None, fast_mode=True,
        main_threshold=0.60, like_threshold=0.75,
        final_threshold=0.72, timeout=30, interval=0.4):
        start = time.time()

        while self.is_running and time.time() - start < timeout:
            pos = self.find_image_with_element_multi(
                main_path=main_path,
                sub_path=sub_path,
                region=region,
                fast_mode=fast_mode,
                main_threshold=main_threshold,
                like_threshold=like_threshold,
                final_threshold=final_threshold
            )
            if pos:
                return pos

            sleep_end = time.time() + interval
            while self.is_running and time.time() < sleep_end:
                time.sleep(0.05)

        return None

    def load_template_transparent(self, template_path):
        """Load a template while preserving its alpha channel."""
        actual_path = get_img_path(template_path)
        cache_key = ("transparent", actual_path)
        if not hasattr(self, "template_transparent_cache"):
            self.template_transparent_cache = {}
        if cache_key in self.template_transparent_cache:
            return self.template_transparent_cache[cache_key]
            
        # 注意这里使用 cv2.IMREAD_UNCHANGED，它会保留透明通道（BGRA）。
        tpl = cv2.imread(actual_path, cv2.IMREAD_UNCHANGED)
        if tpl is not None:
            self.template_transparent_cache[cache_key] = tpl
        return tpl
    def find_image_transparent(self, template_path, region=None, threshold=0.70, fast_mode=True):
        """Match templates with alpha channel while ignoring transparent background."""
        if not self.is_running:
            return None
        try:
            screen_bgr = self.capture_region(region)
            tpl_bgra = self.load_template_transparent(template_path)
            
            if tpl_bgra is None:
                return None
            # 如果模板没有透明通道（不是 4 通道），则退化为普通模板匹配。
            if tpl_bgra.shape[2] != 4:
                return self.find_image_in_screen(screen_bgr, template_path, region, threshold, fast_mode)
            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)
            for scale in scales_to_try:
                # 对带透明通道的模板做缩放。
                if scale == 1.0:
                    tpl_scaled = tpl_bgra.copy()
                else:
                    tpl_scaled = cv2.resize(tpl_bgra, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                h, w = tpl_scaled.shape[:2]
                if h < 5 or w < 5 or h > screen_bgr.shape[0] or w > screen_bgr.shape[1]:
                    continue
                # 分离 BGR 图层和 Alpha 遮罩层。
                tpl_bgr = tpl_scaled[:, :, :3]
                alpha_mask = tpl_scaled[:, :, 3]
                # 核心：使用带 mask 的模板匹配，透明区域不参与计算。
                res = cv2.matchTemplate(screen_bgr, tpl_bgr, cv2.TM_CCOEFF_NORMED, mask=alpha_mask)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= threshold:
                    # 输出透明模板匹配日志。
                    self.log(f"[AlphaMatch] 命中(忽略背景): {template_path} | 得分: {max_val:.3f} (阈值 {threshold}) | 缩放比: {scale:.3f}")
                    return (
                        max_loc[0] + w // 2 + (region[0] if region else 0),
                        max_loc[1] + h // 2 + (region[1] if region else 0),
                    )
            return None
        except Exception as e:
            self.log(f"find_image_transparent 异常: {e}")
            return None
    def wait_for_image_transparent(self, template_path, region=None, threshold=0.70, timeout=30, interval=0.4, fast_mode=True):
        """Wait for a transparent template to appear."""
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_image_transparent(template_path, region, threshold, fast_mode)
            if pos:
                return pos
            time.sleep(interval)
        return None
    def wait_for_image_with_element_stable(
        self,
        main_path,
        sub_path,
        region=None,
        main_threshold=0.60,
        verify_threshold=0.72,
        sub_threshold=0.70,
        max_candidates=15,
        timeout=3,
        interval=0.2
    ):
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_image_with_element_stable(
                main_path=main_path,
                sub_path=sub_path,
                region=region,
                main_threshold=main_threshold,
                verify_threshold=verify_threshold,
                sub_threshold=sub_threshold,
                max_candidates=max_candidates
            )
            if pos:
                return pos
            time.sleep(interval)
        return None
    def wait_for_image_with_element_fast(
        self,
        main_path,
        sub_path,
        region=None,
        threshold=0.70,
        sub_threshold=0.70,
        timeout=4,
        interval=0.25
    ):
        start = time.time()

        while self.is_running and time.time() - start < timeout:
            pos = self.find_image_with_element_fast(
                main_path=main_path,
                sub_path=sub_path,
                region=region,
                threshold=threshold,
                sub_threshold=sub_threshold
            )
            if pos:
                return pos

            time.sleep(interval)

        return None

    # ==========================================
    # --- 终极安全匹配 V5.1：排他校验 + 右下角精细校验 + 强制从左到右 ---
    # ==========================================
    def find_image_ultimate_safe(self, main_path, anti_path, region=None, main_threshold=0.80, anti_threshold=0.65, mask_areas=None):
        if not self.is_running: return None
        try:
            screen_bgr = self.capture_region(region, mask_areas=mask_areas)
            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)

            scales_to_try = self.get_scales_to_try(fast_mode=True)

            for scale in scales_to_try:
                main_tpl_bgr, _ = self.get_scaled_template(main_path, scale)
                anti_tpl_bgr = None
                if anti_path:
                    anti_tpl_bgr, _ = self.get_scaled_template(anti_path, scale)
                if main_tpl_bgr is None:
                    continue
                if anti_path and anti_tpl_bgr is None:
                    continue
                
                main_tpl_gray = cv2.cvtColor(main_tpl_bgr, cv2.COLOR_BGR2GRAY)
                h_m, w_m = main_tpl_bgr.shape[:2]
                h_a, w_a = anti_tpl_bgr.shape[:2]

                if h_m < 10 or w_m < 10 or h_m > screen_bgr.shape[0] or w_m > screen_bgr.shape[1]:
                    continue

                # 1. 鍩虹褰╄壊鍒濈瓫
                res_main = cv2.matchTemplate(screen_bgr, main_tpl_bgr, cv2.TM_CCOEFF_NORMED)
                loc = np.where(res_main >= main_threshold)

                
                points = list(zip(*loc[::-1]))
                # 强制按 X 坐标排序，优先从左到右扫描。
                points.sort(key=lambda p: (p[1] // 50, p[0]))
                
                checked = set()
                for pt in points:
                    x, y = pt
                    if (x // 10, y // 10) in checked: continue
                    checked.add((x // 10, y // 10))

                    base_score = res_main[y, x]
                    
                    roi_bgr = screen_bgr[y:y+h_m, x:x+w_m]
                    roi_gray = screen_gray[y:y+h_m, x:x+w_m]
                    if roi_bgr.shape[:2] != main_tpl_bgr.shape[:2]: continue

                    # ==================================
                    # 防线 1：排他校验。
                    # ==================================
                    if anti_path and anti_tpl_bgr is not None:
                        h_a, w_a = anti_tpl_bgr.shape[:2]
                        pad_anti = 10
                        roi_y1, roi_y2 = max(0, y - pad_anti), min(screen_bgr.shape[0], y + h_m + pad_anti)
                        roi_x1, roi_x2 = max(0, x - pad_anti), min(screen_bgr.shape[1], x + w_m + pad_anti)
                        anti_roi = screen_bgr[roi_y1:roi_y2, roi_x1:roi_x2]
                        if anti_roi.shape[0] >= h_a and anti_roi.shape[1] >= w_a:
                            res_anti = cv2.matchTemplate(anti_roi, anti_tpl_bgr, cv2.TM_CCOEFF_NORMED)
                            _, anti_score, _, _ = cv2.minMaxLoc(res_anti)
                            if anti_score >= anti_threshold:
                                self.log(f"[排他拦截] 发现排除图得分 {anti_score:.2f}，放弃该目标。")
                                continue

                    # ==================================
                    # 闃茬嚎 2: 椤堕儴鏂囧瓧
                    # ==================================
                    top_h = int(h_m * 0.25)
                    tpl_top = main_tpl_gray[:top_h, :]
                    
                    score_top = 0.0
                    pad_slide = 5 
                    if top_h > pad_slide*2 and w_m > pad_slide*2:
                        tpl_top_core = tpl_top[pad_slide:-pad_slide, pad_slide:-pad_slide]
                        search_top = roi_gray[:int(h_m * 0.35), :]
                        if search_top.shape[0] >= tpl_top_core.shape[0] and search_top.shape[1] >= tpl_top_core.shape[1]:
                            res_top = cv2.matchTemplate(search_top, tpl_top_core, cv2.TM_CCOEFF_NORMED)
                            _, score_top, _, _ = cv2.minMaxLoc(res_top)

                    # ==================================
                    # 防线 3：右下角区域校验。
                    # ==================================
                    bottom_h = int(h_m * 0.25)
                    right_w = int(w_m * 0.35)
                    tpl_pi_box = main_tpl_bgr[h_m - bottom_h:, w_m - right_w:]

                    score_bot = 0.0
                    if bottom_h > pad_slide*2 and right_w > pad_slide*2:
                        tpl_pi_core = tpl_pi_box[pad_slide:-pad_slide, pad_slide:-pad_slide]
                        search_y1 = h_m - int(h_m * 0.35)
                        search_x1 = w_m - int(w_m * 0.45)
                        search_bot = roi_bgr[search_y1:, search_x1:]
                        
                        if search_bot.shape[0] >= tpl_pi_core.shape[0] and search_bot.shape[1] >= tpl_pi_core.shape[1]:
                            res_bot = cv2.matchTemplate(search_bot, tpl_pi_core, cv2.TM_CCOEFF_NORMED)
                            _, score_bot, _, _ = cv2.minMaxLoc(res_bot)

                    if base_score >= 0.76 and score_top >= 0.75 and score_bot >= 0.85:
                        self.log(f"[终极安全-通过] 锁定目标！总分:{base_score:.3f} | 顶部车名:{score_top:.2f} | 右下调校:{score_bot:.2f}")
                        return (x + w_m // 2 + (region[0] if region else 0), y + h_m // 2 + (region[1] if region else 0))
                    else:
                        pass  # 静默拦截，继续寻找下一个候选点。

            return None
        except Exception as e:
            self.log(f"ultimate_safe 异常: {e}")
            return None
    def wait_for_image_ultimate_safe(self, main_path, anti_path, region=None, main_threshold=0.80, anti_threshold=0.65, timeout=3, interval=0.2, mask_areas=None):
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_image_ultimate_safe(main_path, anti_path, region, main_threshold, anti_threshold, mask_areas=mask_areas)
            if pos: return pos
            time.sleep(interval)
        return None

    def find_image_smart(self, template_path, primary_region=None, fallback_region=None, threshold=0.75, fast_mode=True):
        if primary_region:
            pos = self.find_image(template_path, region=primary_region, threshold=threshold, fast_mode=fast_mode)
            if pos:
                return pos

        if fallback_region:
            return self.find_image(template_path, region=fallback_region, threshold=threshold, fast_mode=fast_mode)

        return None
    def to_gray_image(self, img):
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    def to_edge_image(self, img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (3, 3), 0)
        edge = cv2.Canny(blur, 50, 150)
        return edge
    def crop_center_ratio(self, img, ratio=0.6):
        h, w = img.shape[:2]
        ch = int(h * ratio)
        cw = int(w * ratio)
        y1 = max(0, (h - ch) // 2)
        x1 = max(0, (w - cw) // 2)
        return img[y1:y1 + ch, x1:x1 + cw]
    def find_image_gray(self, template_path, region=None, threshold=0.75, fast_mode=True, invert_mode=False):
        """
        灰度模板匹配，支持多分辨率缩放和可选反相匹配。

        参数:
            template_path (str): 模板图片路径。
            region (tuple|list|None): 搜索区域，格式通常为 (x, y, w, h)；None 表示全屏或默认区域。
            threshold (float): 匹配阈值，通常在 0~1 之间，越高越严格。
            fast_mode (bool): 是否使用快速缩放模式；True 表示尝试较少缩放比。
            invert_mode (bool): 是否同时尝试原图和反相模板，适合黑白反转场景。

        返回:
            tuple|None: 命中时返回中心点坐标 `(x, y)`，未命中返回 `None`。
        """
        if not self.is_running:
            return None
        try:
            if self._split_enabled():
                pos, _ = self.find_image_with_split_candidates(
                    template_path,
                    region=region,
                    threshold=threshold,
                    fast_mode=fast_mode,
                    invert_mode=invert_mode,
                    use_gray=True,
                )
                if pos is not None:
                    self.last_positions[template_path] = pos
                    return pos
            screen_bgr = self.capture_region(region)
            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)
            effective_threshold = self.get_calibrated_gray_threshold(threshold)
            region_name = next((name for name, rect in self.regions.items() if rect == region), "custom") if region else "全界面"
            started = time.time()
            best_score = 0.0
            best_scale = 1.0
            best_mode = "原图"
            best_position = None

            # 模板只读取一次，避免每个缩放比都重复加载。
            tpl_gray_raw = self.load_template_gray(template_path)
            if tpl_gray_raw is None:
                return None

            for scale in scales_to_try:
                # 始终从原始模板开始缩放，避免多次 resize 造成污染。
                tpl_gray = tpl_gray_raw
                if scale != 1.0:
                    tpl_gray = cv2.resize(tpl_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

                h, w = tpl_gray.shape[:2]
                if h < 5 or w < 5 or h > screen_gray.shape[0] or w > screen_gray.shape[1]:
                    continue

                # ==============================
                # 原图灰度匹配
                # ==============================
                res = cv2.matchTemplate(screen_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val > best_score:
                    best_score = float(max_val)
                    best_scale = float(scale)
                    best_mode = "原图"
                    best_position = (
                        max_loc[0] + w // 2 + (region[0] if region else 0),
                        max_loc[1] + h // 2 + (region[1] if region else 0),
                    )
                if max_val >= effective_threshold:
                    if hasattr(self, "record_diagnostic_match"):
                        self.record_diagnostic_match(
                            "find_image_gray",
                            template_path,
                            region_name=region_name,
                            threshold=threshold,
                            effective_threshold=effective_threshold,
                            fast_mode=fast_mode,
                            invert_mode=invert_mode,
                            hit=True,
                            score=max_val,
                            scale=scale,
                            mode="原图",
                            template=template_path,
                            position=best_position,
                            elapsed_ms=int((time.time() - started) * 1000),
                        )
                    self.log(f"[GrayMatch] 命中: {template_path} | 模式: 原图 | 灰度得分: {max_val:.3f} (阈值 {effective_threshold}) | 缩放比: {scale:.3f}")
                    return (
                        max_loc[0] + w // 2 + (region[0] if region else 0),
                        max_loc[1] + h // 2 + (region[1] if region else 0),
                    )

                # ==============================
                # 反相模板匹配，用于黑白反转场景。
                # ==============================
                if invert_mode:
                    tpl_inv = 255 - tpl_gray
                    res_inv = cv2.matchTemplate(screen_gray, tpl_inv, cv2.TM_CCOEFF_NORMED)
                    _, max_val_inv, _, max_loc_inv = cv2.minMaxLoc(res_inv)
                    if max_val_inv > best_score:
                        best_score = float(max_val_inv)
                        best_scale = float(scale)
                        best_mode = "反相"
                        best_position = (
                            max_loc_inv[0] + w // 2 + (region[0] if region else 0),
                            max_loc_inv[1] + h // 2 + (region[1] if region else 0),
                        )
                    if max_val_inv >= effective_threshold:
                        if hasattr(self, "record_diagnostic_match"):
                            self.record_diagnostic_match(
                                "find_image_gray",
                                template_path,
                                region_name=region_name,
                                threshold=threshold,
                                effective_threshold=effective_threshold,
                                fast_mode=fast_mode,
                                invert_mode=invert_mode,
                                hit=True,
                                score=max_val_inv,
                                scale=scale,
                                mode="反相",
                                template=template_path,
                                position=best_position,
                                elapsed_ms=int((time.time() - started) * 1000),
                            )
                        self.log(f"[GrayMatch] 命中: {template_path} | 模式: 反相 | 灰度得分: {max_val_inv:.3f} (阈值 {effective_threshold}) | 缩放比: {scale:.3f}")
                        return (
                            max_loc_inv[0] + w // 2 + (region[0] if region else 0),
                            max_loc_inv[1] + h // 2 + (region[1] if region else 0),
                        )

            if hasattr(self, "record_diagnostic_match"):
                self.record_diagnostic_match(
                    "find_image_gray",
                    template_path,
                    region_name=region_name,
                    threshold=threshold,
                    effective_threshold=effective_threshold,
                    fast_mode=fast_mode,
                    invert_mode=invert_mode,
                    hit=False,
                    score=best_score,
                    scale=best_scale,
                    mode=best_mode,
                    template=template_path,
                    position=best_position,
                    elapsed_ms=int((time.time() - started) * 1000),
                )
            self.capture_diagnostic_template_miss(
                template_path,
                region=region,
                screen_bgr=screen_bgr,
                best_score=best_score,
                threshold=effective_threshold,
                extra={"mode": best_mode, "scale": best_scale},
            )
            return None
        except Exception as e:
            self.log(f"find_image_gray 异常: {e}")
            return None
    def find_any_image_gray(self, image_list, region=None, threshold=0.75, fast_mode=True, invert_mode=False):
        """
        灰度多模板匹配，支持多分辨率缩放和可选反相匹配。

        参数:
            image_list (list): 模板路径列表，例如 `["a.png", "b.png", "c.png"]`。
            region (tuple|list|None): 搜索区域，None 表示全屏或默认区域。
            threshold (float): 匹配阈值。
            fast_mode (bool): 是否使用快速缩放模式。
            invert_mode (bool): 是否同时尝试原图和反相模板。

        返回:
            tuple|None: 命中任意一张模板时返回中心点坐标 `(x, y)`，否则返回 `None`。
        """
        if not self.is_running:
            return None
        try:
            screen_bgr = self.capture_region(region)
            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)
            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)
            effective_threshold = self.get_calibrated_gray_threshold(threshold)

            if self._split_enabled():
                candidates = self._get_split_candidates(screen_bgr)
                if candidates:
                    for img_path in image_list:
                        tpl_gray_raw = self.load_template_gray(img_path)
                        if tpl_gray_raw is None:
                            continue
                        for scale in scales_to_try:
                            tpl = tpl_gray_raw
                            if scale != 1.0:
                                tpl = cv2.resize(tpl, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                            h, w = tpl.shape[:2]
                            if h < 5 or w < 5 or h > screen_gray.shape[0] or w > screen_gray.shape[1]:
                                continue
                            hit = self._match_gray_in_candidates(
                                screen_gray, tpl, candidates, effective_threshold, invert_mode=invert_mode
                            )
                            if hit and hit["score"] >= effective_threshold:
                                pos = (
                                    hit["loc"][0] + hit["tpl_w"] // 2 + (region[0] if region else 0),
                                    hit["loc"][1] + hit["tpl_h"] // 2 + (region[1] if region else 0),
                                )
                                self.last_positions[img_path] = pos
                                self.log(f"[SplitMatchAny] 命中: {img_path} | 模式: {hit['mode']} | 得分: {hit['score']:.3f} (阈值 {effective_threshold}) | 缩放: {scale:.3f}")
                                return pos

            region_name = next((name for name, rect in self.regions.items() if rect == region), "custom") if region else "全界面"
            started = time.time()
            best_template = None
            best_score = 0.0
            best_scale = 1.0
            best_mode = "原图"
            best_position = None

            for img_path in image_list:
                # 模板只读取一次，避免重复加载。
                tpl_gray_raw = self.load_template_gray(img_path)
                if tpl_gray_raw is None:
                    continue

                for scale in scales_to_try:
                    # 始终从原始模板开始缩放。
                    tpl_gray = tpl_gray_raw
                    if scale != 1.0:
                        tpl_gray = cv2.resize(tpl_gray, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

                    h, w = tpl_gray.shape[:2]
                    if h < 5 or w < 5 or h > screen_gray.shape[0] or w > screen_gray.shape[1]:
                        continue

                    # ==============================
                    # 原图灰度匹配
                    # ==============================
                    res = cv2.matchTemplate(screen_gray, tpl_gray, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)
                    if max_val > best_score:
                        best_template = img_path
                        best_score = float(max_val)
                        best_scale = float(scale)
                        best_mode = "原图"
                        best_position = (
                            max_loc[0] + w // 2 + (region[0] if region else 0),
                            max_loc[1] + h // 2 + (region[1] if region else 0),
                        )
                    if max_val >= effective_threshold:
                        if hasattr(self, "record_diagnostic_match"):
                            self.record_diagnostic_match(
                                "find_any_image_gray",
                                img_path,
                                region_name=region_name,
                                threshold=threshold,
                                effective_threshold=effective_threshold,
                                fast_mode=fast_mode,
                                invert_mode=invert_mode,
                                hit=True,
                                score=max_val,
                                scale=scale,
                                mode="原图",
                                template=img_path,
                                position=best_position,
                                elapsed_ms=int((time.time() - started) * 1000),
                            )
                        self.log(f"[GrayMatchAny] 命中: {img_path} | 模式: 原图 | 灰度得分: {max_val:.3f} (阈值 {effective_threshold}) | 缩放比: {scale:.3f}")
                        return (
                            max_loc[0] + w // 2 + (region[0] if region else 0),
                            max_loc[1] + h // 2 + (region[1] if region else 0),
                        )

                    # ==============================
                    # 反相模板匹配，用于黑白反转场景。
                    # ==============================
                    if invert_mode:
                        tpl_inv = 255 - tpl_gray
                        res_inv = cv2.matchTemplate(screen_gray, tpl_inv, cv2.TM_CCOEFF_NORMED)
                        _, max_val_inv, _, max_loc_inv = cv2.minMaxLoc(res_inv)
                        if max_val_inv > best_score:
                            best_template = img_path
                            best_score = float(max_val_inv)
                            best_scale = float(scale)
                            best_mode = "反相"
                            best_position = (
                                max_loc_inv[0] + w // 2 + (region[0] if region else 0),
                                max_loc_inv[1] + h // 2 + (region[1] if region else 0),
                            )
                        if max_val_inv >= effective_threshold:
                            if hasattr(self, "record_diagnostic_match"):
                                self.record_diagnostic_match(
                                    "find_any_image_gray",
                                    img_path,
                                    region_name=region_name,
                                    threshold=threshold,
                                    effective_threshold=effective_threshold,
                                    fast_mode=fast_mode,
                                    invert_mode=invert_mode,
                                    hit=True,
                                    score=max_val_inv,
                                    scale=scale,
                                    mode="反相",
                                    template=img_path,
                                    position=best_position,
                                    elapsed_ms=int((time.time() - started) * 1000),
                                )
                            self.log(f"[GrayMatchAny] 命中: {img_path} | 模式: 反相 | 灰度得分: {max_val_inv:.3f} (阈值 {effective_threshold}) | 缩放比: {scale:.3f}")
                            return (
                                max_loc_inv[0] + w // 2 + (region[0] if region else 0),
                                max_loc_inv[1] + h // 2 + (region[1] if region else 0),
                            )

            if hasattr(self, "record_diagnostic_match"):
                self.record_diagnostic_match(
                    "find_any_image_gray",
                    best_template or "|".join(image_list),
                    region_name=region_name,
                    threshold=threshold,
                    effective_threshold=effective_threshold,
                    fast_mode=fast_mode,
                    invert_mode=invert_mode,
                    hit=False,
                    score=best_score,
                    scale=best_scale,
                    mode=best_mode,
                    template=best_template,
                    position=best_position,
                    elapsed_ms=int((time.time() - started) * 1000),
                    extra={"candidates": image_list},
                )
            if best_template:
                self.capture_diagnostic_template_miss(
                    best_template,
                    region=region,
                    screen_bgr=screen_bgr,
                    best_score=best_score,
                    threshold=effective_threshold,
                    extra={"mode": best_mode, "scale": best_scale, "candidates": image_list},
                )
            return None
        except Exception as e:
            self.log(f"find_any_image_gray 异常: {e}")
            return None

    def wait_for_any_image_gray(self, image_list, region=None, threshold=0.75, timeout=30, interval=0.3, fast_mode=True, invert_mode=False):
        """
        等待多张灰度模板中的任意一张出现。

        参数:
            image_list (list): 模板路径列表。
            region (tuple|list|None): 搜索区域。
            threshold (float): 匹配阈值。
            timeout (int|float): 最大等待时间，单位秒。
            interval (int|float): 每次检测失败后的等待间隔，单位秒。
            fast_mode (bool): 是否使用快速缩放模式。
            invert_mode (bool): 是否启用反相模板匹配。

        返回:
            tuple|None: 超时前命中则返回中心点坐标 `(x, y)`，否则返回 `None`。
        """
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_any_image_gray(
                image_list,
                region=region,
                threshold=threshold,
                fast_mode=fast_mode,
                invert_mode=invert_mode
            )
            if pos:
                return pos

            # 分段短睡眠，保持可中断。
            sleep_end = time.time() + interval
            while self.is_running and time.time() < sleep_end:
                time.sleep(0.05)
        return None

    def wait_for_image_gray(self, template_path, region=None, threshold=0.75, timeout=30, interval=0.3, fast_mode=True, invert_mode=False):
        """
        等待单张灰度模板出现。

        参数:
            template_path (str): 模板图片路径。
            region (tuple|list|None): 搜索区域。
            threshold (float): 匹配阈值。
            timeout (int|float): 最大等待时间，单位秒。
            interval (int|float): 每次检测失败后的等待间隔，单位秒。
            fast_mode (bool): 是否使用快速缩放模式。
            invert_mode (bool): 是否启用反相模板匹配。

        返回:
            tuple|None: 超时前命中则返回中心点坐标 `(x, y)`，否则返回 `None`。
        """
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_image_gray(
                template_path,
                region=region,
                threshold=threshold,
                fast_mode=fast_mode,
                invert_mode=invert_mode
            )
            if pos:
                return pos

            # 分段短睡眠，保持可中断。
            sleep_end = time.time() + interval
            while self.is_running and time.time() < sleep_end:
                time.sleep(0.05)
        return None

    def find_any_image_transparent(self, image_list, region=None, threshold=0.70, fast_mode=True):
        """Find any transparent template from a list."""
        if not self.is_running:
            return None
        try:
            screen_bgr = self.capture_region(region)
            scales_to_try = self.get_scales_to_try(fast_mode=fast_mode)

            for template_path in image_list:
                tpl_bgra = self.load_template_transparent(template_path)
                if tpl_bgra is None:
                    continue
                
                # 如果模板没有透明通道，则退化为普通模板匹配。
                if tpl_bgra.shape[2] != 4:
                    pos = self.find_image_in_screen(screen_bgr, template_path, region, threshold, fast_mode)
                    if pos: return pos
                    continue

                for scale in scales_to_try:
                    if scale == 1.0:
                        tpl_scaled = tpl_bgra.copy()
                    else:
                        tpl_scaled = cv2.resize(tpl_bgra, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)

                    h, w = tpl_scaled.shape[:2]
                    if h < 5 or w < 5 or h > screen_bgr.shape[0] or w > screen_bgr.shape[1]:
                        continue

                    tpl_bgr = tpl_scaled[:, :, :3]
                    alpha_mask = tpl_scaled[:, :, 3]

                    res = cv2.matchTemplate(screen_bgr, tpl_bgr, cv2.TM_CCOEFF_NORMED, mask=alpha_mask)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)

                    if max_val >= threshold:
                        # 输出多模板透明匹配日志。
                        self.log(f"[AlphaMatchAny] 命中(忽略背景): {template_path} | 得分: {max_val:.3f} (阈值 {threshold}) | 缩放比: {scale:.3f}")
                        return (
                            max_loc[0] + w // 2 + (region[0] if region else 0),
                            max_loc[1] + h // 2 + (region[1] if region else 0),
                        )
            return None
        except Exception as e:
            self.log(f"find_any_image_transparent 异常: {e}")
            return None

    def wait_for_any_image_transparent(self, image_list, region=None, threshold=0.70, timeout=30, interval=0.4, fast_mode=True):
        """Wait for any transparent template from a list to appear."""
        start = time.time()
        while self.is_running and time.time() - start < timeout:
            pos = self.find_any_image_transparent(image_list, region, threshold, fast_mode)
            if pos:
                return pos
            
            sleep_end = time.time() + interval
            while self.is_running and time.time() < sleep_end:
                time.sleep(0.05)
        return None
    def wait_for_any_image(self, image_list, region=None, threshold=0.75, timeout=30, interval=0.4, fast_mode=True, log_text=None):
        start = time.time()

        while self.is_running and time.time() - start < timeout:
            try:
                screen_bgr = self.capture_region(region)
                for img_path in image_list:
                    pos = self.find_image_in_screen(
                        screen_bgr,
                        img_path,
                        region=region,
                        threshold=threshold,
                        fast_mode=fast_mode
                    )
                    if pos:
                        return pos
            except Exception as e:
                self.log(f"wait_for_any_image 异常: {e}")

            if log_text:
                self.log(log_text)

            sleep_end = time.time() + interval
            while self.is_running and time.time() < sleep_end:
                time.sleep(0.05)

        return None

    def wait_for_image(self, template_path, region=None, threshold=0.75, timeout=30, interval=0.4, fast_mode=True, log_text=None):
        return self.wait_for_any_image(
            [template_path],
            region=region,
            threshold=threshold,
            timeout=timeout,
            interval=interval,
            fast_mode=fast_mode,
            log_text=log_text
        )

    def wait_for_image_with_element(self, main_path, sub_path, region=None, threshold=0.85, timeout=30, interval=0.4, fast_mode=True):
        start = time.time()

        while self.is_running and time.time() - start < timeout:
            pos = self.find_image_with_element(
                main_path,
                sub_path,
                region=region,
                threshold=threshold,
                fast_mode=fast_mode
            )
            if pos:
                return pos

            sleep_end = time.time() + interval
            while self.is_running and time.time() < sleep_end:
                time.sleep(0.05)

        return None

    def match_template_score(self, src, tpl):
        try:
            if tpl is None or src is None:
                return 0.0
            th, tw = tpl.shape[:2]
            sh, sw = src.shape[:2]
            if th < 5 or tw < 5 or th > sh or tw > sw:
                return 0.0
            res = cv2.matchTemplate(src, tpl, cv2.TM_CCOEFF_NORMED)
            return cv2.minMaxLoc(res)[1]
        except Exception:
            return 0.0
    #===============================
    #---娴嬭瘯鍑芥暟-----
    #===============================
    def start_test_find_image(self):
        """F3 test helper: repeatedly search targets without clicking them."""
        if self.is_running:
            self.log("已有任务正在运行，无法执行 F3 测图。")
            return

        self.is_running = True
        self.is_paused = False
        self.save_config()

        self.reset_run_stats()
        self.update_running_state("running")
        self.update_running_ui("F3测图", 0, 12)
        self.ui_call(self.lbl_runtime_loop.configure, text="测试模式")
        self.update_timer()

        self.log("====== 开始 F3 测试原二阶段找图 ======")

        def test_runner():
            try:
                if not self.check_and_focus_game():
                    self.log("未能聚焦游戏窗口，测试结束。")
                    return

                found_positions = []
                mask_areas = []

                for i in range(15):
                    if not self.is_running:
                        return
                    self.check_pause()

                    pos = self.find_image_with_element_multi(
                        "newCC.png",
                        "newcartag.png",
                        region=self.regions["全界面"],
                        main_threshold=0.70,
                        like_threshold=0.70,
                        final_threshold=0.70,
                        fast_mode=True,
                        mask_areas=mask_areas
                    )

                    if not pos:
                        self.log(f"第 {i + 1} 次查找：未找到新的目标，测试结束。")
                        break

                    x, y = int(pos[0]), int(pos[1])

                    duplicated = False
                    for old_x, old_y in found_positions:
                        if abs(x - old_x) <= 80 and abs(y - old_y) <= 80:
                            duplicated = True
                            break

                    region_x, region_y, _, _ = self.regions["全界面"]
                    local_x = x - region_x
                    local_y = y - region_y

                    block_w = 210
                    block_h = 120
                    mask_areas.append((
                        local_x - block_w // 2,
                        local_y - block_h // 2,
                        local_x + block_w // 2,
                        local_y + block_h // 2
                    ))

                    if duplicated:
                        self.log(f"F3 测试：识别到重复目标 ({x}, {y})，已扩大遮罩，继续查找。")
                        continue

                    found_positions.append((x, y))
                    self.update_running_ui("F3测试找图", len(found_positions), 12)
                    self.log(f"F3测试：找到第 {len(found_positions)} 个目标 -> ({x}, {y})")
                    self.hw_mouse_move(x, y)
                    time.sleep(0.5)

                self.log(f"F3 测试完成，共找到 {len(found_positions)} 个目标。")

            except Exception as e:
                self.log(f"F3测试异常: {e}")
            finally:
                self.stop_all()

        self.current_thread = threading.Thread(target=test_runner, daemon=True)
        self.current_thread.start()
    # ==========================================
    # --- 模块：跑图前置与循环跑图 ---

ImageMatcherMixin = TemplateMatcherMixin

