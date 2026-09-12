"""
Bilibili 中间件 — 搜索B站视频、提取AI字幕

提供两个工具给 Agent：
- bili_search: 按关键词搜索B站视频
- bili_extract: 从B站视频提取AI字幕（支持分集选择）

用法:
    from src.middlewares.tools.bilibili import BilibiliMiddleware, create_bilibili_middleware

    middleware = create_bilibili_middleware()
    agent = create_agent(
        model="...",
        tools=[],
        middleware=[middleware, ...],
    )
"""

from __future__ import annotations

import os
import re
import time
from typing import Any, ClassVar

from langchain.agents.middleware import AgentMiddleware
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

import requests

from src.middlewares.cache.providers import CacheBackend, JsonFileCacheBackend, create_default_cache

# ═══════════════════════════════════════════════
# 常量
# ═══════════════════════════════════════════════

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://www.bilibili.com",
}

_SEARCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Referer": "https://search.bilibili.com",
}

# 本项目根目录
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")

LANG_NAMES = {
    "ai-zh": "中文（AI）", "zh-CN": "中文", "zh-Hans": "中文（简体）", "zh-Hant": "中文（繁体）",
    "en": "英文", "ja": "日文", "es": "西班牙语", "ar": "阿拉伯语", "pt": "葡萄牙语",
    "id": "印尼语", "th": "泰语", "vi": "越南语", "ru": "俄语", "de": "德语", "fr": "法语", "ko": "韩语",
}

# ═══════════════════════════════════════════════
# Cookie 加载
# ═══════════════════════════════════════════════

def _load_cookies() -> dict[str, str]:
    """加载 Bilibili Cookie，优先级: .env 文件 > 环境变量

    .env 文件格式:
        BILIBILI_COOKIE="buvid3=...; SESSDATA=..."
    """
    s = ""

    # 1. 从 .env 文件读取
    try:
        with open(_ENV_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BILIBILI_COOKIE="):
                    s = line.split("=", 1)[1].strip("\"'")
                    break
    except Exception:
        pass

    # 2. 从环境变量读取
    if not s:
        s = os.environ.get("BILIBILI_COOKIE", "")

    # 解析 Cookie 字符串
    cookies = {}
    for item in s.split("; "):
        if "=" in item:
            k, v = item.split("=", 1)
            cookies[k] = v
    return cookies


# ═══════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════

def _fmt_num(n: Any) -> str:
    """格式化大数字，如 10000 -> 1.0万"""
    try:
        n = int(n)
        return f"{n / 10000:.1f}万" if n >= 10000 else str(n)
    except (ValueError, TypeError):
        return str(n)


def _fmt_dur(s: Any) -> str:
    """格式化时长，秒 -> M:SS"""
    try:
        s = int(s)
        return f"{s // 60}:{s % 60:02d}"
    except (ValueError, TypeError):
        s = str(s)
        return s if ":" in s else s + "?"


def _fmt_time(ts: Any) -> str:
    """时间戳 -> MM-DD"""
    if not ts:
        return "?"
    try:
        return time.strftime("%m-%d", time.localtime(int(ts)))
    except (ValueError, TypeError):
        return "?"


def _clean_title(t: str) -> str:
    """去掉标题中的 HTML 标签"""
    return re.sub(r"<[^>]+>", "", t)


def _extract_bvid(text: str) -> str | None:
    """从文本中提取 BV 号"""
    m = re.search(r"BV[a-zA-Z0-9]+", text)
    return m.group(0) if m else None


# ═══════════════════════════════════════════════
# 中间件
# ═══════════════════════════════════════════════

class BilibiliMiddleware(AgentMiddleware):
    """Bilibili 搜索与字幕提取中间件

    为 Agent 提供两个工具：
    - ``bili_search``: 搜索 B 站视频
    - ``bili_extract``: 提取 B 站视频 AI 字幕

    使用时传入 ``middleware`` 参数即可：

    .. code-block:: python

        from src.middlewares.tools.bilibili import create_bilibili_middleware

        agent = create_agent(
            model="...",
            middleware=[create_bilibili_middleware(), ...],
        )
    """

    def __init__(self, cache: CacheBackend | None = None):
        super().__init__()
        self._cookies = _load_cookies()
        self._tools_cache: list | None = None
        self._cache = cache if cache is not None else create_default_cache()

    # ── 序列化兼容（LangGraph checkpoint） ──

    def __getstate__(self) -> dict:
        state = self.__dict__.copy()
        state.pop("_tools_cache", None)
        state.pop("_cache", None)
        return state

    def __setstate__(self, state: dict) -> None:
        self.__dict__.update(state)
        self._tools_cache = None
        self._cache = create_default_cache()

    # ── 工具暴露 ──

    @property
    def tools(self) -> list:
        if self._tools_cache is None:
            self._tools_cache = [
                self._create_search_tool(),
                self._create_extract_tool(),
            ]
        return self._tools_cache

    # ═══════════════════════════════════════════
    # bili_search 工具
    # ═══════════════════════════════════════════

    class _BiliSearchInput(BaseModel):
        """bili_search 的输入参数"""
        keyword: str = Field(description="搜索关键词，如「高等数学」「机器学习」「尼古喵喵」")
        page: int = Field(default=1, description="页码，从 1 开始", ge=1)

    def _create_search_tool(self) -> StructuredTool:
        description = """搜索 B 站视频，返回标题、播放量、时长、UP 主等信息。

## 参数
- ``keyword``: 搜索关键词
- ``page``: 页码（默认 1）

## 返回格式
每行一个视频，格式为：
    标题
    播放量  弹幕数  时长
    UP主 · 日期  BV号

## 示例
- 搜索「高等数学」: bili_search(keyword="高等数学")
- 翻到第 2 页: bili_search(keyword="高等数学", page=2)

## 说明
- 需要设置 BILIBILI_COOKIE 才能获得完整结果（见 .env 文件）
- 搜索结果来自 B 站搜索 API，每页最多 20 条
"""

        def search_sync(
            keyword: str,
            page: int = 1,
        ) -> str:
            """搜索 B 站视频（同步版本）"""
            # ── 缓存查找 ──
            cache_key = f"search:{keyword.strip().lower()}:{page}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            # ── API 请求 ──
            cookies = self._cookies

            try:
                resp = requests.get(
                    "https://api.bilibili.com/x/web-interface/search/type",
                    params={
                        "search_type": "video",
                        "keyword": keyword,
                        "page": page,
                        "page_size": 20,
                    },
                    headers=_SEARCH_HEADERS,
                    cookies=cookies,
                    timeout=15,
                )
                data = resp.json()
            except requests.exceptions.RequestException as e:
                return f"搜索请求失败: {e}"
            except Exception as e:
                return f"解析响应失败: {e}"

            if data.get("code") != 0:
                return f"B站API错误({data['code']}): {data.get('message', '未知')}"

            result_list = data.get("data", {}).get("result", [])
            if not result_list:
                return f"未找到「{keyword}」的相关视频。"

            lines = [f"共找到以下「{keyword}」的视频（第{page}页）:", ""]
            for r in result_list:
                title = _clean_title(r.get("title", ""))
                bvid = r.get("bvid", "")
                author = r.get("author", "")
                plays = _fmt_num(r.get("play", 0))
                danmaku = _fmt_num(r.get("video_review", 0))
                dur = _fmt_dur(r.get("duration", ""))
                pub = _fmt_time(r.get("pubdate", 0))
                lines.append(f"{title}")
                lines.append(f"{plays}  {danmaku}  {dur}")
                lines.append(f"{author} · {pub}  {bvid}")
                lines.append("")

            result_text = "\n".join(lines).strip()
            # ── 写入缓存 ──
            self._cache.set(cache_key, "search", result_text)
            return result_text

        return StructuredTool.from_function(
            func=search_sync,
            name="bili_search",
            description=description,
            args_schema=self._BiliSearchInput,
        )

    # ═══════════════════════════════════════════
    # bili_extract 工具
    # ═══════════════════════════════════════════

    class _BiliExtractInput(BaseModel):
        """bili_extract 的输入参数"""
        bvid: str = Field(description="B站视频 BV 号，如 BV1CAxaeHEeH")
        part: int | None = Field(default=None, description="分 P 序号（从 1 开始）。不填则列出所有分 P")
        lang: str = Field(default="zh", description="字幕语言代码，如 zh/en/ja/es/ar/pt 等")
        max_lines: int = Field(default=0, description="最多显示字幕条数，0=不限制")

    def _get_video_info(self, bvid: str) -> tuple[dict | None, str | None]:
        """获取视频信息（标题、分集列表等）"""
        try:
            resp = requests.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bvid},
                headers=HEADERS,
                cookies=self._cookies,
                timeout=15,
            )
            data = resp.json()
        except requests.exceptions.RequestException as e:
            return None, f"网络请求失败: {e}"
        except Exception as e:
            return None, f"解析响应失败: {e}"

        if data.get("code") != 0:
            return None, f"B站API错误({data['code']}): {data.get('message', '未知')}"
        return data.get("data"), None

    def _get_subtitles(self, aid: int, cid: int) -> list:
        """获取指定 aid+cid 的字幕列表"""
        try:
            resp = requests.get(
                f"https://api.bilibili.com/x/player/wbi/v2?aid={aid}&cid={cid}",
                headers=HEADERS,
                cookies=self._cookies,
                timeout=15,
            )
            data = resp.json()
        except Exception:
            return []

        if data.get("code") != 0:
            return []
        return data.get("data", {}).get("subtitle", {}).get("subtitles", [])

    def _download_subtitle(self, url: str) -> list:
        """下载字幕 JSON 内容"""
        if url.startswith("//"):
            url = "https:" + url
        try:
            resp = requests.get(url, headers=HEADERS, cookies=self._cookies, timeout=15)
            return resp.json().get("body", [])
        except Exception:
            return []

    def _create_extract_tool(self) -> StructuredTool:
        description = """提取 B 站视频的 AI 字幕。

## 参数说明
- ``bvid`` (必填): BV 号，如 BV1CAxaeHEeH
- ``part`` (可选，默认不填): 分 P 序号。不填则列出所有分 P 让你选择
- ``lang`` (可选，默认 zh): 字幕语言。zh=中文，en=英文，ja=日文，es=西班牙语等
- ``max_lines`` (可选，默认 0): **最多显示几条字幕**。0=全部显示不限制

## 关于 max_lines 的重要使用说明
字幕条数可能非常多（几百条），**必须根据你的用途来设置**：

- **只看重点** → 设 max_lines=30，只看开头部分的内容
- **大致了解** → 设 max_lines=100，看前面一部分就够了
- **需要完整内容用于教学/分析** → 不设 max_lines（默认 0=全部），拿到全部字幕
- **如果后续要提取多个分集** → 每集先用 max_lines=30 快速浏览，选中重点分集后再拉满

## 使用示例
| 用途 | 命令 |
|------|------|
| 查看视频分集 | ``bili_extract(bvid="BV1CAxaeHEeH")`` |
| 提取第1集全部字幕 | ``bili_extract(bvid="BV1CAxaeHEeH", part=1)`` |
| 只看第1集前30条 | ``bili_extract(bvid="BV1CAxaeHEeH", part=1, max_lines=30)`` |
| 英文字幕前100条 | ``bili_extract(bvid="BV1CAxaeHEeH", part=1, lang="en", max_lines=100)`` |

## 返回格式
- 列出分集时：每行格式为「[序号] 标题 (时长)」
- 提取字幕时：返回标题、分集名、语言、总条数、以及带时间轴的字幕内容
- 无字幕时：提示该视频没有 AI 字幕

## 说明
- 需要 B站登录 Cookie（SESSDATA）才能获取字幕
- Cookie 请在项目 .env 文件中设置 BILIBILI_COOKIE
- 仅支持 AI 字幕，UP 主上传的自定义字幕可能无法提取
"""

        def extract_sync(
            bvid: str,
            part: int | None = None,
            lang: str = "zh",
            max_lines: int = 0,
        ) -> str:
            """提取字幕（同步版本）"""
            bvid_clean = _extract_bvid(bvid)
            if not bvid_clean:
                return f"错误: 无法从「{bvid}」中识别 BV 号"

            # ── 缓存查找 ──
            cache_key = f"extract:{bvid_clean}:{part}:{lang.lower()}:max{max_lines}"
            cached = self._cache.get(cache_key)
            if cached is not None:
                return cached

            # 1. 获取视频信息
            info, err = self._get_video_info(bvid_clean)
            if err:
                return f"获取视频信息失败: {err}"
            if not info:
                return "获取视频信息失败: 返回为空"

            title = info.get("title", bvid_clean)
            pages = info.get("pages", [])
            aid = info.get("aid")

            if not pages:
                return f"视频「{title}」没有分集信息。"

            # 2. 如果未指定分集，列出所有分集
            if part is None:
                lines = [f"📹 {title}", f"共 {len(pages)} 个分集:", ""]
                for i, p in enumerate(pages, 1):
                    dur = p.get("duration", 0)
                    part_title = p.get("part", f"第{i}集")
                    lines.append(f"  [{i}] {part_title}  ({_fmt_dur(dur)})")
                lines.append("")
                lines.append("提示: 指定 part 参数提取字幕，例如:")
                lines.append(f'  bili_extract(bvid="{bvid_clean}", part=1)')
                result_text = "\n".join(lines)
                self._cache.set(cache_key, "extract", result_text)
                return result_text

            # 3. 验证 part 范围
            if part < 1 or part > len(pages):
                return f"错误: part={part} 超出范围，该视频只有 {len(pages)} 个分集（1-{len(pages)}）"

            # 4. 获取字幕
            page = pages[part - 1]
            cid = page["cid"]
            part_title = page.get("part", f"第{part}集")
            dur = page.get("duration", 0)

            subtitles = self._get_subtitles(aid, cid)
            if not subtitles:
                result_text = (
                    f"📹 {title}\n"
                    f"📺 第{part}集: {part_title} ({_fmt_dur(dur)})\n"
                    f"⚠️  该视频没有 AI 字幕\n\n"
                    f"提示: 并非所有 B 站视频都有 AI 字幕，可尝试其他分集。"
                )
                self._cache.set(cache_key, "extract", result_text)
                return result_text

            # 5. 选择字幕语言
            target = None
            if lang:
                # 精确匹配
                for s in subtitles:
                    if s.get("lan") == lang:
                        target = s
                        break
                # 前缀匹配（如 "zh" 匹配 "zh-CN"）
                if not target:
                    for s in subtitles:
                        if s.get("lan", "").startswith(lang):
                            target = s
                            break
            if not target:
                # 默认中文
                for s in subtitles:
                    if s.get("lan", "") in ("ai-zh", "zh-CN") or s.get("lan", "").startswith("zh"):
                        target = s
                        break
            if not target:
                target = subtitles[0]

            # 6. 下载字幕
            body = self._download_subtitle(target["subtitle_url"])
            if not body:
                return f"下载字幕失败（{target.get('lan_doc', target['lan'])}）"

            # 7. 格式化输出
            lan_doc = target.get("lan_doc", LANG_NAMES.get(target["lan"], target["lan"]))
            lines = [
                f"📹 {title}",
                f"📺 第{part}集: {part_title} ({_fmt_dur(dur)})",
                f"📝 字幕: {lan_doc}  |  共 {len(body)} 条",
                "",
            ]

            # 显示字幕内容（max_lines=0 为不限制）
            show_count = len(body) if max_lines <= 0 else min(max_lines, len(body))
            for i, b in enumerate(body[:show_count]):
                if i % 10 == 0:
                    lines.append(f"{b['content']} [{b['from']:6.1f}s - {b['to']:6.1f}s]")
                else:
                    lines.append(b['content'])

            if show_count < len(body):
                lines.append(f"\n... 共 {len(body)} 条字幕，仅显示前 {show_count} 条（指定 max_lines 查看更多）")

            result_text = "\n".join(lines)
            # ── 写入缓存 ──
            self._cache.set(cache_key, "extract", result_text)
            return result_text

        return StructuredTool.from_function(
            func=extract_sync,
            name="bili_extract",
            description=description,
            args_schema=self._BiliExtractInput,
        )


# ═══════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════

def create_bilibili_middleware(
    cache: CacheBackend | None = None,
) -> BilibiliMiddleware:
    """创建 Bilibili 中间件的便捷函数

    Args:
        cache: 缓存后端实例。为 None 时默认使用 ``JsonFileCacheBackend``。

    Returns:
        BilibiliMiddleware 实例
    """
    return BilibiliMiddleware(cache=cache)
