from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field
import os
import mimetypes
import asyncio
import base64
import json
import re
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.tools import load_mcp_tools
import atexit
from typing import List, Dict, Any
from difflib import SequenceMatcher
import langchain_mcp_adapters.tools
import langchain_mcp_adapters.client
from langchain_mcp_adapters.sessions import create_session

# Monkey Patch to fix UnboundLocalError in langchain_mcp_adapters.tools.load_mcp_tools
async def _patched_load_mcp_tools(session, *, connection=None):
    if session is None and connection is None:
        msg = "Either a session or a connection config must be provided"
        raise ValueError(msg)

    tools = []
    if session is None:
        # If a session is not provided, we will create one on the fly
        async with create_session(connection) as tool_session:
            await tool_session.initialize()
            tools = await langchain_mcp_adapters.tools._list_all_tools(tool_session)
    else:
        tools = await langchain_mcp_adapters.tools._list_all_tools(session)

    return [
        langchain_mcp_adapters.tools.convert_mcp_tool_to_langchain_tool(session, tool, connection=connection) for tool in tools
    ]

langchain_mcp_adapters.tools.load_mcp_tools = _patched_load_mcp_tools
langchain_mcp_adapters.client.load_mcp_tools = _patched_load_mcp_tools
# End Monkey Patch
from dotenv import load_dotenv
load_dotenv()
# 从环境变量加载 MCP 配置
def load_mcp_config():
    """从环境变量加载 MCP 配置，如果导入失败或没有识别到则不导入"""
    config_str = os.getenv("MCP_CONFIG")
    if not config_str:
        print("信息: 未找到 MCP_CONFIG 环境变量，返回空配置")
        return {}
    
    try:
        config = json.loads(config_str)
        
        # 验证配置格式
        if not isinstance(config, dict):
            print(f"警告: MCP_CONFIG 必须是字典格式，实际类型: {type(config)}")
            return {}
        
        # 验证每个配置项
        valid_config = {}
        for name, settings in config.items():
            if not isinstance(settings, dict):
                print(f"警告: MCP 配置项 '{name}' 必须是字典格式，跳过该项")
                continue
            
            # 检查必需的字段
            required_fields = ["transport", "url"]
            missing_fields = [field for field in required_fields if field not in settings]
            if missing_fields:
                print(f"警告: MCP 配置项 '{name}' 缺少必需字段: {missing_fields}，跳过该项")
                continue
            
            # 检查字段类型
            if not isinstance(settings["transport"], str):
                print(f"警告: MCP 配置项 '{name}' 的 transport 必须是字符串，跳过该项")
                continue
            
            if not isinstance(settings["url"], str):
                print(f"警告: MCP 配置项 '{name}' 的 url 必须是字符串，跳过该项")
                continue
            
            # 检查 URL 格式（基本验证）
            if not settings["url"].startswith(("http://", "https://")):
                print(f"警告: MCP 配置项 '{name}' 的 URL 格式不正确: {settings['url']}，跳过该项")
                continue
            
            valid_config[name] = settings
            print(f"信息: 成功加载 MCP 配置项: {name}")
        
        if not valid_config:
            print("警告: 没有有效的 MCP 配置项被加载")
            return {}
        
        return valid_config
        
    except json.JSONDecodeError as e:
        print(f"警告: MCP_CONFIG 环境变量格式错误: {e}，返回空配置")
        return {}
    except Exception as e:
        print(f"警告: 加载 MCP_CONFIG 时发生未知错误: {e}，返回空配置")
        return {}

MCP_CONFIG = load_mcp_config()

# 全局MCP客户端实例
_mcp_client = None

def _filter_tools_by_query(
    tool_dicts: List[Dict[str, Any]], 
    query: str = "",
    match_threshold: float = 0.3,
    max_results: int = 50
) -> List[Dict[str, Any]]:
    """
    人性化的工具筛选函数，支持多种匹配策略
    """
    if not query or not tool_dicts:
        return tool_dicts[:max_results]
    
    # 预处理查询词
    q = query.lower().strip()
    q_words = re.findall(r'\b\w+\b', q)
    q_words = [w for w in q_words if len(w) > 1]
    
    if not q_words:
        return []
    
    scored_tools = []
    
    for tool in tool_dicts:
        name = tool.get("name", "").lower()
        description = tool.get("description", "").lower()
        
        score = 0
        matched_fields = []
        
        # 1. 精确匹配
        if q == name:
            score += 100
            matched_fields.append("名称完全匹配")
        elif q in name:
            score += 50
            matched_fields.append("名称包含查询词")
        elif q in description:
            score += 30
            matched_fields.append("描述包含查询词")
        
        # 2. 单词匹配
        name_words = re.findall(r'\b\w+\b', name)
        desc_words = re.findall(r'\b\w+\b', description)
        
        name_word_matches = 0
        for q_word in q_words:
            for n_word in name_words:
                if q_word in n_word or n_word in q_word:
                    score += 10
                    name_word_matches += 1
                    break
        
        desc_word_matches = 0
        for q_word in q_words:
            for d_word in desc_words:
                if q_word in d_word or d_word in q_word:
                    score += 5
                    desc_word_matches += 1
                    break
        
        if name_word_matches > 0:
            matched_fields.append(f"名称匹配{name_word_matches}个单词")
        if desc_word_matches > 0:
            matched_fields.append(f"描述匹配{desc_word_matches}个单词")
        
        # 3. 模糊匹配
        best_fuzzy_score = 0
        for q_word in q_words:
            for n_word in name_words:
                ratio = SequenceMatcher(None, q_word, n_word).ratio()
                if ratio > match_threshold and ratio > best_fuzzy_score:
                    best_fuzzy_score = ratio
                    score += int(ratio * 20)
            
            for d_word in desc_words:
                ratio = SequenceMatcher(None, q_word, d_word).ratio()
                if ratio > match_threshold and ratio > best_fuzzy_score:
                    best_fuzzy_score = ratio
                    score += int(ratio * 10)
        
        if best_fuzzy_score > 0:
            matched_fields.append(f"模糊匹配({best_fuzzy_score:.2f})")
        
        # 4. 首字母匹配
        if len(q_words) == 1 and len(q_words[0]) <= 4:
            if len(name_words) >= len(q_words[0]):
                initials = ''.join([w[0] for w in name_words if w])
                if q_words[0] in initials:
                    score += 15
                    matched_fields.append("首字母缩写匹配")
        
        # 5. 分类/标签匹配
        categories = tool.get("categories", [])
        if categories:
            for category in categories:
                cat_lower = category.lower()
                if q in cat_lower:
                    score += 25
                    matched_fields.append(f"分类匹配: {category}")
                else:
                    for q_word in q_words:
                        if q_word in cat_lower:
                            score += 15
                            matched_fields.append(f"分类单词匹配: {category}")
                            break
        
        # 6. 权重调整
        if len(description) > 200:
            score *= 0.9
        
        common_tools = ["search", "query", "fetch", "get", "find", "list"]
        if any(common in name for common in common_tools):
            score += 5
        
        if score > 0:
            scored_tools.append({
                "tool": tool,
                "score": score,
                "matched_fields": matched_fields
            })
    
    scored_tools.sort(key=lambda x: x["score"], reverse=True)
    result = [item["tool"] for item in scored_tools[:max_results]]
    
    if len(result) > 20:
        result = _group_similar_tools(result, q_words)
    
    return result

def _group_similar_tools(tools, query_words):
    # 简单的分组逻辑占位符
    return tools

async def get_mcp_client():
    """获取全局MCP客户端实例"""
    global _mcp_client
    if _mcp_client is None:
        _mcp_client = MultiServerMCPClient(MCP_CONFIG)
    return _mcp_client

async def cleanup_mcp_client():
    """清理MCP客户端资源"""
    global _mcp_client
    if _mcp_client is not None:
        try:
            if hasattr(_mcp_client, '_session') and hasattr(_mcp_client._session, 'aclose'):
                await _mcp_client._session.aclose()
            _mcp_client = None
        except Exception:
            pass

# 我们不再需要在 atexit 中注册同步清理函数，而是由主程序的异步上下文管理

class ListResourcesInput(BaseModel):
    """list_resources 输入参数"""
    query: str = Field(default="", description="关键词过滤（匹配 name 和 description）")
    page: int = Field(default=1, ge=1, description="页码，从 1 开始")
    page_size: int = Field(default=20, ge=1, le=200, description="每页数量")

async def list_resources(query: str = "", page: int = 1, page_size: int = 20) -> Dict[str, Any]:
    """列出 MCP 工具（带过滤和分页）"""
    try:
        mcp_client = await get_mcp_client()
        tools = await mcp_client.get_tools()

        tool_dicts = []
        for tool in tools:
            tool_dicts.append({
                "name": tool.name,
                "description": tool.description,
                "args_schema": getattr(tool, "args_schema", None),
                "response_format": getattr(tool, "response_format", None)
            })

        filtered = _filter_tools_by_query(tool_dicts, query)

        total = len(filtered)
        start = (page - 1) * page_size
        end = start + page_size
        page_items = filtered[start:end]

        return {
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": (total + page_size - 1) // page_size,
            "results": page_items
        }
    except Exception as e:
        return {"error": f"获取工具列表失败: {str(e)}"}

def list_resources_sync(*args, **kwargs):
    raise NotImplementedError("请使用异步方式调用此工具")

list_resources_tool = StructuredTool.from_function(
    func=list_resources_sync,
    coroutine=list_resources,
    name="list_resources",
    description="【第一步】列出可用的 MCP 工具，支持 query / page / page_size 筛选。必须先调用此工具获取可用工具列表，然后才能使用 call_tool 调用其中的工具。返回结果中的每个工具都有 name 和 description 字段，这些 name 就是 call_tool 可以使用的工具名称。",
    args_schema=ListResourcesInput
)

class CallToolInput(BaseModel):
    tool_name: str = Field(description="需要调用的 MCP 工具名称，必须是 list_resources 工具返回列表中存在的工具名称")
    args: dict = Field(default_factory=dict, description="传递给 MCP 工具的参数")

async def call_tool(tool_name: str, args: dict):
    """异步调用 MCP 工具"""
    try:
        print(f"正在调用 MCP 工具：{tool_name}")
        
        mcp_client = await get_mcp_client()
        available_tools = await mcp_client.get_tools()
        
        target_tool = None
        for tool in available_tools:
            if tool.name == tool_name:
                target_tool = tool
                break
        
        if target_tool is None:
            return {"error": f"找不到工具: {tool_name}"}
            
        # 直接调用 LangChain Tool 对象
        result = await target_tool.ainvoke(args)
        return result
    except Exception as e:
        return {"error": f"调用工具失败: {str(e)}"}

def call_tool_sync(*args, **kwargs):
    raise NotImplementedError("请使用异步方式调用此工具")

call_tool_tool = StructuredTool.from_function(
    func=call_tool_sync,
    coroutine=call_tool,
    name="call_tool",
    description="【第二步】调用 MCP 工具。只能使用 list_resources 工具返回列表中存在的工具名称，不能调用未列出的工具。例如：{'tool_name': 'fetch', 'args': {'url': 'https://example.com'}}。在调用此工具前，必须先调用 list_resources 获取可用工具列表。",
    args_schema=CallToolInput
)
#print(asyncio.run(list_resources(query="fetch")))