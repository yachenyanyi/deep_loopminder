"""
AI 公司 - 员工通信中间件

每个 agent 就像公司的员工，有自己的职责和专业领域。
员工之间可以相互通信、协作完成任务。

核心概念：
- 每个 agent 是独立的员工，有自己的职责
- 员工之间可以相互发送消息、请求帮助
- 每个员工有独立的对话历史和工作记忆
- 支持任务委派和协作
"""

from typing import Annotated, Any, TypedDict
import time

from langchain.agents.middleware import AgentMiddleware
from langchain.tools import StructuredTool
from langchain_core.messages import HumanMessage

try:
    from langgraph.pregel.remote import RemoteGraph
except ImportError:
    RemoteGraph = None


class Employee(TypedDict):
    """员工定义"""
    name: str                    # 员工名称（langgraph.json 中的 graph 名称）
    role: str                    # 角色描述（如：前端工程师、数据分析师）
    expertise: list[str]         # 专业领域
    description: str             # 详细描述


class AgentCommunicationMiddleware(AgentMiddleware):
    """AI 公司 - 员工通信中间件

    使用方法：
    ```python
    from src.middlewares.agent_communication import AgentCommunicationMiddleware, Employee

    employees = [
        Employee(
            name="intelligent_deep_agent_mobile",
            role="高级顾问",
            expertise=["问题分析", "决策支持", "知识管理"],
            description="公司的智能顾问，擅长分析复杂问题、提供建议和决策支持。"
        ),
        Employee(
            name="autoglm_agent",
            role="自动化工程师",
            expertise=["手机自动化", "任务执行", "流程自动化"],
            description="专注于手机端自动化任务，可以执行各种手机操作和自动化流程。"
        ),
    ]

    middleware = AgentCommunicationMiddleware(
        server_url="http://localhost:8123",
        employees=employees,
        current_employee="intelligent_deep_agent_mobile",
    )
    ```
    """

    def __init__(
        self,
        server_url: str = "http://localhost:8123",
        employees: list[Employee] | None = None,
        current_employee: str | None = None,
    ):
        """
        Args:
            server_url: LangGraph Server 地址
            employees: 公司员工列表
            current_employee: 当前 agent 的员工名称（用于识别自己，避免向自己发送消息）
        """
        super().__init__()
        self.server_url = server_url
        self.employees = {e["name"]: e for e in (employees or [])}
        self.current_employee = current_employee

    def tools(self) -> list:
        """返回中间件提供的工具列表"""
        return [
            self._create_consult_tool(),
            self._create_delegate_tool(),
        ]

    def _get_employee_thread_id(self, context_thread_id: str, employee_name: str) -> str:
        """为员工生成独立的 thread_id

        格式: emp_{employee_name}_{context_thread_id}
        确保每个员工有独立的对话历史
        """
        return f"emp_{employee_name}_{context_thread_id}"

    def _build_employee_list_description(self) -> str:
        """构建员工列表描述"""
        lines = []
        for name, emp in self.employees.items():
            expertise = ", ".join(emp.get("expertise", []))
            lines.append(f"- **{name}** ({emp.get('role', '员工')}): {emp.get('description', '')}")
            if expertise:
                lines.append(f"  专业领域: {expertise}")
        return "\n".join(lines)

    def _create_consult_tool(self):
        """创建咨询工具 - 向其他员工请求帮助"""

        employee_list = self._build_employee_list_description()

        description = f"""向其他员工咨询问题、请求帮助或分享信息。

## 同事列表
{employee_list}

## 使用场景
- 遇到不熟悉的问题，需要其他专业员工的意见
- 需要其他员工的特定技能或知识
- 想分享信息给相关同事
- 协作解决复杂问题

## 注意事项
- 每个员工有独立的对话历史，可以记住之前的交流
- 清晰描述你的问题，让同事更好地帮助你
- 选择专业领域匹配的同事"""

        def consult(
            colleague: Annotated[str, "同事名称（员工名称）"],
            question: Annotated[str, "要咨询的问题或请求"],
            context: Annotated[str | None, "可选的背景信息，帮助同事理解上下文"] = None,
            config: dict | None = None,
        ) -> str:
            """向同事咨询问题"""
            if RemoteGraph is None:
                return "错误：需要安装 langgraph 库才能使用此功能"

            if colleague not in self.employees:
                available = ", ".join(self.employees.keys())
                return f"错误：未找到同事 '{colleague}'。可用同事: {available}"

            if colleague == self.current_employee:
                return "提示：不能向自己咨询，请选择其他同事。"

            # 获取当前对话的 thread_id
            configurable = (config or {}).get("configurable", {})
            main_thread_id = configurable.get("thread_id", "default")

            # 为目标员工生成独立的 thread_id
            target_thread_id = self._get_employee_thread_id(main_thread_id, colleague)

            # 构建消息
            message = question
            if context:
                message = f"[背景信息]\n{context}\n\n[问题]\n{question}"

            # 调用远程员工
            remote = RemoteGraph(
                assistant_id=colleague,
                url=self.server_url,
            )

            try:
                result = remote.invoke(
                    {"messages": [HumanMessage(content=message)]},
                    config={"configurable": {"thread_id": target_thread_id}},
                )

                if "messages" in result and result["messages"]:
                    response = result["messages"][-1]
                    content = response.content if hasattr(response, "content") else str(response)
                    return f"[{colleague} 回复]\n{content}"
                return str(result)

            except Exception as e:
                return f"联系 {colleague} 失败: {str(e)}"

        async def consult_async(
            colleague: str,
            question: str,
            context: str | None = None,
            config: dict | None = None,
        ) -> str:
            """向同事咨询问题（异步版本）"""
            if RemoteGraph is None:
                return "错误：需要安装 langgraph 库才能使用此功能"

            if colleague not in self.employees:
                available = ", ".join(self.employees.keys())
                return f"错误：未找到同事 '{colleague}'。可用同事: {available}"

            if colleague == self.current_employee:
                return "提示：不能向自己咨询，请选择其他同事。"

            configurable = (config or {}).get("configurable", {})
            main_thread_id = configurable.get("thread_id", "default")
            target_thread_id = self._get_employee_thread_id(main_thread_id, colleague)

            message = question
            if context:
                message = f"[背景信息]\n{context}\n\n[问题]\n{question}"

            remote = RemoteGraph(assistant_id=colleague, url=self.server_url)

            try:
                result = await remote.ainvoke(
                    {"messages": [HumanMessage(content=message)]},
                    config={"configurable": {"thread_id": target_thread_id}},
                )

                if "messages" in result and result["messages"]:
                    response = result["messages"][-1]
                    content = response.content if hasattr(response, "content") else str(response)
                    return f"[{colleague} 回复]\n{content}"
                return str(result)

            except Exception as e:
                return f"联系 {colleague} 失败: {str(e)}"

        return StructuredTool.from_function(
            func=consult,
            coroutine=consult_async,
            name="consult_colleague",
            description=description,
        )

    def _create_delegate_tool(self):
        """创建委派工具 - 将任务完全交给其他员工处理"""

        employee_list = self._build_employee_list_description()

        description = f"""将任务委派给其他员工独立完成。

## 同事列表
{employee_list}

## 使用场景
- 任务更适合其他专业领域的同事
- 需要并行处理多个独立任务
- 任务需要特定员工的技能

## 特点
- 委派后，同事会独立完成任务
- 可以开始新对话或继续之前的工作
- 适合需要同事独立负责的任务"""

        def delegate(
            colleague: Annotated[str, "负责该任务的同事名称"],
            task: Annotated[str, "要委派的任务描述"],
            new_project: Annotated[bool, "是否开始新项目（新对话）。默认 False，继续之前的工作"] = False,
            config: dict | None = None,
        ) -> str:
            """委派任务给同事"""
            if RemoteGraph is None:
                return "错误：需要安装 langgraph 库才能使用此功能"

            if colleague not in self.employees:
                available = ", ".join(self.employees.keys())
                return f"错误：未找到同事 '{colleague}'。可用同事: {available}"

            if colleague == self.current_employee:
                return "提示：不能委派给自己，请选择其他同事。"

            configurable = (config or {}).get("configurable", {})
            main_thread_id = configurable.get("thread_id", "default")

            # 生成 thread_id
            if new_project:
                target_thread_id = f"emp_{colleague}_new_{int(time.time())}"
            else:
                target_thread_id = self._get_employee_thread_id(main_thread_id, colleague)

            # 构建委派消息
            employee = self.employees[colleague]
            role = employee.get("role", "同事")
            message = f"""[任务委派]

作为公司的 {role}，请独立完成以下任务：

{task}

请详细报告你的工作过程和结果。"""

            remote = RemoteGraph(assistant_id=colleague, url=self.server_url)

            try:
                result = remote.invoke(
                    {"messages": [HumanMessage(content=message)]},
                    config={"configurable": {"thread_id": target_thread_id}},
                )

                if "messages" in result and result["messages"]:
                    response = result["messages"][-1]
                    content = response.content if hasattr(response, "content") else str(response)
                    return f"[{colleague} 完成报告]\n{content}"
                return str(result)

            except Exception as e:
                return f"委派给 {colleague} 失败: {str(e)}"

        async def delegate_async(
            colleague: str,
            task: str,
            new_project: bool = False,
            config: dict | None = None,
        ) -> str:
            """委派任务给同事（异步版本）"""
            if RemoteGraph is None:
                return "错误：需要安装 langgraph 库才能使用此功能"

            if colleague not in self.employees:
                return f"错误：未找到同事 '{colleague}'。"

            if colleague == self.current_employee:
                return "提示：不能委派给自己。"

            configurable = (config or {}).get("configurable", {})
            main_thread_id = configurable.get("thread_id", "default")

            if new_project:
                target_thread_id = f"emp_{colleague}_new_{int(time.time())}"
            else:
                target_thread_id = self._get_employee_thread_id(main_thread_id, colleague)

            employee = self.employees[colleague]
            message = f"""[任务委派]

作为公司的 {employee.get('role', '同事')}，请独立完成以下任务：

{task}

请详细报告你的工作过程和结果。"""

            remote = RemoteGraph(assistant_id=colleague, url=self.server_url)

            try:
                result = await remote.ainvoke(
                    {"messages": [HumanMessage(content=message)]},
                    config={"configurable": {"thread_id": target_thread_id}},
                )

                if "messages" in result and result["messages"]:
                    response = result["messages"][-1]
                    content = response.content if hasattr(response, "content") else str(response)
                    return f"[{colleague} 完成报告]\n{content}"
                return str(result)

            except Exception as e:
                return f"委派给 {colleague} 失败: {str(e)}"

        return StructuredTool.from_function(
            func=delegate,
            coroutine=delegate_async,
            name="delegate_task",
            description=description,
        )


def create_agent_communication_middleware(
    server_url: str = "http://localhost:8123",
    employees: list[Employee] | None = None,
    current_employee: str | None = None,
) -> AgentCommunicationMiddleware:
    """创建员工通信中间件的便捷函数

    Args:
        server_url: LangGraph Server 地址
        employees: 公司员工列表
        current_employee: 当前 agent 的员工名称

    Returns:
        AgentCommunicationMiddleware 实例
    """
    return AgentCommunicationMiddleware(
        server_url=server_url,
        employees=employees,
        current_employee=current_employee,
    )