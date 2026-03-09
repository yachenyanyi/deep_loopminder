from deepagents.backends import StoreBackend


class NamespacedStoreBackend(StoreBackend):
    """自定义 StoreBackend，支持通过模板动态生成命名空间"""
    def __init__(self, runtime, namespace_template: tuple[str, ...], store=None):
        self._explicit_store = store
        self.namespace_template = namespace_template

        def namespace(ctx) -> tuple[str, ...]:
            config = getattr(ctx.runtime, "config", {})
            configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
            user_id = configurable.get("user_id", "default_user")
            thread_id = configurable.get("thread_id", "default_thread")
            return tuple(
                part.replace("{user_id}", user_id).replace("{thread_id}", thread_id)
                for part in namespace_template
            )

        super().__init__(runtime, namespace=namespace)

    def _get_store(self):
        # 1. 如果有显式注入的 store，优先使用
        if self._explicit_store is not None:
            return self._explicit_store
            
        # 2. 尝试从 runtime 中获取 store
        runtime_store = getattr(self.runtime, "store", None)
        
        # 3. 这里的关键逻辑：如果 runtime.store 是内存存储（开发模式下），
        # 我们检查是否有名为 global_store 的全局变量（通常是我们配置的 PostgresStore）
        # 或者通过 deep_agent.py 中的 get_postgres_store 获取
        if runtime_store is not None:
            # 简单判断是否为内存存储，如果是，则尝试寻找持久化存储
            from langgraph.store.memory import InMemoryStore
            if not isinstance(runtime_store, InMemoryStore):
                return runtime_store
        
        # 4. 回退到全局 PostgreSQL Store (如果已定义)
        try:
            from src.deep_agents.deep_agent import global_store
            if global_store is not None:
                return global_store
        except ImportError:
            pass
            
        return super()._get_store()
