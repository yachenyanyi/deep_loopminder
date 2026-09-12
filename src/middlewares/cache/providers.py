"""
缓存抽象层 — 为 BilibiliMiddleware 提供可插拔缓存后端

支持后端：
- ``PostgresCacheBackend`` — PostgreSQL（需 ``sqlalchemy``）
- ``JsonFileCacheBackend`` — 本地 JSON 文件（零依赖，自动回退）
- ``NoneCacheBackend`` — 无缓存（调试用）

默认策略（``create_default_cache``）：先试 PostgreSQL，连不上则回退到 JSON 文件。
所有后端的 ``get()`` / ``set()`` 均用 try/except 包裹，
缓存不可用时静默回退，不影响正常功能。
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any

# ── 项目根目录探测（与 bilibili.py 保持一致） ──
_PROJECT_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)


# ════════════════════════════════════════════════════════════════
# 抽象基类
# ════════════════════════════════════════════════════════════════

class CacheBackend(ABC):
    """缓存后端抽象基类"""

    @abstractmethod
    def get(self, key: str) -> str | None:
        """获取缓存。不存在或出错时返回 None。"""
        ...

    @abstractmethod
    def set(self, key: str, type_: str, result: str) -> None:
        """写入缓存。出错时静默忽略。"""
        ...


# ════════════════════════════════════════════════════════════════
# 无缓存（调试用）
# ════════════════════════════════════════════════════════════════

class NoneCacheBackend(CacheBackend):
    """无缓存后端，永远返回 None"""

    def get(self, key: str) -> None:
        return None

    def set(self, key: str, type_: str, result: str) -> None:
        pass


# ════════════════════════════════════════════════════════════════
# JSON 文件缓存
# ════════════════════════════════════════════════════════════════

class JsonFileCacheBackend(CacheBackend):
    """本地 JSON 文件缓存

    所有数据保存在一个 JSON 文件中，结构：
    .. code-block:: json
        {
            "search:keyword:page": {
                "type": "search",
                "result": "...",
                "created_at": "2026-07-16T12:00:00"
            }
        }

    Args:
        file_path: JSON 文件路径（默认项目根目录下 bili_cache.json）
    """

    def __init__(self, file_path: str | None = None):
        self.file_path = file_path or os.path.join(_PROJECT_ROOT, "bili_cache.json")
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    # ── 序列化兼容 ──

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)

    # ── 内部 ──

    def _load(self) -> None:
        """从磁盘加载 JSON 文件"""
        try:
            with open(self.file_path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            self._data = {}

    def _save(self) -> None:
        """保存到磁盘"""
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except OSError:
            pass  # 磁盘满 / 权限错误 → 静默忽略

    # ── 公开接口 ──

    def get(self, key: str) -> str | None:
        try:
            entry = self._data.get(key)
            return entry["result"] if entry else None
        except Exception:
            return None

    def set(self, key: str, type_: str, result: str) -> None:
        try:
            self._data[key] = {
                "type": type_,
                "result": result,
                "created_at": datetime.now().isoformat(),
            }
            self._save()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
# PostgreSQL 缓存（需 sqlalchemy）
# ════════════════════════════════════════════════════════════════

class PostgresCacheBackend(CacheBackend):
    """PostgreSQL 缓存后端

    使用 ``sqlalchemy`` 同步引擎操作 ``bili_cache`` 表。
    表自动创建（``CREATE TABLE IF NOT EXISTS``）。

    Args:
        db_uri: PostgreSQL 连接 URI，如 ``postgresql://user:pass@host/db``
    """

    def __init__(self, db_uri: str):
        self._db_uri = db_uri
        self._engine: Any = None  # 惰性初始化
        self._available: bool = False
        self._init()

    # ── 序列化兼容 ──

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state.pop("_engine", None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._engine = None
        self._available = False

    # ── 公开 ──

    @property
    def is_available(self) -> bool:
        """后端是否可用（连接成功、表已就绪）"""
        return self._available

    # ── 内部 ──

    def _get_engine(self):
        """惰性创建 SQLAlchemy 引擎"""
        if self._engine is None:
            from sqlalchemy import create_engine

            self._engine = create_engine(
                self._db_uri,
                pool_pre_ping=True,
                pool_size=2,
                max_overflow=1,
            )
        return self._engine

    def _init(self) -> None:
        """创建表（如果不存在）"""
        try:
            from sqlalchemy import text

            engine = self._get_engine()
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "CREATE TABLE IF NOT EXISTS bili_cache ("
                        "  cache_key TEXT PRIMARY KEY,"
                        "  cache_type TEXT NOT NULL,"
                        "  result TEXT NOT NULL,"
                        "  created_at TIMESTAMP DEFAULT NOW()"
                        ")"
                    )
                )
                conn.commit()
            self._available = True
        except Exception:
            self._available = False  # 连接失败 → 后续 get/set 跳过

    # ── 公开接口 ──

    def get(self, key: str) -> str | None:
        if not self._available:
            return None
        try:
            from sqlalchemy import text

            engine = self._get_engine()
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT result FROM bili_cache WHERE cache_key = :key"),
                    {"key": key},
                ).first()
                return str(row[0]) if row else None
        except Exception:
            return None

    def set(self, key: str, type_: str, result: str) -> None:
        if not self._available:
            return
        try:
            from sqlalchemy import text

            engine = self._get_engine()
            with engine.connect() as conn:
                conn.execute(
                    text(
                        "INSERT INTO bili_cache (cache_key, cache_type, result)"
                        " VALUES (:key, :type, :result)"
                        " ON CONFLICT (cache_key) DO NOTHING"
                    ),
                    {"key": key, "type": type_, "result": result},
                )
                conn.commit()
        except Exception:
            pass


# ════════════════════════════════════════════════════════════════
# 工厂函数
# ════════════════════════════════════════════════════════════════

def create_cache_backend(
    backend: str = "json",
    **kwargs: Any,
) -> CacheBackend:
    """创建缓存后端实例

    Args:
        backend: ``"json"`` | ``"postgres"`` | ``"none"``
        **kwargs: 传递给后端构造函数的额外参数
            - ``json``: ``file_path`` — JSON 文件路径
            - ``postgres``: ``db_uri`` — 连接 URI（必填）

    Returns:
        CacheBackend 实例

    Raises:
        ValueError: 不支持的 backend 名称
    """
    backend_map: dict[str, type[CacheBackend]] = {
        "none": NoneCacheBackend,
        "json": JsonFileCacheBackend,
        "postgres": PostgresCacheBackend,
    }

    cls = backend_map.get(backend)
    if cls is None:
        raise ValueError(
            f"不支持的缓存后端: {backend!r}，可选: {', '.join(backend_map)}"
        )

    if backend == "postgres":
        db_uri = kwargs.get("db_uri")
        if not db_uri:
            # 尝试从环境变量读取
            db_uri = os.environ.get(
                "CACHE_DB_URI",
                os.environ.get("LANGGRAPH_POSTGRES_URI"),
            )
        if not db_uri:
            raise ValueError(
                "PostgresCacheBackend 需要 db_uri 参数或 CACHE_DB_URI 环境变量"
            )
        return PostgresCacheBackend(db_uri)

    if backend == "json":
        file_path = kwargs.get("file_path")
        return JsonFileCacheBackend(file_path=file_path)

    return cls()


# ════════════════════════════════════════════════════════════════
# 默认缓存创建（PG 优先，JSON 回退）
# ════════════════════════════════════════════════════════════════

def create_default_cache() -> CacheBackend:
    """创建默认缓存后端

    优先级：
    1. 环境变量 ``CACHE_DB_URI`` 或 ``LANGGRAPH_POSTGRES_URI`` 存在 → 创建 ``PostgresCacheBackend``
    2. PostgreSQL 连接成功 → 返回 PG 后端
    3. 连接失败或环境变量未设 → 返回 ``JsonFileCacheBackend``（项目根目录 ``bili_cache.json``）
    """
    db_uri = (
        os.environ.get("CACHE_DB_URI")
        or os.environ.get("LANGGRAPH_POSTGRES_URI")
        or ""
    )
    if db_uri:
        pg = PostgresCacheBackend(db_uri)
        if pg.is_available:
            return pg

    return JsonFileCacheBackend()
