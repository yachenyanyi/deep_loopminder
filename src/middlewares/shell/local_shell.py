from langchain.agents.middleware import ShellToolMiddleware, HostExecutionPolicy
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

local_shell_middleware = ShellToolMiddleware(
    workspace_root=WORKSPACE_DIR,
    execution_policy=HostExecutionPolicy(
        command_timeout=60.0,
    ),
    startup_commands=[],
    shell_command=shell_cmd,
    env=current_env,  # 传递当前环境变量
)
