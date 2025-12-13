from langchain_core.messages import (
    BaseMessage,
    AIMessage,
    HumanMessage,
    SystemMessage,
    merge_message_runs,
)
from typing_extensions import TypedDict, List, Literal,Optional, Dict, Any,Union

class AgentState(TypedDict):

    conversation_history: List[BaseMessage]
    last_output: str
    current_agent: str
    iterations: int
    user_id: str
    max_history: Optional[int]          # 控制 conversation_history 长度
    config: dict
