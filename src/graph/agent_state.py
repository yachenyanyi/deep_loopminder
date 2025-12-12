from langchain_core.messages import (
    BaseMessage,
    AIMessage,
    HumanMessage,
    SystemMessage,
    merge_message_runs,
)
from typing_extensions import TypedDict, List, Literal,Optional, Dict, Any,Union

class AgentState(TypedDict):
    # Mem0 相关的系统消息（多条，可滚动）
    mem0_message: List[SystemMessage]
    # 对话历史（仅 Human/AI）
    conversation_history: List[BaseMessage]
    last_output: str
    current_agent: str
    iterations: int
    user_id: str
    max_history: Optional[int]          # 控制 conversation_history 长度
    max_mem0: Optional[int]             # 控制 mem0_message 长度（新增）
    config: dict
    done: bool