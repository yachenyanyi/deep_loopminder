"""
自定义 Shell 工具 - Windows 兼容版本
"""
import subprocess
import os
from langchain_core.tools import tool

WORKSPACE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))), "workspace")

@tool
def run_shell_command(command: str) -> str:
    """
    在 workspace 目录下执行 shell 命令。

    ## 参数
    - command: 要执行的命令字符串

    ## 返回
    - STDOUT: 命令的标准输出
    - STDERR: 命令的错误输出
    - Exit code: 退出码（非 0 时显示）

    ## 示例
    ```python
    # 列出文件
    run_shell_command("ls -la")

    # 安装 Python 包
    run_shell_command("pip install requests")

    # 查看文件内容
    run_shell_command("cat README.md")
    ```

    ## 注意事项
    - 超时时间：60 秒
    - 工作目录：workspace/
    - Windows 使用 cmd.exe，Linux/Mac 使用 bash
    """
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=60,
            cwd=WORKSPACE_DIR,
            encoding='utf-8',
            errors='replace'
        )
        
        output = []
        if result.stdout:
            output.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output.append(f"STDERR:\n{result.stderr}")
        if result.returncode != 0:
            output.append(f"Exit code: {result.returncode}")
            
        return "\n".join(output) if output else "Command executed successfully (no output)"
        
    except subprocess.TimeoutExpired:
        return "Error: Command timed out after 60 seconds"
    except Exception as e:
        return f"Error: {str(e)}"


shell_tools = [run_shell_command]
