"""蓝图类型（普通蓝图 / 组件蓝图）的读写与识别。

为什么单独抽一层：
    以前「组件」是靠「文件放在 components/ 目录下」识别的，等于把类型和存放位置绑死。
    现在蓝图放哪儿完全由用户决定（资源面板里想建在哪个目录就建在哪个目录），
    类型必须成为蓝图自身的元数据 —— 写在蓝图 JSON **顶层** 的 ``bp_type`` 字段里。

    写在顶层而不是 metadata 里，是为了「只读文件头部就能判类型」：
    BlueprintStore.save 用 sort_keys=True 序列化，``bp_type`` 字母序最靠前，
    永远落在文件第 2 行，资源树渲染时读前 4KB 即可，不必把每张蓝图完整解析一遍。

兼容：老蓝图没有 bp_type 字段 → 回退到目录约定（components/ 下视为组件），
      一旦被保存/切换类型，字段就补上了。
"""

from __future__ import annotations

import json
import os
from typing import Dict, Optional, Tuple

BP_TYPE_BLUEPRINT = "blueprint"
BP_TYPE_COMPONENT = "component"

VALID_TYPES = (BP_TYPE_BLUEPRINT, BP_TYPE_COMPONENT)

# 旧版目录约定：这些目录下的蓝图（且没写 bp_type）视为组件
LEGACY_COMPONENT_DIR = "components"

_HEADER_BYTES = 4096
_KEY = '"bp_type"'

# path(normcase) -> (mtime, bp_type)
_cache: Dict[str, Tuple[float, str]] = {}


def _norm(path: str) -> str:
    try:
        return os.path.normcase(os.path.abspath(path))
    except Exception:
        return str(path or "")


def _fallback_by_dir(path: str, project_root: Optional[str] = None) -> str:
    """老蓝图没有 bp_type 字段时的目录兜底：components/ 下视为组件。"""
    root = project_root or project_root_of(path)
    if not root:
        return BP_TYPE_BLUEPRINT
    try:
        rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    except Exception:
        return BP_TYPE_BLUEPRINT
    parts = rel.replace("\\", "/").split("/")
    if len(parts) > 1 and parts[0].lower() == LEGACY_COMPONENT_DIR:
        return BP_TYPE_COMPONENT
    return BP_TYPE_BLUEPRINT


def project_root_of(path: str) -> Optional[str]:
    """从 app_paths 拿当前工程根（Qt-free，未开工程返回 None）。"""
    try:
        from src.config.app_paths import project_root_dir

        return project_root_dir()
    except Exception:
        return None


def read_bp_type(path: str, project_root: Optional[str] = None) -> str:
    """读蓝图类型。非蓝图文件/读不到 → 返回普通蓝图。结果按 (路径, mtime) 缓存。"""
    if not path or not os.path.isfile(path):
        return BP_TYPE_BLUEPRINT
    key = _norm(path)
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return BP_TYPE_BLUEPRINT
    hit = _cache.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]

    val = ""
    try:
        with open(path, "rb") as f:
            head = f.read(_HEADER_BYTES)
        text = head.decode("utf-8", "ignore")
        i = text.find(_KEY)
        if i >= 0:
            seg = text[i + len(_KEY):]
            colon = seg.find(":")
            if colon >= 0:
                seg = seg[colon + 1:].lstrip()
                if seg[:1] == '"':
                    end = seg.find('"', 1)
                    if end > 0:
                        val = seg[1:end].strip().lower()
    except Exception:
        val = ""

    if val not in VALID_TYPES:
        val = _fallback_by_dir(path, project_root)
    _cache[key] = (mtime, val)
    return val


def is_component(path: str, project_root: Optional[str] = None) -> bool:
    return read_bp_type(path, project_root) == BP_TYPE_COMPONENT


def write_bp_type(path: str, bp_type: str, project_root: Optional[str] = None) -> bool:
    """原地改写蓝图 JSON 的 bp_type 字段。

    刻意只动这一个字段、不刷新 updated_at、不重新序列化整张蓝图：
    这是「分类」而不是「内容修改」，用户画布上的修改理应保持独立。
    """
    if bp_type not in VALID_TYPES:
        return False
    if not path or not os.path.isfile(path):
        return False
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            return False
        if raw.get("bp_type") == bp_type:
            return True
        raw["bp_type"] = bp_type
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw, f, sort_keys=True, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception:
        try:
            if os.path.exists(path + ".tmp"):
                os.remove(path + ".tmp")
        except Exception:
            pass
        return False
    invalidate(path)
    return True


def invalidate(path: Optional[str] = None) -> None:
    """清缓存（保存蓝图 / 外部改动后调用）。传 None 清全部。"""
    if path is None:
        _cache.clear()
        return
    _cache.pop(_norm(path), None)


def display_label(bp_type: str) -> str:
    return "组件" if bp_type == BP_TYPE_COMPONENT else "蓝图"
