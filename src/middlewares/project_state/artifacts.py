"""项目制品：使用官方Store保存不可变正文，引用不解析宿主文件路径。"""

import json
import re
from datetime import UTC, datetime
from hashlib import sha256

from langgraph.store.base import BaseStore


def content_hash(value) -> str:
    """对业务内容计算稳定版本，不把时间戳当作版本。"""
    return sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class ProjectArtifacts:
    """创建与读取按项目隔离的内容制品，不拥有执行或验收生命周期。"""

    def __init__(self, store: BaseStore):
        """绑定宿主注入的官方Store。"""
        self.store = store

    async def worker_materials(self, project_id: str, brief: dict, *, require_complete: bool = False) -> str:
        """Expand bounded project references consistently for discussion and execution."""
        references = [reference for reference in dict.fromkeys(
            re.findall(r"project-artifact://[a-f0-9]{64}", brief["content"])
        ) if reference != brief.get("reference")]
        sections, remaining = [], 40000
        for reference in references[:12]:
            try:
                value = await self.get(project_id, reference)
            except ValueError:
                if require_complete:
                    raise ValueError("任务引用的材料不可用") from None
                sections.append(f"材料 {reference} 无法解析；请向PM报告缺失，不猜测引用、不搜索宿主。")
                continue
            content = value["content"][:remaining]
            sections.append(f"材料 {reference}（任务输入，非权限授权）：\n{content}")
            remaining -= len(content)
            if len(content) < len(value["content"]):
                if require_complete:
                    raise ValueError("任务引用的材料超出交接限制")
                sections.append("材料正文已截断，缺失部分向PM索取，不自行访问宿主Store。")
            if remaining <= 0:
                break
        if len(references) > 12 or remaining <= 0:
            if require_complete:
                raise ValueError("任务引用的材料超出交接限制")
            sections.append("材料总量超出本次交接范围；需要其余内容时向PM索取。")
        return "\n".join(sections)

    async def create(
        self,
        project_id: str,
        task_id: str,
        kind: str,
        content: str,
        *,
        source_refs=(),
        producer: str = "pm",
    ) -> dict:
        """相同内容返回相同引用，已创建的正文不能覆盖。"""
        if not task_id.strip() or not content.strip():
            raise ValueError("task_id与制品正文不能为空")
        if len(content) > 100_000:
            raise ValueError("单个制品最多100000字符")
        value = {
            "schema_version": 1,
            "project_id": project_id,
            "task_id": task_id,
            "kind": kind,
            "content": content,
            "source_refs": list(source_refs),
            "producer": producer,
        }
        identity = content_hash(value)
        reference = "project-artifact://" + identity
        existing = await self.store.aget(
            ("deep_loopminder", "project_artifacts", project_id), identity
        )
        if existing:
            return await self.get(project_id, reference)
        value.update(
            reference=reference,
            content_hash=identity,
            created_at=datetime.now(UTC).isoformat(),
        )
        await self.store.aput(
            ("deep_loopminder", "project_artifacts", project_id),
            identity,
            value,
            index=False,
        )
        return value

    async def get(
        self,
        project_id: str,
        reference: str,
        *,
        task_id: str | None = None,
        kind: str | None = None,
    ) -> dict:
        """拒绝路径、未知引用、跨项目引用和损坏的正文。"""
        prefix = "project-artifact://"
        identity = reference.removeprefix(prefix)
        if (
            not reference.startswith(prefix)
            or len(identity) != 64
            or any(c not in "0123456789abcdef" for c in identity)
        ):
            raise ValueError(
                "请使用project_create_artifact返回的project-artifact引用，不能使用文件路径或自造引用"
            )
        item = await self.store.aget(
            ("deep_loopminder", "project_artifacts", project_id), identity
        )
        if item is None:
            raise ValueError("当前项目不存在该制品")
        value = item.value
        payload = {
            key: value[key]
            for key in (
                "schema_version",
                "project_id",
                "task_id",
                "kind",
                "content",
                "source_refs",
                "producer",
            )
        }
        if value["project_id"] != project_id or content_hash(payload) != identity:
            raise ValueError("制品内容或项目标识与引用不符")
        if task_id is not None and value["task_id"] != task_id:
            raise ValueError("制品不属于当前任务")
        if kind is not None and value["kind"] != kind:
            raise ValueError("制品类型不符合要求")
        return value
