import asyncio
from langchain_ollama import OllamaEmbeddings

def get_qwen_embeddings():
    """
    获取 Qwen3-embedding 模型实例
    供 langgraph.json 配置文件引用
    """
    return OllamaEmbeddings(
        model="qwen3-embedding:latest", # 或者使用 qwen3-embedding:0.6b, 4b, 8b
    )

# 供测试使用
if __name__ == "__main__":
    async def test():
        embeddings = get_qwen_embeddings()
        text = "你好，世界"
        query_result = await embeddings.aembed_query(text)
        print(f"向量维度: {len(query_result)}")
        # print(f"向量前5位: {query_result[:5]}")

    asyncio.run(test())
