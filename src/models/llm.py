import os
from dotenv import load_dotenv
load_dotenv()

from langchain_community.chat_models import ChatZhipuAI
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI
from langchain_ollama import ChatOllama
from langchain_google_genai import ChatGoogleGenerativeAI


def get_open_router_model():
    return ChatOpenAI(
        model="qwen/qwen3-vl-235b-a22b-thinking",
        api_key=os.getenv("OPEN_ROUTER_API_KEY"),
        base_url="https://openrouter.ai/api/v1",
    )

def get_zhi_pu_model():
    api_key = os.getenv("ZHIPUAI_API_KEY", "")
    #if not api_key:
    #   return None
    os.environ["ZhipuAI_API_KEY"] = api_key
    return ChatZhipuAI(
        model="autoglm-phone",
        temperature=1.5,
    )

def get_gpt_model():
    api_key = os.getenv("OPENAI_API_KEY", "")
    if not api_key:
        return None
    return ChatOpenAI(
        model="gpt-4o",
        temperature=1.5,
        max_tokens=None,
        timeout=None,
        max_retries=2,
    )

def get_ollama_model():
    return ChatOllama(
        model="qwen3:4b",
        temperature=2,
    )

def get_qwen_model():
    api_key = os.getenv("DASHSCOPE_API_KEY", "")
    if not api_key:
        return None
    return ChatOpenAI(
        api_key=api_key,
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus-1125",
    )

def get_gemini_model():
    api_key = os.getenv("GOOGLE_API_KEY", "")
    if not api_key:
        return None
    os.environ["GOOGLE_API_KEY"] = api_key
    return ChatGoogleGenerativeAI(
        model="gemini-2.0-flash-exp",
        temperature=2,
        max_retries=2,
    )

def get_deepseek_model():
    return ChatDeepSeek(
        model="deepseek-chat",
        temperature=1.5
    )

def get_doubao_model():
    api_key = os.getenv("DOUBAO_API_KEY", "")
    if not api_key:
        return None
    return ChatOpenAI(
        api_key=api_key,
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        model="doubao-seed-1-6-flash-250615",
    )


open_router_model = get_open_router_model()
deepseek_model = get_deepseek_model()
ollama_model = get_ollama_model()

zhi_pu_model = get_zhi_pu_model()
gpt_model = get_gpt_model()
qwen_model = get_qwen_model()
gemini_model = get_gemini_model()
doubao_model = get_doubao_model()

default_model = open_router_model
