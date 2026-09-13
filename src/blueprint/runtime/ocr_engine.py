"""内置 OCR 引擎（懒加载、常驻复用）。

设计要点（对齐 ccgyame/ocr_service 的 engine.py 思路，但作为库内嵌）：
  - 引擎按 (lang) 缓存，只初始化一次，之后反复复用，避免每次识别都重新加载模型导致卡顿。
  - 模型/依赖按需加载：本模块 import 时不碰 paddle，首次调用 recognize 才真正 import PaddleOCR。
  - PaddlePaddle 3.x + PaddleOCR 3.x 的 PIR/oneDNN 兼容性坑，靠 import 前设置环境变量规避。
  - 模型缓存目录定位到可插拔的 ocr_runtime/ 目录（见 get_runtime_dir），便于「动态打包」——
    导出时若蓝图不含 OCR 节点，就完全不带这个目录。

本模块不依赖 Qt，主程序与运行时 exe 共用。
"""
from __future__ import annotations

import os
import threading
from typing import Any, Dict, Optional

import numpy as np

# 必须在 import paddleocr 之前设置，且初始化时传 enable_mkldnn=False
os.environ.setdefault("FLAGS_enable_pir_api", "0")
os.environ.setdefault("FLAGS_use_mkldnn", "0")


def get_runtime_dir() -> str:
    """返回 OCR 运行时目录（模型缓存放这里）。可被动态打包/测试覆盖。"""
    from src.config.app_paths import APP_DIR

    return os.path.join(APP_DIR, "ocr_runtime")


class OcrUnavailable(Exception):
    """OCR 引擎不可用（依赖未安装 / 模型未就绪）。"""


class OcrEngine:
    """PaddleOCR 识别引擎（懒加载、常驻内存复用）。"""

    def __init__(self, lang: str = "ch"):
        self.lang = lang
        self._engine = None
        self._lock = threading.Lock()

    def _ensure(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            # 模型缓存定位到 ocr_runtime/models，使模型随目录走，便于整体拷贝/动态打包
            runtime_dir = get_runtime_dir()
            os.environ.setdefault("PADDLE_PDX_CACHE_HOME", os.path.join(runtime_dir, "models"))
            try:
                from paddleocr import PaddleOCR
            except ImportError as e:
                raise OcrUnavailable(
                    "PaddleOCR 未安装。请运行: pip install paddlepaddle paddleocr"
                ) from e
            try:
                self._engine = PaddleOCR(
                    use_textline_orientation=True,
                    lang=self.lang,
                    enable_mkldnn=False,
                )
            except Exception as e:
                raise OcrUnavailable(f"PaddleOCR 初始化失败: {e}") from e
            return self._engine

    def recognize(self, img_bgr: np.ndarray) -> Dict[str, Any]:
        """识别 BGR 图像，返回 {items, text, sha}。items 为 [{text,conf,box,index}]。"""
        engine = self._ensure()
        img_rgb = cv2_cvt_color(img_bgr)
        try:
            result = engine.ocr(img_rgb)
        except Exception as e:
            return {"items": [], "text": "", "sha": "", "error": f"{type(e).__name__}: {e}"}

        items, text = _extract(result)
        return {
            "items": items,
            "text": text,
            "sha": _hash(text),
        }


def cv2_cvt_color(img_bgr: np.ndarray) -> np.ndarray:
    """BGR -> RGB（不 import cv2 到顶层，避免加载顺序问题）。"""
    import cv2

    if img_bgr is None or img_bgr.size == 0:
        raise ValueError("空图像")
    if img_bgr.ndim == 2:
        return cv2.cvtColor(img_bgr, cv2.COLOR_GRAY2RGB)
    if img_bgr.shape[2] == 4:
        return cv2.cvtColor(img_bgr, cv2.COLOR_BGRA2RGB)
    return cv2.cvtColor(img_bgr, cv2.COLOR_BGR2RGB)


# 引擎实例池：按 lang 缓存
_POOL: Dict[str, OcrEngine] = {}
_POOL_LOCK = threading.Lock()


def get_engine(lang: str = "ch") -> OcrEngine:
    lang = (lang or "ch").lower()
    with _POOL_LOCK:
        eng = _POOL.get(lang)
        if eng is None:
            eng = OcrEngine(lang=lang)
            _POOL[lang] = eng
        return eng


def recognize(img_bgr: np.ndarray, lang: str = "ch") -> Dict[str, Any]:
    return get_engine(lang).recognize(img_bgr)


def _extract(result) -> tuple:
    """把 PaddleOCR 3.x / 2.x 返回归一化为 (items, text)。"""
    items: list = []
    lines: list = []
    if not result or not result[0]:
        return items, ""

    first = result[0]
    if isinstance(first, dict) and "rec_texts" in first:
        texts = first.get("rec_texts") or []
        scores = first.get("rec_scores") or [None] * len(texts)
        polys = first.get("rec_polys") or []
        for idx, t in enumerate(texts):
            t = (t or "").strip()
            if not t:
                continue
            item = {
                "text": t,
                "conf": float(scores[idx]) if idx < len(scores) and scores[idx] is not None else -1.0,
                "box": _poly_to_box(polys[idx]) if idx < len(polys) else None,
                "index": idx,
            }
            items.append(item)
            lines.append(t)
    else:
        for idx, line in enumerate(first):
            try:
                text = (line[1][0] or "").strip()
            except Exception:
                continue
            if not text:
                continue
            conf = -1.0
            try:
                conf = float(line[1][1])
            except Exception:
                pass
            item = {
                "text": text,
                "conf": conf,
                "box": _poly_to_box(line[0]),
                "index": idx,
            }
            items.append(item)
            lines.append(text)

    return items, "\n".join(lines)


def _poly_to_box(poly) -> Optional[dict]:
    try:
        arr = np.asarray(poly, dtype=float)
        if arr.size == 0 or arr.ndim != 2:
            return None
        xs = arr[:, 0]
        ys = arr[:, 1]
        return {
            "x": float(xs.min()),
            "y": float(ys.min()),
            "w": float(xs.max() - xs.min()),
            "h": float(ys.max() - ys.min()),
        }
    except Exception:
        return None


def _hash(text: str) -> str:
    import hashlib

    return hashlib.md5((text or "").encode("utf-8")).hexdigest()
