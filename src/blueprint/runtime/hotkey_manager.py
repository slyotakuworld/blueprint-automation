"""独立运行时的全局热键（pynput）。

热键约定与编辑器保持一致（编辑器默认：F5 运行 / F6 暂停继续 / F7 停止），
这样「编辑器里点▶运行」和「导出的 EXE」操作手感一致：

    F5   开始 / 重新开始
    F6   暂停 / 继续
    F7   优雅停止（跑完当前节点后退出）
    F12  紧急退出（直接结束进程，鼠标乱飞时救命）

注意：pynput 的回调运行在**监听线程**，本模块只负责「触发通知」，
绝不做任何 UI 操作；调用方需要通过 Qt Signal 把处理切回主线程。
"""

import os
import sys
import threading
from typing import Callable, Optional, Sequence, Tuple

try:
    from pynput import keyboard

    _HAS_PYNPUT = True
except Exception:
    _HAS_PYNPUT = False


KILL_ID = "kill"

# (hotkey_id, pynput 键串, 展示文案)
DEFAULT_HOTKEYS: Tuple[Tuple[str, str, str], ...] = (
    ("start", "<f5>", "F5 开始 / 重新开始"),
    ("pause", "<f6>", "F6 暂停 / 继续"),
    ("stop", "<f7>", "F7 停止"),
    (KILL_ID, "<f12>", "F12 紧急退出"),
)


class HotkeyManager:
    """注册一组全局热键，触发时把 hotkey_id 交给 dispatch 处理。

    :param dispatch: callable(hotkey_id) -> None；在监听线程被调用，
                     实现方需自行切回主线程。
    :param log_fn:   callable(msg, level) -> None；同样在监听线程被调用。
    :param hotkeys:  (hotkey_id, pynput键串, 文案) 序列，默认见 DEFAULT_HOTKEYS。
    """

    def __init__(
        self,
        dispatch: Optional[Callable[[str], None]] = None,
        log_fn: Optional[Callable[[str, str], None]] = None,
        hotkeys: Sequence[Tuple[str, str, str]] = DEFAULT_HOTKEYS,
    ):
        self._dispatch = dispatch
        self._log = log_fn or (lambda m, l="INFO": None)
        self._hotkeys = tuple(hotkeys)
        self._listener = None
        self._lock = threading.Lock()
        self._active = False

    # ---------------- 展示 ----------------
    @property
    def labels(self):
        return [label for _, _, label in self._hotkeys]

    # ---------------- 紧急退出（内部直接处理，不走 UI） ----------------
    def _kill(self):
        try:
            self._log("[Hotkey] F12 紧急退出：直接结束进程", "ERROR")
        except Exception:
            pass
        try:
            os._exit(1)
        except Exception:
            sys.exit(1)

    def _make_cb(self, hotkey_id: str, label: str):
        def cb():
            if hotkey_id == KILL_ID:
                self._kill()
                return
            try:
                self._log(f"[Hotkey] {label}", "INFO")
            except Exception:
                pass
            if self._dispatch is None:
                return
            try:
                self._dispatch(hotkey_id)
            except Exception as e:
                try:
                    self._log(f"[Hotkey] {label} 处理失败：{e}", "WARN")
                except Exception:
                    pass

        return cb

    # ---------------- 生命周期 ----------------
    def start(self) -> bool:
        with self._lock:
            if self._active:
                return True
            if not _HAS_PYNPUT:
                self._log("[Hotkey] pynput 不可用，全局热键失效。", "WARN")
                return False
            try:
                mapping = {keys: self._make_cb(hid, lbl) for hid, keys, lbl in self._hotkeys}
                self._listener = keyboard.GlobalHotKeys(mapping)
                self._listener.daemon = True
                self._listener.start()
                self._active = True
                self._log("[Hotkey] 已注册：" + " | ".join(self.labels), "INFO")
                return True
            except Exception as e:
                self._listener = None
                self._active = False
                self._log(f"[Hotkey] 注册失败：{e}", "WARN")
                return False

    def stop(self):
        with self._lock:
            if not self._active:
                return
            try:
                if self._listener is not None:
                    self._listener.stop()
            except Exception:
                pass
            self._listener = None
            self._active = False
            self._log("[Hotkey] 已注销全局热键", "INFO")
