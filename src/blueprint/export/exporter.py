"""把当前蓝图导出成一个可独立运行的 EXE（主程序侧逻辑）。

做法：
    1. 找到运行时模板 exe：<程序目录>/BlueprintRuntime.exe
       （由 build_runtime.cmd / build.cmd 生成，见项目根脚本）
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
                "请先运行项目根目录下的 build_runtime.cmd 生成运行时模板"
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
