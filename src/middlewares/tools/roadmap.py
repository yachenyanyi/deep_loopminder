"""
学习路线图中间件 — 让 AI 能生成和管理学生的学习路线图

提供 5 个工具给 Agent：
- generate_roadmap: 生成结构化学习路线图（调用 Node.js API）
- roadmap_list: 列出用户所有路线图
- roadmap_get: 获取路线图详情
- roadmap_delete: 删除路线图
- roadmap_update_topic: 更新知识点状态

架构决策（双方确认 v2）：
- 认证方式: 内部 Secret（X-Internal-Secret header），不走 JWT
- 网络路径: 局域网直连（NODE_INTERNAL_URL）
- 知识点结构: v1 只做 2 层（阶段 → 知识点）
- 主键类型: SERIAL（整数）
- 路线图卡片: additional_kwargs.roadmap_attachment（结构化字段）

用法:
    from src.middlewares.tools.roadmap import create_roadmap_middleware

    middleware = create_roadmap_middleware()
    agent = create_agent(
        model="...",
        middleware=[middleware, ...],
    )

Node.js 后端需在调用 LangGraph 时在 config.configurable 中传入:
    {
        "user_id": 1,
    }
"""

from __future__ import annotations

import json
import os
from typing import Any, ClassVar

from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import BaseModel, Field

import requests

load_dotenv()

# ═══════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════

_DEFAULT_NODE_URL = "http://localhost:3000"
_TIMEOUT = 15


# ═══════════════════════════════════════════════
# 中间件
# ═══════════════════════════════════════════════


class RoadmapMiddleware(AgentMiddleware):
    """学习路线图中中间件 — 生成和管理学生的学习路线图

    提供 5 个工具供 Agent 调用：
    ``generate_roadmap`` / ``roadmap_list`` / ``roadmap_get``
    ``roadmap_delete`` / ``roadmap_update_topic``

    需要 Node.js 后端在调用 LangGraph 时通过
    ``config.configurable`` 传入 ``user_id``。
    """

    def __init__(
        self,
        node_api_url: str | None = None,
        internal_secret: str | None = None,
    ):
        super().__init__()
        self._node_api_url = (
            node_api_url
            or os.environ.get("NODE_INTERNAL_URL")
            or _DEFAULT_NODE_URL
        ).rstrip("/")
        self._internal_secret = (
            internal_secret
            or os.environ.get("INTERNAL_SECRET")
            or ""
        )
        self._tools_cache: list | None = None

    # ── 序列化兼容（LangGraph checkpoint） ──

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state.pop("_tools_cache", None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._tools_cache = None

    # ── 工具暴露 ──

    @property
    def tools(self) -> list:
        if self._tools_cache is None:
            self._tools_cache = [
                self._create_generate_tool(),
                self._create_list_tool(),
                self._create_get_tool(),
                self._create_delete_tool(),
                self._create_update_topic_tool(),
            ]
        return self._tools_cache

    # ── 用户上下文读取 ──

    def _get_user_context(self) -> dict:
        """从 LangGraph runtime config 读取用户上下文"""
        try:
            config = get_config()
            configurable = config.get("configurable", {})
            return {
                "user_id": configurable.get("user_id"),
            }
        except Exception:
            return {"user_id": None}

    # ── HTTP 调用封装 ──

    def _call_api(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
        use_internal_auth: bool = True,
    ) -> str:
        """调用 Node.js 后端 API

        Args:
            method: HTTP 方法
            path: API 路径（如 /api/roadmaps）
            params: URL 查询参数
            json_body: JSON 请求体
            use_internal_auth: 是否使用内部 Secret 认证（默认 True）

        Returns:
            响应文本或错误信息
        """
        url = f"{self._node_api_url}{path}"

        if use_internal_auth:
            headers = {
                "X-Internal-Secret": self._internal_secret,
                "Content-Type": "application/json",
            }
        else:
            # 需要 JWT 的接口（如前端调用的）
            user_ctx = self._get_user_context()
            jwt_token = None
            try:
                config = get_config()
                jwt_token = config.get("configurable", {}).get("jwt_token")
            except Exception:
                pass
            if not jwt_token:
                return "请先登录后再操作。"
            headers = {
                "Authorization": f"Bearer {jwt_token}",
                "Content-Type": "application/json",
            }

        try:
            resp = requests.request(
                method=method,
                url=url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=_TIMEOUT,
            )
        except requests.exceptions.ConnectionError:
            return "无法连接到路线图服务，请稍后再试。"
        except requests.exceptions.Timeout:
            return "路线图服务响应超时，请稍后再试。"
        except requests.exceptions.RequestException as e:
            return f"路线图服务请求失败: {e}"

        if resp.status_code == 401:
            return "登录已过期，请重新登录。"
        if resp.status_code == 403:
            return "没有权限执行此操作。"
        if resp.status_code == 404:
            return "未找到指定的路线图。"
        if resp.status_code == 429:
            return "生成路线图过于频繁，请稍后再试。"

        if not resp.ok:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text[:200]
            return f"路线图服务错误({resp.status_code}): {detail}"

        return resp.text

    # ═══════════════════════════════════════════
    # 工具 1: generate_roadmap
    # ═══════════════════════════════════════════

    class _GenerateRoadmapInput(BaseModel):
        """generate_roadmap 的输入参数"""
        title: str = Field(description="路线图标题，如「Python 学习路线」")
        subject: str = Field(description="学科/领域，如「Python」「数学」「英语」")
        description: str = Field(description="路线图整体描述（2-3 句话概括学习目标和路径）")
        level: str = Field(
            default="basic",
            description="难度等级: basic(零基础) / intermediate(有基础) / advanced(进阶) / expert(专家)"
        )
        category: str | None = Field(default=None, description="分类，如「编程」「语言」「理科」")
        tags: list[str] = Field(default_factory=list, description="标签数组")
        estimated_total_hours: float = Field(
            default=0, description="预估总学时（小时）"
        )
        stages: list[dict] = Field(
            description="""阶段列表，每个阶段包含:
            - name: 阶段名称
            - description: 阶段描述
            - sort_order: 排序（从 1 开始）
            - topics: 知识点数组，每个知识点包含 name/description/keywords/estimated_hours/sort_order"""
        )

    def _create_generate_tool(self) -> StructuredTool:
        description = """为学生生成一份结构化的学习路线图。

## 参数
- ``title`` (必填): 路线图标题
- ``subject`` (必填): 学科/领域
- ``description`` (必填): 整体描述
- ``level`` (可选): 难度 basic/intermediate/advanced/expert，默认 basic
- ``category`` (可选): 分类
- ``tags`` (可选): 标签数组
- ``estimated_total_hours`` (可选): 预估总学时
- ``stages`` (必填): 阶段列表，每个阶段包含 topics 知识点

## stages 结构
每个 stage 是 dict:
  - name: str — 阶段名称
  - description: str — 阶段描述
  - sort_order: int — 排序序号（从 1 开始）
  - topics: list[dict] — 知识点列表，每个包含:
    - name: str — 知识点名称
    - description: str — 知识点描述
    - keywords: list[str] — 关键词（用于联动学习）
    - estimated_hours: float — 预估学时
    - sort_order: int — 排序

## 使用场景
- 用户说"帮我规划学习路线" → 先追问基础和目标，再调用此工具
- 用户填写了生成表单 → 直接调用此工具

## 重要
- 阶段数量建议 3-6 个
- 每个阶段的知识点 3-6 个
- 总知识点 15-25 个
- keywords 要精准，用于后续联动 Teacher Agent 学习
"""

        def generate_sync(
            title: str,
            subject: str,
            description: str,
            level: str = "basic",
            category: str | None = None,
            tags: list[str] | None = None,
            estimated_total_hours: float = 0,
            stages: list[dict] | None = None,
        ) -> str:
            user_ctx = self._get_user_context()
            user_id = user_ctx.get("user_id")
            if not user_id:
                return "无法识别用户身份，请确保已登录。"

            body = {
                "title": title,
                "subject": subject,
                "description": description,
                "level": level,
                "tags": tags or [],
                "estimated_total_hours": estimated_total_hours,
                "user_id": user_id,
                "stages": stages or [],
            }
            if category:
                body["category"] = category

            result = self._call_api("POST", "/api/roadmaps", json_body=body, use_internal_auth=True)

            # 尝试解析返回的 roadmap_id，存入 additional_kwargs
            try:
                data = json.loads(result)
                roadmap_id = data.get("id")
                if roadmap_id:
                    # 标记需要在 after_model 中注入 roadmap_attachment
                    self._pending_roadmap = {
                        "roadmap_id": roadmap_id,
                        "title": data.get("title", title),
                        "total_stages": data.get("total_stages", 0),
                        "total_topics": data.get("total_topics", 0),
                        "estimated_total_hours": data.get("estimated_total_hours", 0),
                    }
            except Exception:
                pass

            return result

        return StructuredTool.from_function(
            func=generate_sync,
            name="generate_roadmap",
            description=description,
            args_schema=self._GenerateRoadmapInput,
        )

    # ═══════════════════════════════════════════
    # 工具 2: roadmap_list
    # ═══════════════════════════════════════════

    def _create_list_tool(self) -> StructuredTool:
        description = """列出用户的所有学习路线图。

## 返回格式
返回路线图列表，每个路线图包含标题、学科、进度、状态等信息。

## 使用场景
- 用户说"我有哪些学习路线" → 调用此工具
- 需要了解用户已有的学习计划
"""

        def list_sync() -> str:
            user_ctx = self._get_user_context()
            user_id = user_ctx.get("user_id")
            if not user_id:
                return "无法识别用户身份，请确保已登录。"
            return self._call_api(
                "GET", "/api/roadmaps",
                params={"user_id": user_id},
                use_internal_auth=True,
            )

        return StructuredTool.from_function(
            func=list_sync,
            name="roadmap_list",
            description=description,
        )

    # ═══════════════════════════════════════════
    # 工具 3: roadmap_get
    # ═══════════════════════════════════════════

    class _RoadmapGetInput(BaseModel):
        """roadmap_get 的输入参数"""
        roadmap_id: int = Field(description="路线图 ID")

    def _create_get_tool(self) -> StructuredTool:
        description = """获取路线图详情（含所有阶段和知识点）。

## 参数
- ``roadmap_id`` (必填): 路线图 ID

## 使用场景
- 用户问"我的 Python 学习路线进度怎样了" → 先 list 找到 id，再 get 查看详情
"""

        def get_sync(roadmap_id: int) -> str:
            return self._call_api(
                "GET", f"/api/roadmaps/{roadmap_id}",
                use_internal_auth=True,
            )

        return StructuredTool.from_function(
            func=get_sync,
            name="roadmap_get",
            description=description,
            args_schema=self._RoadmapGetInput,
        )

    # ═══════════════════════════════════════════
    # 工具 4: roadmap_delete
    # ═══════════════════════════════════════════

    class _RoadmapDeleteInput(BaseModel):
        """roadmap_delete 的输入参数"""
        roadmap_id: int = Field(description="路线图 ID")

    def _create_delete_tool(self) -> StructuredTool:
        description = """删除一个路线图。

## 参数
- ``roadmap_id`` (必填): 路线图 ID

## 使用场景
- 用户说"把我的 XX 路线删掉" → 调用此工具
"""

        def delete_sync(roadmap_id: int) -> str:
            return self._call_api(
                "DELETE", f"/api/roadmaps/{roadmap_id}",
                use_internal_auth=True,
            )

        return StructuredTool.from_function(
            func=delete_sync,
            name="roadmap_delete",
            description=description,
            args_schema=self._RoadmapDeleteInput,
        )

    # ═══════════════════════════════════════════
    # 工具 5: roadmap_update_topic
    # ═══════════════════════════════════════════

    class _RoadmapUpdateTopicInput(BaseModel):
        """roadmap_update_topic 的输入参数"""
        roadmap_id: int = Field(description="路线图 ID")
        topic_id: int = Field(description="知识点 ID")
        status: str = Field(
            description="新状态: not_started / in_progress / completed"
        )
        notes: str | None = Field(default=None, description="用户笔记（可选）")

    def _create_update_topic_tool(self) -> StructuredTool:
        description = """更新路线图中某个知识点的状态或笔记。

## 参数
- ``roadmap_id`` (必填): 路线图 ID
- ``topic_id`` (必填): 知识点 ID
- ``status`` (必填): not_started / in_progress / completed
- ``notes`` (可选): 用户笔记

## 使用场景
- 用户说"变量与类型这个知识点我已经学完了" → status="completed"
- 用户想给某个知识点加笔记
"""

        def update_topic_sync(
            roadmap_id: int,
            topic_id: int,
            status: str,
            notes: str | None = None,
        ) -> str:
            body: dict[str, Any] = {"status": status}
            if notes is not None:
                body["notes"] = notes
            return self._call_api(
                "PUT",
                f"/api/roadmaps/{roadmap_id}/topics/{topic_id}",
                json_body=body,
                use_internal_auth=True,
            )

        return StructuredTool.from_function(
            func=update_topic_sync,
            name="roadmap_update_topic",
            description=description,
            args_schema=self._RoadmapUpdateTopicInput,
        )

    # ── after_model 钩子：注入 roadmap_attachment ──

    def after_model(self, state, runtime):
        """在 AI 回复后，如果有待注入的路线图信息，附加到消息"""
        pending = getattr(self, "_pending_roadmap", None)
        if not pending:
            return None
        self._pending_roadmap = None

        messages = state.get("messages", [])
        if not messages:
            return None

        # 找到最后一条 AI 消息
        from langchain_core.messages import AIMessage
        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        # 注入 roadmap_attachment 到 additional_kwargs
        additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
        additional_kwargs["roadmap_attachment"] = pending

        updated_msg = last_msg.model_copy(
            update={"additional_kwargs": additional_kwargs}
        )
        return {"messages": [updated_msg]}

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)


# ═══════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════


def create_roadmap_middleware(
    node_api_url: str | None = None,
    internal_secret: str | None = None,
) -> RoadmapMiddleware:
    """创建路线图中中间件

    Args:
        node_api_url: Node.js 内部 API 地址。默认从 NODE_INTERNAL_URL 环境变量读取。
        internal_secret: 内部认证密钥。默认从 INTERNAL_SECRET 环境变量读取。

    Returns:
        RoadmapMiddleware 实例
    """
    return RoadmapMiddleware(
        node_api_url=node_api_url,
        internal_secret=internal_secret,
    )
