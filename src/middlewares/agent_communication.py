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

from typing import Annotated, TypedDict

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
                self._create_check_tool(),
                self._create_thread_info_tool(),
            ]
        return self._tools_cache

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

        description = f"""向同事发送消息（后台运行，不等待结果）。

## 同事列表
{employee_list}

## 使用方式
- 发送消息给同事，任务在后台运行
- 返回 run_id，可用 check_colleague 查询结果

## 参数说明
- `colleague`: 选择专业领域匹配的同事
- `message`: 你要发送的完整消息（自行组织内容）
- `new_thread`: 是否开始新对话（默认 False，继续之前对话）

## 示例
```python
# 发送消息
collaborate(colleague="researcher_agent", message="请调研...")
# 返回: run_id = "xxx", thread_id = "yyy"

# 然后查询结果
check_colleague(colleague="researcher_agent", wait=True, timeout=60)
```"""

        def collaborate(
            colleague: Annotated[str, "同事名称"],
            message: Annotated[str, "要发送的完整消息内容"],
            new_thread: Annotated[bool, "是否开始新对话。默认 False，继续之前的对话历史"] = False,
            config: dict | None = None,
        ) -> str:
            """向同事发送消息（同步版本，返回提示信息）"""
            return "请使用异步版本 collaborate 进行协作。"

        async def collaborate_async(
            colleague: str,
            message: str,
            new_thread: bool = False,
            config: dict | None = None,
        ) -> str:
            """向同事发送消息（异步版本，后台运行）

            Args:
                colleague: 同事名称（对应 langgraph.json 中的 graph_id）
                message: 要发送的完整消息内容
                new_thread: 是否开始新对话
                config: 可选的运行配置，传递给 LangGraph API

            Returns:
                包含 run_id 和 thread_id 的信息，可用 check_colleague 查询结果
            """
            if not HAS_SDK:
                return "错误：需要安装 langgraph-sdk 库才能使用此功能"

            if colleague not in self.employees:
                available = ", ".join(self.employees.keys())
                return f"错误：未找到同事 '{colleague}'。可用同事: {available}"

            if colleague == self.current_employee:
                return "提示：不能向自己协作，请选择其他同事。"

            # 获取或创建 thread_id
            if new_thread:
                thread_id = await self.thread_manager.create_new_thread(colleague)
            else:
                thread_info = await self.thread_manager.get_thread(colleague)
                thread_id = thread_info.current

            try:
                client = get_client(url=self.server_url)

                input_data = {"messages": [HumanMessage(content=message)]}

                # 发送消息（后台运行）
                run = await client.runs.create(
                    thread_id,
                    colleague,
                    input=input_data,
                    config=config,
                )
                run_id = run["run_id"]

                return f"消息已发送给 [{colleague}]\n- run_id: {run_id}\n- thread_id: {thread_id}\n\n使用 check_colleague(colleague=\"{colleague}\", run_id=\"{run_id}\", wait=True) 查询结果。\n重要：传入 run_id 可确保获取这次任务的回复。"

            except Exception as e:
                return f"发送给 {colleague} 失败: {str(e)}"

        return StructuredTool.from_function(
            func=collaborate,
            coroutine=collaborate_async,
            name="collaborate",
            description=description,
        )

    def _create_check_tool(self):
        """创建查询同事回复的工具"""

        employee_list = self._build_employee_list_description()

        description = f"""查询同事的回复或任务状态。

## 同事列表
{employee_list}

## 参数说明
- `colleague`: 同事名称
- `run_id`: 可选，collaborate 返回的 run_id（用于跟踪特定任务）
- `wait`: 是否等待结果（默认 True，False 则只检查当前状态）
- `timeout`: 最大等待秒数（默认 60 秒，仅 wait=True 时有效）

## 使用场景
- collaborate 发送消息后，查询结果
- 检查后台任务是否完成

## 重要：使用 run_id 跟踪特定任务
发送消息时会返回 run_id，查询时传入 run_id 可以：
- 确保获取的是这次任务的回复（不是旧的）
- 知道任务是否完成

## 返回状态
- 有回复：返回同事的回答
- 处理中：返回当前状态
- 出错：返回错误信息"""

        def check_colleague(
            colleague: Annotated[str, "同事名称"],
            run_id: Annotated[str | None, "可选，collaborate 返回的 run_id，用于跟踪特定任务"] = None,
            wait: Annotated[bool, "是否等待结果。默认 True，False 则只检查当前状态"] = True,
            timeout: Annotated[int, "最大等待秒数。默认 60 秒"] = 60,
        ) -> str:
            """查询同事回复（同步版本，返回提示信息）"""
            return "请使用异步版本 check_colleague 进行查询。"

        async def check_colleague_async(
            colleague: str,
            run_id: str | None = None,
            wait: bool = True,
            timeout: int = 60,
        ) -> str:
            """查询同事的回复（异步版本）"""
            if not HAS_SDK:
                return "错误：需要安装 langgraph-sdk 库才能使用此功能"

            if colleague not in self.employees:
                available = ", ".join(self.employees.keys())
                return f"错误：未找到同事 '{colleague}'。可用同事: {available}"

            try:
                client = get_client(url=self.server_url)

                # 获取线程信息
                thread_info = await self.thread_manager.get_thread(colleague)
                thread_id = thread_info.current

                # 如果提供了 run_id，先检查这个 run 的状态
                if run_id:
                    run_status = await client.runs.get(thread_id, run_id)
                    status = run_status.get("status")

                    if status == "pending":
                        if not wait:
                            return f"[{colleague}] 任务排队中..."
                        # 继续等待
                    elif status == "running":
                        if not wait:
                            return f"[{colleague}] 任务执行中..."
                        # 继续等待
                    elif status == "error":
                        return f"[{colleague}] 任务执行出错"
                    elif status == "cancelled":
                        return f"[{colleague}] 任务被取消"
                    elif status == "success":
                        # 任务完成，获取结果
                        state = await client.threads.get_state(thread_id)
                        messages = state.get("values", {}).get("messages", [])
                        last_ai_msg = None
                        for msg in reversed(messages):
                            msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
                            if msg_type == "ai":
                                last_ai_msg = msg
                                break
                        if last_ai_msg:
                            if isinstance(last_ai_msg, dict):
                                content = last_ai_msg.get("content", "")
                            else:
                                content = getattr(last_ai_msg, "content", str(last_ai_msg))
                            return f"[{colleague}]\n{content}"
                        return f"[{colleague}] 任务完成但无回复"

                if not wait:
                    # 不等待，只检查当前状态
                    state = await client.threads.get_state(thread_id)
                    messages = state.get("values", {}).get("messages", [])

                    last_ai_msg = None
                    for msg in reversed(messages):
                        msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
                        if msg_type == "ai":
                            last_ai_msg = msg
                            break

                    if last_ai_msg:
                        if isinstance(last_ai_msg, dict):
                            content = last_ai_msg.get("content", "")
                        else:
                            content = getattr(last_ai_msg, "content", str(last_ai_msg))
                        return f"[{colleague}]\n{content}（注意：可能是旧回复，建议传入 run_id 跟踪特定任务）"
                    else:
                        return f"[{colleague}] 暂无回复，任务可能还在处理中"

                # 等待结果（轮询）
                import asyncio
                poll_interval = 2
                waited = 0

                while waited < timeout:
                    # 如果有 run_id，检查这个 run 的状态
                    if run_id:
                        run_status = await client.runs.get(thread_id, run_id)
                        status = run_status.get("status")

                        if status == "success":
                            # 任务完成
                            state = await client.threads.get_state(thread_id)
                            messages = state.get("values", {}).get("messages", [])
                            last_ai_msg = None
                            for msg in reversed(messages):
                                msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
                                if msg_type == "ai":
                                    last_ai_msg = msg
                                    break
                            if last_ai_msg:
                                if isinstance(last_ai_msg, dict):
                                    content = last_ai_msg.get("content", "")
                                else:
                                    content = getattr(last_ai_msg, "content", str(last_ai_msg))
                                return f"[{colleague}]\n{content}"
                            return f"[{colleague}] 任务完成但无回复"
                        elif status == "error":
                            return f"[{colleague}] 任务执行出错"
                        elif status == "cancelled":
                            return f"[{colleague}] 任务被取消"
                    else:
                        # 没有 run_id，检查是否有新回复
                        state = await client.threads.get_state(thread_id)
                        messages = state.get("values", {}).get("messages", [])

                        last_ai_msg = None
                        for msg in reversed(messages):
                            msg_type = msg.get("type") if isinstance(msg, dict) else getattr(msg, "type", None)
                            if msg_type == "ai":
                                last_ai_msg = msg
                                break

                        if last_ai_msg:
                            if isinstance(last_ai_msg, dict):
                                content = last_ai_msg.get("content", "")
                            else:
                                content = getattr(last_ai_msg, "content", str(last_ai_msg))
                            return f"[{colleague}]\n{content}"

                    # 继续等待
                    await asyncio.sleep(poll_interval)
                    waited += poll_interval

                # 超时
                if run_id:
                    run_status = await client.runs.get(thread_id, run_id)
                    status = run_status.get("status")
                    return f"[{colleague}] 处理超时（已等待 {timeout} 秒），当前状态: {status}\n可再次调用 check_colleague 或增加 timeout 参数。"
                else:
                    return f"[{colleague}] 处理超时（已等待 {timeout} 秒），任务仍在后台运行。\n建议：使用 collaborate 返回的 run_id 来跟踪特定任务。"

            except Exception as e:
                return f"查询 {colleague} 失败: {str(e)}"

        return StructuredTool.from_function(
            func=check_colleague,
            coroutine=check_colleague_async,
            name="check_colleague",
            description=description,
        )

    def _create_thread_info_tool(self):
        """创建线程信息查询工具"""

        description = """查询员工（代理）的线程信息，包括当前线程和历史对话。

可以查看某个员工的对话历史记录，帮助了解之前的工作内容。"""

        def get_thread_info(
            colleague: Annotated[str, "要查询的同事名称，不填则查询所有"] = "",
        ) -> str:
            """查询线程信息（同步版本，仅读取本地配置）"""

            if colleague:
                if colleague not in self.employees:
                    return f"错误：未找到同事 '{colleague}'"
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