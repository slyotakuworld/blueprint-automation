import json
import os
import shutil
import sys
from typing import Any, Callable, Dict, Optional


# ============================================================
# 工程路径 Provider 注入（由 ProjectManager 在 open_project 时赋值）
# 函数签名: provider(key: str) -> Optional[str]
#   key ∈ {"root", "assets", "cache", "debug"}
#   返回工程目录对应子路径；未开工程 / 不识别 key 时返回 None。
# ============================================================
_project_path_provider: Optional[Callable[[str], Optional[str]]] = None


def set_project_path_provider(provider: Optional[Callable[[str], Optional[str]]]) -> None:
    """供 ProjectManager 调用：打开/关闭工程时注入或卸载路径解析器。"""
    global _project_path_provider
    _project_path_provider = provider


def _project_sub(key: str) -> Optional[str]:
    if not _project_path_provider:
        return None
    try:
        p = _project_path_provider(key)
        return p if (p and os.path.isdir(p)) else None
    except Exception:
        return None


def project_root_dir() -> Optional[str]:
    """当前工程根目录（未开工程 / 运行时未注入则 None）。

    用途：蓝图里的图片资源现在按「相对工程根的路径」存（如 resource/脚本/xxx.png），
    解析时必须知道工程根。运行时 frozen exe 里注册的是解包目录（伪工程根），
    因此这个函数在编辑器和运行时都成立。
    """
    return _project_sub("root")


def get_app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    # 开发模式：app_paths.py 在 src/config/ 下 -> 项目根
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_internal_dir():
    if hasattr(sys, "_MEIPASS"):
        return sys._MEIPASS
    return get_app_dir()


APP_DIR = get_app_dir()
INTERNAL_DIR = get_internal_dir()
CONFIG_DIR = os.path.join(APP_DIR, "config")
USER_CONFIG_FILE = os.path.join(APP_DIR, "config.json")
LOG_FILE = os.path.join(APP_DIR, "bot_log.txt")
# CACHE_DIR / TEMPLATE_CACHE_FILE / TEMPLATE_META_FILE：
#   有工程时用工程目录的 cache/（通过 cache_dir()）
#   未开工程时回退到全局 APP_DIR/cache/
DEFAULT_IMAGES_DIRNAME = "1080p"


def cache_dir() -> str:
    """
    有工程时：<project>/cache/
    未开工程：直接抛 RuntimeError（禁止写入全局 APP_DIR/cache/，避免串工程和污染 src/ 代码目录）
    """
    p = _project_sub("cache")
    if p:
        return p
    raise RuntimeError(
        "[cache_dir] 尚未打开任何 BlueprintAutomation 工程，无法获取缓存目录。"
        "请先新建或打开一个工程后再执行此操作。"
    )


def template_cache_file() -> str:
    return os.path.join(cache_dir(), "template_cache.pkl")


def template_meta_file() -> str:
    return os.path.join(cache_dir(), "template_meta.json")


CURRENT_VERSION = "4.1"


def debug_dir() -> str:
    """
    有工程时：<project>/debug/
    未开工程：直接抛 RuntimeError（禁止写入全局 APP_DIR/debug/，避免串工程和污染 src/ 代码目录）
    """
    p = _project_sub("debug")
    if p:
        return p
    raise RuntimeError(
        "[debug_dir] 尚未打开任何 BlueprintAutomation 工程，无法获取 debug/ 目录。"
        "请先新建或打开一个工程后再运行蓝图。"
    )


def get_images_dir(base_dir):
    profiled_dir = os.path.join(base_dir, "images", DEFAULT_IMAGES_DIRNAME)
    if os.path.isdir(profiled_dir):
        return profiled_dir

    return None


def auto_extract_configs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    old_configs = [
        os.path.join(APP_DIR, "bot_config.json"),
        os.path.join(APP_DIR, "bot-config.json"),
        os.path.join(CONFIG_DIR, "bot-config.json"),
        os.path.join(CONFIG_DIR, "bot_config.json"),
        os.path.join(CONFIG_DIR, "config.json"),
    ]
    for old_path in old_configs:
        if os.path.exists(old_path):
            try:
                if not os.path.exists(USER_CONFIG_FILE):
                    shutil.move(old_path, USER_CONFIG_FILE)
                else:
                    os.remove(old_path)
            except Exception:
                pass


def auto_extract_images(folder_name="images"):
    internal_dir = os.path.join(INTERNAL_DIR, folder_name)
    external_dir = os.path.join(APP_DIR, folder_name)

    if not os.path.isdir(internal_dir):
        print(f"[auto_extract_images] missing internal directory: {internal_dir}")
        return

    try:
        os.makedirs(external_dir, exist_ok=True)
        target_base = os.path.join(external_dir, DEFAULT_IMAGES_DIRNAME)
        os.makedirs(target_base, exist_ok=True)

        for root, _, files in os.walk(internal_dir):
            rel_path = os.path.relpath(root, internal_dir)
            if rel_path == ".":
                rel_path = ""
            if rel_path.startswith(f"{DEFAULT_IMAGES_DIRNAME}{os.sep}") or rel_path == DEFAULT_IMAGES_DIRNAME:
                rel_child = rel_path[len(DEFAULT_IMAGES_DIRNAME):].lstrip("\\/")
            else:
                rel_child = rel_path

            target_root = target_base if not rel_child else os.path.join(target_base, rel_child)
            os.makedirs(target_root, exist_ok=True)

            for file in files:
                src_file = os.path.join(root, file)
                dst_file = os.path.join(target_root, file)
                if not os.path.exists(dst_file):
                    shutil.copy2(src_file, dst_file)
    except Exception as e:
        print(f"[auto_extract_images] extract failed: {e}")


def get_img_path(filename):
    """把蓝图参数里的图片路径解析成真实可用路径。

    解析顺序（编辑器与运行时口径完全一致）：
      1) 绝对路径且存在        → 原样（runtime 载荷还原后就是这种）
      2) <工程根>/相对路径     → resource/<蓝图目录>/xxx.png 这类「跟着蓝图走」的新式资源
      3) <工程根>/templates/x  → 旧式：只存文件名，落在工程 templates/ 下
      4) 全局 images/1080p     → 更老的兼容路径
    """
    rel_name = os.path.normpath(str(filename))
    basename = os.path.basename(rel_name)
    cache = getattr(get_img_path, "_cache", None)
    if cache is None:
        cache = {}
        setattr(get_img_path, "_cache", cache)
    if rel_name in cache:
        return cache[rel_name]

    if os.path.isabs(rel_name) and os.path.exists(rel_name):
        cache[rel_name] = rel_name
        return rel_name

    # 2) 工程根相对路径（新式）
    root = project_root_dir()
    if root:
        cand = os.path.normpath(os.path.join(root, rel_name))
        if os.path.exists(cand):
            cache[rel_name] = cand
            return cand

    # 3) 全局 images/1080p（更老的兼容路径）
    for candidate_name in (rel_name, basename):
        ext_path = os.path.join(APP_DIR, "images", DEFAULT_IMAGES_DIRNAME, candidate_name)
        if os.path.exists(ext_path):
            cache[rel_name] = ext_path
            return ext_path

        int_path = os.path.join(INTERNAL_DIR, "images", DEFAULT_IMAGES_DIRNAME, candidate_name)
        if os.path.exists(int_path):
            cache[rel_name] = int_path
            return int_path

    cache[rel_name] = filename
    return filename


def get_asset_path(*parts):
    asset_path = os.path.join(INTERNAL_DIR, "assets", *parts)
    if os.path.exists(asset_path):
        return asset_path

    dev_asset_path = os.path.join(get_app_dir(), "assets", *parts)
    if os.path.exists(dev_asset_path):
        return dev_asset_path

    return None


def clear_all_project_caches() -> None:
    """
    工程切换后必须调用的统一清理入口。
    所有工程无关的模块级缓存（首次加载后会残留旧工程路径/内容的）
    都必须在本函数里显式清空，避免「串工程」。
    """
    # 1) get_img_path：函数属性的相对路径→绝对路径映射缓存
    #    旧工程和新工程若有同名的 templates/a.png / screenshots/b.jpg
    #    不清缓存的话，命中后返回的永远是第一次加载时的旧工程绝对路径
    try:
        img_cache = getattr(get_img_path, "_cache", None)
        if isinstance(img_cache, dict):
            img_cache.clear()
    except Exception:
        pass
    # 2) 未来新增任何模块级缓存，都在此处统一清理（TemplateMatcher的内存缓存是实例级的，
    #    且 BlueprintRunner 每次运行都会重建，无需在这里清）


def _default_user_config_content() -> Dict[str, Any]:
    """首次启动时写入 config.json 的默认模板（空 recent_projects + 默认热键）。"""
    try:
        from .user_settings import _DEFAULT_SETTINGS
        base = dict(_DEFAULT_SETTINGS)
    except Exception:
        base = {
            "hotkeys": {
                "run_blueprint": "<f5>",
                "pause_blueprint": "<f6>",
                "stop_blueprint": "<f7>",
            },
            "ui": {"theme": "dark", "start_maximized": True},
        }
    base["recent_projects"] = []
    return base


def ensure_user_config_file() -> bool:
    """打包后（或开发模式）启动时调用：若 exe 目录（APP_DIR）下没有 config.json，
    就用默认模板创建一个空的，保证用户设置能正常读写。

    优先级（只拷贝/写入，从不覆盖用户已有的文件）：
      1. USER_CONFIG_FILE 已存在 → 直接 True 不动
      2. INTERNAL_DIR/config/config.default.json 有模板 → 拷一份到 USER_CONFIG_FILE
      3. 否则用代码内置的 _default_user_config_content() 写一份

    返回 True 表示文件已存在或创建成功，False 表示创建失败。
    """
    try:
        if USER_CONFIG_FILE and os.path.isfile(USER_CONFIG_FILE):
            return True
        parent = os.path.dirname(USER_CONFIG_FILE) if USER_CONFIG_FILE else APP_DIR
        if parent:
            os.makedirs(parent, exist_ok=True)
        # 2) 内部模板
        tpl = os.path.join(INTERNAL_DIR, "config", "config.default.json")
        if os.path.isfile(tpl):
            try:
                shutil.copy2(tpl, USER_CONFIG_FILE)
                if os.path.isfile(USER_CONFIG_FILE):
                    return True
            except Exception:
                pass
        # 3) 代码内置默认
        try:
            with open(USER_CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(_default_user_config_content(), f, ensure_ascii=False, indent=2)
            return os.path.isfile(USER_CONFIG_FILE)
        except Exception:
            return False
    except Exception:
        return False
