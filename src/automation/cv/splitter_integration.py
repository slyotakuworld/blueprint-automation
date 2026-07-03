import os
import sys
import datetime
import shutil

import cv2
import numpy as np

from PIL import ImageGrab

try:
    from .split_elements import UIElementSplitter
    _SPLITTER_AVAILABLE = True
except Exception:
    _SPLITTER_AVAILABLE = False
    UIElementSplitter = None


def _write_png_cn(file_path, image_bgr):
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        ok, buf = cv2.imencode(".png", image_bgr)
        if ok:
            buf.tofile(file_path)
            return True
    except Exception:
        pass
    return False


def capture_region_bgr(region=None):
    try:
        if region:
            x, y, w, h = region
            bbox = (int(x), int(y), int(x + w), int(y + h))
            screen = ImageGrab.grab(bbox=bbox, all_screens=True)
        else:
            screen = ImageGrab.grab(all_screens=True)
        return cv2.cvtColor(np.array(screen), cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def _tmp_image_from_bgr(image_bgr, base_dir):
    tmp_dir = os.path.join(base_dir, "__tmp_split__")
    os.makedirs(tmp_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    tmp_path = os.path.join(tmp_dir, f"src_{ts}.png")
    if _write_png_cn(tmp_path, image_bgr):
        return tmp_path
    return None


def run_splitter_on_image_bgr(image_bgr, app_dir=None, output_dir=None, padding=5, shot_dir=None):
    if not _SPLITTER_AVAILABLE or UIElementSplitter is None:
        return {
            "ok": False,
            "error": "无法导入 UIElementSplitter（split_elements.py 未找到或依赖缺失）",
            "elements": [],
            "output_dir": None,
            "vis_path": None,
        }

    if shot_dir:
        shot_dir = os.path.abspath(shot_dir)
    elif app_dir:
        shot_dir = os.path.join(os.path.abspath(app_dir), "screenshots")
    else:
        shot_dir = os.path.join(os.getcwd(), "screenshots")
    os.makedirs(shot_dir, exist_ok=True)
    if output_dir is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        output_dir = os.path.join(shot_dir, f"split_elements_{timestamp}")
    os.makedirs(output_dir, exist_ok=True)

    tmp_src = _tmp_image_from_bgr(image_bgr, shot_dir)
    if tmp_src is None:
        return {
            "ok": False,
            "error": "无法写入临时截图文件",
            "elements": [],
            "output_dir": output_dir,
            "vis_path": None,
        }

    try:
        splitter = UIElementSplitter(tmp_src, output_dir=output_dir)
    except Exception as e:
        return {
            "ok": False,
            "error": f"UIElementSplitter 初始化失败: {e}",
            "elements": [],
            "output_dir": output_dir,
            "vis_path": None,
        }

    splitter.height, splitter.width = image_bgr.shape[:2]
    splitter.image = image_bgr
    splitter.output_dir = output_dir
    os.makedirs(output_dir, exist_ok=True)

    splitter.detect_all_elements()
    saved_count = splitter.save_elements(padding=padding)
    vis_img = splitter.visualize_detections()

    elements = []
    for idx, (x, y, w, h, area) in enumerate(splitter.elements):
        x_start = max(0, x - padding)
        y_start = max(0, y - padding)
        x_end = min(splitter.width, x + w + padding)
        y_end = min(splitter.height, y + h + padding)
        crop = image_bgr[y_start:y_end, x_start:x_end]
        if crop.size == 0:
            continue
        elements.append({
            "idx": idx,
            "x": x,
            "y": y,
            "w": w,
            "h": h,
            "area": area,
            "crop": crop,
            "filename": f"element_{idx:04d}_{x}_{y}_{w}x{h}.png",
        })

    try:
        os.remove(tmp_src)
    except Exception:
        pass

    vis_path = os.path.join(output_dir, "detections_visualization.png")

    return {
        "ok": True,
        "error": None,
        "elements": elements,
        "output_dir": output_dir,
        "vis_path": vis_path if os.path.isfile(vis_path) else None,
        "saved_count": saved_count,
    }


def run_splitter_on_region(region, app_dir=None, output_dir=None, padding=5, shot_dir=None):
    image_bgr = capture_region_bgr(region)
    if image_bgr is None:
        return {
            "ok": False,
            "error": "无法抓取区域截图",
            "elements": [],
            "output_dir": None,
            "vis_path": None,
        }
    return run_splitter_on_image_bgr(image_bgr, app_dir, output_dir=output_dir, padding=padding, shot_dir=shot_dir)


def import_element_as_template(element_path, target_template_name, images_dir):
    if not os.path.isfile(element_path):
        return None, f"碎图文件不存在: {element_path}"
    if not target_template_name:
        return None, "目标模板名不能为空"

    base = target_template_name.strip()
    if not base.lower().endswith(".png"):
        base += ".png"
    safe_base = os.path.basename(base)

    candidates = [
        os.path.join(images_dir, safe_base),
    ]
    if os.path.isdir(os.path.join(images_dir, "1080p")):
        candidates.insert(0, os.path.join(images_dir, "1080p", safe_base))

    dst_path = candidates[0]
    try:
        os.makedirs(os.path.dirname(dst_path), exist_ok=True)
        shutil.copy2(element_path, dst_path)
    except Exception as e:
        return None, f"复制模板失败: {e}"

    return dst_path, None


def build_candidate_boxes_from_image(image_bgr, app_dir, min_area=100):
    if not _SPLITTER_AVAILABLE or UIElementSplitter is None:
        return None

    h, w = image_bgr.shape[:2]
    shot_dir = os.path.join(app_dir, "screenshots")
    tmp_src = _tmp_image_from_bgr(image_bgr, shot_dir)
    if tmp_src is None:
        return None

    try:
        splitter = UIElementSplitter(tmp_src)
    except Exception:
        try:
            os.remove(tmp_src)
        except Exception:
            pass
        return None

    splitter.image = image_bgr
    splitter.width = w
    splitter.height = h

    boxes = []
    try:
        contour_boxes = splitter._detect_by_contours(min_area=min_area, max_area=w * h * 0.1)
        boxes.extend(contour_boxes)
    except Exception:
        pass
    try:
        comp_boxes = splitter._detect_by_connected_components(min_area=min_area)
        boxes.extend(comp_boxes)
    except Exception:
        pass
    try:
        btn_boxes = splitter._detect_ui_buttons()
        boxes.extend(btn_boxes)
    except Exception:
        pass
    try:
        icon_boxes = splitter._detect_icons(min_size=15, max_size=160)
        boxes.extend(icon_boxes)
    except Exception:
        pass
    try:
        grad_boxes = splitter._detect_ui_elements_by_gradient()
        boxes.extend(grad_boxes)
    except Exception:
        pass

    try:
        os.remove(tmp_src)
    except Exception:
        pass

    try:
        filtered = splitter._filter_large_boxes(boxes)
        merged = splitter._merge_overlapping_boxes(filtered, overlap_threshold=0.4)
    except Exception:
        merged = boxes

    result = []
    seen = set()
    for item in merged:
        if len(item) == 5:
            x, y, bw, bh, area = item
        else:
            continue
        key = (int(x), int(y), int(bw), int(bh))
        if key in seen:
            continue
        seen.add(key)
        result.append({
            "x": int(x),
            "y": int(y),
            "w": int(bw),
            "h": int(bh),
            "area": int(area),
        })
    return result


def list_elements_in_dir(split_dir):
    if not split_dir or not os.path.isdir(split_dir):
        return []
    items = []
    for name in sorted(os.listdir(split_dir)):
        if not name.startswith("element_") or not name.endswith(".png"):
            continue
        full = os.path.join(split_dir, name)
        try:
            stem = name[:-4]
            parts = stem.split("_")
            if len(parts) >= 5:
                try:
                    idx = int(parts[1])
                    ex = int(parts[2])
                    ey = int(parts[3])
                    sz = parts[4]
                    if "x" in sz:
                        sw, sh = sz.split("x", 1)
                        items.append({
                            "idx": idx,
                            "path": full,
                            "name": name,
                            "x": int(ex),
                            "y": int(ey),
                            "w": int(sw),
                            "h": int(sh),
                        })
                except Exception:
                    pass
        except Exception:
            continue
    items.sort(key=lambda r: r["idx"])
    return items
