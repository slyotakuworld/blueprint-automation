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
def _ocr_runtime_dirs() -> List[str]:
    """OCR 运行时的候选位置：程序目录 / ocr_runtime，开发模式下还有项目根（src 的上一级）。"""
    cands: List[str] = []
    try:
        from src.config.app_paths import APP_DIR
        cands.append(os.path.join(APP_DIR, "ocr_runtime"))
        parent = os.path.dirname(os.path.abspath(APP_DIR))
        cands.append(os.path.join(parent, "ocr_runtime"))
    except Exception:
        pass
    return cands


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
      2. 用了 OCR 但没带 ocr_runtime/ 目录 → 导出的 EXE 识别节点必然报「OCR 未安装」。
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

    # ---------- 2) OCR 运行时 ----------
    try:
        if uses_ocr(graph, project_root=project_root):
            if not any(os.path.isdir(d) for d in _ocr_runtime_dirs()):
                issues.append({
                    "level": "warn",
                    "msg": "蓝图包含「文字识别」节点，但没找到 ocr_runtime/ 目录："
                           "导出后必须把 ocr_runtime/ 放到 EXE 同级目录，否则识别节点会报「OCR 未安装」。",
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

    return ExportResult(
        ok=True,
        out_path=out_abs,
        asset_count=payload.asset_count,
        total_bytes=total,
        missing=list(payload.missing),
        message="导出完成",
    )
