"""把当前蓝图导出成一个可独立运行的 EXE（主程序侧逻辑）。

做法：
    1. 找到运行时模板 exe：<程序目录>/BlueprintRuntime.exe
       （由 start.cmd 的「打包发布版 / 仅重打运行时模板」生成，实现见 scripts/bp_dev.py）
    2. 把蓝图 JSON + 它引用的图片资源打成载荷 zip（见 payload.py）
    3. 复制模板 exe，并在末尾追加载荷（见 overlay.py）

产物就是一个单文件 exe，双击即可脱离本项目运行。
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .overlay import append_payload_to_exe, has_payload
from .payload import build_payload, scan_assets

TEMPLATE_EXE_NAME = "BlueprintRuntime.exe"


@dataclass
class ExportResult:
    ok: bool
    out_path: str = ""
    message: str = ""
    asset_count: int = 0
    total_bytes: int = 0
    missing: List[str] = field(default_factory=list)
    ocr_carried: bool = False   # 是否已把 ocr_runtime/ 复制到导出 EXE 同级
    ocr_note: str = ""          # 携带结果说明（成功位置 / 失败原因），供界面展示


def runtime_template_path() -> str:
    """运行时模板 exe 的路径：与主程序同一个目录。"""
    from src.config.app_paths import APP_DIR

    return os.path.join(APP_DIR, TEMPLATE_EXE_NAME)


def template_available() -> bool:
    return os.path.isfile(runtime_template_path())


def template_info() -> Dict[str, Any]:
    """返回模板 exe 的信息，供界面提示用。"""
    p = runtime_template_path()
    exists = os.path.isfile(p)
    size = 0
    if exists:
        try:
            size = os.path.getsize(p)
        except Exception:
            size = 0
    return {"path": p, "exists": exists, "size": size}


def preview_assets(graph) -> Any:
    """预览：扫描蓝图会带出哪些图片资源、哪些找不到。"""
    return scan_assets(graph.to_dict())


# ============================================================
# 导出前体检
# ============================================================
# 2026-10-05 起方案变更：OCR **库**（paddlepaddle/paddleocr）装进 .venv、由
# PyInstaller 直接打进运行时模板；`ocr_runtime/` 里只剩**模型**（models/）。
# 导出含「文字」节点的蓝图时，只把 models/ 复制到 EXE 同级的 ocr_runtime/ 下。
OCR_RUNTIME_DIRNAME = "ocr_runtime"
OCR_MODELS_DIRNAME = "models"
# 复制模型时要跳过的东西（PaddleX 缓存目录等，运行时会自建）
_COPY_IGNORE = shutil.ignore_patterns("__pycache__", "locks", "temp", "func_ret")


def _ocr_runtime_dirs() -> List[str]:
    """OCR 运行时的候选位置：程序目录下的 ocr_runtime/，开发模式下还有项目根。"""
    cands: List[str] = []
    try:
        from src.config.app_paths import APP_DIR
        cands.append(os.path.join(APP_DIR, OCR_RUNTIME_DIRNAME))
        parent = os.path.dirname(os.path.abspath(APP_DIR))
        cands.append(os.path.join(parent, OCR_RUNTIME_DIRNAME))
    except Exception:
        pass
    # 开发模式兜底：本文件在 src/blueprint/export/ 下 → 再上三级是项目根
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))))
        cands.append(os.path.join(root, OCR_RUNTIME_DIRNAME))
    except Exception:
        pass
    seen: set = set()
    out: List[str] = []
    for d in cands:
        k = os.path.normcase(os.path.abspath(d))
        if k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _has_ocr_models(d: str) -> bool:
    """目录里是否有**可用的** OCR 模型（ocr_runtime/models/ 非空即算）。

    模型缺失时 paddleocr 首次识别会尝试联网下载——导出物以「离线可用」为目标，
    所以导出前 models/ 必须在位；只有目录存在且有文件才算可用。
    """
    models = os.path.join(d, OCR_MODELS_DIRNAME)
    if not os.path.isdir(models):
        return False
    try:
        return any(os.scandir(models))
    except OSError:
        return False


def ocr_runtime_source_dir() -> Optional[str]:
    """返回一个可用的 ocr_runtime/ 源目录（必须含真正的模型），找不到返回 None。"""
    for d in _ocr_runtime_dirs():
        if _has_ocr_models(d):
            return d
    return None


def ocr_runtime_size_mb(d: str) -> float:
    """估算 ocr_runtime/models 体积（MB），仅供导出前提示用。"""
    total = 0
    for root, _dirs, files in os.walk(os.path.join(d, OCR_MODELS_DIRNAME)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1048576.0


def carry_ocr_runtime(out_exe_path: str) -> tuple:
    """把 ocr_runtime/models 复制到导出 EXE 的同级目录，返回 (是否成功, 说明)。

    蓝图不含「文字」节点时不会调用本函数 —— 这就是「按需携带」：没用 OCR 的导出物
    一个字节都不多带。

    库（paddle 等）已内置于模板 EXE，这里只带模型（约 180MB），几秒即完。
    仍保持**原子化**（先拷 `<dst>.copying` 再改名），中断不留残缺目录。
    """
    dst = os.path.join(
        os.path.dirname(os.path.abspath(out_exe_path)), OCR_RUNTIME_DIRNAME
    )
    if _has_ocr_models(dst):
        return True, f"OCR 模型已在位（{dst}），未重复复制"
    src = ocr_runtime_source_dir()
    if not src:
        return False, (
            "未找到可用的 OCR 模型（ocr_runtime/models/ 为空或不存在）。"
            "请先在启动器选「准备 OCR 运行时」（命令行：start.cmd ocr-runtime）预取模型，再导出。"
        )
    dst_models = os.path.join(dst, OCR_MODELS_DIRNAME)
    tmp = dst_models + ".copying"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        shutil.copytree(
            os.path.join(src, OCR_MODELS_DIRNAME), tmp, ignore=_COPY_IGNORE
        )
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"复制 OCR 模型失败（磁盘空间或权限？）：{e}"
    try:
        # 清掉可能残留的旧目录/旧半成品，再把临时的改名上去
        if os.path.isdir(dst_models):
            shutil.rmtree(dst_models, ignore_errors=True)
        os.replace(tmp, dst_models)
    except Exception as e:  # noqa: BLE001
        shutil.rmtree(tmp, ignore_errors=True)
        return False, f"OCR 模型落位失败：{e}"
    return True, f"已携带 OCR 模型（{ocr_runtime_size_mb(dst):.0f} MB）→ {dst_models}"


def runtime_source_mtime() -> float:
    """编辑器侧「运行时代码」的最新修改时间（用于判断模板是不是旧的）。"""
    newest = 0.0
    try:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        rt_dir = os.path.join(root, "src", "blueprint", "runtime")
        if os.path.isdir(rt_dir):
            for fn in os.listdir(rt_dir):
                if fn.endswith(".py"):
                    try:
                        newest = max(newest, os.path.getmtime(os.path.join(rt_dir, fn)))
                    except OSError:
                        pass
        runner_py = os.path.join(root, "src", "blueprint_runner.py")
        if os.path.isfile(runner_py):
            newest = max(newest, os.path.getmtime(runner_py))
    except Exception:
        pass
    return newest


def preflight(graph, project_root: Optional[str] = None) -> List[Dict[str, str]]:
    """导出前体检。返回 [{"level": "error"|"warn", "msg": ...}]，空列表表示一切正常。

    检查三项（都是「不检查就会白忙一场」的类型）：
      1. 组件引用断链：蓝图里「调用组件」指向的 .bp.json 不存在（含子画布与递归嵌套）。
      2. 用了 OCR 但本机还没准备 ocr_runtime/（导出会自动携带它，缺了则 EXE 的识别节点会报「OCR 未安装」）。
      3. 运行时模板比编辑器里的运行时代码旧 → 导出的 EXE 跑的还是旧代码（需执行 start.cmd build-runtime）。
    """
    issues: List[Dict[str, str]] = []

    # ---------- 1) 组件引用断链 ----------
    missing: List[str] = []
    visited: set = set()

    def _walk_refs(g, depth: int = 0):
        if g is None or depth > 10:
            return
        try:
            nodes = getattr(g, "nodes", {}) or {}
        except Exception:
            return
        refs: List[str] = []
        for node in nodes.values():
            if getattr(node, "type", "") == "Component":
                r = (getattr(node, "params", None) or {}).get("blueprint_path")
                if isinstance(r, str) and r.strip():
                    refs.append(r.strip())
            p = getattr(node, "params", None)
            if isinstance(p, dict):
                for k in ("loop_body", "seq_body", "par_body"):
                    body = p.get(k)
                    if not (isinstance(body, dict) and isinstance(body.get("nodes"), dict)):
                        continue
                    for sn in (body.get("nodes") or {}).values():
                        if not isinstance(sn, dict) or str(sn.get("type", "")).lower() != "component":
                            continue
                        sp = sn.get("data") if isinstance(sn.get("data"), dict) else sn.get("params")
                        if isinstance(sp, dict):
                            r = sp.get("blueprint_path")
                            if isinstance(r, str) and r.strip():
                                refs.append(r.strip())
        for r in refs:
            cand = r if os.path.isabs(r) else os.path.join(project_root or "", r)
            if not os.path.isfile(cand):
                if r not in missing:
                    missing.append(r)
                continue
            key = os.path.normcase(os.path.abspath(cand))
            if key in visited:
                continue
            visited.add(key)
            try:
                from src.blueprint.core.graph_store import BlueprintStore
                _walk_refs(BlueprintStore.load(cand), depth + 1)
            except Exception:
                pass

    if project_root:
        _walk_refs(graph)
        if missing:
            shown = "、".join(missing[:6]) + ("…" if len(missing) > 6 else "")
            issues.append({
                "level": "error",
                "msg": f"组件引用断链：{len(missing)} 个「调用组件」指向的蓝图文件不存在（{shown}）。"
                       "运行到该节点会失败（按空操作跳过）。",
            })

    # ---------- 2) OCR 模型 ----------
    # 含「文字」节点时，导出会自动把 ocr_runtime/models 复制到 EXE 同级
    # （库已内置于模板，只需模型）；只有「模型还没预取」才需要提醒。
    try:
        if uses_ocr(graph, project_root=project_root):
            if not ocr_runtime_source_dir():
                issues.append({
                    "level": "warn",
                    "msg": "蓝图包含「文字」节点，但本机还没有 OCR 模型（ocr_runtime/models/）。"
                           "导出会自动把它复制到 EXE 同级，现在缺了这一步，导出的 EXE 首次识别会联网下载模型。"
                           "请先在启动器选「准备 OCR 运行时」（命令行：start.cmd ocr-runtime）。",
                })
    except Exception:
        pass

    # ---------- 3) 运行时模板新旧 ----------
    try:
        tpl = runtime_template_path()
        if os.path.isfile(tpl):
            tpl_m = os.path.getmtime(tpl)
            src_m = runtime_source_mtime()
            if src_m > tpl_m + 1:
                from datetime import datetime as _dt
                issues.append({
                    "level": "warn",
                    "msg": "运行时模板比编辑器里的运行时代码旧（模板 "
                           f"{_dt.fromtimestamp(tpl_m):%Y-%m-%d %H:%M} < 代码 {_dt.fromtimestamp(src_m):%Y-%m-%d %H:%M}）："
                           "导出的 EXE 会跑旧代码，请先执行 start.cmd build-runtime 再导出。",
                })
    except Exception:
        pass

    return issues


def uses_ocr(graph, project_root: Optional[str] = None) -> bool:
    """判断蓝图（递归地）是否包含 OCR 识别节点。

    用于「动态打包」：含 OcrRecognize 节点时才需要携带 OCR 运行时（ocr_runtime/ 目录），
    不含则导出的 EXE 完全不带 OCR，体积不涨。

    递归遍历：
      - 内嵌子图（loop_body 等塞在 node.data 里的 dict）→ 按结构递归。
      - Component 组件（node.data.blueprint_path 文件引用）→ 加载组件蓝图文件递归检测。
    """
    def _walk(obj: Any, depth: int = 0) -> bool:
        if depth > 20:
            return False
        if isinstance(obj, dict):
            t = str(obj.get("type", ""))
            # 序列化后的蓝图用 type_short_name（ocr_recognize），也兼容完整名 OcrRecognize
            if t in ("OcrRecognize", "ocr_recognize"):
                return True
            # Component 组件：加载引用文件递归检测
            if t in ("Component", "component"):
                data = obj.get("data") or {}
                bp = str(data.get("blueprint_path", "") or "").strip()
                if bp and project_root:
                    cand = bp if os.path.isabs(bp) else os.path.join(project_root, bp)
                    if os.path.isfile(cand):
                        try:
                            from src.blueprint.core.graph_store import BlueprintStore
                            sub = BlueprintStore.load(cand)
                            if sub is not None and _walk(sub.to_dict(), depth + 1):
                                return True
                        except Exception:
                            pass
            return any(_walk(v, depth + 1) for v in obj.values())
        if isinstance(obj, (list, tuple)):
            return any(_walk(v, depth + 1) for v in obj)
        return False

    try:
        return _walk(graph.to_dict())
    except Exception:
        return False


def export_blueprint(
    graph,
    blueprint_name: str,
    out_path: str,
    source_project: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> ExportResult:
    """执行导出。graph 为 BlueprintGraph（不会被修改）。"""
    tmpl = runtime_template_path()
    if not os.path.isfile(tmpl):
        return ExportResult(
            ok=False,
            message=(
                "未找到运行时模板 BlueprintRuntime.exe。\n\n"
                f"预期位置：{tmpl}\n\n"
                "请先运行项目根目录下的 start.cmd 重打运行时模板"
                "（双击 start.cmd 选「4. 仅重打运行时模板」，或命令行 start.cmd build-runtime）"
                "（正式发布版已随主程序一起分发，通常不会缺）。"
            ),
        )

    out_abs = os.path.abspath(out_path)
    if out_abs == os.path.abspath(tmpl):
        return ExportResult(ok=False, message="输出路径不能指向运行时模板本身，请换一个文件名。")
    if not out_abs.lower().endswith(".exe"):
        out_abs += ".exe"

    if os.path.exists(out_abs):
        # 覆盖前确认可写（比如程序正被占用）
        try:
            with open(out_abs, "ab"):
                pass
        except Exception as e:
            return ExportResult(
                ok=False, message=f"目标文件已存在且无法覆盖（可能正在运行）：\n{out_abs}\n\n{e}"
            )

    try:
        payload = build_payload(
            graph.to_dict(),
            blueprint_name=str(blueprint_name or getattr(graph, "name", "") or "未命名蓝图"),
            source_project=source_project,
            settings=settings,
        )
    except Exception as e:
        return ExportResult(ok=False, message=f"打包蓝图资源失败：\n{e}")

    try:
        total = append_payload_to_exe(tmpl, payload.zip_bytes, out_abs)
    except Exception as e:
        return ExportResult(ok=False, message=f"写出可执行文件失败：\n{e}")

    # 自检：确认写出的文件确实带上了载荷
    if not has_payload(out_abs):
        return ExportResult(ok=False, message="导出文件校验失败：未检测到载荷，请重试。")

    # OCR 运行时：**只**在蓝图含「文字」节点时随 EXE 携带
    # （没用 OCR 的导出物一个字节都不多带 —— 这就是设计里的「按需」）。
    ocr_carried = False
    ocr_note = ""
    try:
        if uses_ocr(graph, project_root=source_project):
            ocr_carried, ocr_note = carry_ocr_runtime(out_abs)
    except Exception as e:  # noqa: BLE001
        ocr_note = f"携带 OCR 运行时失败：{e}"

    return ExportResult(
        ok=True,
        out_path=out_abs,
        asset_count=payload.asset_count,
        total_bytes=total,
        missing=list(payload.missing),
        ocr_carried=ocr_carried,
        ocr_note=ocr_note,
        message="导出完成",
    )
