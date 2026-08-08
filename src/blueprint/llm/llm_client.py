"""
LLM Client - OpenAI SDK 封装层
支持 OpenAI 兼容接口（含 DeepSeek/通义等）和 Ollama 本地部署。
"""
import json as _json
import os
import sys
from typing import Any, Callable, Dict, List, Optional


class LLMError(Exception):
    """LLM 调用异常"""
    pass


class LLMClient:
    """OpenAI 兼容 LLM 客户端，封装 SDK 调用细节。"""

    def __init__(
        self,
        provider: str = "openai",
        base_url: str = "",
        api_key: str = "",
        model: str = "",
        max_ctx: int = 16384,
        max_out: int = 2048,
        temperature: float = 0.7,
        top_p: float = 0.9,
        freq_pen: float = 0.0,
        pres_pen: float = 0.0,
        stream: bool = True,
        skip_ssl: bool = True,
    ):
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_ctx = max_ctx
        self.max_out = max_out
        self.temperature = temperature
        self.top_p = top_p
        self.freq_pen = freq_pen
        self.pres_pen = pres_pen
        self.stream = stream
        self.skip_ssl = skip_ssl
        self._client = None

    @staticmethod
    def from_config() -> "LLMClient":
        """从 user_settings + llm_defaults.json 构建客户端。"""
        from src.config.user_settings import get_llm_config

        cfg = get_llm_config()
        provider = cfg.get("active_provider", "openai")
        providers = cfg.get("providers", {})
        prov = providers.get(provider, {})
        base_url = prov.get("base_url", "")
        model = prov.get("model", "")
        api_key = cfg.get("api_key", "")

        # 直接从 llm_defaults.json 读 stream / skip_ssl（不经过 UI 层）
        stream, skip_ssl = True, True
        try:
            from src.blueprint.llm.prompt_builder import _load_llm_defaults
            defaults = _load_llm_defaults()
            ui_cfg = defaults.get("ui", {})
            stream = bool(ui_cfg.get("stream_output", True))
            skip_ssl = bool(ui_cfg.get("skip_ssl_verify", True))
        except Exception:
            pass

        # 通用参数：从 user_settings 读取（面板可调）
        from src.config.user_settings import get_llm_params
        params = get_llm_params()
        temperature = params.get("temperature", 0.7)
        top_p = params.get("top_p", 0.9)
        freq_pen = params.get("freq_pen", 0.0)
        pres_pen = params.get("pres_pen", 0.0)
        max_ctx = params.get("max_ctx", 16384)
        max_out = params.get("max_out", 2048)

        return LLMClient(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            model=model,
            max_ctx=max_ctx,
            max_out=max_out,
            temperature=temperature,
            top_p=top_p,
            freq_pen=freq_pen,
            pres_pen=pres_pen,
            stream=stream,
            skip_ssl=skip_ssl,
        )

    def _ensure_client(self):
        """懒初始化 OpenAI SDK 客户端。"""
        if self._client is not None:
            return
        try:
            from openai import OpenAI
        except ImportError:
            raise LLMError(
                "openai 库未安装。请在终端执行：pip install openai"
            )

        kwargs: Dict[str, Any] = {
            "base_url": self.base_url or "https://api.openai.com/v1",
            "api_key": self.api_key or "ollama",  # Ollama 不需要 key，给个占位
        }
        if self.skip_ssl:
            import ssl as _ssl
            kwargs["http_client"] = None
            try:
                import httpx as _httpx
                kwargs["http_client"] = _httpx.Client(
                    verify=False,
                    timeout=_httpx.Timeout(connect=15, read=120, write=30, pool=15),
                )
            except ImportError:
                pass

        self._client = OpenAI(**kwargs)

    def chat(
        self,
        messages: List[Dict[str, str]],
        stream_callback: Optional[Callable[[str], None]] = None,
    ) -> str:
        """
        发送聊天请求，返回完整回复文本。

        Args:
            messages: OpenAI 消息格式 [{"role": "system", "content": "..."}, ...]
            stream_callback: 流式回调，每个 token chunk 调用一次

        Returns:
            完整回复文本
        """
        self._ensure_client()

        # 裁剪上下文：保留 system prompt + 最近 N 条消息
        sys_msgs = [m for m in messages if m["role"] == "system"]
        other_msgs = [m for m in messages if m["role"] != "system"]
        # 粗略估算：每条消息平均 200 token，max_ctx 扣除 max_out 和 system
        max_history = max(4, (self.max_ctx - self.max_out - 500) // 200)
        if len(other_msgs) > max_history:
            other_msgs = other_msgs[-max_history:]
        trimmed = sys_msgs + other_msgs

        try:
            if self.stream and stream_callback is not None:
                return self._chat_stream(trimmed, stream_callback)
            else:
                return self._chat_sync(trimmed)
        except Exception as e:
            raise LLMError(str(e))

    def _chat_sync(self, messages: List[Dict[str, str]]) -> str:
        """非流式调用。"""
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=self.max_out,
            temperature=self.temperature,
            top_p=self.top_p,
            frequency_penalty=self.freq_pen,
            presence_penalty=self.pres_pen,
        )
        return resp.choices[0].message.content or ""

    def _chat_stream(
        self,
        messages: List[Dict[str, str]],
        callback: Callable[[str], None],
    ) -> str:
        """流式调用，逐 chunk 回调。"""
        chunks: List[str] = []
        stream = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=self.max_out,
            temperature=self.temperature,
            top_p=self.top_p,
            frequency_penalty=self.freq_pen,
            presence_penalty=self.pres_pen,
            stream=True,
        )
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                chunks.append(text)
                callback(text)
        return "".join(chunks)

    def is_available(self) -> bool:
        """检查客户端是否可用（配置完整 + SDK 已安装）。"""
        if not self.base_url or not self.model:
            return False
        if self.provider != "ollama" and not self.api_key:
            return False
        try:
            import openai  # noqa: F401
            return True
        except ImportError:
            return False
