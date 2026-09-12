"""
SkillsToolMiddleware — 用工具模拟 Skill 渐进式加载

由于 Teacher Agent 使用 create_agent（非 create_deep_agent），
没有 read_file 能力，无法直接读取 SKILL.md。

本中间件实现：
- Level 1: 启动时将 skill 的 name + description 注入 system prompt
- Level 2: 提供 load_skill 工具，agent 调用时返回完整 SKILL.md 内容

使用方式：
    middleware = SkillsToolMiddleware(skills_dir="/path/to/skills")
    agent = create_agent(
        model="...",
        middleware=[middleware],
    )
"""

from __future__ import annotations

import asyncio
import os
import re
import yaml
import logging
from typing import Any, Callable, Awaitable
from pathlib import Path

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class SkillsToolMiddleware(AgentMiddleware):
    """Skills 工具中间件 — 模拟渐进式加载

    扫描指定目录下的 SKILL.md 文件，解析 frontmatter，
    将 name+description 注入 system prompt，
    并提供 load_skill 工具让 agent 按需读取完整内容。
    """

    def __init__(self, skills_dir: str):
        super().__init__()
        self._skills_dir = skills_dir
        self._skills: dict[str, dict[str, str]] = {}  # name -> {description, content, path}
        self._tools_cache: list | None = None
        self._loaded = False
        # 延迟加载：tools 属性只注册静态工具（不扫描文件系统），
        # 扫描在 awrap_model_call 中异步完成，避免 ASGI 事件循环阻塞

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state.pop("_tools_cache", None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._tools_cache = None

    def _scan_skills(self):
        """扫描 skills 目录，解析所有 SKILL.md"""
        skills_root = Path(self._skills_dir)
        if not skills_root.exists():
            logger.warning("Skills 目录不存在: %s", self._skills_dir)
            return

        # 遍历所有子目录，找 SKILL.md
        for skill_dir in skills_root.rglob("SKILL.md"):
            try:
                content = skill_dir.read_text(encoding="utf-8")
                frontmatter, body = self._parse_frontmatter(content)

                name = frontmatter.get("name", skill_dir.parent.name)
                description = frontmatter.get("description", "")

                self._skills[name] = {
                    "description": description,
                    "content": body,  # 完整内容（不含 frontmatter）
                    "path": str(skill_dir),
                }
                logger.info("已加载 Skill: %s — %s", name, description[:50])
            except Exception as e:
                logger.warning("解析 SKILL.md 失败 (%s): %s", skill_dir, e)

    def _ensure_loaded(self):
        """确保 skills 已加载。同步版，仅在非 ASGI 上下文使用。"""
        if not self._loaded:
            self._scan_skills()
            self._loaded = True

    async def _ensure_loaded_async(self):
        """异步延迟加载，用 asyncio.to_thread 避免阻塞事件循环"""
        if not self._loaded:
            await asyncio.to_thread(self._scan_skills)
            self._loaded = True

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict, str]:
        """解析 YAML frontmatter，返回 (metadata_dict, body_text)"""
        pattern = r"^---\s*\n(.*?)\n---\s*\n(.*)$"
        match = re.match(pattern, content, re.DOTALL)
        if not match:
            return {}, content

        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}

        body = match.group(2)
        return metadata, body

    # ── 工具暴露 ──

    @property
    def tools(self) -> list:
        if self._tools_cache is None:
            # 不在这里扫描文件系统（可能在 ASGI 上下文中被调用）
            # 直接注册静态工具，扫描延迟到 awrap_model_call 中异步完成
            self._tools_cache = [self._create_load_skill_tool()]
        return self._tools_cache

    # ── load_skill 工具 ──

    class _LoadSkillInput(BaseModel):
        """load_skill 的输入参数"""
        skill_name: str = Field(description="要加载的技能名称")

    def _create_load_skill_tool(self) -> StructuredTool:
        def load_sync(skill_name: str) -> str:
            self._ensure_loaded()
            skill = self._skills.get(skill_name)
            if not skill:
                available = ", ".join(self._skills.keys()) if self._skills else "无"
                return f"未找到技能 '{skill_name}'。可用技能: {available}"
            return skill["content"]

        return StructuredTool.from_function(
            func=load_sync,
            name="load_skill",
            description="加载指定技能的详细指令。当用户的任务匹配某个技能时，调用此工具获取完整操作指引。",
            args_schema=self._LoadSkillInput,
        )

    # ── wrap_model_call: 注入 skill 摘要到 system prompt ──

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        await self._ensure_loaded_async()
        if not self._skills:
            return await handler(request)

        # 生成 skills 摘要
        lines = ["\n\n[可用技能 — 需要时调用 load_skill 工具加载详细指令]"]
        for name, skill in self._skills.items():
            lines.append(f"- {name}: {skill['description']}")

        skills_summary = "\n".join(lines)

        # 追加到当前 system message
        current_system = getattr(request, "system_message", None)
        if current_system:
            new_content = str(current_system) + skills_summary
        else:
            new_content = skills_summary

        request = request.override(system_message=new_content)
        return await handler(request)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        self._ensure_loaded()
        return asyncio.run(self.awrap_model_call(request, handler))


def create_skills_tool_middleware(skills_dir: str) -> SkillsToolMiddleware:
    """创建 Skills 工具中间件

    Args:
        skills_dir: skills 目录路径（包含子目录/SKILL.md）

    Returns:
        SkillsToolMiddleware 实例
    """
    return SkillsToolMiddleware(skills_dir=skills_dir)
