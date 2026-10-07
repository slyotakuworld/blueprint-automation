# -*- coding: utf-8 -*-
"""崩溃黑匣子（2026-10-07 加入）：历次「闪退」在 Windows 事件查看器里查不到任何
崩溃记录，死因无法回溯。本模块做两件事：

1. **faulthandler**：注册原生致命错误（段错误 / abort / 堆异常…）的转储器，
   崩溃瞬间把所有线程的 Python 调用栈写进日志文件 —— 原生崩溃不再"无声消失"。
2. **退出轨迹**：记录「事件循环返回」「窗口被 close」「主窗口被 hide」等关键节点，
   区分三种死法：
   - 日志里有 faulthandler 栈转储  → 原生崩溃（把文件发给开发者看栈）
   - 日志里有「closeEvent / 事件循环退出」 → 窗口被正常关闭路径退出（查是谁关的）
   - 日志里什么都没有、戛然而止     → 进程被外部直接终止（杀毒软件行为防护 /
     任务管理器强杀等），代码层无死因

日志位置：用户配置目录下 crash_blackbox.log（与 config.json 同目录，与工程无关）。
"""
import atexit
import faulthandler
import os
import sys
import time
import traceback

_FILE = None          # 持有句柄，faulthandler 需要它保持打开
_PATH = ""


def _log_path() -> str:
    try:
        from src.config.app_paths import USER_CONFIG_FILE
        return os.path.join(os.path.dirname(USER_CONFIG_FILE), "crash_blackbox.log")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "crash_blackbox.log")


def enable() -> str:
    """在 main() 最开头调用：打开日志 + 启用 faulthandler + 注册 atexit。"""
    global _FILE, _PATH
    if _FILE is not None:
        return _PATH
    _PATH = _log_path()
    try:
        os.makedirs(os.path.dirname(_PATH), exist_ok=True)
        # append 模式：跨多次启动保留历史（文件自己会滚动吗？——不会，但每行带时间戳+pid，
        # 且该文件只记关键事件，体量很小；排查完可在设置里删）
        _FILE = open(_PATH, "a", encoding="utf-8", buffering=1)  # 行缓冲
        try:
            faulthandler.enable(file=_FILE)
        except Exception:
            pass
        atexit.register(_atexit)
        log(f"[BOOT] 进程启动 pid={os.getpid()} python={sys.version.split()[0]} "
            f"argv={sys.argv[:3]}")
    except Exception:
        _FILE = None
    return _PATH


def log(msg: str):
    """记一条关键事件（行缓冲 + 强制 flush，崩溃前的那行必须已在盘上）。"""
    if _FILE is None:
        return
    try:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        _FILE.write(f"[{ts}] [pid={os.getpid()}] {msg}\n")
        _FILE.flush()
        try:
            os.fsync(_FILE.fileno())
        except Exception:
            pass
    except Exception:
        pass


def log_exception(where: str, exc: BaseException):
    """把异常连栈写进黑匣子（比 cmd traceback 可靠——cmd 可能被关/滚走）。"""
    log(f"[EXC] {where}: {type(exc).__name__}: {exc}")
    if _FILE is None:
        return
    try:
        tb = traceback.format_exc()
        _FILE.write(tb)
        _FILE.flush()
    except Exception:
        pass


def _atexit():
    log("[EXIT] 解释器正常退出（atexit）")
    try:
        if _FILE is not None:
            _FILE.close()
    except Exception:
        pass
