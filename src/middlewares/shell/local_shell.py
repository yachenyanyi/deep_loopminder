from langchain.agents.middleware import ShellToolMiddleware, HostExecutionPolicy
from langchain.agents.middleware.types import AgentMiddleware
import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")

os.makedirs(WORKSPACE_DIR, exist_ok=True)

if sys.platform == 'win32':
    shell_cmd = "cmd.exe"
else:
    shell_cmd = "/bin/bash"

# 获取当前环境变量，确保后台服务也能访问用户安装的工具
current_env = os.environ.copy()

# 创建原始中间件实例（不直接使用）
_internal_shell_middleware = ShellToolMiddleware(
    workspace_root=WORKSPACE_DIR,
    execution_policy=HostExecutionPolicy(
        command_timeout=60.0,
    ),
    startup_commands=[],
    shell_command=shell_cmd,
    env=current_env,  # 传递当前环境变量
)


class _SerializableShellMiddleware(AgentMiddleware):
    """可序列化的 Shell 中间件包装器

    解决 ShellToolMiddleware 包含不可序列化属性（如 _thread.lock）的问题。
    在序列化时只保存配置，反序列化时重新创建中间件实例。
    """

    def __init__(self, middleware):
        self._middleware = middleware
        # 从内部中间件复制 tools 属性
        self._tools = getattr(middleware, 'tools', [])

    @property
    def tools(self):
        return self._tools

    def __getattr__(self, name):
        # 代理所有其他属性访问到内部中间件
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self._middleware, name)

    def __getstate__(self):
        # 只保存配置，不保存中间件实例
        return {
            'workspace_root': WORKSPACE_DIR,
            'shell_command': shell_cmd,
        }

    def __setstate__(self, state):
        # 重新创建中间件实例
        self._middleware = _internal_shell_middleware
        self._tools = getattr(self._middleware, 'tools', [])


# 导出可序列化的包装实例
local_shell_middleware = _SerializableShellMiddleware(_internal_shell_middleware)
