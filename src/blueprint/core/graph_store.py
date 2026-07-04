import io
import base64
import json
import os
import shutil
import time
from typing import Any, Dict, Optional

from .models import BlueprintGraph


BACKUP_SUFFIX_TS = ".bp.backup_{ts}.json"


IMAGE_PARAM_NODES = {
    "FindImage",
    "IfImageDetect",
    "StillnessDetect",
}


# ============================================================
# 图像双模式：PIL↔b64 辅助（内嵌b64 / 独立PNG路径 双模式自动互转）
# ============================================================
def _pil_to_b64(img) -> Optional[str]:
    try:
        from PIL import Image as _PILImage

        if isinstance(img, _PILImage.Image):
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        pass
    try:
        import numpy as _np

        if isinstance(img, _np.ndarray):
            from PIL import Image as _PILImage2

            pil = _PILImage2.fromarray(img)
            buf = io.BytesIO()
            pil.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        pass
    return None


def _b64_to_pil(b64: str):
    try:
        from PIL import Image as _PILImage

        raw = base64.b64decode(b64)
        return _PILImage.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None


def _path_to_pil(path: str):
    try:
        from PIL import Image as _PILImage

        if path and os.path.exists(path):
            return _PILImage.open(path).convert("RGB")
    except Exception:
        pass
    return None


def _sanitize_params_for_save(params: Dict[str, Any]) -> Dict[str, Any]:
    """保存前清洗：把 PIL.Image / numpy.ndarray 转成 b64 字符串，确保 JSON 只含基本类型。"""
    out: Dict[str, Any] = {}
    for k, v in (params or {}).items():
        if v is None:
            out[k] = None
            continue
        # 尝试 PIL
        b64 = _pil_to_b64(v)
        if b64 is not None:
            # 如果原本字段是 image_b64 或 preview_b64，直接写；否则设 _pil_b64 并保留原始key
            if k in ("image_b64", "b64", "roi_preview_b64", "b64_preview"):
                out[k] = b64
            else:
                # 用户 params 里直接塞了 PIL 对象，转成 _b64 后缀，避免原 key 含义被改
                out[f"{k}_b64"] = b64
            continue
        if isinstance(v, (list, tuple)):
            out[k] = [
                _sanitize_params_for_save({"_": x})["_"] if isinstance(x, (dict,)) else x
                for x in v
            ]
            continue
        if isinstance(v, dict):
            out[k] = _sanitize_params_for_save(v)
            continue
        # 基本类型：str/int/float/bool/None → 原样
        if isinstance(v, (str, int, float, bool)):
            out[k] = v
            continue
        # 其他不可序列化类型：兜底 str()
        try:
            s = json.dumps(v, ensure_ascii=False)
            out[k] = v
        except Exception:
            out[k] = str(v)
    return out


def _hydrate_params_after_load(node_type: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """加载后缓存 image_b64 / template_path 对应的 PIL 图到 node.runtime_attrs（不写回params，避免二次序列化膨胀）。

    注意：这个函数只处理 params 本身；调用方需要把返回的 runtime_attrs 字典写到 Node.runtime_attrs 上。
    """
    runtime_attrs: Dict[str, Any] = {}
    if node_type not in IMAGE_PARAM_NODES:
        return runtime_attrs
    # 优先级：template_path 存在且文件有效 > image_b64 非空
    tpl_path = params.get("template_path") or params.get("template") or ""
    if isinstance(tpl_path, str) and tpl_path and os.path.exists(tpl_path):
        pil = _path_to_pil(tpl_path)
        if pil is not None:
            runtime_attrs["_pil_image"] = pil
            return runtime_attrs
    # b64：几个常见 key 都试一遍（常见格式 b64 / b64_preview，其他：image_b64 / roi_preview_b64）
    for k in ("image_b64", "b64", "b64_preview", "roi_preview_b64"):
        v = params.get(k)
        if isinstance(v, str) and v:
            pil = _b64_to_pil(v)
            if pil is not None:
                runtime_attrs["_pil_image"] = pil
                break
    # IfImageDetect: images_json[] 数组，逐个缓存
    if node_type == "IfImageDetect":
        raw = params.get("images_json")
        if isinstance(raw, str) and raw:
            try:
                arr = json.loads(raw)
            except Exception:
                arr = None
        elif isinstance(raw, list):
            arr = raw
        else:
            arr = None
        if isinstance(arr, list):
            cached = []
            for item in arr:
                if not isinstance(item, dict):
                    cached.append(None)
                    continue
                p = item.get("path")
                b = item.get("b64") or item.get("image_b64")
                pil = None
                if isinstance(p, str) and p and os.path.exists(p):
                    pil = _path_to_pil(p)
                if pil is None and isinstance(b, str) and b:
                    pil = _b64_to_pil(b)
                cached.append(pil)
            runtime_attrs["_pil_images"] = cached
    return runtime_attrs


# ============================================================
# BlueprintStore：save/load + 自动备份 + 图像预处理
# ============================================================
class BlueprintStore:
    """蓝图 JSON 存储：save/load + 自动备份 + sort_keys + ensure_ascii=False。"""

    EXT = ".bp.json"

    @staticmethod
    def _split_bp_path(file_path: str):
        """正确拆分蓝图文件路径：处理 .bp.json 双后缀。

        返回 (目录, 无后缀基础名, 完整后缀)
        例：path/to/game3.bp.json → ("path/to", "game3", ".bp.json")
        例：path/to/other.json   → ("path/to", "other", ".json")
        """
        directory = os.path.dirname(file_path) or "."
        basename = os.path.basename(file_path)
        if basename.endswith(BlueprintStore.EXT):  # ".bp.json"
            stem = basename[: -len(BlueprintStore.EXT)]
            return directory, stem, BlueprintStore.EXT
        name, ext = os.path.splitext(basename)
        return directory, name, ext

    @staticmethod
    def save(graph: BlueprintGraph, file_path: str, make_backup: bool = True) -> bool:
        try:
            directory = os.path.dirname(file_path) or "."
            os.makedirs(directory, exist_ok=True)

            if make_backup and os.path.exists(file_path):
                try:
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    _, stem, _ = BlueprintStore._split_bp_path(file_path)
                    backup_path = os.path.join(directory, f"{stem}.bp.backup_{ts}.json")
                    shutil.copy2(file_path, backup_path)
                    BlueprintStore._prune_backups(directory, stem, keep=5)
                except Exception:
                    pass

            # 保存前 1) params → PIL/np.ndarray → b64
            for nid, node in graph.nodes.items():
                try:
                    node.params = _sanitize_params_for_save(node.params)
                except Exception:
                    pass

            # 保存前 2) 只在真正落盘时刷新 updated_at！
            #    （to_dict 是纯只读函数，不再写时间戳；保证 hash 计算时"磁盘内容 hash = 保存后内容 hash"）
            try:
                graph.touch()
            except Exception:
                pass

            data = graph.to_dict()
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, sort_keys=True, indent=2, ensure_ascii=False)
            return True
        except Exception:
            return False

    @staticmethod
    def _prune_backups(directory: str, stem_name: str, keep: int = 5):
        directory = directory or "."
        prefix = stem_name + ".bp.backup_"
        try:
            files = [
                os.path.join(directory, f)
                for f in os.listdir(directory)
                if f.startswith(prefix) and f.endswith(".json")
            ]
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for old in files[keep:]:
                try:
                    os.remove(old)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def load(file_path: str) -> Optional[BlueprintGraph]:
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)

            # ---------------- 格式前置校验 ----------------
            # 蓝图 JSON 必须包含 nodes 和 links
            if not isinstance(raw, dict):
                return None
            has_nodes_dict = "nodes" in raw and isinstance(raw["nodes"], dict)
            has_links_list = "links" in raw and isinstance(raw["links"], list)
            has_blueprint_signature = has_nodes_dict and has_links_list
            if not has_blueprint_signature:
                signature_hint = ""
                if "project_name" in raw and "created_at" in raw:
                    signature_hint = "（看起来是工程配置文件 project_meta.json，不是蓝图）"
                elif "hotkeys" in raw and "recent_projects" in raw:
                    signature_hint = "（看起来是用户配置 config.json，不是蓝图）"
                print(
                    f"[WARN] BlueprintStore.load: 跳过非蓝图文件 {file_path} "
                    f"缺少 nodes/links 蓝图结构 {signature_hint}"
                )
                return None

            # ---------------- 真正反序列化 ----------------
            g = BlueprintGraph.from_dict(raw)
            # 加载后：image_b64 / template_path → PIL 缓存到 runtime_attrs
            for nid, node in g.nodes.items():
                try:
                    ra = _hydrate_params_after_load(node.type, node.params)
                    if ra:
                        node.runtime_attrs.update(ra)
                except Exception:
                    pass
            return g
        except Exception as e:
            BlueprintStore._emit_log(f"[Error] BlueprintStore.load 异常: {e}", level="ERROR")
            return None

    @staticmethod
    def add_node(g: BlueprintGraph, node_type: str, pos=(0, 0)) -> "Node":
        """工厂：给指定蓝图分配自增数字id并装配节点（1,2,3…）。

        - 不使用UUID（用户要求数字自增）
        - 每张蓝图独立计数（BlueprintGraph._next_node_id）
        - 自动注册到 g.nodes[id]
        """
        from .node_library import create_node as _mk_node

        nid_int = int(getattr(g, "_next_node_id", 1))
        nid = str(nid_int)
        # 兜底：与现有节点id重复时自增跳过（极端场景）
        while nid in g.nodes:
            nid_int += 1
            nid = str(nid_int)
        node = _mk_node(node_type, pos=pos, node_id=nid)
        g._next_node_id = nid_int + 1
        g.nodes[node.id] = node
        return node

    @staticmethod
    def new_graph(name: str = "Untitled") -> BlueprintGraph:
        g = BlueprintGraph(name=name)
        # Start = 蓝图中分配的第一个节点（id=1）
        BlueprintStore.add_node(g, "Start", pos=(100, 200))
        return g

    @staticmethod
    def default_path(graph: BlueprintGraph, base_dir: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (graph.name or "Untitled")).strip()
        if not safe:
            safe = "Untitled"
        return os.path.join(base_dir, safe + BlueprintStore.EXT)

    # -------- 可选：日志回写（如果拿不到主窗口，就忽略）--------
    @staticmethod
    def _emit_log(msg: str, level: str = "INFO"):
        try:
            from src.blueprint_app import get_app_main_window

            app = get_app_main_window()
            if app is not None:
                app.log(msg, level)
                return
        except Exception:
            pass
        # 兜底 stdout（加载阶段单测/脚本也能看到）
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] BlueprintStore {level}: {msg}")
