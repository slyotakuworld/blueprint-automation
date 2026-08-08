"""
Conversation Store - 对话历史持久化
保存/加载自由聊天和蓝图生成的会话记录。
"""
import json
import os
import time
from typing import Any, Dict, List, Optional


class ConversationStore:
    """JSON 文件对话历史存储。"""

    def __init__(self, base_dir: str = ""):
        if not base_dir:
            # 默认存到用户目录下 .blueprintautomation/llm_sessions/
            home = os.path.expanduser("~")
            base_dir = os.path.join(home, ".blueprintautomation", "llm_sessions")
        self._dir = base_dir
        os.makedirs(self._dir, exist_ok=True)

    def _session_path(self, sess_id: str) -> str:
        return os.path.join(self._dir, f"{sess_id}.json")

    def save_session(self, sess_id: str, sess_type: str, title: str,
                     messages: List[Dict[str, str]]) -> bool:
        """保存一个会话。sess_type: 'chat' or 'blueprint'."""
        data = {
            "id": sess_id,
            "type": sess_type,
            "title": title,
            "messages": messages,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": time.time(),
        }
        try:
            with open(self._session_path(sess_id), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    def load_session(self, sess_id: str) -> Optional[Dict[str, Any]]:
        """加载一个会话。"""
        try:
            with open(self._session_path(sess_id), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return None

    def list_sessions(self, sess_type: str = "") -> List[Dict[str, Any]]:
        """列出所有会话（按时间倒序）。sess_type 空字符串=全部。"""
        sessions = []
        if not os.path.isdir(self._dir):
            return sessions
        for fname in os.listdir(self._dir):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(self._dir, fname), "r", encoding="utf-8") as f:
                    data = json.load(f)
                if sess_type and data.get("type") != sess_type:
                    continue
                sessions.append(data)
            except Exception:
                continue
        sessions.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
        return sessions

    def delete_session(self, sess_id: str) -> bool:
        """删除一个会话。"""
        try:
            os.remove(self._session_path(sess_id))
            return True
        except Exception:
            return False

    def new_session_id(self) -> str:
        """生成新会话 ID。"""
        return f"sess_{int(time.time() * 1000)}"

    def auto_save_current(self, sess_type: str, messages: List[Dict[str, str]],
                          title: str = "") -> str:
        """自动保存当前会话，返回 sess_id。如果 title 为空用第一条用户消息截断。"""
        sess_id = self.new_session_id()
        if not title and messages:
            for m in messages:
                if m.get("role") == "user":
                    title = m["content"][:30]
                    break
        if not title:
            title = "新对话"
        self.save_session(sess_id, sess_type, title, messages)
        return sess_id
