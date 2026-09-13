"""
用户持久化配置：全局热键/未来其他设置。
负责读写 + 热键格式规范化 + 两种格式互转：
  - pynput 格式:  "<ctrl>+<shift>+<f8>"   （注册热键用）
  - Qt 格式:      "Ctrl+Shift+F8"          （ShortcutRecorder 显示/QKeySequence 用）
"""
import json
import os
import re
from typing import Any, Dict, Optional

from .app_paths import USER_CONFIG_FILE

# ============================================================
# 快捷键元信息 + 默认值：
# ⚠️ 不要让「全局热键」之间冲突；也尽量避开常见游戏/IDE组合：
#   - 避免 Ctrl+Shift+A(微信/QQ截图)、Ctrl+C/V/Z/X(编辑)、F11(全屏)、F5(Firefox刷新/调试继续)等
#   - 「全局热键」用 GlobalHotkeyManager 注册 → 游戏前台也能按到（跨窗口生效）
#   - 「局部快捷键」用 QAction.setShortcut → 只有主窗口激活时生效（不抢其它 app）
# ============================================================

# ---------- 默认热键（pynput 格式）----------
# 兜底值：只在「读取/规范化一个尚未注册的 key」时用到，正常路径不会走到
DEFAULT_HOTKEY_FALLBACK: str = "<f8>"

# 全部快捷键：有序注册 → 用于设置页面逐行显示 / 主窗口统一绑定
# 结构: ( key_id, 中文说明, 默认pynput值, scope: "global"|"local" )
# 【2026-07-03 按用户最新要求：
#   1) 标准快捷键只保留 4 个硬编码：Ctrl+S 保存 / Ctrl+Z 撤销 / Ctrl+C 复制 / Ctrl+V 粘贴
#      （其它 Ctrl+N / O / Shift+S / 0 / 1 等仍然硬编码可用，但不在设置界面展示了）
#   2) 全局热键只保留 3 项：F5运行 / F6暂停 / F7停止
#      → 已删除：✂ 切分UI元素独立快捷键(Ctrl+Shift+X) / ⚠ 紧急杀进程(F6原绑定)
#      → 2026-09-13 再删：F8「截屏 + 打开切分」——切图工具已独立到
#        G:\work\ui_element_splitter（GUI + 命令行），本项目不再内嵌切图界面
#   3) 设置界面「🎛 通用」改为开发者介绍文案。】
SHORTCUT_REGISTRY: tuple = (
    # ---- 全局热键（跨窗口，游戏前台也能按到）—— 顺序=优先级（冲突修复时越靠前越优先保留） ----
    ("run_blueprint",       "运行蓝图",                     "<f5>",                 "global"),
    ("pause_blueprint",     "暂停 / 继续蓝图",               "<f6>",                "global"),
    ("stop_blueprint",      "停止蓝图",                     "<f7>",                 "global"),
)
# key -> 默认值 快速查表
_DEFAULT_BY_KEY: Dict[str, str] = {row[0]: row[2] for row in SHORTCUT_REGISTRY}
# key -> scope 快速查表
_SCOPE_BY_KEY: Dict[str, str] = {row[0]: row[3] for row in SHORTCUT_REGISTRY}
# key -> label 快速查表
_LABEL_BY_KEY: Dict[str, str] = {row[0]: row[1] for row in SHORTCUT_REGISTRY}


# 方便外部判断"这个快捷键要不要注册到GlobalHotkeyManager"
def is_global_shortcut(key_id: str) -> bool:
    return _SCOPE_BY_KEY.get(key_id) == "global"


def shortcut_label(key_id: str) -> str:
    return _LABEL_BY_KEY.get(key_id, key_id)


def all_shortcut_keys() -> list:
    return [row[0] for row in SHORTCUT_REGISTRY]


_DEFAULT_SETTINGS: Dict[str, Any] = {
    "hotkeys": {
        k: v for k, v in _DEFAULT_BY_KEY.items()
    },
    "ui": {
        "theme": "dark",
    },
}


# ============================================================
# 读写
# ============================================================
def read_user_settings() -> Dict[str, Any]:
    """读取用户 settings，缺失字段自动补默认值（不破坏用户文件里已有的其他键）。

    ⚠️ 重要：返回的 settings 对象里**不包含 recent_projects 字段**。
    recent_projects 由 ProjectManager 独立维护（直接读写 config.json），
    不应该被缓存在任何内存 settings 对象里——否则旧快照会覆盖 PM 刚写入的最新值。
    （内部会执行一次 recent_projects 的清理操作，但清理完不把这个字段带给调用方。）
    """
    data: Dict[str, Any] = {}
    path = USER_CONFIG_FILE
    try:
        if path and os.path.isfile(path):
            loaded_raw: Any = None
            try:
                with open(path, "r", encoding="utf-8") as f:
                    loaded_raw = json.load(f)
            except (json.JSONDecodeError, OSError):
                try:
                    with open(path, "r", encoding="gbk", errors="ignore") as f:
                        loaded_raw = json.load(f)
                except Exception:
                    loaded_raw = None

            if isinstance(loaded_raw, dict):
                data = loaded_raw
    except Exception:
        data = {}

    _merge_defaults(data, _DEFAULT_SETTINGS)

    # ---------------- 启动时顺便清理 recent_projects（清理完直接写磁盘，不把字段带进返回值） ----------------
    changed_for_force_clean = False
    cleaned_recent = None
    if isinstance(data.get("recent_projects"), list):
        import tempfile as _tmp
        _tmp_root = _tmp.gettempdir()
        clean_lst = []
        for x in data["recent_projects"]:
            if not isinstance(x, str):
                continue
            try:
                rp = os.path.realpath(os.path.abspath(x))
                tr = os.path.realpath(_tmp_root)
                if os.path.commonpath([rp, tr]) == tr:
                    changed_for_force_clean = True
                    continue
            except Exception:
                pass
            if not os.path.isdir(x):
                changed_for_force_clean = True
                continue
            clean_lst.append(x)
        if clean_lst != data["recent_projects"]:
            changed_for_force_clean = True
        cleaned_recent = clean_lst
        # 从内存 data 里剔除（不返回给调用方缓存）
        data.pop("recent_projects", None)
        # 有变化就写磁盘（这里传 include_recent=True，因为 cleaned_recent 就是刚读磁盘+清理的最新版本）
        if changed_for_force_clean:
            try:
                data_with_recent = dict(data)
                data_with_recent["recent_projects"] = cleaned_recent
                _save_user_settings_impl(data_with_recent, preserve_recent=False)
            except Exception:
                pass
    else:
        # 没有 recent_projects 字段就确保 data 里也不带
        data.pop("recent_projects", None)
    return data


def save_user_settings(settings: Dict[str, Any]) -> bool:
    """保存用户 settings 到 USER_CONFIG_FILE。失败返回 False。

    ⚠️ 默认 preserve_recent=True：写入前会**从磁盘读回当前的 recent_projects**，
    避免调用方用内存里的旧 settings 对象（不含/含旧 recent_projects）覆盖掉
    ProjectManager 刚刚写入的最新最近工程列表。

    如果你明确要覆盖 recent_projects（比如 read_user_settings 内部清理完后保存），
    请调用内部函数 _save_user_settings_impl(..., preserve_recent=False)。
    """
    return _save_user_settings_impl(settings, preserve_recent=True)


def _save_user_settings_impl(settings: Dict[str, Any], preserve_recent: bool) -> bool:
    """save_user_settings 的内部实现。

    参数 preserve_recent：
      - True  (对外 save_user_settings 默认)：写入前先读磁盘上的 recent_projects，
        把磁盘上的值合并进 settings，避免内存旧 settings 覆盖 ProjectManager 维护的最近工程。
      - False (内部使用)：直接用 settings 里的 recent_projects 写磁盘，
        只允许 read_user_settings 启动清理流程调用（此时 settings.recent_projects 是最新值）。
    """
    try:
        path = USER_CONFIG_FILE
        if not path:
            return False
        # ============================================================
        # 【关键保护】recent_projects 字段是 ProjectManager 独立维护的，不要相信
        # 内存 settings 对象里的值（它是启动时的旧快照）。
        # 写入前先从磁盘读回当前 config.json 的 recent_projects 原样保留。
        # ============================================================
        final_settings = dict(settings) if isinstance(settings, dict) else {}
        if preserve_recent:
            try:
                if os.path.isfile(path):
                    with open(path, "r", encoding="utf-8") as f:
                        disk_raw = json.load(f)
                    if isinstance(disk_raw, dict) and "recent_projects" in disk_raw:
                        final_settings["recent_projects"] = disk_raw["recent_projects"]
            except Exception:
                # 读磁盘失败就不合并（至少不要把已有 recent_projects 弄丢）
                pass
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(final_settings, f, ensure_ascii=False, indent=2, sort_keys=False)
        try:
            os.replace(tmp, path)
        except OSError:
            # Windows 上 replace 偶尔因句柄/杀毒失败，直接 rename 兜底
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception:
                pass
            os.rename(tmp, path)
        return True
    except Exception as e:
        print(f"[user_settings] save failed: {e}")
        return False


def get_shortcut(key_id: str, settings: Optional[Dict[str, Any]] = None) -> str:
    """通用：统一读快捷键（取不到时用 SHORTCUT_REGISTRY 对应默认值，并过一次 normalize_pynput_hotkey 保证格式一致。"""
    s = settings if settings is not None else read_user_settings()
    try:
        v = s["hotkeys"][key_id]
    except Exception:
        v = None
    default = _DEFAULT_BY_KEY.get(key_id) or DEFAULT_HOTKEY_FALLBACK
    hk = v if isinstance(v, str) and v.strip() else default
    return normalize_pynput_hotkey(hk)


def set_shortcut(settings: Dict[str, Any], key_id: str, pynput_hotkey: str) -> str:
    """统一设置快捷键并规范化，返回规范化后的 pynput 热键字符串。"""
    default = _DEFAULT_BY_KEY.get(key_id) or DEFAULT_HOTKEY_FALLBACK
    if not (isinstance(pynput_hotkey, str) and pynput_hotkey.strip()):
        pynput_hotkey = default
    hk = normalize_pynput_hotkey(pynput_hotkey)
    settings.setdefault("hotkeys", {})[key_id] = hk
    return hk


# ============================================================
# 合并默认值（递归，不覆盖已有键）
# ============================================================
def _merge_defaults(data: Dict[str, Any], defaults: Dict[str, Any]):
    for k, v in defaults.items():
        if k not in data:
            data[k] = v if not isinstance(v, dict) else dict(v)
        elif isinstance(v, dict) and isinstance(data[k], dict):
            _merge_defaults(data[k], v)


# ============================================================
# 规范化 + 互转
# ============================================================

# 修饰键别名映射（小写后比较）→ pynput 标准 token
_MOD_MAP: Dict[str, str] = {
    # control 家族
    "ctrl": "<ctrl>", "control": "<ctrl>", "ctl": "<ctrl>", "<control>": "<ctrl>",
    # win 家族
    "win": "<cmd>", "windows": "<cmd>", "super": "<cmd>", "meta": "<cmd>",
    "cmd": "<cmd>", "command": "<cmd>", "<win>": "<cmd>", "<windows>": "<cmd>",
    "<super>": "<cmd>", "<meta>": "<cmd>", "<command>": "<cmd>",
    # alt 家族
    "alt": "<alt>", "option": "<alt>", "<option>": "<alt>",
    # shift 家族
    "shift": "<shift>", "<shft>": "<shift>",
}

# Qt -> 别名 -> token
_QT_MOD_MAP: Dict[str, str] = {
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt",
    "shift": "shift",
    "meta": "meta", "super": "meta", "win": "meta", "windows": "meta", "cmd": "meta", "command": "meta",
}

_SPLIT_RE = re.compile(r"[+ \u002b]+")  # 只按 + / 空格 拆分（不要含下划线！否则 print_screen 被拆坏）

# 功能键名集合（pynput/Qt 都需要尖括号/大写）
_SPECIAL_KEYS: set = {
    "f1","f2","f3","f4","f5","f6","f7","f8","f9","f10","f11","f12","f13","f14","f15","f16","f17","f18","f19","f20",
    "esc","escape","tab","space","enter","return","backspace","delete","del","insert","home","end",
    "page_up","page_down","up","down","left","right","pause","break","print_screen","printscreen","prtsc","prtscr",
    "caps_lock","num_lock","scroll_lock","scrolllock",
    "media_play_pause","media_next","media_previous","media_volume_up","media_volume_down","media_volume_mute",
}


def normalize_pynput_hotkey(hk: str) -> str:
    """
    规范化 pynput 热键字符串：
      例:  "Ctrl+F8" / "ctrl + shift + A" / "<Ctrl><Shift>F8"
        → "<ctrl>+<f8>" / "<ctrl>+<shift>+a"
    规则：
      - 修饰键（ctrl/shift/alt/cmd）统一加 <> 并按固定顺序（ctrl→shift→alt→cmd）
      - 单字母/数字：不加 <> （pynput要求"<ctrl>+a"而不是"<ctrl>+<a>"）
      - 功能键（F1..F12 / print_screen / space 等）加 <>
    """
    if not isinstance(hk, str) or not hk.strip():
        return DEFAULT_HOTKEY_FALLBACK
    raw_in = hk.strip()

    # ---- 阶段1：把 "<xxx><yyy>F8" 这种无分隔符形式 先切 成 "<xxx>+<yyy>+F8" ----
    buf: list[str] = []
    i, n = 0, len(raw_in)
    while i < n:
        ch = raw_in[i]
        if ch == "<":
            j = raw_in.find(">", i)
            if j >= 0:
                buf.append(raw_in[i:j + 1])
                i = j + 1
                continue
        # 非 <> 块：读到下一个 < 或结尾
        j = i
        while j < n and raw_in[j] != "<":
            j += 1
        seg = raw_in[i:j]
        for sub in _SPLIT_RE.split(seg):
            if sub:
                buf.append(sub)
        i = j

    # ---- 阶段2：识别 修饰键 vs 最终键 ----
    mods: list[str] = []
    key_tok = ""
    for t in buf:
        # 去掉 <>
        tt = t.replace("<", "").replace(">", "").lower()
        if not tt:
            continue
        # 是否修饰键？
        mapped = _MOD_MAP.get(tt)
        if mapped is None:
            # 试试别名匹配：control→<ctrl> 这种（_MOD_MAP里有 "<control>" -> "<ctrl>"）——但我们replace过了，
            # 再补一次映射（去掉<后比较）
            for k_alias, v_mod in _MOD_MAP.items():
                clean = k_alias.replace("<", "").replace(">", "").lower()
                if clean == tt:
                    mapped = v_mod
                    break
        if mapped is not None:
            if mapped not in mods:
                mods.append(mapped)
        else:
            key_tok = tt

    if not key_tok:
        return DEFAULT_HOTKEY_FALLBACK

    # ---- 阶段3：最后一个键 是否需要 <> ----
    # 长度1=字母数字 -> 不加
    # 特殊键 -> 加<>
    # 其他（如 print_screen）-> 加<>
    need_bracket = (len(key_tok) > 1) or key_tok in _SPECIAL_KEYS
    if need_bracket:
        key_part = f"<{key_tok}>"
    else:
        key_part = key_tok

    # 修饰键排序 ctrl→shift→alt→cmd
    order = {"<ctrl>": 1, "<shift>": 2, "<alt>": 3, "<cmd>": 4}
    mods_sorted = sorted(mods, key=lambda m: order.get(m, 99))
    parts = mods_sorted + [key_part]
    return "+".join(parts)


def pynput_to_qkeysequence(hk: str) -> str:
    """
    pynput "<ctrl>+<shift>+<f8>" / "<ctrl>+a" → Qt "Ctrl+Shift+F8" / "Ctrl+A"
    """
    norm = normalize_pynput_hotkey(hk)
    # tokens: <ctrl>, <shift>, <f8>  或  <ctrl>, a
    tokens = [t for t in norm.split("+") if t]
    qt_mods: list[str] = []
    qt_key = ""
    for tok in tokens:
        t = tok.replace("<", "").replace(">", "").lower()
        if t in ("ctrl", "control"):
            qt_mods.append("Ctrl")
        elif t in ("shift",):
            qt_mods.append("Shift")
        elif t in ("alt", "option"):
            qt_mods.append("Alt")
        elif t in ("cmd", "win", "windows", "super", "meta", "command"):
            qt_mods.append("Meta")
        else:
            # 字母（长度1）→ 大写；功能键 → 首字母大写其余小写（f1->F1, print_screen->Print_Screen）
            if len(t) == 1:
                qt_key = t.upper()
            else:
                # 按_分段每个单词首字母大写
                pieces = t.split("_")
                qt_key = "_".join(
                    (p[0].upper() + p[1:]) if p else "" for p in pieces
                )
    order = {"Ctrl": 1, "Shift": 2, "Alt": 3, "Meta": 4}
    qt_mods_sorted = sorted(qt_mods, key=lambda m: order.get(m, 99))
    all_parts = qt_mods_sorted + ([qt_key] if qt_key else [])
    return "+".join(all_parts) if all_parts else "(无)"


def qkeysequence_to_pynput(qks_str: str) -> Optional[str]:
    """
    Qt "Ctrl+Shift+F8" / "Alt + A" → pynput "<ctrl>+<shift>+<f8>" / "<alt>+a"
    无法解析时返回 None。
    """
    if not isinstance(qks_str, str) or not qks_str.strip():
        return None
    raw = qks_str.strip()
    if raw in ("(无)", "", "none", "(未设置)"):
        return None

    # token 拆分：考虑"<Ctrl><Shift>A"这种不带+但带<>的场景
    buf: list[str] = []
    i, n = 0, len(raw)
    while i < n:
        ch = raw[i]
        if ch == "<":
            j = raw.find(">", i)
            if j >= 0:
                buf.append(raw[i:j + 1])
                i = j + 1
                continue
        j = i
        while j < n and raw[j] != "<":
            j += 1
        seg = raw[i:j]
        for sub in _SPLIT_RE.split(seg):
            if sub:
                buf.append(sub)
        i = j

    mods: list[str] = []
    key = ""
    for t in buf:
        tt = t.replace("<", "").replace(">", "").lower()
        if not tt:
            continue
        m = _QT_MOD_MAP.get(tt)
        if m == "ctrl":
            if "<ctrl>" not in mods:
                mods.append("<ctrl>")
        elif m == "shift":
            if "<shift>" not in mods:
                mods.append("<shift>")
        elif m == "alt":
            if "<alt>" not in mods:
                mods.append("<alt>")
        elif m == "meta":
            if "<cmd>" not in mods:
                mods.append("<cmd>")
        else:
            key = tt
    if not key:
        return None

    # 最后键是否需要 <>
    need_bracket = (len(key) > 1) or key in _SPECIAL_KEYS
    if need_bracket:
        key_part = f"<{key}>"
    else:
        key_part = key

    order = {"<ctrl>": 1, "<shift>": 2, "<alt>": 3, "<cmd>": 4}
    mods_sorted = sorted(mods, key=lambda m: order.get(m, 99))
    parts = mods_sorted + [key_part]
    return "+".join(parts)

