"""
移动端操作中间件 - 输出「思考过程 + JSON 执行指令」格式
类似于 AutoGLM 的输出模式
"""
from typing import Any, Dict, List, Optional
from langchain.agents.middleware import AgentMiddleware
from langchain.messages import AIMessage
from langchain_core.messages import ToolMessage
import json
import re


class MobileActionMiddleware(AgentMiddleware):
    """
    移动端操作中间件

    核心功能：
    1. 解析 AI 回复中的思考链和 JSON 行动
    2. 过滤消息历史：只保存结构化 JSON 行动，移除冗长的思考链
    3. 检测 Finish 信号，标记任务完成状态
    """

    def __init__(self):
        super().__init__()

    def after_agent(self, state: Dict[str, Any], runtime: Any) -> Dict[str, Any]:
        """在 agent 执行后调用，格式化输出并过滤消息历史

        核心功能：
        1. 解析 AI 回复中的思考链和 JSON 行动
        2. 过滤消息历史：只保存结构化 JSON 行动，移除冗长的思考链
        3. 思考链保存到 additional_kwargs 供调试使用
        4. 检测 Finish 信号，标记任务完成状态
        """
        messages = state.get("messages", [])

        if not messages:
            return {}

        # 获取最新的 AI 回复
        last_message = messages[-1]

        if isinstance(last_message, AIMessage):
            content = last_message.content
            tool_calls = getattr(last_message, 'tool_calls', [])

            # 如果有工具调用，格式化为 JSON 行动指令
            if tool_calls:
                formatted_output = self._format_tool_calls_as_action(tool_calls, content)

                # 过滤消息：只保存行动，移除思考链
                clean_messages = self._clean_messages(messages, formatted_output)

                # 检测是否为 Finish 信号
                is_finish = self._is_finish_action(formatted_output.get("actions", []))

                return {
                    "messages": clean_messages,
                    "formatted_response": formatted_output,
                    "is_finish": is_finish  # 添加完成标记
                }

            # 否则尝试解析内容中的 JSON
            json_actions = self._extract_json_actions(content)
            if json_actions:
                thought = self._extract_thought(content)
                formatted_output = {
                    "thought": thought,
                    "actions": json_actions
                }

                # 过滤消息：只保存行动，移除思考链
                clean_messages = self._clean_messages(messages, formatted_output)

                # 检测是否为 Finish 信号
                is_finish = self._is_finish_action(json_actions)

                return {
                    "messages": clean_messages,
                    "formatted_response": formatted_output,
                    "is_finish": is_finish  # 添加完成标记
                }

        return {}

    def _is_finish_action(self, actions: List[Dict]) -> bool:
        """检测行动中是否包含 Finish 信号

        Args:
            actions: 行动列表

        Returns:
            如果包含 Finish 行动则返回 True
        """
        for action in actions:
            # 检查 action 字段
            if action.get("action", "").lower() == "finish":
                return True
            # 检查_metadata 字段
            if action.get("_metadata", "").lower() == "finish":
                return True
        return False

    def _clean_messages(self, messages: List, formatted_output: Dict) -> List:
        """过滤消息历史，只保存结构化行动，移除思考链

        Args:
            messages: 原始消息列表
            formatted_output: 解析后的输出（包含 thought 和 actions）

        Returns:
            过滤后的消息列表
        """
        if not messages:
            return messages

        last_message = messages[-1]

        if not isinstance(last_message, AIMessage):
            return messages

        # 创建干净的消息内容：只保存 JSON 行动
        thought = formatted_output.get("thought", "")
        actions = formatted_output.get("actions", [])

        if actions:
            # 只保存第一个行动作为消息内容（简洁）
            clean_content = json.dumps(actions[0], ensure_ascii=False)
        else:
            # 如果没有行动，保留原始内容
            clean_content = last_message.content

        # 创建新消息替换原消息
        new_last_message = AIMessage(
            content=clean_content,  # 只保存行动 JSON
            additional_kwargs={
                "thought": thought,  # 思考链保存到 metadata 供调试
                "actions": actions
            },
            id=getattr(last_message, 'id', None),
            tool_calls=last_message.tool_calls if hasattr(last_message, 'tool_calls') else []
        )

        # 返回过滤后的消息列表
        return messages[:-1] + [new_last_message]

    def _format_tool_calls_as_action(self, tool_calls: List[Dict], content: str) -> Dict:
        """将工具调用格式化为行动指令"""
        actions = []
        for tc in tool_calls:
            action = {
                "action": tc.get("name", "unknown"),
                "element": tc.get("args", {}).get("element", []),
                "_metadata": "do"
            }
            actions.append(action)

        thought = self._extract_thought(content)

        return {
            "thought": thought,
            "actions": actions
        }

    def _extract_json_actions(self, content: str) -> List[Dict]:
        """从内容中提取 JSON 行动"""
        actions = []

        # 匹配 ```json 代码块 - 支持嵌套对象
        # 使用更灵活的匹配方式，匹配完整的 JSON 代码块
        json_block_pattern = r'```json\s*([\s\S]*?)\s*```'
        matches = re.findall(json_block_pattern, content)

        for match in matches:
            # 尝试直接解析整个内容
            try:
                # 可能包含多个 JSON 对象，尝试逐个解析
                match = match.strip()
                if match.startswith('{') and match.endswith('}'):
                    # 单个 JSON 对象
                    action = json.loads(match)
                    if isinstance(action, dict) and 'action' in action:
                        actions.append(action)
                elif match.startswith('[') and match.endswith(']'):
                    # JSON 数组
                    arr = json.loads(match)
                    for item in arr:
                        if isinstance(item, dict) and 'action' in item:
                            actions.append(item)
            except json.JSONDecodeError:
                # 尝试提取单个 JSON 对象（处理嵌套）
                try:
                    # 使用平衡括号法提取完整 JSON 对象
                    json_obj = self._extract_balanced_json(match)
                    if json_obj and 'action' in json_obj:
                        actions.append(json_obj)
                except:
                    pass

        # 如果没有找到 ```json 格式，尝试直接匹配 JSON 对象
        if not actions:
            # 使用平衡括号法提取完整 JSON 对象（支持嵌套 params）
            json_obj = self._extract_balanced_json(content)
            if json_obj and 'action' in json_obj:
                actions.append(json_obj)

        return actions

    def _extract_balanced_json(self, text: str) -> Optional[Dict]:
        """从文本中提取平衡的 JSON 对象（支持嵌套）"""
        # 找到第一个 { 的位置
        start = text.find('{')
        if start == -1:
            return None

        # 计算括号平衡
        depth = 0
        in_string = False
        escape_next = False

        for i in range(start, len(text)):
            char = text[i]

            if escape_next:
                escape_next = False
                continue

            if char == '\\':
                escape_next = True
                continue

            if char == '"' and not escape_next:
                in_string = not in_string
                continue

            if not in_string:
                if char == '{':
                    depth += 1
                elif char == '}':
                    depth -= 1
                    if depth == 0:
                        # 找到完整的 JSON 对象
                        json_str = text[start:i+1]
                        try:
                            return json.loads(json_str)
                        except json.JSONDecodeError:
                            return None

        return None

    def _extract_thought(self, content: str) -> str:
        """从内容中提取思考过程"""
        # 移除 JSON 代码块 - 使用平衡括号法
        thought = content

        # 先移除 ```json 代码块
        def replace_json_block(match):
            return ''

        json_block_pattern = r'```json\s*[\s\S]*?\s*```'
        thought = re.sub(json_block_pattern, '', thought)

        # 再移除独立的 JSON 对象（支持嵌套）
        # 找到并移除所有完整的 JSON 对象
        result = []
        i = 0
        while i < len(thought):
            if thought[i] == '{':
                # 尝试找到平衡的 JSON 对象
                json_obj = self._extract_balanced_json(thought[i:])
                if json_obj:
                    # 跳过这个 JSON 对象
                    json_str = json.dumps(json_obj, ensure_ascii=False)
                    i += len(json_str) + (thought[i:].find(json_str) if json_str in thought[i:] else 0)
                    # 更精确地跳过
                    depth = 0
                    in_string = False
                    escape_next = False
                    start = i
                    for j in range(i, len(thought)):
                        char = thought[j]
                        if escape_next:
                            escape_next = False
                            continue
                        if char == '\\':
                            escape_next = True
                            continue
                        if char == '"' and not escape_next:
                            in_string = not in_string
                            continue
                        if not in_string:
                            if char == '{':
                                depth += 1
                            elif char == '}':
                                depth -= 1
                                if depth == 0:
                                    i = j + 1
                                    break
                else:
                    result.append(thought[i])
                    i += 1
            else:
                result.append(thought[i])
                i += 1

        thought = ''.join(result)

        # 清理空白
        thought = thought.strip()
        return thought if thought else "分析界面中..."


# 创建中间件实例
mobile_action_middleware = MobileActionMiddleware()
