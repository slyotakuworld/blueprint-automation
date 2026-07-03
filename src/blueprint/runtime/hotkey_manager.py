import os
import sys
import threading
from typing import Callable, Optional

try:
    from pynput import keyboard
    _HAS_PYNPUT = True
except Exception:
    _HAS_PYNPUT = False


class HotkeyManager:
    """
    硬约束 §3：Runtime 启动时注册全局热键：
      - F8 → 优雅停止：ctx.is_running = False（已入队节点执行完后退出)
      - F6 → 紧急杀：os.kill(os.getpid(), 9)（鼠标乱飞救命用)
    """

    def __init__(
        self,
        on_stop: Optional[Callable[[], None]] = None,
        log_fn: Optional[Callable[[str, str], None]] = None,
    ):
        self._on_stop = on_stop
        self._log = log_fn or (lambda m, l="INFO": None)
        self._listener = None
        self._lock = threading.Lock()
        self._active = False

    # ---------------- pynput key 兼容 ----------------
    @staticmethod
    def _match(key, target_name: str) -> bool:
        try:
            name = getattr(keyboard.Key, target_name, None)
            if name is not None and key == name:
                return True
            if hasattr(key, "name"):
                if str(getattr(key, "name", "")).lower() == target_name.lower():
                    return True
        except Exception:
            pass
        return False

    def _on_press(self, key):
        try:
            if self._match(key, "f8"):
                self._log("[Hotkey] F8: 优雅停止 (is_running=False)", "WARN")
                if self._on_stop:
                    try:
                        self._on_stop()
                    except Exception:
                        pass
            elif self._match(key, "f6"):
                self._log("[Hotkey] F6: 紧急杀进程!!!", "ERROR")
                try:
                    if os.name == "nt":
                        os._exit(1)
                    else:
                        os.kill(os.getpid(), 9)
                except Exception:
                    sys.exit(1)
        except Exception:
            pass

    # ---------------- 生命周期 ----------------
    def start(self):
        with self._lock:
            if self._active:
                return
            if not _HAS_PYNPUT:
                self._log("[Hotkey] pynput 不可用，跳过全局热键注册。", "WARN")
                return
            try:
                self._listener = keyboard.Listener(on_press=self._on_press)
                self._listener.daemon = True
                self._listener.start()
                self._active = True
                self._log("[Hotkey] 已注册: F8=优雅停止, F6=紧急杀", "INFO")
            except Exception as e:
                self._log(f"[Hotkey] 注册失败: {e}", "WARN")

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
