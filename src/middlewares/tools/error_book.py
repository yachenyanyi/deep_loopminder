"""
错题本中间件 — 让 AI 能读写学生的错题数据

提供 9 个工具给 Agent：
- error_book_list: 查询错题列表
- error_book_add: 添加错题
- error_book_get: 查看错题详情
- error_book_update: 编辑错题
- error_book_delete: 删除错题
- error_book_review: 提交复习结果
- error_book_stats: 错题统计
- error_book_weak: 薄弱知识点分析
- error_book_review_q: 待复习队列

用法:
    from src.middlewares.tools.error_book import ErrorBookMiddleware, create_error_book_middleware

    middleware = create_error_book_middleware()
    agent = create_agent(
        model="...",
        middleware=[middleware, ...],
    )

Node.js 后端需在调用 LangGraph 时在 config.configurable 中传入:
    {
        "user_id": "<用户ID>",
        "jwt_token": "<用户的 JWT>",
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

_DEFAULT_API_URL = "https://api.yachenyanyi.top"
_TIMEOUT = 15

# ═══════════════════════════════════════════════
# 中间件
# ═══════════════════════════════════════════════


class ErrorBookMiddleware(AgentMiddleware):
    """错题本中间件 — 让 AI 能读写学生的错题数据

    提供 9 个工具供 Agent 调用：
    ``error_book_list`` / ``add`` / ``get`` / ``update`` / ``delete``
    ``review`` / ``stats`` / ``weak`` / ``review_q``

    需要 Node.js 后端在调用 LangGraph 时通过
    ``config.configurable`` 传入 ``user_id`` 和 ``jwt_token``。
    """

    def __init__(self, api_base_url: str | None = None, default_headers: dict | None = None):
        super().__init__()
        self._api_base_url = (api_base_url or os.environ.get("ERROR_BOOK_API_URL") or _DEFAULT_API_URL).rstrip("/")
        self._default_headers = default_headers or {}
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
                self._create_list_tool(),
                self._create_add_tool(),
                self._create_get_tool(),
                self._create_update_tool(),
                self._create_delete_tool(),
                self._create_review_tool(),
                self._create_stats_tool(),
                self._create_weak_tool(),
                self._create_review_q_tool(),
            ]
        return self._tools_cache

    # ── 用户上下文读取 ──

    def _get_user_context(self) -> dict:
        """从 LangGraph runtime config 读取用户上下文

        Node.js 后端需在调用时传入:
            config.configurable.user_id
            config.configurable.jwt_token
        """
        try:
            config = get_config()
            configurable = config.get("configurable", {})
            return {
                "user_id": configurable.get("user_id"),
                "jwt_token": configurable.get("jwt_token"),
            }
        except Exception:
            return {"user_id": None, "jwt_token": None}

    # ── HTTP 调用封装 ──

    def _call_api(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        json_body: dict | None = None,
    ) -> str:
        """统一调用 Node.js 后端 API

        Args:
            method: HTTP 方法（GET/POST/PUT/DELETE）
            path: API 路径（如 /questions）
            params: URL 查询参数
            json_body: JSON 请求体

        Returns:
            格式化后的响应文本或错误信息
        """
        user_ctx = self._get_user_context()
        jwt_token = user_ctx.get("jwt_token")
        if not jwt_token:
            return "请先登录后再查看错题本。"

        url = f"{self._api_base_url}/api/error-book{path}"
        headers = {
            "Authorization": f"Bearer {jwt_token}",
            "Content-Type": "application/json",
            **self._default_headers,
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
            return "无法连接到错题本服务，请稍后再试。"
        except requests.exceptions.Timeout:
            return "错题本服务响应超时，请稍后再试。"
        except requests.exceptions.RequestException as e:
            return f"错题本服务请求失败: {e}"

        if resp.status_code == 401:
            return "登录已过期，请重新登录后再查看错题本。"
        if resp.status_code == 403:
            return "你没有权限执行此操作。"
        if resp.status_code == 404:
            return "未找到指定的错题记录。"

        if not resp.ok:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text[:200]
            return f"错题本服务错误({resp.status_code}): {detail}"

        return resp.text

    # ═══════════════════════════════════════════
    # 工具 1: error_book_list
    # ═══════════════════════════════════════════

    class _ErrorBookListInput(BaseModel):
        """error_book_list 的输入参数"""
        subject: str | None = Field(default=None, description="学科筛选，如「数学」「英语」「物理」。不填则查全部")
        page: int = Field(default=1, description="页码，从 1 开始", ge=1)
        page_size: int = Field(default=20, description="每页条数，默认 20，最大 100", ge=1, le=100)

    def _create_list_tool(self) -> StructuredTool:
        description = """查询学生的错题列表。

## 参数
- ``subject`` (可选): 学科筛选，如「数学」「英语」。不填则查全部
- ``page`` (可选): 页码，默认 1
- ``page_size`` (可选): 每页条数，默认 20，最大 100

## 返回格式
返回错题列表，包含题目内容、正确答案、学科、知识点、错因、创建时间。

## 使用场景
- 学生说"帮我看看我的错题" → 调用此工具
- 需要根据错题针对性讲解 → 先调用此工具查看错题
"""

        def list_sync(subject: str | None = None, page: int = 1, page_size: int = 20) -> str:
            params = {"page": page, "page_size": page_size}
            if subject:
                params["subject"] = subject
            return self._call_api("GET", "/questions", params=params)

        return StructuredTool.from_function(
            func=list_sync,
            name="error_book_list",
            description=description,
            args_schema=self._ErrorBookListInput,
        )

    # ═══════════════════════════════════════════
    # 工具 2: error_book_add
    # ═══════════════════════════════════════════

    class _ErrorBookAddInput(BaseModel):
        """error_book_add 的输入参数"""
        question_text: str = Field(description="题目内容")
        correct_answer: str = Field(description="正确答案")
        user_answer: str | None = Field(default=None, description="用户的错误答案（可选）")
        subject: str = Field(description="学科，如「数学」「英语」「物理」")
        knowledge_point: str = Field(description="知识点，如「函数求导」「被动语态」")
        error_reason: str = Field(description="错误原因，如「概念不清」「计算错误」「审题不清」")
        analysis: str | None = Field(default=None, description="题目解析（可选）")
        difficulty: int | None = Field(default=None, description="难度等级，1-5（可选），1=简单，5=困难", ge=1, le=5)

    def _create_add_tool(self) -> StructuredTool:
        description = """添加一道错题到学生的错题本。

## 参数
- ``question_text`` (必填): 题目内容
- ``correct_answer`` (必填): 正确答案
- ``user_answer`` (可选): 用户的错误答案
- ``subject`` (必填): 学科，如「数学」「英语」「物理」
- ``knowledge_point`` (必填): 知识点，如「函数求导」「被动语态」
- ``error_reason`` (必填): 错误原因
- ``analysis`` (可选): 题目解析
- ``difficulty`` (可选): 难度等级 1-5

## 使用场景
- 学生说"帮我记一下这道错题" → 调用此工具
- 讲解过程中发现学生做错题 → 主动帮学生记录
"""

        def add_sync(
            question_text: str,
            correct_answer: str,
            subject: str,
            knowledge_point: str,
            error_reason: str,
            user_answer: str | None = None,
            analysis: str | None = None,
            difficulty: int | None = None,
        ) -> str:
            body = {
                "question_text": question_text,
                "correct_answer": correct_answer,
                "subject": subject,
                "knowledge_point": knowledge_point,
                "error_reason": error_reason,
            }
            if user_answer is not None:
                body["user_answer"] = user_answer
            if analysis is not None:
                body["analysis"] = analysis
            if difficulty is not None:
                body["difficulty"] = difficulty
            return self._call_api("POST", "/questions", json_body=body)

        return StructuredTool.from_function(
            func=add_sync,
            name="error_book_add",
            description=description,
            args_schema=self._ErrorBookAddInput,
        )

    # ═══════════════════════════════════════════
    # 工具 3: error_book_get
    # ═══════════════════════════════════════════

    class _ErrorBookGetInput(BaseModel):
        """error_book_get 的输入参数"""
        id: int = Field(description="错题 ID")

    def _create_get_tool(self) -> StructuredTool:
        description = """查看错题详情。

## 参数
- ``id`` (必填): 错题 ID

## 返回格式
返回错题的完整信息，包括题目内容、正确答案、用户答案、学科、知识点、错因、解析、创建时间、复习状态等。
"""

        def get_sync(id: int) -> str:
            return self._call_api("GET", f"/questions/{id}")

        return StructuredTool.from_function(
            func=get_sync,
            name="error_book_get",
            description=description,
            args_schema=self._ErrorBookGetInput,
        )

    # ═══════════════════════════════════════════
    # 工具 4: error_book_update
    # ═══════════════════════════════════════════

    class _ErrorBookUpdateInput(BaseModel):
        """error_book_update 的输入参数"""
        id: int = Field(description="错题 ID")
        question_text: str | None = Field(default=None, description="题目内容")
        correct_answer: str | None = Field(default=None, description="正确答案")
        user_answer: str | None = Field(default=None, description="用户的错误答案")
        subject: str | None = Field(default=None, description="学科")
        knowledge_point: str | None = Field(default=None, description="知识点")
        error_reason: str | None = Field(default=None, description="错误原因")
        analysis: str | None = Field(default=None, description="题目解析")
        difficulty: int | None = Field(default=None, description="难度等级 1-5", ge=1, le=5)

    def _create_update_tool(self) -> StructuredTool:
        description = """编辑错题信息。只传需要修改的字段即可，未传的字段保持不变。

## 参数
- ``id`` (必填): 错题 ID
- 其他字段均为可选，只传需要修改的

## 使用场景
- 学生发现自己录错了，要求修改
- AI 发现学生录入的解析有误，自动修正
"""

        def update_sync(
            id: int,
            question_text: str | None = None,
            correct_answer: str | None = None,
            user_answer: str | None = None,
            subject: str | None = None,
            knowledge_point: str | None = None,
            error_reason: str | None = None,
            analysis: str | None = None,
            difficulty: int | None = None,
        ) -> str:
            body: dict = {}
            if question_text is not None:
                body["question_text"] = question_text
            if correct_answer is not None:
                body["correct_answer"] = correct_answer
            if user_answer is not None:
                body["user_answer"] = user_answer
            if subject is not None:
                body["subject"] = subject
            if knowledge_point is not None:
                body["knowledge_point"] = knowledge_point
            if error_reason is not None:
                body["error_reason"] = error_reason
            if analysis is not None:
                body["analysis"] = analysis
            if difficulty is not None:
                body["difficulty"] = difficulty
            if not body:
                return "未指定需要修改的字段。"
            return self._call_api("PUT", f"/questions/{id}", json_body=body)

        return StructuredTool.from_function(
            func=update_sync,
            name="error_book_update",
            description=description,
            args_schema=self._ErrorBookUpdateInput,
        )

    # ═══════════════════════════════════════════
    # 工具 5: error_book_delete
    # ═══════════════════════════════════════════

    class _ErrorBookDeleteInput(BaseModel):
        """error_book_delete 的输入参数"""
        id: int = Field(description="错题 ID")

    def _create_delete_tool(self) -> StructuredTool:
        description = """删除错题。

## 参数
- ``id`` (必填): 错题 ID

## 使用场景
- 学生说"把这道题删掉"
"""

        def delete_sync(id: int) -> str:
            return self._call_api("DELETE", f"/questions/{id}")

        return StructuredTool.from_function(
            func=delete_sync,
            name="error_book_delete",
            description=description,
            args_schema=self._ErrorBookDeleteInput,
        )

    # ═══════════════════════════════════════════
    # 工具 6: error_book_review
    # ═══════════════════════════════════════════

    class _ErrorBookReviewInput(BaseModel):
        """error_book_review 的输入参数"""
        id: int = Field(description="错题 ID")
        mastered: bool = Field(description="是否已掌握，true=已掌握，false=未掌握")

    def _create_review_tool(self) -> StructuredTool:
        description = """提交错题复习结果。

## 参数
- ``id`` (必填): 错题 ID
- ``mastered`` (必填): 是否已掌握。true=已掌握，false=未掌握

## 使用场景
- 学生说"这道题我学会了" → mastered=true
- 学生说"这道题还是不会" → mastered=false
"""

        def review_sync(id: int, mastered: bool) -> str:
            return self._call_api("POST", f"/questions/{id}/review", json_body={"mastered": mastered})

        return StructuredTool.from_function(
            func=review_sync,
            name="error_book_review",
            description=description,
            args_schema=self._ErrorBookReviewInput,
        )

    # ═══════════════════════════════════════════
    # 工具 7: error_book_stats
    # ═══════════════════════════════════════════

    def _create_stats_tool(self) -> StructuredTool:
        description = """查看错题统计，按学科统计各学科错题数量，以及总错题数。

## 使用场景
- 学生说"我的错题统计"
- 想要了解学生在各学科的表现
"""

        def stats_sync() -> str:
            return self._call_api("GET", "/stats")

        return StructuredTool.from_function(
            func=stats_sync,
            name="error_book_stats",
            description=description,
        )

    # ═══════════════════════════════════════════
    # 工具 8: error_book_weak
    # ═══════════════════════════════════════════

    def _create_weak_tool(self) -> StructuredTool:
        description = """分析学生的薄弱知识点。

## 返回格式
返回薄弱知识点列表，包含知识点名称、错误次数、薄弱程度等信息。

## 使用场景
- 学生说"帮我看看我哪里最薄弱"
- 需要针对性地推荐练习内容
"""

        def weak_sync() -> str:
            return self._call_api("GET", "/weak-points")

        return StructuredTool.from_function(
            func=weak_sync,
            name="error_book_weak",
            description=description,
        )

    # ═══════════════════════════════════════════
    # 工具 9: error_book_review_q
    # ═══════════════════════════════════════════

    class _ErrorBookReviewQInput(BaseModel):
        """error_book_review_q 的输入参数"""
        page: int = Field(default=1, description="页码，从 1 开始", ge=1)
        page_size: int = Field(default=20, description="每页条数，默认 20，最大 100", ge=1, le=100)

    def _create_review_q_tool(self) -> StructuredTool:
        description = """获取待复习的错题队列。返回需要复习的错题列表。

## 参数
- ``page`` (可选): 页码，默认 1
- ``page_size`` (可选): 每页条数，默认 20，最大 100

## 使用场景
- 学生说"我今天要复习哪些题"
- 安排复习计划
"""

        def review_q_sync(page: int = 1, page_size: int = 20) -> str:
            return self._call_api("GET", "/review", params={"page": page, "page_size": page_size})

        return StructuredTool.from_function(
            func=review_q_sync,
            name="error_book_review_q",
            description=description,
            args_schema=self._ErrorBookReviewQInput,
        )


# ═══════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════


def create_error_book_middleware(
    api_base_url: str | None = None,
    default_headers: dict | None = None,
) -> ErrorBookMiddleware:
    """创建错题本中间件的便捷函数

    Args:
        api_base_url: 后端 API 基础 URL。默认从环境变量
            ``ERROR_BOOK_API_URL`` 读取，或回退到 ``https://api.yachenyanyi.top``。
        default_headers: 附加的默认请求头（可选）

    Returns:
        ErrorBookMiddleware 实例
    """
    return ErrorBookMiddleware(api_base_url=api_base_url, default_headers=default_headers)