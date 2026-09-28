"""导出载荷（payload）的构建与还原：把一张蓝图 + 它引用到的图片资源打成一个 zip。

为什么要有这一层：
    FindImage / IfImageDetect 这类节点在蓝图 JSON 里存的是【图片的绝对路径】
    （例如 C:\\...\\projects\\我的工程\\templates\\a.png）。一旦脱离原来的工程目录，
    这些路径全都失效，找图节点会直接失败。所以导出时必须：

      1. 递归扫描蓝图 JSON，把"确实存在的图片文件路径"收集起来；
      2. 在蓝图 JSON 里把这些路径替换成占位记号 @@BPASSET:序号@@；
      3. 把图片按 assets/xxxx_原名 的结构一起打进 zip；
      4. manifest.json 记录"记号 → 归档路径"的映射。

    运行端解包后，只要把 JSON 文本里的记号换回解包目录下的真实路径即可，
    因为是在 JSON 文本层面做替换，连 images_json 这种"字符串里套 JSON"的字段
    也能一并覆盖到。

本模块不依赖 Qt，主程序与运行时 exe 共用。
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

MANIFEST_NAME = "manifest.json"
BLUEPRINT_NAME = "blueprint.bp.json"
FORMAT_TAG = "blueprint-runtime-payload"
FORMAT_VERSION = 1

TOKEN_PREFIX = "@@BPASSET:"
TOKEN_RE = re.compile(r"@@BPASSET:(\d+)@@")

# 需要随蓝图一起打包的图片类型
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".gif", ".tif", ".tiff"}

_INVALID_NAME_CHARS = '\\/:*?"<>|'


@dataclass
class PayloadBuildResult:
    """一次载荷构建的结果。"""

    zip_bytes: bytes
    manifest: Dict[str, Any]
    assets: List[Dict[str, Any]] = field(default_factory=list)
    missing: List[str] = field(default_factory=list)

    @property
    def asset_count(self) -> int:
        return len(self.assets)

    @property
    def asset_total_size(self) -> int:
        return sum(int(a.get("size") or 0) for a in self.assets)


# ============================================================
# 扫描 / 记号化
# ============================================================
def _sanitize_name(name: str) -> str:
    out = "".join(("_" if ch in _INVALID_NAME_CHARS else ch) for ch in (name or "")).strip()
    return out or "asset"


def _is_image_path(s: str) -> bool:
    """字符串是否"看起来是图片路径"（只看扩展名，不碰文件系统）。"""
    if not s or len(s) > 8192 or "\n" in s or "\x00" in s:
        return False
    return os.path.splitext(s)[1].lower() in IMAGE_EXTS


def _isfile(path: str) -> bool:
    try:
        return os.path.isfile(path)
    except Exception:
        return False


def _resolve_img(s: str) -> str:
    """把「看起来是图片路径」的字符串解析成真实存在的绝对路径。

    新式资源路径是「相对工程根」的（如 res/脚本/组件1/tpl_xxx.png），
    直接丢给 os.path.isfile 会因为按进程 CWD 解析而误判成文件缺失 →
    整张图既不会被打包、导出后路径也失效。所以统一走 app_paths.get_img_path
    （编辑器找图、运行时找图用的也是它），保证「打包口径 = 找图口径」。
    解析不到（文件真的不存在）返回空串。
    """
    if _isfile(s):
        try:
            return os.path.abspath(s)
        except Exception:
            return s
    try:
        from src.config.app_paths import get_img_path

        cand = get_img_path(s)
    except Exception:
        return ""
    if cand and _isfile(cand):
        try:
            return os.path.abspath(cand)
        except Exception:
            return cand
    return ""


class _Scanner:
    """递归遍历蓝图结构：收集存在的图片、记录缺失的图片、把路径换成记号。"""

    def __init__(self) -> None:
        self.assets: List[Dict[str, Any]] = []
        self.missing: List[str] = []
        self._seen: Dict[str, str] = {}

    def token_for(self, path: str) -> str:
        ap = os.path.abspath(path)
        key = ap.lower() if os.name == "nt" else ap
        hit = self._seen.get(key)
        if hit is not None:
            return hit
        idx = len(self.assets)
        token = f"{TOKEN_PREFIX}{idx}@@"
        base = _sanitize_name(os.path.basename(ap))
        arc = f"assets/{idx:04d}_{base}"
        try:
            size = os.path.getsize(ap)
        except Exception:
            size = 0
        self.assets.append({"token": token, "orig": ap, "arc": arc, "size": size})
        self._seen[key] = token
        return token

    def walk(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: self.walk(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.walk(v) for v in obj]
        if isinstance(obj, str):
            return self._walk_str(obj)
        return obj

    def _walk_str(self, s: str) -> str:
        if not s:
            return s
        # 1) 整串就是一个存在的图片路径 → 换成记号
        if _is_image_path(s):
            real = _resolve_img(s)
            if real:
                return self.token_for(real)
            if s not in self.missing:
                self.missing.append(s)
            return s
        # 2) 可能是"字符串里套着 JSON"（如 images_json）→ 解出来递归、再序列化回去
        if len(s) <= 8 * 1024 * 1024 and s.lstrip()[:1] in ("{", "["):
            try:
                parsed = json.loads(s)
            except Exception:
                return s
            if isinstance(parsed, (dict, list)):
                new = self.walk(parsed)
                try:
                    return json.dumps(new, ensure_ascii=False, separators=(",", ":"))
                except Exception:
                    return s
        return s


def scan_assets(blueprint_dict: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """只扫描不打包：返回 (资源列表, 缺失列表)，供导出对话框预览用。"""
    sc = _Scanner()
    sc.walk(blueprint_dict)
    return sc.assets, sc.missing


def build_payload(
    blueprint_dict: Dict[str, Any],
    blueprint_name: str,
    source_project: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> PayloadBuildResult:
    """把蓝图字典 + 图片资源打成载荷 zip。

    blueprint_dict 不会被修改（内部边遍历边新建结构）。
    """
    sc = _Scanner()
    tokenized = sc.walk(blueprint_dict)
    blueprint_json = json.dumps(tokenized, ensure_ascii=False, indent=2)

    manifest: Dict[str, Any] = {
        "format": FORMAT_TAG,
        "format_version": FORMAT_VERSION,
        "blueprint_name": str(blueprint_name or "未命名蓝图"),
        "blueprint_file": BLUEPRINT_NAME,
        "source_project": str(source_project or ""),
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "asset_count": len(sc.assets),
        "settings": dict(settings or {}),
        "assets": {
            a["token"]: {"arc": a["arc"], "orig": a["orig"], "size": a["size"]}
            for a in sc.assets
        },
    }

    buf = io.BytesIO()
    skipped: List[str] = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
        z.writestr(BLUEPRINT_NAME, blueprint_json.encode("utf-8"))
        for a in sc.assets:
            try:
                z.write(a["orig"], a["arc"])
            except Exception:
                # 单个图片读不了不应该让整包失败，记下来提示用户
                skipped.append(a["orig"])
    missing = list(sc.missing) + [p for p in skipped if p not in sc.missing]
    return PayloadBuildResult(
        zip_bytes=buf.getvalue(), manifest=manifest, assets=sc.assets, missing=missing
    )


# ============================================================
# 运行端：解包 / 还原
# ============================================================
def load_manifest(zip_bytes: bytes) -> Dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        return json.loads(z.read(MANIFEST_NAME).decode("utf-8"))


def extract_to(zip_bytes: bytes, dest_dir: str) -> Dict[str, Any]:
    """把载荷解包到 dest_dir，返回 manifest。带基本的路径穿越防护。"""
    dest_abs = os.path.abspath(dest_dir)
    os.makedirs(dest_abs, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for member in z.infolist():
            name = member.filename
            target = os.path.abspath(os.path.join(dest_abs, name))
            if target != dest_abs and not target.startswith(dest_abs + os.sep):
                raise ValueError(f"载荷内含非法路径，已拒绝解包：{name}")
        z.extractall(dest_abs)
        manifest = json.loads(z.read(MANIFEST_NAME).decode("utf-8"))
    return manifest


def _token_to_path(tok: str, dest_dir: str, mapping: Dict[str, Any]) -> str:
    info = mapping.get(tok)
    if not isinstance(info, dict):
        return tok
    arc = str(info.get("arc") or "")
    if not arc:
        return tok
    parts = arc.replace("\\", "/").split("/")
    return os.path.join(dest_dir, *parts)


def _restore_obj(obj: Any, dest_dir: str, mapping: Dict[str, Any]) -> Any:
    if isinstance(obj, dict):
        return {k: _restore_obj(v, dest_dir, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_restore_obj(v, dest_dir, mapping) for v in obj]
    if isinstance(obj, str):
        return _restore_str(obj, dest_dir, mapping)
    return obj


def _restore_str(s: str, dest_dir: str, mapping: Dict[str, Any]) -> str:
    # 情况一：字符串本身是一段 JSON（如 IfImageDetect 的 images_json）
    #   解出来递归还原，再序列化回去 —— 转义完全交给 json 模块，天然处理多层嵌套
    if len(s) <= 8 * 1024 * 1024 and s.lstrip()[:1] in ("{", "["):
        try:
            parsed = json.loads(s)
        except Exception:
            parsed = None
        if isinstance(parsed, (dict, list)):
            new = _restore_obj(parsed, dest_dir, mapping)
            try:
                return json.dumps(new, ensure_ascii=False, separators=(",", ":"))
            except Exception:
                pass
    # 情况二：普通字符串，直接把记号换成真实路径
    #   （这里是 Python 字符串层面，不需要任何 JSON 转义）
    if TOKEN_PREFIX in s:
        return TOKEN_RE.sub(lambda m: _token_to_path(m.group(0), dest_dir, mapping), s)
    return s


def restore_blueprint_dict(
    blueprint_json_text: str, dest_dir: str, manifest: Dict[str, Any]
) -> Dict[str, Any]:
    """把载荷里的蓝图 JSON 文本还原成 dict，并把 @@BPASSET:n@@ 换成解包后的真实路径。

    刻意在【Python 对象层面】做替换而不是在 JSON 文本层面做字符串替换：
    images_json 这类字段是"字符串里又套了一层 JSON"，文本替换很容易把转义层数算错
    （内层路径需要转义两次），改成解析后递归替换就完全没有这个问题。
    """
    data = json.loads(blueprint_json_text)
    mapping = (manifest or {}).get("assets", {}) or {}
    if not mapping:
        return data
    return _restore_obj(data, dest_dir, mapping)

