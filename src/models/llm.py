"""
模型配置 - 懒加载模式

只有实际使用时才初始化模型，避免启动时因缺少 API Key 而失败。
"""

import os
from dotenv import load_dotenv
load_dotenv()

from langchain_community.chat_models import ChatZhipuAI
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_google_genai import ChatGoogleGenerativeAI
from typing import Optional


# 模型缓存
_models: dict[str, any] = {}


# ============================================================================
# 工厂函数 - 懒加载
# ============================================================================

def get_open_router_model() -> Optional[ChatOpenAI]:
    """获取 OpenRouter 模型"""
    if "open_router" in _models:
        return _models["open_router"]

    api_key = os.getenv("OPEN_ROUTER_API_KEY", "")
    if not api_key:
        return None

    _models["open_router"] = ChatOpenAI(
        model="qwen/qwen3-vl-235b-a22b-thinking",
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
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

    _models["deepseek"] = ChatDeepSeek(
        model="deepseek-chat",
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


# ============================================================================
# 默认模型 - 按优先级自动选择
# ============================================================================

def get_default_model():
    """
    获取默认模型，按优先级自动选择可用模型：
    1. DeepSeek (推荐，便宜好用)
    2. OpenRouter (多模型支持)
    3. Ollama (本地，无需 API Key)
    """
    if "default" in _models:
        return _models["default"]

    # 按优先级尝试
    model = get_deepseek_model()
    if model:
        print("✅ 使用 DeepSeek 模型")
        _models["default"] = model
        return model

    model = get_open_router_model()
    if model:
        print("✅ 使用 OpenRouter 模型")
        _models["default"] = model
        return model

    # 回退到 Ollama（本地模型，始终可用）
    print("⏳ 未配置 API Key，使用 Ollama 本地模型")
    model = get_ollama_model()
    _models["default"] = model
    return model


# ============================================================================
# 向后兼容 - 属性访问器
# ============================================================================

class _ModelProxy:
    """模型代理类，支持懒加载和向后兼容"""

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