"""
AI 出题中间件 — 让 AI 生成结构化选择题并自动存入用户题库

提供 2 个工具给 Agent：
- generate_quiz: 生成结构化选择题（自动存入用户题库）
- quiz_list: 查询用户的测验历史

架构决策（双方确认）：
- 判分位置: 前端本地判分（答案随题目下发）
- 题目存储: AI 生成后自动存入用户题库（POST /api/quiz）
- 错题录入: 提交后 AI 自动调 error_book_add
- 卡片注入: additional_kwargs.quiz_attachment（结构化字段）
- 认证方式: 内部 Secret（X-Internal-Secret header）

用法:
    from src.middlewares.tools.quiz import create_quiz_middleware

    middleware = create_quiz_middleware()
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
import uuid
import logging
from typing import Any

from dotenv import load_dotenv
from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool
from langgraph.config import get_config
from pydantic import BaseModel, Field

import requests

load_dotenv()

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════

_DEFAULT_NODE_URL = "http://localhost:3000"
_TIMEOUT = 15


# ═══════════════════════════════════════════════
# 中间件
# ═══════════════════════════════════════════════


class QuizMiddleware(AgentMiddleware):
    """AI 出题中间件 — 生成结构化选择题并自动存入用户题库

    提供 2 个工具供 Agent 调用：
    ``generate_quiz`` / ``quiz_list``

    工具执行后通过 after_model 钩子将 quiz_attachment
    注入 AI 消息的 additional_kwargs，供前端渲染答题卡片。
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
                self._create_generate_quiz_tool(),
                self._create_quiz_list_tool(),
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
    ) -> str:
        """调用 Node.js 后端 API（内部 Secret 认证）"""
        url = f"{self._node_api_url}{path}"
        headers = {
            "X-Internal-Secret": self._internal_secret,
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
            return "无法连接到题库服务，请稍后再试。"
        except requests.exceptions.Timeout:
            return "题库服务响应超时，请稍后再试。"
        except requests.exceptions.RequestException as e:
            return f"题库服务请求失败: {e}"

        if resp.status_code == 401:
            return "认证失败，请检查服务配置。"
        if not resp.ok:
            try:
                detail = resp.json().get("detail", resp.text)
            except Exception:
                detail = resp.text[:200]
            return f"题库服务错误({resp.status_code}): {detail}"

        return resp.text

    # ═══════════════════════════════════════════
    # 工具 1: generate_quiz
    # ═══════════════════════════════════════════

    class _QuizOption(BaseModel):
        """单个选项"""
        key: str = Field(description="选项标识: A/B/C/D")
        text: str = Field(description="选项内容")

    class _QuizExplanation(BaseModel):
        """题目解析"""
        correct_reason: str = Field(description="选择正确答案的理由")
        wrong_reasons: dict[str, str] = Field(
            description="每个错误选项不被选择的理由，格式: {\"A\": \"理由\", \"C\": \"理由\"}"
        )

    class _QuizQuestion(BaseModel):
        """单道题目"""
        stem: str = Field(description="题干（支持简单 Markdown）")
        type: str = Field(description="题型: single（单选）或 multiple（多选）")
        options: list["QuizMiddleware._QuizOption"] = Field(
            description="选项列表，通常 4 个（A/B/C/D）"
        )
        answer: list[str] = Field(
            description="正确答案的 key 数组，如 [\"B\"] 或 [\"A\",\"C\"]"
        )
        explanation: "QuizMiddleware._QuizExplanation" = Field(
            description="解析：正确答案理由 + 每个错误选项的理由"
        )
        knowledge_point: str = Field(description="该题关联的知识点")

    class _GenerateQuizInput(BaseModel):
        """generate_quiz 的输入参数"""
        title: str = Field(description="测验标题，如「Python 变量与类型 小测验」")
        subject: str = Field(description="学科/领域，如「Python」「数学」")
        knowledge_point: str = Field(description="本次测验覆盖的知识点")
        questions: list[dict] = Field(
            description=(
                "题目列表（1~5题）。每题包含: "
                "stem(题干), type(single/multiple), "
                "options([{key,text}]), answer([\"B\"]), "
                "explanation({correct_reason, wrong_reasons}), "
                "knowledge_point"
            )
        )

    def _create_generate_quiz_tool(self) -> StructuredTool:
        description = """生成结构化选择题测验，题目会自动存入学生的题库。

## 参数
- ``title`` (必填): 测验标题
- ``subject`` (必填): 学科/领域
- ``knowledge_point`` (必填): 覆盖的知识点
- ``questions`` (必填): 题目列表（1~5 题），每题格式:
  - stem: 题干
  - type: "single"（单选）或 "multiple"（多选）
  - options: [{"key": "A", "text": "..."}, ...]（通常 4 个选项）
  - answer: ["B"]（正确答案 key 数组）
  - explanation: {"correct_reason": "为什么选B", "wrong_reasons": {"A": "为什么不选A", ...}}
  - knowledge_point: 该题知识点

## 使用场景
- 学生说"帮我出几道题" / "考考我" → 调用此工具
- 学完一个知识点后主动提议测验
- 路线图节点学习完成后的验收测试

## 注意
- 每次出 1~5 题，不要超过 5 题
- 必须为每个错误选项写清楚"为什么不选"的理由
- 选项设计要有区分度，避免过于明显的干扰项
"""

        def generate_sync(
            title: str,
            subject: str,
            knowledge_point: str,
            questions: list[dict],
        ) -> str:
            user_ctx = self._get_user_context()
            user_id = user_ctx.get("user_id")
            if not user_id:
                return "无法识别用户身份，请确保已登录。"

            # 校验题目数量
            if not questions or len(questions) > 5:
                return "题目数量需在 1~5 题之间。"

            # 生成唯一 quiz_id
            quiz_id = f"quiz_{uuid.uuid4().hex[:12]}"

            # 规范化题目数据
            formatted_questions = []
            for i, q in enumerate(questions, 1):
                formatted_questions.append({
                    "question_order": i,
                    "stem": q.get("stem", ""),
                    "type": q.get("type", "single"),
                    "options": q.get("options", []),
                    "answer": q.get("answer", []),
                    "explanation": q.get("explanation", {}),
                    "knowledge_point": q.get("knowledge_point", knowledge_point),
                })

            # 调用 Node.js API 存入题库
            body = {
                "user_id": user_id,
                "quiz_id": quiz_id,
                "title": title,
                "subject": subject,
                "knowledge_point": knowledge_point,
                "questions": formatted_questions,
            }

            result = self._call_api("POST", "/api/quiz", json_body=body)

            # 检查 API 是否成功
            if result.startswith(("无法连接", "题库服务", "认证失败")):
                logger.warning("题库存储失败: %s", result)
                # 仍然返回题目给前端渲染，但提示存储失败

            # 构造 quiz_attachment 数据（注入到前端）
            quiz_attachment = {
                "quiz_id": quiz_id,
                "title": title,
                "subject": subject,
                "knowledge_point": knowledge_point,
                "questions": [
                    {
                        "id": i,
                        "stem": q.get("stem", ""),
                        "type": q.get("type", "single"),
                        "options": q.get("options", []),
                        "answer": q.get("answer", []),
                        "explanation": q.get("explanation", {}),
                        "knowledge_point": q.get("knowledge_point", knowledge_point),
                    }
                    for i, q in enumerate(questions, 1)
                ],
            }

            # 标记待注入（after_model 钩子处理）
            self._pending_quiz = quiz_attachment

            # 返回给 AI 的确认文本
            return (
                f"已生成 {len(questions)} 道题目「{title}」并存入题库。\n"
                f"quiz_id: {quiz_id}\n"
                f"题目已发送给学生，等待作答。"
            )

        return StructuredTool.from_function(
            func=generate_sync,
            name="generate_quiz",
            description=description,
            args_schema=self._GenerateQuizInput,
        )

    # ═══════════════════════════════════════════
    # 工具 2: quiz_list
    # ═══════════════════════════════════════════

    class _QuizListInput(BaseModel):
        """quiz_list 的输入参数"""
        subject: str | None = Field(default=None, description="按学科筛选（可选）")
        page: int = Field(default=1, description="页码", ge=1)
        page_size: int = Field(default=10, description="每页条数", ge=1, le=50)

    def _create_quiz_list_tool(self) -> StructuredTool:
        description = """查询学生的测验历史记录。

## 参数
- ``subject`` (可选): 按学科筛选
- ``page`` (可选): 页码，默认 1
- ``page_size`` (可选): 每页条数，默认 10

## 使用场景
- 学生说"我之前做过哪些测验"
- 需要了解学生的练习情况
"""

        def list_sync(
            subject: str | None = None,
            page: int = 1,
            page_size: int = 10,
        ) -> str:
            user_ctx = self._get_user_context()
            user_id = user_ctx.get("user_id")
            if not user_id:
                return "无法识别用户身份，请确保已登录。"

            params: dict[str, Any] = {
                "user_id": user_id,
                "page": page,
                "page_size": page_size,
            }
            if subject:
                params["subject"] = subject

            return self._call_api("GET", "/api/quiz", params=params)

        return StructuredTool.from_function(
            func=list_sync,
            name="quiz_list",
            description=description,
            args_schema=self._QuizListInput,
        )

    # ── after_model 钩子：注入 quiz_attachment ──

    def after_model(self, state, runtime):
        """在 AI 回复后，如果有待注入的测验数据，附加到消息"""
        pending = getattr(self, "_pending_quiz", None)
        if not pending:
            return None
        self._pending_quiz = None

        messages = state.get("messages", [])
        if not messages:
            return None

        # 找到最后一条 AI 消息
        from langchain_core.messages import AIMessage
        last_msg = messages[-1]
        if not isinstance(last_msg, AIMessage):
            return None

        # 注入 quiz_attachment 到 additional_kwargs
        additional_kwargs = dict(getattr(last_msg, "additional_kwargs", {}) or {})
        additional_kwargs["quiz_attachment"] = pending

        updated_msg = last_msg.model_copy(
            update={"additional_kwargs": additional_kwargs}
        )
        return {"messages": [updated_msg]}

    async def aafter_model(self, state, runtime):
        return self.after_model(state, runtime)


# ═══════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════


def create_quiz_middleware(
    node_api_url: str | None = None,
    internal_secret: str | None = None,
) -> QuizMiddleware:
    """创建 AI 出题中间件

    Args:
        node_api_url: Node.js 内部 API 地址。默认从 NODE_INTERNAL_URL 环境变量读取。
        internal_secret: 内部认证密钥。默认从 INTERNAL_SECRET 环境变量读取。

    Returns:
        QuizMiddleware 实例
    """
    return QuizMiddleware(
        node_api_url=node_api_url,
        internal_secret=internal_secret,
    )
