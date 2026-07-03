import json
import os
from typing import Callable, List, Optional

from PySide6.QtCore import QObject, Signal


class ProjectManager(QObject):
    """
    工程管理器（单例）：
      - 每个"工程"是一个独立文件夹，内部结构：
          <project_root>/
            ├── <project_name>.bp.json     # 蓝图文件（节点/连线/配置）
            ├── project_meta.json          # 工程元数据（创建/最近打开时间、工程名、版本）
            ├── screenshots/               # 热键截图/源图
            │   ├── autocap_autosave/      #   目标窗口自动抓帧保存的PNG
            │   └── imported_images/       #   从外部磁盘导入的图片拷贝（缓存）
            ├── templates/                 # 裁剪为模板的小PNG（FindAnyImage/WaitAnyImage用）
            ├── split_elements_<ts>/       # ElementSplitDialog自动生成的碎图目录（每帧切一次一个）
            ├── cache/                     # 模板匹配缓存/其它运行期缓存
            └── debug/                     # 运行期debug截图/标注输出
      - 启动时必须先新建或打开一个工程（否则所有写资源操作都被拦截）。
      - 通过 register_path_provider() 把自身路径解析函数注入给 src.config.app_paths，
        这样 app_paths 下的 screenshot_dir/template_dir 等老代码无需修改就能自动路由到工程目录。
    """

    project_opened = Signal(str)       # project_root
    project_closed = Signal()
    project_changed = Signal()          # 任何工程属性变化（打开/关闭/重命名）

    _instance: Optional["ProjectManager"] = None

    @classmethod
    def instance(cls) -> "ProjectManager":
        if cls._instance is None:
            cls._instance = ProjectManager()
        return cls._instance

    def __init__(self):
        super().__init__()
        self.project_root: Optional[str] = None
        self.project_name: str = ""
        self._meta_path: Optional[str] = None
        self._meta: dict = {}  # 当前工程的 meta 字典缓存（打开时同步磁盘）

    # ============================================================
    # 基础查询
    # ============================================================
    def is_open(self) -> bool:
        return bool(self.project_root) and os.path.isdir(self.project_root)

    def blueprint_path(self) -> Optional[str]:
        """返回本工程的 .bp.json 蓝图文件绝对路径（即第一次创建工程时自动生成的同名默认蓝图路径）"""
        if not self.is_open():
            return None
        return os.path.join(self.project_root, f"{self.project_name}.bp.json")

    # ============================================================
    # 工程 meta.json 读写（集中封装，避免重复 open/close）
    # ============================================================
    def _read_meta(self, force: bool = False) -> dict:
        """读取当前工程的 project_meta.json；命中缓存则直接返回，除非 force=True 强制重读磁盘"""
        if not self.is_open() or not self._meta_path:
            return {}
        if self._meta and not force:
            return self._meta
        try:
            with open(self._meta_path, "r", encoding="utf-8") as f:
                self._meta = json.load(f) or {}
        except Exception:
            self._meta = {}
        return self._meta

    def _write_meta(self, extra_updates: Optional[dict] = None) -> bool:
        """将缓存的 self._meta（+extra_updates）写回磁盘。成功返回 True。"""
        if not self.is_open() or not self._meta_path:
            return False
        try:
            if extra_updates:
                self._meta.update(extra_updates)
            with open(self._meta_path, "w", encoding="utf-8") as f:
                json.dump(self._meta, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    # ============================================================
    # 最近使用的蓝图（写在 project_meta.json 里，工程级隔离）
    # ============================================================
    def get_last_opened_blueprint(self) -> Optional[str]:
        """返回上次打开/保存的 .bp.json 绝对路径；没有则返回 None"""
        if not self.is_open():
            return None
        d = self._read_meta()
        p = d.get("last_opened_blueprint")
        if not isinstance(p, str) or not p:
            return None
        # 存的可能是相对路径，也可能是绝对路径——统一成绝对路径
        if not os.path.isabs(p):
            p = os.path.join(self.project_root, p)
        return os.path.abspath(p)

    def set_last_opened_blueprint(self, path: Optional[str]) -> bool:
        """设置最近使用的蓝图路径；传 None 则清空。写入磁盘成功返回 True。"""
        if not self.is_open() or not self._meta_path:
            return False
        d = self._read_meta()
        if path is None or path == "":
            d.pop("last_opened_blueprint", None)
        else:
            abs_path = os.path.abspath(path)
            # 存相对路径（相对于工程根，工程目录整体搬移也不会失效）
            try:
                rel = os.path.relpath(abs_path, self.project_root)
            except Exception:
                rel = abs_path
            d["last_opened_blueprint"] = rel
        return self._write_meta()

    # ============================================================
    # 工程根目录下的各子目录（按需创建并返回绝对路径）
    # ============================================================
    def _sub(self, *parts: str, create: bool = True) -> Optional[str]:
        if not self.is_open():
            return None
        p = os.path.join(self.project_root, *parts)
        if create:
            os.makedirs(p, exist_ok=True)
        return p

    def screenshot_dir(self) -> Optional[str]:
        return self._sub("screenshots")

    def screenshot_autocap_dir(self) -> Optional[str]:
        return self._sub("screenshots", "autocap_autosave")

    def screenshot_imported_dir(self) -> Optional[str]:
        return self._sub("screenshots", "imported_images")

    def split_root_dir(self) -> Optional[str]:
        """切图产生的 split_elements_* 目录的父目录（即 screenshots/，与之前保持同级语义）"""
        return self._sub("screenshots")

    def template_dir(self) -> Optional[str]:
        return self._sub("templates")

    def cache_dir(self) -> Optional[str]:
        return self._sub("cache")

    def debug_dir(self) -> Optional[str]:
        return self._sub("debug")

    # ============================================================
    # 给 app_paths 用的 provider：返回 路径或 None（None 表示让 app_paths 回退到 APP_DIR 全局路径）
    # ============================================================
    def path_provider(self, key: str) -> Optional[str]:
        """
        供 app_paths 注入回调时调用。
        key ∈ {"screenshot", "template", "cache", "debug"}
        """
        if not self.is_open():
            return None
        if key == "screenshot":
            return self.screenshot_dir()
        if key == "template":
            return self.template_dir()
        if key == "cache":
            return self.cache_dir()
        if key == "debug":
            return self.debug_dir()
        return None

    # ============================================================
    # 新建 / 打开 / 关闭
    # ============================================================
    @staticmethod
    def default_projects_root() -> str:
        """默认把所有工程统一放到 <APP>/projects/ 下"""
        from src.config.app_paths import APP_DIR
        p = os.path.join(APP_DIR, "projects")
        os.makedirs(p, exist_ok=True)
        return p

    @staticmethod
    def is_valid_project_dir(path: str) -> bool:
        """识别目录是否是一个工程（必须有 project_meta.json，且至少有同名 .bp.json 或新工程占位）"""
        if not os.path.isdir(path):
            return False
        meta = os.path.join(path, "project_meta.json")
        if not os.path.isfile(meta):
            return False
        try:
            with open(meta, "r", encoding="utf-8") as f:
                d = json.load(f)
            return bool(d.get("project_name"))
        except Exception:
            return False

    def create_project(self, parent_dir: str, project_name: str) -> Optional[str]:
        """
        新建一个工程。返回工程根目录绝对路径（成功）或 None（失败）。
        同时写 project_meta.json，创建所有子目录，**仅首次创建时自动生成初始空白蓝图（带Start节点）**，
        并把这张同名默认蓝图设为 last_opened_blueprint。
        """
        if not parent_dir or not os.path.isdir(parent_dir):
            return None
        safe = "".join(ch if ch.isalnum() or ch in "-_ " else "_" for ch in (project_name or "")).strip()
        if not safe:
            safe = "Untitled_Project"
        project_root = os.path.join(parent_dir, safe)
        # 避免与已存在目录重名冲突：加 _2 _3 …
        if os.path.exists(project_root):
            i = 2
            while os.path.exists(f"{project_root}_{i}"):
                i += 1
            project_root = f"{project_root}_{i}"
            safe = os.path.basename(project_root)
        try:
            os.makedirs(project_root, exist_ok=True)
        except Exception:
            return None
        # 子目录
        for sub in [
            "screenshots",
            os.path.join("screenshots", "autocap_autosave"),
            os.path.join("screenshots", "imported_images"),
            "templates",
            "cache",
            "debug",
        ]:
            os.makedirs(os.path.join(project_root, sub), exist_ok=True)
        # project_meta.json（仅首次创建时写入初始字段）
        import time
        self._meta_path = os.path.join(project_root, "project_meta.json")
        self._meta = {
            "project_name": safe,
            "version": 1,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            with open(self._meta_path, "w", encoding="utf-8") as f:
                json.dump(self._meta, f, ensure_ascii=False, indent=2)
        except Exception:
            return None
        # 初始蓝图（带Start节点）——**只有首次创建工程时自动生成这一张**
        from src.blueprint.core.graph_store import BlueprintStore
        bp_path = os.path.join(project_root, f"{safe}.bp.json")
        g = BlueprintStore.new_graph(name=safe)
        if not BlueprintStore.save(g, bp_path, make_backup=False):
            return None
        # 打开它（open_project 后 is_open 才会 True，此时才能成功写 meta）
        ok = self.open_project(project_root)
        if not ok:
            return None
        # 把这张首次自动生成的同名蓝图设为 last_opened_blueprint（必须放在 open_project 之后）
        try:
            self.set_last_opened_blueprint(bp_path)
        except Exception:
            pass
        self._add_recent_project(project_root)
        return project_root

    def open_project(self, project_root: str) -> bool:
        """打开工程目录。成功后工程切换到该目录，保存到最近工程列表，发信号。"""
        if not self.is_valid_project_dir(project_root):
            return False
        meta_path = os.path.join(project_root, "project_meta.json")
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            project_name = str(d.get("project_name") or os.path.basename(project_root))
        except Exception:
            return False
        self.close_project(emit=False)
        self.project_root = os.path.abspath(project_root)
        self.project_name = project_name
        self._meta_path = meta_path
        self._meta = d  # 直接缓存读到的 meta
        # 刷新 opened_at（走统一写接口）
        try:
            import time
            self._write_meta({"opened_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        except Exception:
            pass
        self._add_recent_project(self.project_root)
        # 注册 provider
        from src.config import app_paths as _ap
        _ap.set_project_path_provider(self.path_provider)
        self.project_opened.emit(self.project_root)
        self.project_changed.emit()
        return True

    def close_project(self, emit: bool = True) -> None:
        if self.project_root is None:
            return
        self.project_root = None
        self.project_name = ""
        self._meta_path = None
        self._meta = {}  # 清缓存
        # 卸载 provider
        try:
            from src.config import app_paths as _ap
            _ap.set_project_path_provider(None)
        except Exception:
            pass
        if emit:
            self.project_closed.emit()
            self.project_changed.emit()

    # ============================================================
    # 最近工程列表（写到全局 config.json 里）
    # ============================================================
    @staticmethod
    def _recent_projects_file() -> str:
        from src.config.app_paths import USER_CONFIG_FILE
        return USER_CONFIG_FILE

    def recent_projects(self, limit: int = 8) -> List[str]:
        try:
            p = self._recent_projects_file()
            if not os.path.isfile(p):
                return []
            with open(p, "r", encoding="utf-8") as f:
                d = json.load(f)
            lst = list(d.get("recent_projects", []) or [])
            # 只过滤「目录已经不存在」的条目；
            # 不再用 is_valid_project_dir() 过滤——否则 meta 损坏/字段临时为空的工程会被直接隐藏，
            # 用户以为自己的工程丢了（非常困惑）。真实行为：目录仍在就显示，
            # 用户点打开时，再通过 open_project() 里的 is_valid_project_dir 检查弹警告告知损坏，
            # 让用户能明确知道“我的工程还在，只是 meta 坏了，我修一下”，而不是凭空消失。
            lst = [x for x in lst if isinstance(x, str) and os.path.isdir(x)]
            return lst[:limit]
        except Exception:
            return []

    def _add_recent_project(self, project_root: str) -> None:
        try:
            # ============================================================
            # 【永远不要把临时目录下的假工程写进用户最近列表】
            # Smoke 脚本 / 单元测试会在 tempfile.gettempdir() 下创建临时工程，
            # 以前这些会全部塞进 config.json.recent_projects 里污染用户真实记录，
            # 现在直接拦截——临时目录（任何子目录都算）一律不写入最近工程。
            # ============================================================
            import tempfile as _tmp
            _tmp_root = _tmp.gettempdir()
            try:
                rp = os.path.realpath(os.path.abspath(project_root))
                tr = os.path.realpath(_tmp_root)
                if os.path.commonpath([rp, tr]) == tr:
                    return
            except Exception:
                pass
            p = self._recent_projects_file()
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f) if os.path.isfile(p) else {}
            except Exception:
                d = {}
            lst = list(d.get("recent_projects", []) or [])
            # 去重+置顶
            root = os.path.abspath(project_root)
            if root in lst:
                lst.remove(root)
            lst.insert(0, root)
            lst = lst[:16]
            d["recent_projects"] = lst
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
