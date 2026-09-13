import json
import os
from typing import List, Optional

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
            ├── templates/                 # 裁剪为模板的小PNG（FindAnyImage 用）
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

    def create_project(self, parent_dir: str, project_name: str, auto_open: bool = True) -> Optional[str]:
        """
        新建一个工程。返回工程根目录绝对路径（成功）或 None（失败）。
        同时写 project_meta.json，创建所有子目录，**仅首次创建时自动生成初始空白蓝图（带Start节点）**，
        并把这张同名默认蓝图设为 last_opened_blueprint。

        参数 auto_open：
          - True  (默认)：创建完成后自动调 open_project 切到新工程，is_open=True，后续可直接操作。
          - False：只创建磁盘结构 + 写好 meta + 生成初始蓝图，**绝不改动 ProjectManager 的状态**。
                  用于『切工程前要先 confirm_discard 保存旧工程修改』的场景（时序严格场景），
                  避免 create_project 内部提前把 project_root / _meta 切到新工程，导致后续
                  set_last_opened_blueprint 把旧工程的蓝图路径误写进新工程。
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
        meta_path = os.path.join(project_root, "project_meta.json")
        meta_dict = {
            "project_name": safe,
            "version": 1,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "opened_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta_dict, f, ensure_ascii=False, indent=2)
        except Exception:
            return None
        # 初始蓝图（带Start节点）——**只有首次创建工程时自动生成这一张**
        from src.blueprint.core.graph_store import BlueprintStore
        bp_path = os.path.join(project_root, f"{safe}.bp.json")
        g = BlueprintStore.new_graph(name=safe)
        if not BlueprintStore.save(g, bp_path, make_backup=False):
            return None
        # 把这张首次自动生成的同名蓝图设为 last_opened_blueprint（直接写 meta 文件，无需 is_open 为 True）
        # 注意：这里不能用 set_last_opened_blueprint()，因为 auto_open=False 时 is_open 会返回 False，
        # set_last_opened_blueprint 会直接拒绝。改用内部函数 _write_meta_to_file，接受任意 meta_path。
        self._write_meta_to_file(meta_path, {"last_opened_blueprint": f"{safe}.bp.json"})

        if auto_open:
            # 打开它（open_project 后 is_open 才会 True，此时才能成功写 meta）
            ok = self.open_project(project_root)
            if not ok:
                return None
        # add_recent_project 不管 auto_open 都要做，下次打开最近工程列表里就能看到
        self._add_recent_project(project_root)
        return project_root

    @staticmethod
    def _write_meta_to_file(meta_path: str, extra_updates: Optional[dict] = None) -> bool:
        """「无状态」写 meta 文件：不依赖 is_open()，只接受任意 meta_path 并更新字段。

        只用于 create_project(auto_open=False) 这种「工程还没正式打开，但需要先把
        last_opened_blueprint 写进 meta 磁盘文件」的特殊场景。
        """
        if not meta_path or not os.path.isfile(meta_path):
            return False
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                d = json.load(f) or {}
            if extra_updates:
                d.update(extra_updates)
            tmp = meta_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            os.replace(tmp, meta_path)
            return True
        except Exception:
            return False

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
            seen = set()
            norm_lst: List[str] = []
            for x in lst:
                if not isinstance(x, str):
                    continue
                try:
                    nx = os.path.normpath(os.path.abspath(x))
                except Exception:
                    continue
                if not os.path.isdir(nx):
                    continue
                # 去重（同一工程绝对路径只保留首次出现的那条）
                try:
                    key = nx.lower() if os.name == "nt" else nx
                except Exception:
                    key = nx
                if key in seen:
                    continue
                seen.add(key)
                norm_lst.append(nx)
            return norm_lst[:limit]
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
            raw_lst = list(d.get("recent_projects", []) or [])

            # 目标路径规范化（统一格式：绝对路径 + normpath，避免反斜杠/多余点号差异）
            try:
                root_norm = os.path.normpath(os.path.abspath(project_root))
            except Exception:
                return
            try:
                root_key = root_norm.lower() if os.name == "nt" else root_norm
            except Exception:
                root_key = root_norm

            # 第一步：遍历现有列表，做「逐个规范化 + 去重」
            # （老 config 里可能混了单反斜杠/双反斜杠/大小写等多种写法）
            dedup_keys = set()
            clean_lst: List[str] = []
            for x in raw_lst:
                if not isinstance(x, str):
                    continue
                try:
                    nx = os.path.normpath(os.path.abspath(x))
                except Exception:
                    continue
                try:
                    k = nx.lower() if os.name == "nt" else nx
                except Exception:
                    k = nx
                if k in dedup_keys:
                    # 同一工程重复出现 → 只保留第一次（不影响排序）
                    continue
                # 如果与新加入的目标相同 → 跳过（后面统一 insert 到最前做置顶）
                if k == root_key:
                    continue
                dedup_keys.add(k)
                clean_lst.append(nx)

            # 第二步：把「目标工程」置顶插入最前
            clean_lst.insert(0, root_norm)
            clean_lst = clean_lst[:16]

            d["recent_projects"] = clean_lst
            os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def remove_recent_project(self, project_root: str) -> bool:
        """只从『最近工程列表』里移除一条记录，不碰磁盘上的工程目录。
        返回 True 表示 config 写回成功（或记录根本就不在，也算成功）。
        """
        try:
            p = self._recent_projects_file()
            try:
                with open(p, "r", encoding="utf-8") as f:
                    d = json.load(f) if os.path.isfile(p) else {}
            except Exception:
                d = {}
            raw_lst = list(d.get("recent_projects", []) or [])

            # 目标路径规范化
            try:
                target_norm = os.path.normpath(os.path.abspath(project_root))
            except Exception:
                target_norm = ""
            try:
                target_key = target_norm.lower() if os.name == "nt" else target_norm
            except Exception:
                target_key = target_norm

            # 遍历现有列表：逐个规范化 + 移除匹配项（所有变体都移除）+ 顺手去重
            seen = set()
            clean_lst: List[str] = []
            changed = False
            for x in raw_lst:
                if not isinstance(x, str):
                    changed = True
                    continue
                try:
                    nx = os.path.normpath(os.path.abspath(x))
                except Exception:
                    changed = True
                    continue
                try:
                    k = nx.lower() if os.name == "nt" else nx
                except Exception:
                    k = nx
                if k == target_key:
                    # 命中 → 移除
                    changed = True
                    continue
                if k in seen:
                    # 重复 → 移除
                    changed = True
                    continue
                seen.add(k)
                clean_lst.append(nx)

            if changed or clean_lst != raw_lst:
                d["recent_projects"] = clean_lst
                os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def delete_project(self, project_root: str, permanent: bool = False) -> tuple[bool, str]:
        """【单一删除入口】从磁盘删除一个工程目录，同时从最近工程列表里移除。

        参数：
          project_root: 工程根目录的绝对路径（必须 is_valid_project_dir() 才允许）。
          permanent:    True  → shutil.rmtree 永久删除（不可恢复）。
                        False → 先尝试移动到回收站；回收站不可用则抛错，由调用方决定是否升级为 permanent。

        返回：
          (ok: bool, reason: str)
            ok=True   → 删除完成，reason 为"移动到回收站"或"已永久删除"的可读描述。
            ok=False  → 删除被拒绝或失败，reason 是具体原因（含 "TRASH_UNAVAILABLE" 标识供调用方识别）。

        安全保护（任一命中都会直接拒绝，返回 False）：
          - 目标不是合法工程目录
          - 目标工程当前正在被打开（pm.is_open() 且 project_root == 当前根目录）
          - 目标是 default_projects_root() 的父级或根级（防删除整个 projects 文件夹）
        """
        if not isinstance(project_root, str) or not project_root:
            return False, "空路径"
        target = os.path.abspath(project_root)
        # 安全 1：禁止删除当前正打开的工程
        if self.is_open() and os.path.abspath(self.project_root) == target:
            return False, "该工程正在使用中，请先关闭或切换到其他工程后再删除。"
        # 安全 2：必须是合法工程（有 project_meta.json + project_name），避免删错用户目录
        if not ProjectManager.is_valid_project_dir(target):
            return False, (
                f"{target}\n不是合法的 BlueprintAutomation 工程目录"
                f"（缺少 project_meta.json 或 project_name）。"
            )
        # 安全 3：不能是 default_projects_root 本身（防止误删整个 projects/）
        try:
            dpr = os.path.abspath(ProjectManager.default_projects_root())
            if target == dpr:
                return False, "禁止删除全局默认工程父目录（projects/）。"
        except Exception:
            pass
        # 真正执行删除（只做一次，绝不重复）
        removed_by_trash = False
        err = ""
        if not permanent:
            try:
                import send2trash  # type: ignore
                send2trash.send2trash(target)
                removed_by_trash = True
            except Exception as e:
                return False, f"TRASH_UNAVAILABLE: 移动到回收站失败：{e}"
        if permanent and not removed_by_trash:
            import shutil as _sh
            try:
                _sh.rmtree(target)
            except Exception as e:
                return False, f"永久删除失败（shutil.rmtree）：{e}"
        # 最后：从最近工程列表里移除（仅一次）
        self.remove_recent_project(target)
        if removed_by_trash:
            return True, f"工程已移动到回收站：{target}"
        return True, f"工程已永久删除：{target}"
