"""
LLM Worker - QThread 异步调用层
在后台线程调用 LLM API，通过信号回传结果，避免阻塞 UI 主线程。
"""
from PySide6.QtCore import QThread, Signal
from typing import Callable, Dict, List, Optional


class ChatWorker(QThread):
    """
    异步 LLM 聊天 Worker。
    
    Signals:
        chunk_received(str): 流式输出时逐 chunk 发射
        finished(str): 完整回复文本（正常结束）
        error(str): 错误信息（异常时）
    """
    chunk_received = Signal(str)
    finished = Signal(str)
    error = Signal(str)

    def __init__(
        self,
        messages: List[Dict[str, str]],
        client=None,
        stream: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._messages = messages
        self._client = client
        self._stream = stream
        self._cancelled = False

    def cancel(self):
        """请求取消（协作式，不强制中断）。"""
        self._cancelled = True

    def run(self):
        try:
            if self._client is None:
                from src.blueprint.llm.llm_client import LLMClient
                self._client = LLMClient.from_config()

            if not self._client.is_available():
                # 配置不完整，返回提示信息而不是报错
                hints = []
                if not self._client.base_url:
                    hints.append("Base URL 未配置")
                if not self._client.model:
                    hints.append("模型未选择")
                if self._client.provider != "ollama" and not self._client.api_key:
                    hints.append("API Key 未填写")
                if hints:
                    self.error.emit(
                        f"LLM 未就绪：{'、'.join(hints)}。\n"
                        f"请打开设置面板（⚙ 助手设置）配置后重试。"
                    )
                    return
                # SDK 未安装
                try:
                    import openai  # noqa: F401
                except ImportError:
                    self.error.emit("openai 库未安装。请在终端执行：pip install openai")
                    return

            def _on_chunk(text: str):
                if not self._cancelled:
                    self.chunk_received.emit(text)

            if self._stream:
                reply = self._client.chat(self._messages, stream_callback=_on_chunk)
            else:
                reply = self._client.chat(self._messages)

            if self._cancelled:
                return
            self.finished.emit(reply)

        except Exception as e:
            if not self._cancelled:
                self.error.emit(f"LLM 调用失败：{type(e).__name__}: {e}")
