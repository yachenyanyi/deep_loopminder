import os
import sys
import asyncio
from pathlib import Path

# 定义基础路径
# 在模块级别获取路径是安全的，因为它在 ASGI 服务器启动时的导入阶段执行
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
SKILLS_REPO_DIR = os.path.join(BASE_DIR, "skills_repo")

# 确保目录存在
Path(WORKSPACE_DIR).mkdir(parents=True, exist_ok=True)
Path(SKILLS_REPO_DIR).mkdir(parents=True, exist_ok=True)


# Windows系统需要设置兼容的事件循环策略
if sys.platform == 'win32':
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def load_prompt_from_file(filepath):
    full_path = os.path.join(BASE_DIR, filepath) if not os.path.isabs(filepath) else filepath
    if not os.path.exists(full_path):
        return ""
    with open(full_path, 'r', encoding='utf-8') as f:
        return f.read().strip()

boy_prompt = load_prompt_from_file('src/agents/boy.txt')