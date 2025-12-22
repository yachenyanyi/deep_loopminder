from deepagents.backends import StoreBackend

class PrivateThreadBackend(StoreBackend):
    """
    后端 1：只有当前用户的当前线程才可以访问
    """
    def _get_namespace(self) -> tuple[str, ...]:
        config = getattr(self.runtime, "config", {})
        configurable = config.get("configurable", {})
        
        # 增加校验：确保 ID 不为空字符串，否则使用默认值
        user_id = configurable.get("user_id") or "default_user"
        thread_id = configurable.get("thread_id") or "default_thread"
        
        return (str(user_id), str(thread_id), "private")

class SharedUserBackend(StoreBackend):
    """
    后端 2：当前用户的每个线程都可以访问
    """
    def _get_namespace(self) -> tuple[str, ...]:
        config = getattr(self.runtime, "config", {})
        configurable = config.get("configurable", {})
        
        # 增加校验：确保 ID 不为空字符串
        user_id = configurable.get("user_id") or "default_user"
        
        return (str(user_id), "shared")
class NamespacedStoreBackend(StoreBackend):
    """自定义 StoreBackend，支持通过模板动态生成命名空间"""
    def __init__(self, runtime, namespace_template: tuple[str, ...]):
        super().__init__(runtime)
        self.namespace_template = namespace_template

    def _get_namespace(self) -> tuple[str, ...]:
        # 从运行时配置中提取 user_id 和 thread_id
        # 这些参数由前端通过 configurable 传入
        config = getattr(self.runtime, "config", {})
        configurable = config.get("configurable", {})
        
        user_id = configurable.get("user_id", "default_user")
        thread_id = configurable.get("thread_id", "default_thread")
        
        # 填充模板中的占位符
        return tuple(
            part.replace("{user_id}", user_id).replace("{thread_id}", thread_id)
            for part in self.namespace_template
        )