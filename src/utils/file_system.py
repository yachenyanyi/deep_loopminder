from __future__ import annotations

import os
import re
import json
from typing import Iterable, Any

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, Body
from starlette.responses import StreamingResponse
from sqlalchemy import create_engine, text, inspect
import pandas as pd

from deepagents.backends.utils import create_file_data, file_data_to_string, format_read_response
from langgraph.store.base import BaseStore, Item
from src.deep_agents import get_postgres_store

router = APIRouter(prefix="/files", tags=["files"])

# 数据库连接配置 (从环境变量或默认值获取)
DB_URI = os.getenv("DATABASE_URL", "postgresql://postgres:11226647jqk@localhost:5432/postgres")
engine = create_engine(DB_URI, pool_pre_ping=True)


def _validate_path(path: str) -> str:
    if ".." in path or path.startswith("~"):
        raise HTTPException(status_code=400, detail=f"不允许的路径: {path}")
    if re.match(r"^[a-zA-Z]:", path):
        raise HTTPException(status_code=400, detail=f"不支持 Windows 绝对路径: {path}")
    normalized = os.path.normpath(path).replace("\\", "/")
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    return normalized


def _resolve_namespace(path: str, user_id: str | None, thread_id: str | None) -> tuple[str, ...]:
    resolved_user = user_id or "default_user"
    resolved_thread = thread_id or "default_thread"
    if path.startswith("/user/"):
        return (resolved_user, "shared_memory")
    if path.startswith("/thread/"):
        return (resolved_user, resolved_thread)
    return (resolved_user, resolved_thread)


async def _search_all(store: BaseStore, namespace: tuple[str, ...]) -> list[Item]:
    items: list[Item] = []
    offset = 0
    limit = 100
    while True:
        page = await store.asearch(namespace, limit=limit, offset=offset)
        if not page:
            break
        items.extend(page)
        if len(page) < limit:
            break
        offset += limit
    return items


def _filter_paths(paths: Iterable[str], prefix: str | None) -> list[str]:
    if prefix is None:
        return list(paths)
    normalized = _validate_path(prefix)
    return [path for path in paths if str(path).startswith(normalized)]


@router.post("/upload")
async def upload_file(
    file: UploadFile = File(...),
    path: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
):
    # 处理可能的 FastAPI 参数对象（当函数被直接调用时）
    from fastapi.params import Form as FastAPIForm
    actual_path = None
    if path is not None and not isinstance(path, FastAPIForm):
        actual_path = str(path)
    
    actual_user = None
    if user_id is not None and not isinstance(user_id, FastAPIForm):
        actual_user = str(user_id)
        
    actual_thread = None
    if thread_id is not None and not isinstance(thread_id, FastAPIForm):
        actual_thread = str(thread_id)

    filename = os.path.basename(file.filename or "")
    if not filename:
        raise HTTPException(status_code=400, detail="缺少文件名")
    target_path = _validate_path(actual_path or f"/{filename}")
    store = await get_postgres_store()
    namespace = _resolve_namespace(target_path, actual_user, actual_thread)
    content_bytes = await file.read()
    try:
        content_str = content_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="只支持 UTF-8 编码文本文件") from exc
    file_data = create_file_data(content_str)
    store_value = {
        "content": file_data["content"],
        "created_at": file_data["created_at"],
        "modified_at": file_data["modified_at"],
    }
    await store.aput(namespace, target_path, store_value)
    return {"path": target_path, "namespace": list(namespace), "size": len(content_bytes)}


@router.get("/read")
async def read_file(
    path: str = None,
    user_id: str | None = None,
    thread_id: str | None = None,
    offset: int = 0,
    limit: int = 2000,
):
    # 处理可能的 FastAPI 参数对象（当函数被直接调用时）
    from fastapi.params import Query as FastAPIQuery
    actual_path = path
    if path is not None and not isinstance(path, str) and not isinstance(path, FastAPIQuery):
        actual_path = str(path)
    
    actual_user = user_id
    if user_id is not None and not isinstance(user_id, str) and not isinstance(user_id, FastAPIQuery):
        actual_user = str(user_id)
        
    actual_thread = thread_id
    if thread_id is not None and not isinstance(thread_id, str) and not isinstance(thread_id, FastAPIQuery):
        actual_thread = str(thread_id)

    target_path = _validate_path(actual_path)
    store = await get_postgres_store()
    namespace = _resolve_namespace(target_path, actual_user, actual_thread)
    item = await store.aget(namespace, target_path)
    if item is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    content = format_read_response(item.value, offset, limit)
    return {"path": target_path, "content": content}


@router.delete("/delete")
async def delete_file(
    path: str = None,
    user_id: str | None = None,
    thread_id: str | None = None,
):
    # 处理可能的 FastAPI 参数对象（当函数被直接调用时）
    from fastapi.params import Query as FastAPIQuery
    actual_path = path
    if path is not None and not isinstance(path, str) and not isinstance(path, FastAPIQuery):
        actual_path = str(path)
    
    actual_user = user_id
    if user_id is not None and not isinstance(user_id, str) and not isinstance(user_id, FastAPIQuery):
        actual_user = str(user_id)
        
    actual_thread = thread_id
    if thread_id is not None and not isinstance(thread_id, str) and not isinstance(thread_id, FastAPIQuery):
        actual_thread = str(thread_id)

    target_path = _validate_path(actual_path)
    store = await get_postgres_store()
    namespace = _resolve_namespace(target_path, actual_user, actual_thread)
    item = await store.aget(namespace, target_path)
    if item is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    await store.adelete(namespace, target_path)
    return {"path": target_path, "deleted": True}


@router.get("/list")
async def list_files(
    path_prefix: str | None = None,
    user_id: str | None = None,
    thread_id: str | None = None,
):
    # 处理可能的 FastAPI 参数对象（当函数被直接调用时）
    from fastapi.params import Query as FastAPIQuery
    actual_prefix = None
    if path_prefix is not None and not isinstance(path_prefix, FastAPIQuery):
        actual_prefix = str(path_prefix)
    
    actual_user = None
    if user_id is not None and not isinstance(user_id, FastAPIQuery):
        actual_user = str(user_id)
        
    actual_thread = None
    if thread_id is not None and not isinstance(thread_id, FastAPIQuery):
        actual_thread = str(thread_id)

    # 默认列出线程文件，除非明确指定了 /user/ 前缀
    search_path = actual_prefix or "/thread/"
    namespace = _resolve_namespace(search_path, actual_user, actual_thread)
    
    store = await get_postgres_store()
    items = await _search_all(store, namespace)
    paths = [str(item.key) for item in items]
    return {"paths": _filter_paths(paths, actual_prefix)}


@router.get("/download")
async def download_file(
    path: str = Query(...),
    user_id: str | None = Query(None),
    thread_id: str | None = Query(None),
):
    target_path = _validate_path(path)
    store = await get_postgres_store()
    namespace = _resolve_namespace(target_path, user_id, thread_id)
    item = await store.aget(namespace, target_path)
    if item is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    content = file_data_to_string(item.value)
    content_bytes = content.encode("utf-8")
    filename = os.path.basename(target_path) or "download"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(iter([content_bytes]), media_type="application/octet-stream", headers=headers)


# --- 数据库管理 (PSQL 直接操作) ---

@router.get("/db/tables")
async def list_tables():
    """列出数据库中所有表"""
    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names()
        return {"tables": tables}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取表列表失败: {str(e)}")


@router.get("/db/schema/{table_name}")
async def get_table_schema(table_name: str):
    """获取指定表的结构"""
    try:
        inspector = inspect(engine)
        columns = inspector.get_columns(table_name)
        # 转换为更友好的格式
        schema = [
            {
                "name": col["name"],
                "type": str(col["type"]),
                "nullable": col["nullable"],
                "default": str(col["default"]) if col.get("default") else None
            }
            for col in columns
        ]
        return {"table": table_name, "schema": schema}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取表结构失败: {str(e)}")


@router.post("/db/query")
async def execute_sql(query: str = Body(..., embed=True)):
    """执行自定义 SQL 语句 (慎用)"""
    try:
        with engine.connect() as conn:
            if query.strip().lower().startswith("select"):
                df = pd.read_sql(text(query), conn)
                return {"type": "select", "data": df.to_dict(orient="records")}
            else:
                result = conn.execute(text(query))
                conn.commit()
                return {"type": "command", "affected_rows": result.rowcount}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"SQL 执行失败: {str(e)}")


@router.get("/db/stats")
async def get_db_stats():
    """获取数据库统计信息"""
    try:
        query = """
        SELECT 
            relname AS table_name,
            n_live_tup AS row_count,
            pg_size_pretty(pg_total_relation_size(relid)) AS total_size
        FROM pg_stat_user_tables 
        ORDER BY pg_total_relation_size(relid) DESC
        """
        with engine.connect() as conn:
            df = pd.read_sql(text(query), conn)
            db_size_query = "SELECT pg_size_pretty(pg_database_size(current_database()))"
            db_size = conn.execute(text(db_size_query)).scalar()
            return {
                "database_size": db_size,
                "tables": df.to_dict(orient="records")
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取统计信息失败: {str(e)}")


# --- Store (LangGraph) 全局管理 ---

@router.get("/store/namespaces")
async def list_store_namespaces():
    """列出 Store 表中所有命名空间前缀 (prefix)"""
    try:
        query = "SELECT DISTINCT prefix FROM store"
        with engine.connect() as conn:
            result = conn.execute(text(query)).fetchall()
            return {"namespaces": [r[0] for r in result]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"获取命名空间失败: {str(e)}")


@router.get("/store/search")
async def search_store_globally(
    namespace_prefix: str | None = None,
    key_search: str | None = None,
    value_search: str | None = None,
    limit: int = 100
):
    """跨命名空间全局搜索 Store 内容"""
    try:
        query_str = "SELECT prefix, key, value, updated_at FROM store WHERE 1=1"
        params = {"limit": limit}
        
        if namespace_prefix:
            query_str += " AND prefix LIKE :ns"
            params["ns"] = f"{namespace_prefix}%"
            
        if key_search:
            query_str += " AND key LIKE :key"
            params["key"] = f"%{key_search}%"
            
        if value_search:
            query_str += " AND CAST(value AS TEXT) LIKE :val"
            params["val"] = f"%{value_search}%"
            
        query_str += " ORDER BY updated_at DESC LIMIT :limit"
        
        with engine.connect() as conn:
            df = pd.read_sql(text(query_str), conn, params=params)
            # 处理 JSON 值以确保可序列化
            data = df.to_dict(orient="records")
            for item in data:
                if isinstance(item["value"], str):
                    try:
                        item["value"] = json.loads(item["value"])
                    except:
                        pass
                item["updated_at"] = str(item["updated_at"])
            return {"results": data}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Store 搜索失败: {str(e)}")


@router.post("/store/upsert")
async def upsert_store_item(
    namespace: list[str] | str,
    key: str,
    value: Any = Body(...)
):
    """
    手动插入或更新 Store 项。
    注意：此操作会绕过 LangGraph 的层级结构，直接写入数据库。
    """
    try:
        # 统一 prefix 格式 (逗号分隔)
        prefix = ",".join(namespace) if isinstance(namespace, list) else namespace
        
        query = text("""
            INSERT INTO store (prefix, key, value)
            VALUES (:prefix, :key, :value)
            ON CONFLICT (prefix, key)
            DO UPDATE SET value = EXCLUDED.value, updated_at = CURRENT_TIMESTAMP
        """)
        
        with engine.connect() as conn:
            conn.execute(query, {
                "prefix": prefix, 
                "key": key, 
                "value": json.dumps(value) if not isinstance(value, str) else value
            })
            conn.commit()
            return {"status": "success", "prefix": prefix, "key": key}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Upsert 失败: {str(e)}")


from datetime import datetime
from pydantic import BaseModel, Field as PydanticField


class MemoryUpdateInput(BaseModel):
    """版本化记忆更新输入参数"""
    path: str = PydanticField(default="/user/agent.md", description="记忆文件路径，默认为 /user/agent.md")
    change_description: str = PydanticField(..., description="本次更新的简要描述，如'添加用户偏好：喜欢简洁的UI设计'")
    updated_fields: dict = PydanticField(..., description="本次更新的字段内容，如 {'preferences': {'ui_style': 'minimalist'}}")
    expected_version: int = PydanticField(..., description="期望的当前版本号，用于乐观锁检查")
    user_id: str | None = PydanticField(default=None, description="用户ID")
    thread_id: str | None = PydanticField(default=None, description="线程ID")


class MemoryInitInput(BaseModel):
    """初始化记忆文件输入参数"""
    path: str = PydanticField(default="/user/agent.md", description="记忆文件路径")
    initial_profile: dict = PydanticField(default_factory=dict, description="初始用户画像")
    user_id: str | None = PydanticField(default=None, description="用户ID")
    thread_id: str | None = PydanticField(default=None, description="线程ID")


def _create_empty_memory() -> dict:
    """创建空的记忆结构"""
    return {
        "version": 0,
        "current_state": {
            "user_profile": {},
            "preferences": {},
            "important_facts": [],
            "active_goals": []
        },
        "change_history": []
    }


def _format_memory_for_display(memory: dict) -> str:
    """将记忆结构格式化为可读的 Markdown 格式"""
    lines = [
        f"# 用户记忆档案 (版本: {memory['version']})",
        "",
        "## 当前状态",
        "",
        "### 用户画像",
    ]
    
    profile = memory["current_state"].get("user_profile", {})
    if profile:
        for key, value in profile.items():
            lines.append(f"- **{key}**: {value}")
    else:
        lines.append("_暂无信息_")
    
    lines.extend(["", "### 偏好设置"])
    prefs = memory["current_state"].get("preferences", {})
    if prefs:
        for key, value in prefs.items():
            lines.append(f"- **{key}**: {value}")
    else:
        lines.append("_暂无信息_")
    
    lines.extend(["", "### 重要事实"])
    facts = memory["current_state"].get("important_facts", [])
    if facts:
        for fact in facts:
            lines.append(f"- {fact}")
    else:
        lines.append("_暂无信息_")
    
    lines.extend(["", "### 当前目标"])
    goals = memory["current_state"].get("active_goals", [])
    if goals:
        for goal in goals:
            lines.append(f"- {goal}")
    else:
        lines.append("_暂无信息_")
    
    lines.extend(["", "---", "", "## 变更历史", ""])
    history = memory.get("change_history", [])
    if history:
        for entry in reversed(history[-10:]):
            lines.append(f"### v{entry['version']} ({entry['timestamp']})")
            lines.append(f"**变更**: {entry['description']}")
            lines.append(f"**详情**: {json.dumps(entry['changes'], ensure_ascii=False)}")
            lines.append("")
    else:
        lines.append("_暂无变更记录_")
    
    return "\n".join(lines)


@router.get("/memory/read")
async def read_memory(
    path: str = "/user/agent.md",
    user_id: str | None = None,
    thread_id: str | None = None,
    format: str = "markdown"
):
    """
    读取版本化记忆文件。
    
    返回格式：
    - markdown: 人类可读的 Markdown 格式
    - json: 原始 JSON 结构（包含版本号，用于后续更新）
    """
    target_path = _validate_path(path)
    store = await get_postgres_store()
    namespace = _resolve_namespace(target_path, user_id, thread_id)
    item = await store.aget(namespace, target_path)
    
    if item is None:
        empty_memory = _create_empty_memory()
        return {
            "path": target_path,
            "exists": False,
            "version": 0,
            "content": _format_memory_for_display(empty_memory) if format == "markdown" else empty_memory,
            "message": "记忆文件不存在，这是初始状态。请使用 /memory/init 初始化。"
        }
    
    try:
        memory_data = json.loads(item.value.get("content", "{}"))
    except (json.JSONDecodeError, TypeError):
        memory_data = _create_empty_memory()
    
    if format == "markdown":
        content = _format_memory_for_display(memory_data)
    else:
        content = memory_data
    
    return {
        "path": target_path,
        "exists": True,
        "version": memory_data.get("version", 0),
        "content": content
    }


@router.post("/memory/init")
async def init_memory(
    path: str = Body(default="/user/agent.md"),
    initial_profile: dict = Body(default_factory=dict),
    user_id: str | None = Body(default=None),
    thread_id: str | None = Body(default=None)
):
    """
    初始化版本化记忆文件。
    
    仅在文件不存在时创建，已存在则返回错误。
    """
    target_path = _validate_path(path)
    store = await get_postgres_store()
    namespace = _resolve_namespace(target_path, user_id, thread_id)
    
    existing = await store.aget(namespace, target_path)
    if existing is not None:
        return {
            "status": "error",
            "message": "记忆文件已存在，请使用 /memory/update 更新",
            "current_version": json.loads(existing.value.get("content", "{}")).get("version", 0)
        }
    
    memory_data = _create_empty_memory()
    memory_data["version"] = 1
    memory_data["current_state"]["user_profile"] = initial_profile
    memory_data["change_history"].append({
        "version": 1,
        "timestamp": datetime.now().isoformat(),
        "description": "初始化用户记忆档案",
        "changes": {"initial_profile": initial_profile}
    })
    
    file_data = create_file_data(json.dumps(memory_data, ensure_ascii=False, indent=2))
    await store.aput(namespace, target_path, {
        "content": file_data["content"],
        "created_at": file_data["created_at"],
        "modified_at": file_data["modified_at"]
    })
    
    return {
        "status": "success",
        "version": 1,
        "message": "记忆文件初始化成功"
    }


@router.post("/memory/update")
async def update_memory(
    path: str = Body(default="/user/agent.md"),
    change_description: str = Body(...),
    updated_fields: dict = Body(...),
    expected_version: int = Body(...),
    user_id: str | None = Body(default=None),
    thread_id: str | None = Body(default=None)
):
    """
    版本化更新记忆文件（带乐观锁）。
    
    工作流程：
    1. 读取当前记忆文件，获取版本号
    2. 检查 expected_version 是否匹配当前版本
    3. 如果匹配，追加更新并递增版本号
    4. 如果不匹配，返回冲突错误（说明有并发修改）
    
    返回：
    - success: 更新成功，返回新版本号
    - conflict: 版本冲突，需要重新读取并重试
    """
    target_path = _validate_path(path)
    store = await get_postgres_store()
    namespace = _resolve_namespace(target_path, user_id, thread_id)
    
    item = await store.aget(namespace, target_path)
    
    if item is None:
        return {
            "status": "error",
            "message": "记忆文件不存在，请先使用 /memory/init 初始化",
            "expected_version": expected_version,
            "actual_version": 0
        }
    
    try:
        memory_data = json.loads(item.value.get("content", "{}"))
    except (json.JSONDecodeError, TypeError):
        memory_data = _create_empty_memory()
    
    current_version = memory_data.get("version", 0)
    
    if expected_version != current_version:
        return {
            "status": "conflict",
            "message": f"版本冲突：期望版本 {expected_version}，实际版本 {current_version}。请重新读取最新数据后重试。",
            "expected_version": expected_version,
            "actual_version": current_version,
            "hint": "使用 /memory/read?format=json 获取最新版本号"
        }
    
    new_version = current_version + 1
    
    def _deep_merge(base: dict, update: dict) -> dict:
        """深度合并字典"""
        result = base.copy()
        for key, value in update.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = _deep_merge(result[key], value)
            elif key in result and isinstance(result[key], list) and isinstance(value, list):
                result[key] = result[key] + value
            else:
                result[key] = value
        return result
    
    memory_data["current_state"] = _deep_merge(memory_data["current_state"], updated_fields)
    
    memory_data["change_history"].append({
        "version": new_version,
        "timestamp": datetime.now().isoformat(),
        "description": change_description,
        "changes": updated_fields
    })
    
    memory_data["version"] = new_version
    
    if len(memory_data["change_history"]) > 100:
        memory_data["change_history"] = memory_data["change_history"][-50:]
    
    file_data = create_file_data(json.dumps(memory_data, ensure_ascii=False, indent=2))
    await store.aput(namespace, target_path, {
        "content": file_data["content"],
        "created_at": item.value.get("created_at", file_data["created_at"]),
        "modified_at": file_data["modified_at"]
    })
    
    return {
        "status": "success",
        "version": new_version,
        "message": f"记忆更新成功：{change_description}",
        "change_recorded": {
            "version": new_version,
            "description": change_description,
            "changes": updated_fields
        }
    }


@router.get("/memory/history")
async def get_memory_history(
    path: str = "/user/agent.md",
    user_id: str | None = None,
    thread_id: str | None = None,
    limit: int = 20
):
    """
    获取记忆变更历史。
    """
    target_path = _validate_path(path)
    store = await get_postgres_store()
    namespace = _resolve_namespace(target_path, user_id, thread_id)
    
    item = await store.aget(namespace, target_path)
    
    if item is None:
        return {"status": "error", "message": "记忆文件不存在"}
    
    try:
        memory_data = json.loads(item.value.get("content", "{}"))
    except (json.JSONDecodeError, TypeError):
        return {"status": "error", "message": "记忆文件格式错误"}
    
    history = memory_data.get("change_history", [])
    
    return {
        "path": target_path,
        "current_version": memory_data.get("version", 0),
        "total_changes": len(history),
        "history": history[-limit:] if limit else history
    }
