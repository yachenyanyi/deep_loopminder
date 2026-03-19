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
    执行 shell 命令并返回输出。
    在 Windows 上使用 cmd.exe，在 Linux/Mac 上使用 bash。
    
    Args:
        command: 要执行的命令
        
    Returns:
        命令的标准输出和错误输出
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
