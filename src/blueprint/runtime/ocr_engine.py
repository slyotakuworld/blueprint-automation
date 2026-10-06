"""内置 OCR 引擎（懒加载、常驻复用）。

设计要点（对齐 ccgyame/ocr_service 的 engine.py 思路，但作为库内嵌）：
  - 引擎按 (lang) 缓存，只初始化一次，之后反复复用，避免每次识别都重新加载模型导致卡顿。
  - 模型/依赖按需加载：本模块 import 时不碰 paddle，首次调用 recognize 才真正 import PaddleOCR。
  - PaddlePaddle 3.x 的 PIR 兼容性坑，靠 import 前设置 FLAGS_enable_pir_api=0 规避。
  - 流水线只开 det+rec 两个阶段（文档方向/矫正/行方向全关，屏幕截图用不上——
    它们对结果零贡献，关掉可省掉 UVDoc 等三个模型的加载）。
  - 推理后端默认优先用 ONNX Runtime（见下），不可用时退回 paddle 原生后端。

⚠ 实测结论（2026-10-06，本机 2 物理核/4 逻辑核 Skylake-U，paddle 3.2.2 +
  PaddleOCR 3.7.0 + oneDNN 3.6.2）：
  「慢」的根因**不在本模块的参数**。逐一对照过 MKLDNN 开/关、cpu_threads=2/10、
  OMP_NUM_THREADS=1/4、FLAGS_enable_pir_api=0/1 —— 全部差异 <20%，都在噪声内。
  进程内首跑也几乎不是瓶颈（首跑 4.7s vs 稳态 3.8s，仅差 ~20%），故**不做预热**。
  真正的原因是 **paddle 的卷积走 oneDNN，在本机这类仅有 AVX2 的老 CPU 上内核效率
  极低**：oneDNN verbose 里单层 1x1 conv 就要 660ms、有效算力 0.86 GFLOPS，而同机
  paddle 的 GEMM 实测 58 GFLOPS、numpy 91 GFLOPS（差 67 倍）⇒ 硬件与框架 BLAS 都正常。

✅ 解法 = **换推理后端**（模型权重与网络结构一字不改，准确率不变）：
  同一张图实测（同一台机、同一份 PP-OCRv6_medium det+rec）——
    paddle 原生后端：5265 / 4240 / 4417 ms
    onnxruntime  ： 861 /  853 /  865 ms   ⇒ 约 5 倍，输出文本逐条一致。
  原理：ONNX Runtime 的 CPU 卷积走自家 MLAS 内核，不经过 oneDNN，绕开了这个退化。
  ⇒ 默认 auto：装了 onnxruntime 就用它。`BP_OCR_ENGINE=paddle` 可强制退回原生后端
    （排查用）；`BP_OCR_ENGINE=onnxruntime` 可强制启用（缺包时报错而非静默降级）。
  ⚠ onnxruntime 需要**另一份 ONNX 格式的模型**（`*_onnx`，det+rec 约 140MB），
    由 ocr-runtime / 启动器的模型预取一并下载；导出时和 paddle 模型一起躺在
    ocr_runtime/models 随目录携带，离线可用。
  ⚠ MKLDNN 开关（BP_OCR_MKLDNN）只在 paddle 后端下有意义。

  - 模型缓存目录定位到可插拔的 ocr_runtime/ 目录（见 get_runtime_dir），便于「动态打包」——
    导出时若蓝图不含 OCR 节点，就完全不带这个目录。

本模块不依赖 Qt，主程序与运行时 exe 共用。
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Dict, Optional

import numpy as np

# 必须在 import paddleocr 之前设置：关 PIR API（3.x 的 PIR 兼容性坑）。
# 注意：不再全局关 MKLDNN —— 它是 CPU 推理的提速主力，关了识别一张图要几分钟。
# 前提：paddlepaddle 必须钉 3.2.2！3.3.0/3.3.1 的 oneDNN PIR 转换有回归，
# MKLDNN + PP-OCRv6 一推理就报 NotImplementedError（见 bp_dev.py OCR_PKGS 注释）。
os.environ.setdefault("FLAGS_enable_pir_api", "0")
# 应急开关：仅 paddle 后端有效。设 BP_OCR_MKLDNN=0 关掉 oneDNN 加速（排查用）。
_MKLDNN_ENABLED = os.environ.get("BP_OCR_MKLDNN", "1") != "0"
# 推理后端选择：auto（默认，装了 onnxruntime 就用它）/ onnxruntime / paddle。
# 见模块 docstring —— 换后端不改模型，只换执行引擎，实测约 5 倍且结果一致。
_BACKEND_PREF = os.environ.get("BP_OCR_ENGINE", "auto").strip().lower()


def _has_module(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:
        return False


def _pick_backend() -> Optional[str]:
    """返回要传给 PaddleOCR 的 engine；None = 用 paddle 原生后端（默认行为）。

    auto 模式下若 onnxruntime 不可用会**静默退回** paddle（保证功能不中断）；
    显式指定 onnxruntime 时缺包则抛错（避免用户以为生效了其实没生效）。
    """
    if _BACKEND_PREF in ("paddle", "paddle_static", "native"):
        return None
    if _BACKEND_PREF == "onnxruntime":
        if not _has_module("onnxruntime"):
            raise OcrUnavailable(
                "BP_OCR_ENGINE=onnxruntime 但未安装 onnxruntime 包。"
                "请运行 start.cmd（菜单 1/2 会自动按 requirements.txt 安装）。"
            )
        return "onnxruntime"
    if _BACKEND_PREF not in ("auto", ""):
        raise OcrUnavailable(f"未知的 BP_OCR_ENGINE={_BACKEND_PREF!r}（可选 auto/onnxruntime/paddle）")
    return "onnxruntime" if _has_module("onnxruntime") else None


def get_runtime_dir() -> str:
    """返回 OCR 运行时目录（模型缓存放这里）。可被动态打包/测试覆盖。

    ⚠ 用「ASCII 视图」——paddle 的 C++ 层打不开含中文的路径（窄字符 API），
    中文用户名的电脑上 EXE 放桌面/下载等目录时，模型必须经
    C:\\Users\\Public 下的目录联接访问。详见 app_paths.ocr_runtime_view_dir()。
    """
    from src.config.app_paths import ocr_runtime_view_dir

    return ocr_runtime_view_dir()


class OcrUnavailable(Exception):
    """OCR 引擎不可用（依赖未安装 / 模型未就绪）。"""


def _chain(e: BaseException, depth: int = 5) -> str:
    """把异常 __cause__ 链拼成一串。

    paddleocr 会把自己的真实错误用 `raise RuntimeError(...) from e` 包一层
    （如 paddlex 的 DependencyError 不写明细，只留一句「参照安装文档」），
    不挖 __cause__ 就永远看不到真凶。
    """
    parts: list[str] = []
    seen: set[int] = set()
    cur: Optional[BaseException] = e
    while cur is not None and depth > 0 and id(cur) not in seen:
        seen.add(id(cur))
        msg = str(cur).strip()
        parts.append(f"{type(cur).__name__}: {msg}" if msg else type(cur).__name__)
        cur = cur.__cause__ or cur.__context__
        depth -= 1
    return " <- ".join(parts)


class OcrEngine:
    """PaddleOCR 识别引擎（懒加载、常驻内存复用）。"""

    def __init__(self, lang: str = "ch"):
        self.lang = lang
        self._engine = None
        self._lock = threading.Lock()
        # 首次初始化耗时（ms），由 recognize 随结果带回一次后清零——
        # 用于区分「初始化慢」和「单张推理慢」，二者解法完全不同。
        self._pending_init_ms = 0
        self._engine_info = ""

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
                    "未找到 OCR 引擎（paddleocr）。请运行 start.cmd（菜单 1/2 会自动"
                    "安装依赖与 OCR 库；命令行等价：start.cmd ocr-runtime）。"
                ) from e
            try:
                t0 = time.perf_counter()
                # 只留 det + rec 两个核心阶段（准确率与默认流水线一致——模型没变），
                # 关掉三个对「屏幕截图」纯开销的阶段：
                #   use_doc_orientation_classify  整图方向分类（截图永远是正的）
                #   use_doc_unwarping             UVDoc 文档矫正（给弯曲纸张用的，
                #                                 对大截图 CPU 上分钟级，最大性能杀手）
                #   use_textline_orientation      逐行方向分类（屏幕文本没有旋转行）
                kw: Dict[str, Any] = {
                    "lang": self.lang,
                    "use_doc_orientation_classify": False,
                    "use_doc_unwarping": False,
                    "use_textline_orientation": False,
                }
                backend = _pick_backend()
                if backend:
                    # engine=onnxruntime 时 paddle_static 的配置无用，不要一起传
                    kw["engine"] = backend
                else:
                    kw["enable_mkldnn"] = _MKLDNN_ENABLED
                self._engine = PaddleOCR(**kw)
                self._pending_init_ms = int((time.perf_counter() - t0) * 1000)
                # 记录引擎指纹，随下一次识别结果带回一次——用于确认实际生效的后端
                try:
                    import paddle

                    info = f"paddle={paddle.__version__}"
                    if backend:
                        import onnxruntime as ort

                        info += f" backend=onnxruntime {ort.__version__}"
                    else:
                        info += f" backend=paddle mkldnn={'on' if _MKLDNN_ENABLED else 'off'}"
                    self._engine_info = info
                except Exception:
                    self._engine_info = f"backend={backend or 'paddle'}"
            except OcrUnavailable:
                raise
            except Exception as e:
                raise OcrUnavailable(f"PaddleOCR 初始化失败: {_chain(e)}") from e
            return self._engine

    def recognize(self, img_bgr: np.ndarray) -> Dict[str, Any]:
        """识别 BGR 图像，返回 {items, text, sha}。items 为 [{text,conf,box,index}]。"""
        engine = self._ensure()
        img_rgb = cv2_cvt_color(img_bgr)
        t0 = time.perf_counter()
        try:
            result = engine.ocr(img_rgb)
        except Exception as e:
            return {"items": [], "text": "", "sha": "", "error": f"{type(e).__name__}: {e}"}
        infer_ms = int((time.perf_counter() - t0) * 1000)
        init_ms = self._pending_init_ms
        self._pending_init_ms = 0
        engine_info = self._engine_info
        self._engine_info = ""

        items, text = _extract(result)
        return {
            "items": items,
            "text": text,
            "sha": _hash(text),
            "infer_ms": infer_ms,
            "init_ms": init_ms,
            "engine_info": engine_info,
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
