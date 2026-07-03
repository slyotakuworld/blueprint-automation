import json
import os
import shutil
import time
from typing import Optional

from .models import BlueprintGraph


BACKUP_SUFFIX_TS = ".bp.backup_{ts}.json"


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
                    # 备份文件名：路径 + 基础名 + .bp.backup_时间戳.json
                    backup_path = os.path.join(directory, f"{stem}.bp.backup_{ts}.json")
                    shutil.copy2(file_path, backup_path)
                    # 清理旧备份（按基础名匹配）
                    BlueprintStore._prune_backups(directory, stem, keep=5)
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
                data = json.load(f)
            return BlueprintGraph.from_dict(data)
        except Exception:
            return None

    @staticmethod
    def new_graph(name: str = "Untitled") -> BlueprintGraph:
        from .node_library import create_node
        g = BlueprintGraph(name=name)
        start = create_node("Start", pos=(100, 200))
        g.nodes[start.id] = start
        return g

    @staticmethod
    def default_path(graph: BlueprintGraph, base_dir: str) -> str:
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (graph.name or "Untitled")).strip()
        if not safe:
            safe = "Untitled"
        return os.path.join(base_dir, safe + BlueprintStore.EXT)
