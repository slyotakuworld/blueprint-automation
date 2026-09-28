"""蓝图资源的落盘位置、「已使用 / 未使用」判定与清理。

新的资源组织方式（跟着蓝图走，不再一股脑塞一个目录）::

    <工程根>/resource/<蓝图所在的相对目录>/<资源文件>

例如蓝图 ``<工程根>/blueprints/登录.bp.json`` 用到的模板图，
存到 ``<工程根>/resource/blueprints/tpl_20260914_100000_123.png``。
一个目录里的蓝图对应 ``resource/`` 下一个同名目录，整体搬移 / 删除都很干脆。

为什么要抽这一层：
  - 以前所有资源都堆在一个目录，找图节点一多就分不清谁在用谁；
  - 「哪些资源已经没人引用」必须有个统一口径 —— 这里的口径就是
    **「整张蓝图 JSON 里出现过、并且文件真实存在」**，与导出打包的口径完全一致
    （直接复用 export.payload.scan_assets，避免"这里算用了、导出却没打包"两套标准）。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Iterable, List, Optional, Set, Tuple

# 新式资源根目录（相对工程根）
ASSET_ROOT = "resource"
# 清理扫描范围（相对工程根）；只扫 resource/ 下的图片
SCAN_DIRS: Tuple[str, ...] = (ASSET_ROOT,)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tif", ".tiff")

# 遍历蓝图时跳过的目录（运行产物 / 版本库 / 备份）
# 注意：刻意不剪 res/ —— 蓝图画在哪里都由用户决定，万一有蓝图放 res/ 下也要扫到
_PRUNE_DIRS = {
    "cache", "debug", "log", "logs", "__pycache__",
    ".git", ".bp", ".idea", ".vscode", ".workbuddy",
    "backup", "backups", "dist", "build",
}


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return str(path or "")


def to_posix_rel(project_root: str, path: str) -> str:
    """把绝对路径转成「相对工程根」的 posix 斜杠路径（工程外则返回绝对路径）。"""
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(project_root))
    except Exception:
        return path
    if rel.startswith(".."):
        return os.path.abspath(path)
    return rel.replace("\\", "/")


def blueprint_rel_dir(project_root: str, blueprint_path: Optional[str]) -> str:
    """蓝图所在目录（相对工程根，posix 斜杠）；蓝图在工程外或未保存 → 空串。"""
    if not blueprint_path:
        return ""
    try:
        rel = os.path.relpath(os.path.abspath(blueprint_path), os.path.abspath(project_root))
    except Exception:
        return ""
    if rel.startswith(".."):
        return ""
    d = os.path.dirname(rel).replace("\\", "/").strip("/")
    return "" if d == "." else d


def assets_dir_for(project_root: str, blueprint_path: Optional[str], create: bool = True) -> str:
    """该蓝图对应的资源目录：<工程根>/resource/<蓝图相对目录>（按需创建）。"""
    parts = [p for p in blueprint_rel_dir(project_root, blueprint_path).split("/") if p]
    d = os.path.join(os.path.abspath(project_root), ASSET_ROOT, *parts)
    if create:
        os.makedirs(d, exist_ok=True)
    return d


def unique_name(prefix: str, ext: str = ".png") -> str:
    """带时间戳 + 随机后缀的文件名，避免并发/同秒撞名。"""
    ts = time.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{ts}_{uuid.uuid4().hex[:6]}{ext}"


def write_image(image, full_path: str) -> bool:
    """把 PIL.Image 或 numpy(BGR) 写到 full_path。cv2 走 imencode+tofile 以支持中文路径。"""
    os.makedirs(os.path.dirname(full_path) or ".", exist_ok=True)
    ext = (os.path.splitext(full_path)[1] or ".png").lower()
    try:
        from PIL import Image as _PILImage

        if isinstance(image, _PILImage.Image):
            image.save(full_path)
            return os.path.isfile(full_path)
    except Exception:
        pass
    try:
        import cv2
        import numpy as np

        if isinstance(image, np.ndarray):
            ok, buf = cv2.imencode(ext if ext in (".png", ".jpg", ".jpeg", ".bmp", ".webp") else ".png", image)
            if ok:
                buf.tofile(full_path)
                return os.path.isfile(full_path)
    except Exception:
        pass
    return False


def save_image(
    project_root: str,
    blueprint_path: Optional[str],
    image,
    prefix: str = "tpl",
    ext: str = ".png",
) -> str:
    """把图片存进该蓝图对应的 resource 目录，返回「相对工程根」的 posix 路径（存进节点参数的就是它）。

    返回空串表示保存失败（调用方应给出提示，不要静默继续）。
    """
    if not project_root or not os.path.isdir(project_root):
        return ""
    d = assets_dir_for(project_root, blueprint_path)
    full = os.path.join(d, unique_name(prefix, ext))
    if not write_image(image, full):
        return ""
    return to_posix_rel(project_root, full)


def copy_into_store(
    project_root: str,
    blueprint_path: Optional[str],
    src_path: str,
    prefix: str = "import",
) -> str:
    """把外部图片拷进该蓝图的 resource 目录，返回相对工程根的 posix 路径。"""
    if not (project_root and os.path.isfile(src_path)):
        return ""
    ext = os.path.splitext(src_path)[1].lower() or ".png"
    d = assets_dir_for(project_root, blueprint_path)
    full = os.path.join(d, unique_name(prefix, ext))
    try:
        import shutil

        shutil.copy2(src_path, full)
    except Exception:
        return ""
    return to_posix_rel(project_root, full)


# ============================================================
# 遍历
# ============================================================
def iter_project_blueprints(project_root: str) -> Iterable[str]:
    """遍历工程内所有蓝图文件（跳过隐藏目录、运行产物、res/ 与备份文件）。"""
    if not project_root or not os.path.isdir(project_root):
        return
    for dirpath, dirnames, filenames in os.walk(project_root):
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d.lower() not in _PRUNE_DIRS
        ]
        for fn in filenames:
            if fn.lower().endswith(".bp.json") and ".backup_" not in fn:
                yield os.path.join(dirpath, fn)


def iter_files(directory: str) -> List[str]:
    """列出目录下所有图片文件（递归）。"""
    out: List[str] = []
    if not directory or not os.path.isdir(directory):
        return out
    for dirpath, _dirnames, filenames in os.walk(directory):
        for fn in filenames:
            if fn.lower().endswith(".tmp") or ".backup_" in fn.lower():
                continue
            if os.path.splitext(fn)[1].lower() in IMAGE_EXTS:
                out.append(os.path.join(dirpath, fn))
    return out


def iter_scan_files(project_root: str) -> List[str]:
    """清理扫描范围内的全部图片（resource/）。"""
    out: List[str] = []
    for sub in SCAN_DIRS:
        out.extend(iter_files(os.path.join(project_root, sub)))
    return out


# ============================================================
# 引用扫描 / 未使用资源
# ============================================================
def collect_referenced_assets(project_root: str) -> Set[str]:
    """扫全部蓝图（含子画布 / 内嵌组件调用），返回「被引用且真实存在」的图片绝对路径集合。

    口径与导出打包完全一致：解析后复用 export.payload.scan_assets。
    """
    used: Set[str] = set()
    if not project_root:
        return used
    try:
        from ..export.payload import scan_assets
    except Exception:
        return used
    for bp in iter_project_blueprints(project_root):
        try:
            with open(bp, "r", encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            continue
        if not (isinstance(raw, dict) and isinstance(raw.get("nodes"), dict)):
            continue
        try:
            assets, _missing = scan_assets(raw)
        except Exception:
            continue
        for a in assets or []:
            orig = a.get("orig") if isinstance(a, dict) else None
            if isinstance(orig, str) and orig:
                used.add(_norm(orig))
    return used


def find_unused_assets(project_root: str) -> Tuple[List[str], int]:
    """返回 (未被任何蓝图引用的图片绝对路径列表, 扫描到的资源总数)。"""
    files = iter_scan_files(project_root)
    if not files:
        return [], 0
    used = collect_referenced_assets(project_root)
    unused = [p for p in files if _norm(p) not in used]
    unused.sort(key=lambda p: (os.path.dirname(p).lower(), os.path.basename(p).lower()))
    return unused, len(files)
