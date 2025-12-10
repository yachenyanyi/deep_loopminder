#!/usr/bin/env python3
"""
创建30个模拟的MCP工具数据，用于测试关键词匹配功能
"""
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
MOCK_TOOLS = [
    {
        "name": "SearchDocsByLangChain",
        "description": "Search across the Docs by LangChain knowledge base to find relevant information, code examples, API references, and guides. Use this tool when you need to answer questions about Docs by LangChain, find specific documentation, understand how features work, or locate implementation details. The search returns contextual content with titles and direct links to the documentation pages."
    },
    {
        "name": "fetch",
        "description": "Fetch content from a URL and optionally extract it as markdown. This tool can be used to retrieve web pages, API responses, or any HTTP-accessible content. It supports various content types and can convert HTML to markdown for easier processing."
    },
    {
        "name": "CodeAnalyzer",
        "description": "Analyze source code for quality, security issues, and best practices. Supports multiple programming languages including Python, JavaScript, TypeScript, Java, and Go. Provides detailed reports on code complexity, potential bugs, and style violations."
    },
    {
        "name": "DatabaseQuery",
        "description": "Execute SQL queries against connected databases. Supports PostgreSQL, MySQL, SQLite, and MongoDB. Provides secure parameterized queries, result formatting, and connection pooling for efficient database operations."
    },
    {
        "name": "FileSystemManager",
        "description": "Manage file system operations including reading, writing, copying, moving, and deleting files and directories. Provides cross-platform compatibility and secure file handling with proper permission management."
    },
    {
        "name": "APIConnector",
        "description": "Connect to REST APIs with automatic authentication, rate limiting, and retry mechanisms. Supports OAuth 2.0, API keys, and custom authentication methods. Handles JSON/XML parsing and error handling."
    },
    {
        "name": "TextProcessor",
        "description": "Process and transform text data with advanced features including regex matching, string replacement, case conversion, encoding detection, and text extraction. Supports Unicode and multiple character encodings."
    },
    {
        "name": "ImageProcessor",
        "description": "Process images with operations like resize, crop, rotate, format conversion, and filter application. Supports JPEG, PNG, GIF, WebP formats. Provides batch processing and optimization features."
    },
    {
        "name": "PDFExtractor",
        "description": "Extract text, images, and metadata from PDF files. Supports encrypted PDFs, table extraction, and form data processing. Can convert PDFs to other formats like HTML or plain text."
    },
    {
        "name": "EmailSender",
        "description": "Send emails with HTML/text content, attachments, and template support. Integrates with SMTP servers, handles authentication, and provides delivery status tracking. Supports bulk email operations."
    },
    {
        "name": "WebScraper",
        "description": "Extract data from web pages using CSS selectors, XPath, or regular expressions. Handles JavaScript-rendered content, pagination, and anti-bot measures. Provides data cleaning and export features."
    },
    {
        "name": "DataValidator",
        "description": "Validate data against schemas, business rules, and custom constraints. Supports JSON Schema, XML Schema, and custom validation functions. Provides detailed error messages and validation reports."
    },
    {
        "name": "CacheManager",
        "description": "Manage caching operations with support for Redis, Memcached, and in-memory caching. Provides TTL management, cache invalidation, and performance metrics. Supports distributed caching scenarios."
    },
    {
        "name": "Logger",
        "description": "Advanced logging system with multiple handlers, formatters, and filters. Supports structured logging, log rotation, and remote log aggregation. Integrates with popular monitoring systems."
    },
    {
        "name": "ConfigManager",
        "description": "Manage application configuration from files, environment variables, and remote sources. Supports hot-reloading, configuration validation, and environment-specific settings. Provides secure secret management."
    },
    {
        "name": "SecurityScanner",
        "description": "Scan code and dependencies for security vulnerabilities. Checks against CVE databases, performs static analysis, and identifies potential security risks. Provides remediation recommendations."
    },
    {
        "name": "PerformanceProfiler",
        "description": "Profile application performance with CPU, memory, and I/O analysis. Identifies bottlenecks, memory leaks, and optimization opportunities. Provides visual reports and performance metrics."
    },
    {
        "name": "MachineLearning",
        "description": "Train and deploy machine learning models with support for classification, regression, and clustering. Integrates with scikit-learn, TensorFlow, and PyTorch. Provides model evaluation and hyperparameter tuning."
    },
    {
        "name": "DataVisualization",
        "description": "Create charts, graphs, and interactive visualizations from data. Supports matplotlib, Plotly, and D3.js. Provides statistical analysis features and export to multiple formats."
    },
    {
        "name": "DocumentGenerator",
        "description": "Generate documents in various formats including PDF, Word, HTML, and Markdown. Supports templates, dynamic content insertion, and styling. Provides mail merge and batch generation capabilities."
    },
    {
        "name": "BackupManager",
        "description": "Automated backup and restore operations for files, databases, and system configurations. Supports incremental backups, compression, encryption, and cloud storage integration."
    },
    {
        "name": "NotificationService",
        "description": "Send notifications through multiple channels including email, SMS, Slack, and webhooks. Supports templating, scheduling, and delivery tracking. Integrates with popular messaging platforms."
    },
    {
        "name": "WorkflowEngine",
        "description": "Orchestrate complex workflows with conditional logic, parallel execution, and error handling. Supports task dependencies, retry mechanisms, and progress tracking. Provides visual workflow designer."
    },
    {
        "name": "APIRateLimiter",
        "description": "Implement rate limiting for API endpoints with configurable limits and time windows. Supports burst handling, client identification, and distributed rate limiting. Provides usage analytics."
    },
    {
        "name": "DataTransformer",
        "description": "Transform data between different formats including JSON, XML, CSV, and Parquet. Supports schema mapping, data validation, and batch processing. Provides streaming transformation capabilities."
    },
    {
        "name": "SearchEngine",
        "description": "Full-text search functionality with indexing, querying, and ranking. Supports Elasticsearch, Solr, and SQLite FTS. Provides fuzzy matching, faceted search, and search result highlighting."
    },
    {
        "name": "CryptoManager",
        "description": "Handle cryptographic operations including encryption, decryption, hashing, and digital signatures. Supports multiple algorithms and key management. Provides secure random number generation."
    },
    {
        "name": "NetworkAnalyzer",
        "description": "Analyze network traffic, monitor connectivity, and diagnose network issues. Supports packet capture, port scanning, and bandwidth monitoring. Provides network topology mapping."
    },
    {
        "name": "TimeSeries",
        "description": "Process and analyze time series data with trend analysis, seasonality detection, and anomaly identification. Supports forecasting, aggregation, and data interpolation. Integrates with pandas and NumPy."
    },
    {
        "name": "GraphDatabase",
        "description": "Interact with graph databases to store and query connected data. Supports Neo4j, ArangoDB, and network analysis algorithms. Provides path finding, centrality analysis, and graph visualization."
    }
]
def _filter_tools_by_query(
    tool_dicts: List[Dict[str, Any]], 
    query: str = "",
    match_threshold: float = 0.3,
    max_results: int = 50
) -> List[Dict[str, Any]]:
    """
    人性化的工具筛选函数，支持多种匹配策略
    
    Args:
        tool_dicts: 工具字典列表
        query: 查询关键词
        match_threshold: 模糊匹配阈值 (0-1)
        max_results: 最大返回结果数
    
    Returns:
        过滤后的工具字典列表，按匹配度排序
    """
    if not query or not tool_dicts:
        return tool_dicts[:max_results]
    
    # 预处理查询词
    q = query.lower().strip()
    q_words = re.findall(r'\b\w+\b', q)  # 分割单词
    q_words = [w for w in q_words if len(w) > 1]  # 过滤掉单个字母
    
    # 如果没有有效单词，返回空
    if not q_words:
        return []
    
    scored_tools = []
    
    for tool in tool_dicts:
        name = tool.get("name", "").lower()
        description = tool.get("description", "").lower()
        
        # 初始化匹配分数
        score = 0
        matched_fields = []
        
        # 1. 精确匹配 (最高优先级)
        if q == name:
            score += 100  # 名称完全匹配
            matched_fields.append("名称完全匹配")
        elif q in name:
            score += 50  # 名称包含完整查询词
            matched_fields.append("名称包含查询词")
        elif q in description:
            score += 30  # 描述包含完整查询词
            matched_fields.append("描述包含查询词")
        
        # 2. 单词匹配
        name_words = re.findall(r'\b\w+\b', name)
        desc_words = re.findall(r'\b\w+\b', description)
        
        # 名称中的单词匹配
        name_word_matches = 0
        for q_word in q_words:
            for n_word in name_words:
                if q_word in n_word or n_word in q_word:
                    score += 10
                    name_word_matches += 1
                    break
        
        # 描述中的单词匹配
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
        
        # 3. 模糊匹配 (用于处理拼写错误或相似词)
        best_fuzzy_score = 0
        for q_word in q_words:
            # 与名称中的单词模糊匹配
            for n_word in name_words:
                ratio = SequenceMatcher(None, q_word, n_word).ratio()
                if ratio > match_threshold and ratio > best_fuzzy_score:
                    best_fuzzy_score = ratio
                    score += int(ratio * 20)  # 根据相似度给分
            
            # 与描述中的单词模糊匹配
            for d_word in desc_words:
                ratio = SequenceMatcher(None, q_word, d_word).ratio()
                if ratio > match_threshold and ratio > best_fuzzy_score:
                    best_fuzzy_score = ratio
                    score += int(ratio * 10)  # 描述模糊匹配权重较低
        
        if best_fuzzy_score > 0:
            matched_fields.append(f"模糊匹配({best_fuzzy_score:.2f})")
        
        # 4. 首字母匹配 (缩写匹配)
        if len(q_words) == 1 and len(q_words[0]) <= 4:
            # 检查是否是工具名的首字母缩写
            if len(name_words) >= len(q_words[0]):
                initials = ''.join([w[0] for w in name_words if w])
                if q_words[0] in initials:
                    score += 15
                    matched_fields.append("首字母缩写匹配")
        
        # 5. 分类/标签匹配 (如果工具有分类信息)
        categories = tool.get("categories", [])
        if categories:
            for category in categories:
                cat_lower = category.lower()
                if q in cat_lower:
                    score += 25
                    matched_fields.append(f"分类匹配: {category}")
                else:
                    # 检查是否匹配分类中的单词
                    for q_word in q_words:
                        if q_word in cat_lower:
                            score += 15
                            matched_fields.append(f"分类单词匹配: {category}")
                            break
        
        # 6. 相关性权重调整
        # - 较长的描述通常包含更多关键词，适当降低权重
        if len(description) > 200:
            score *= 0.9
        
        # - 常用工具加分 (可以通过外部数据或使用频率来定义)
        common_tools = ["search", "query", "fetch", "get", "find", "list"]
        if any(common in name for common in common_tools):
            score += 5
        
        # 只有匹配的工具才加入结果
        if score > 0:
            scored_tools.append({
                "tool": tool,
                "score": score,
                "matched_fields": matched_fields
            })
    
    # 按分数降序排序
    scored_tools.sort(key=lambda x: x["score"], reverse=True)
    
    # 返回原始工具信息，限制结果数量
    result = [item["tool"] for item in scored_tools[:max_results]]
    
    # 如果结果太多，添加智能分组

    return result
def create_mock_tool_response(query="", page=1, page_size=10):
    """
    模拟 list_resources 函数的返回格式
    """
    from typing import List, Dict, Any
    
    def filter_tools_by_query(tool_dicts: List[Dict[str, Any]], query: str = "") -> List[Dict[str, Any]]:
        """根据查询关键词过滤工具列表"""
        if not query:
            return tool_dicts
        
        q = query.lower().strip()
        if not q:
            return tool_dicts
        
        filtered = [
            tool for tool in tool_dicts
            if q in tool.get("name", "").lower() or q in tool.get("description", "").lower()
        ]
        
        return filtered
    
    # 过滤工具
    filtered = filter_tools_by_query(MOCK_TOOLS, query)
    
    # 分页处理
    total = len(filtered)
    start = (page - 1) * page_size
    end = start + page_size
    page_items = filtered[start:end]
    
    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size,
        "results": page_items,
        "has_next": end < total,
        "has_prev": start > 0
    }

if __name__ == "__main__":
    # 测试不同的查询
    test_queries = [
        "",
        "search", 
        "fetch",
        "langchain",
        "code",
        "database",
        "image",
        "pdf",
        "email",
        "web",
        "data",
        "cache",
        "log",
        "config",
        "security",
        "performance",
        "machine",
        "visualization",
        "document",
        "backup",
        "notification",
        "workflow",
        "api",
        "transform",
        "crypto",
        "network",
        "time",
        "graph"
    ]
    
    #print("🧪 测试工具过滤功能")
    #print("=" * 60)
    #
    #for query in test_queries:
    #    result = create_mock_tool_response(query, page=1, page_size=5)
    #    print(f"\n查询 '{query}':")
    #    print(f"  找到 {result['total']} 个工具")
    #    
    #    if result['results']:
    #        print(f"  前几个匹配的工具:")
    #        for tool in result['results'][:3]:
    #            print(f"    - {tool['name']}: {tool['description'][:80]}...")
    #
    ## 测试分页
    #print("\n" + "=" * 60)
    #print("📄 测试分页功能")
    #print("=" * 60)
    #
    #page1 = create_mock_tool_response("", page=1, page_size=5)
    #page2 = create_mock_tool_response("", page=2, page_size=5)
    #
    #print(f"第一页: {len(page1['results'])} 个工具")
    #for tool in page1['results']:
    #    print(f"  - {tool['name']}")
    #
    #print(f"\n第二页: {len(page2['results'])} 个工具")
    #for tool in page2['results']:
    #    print(f"  - {tool['name']}")
    #
    #print(f"\n总工具数: {page1['total']}")
    #print(f"总页数: {page1['total_pages']}")
    #print(f"有下一页: {page1['has_next']}")
    #print(f"有上一页: {page1['has_prev']}")、

    for i in test_queries:
        result = _filter_tools_by_query(MOCK_TOOLS, query=i,match_threshold=0.1,max_results=5)
        print(f"\n查询 '{i}':")
        print(result)
        print(f"----------------------------------------------")