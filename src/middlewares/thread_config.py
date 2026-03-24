"""
线程配置管理

管理每个代理的 thread_id，支持：
- 持久化存储到 threads.json
- 当前线程和历史线程管理
- 自动向 LangGraph API 注册线程
"""

import json
import os
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
import uuid
import asyncio


@dataclass
class AgentThreadInfo:
    """单个代理的线程信息"""
    current: str                    # 当前使用的 thread_id
    history: list[str] = field(default_factory=list)  # 历史对话


class ThreadConfigManager:
    """线程配置管理器

    配置文件格式 (threads.json):
    {
        "chat_agent": {
            "current": "uuid-1",
            "history": ["uuid-0"]
        },
        "coder_agent": {
            "current": "uuid-2",
            "history": []
        }
    }

    注意：thread_id 必须是有效的 UUID 格式，调用 LangGraph API 时需要先注册。
    """

    DEFAULT_CONFIG_NAME = "threads.json"

    def __init__(
        self,
        config_path: Optional[str] = None,
        server_url: str = "http://127.0.0.1:2024"
    ):
        """
        Args:
            config_path: 配置文件路径，默认为项目根目录下的 threads.json
            server_url: LangGraph Server 地址，用于注册线程
        """
        if config_path:
            self.config_path = Path(config_path)
        else:
            # 默认在项目根目录
            # 路径: thread_config.py -> middlewares -> src -> deep_loopminder
            self.config_path = Path(__file__).parent.parent.parent / self.DEFAULT_CONFIG_NAME

        self.server_url = server_url
        self._config: dict[str, dict] = {}
        self._client = None
        self._load_config()

    def _load_config(self) -> None:
        """加载配置文件"""
        if self.config_path.exists():
            try:
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._config = json.load(f)
                print(f"✅ 加载线程配置: {self.config_path}")
            except Exception as e:
                print(f"⚠️ 加载线程配置失败: {e}")
                self._config = {}
        else:
            self._config = {}
            print(f"📝 线程配置文件不存在，将创建: {self.config_path}")

    def _save_config(self) -> None:
        """保存配置文件"""
        try:
            with open(self.config_path, 'w', encoding='utf-8') as f:
                json.dump(self._config, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"⚠️ 保存线程配置失败: {e}")

    def _is_valid_uuid(self, thread_id: str) -> bool:
        """检查是否是有效的 UUID 格式"""
        try:
            uuid.UUID(thread_id)
            return True
        except (ValueError, AttributeError):
            return False

    async def _get_client(self):
        """获取 LangGraph SDK 客户端（懒加载）"""
        if self._client is None:
            try:
                from langgraph_sdk import get_client
                self._client = get_client(url=self.server_url)
            except ImportError:
                print("⚠️ 未安装 langgraph-sdk")
                return None
        return self._client

    async def _ensure_thread_exists(self, thread_id: str) -> bool:
        """确保线程在 LangGraph API 中存在

        Args:
            thread_id: 线程 ID（必须是有效的 UUID）

        Returns:
            线程是否可用
        """
        client = await self._get_client()
        if client is None:
            return False

        try:
            # 尝试创建线程，如果已存在则不做任何操作
            await client.threads.create(
                thread_id=thread_id,
                if_exists='do_nothing'
            )
            return True
        except Exception as e:
            print(f"⚠️ 创建线程失败 {thread_id}: {e}")
            return False

    async def get_thread(self, agent_name: str) -> AgentThreadInfo:
        """获取代理的线程信息

        如果不存在，会自动创建新的线程并向 API 注册。
        """
        if agent_name not in self._config:
            # 创建新的线程
            new_thread_id = str(uuid.uuid4())

            # 向 API 注册
            success = await self._ensure_thread_exists(new_thread_id)
            if not success:
                print(f"⚠️ 无法为 {agent_name} 创建线程")

            self._config[agent_name] = {
                "current": new_thread_id,
                "history": []
            }
            self._save_config()
            print(f"🆕 为 {agent_name} 创建新线程: {new_thread_id}")

        info = self._config[agent_name]
        current = info.get("current", "")

        # 验证当前线程 ID 是否有效
        if not self._is_valid_uuid(current):
            # 生成新的有效 UUID
            new_thread_id = str(uuid.uuid4())
            await self._ensure_thread_exists(new_thread_id)
            self._config[agent_name]["current"] = new_thread_id
            self._save_config()
            current = new_thread_id

        return AgentThreadInfo(
            current=current,
            history=info.get("history", [])
        )

    async def set_current_thread(self, agent_name: str, thread_id: str) -> bool:
        """设置代理的当前线程

        旧的当前线程会移到历史记录中。
        线程 ID 必须是有效的 UUID 格式。

        Returns:
            是否设置成功
        """
        # 验证 UUID 格式
        if not self._is_valid_uuid(thread_id):
            print(f"⚠️ 无效的 thread_id 格式: {thread_id}")
            return False

        # 确保 API 中存在该线程
        success = await self._ensure_thread_exists(thread_id)
        if not success:
            return False

        if agent_name not in self._config:
            self._config[agent_name] = {
                "current": thread_id,
                "history": []
            }
        else:
            old_current = self._config[agent_name].get("current")
            if old_current and old_current != thread_id:
                # 将旧的移到历史
                history = self._config[agent_name].get("history", [])
                if old_current not in history:
                    history.insert(0, old_current)  # 最新的在前面
                self._config[agent_name]["history"] = history

            self._config[agent_name]["current"] = thread_id

        self._save_config()
        print(f"📝 更新 {agent_name} 当前线程: {thread_id}")
        return True

    async def create_new_thread(self, agent_name: str) -> str:
        """为代理创建新的线程

        旧的当前线程会移到历史记录中。
        返回新创建的 thread_id。
        """
        new_thread_id = str(uuid.uuid4())
        success = await self.set_current_thread(agent_name, new_thread_id)
        if success:
            return new_thread_id
        return ""

    def get_history_threads(self, agent_name: str) -> list[str]:
        """获取代理的历史线程列表"""
        if agent_name not in self._config:
            return []
        return self._config[agent_name].get("history", [])

    async def switch_to_history_thread(self, agent_name: str, thread_id: str) -> bool:
        """切换到历史线程

        Args:
            agent_name: 代理名称
            thread_id: 历史线程 ID

        Returns:
            是否切换成功
        """
        history = self.get_history_threads(agent_name)
        if thread_id in history:
            return await self.set_current_thread(agent_name, thread_id)
        return False

    def list_all_threads(self) -> dict[str, AgentThreadInfo]:
        """列出所有代理的线程信息（不验证，仅返回配置）"""
        result = {}
        for agent_name in self._config:
            info = self._config[agent_name]
            result[agent_name] = AgentThreadInfo(
                current=info.get("current", ""),
                history=info.get("history", [])
            )
        return result

    def clear_history(self, agent_name: str) -> None:
        """清空代理的历史线程"""
        if agent_name in self._config:
            self._config[agent_name]["history"] = []
            self._save_config()


# 全局单例
_thread_config_manager: Optional[ThreadConfigManager] = None


def get_thread_config_manager(
    config_path: Optional[str] = None,
    server_url: str = "http://127.0.0.1:2024"
) -> ThreadConfigManager:
    """获取线程配置管理器单例"""
    global _thread_config_manager
    if _thread_config_manager is None:
        _thread_config_manager = ThreadConfigManager(config_path, server_url)
    return _thread_config_manager