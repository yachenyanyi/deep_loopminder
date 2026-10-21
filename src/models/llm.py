"""
模型配置 - 懒加载模式

只有实际使用时才初始化模型，避免启动时因缺少 API Key 而失败。
"""

from __future__ import annotations

import os
from dotenv import load_dotenv
load_dotenv()

from langchain_openai import ChatOpenAI
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from langchain_anthropic import ChatAnthropic
    from langchain_community.chat_models import ChatZhipuAI
    from langchain_deepseek import ChatDeepSeek
    from langchain_google_genai import ChatGoogleGenerativeAI
    from langchain_ollama import ChatOllama


# 模型缓存
_models: dict[str, any] = {}


# ============================================================================
# 工厂函数 - 懒加载
# ============================================================================

def get_command_goat_model() -> Optional[ChatOpenAI]:
    """通过官方 OpenAI 兼容接口使用 Command Code GOAT 套餐。"""
    if "command_goat" in _models:
        return _models["command_goat"]

    api_key = os.getenv("COMMADN_GOAT_API_KEY") or os.getenv("COMMAND_CODE_API_KEY", "")
    if not api_key:
        return None

    _models["command_goat"] = ChatOpenAI(
        model=os.getenv("COMMAND_CODE_MODEL") or "meta/muse-spark-1.3-contributor",
        api_key=api_key,
        base_url=os.getenv("COMMAND_CODE_BASE_URL") or "https://api.commandcode.ai/provider/v1",
        use_responses_api=False,
        reasoning_effort=os.getenv("COMMAND_CODE_REASONING_EFFORT") or "medium",
        max_tokens=int(os.getenv("COMMAND_CODE_MAX_TOKENS") or "8192"),
    )
    return _models["command_goat"]


def get_open_router_model() -> Optional[ChatOpenAI]:
    """真实运行与测试共享的免费模型配置，默认输出上限32k。"""
    if "open_router" in _models:
        return _models["open_router"]

    api_key = os.getenv("OPEN_ROUTER_API_KEY") or os.getenv("OPENROUTER_API_KEY", "")
    if not api_key:
        return None

    _models["open_router"] = ChatOpenAI(
        model="minimax/minimax-m3.1-flash:free",
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
        use_responses_api=False,
        max_tokens=32768,
        timeout=120,
        max_retries=1,
    )
    return _models["open_router"]


def get_zhi_pu_model() -> Optional[ChatZhipuAI]:
    """获取智谱模型"""
    if "zhi_pu" in _models:
        return _models["zhi_pu"]

    api_key = os.getenv("ZHIPUAI_API_KEY", "")
    if not api_key:
        return None

    os.environ["ZHIPUAI_API_KEY"] = api_key
    from langchain_community.chat_models import ChatZhipuAI

    _models["zhi_pu"] = ChatZhipuAI(
        model="autoglm-phone",
        temperature=1.5,
    )
    return _models["zhi_pu"]


def get_gpt_model() -> Optional[ChatOpenAI]:
    """获取 GPT 模型"""
    if "gpt" in _models:
        return _models["gpt"]

    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None

    _models["gpt"] = ChatOpenAI(
        model="gpt-4o",
        temperature=1.5,
        max_tokens=None,
        timeout=None,
        max_retries=2,
    )
    return _models["gpt"]


def get_ollama_model() -> ChatOllama:
    """获取 Ollama 模型（本地，无需 API Key）"""
    if "ollama" in _models:
        return _models["ollama"]

    from langchain_ollama import ChatOllama

    _models["ollama"] = ChatOllama(
        model="qwen3:4b",
        temperature=2,
    )
    return _models["ollama"]


def get_qwen_model() -> Optional[ChatOpenAI]:
    """获取通义千问模型"""
    if "qwen" in _models:
        return _models["qwen"]

    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        return None

    _models["qwen"] = ChatOpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus-1125",
    )
    return _models["qwen"]


def get_gemini_model() -> Optional[ChatGoogleGenerativeAI]:
    """获取 Gemini 模型"""
    if "gemini" in _models:
        return _models["gemini"]

    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return None

    os.environ["GOOGLE_API_KEY"] = api_key
    from langchain_google_genai import ChatGoogleGenerativeAI

    _models["gemini"] = ChatGoogleGenerativeAI(
        model="gemini-2.0-flash-exp",
        temperature=2,
        max_retries=2,
    )
    return _models["gemini"]


def get_deepseek_model() -> Optional[ChatDeepSeek]:
    """获取 DeepSeek 模型"""
    if "deepseek" in _models:
        return _models["deepseek"]

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return None

    from langchain_deepseek import ChatDeepSeek

    _models["deepseek"] = ChatDeepSeek(
        model="deepseek-v4-flash",
        temperature=1.5
    )
    return _models["deepseek"]


def get_doubao_model() -> Optional[ChatOpenAI]:
    """获取豆包模型"""
    if "doubao" in _models:
        return _models["doubao"]

    api_key = os.getenv("DOUBAO_API_KEY", "")
    if not api_key:
        return None

    _models["doubao"] = ChatOpenAI(
        api_key=api_key,
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-seed-1-6-flash-250615",
    )
    return _models["doubao"]


def get_anthropic_model() -> Optional[ChatAnthropic]:
    """获取 Anthropic 模型（通过 DashScope 代理）

    环境变量:
        ANTHROPIC_AUTH_TOKEN: API Key
        ANTHROPIC_BASE_URL: 代理地址，默认 https://dashscope.aliyuncs.com/apps/anthropic
        ANTHROPIC_MODEL: 模型名，默认 deepseek-v4-flash
    """
    if "anthropic" in _models:
        return _models["anthropic"]

    api_key = os.getenv("ANTHROPIC_AUTH_TOKEN", "")
    if not api_key:
        return None

    base_url = os.getenv("ANTHROPIC_BASE_URL", "https://dashscope.aliyuncs.com/apps/anthropic")
    model = os.getenv("ANTHROPIC_MODEL", "deepseek-v4-flash")

    from langchain_anthropic import ChatAnthropic

    _models["anthropic"] = ChatAnthropic(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=1.5,
    )
    return _models["anthropic"]


# ============================================================================
# 默认模型 - 真实运行与测试共用免费路由
# ============================================================================

def get_default_model():
    """默认统一使用minimax/minimax-m3.1-flash:free；缺少密钥明确失败，避免自动切到其他服务。"""
    if "default" not in _models:
        model = get_open_router_model()
        if model is None:
            raise ValueError("请配置OPEN_ROUTER_API_KEY或OPENROUTER_API_KEY")
        _models["default"] = model
    return _models["default"]


# ============================================================================
# 向后兼容 - 属性访问器
# ============================================================================

class _ModelProxy:
    """模型代理类，支持懒加载和向后兼容"""

    @property
    def command_goat(self):
        return get_command_goat_model()

    @property
    def open_router(self):
        return get_open_router_model()

    @property
    def deepseek(self):
        return get_deepseek_model()

    @property
    def ollama(self):
        return get_ollama_model()

    @property
    def zhi_pu(self):
        return get_zhi_pu_model()

    @property
    def gpt(self):
        return get_gpt_model()

    @property
    def qwen(self):
        return get_qwen_model()

    @property
    def gemini(self):
        return get_gemini_model()

    @property
    def doubao(self):
        return get_doubao_model()

    @property
    def anthropic(self):
        return get_anthropic_model()


# 导出
models = _ModelProxy()

# 向后兼容：default_model 作为属性
class _DefaultModelProxy:
    """延迟加载 default_model"""
    def __getattr__(self, name):
        return getattr(get_default_model(), name)

    def __call__(self, *args, **kwargs):
        return get_default_model()(*args, **kwargs)

default_model = _DefaultModelProxy()


# ============================================================================
# 便捷函数
# ============================================================================

def list_available_models() -> dict:
    """列出所有可用模型"""
    return {
        "command_goat": get_command_goat_model() is not None,
        "anthropic": get_anthropic_model() is not None,
        "deepseek": get_deepseek_model() is not None,
        "open_router": get_open_router_model() is not None,
        "gpt": get_gpt_model() is not None,
        "qwen": get_qwen_model() is not None,
        "gemini": get_gemini_model() is not None,
        "doubao": get_doubao_model() is not None,
        "zhi_pu": get_zhi_pu_model() is not None,
        "ollama": True,  # 本地模型始终可用
    }


def print_model_status():
    """打印模型状态"""
    status = list_available_models()
    print("\n📋 模型状态:")
    for name, available in status.items():
        icon = "✅" if available else "❌"
        print(f"  {icon} {name}")
