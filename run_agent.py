#!/usr/bin/env python3
"""
Deep Agent CLI 入口
运行 create_intelligent_deep_agent
支持文本和图片输入
"""

import asyncio
import sys
import os
import base64
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

sys.path.insert(0, BASE_DIR)

from src.deep_agents.deep_agent import create_intelligent_deep_agent
from src.agents.agent import create_intelligent_deep_agent_mobile
from src.agents.agent import create_autoglm_agent


# 支持的图片格式
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.bmp', '.gif', '.webp'}
MAX_IMAGE_SIZE = 4 * 1024 * 1024  # 4MB

def is_image_file(path: Path) -> bool:
    """检查是否是图片文件"""
    return path.suffix.lower() in IMAGE_EXTENSIONS

def compress_image(image_path: Path, max_size: int = MAX_IMAGE_SIZE) -> bytes:
    """
    压缩图片至指定大小以内

    Args:
        image_path: 图片路径
        max_size: 最大文件大小（字节），默认 4MB

    Returns:
        压缩后的图片数据
    """
    try:
        from PIL import Image
        from io import BytesIO

        img = Image.open(image_path)
        original_size = os.path.getsize(image_path)

        # 如果原图小于限制，直接返回
        if original_size <= max_size:
            print(f"图片大小：{original_size/1024:.1f}KB (无需压缩)")
            with open(image_path, 'rb') as f:
                return f.read()

        print(f"图片过大 ({original_size/1024:.1f}KB)，开始压缩...")

        # 转换为 RGB 模式（PNG 等可能有 alpha 通道）
        if img.mode in ('RGBA', 'P'):
            img = img.convert('RGB')

        # 方法 1：降低 JPEG 质量
        quality = 95
        while quality >= 20:
            buffer = BytesIO()
            img.save(buffer, format='JPEG', quality=quality, optimize=True)
            data = buffer.getvalue()
            if len(data) <= max_size:
                print(f"压缩成功：{original_size/1024:.1f}KB -> {len(data)/1024:.1f}KB (quality={quality})")
                return data
            quality -= 5

        # 方法 2：缩小尺寸
        max_dim = 2048
        while max_dim >= 512:
            ratio = max_dim / max(img.width, img.height)
            if ratio < 1:
                new_size = (int(img.width * ratio), int(img.height * ratio))
                resized = img.resize(new_size, Image.Resampling.LANCZOS)
                buffer = BytesIO()
                resized.save(buffer, format='JPEG', quality=85, optimize=True)
                data = buffer.getvalue()
                if len(data) <= max_size:
                    print(f"压缩成功：{original_size/1024:.1f}KB -> {len(data)/1024:.1f}KB ({max_dim}px)")
                    return data
            max_dim -= 256

        # 如果还是太大，返回尽可能小的版本
        print("警告：无法压缩至 4MB 以内，返回最小版本")
        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=50, optimize=True)
        return buffer.getvalue()

    except ImportError:
        # PIL 不可用，直接返回原图
        print("警告：未安装 Pillow，无法压缩图片")
        with open(image_path, 'rb') as f:
            return f.read()
    except Exception as e:
        print(f"压缩失败：{e}，返回原图")
        with open(image_path, 'rb') as f:
            return f.read()

def encode_image_to_base64(image_path: Path, compress: bool = True) -> str:
    """
    将图片编码为 base64

    Args:
        image_path: 图片路径
        compress: 是否压缩图片（默认 True）

    Returns:
        base64 编码的图片数据
    """
    if compress:
        image_data = compress_image(image_path)
    else:
        with open(image_path, 'rb') as f:
            image_data = f.read()
    return base64.b64encode(image_data).decode('utf-8')

def get_image_mime_type(image_path: Path) -> str:
    """获取图片的 MIME 类型"""
    suffix = image_path.suffix.lower()
    mime_types = {
        '.jpg': 'image/jpeg',
        '.jpeg': 'image/jpeg',
        '.png': 'image/png',
        '.bmp': 'image/bmp',
        '.gif': 'image/gif',
        '.webp': 'image/webp'
    }
    return mime_types.get(suffix, 'image/jpeg')

def build_message_content(text: str, image_paths: list[Path]) -> list:
    """构建多模态消息内容"""
    content = []

    # 添加图片（自动压缩）
    for image_path in image_paths:
        mime_type = get_image_mime_type(image_path)
        # 使用压缩后的图片
        base64_data = encode_image_to_base64(image_path, compress=True)
        # OpenRouter/Qwen-VL 使用 image_url 格式
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{mime_type};base64,{base64_data}"
            }
        })

    # 添加文本
    if text:
        content.append({
            "type": "text",
            "text": text
        })

    return content

async def main():
    print("正在初始化 Deep Agent...")

    agent = await create_intelligent_deep_agent()

    print("Deep Agent 已就绪！输入 'exit' 退出")
    print("支持图片输入：可以直接输入图片路径，或文字 + 图片路径混合输入")
    print("支持的图片格式：jpg, jpeg, png, bmp, gif, webp")
    print("-" * 40)

    # 使用时间戳生成 thread_id，避免旧数据格式冲突
    import time
    thread_id = f"default_{int(time.time())}"

    while True:
        try:
            user_input = input("\n你：").strip()

            if user_input.lower() in ['exit', 'quit', 'q']:
                print("再见！")
                break

            if not user_input:
                continue

            # 检查是否是图片路径
            image_paths = []
            text_input = user_input

            # 检查输入是否是纯图片路径
            path = Path(user_input)
            if path.exists() and is_image_file(path):
                image_paths = [path]
                text_input = "请描述这张图片"
            else:
                # 检查输入中是否包含图片路径（以空格分隔）
                parts = user_input.split()
                valid_parts = []
                for part in parts:
                    p = Path(part)
                    if p.exists() and is_image_file(p):
                        image_paths.append(p)
                    else:
                        valid_parts.append(part)
                text_input = " ".join(valid_parts)

            # 构建消息
            if image_paths:
                # 多模态消息（图片 + 文本）
                messages_content = build_message_content(text_input, image_paths)
                messages = [{"role": "user", "content": messages_content}]
                print(f"\n📷 已加载 {len(image_paths)} 张图片：{[str(p) for p in image_paths]}")
            else:
                # 纯文本消息
                messages = [{"role": "user", "content": text_input}]

            print("\nAgent: ", end="", flush=True)

            async for event in agent.astream_events(
                {"messages": messages},
                version="v2",
                config={
                    "configurable": {"thread_id": thread_id},
                    "recursion_limit": 100
                }
            ):
                if event["event"] == "on_chat_model_stream":
                    chunk = event["data"]["chunk"]
                    if hasattr(chunk, "content") and chunk.content:
                        print(chunk.content, end="", flush=True)

            print()

        except KeyboardInterrupt:
            print("\n再见！")
            break
        except Exception as e:
            print(f"\n错误：{e}")

if __name__ == "__main__":
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    asyncio.run(main())
