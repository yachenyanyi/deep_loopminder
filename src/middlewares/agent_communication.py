"""
AI 公司 - 员工通信中间件

每个 agent 就像公司的员工，有自己的职责和专业领域。
员工之间可以相互通信、协作完成任务。

核心概念：
- 每个 agent 是独立的员工，有自己的职责
- 员工之间可以相互发送消息、请求帮助
- 每个员工有独立的对话历史和工作记忆（通过 threads.json 管理）
- 支持任务委派和协作
"""

from typing import Annotated, Any, TypedDict
import time

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool
from langchain_core.messages import HumanMessage

from .thread_config import get_thread_config_manager

try:
    from langgraph_sdk import get_client
    HAS_SDK = True
except ImportError:
    HAS_SDK = False


class Employee(TypedDict):
    """员工定义"""
    name: str                    # 员工名称（langgraph.json 中的 graph 名称）
    role: str                    # 角色描述（如：前端工程师、数据分析师）
    expertise: list[str]         # 专业领域
    description: str             # 详细描述


class AgentCommunicationMiddleware(AgentMiddleware):
    """AI 公司 - 员工通信中间件

    线程管理：
    - 每个员工有独立的 thread_id，存储在 threads.json 中
    - 员工之间的对话历史相互独立，互不干扰
    - 支持查看和切换历史对话

    使用方法：
    ```python
    from src.middlewares.agent_communication import AgentCommunicationMiddleware, Employee

    employees = [
        Employee(
            name="coder_agent",
            role="高级工程师",
            expertise=["编程", "调试", "代码审查"],
            description="负责所有代码相关的任务。"
        ),
    ]

    middleware = AgentCommunicationMiddleware(
        server_url="http://127.0.0.1:2024",
        employees=employees,
        current_employee="chat_agent",
    )
    ```
    """

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:2024",
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

        # 线程配置管理器（传入 server_url 以便注册线程）
        self.thread_manager = get_thread_config_manager(server_url=server_url)

        # 缓存 assistant_id 映射 {graph_id: assistant_id}
        self._assistant_cache: dict[str, str] = {}

        # 预创建工具列表
        self._tools_cache = None

    def __getstate__(self):
        """排除不可序列化的属性"""
        state = self.__dict__.copy()
        # _tools_cache 包含 StructuredTool 对象，可能有不可序列化的属性
        # thread_manager 是全局单例，不需要序列化
        state.pop('_tools_cache', None)
        state.pop('thread_manager', None)
        return state

    def __setstate__(self, state):
        """恢复被排除的属性"""
        self.__dict__.update(state)
        # 重新获取全局单例
        self.thread_manager = get_thread_config_manager(server_url=self.server_url)
        # _tools_cache 会在下次访问时重新创建
        self._tools_cache = None

    @property
    def tools(self) -> list:
        """返回中间件提供的工具列表（作为属性，兼容 LangChain）"""
        if self._tools_cache is None:
            self._tools_cache = [
                self._create_collaborate_tool(),
                self._create_thread_info_tool(),
            ]
        return self._tools_cache

    async def _get_assistant_id(self, graph_id: str) -> str | None:
        """通过 graph_id 获取 assistant_id（带缓存）"""
        if graph_id in self._assistant_cache:
            return self._assistant_cache[graph_id]

        if not HAS_SDK:
            return None

        try:
            client = get_client(url=self.server_url)
            assistants = await client.assistants.search(graph_id=graph_id)
            if assistants:
                assistant_id = assistants[0]["assistant_id"]
                self._assistant_cache[graph_id] = assistant_id
                return assistant_id
        except Exception as e:
            print(f"获取 assistant_id 失败: {e}")

        return None

    def _build_employee_list_description(self) -> str:
        """构建员工列表描述"""
        lines = []
        for name, emp in self.employees.items():
            expertise = ", ".join(emp.get("expertise", []))
            lines.append(f"- **{name}** ({emp.get('role', '员工')}): {emp.get('description', '')}")
            if expertise:
                lines.append(f"  专业领域: {expertise}")
        return "\n".join(lines)

    def _create_collaborate_tool(self):
        """创建协作工具 - 与同事协作的统一入口"""

        employee_list = self._build_employee_list_description()

        description = f"""与同事协作完成任务或讨论问题。

## 同事列表
{employee_list}

## 使用方式
你可以自由决定如何与同事交流：
- **咨询问题**：描述问题背景，请求建议或答案
- **委派任务**：明确任务要求，让同事独立完成并报告结果
- **分享信息**：传递重要信息给相关同事
- **协作讨论**：多轮对话解决复杂问题

## 参数说明
- `colleague`: 选择专业领域匹配的同事
- `message`: 你要发送的完整消息（自行组织内容）
- `new_thread`: 是否开始新对话（默认 False，继续之前对话）

## 示例
```python
# 咨询代码问题
collaborate(colleague="coder_agent", message="我在实现用户认证时遇到 JWT 过期问题，请帮我看看处理逻辑是否正确...")

# 委派任务
collaborate(colleague="researcher_agent", message="请调研市面上主流的实时通信方案（WebSocket vs SSE vs长轮询），对比优缺点并给出推荐。请详细报告你的调研过程和结论。")

# 继续之前的讨论
collaborate(colleague="coder_agent", message="根据上次讨论的建议，我修改了认证逻辑，现在请帮我审查新的实现...")
```"""

        def collaborate(
            colleague: Annotated[str, "同事名称"],
            message: Annotated[str, "要发送的完整消息内容"],
            new_thread: Annotated[bool, "是否开始新对话。默认 False，继续之前的对话历史"] = False,
            config: dict | None = None,
        ) -> str:
            """与同事协作（同步版本，返回提示信息）"""
            return "请使用异步版本 collaborate 进行协作。"

        async def collaborate_async(
            colleague: str,
            message: str,
            new_thread: bool = False,
            config: dict | None = None,
        ) -> str:
            """与同事协作（异步版本）"""
            if not HAS_SDK:
                return "错误：需要安装 langgraph-sdk 库才能使用此功能"

            if colleague not in self.employees:
                available = ", ".join(self.employees.keys())
                return f"错误：未找到同事 '{colleague}'。可用同事: {available}"

            if colleague == self.current_employee:
                return "提示：不能向自己协作，请选择其他同事。"

            # 获取 assistant_id
            assistant_id = await self._get_assistant_id(colleague)
            if not assistant_id:
                return f"错误：无法找到 {colleague} 的 assistant_id"

            # 获取或创建 thread_id
            if new_thread:
                thread_id = await self.thread_manager.create_new_thread(colleague)
            else:
                thread_info = await self.thread_manager.get_thread(colleague)
                thread_id = thread_info.current

            try:
                client = get_client(url=self.server_url)

                # 直接发送消息，不添加任何固定格式
                input_data = {"messages": [HumanMessage(content=message)]}
                result_content = ""

                async for chunk in client.runs.stream(
                    thread_id,
                    assistant_id,
                    input=input_data,
                ):
                    if chunk.event == "values" and "messages" in chunk.data:
                        msg = chunk.data["messages"][-1]
                        if isinstance(msg, dict):
                            result_content = msg.get("content", "")
                        else:
                            result_content = getattr(msg, "content", str(msg))

                if result_content:
                    return f"[{colleague}]\n{result_content}"
                return f"[{colleague}] 未返回有效内容"

            except Exception as e:
                return f"与 {colleague} 协作失败: {str(e)}"

        return StructuredTool.from_function(
            func=collaborate,
            coroutine=collaborate_async,
            name="collaborate",
            description=description,
        )

    def _create_thread_info_tool(self):
        """创建线程信息查询工具"""

        description = """查询员工（代理）的线程信息，包括当前线程和历史对话。

可以查看某个员工的对话历史记录，帮助了解之前的工作内容。"""

        def get_thread_info(
            colleague: Annotated[str, "要查询的同事名称，不填则查询所有"],
        ) -> str:
            """查询线程信息（同步版本，仅读取本地配置）"""

            if colleague:
                if colleague not in self.employees:
                    return f"错误：未找到同事 '{colleague}'"
                # 使用同步版本（仅读取本地配置）
                all_threads = self.thread_manager.list_all_threads()
                if colleague in all_threads:
                    thread_info = all_threads[colleague]
                    history_count = len(thread_info.history)
                    return f"""{colleague} 的线程信息：
- 当前线程: {thread_info.current}
- 历史对话数: {history_count}"""
                else:
                    return f"未找到 {colleague} 的线程配置"
            else:
                # 查询所有
                all_threads = self.thread_manager.list_all_threads()
                lines = ["所有员工的线程信息："]
                for name, info in all_threads.items():
                    lines.append(f"\n**{name}**")
                    lines.append(f"  当前线程: {info.current}")
                    lines.append(f"  历史对话: {len(info.history)} 个")
                return "\n".join(lines)

        return StructuredTool.from_function(
            func=get_thread_info,
            name="get_thread_info",
            description=description,
        )


def create_agent_communication_middleware(
    server_url: str = "http://127.0.0.1:2024",
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