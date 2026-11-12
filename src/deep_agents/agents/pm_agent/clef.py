"""Call Workers AI Clef for the PM's structured discussion decision."""

import json
from dataclasses import dataclass
from typing import Literal

import httpx

_MODEL = "@cf/cloudflare/clef-flash"


@dataclass(frozen=True, slots=True)
class ClefDecision:
    """A decision is unknown unless the complete provider response is valid."""

    outcome: Literal["pass", "reject", "unknown"]
    answer: dict | None = None
    error: str | None = None


async def judge_discussion(
    state: dict,
    *,
    account_id: str,
    token: str,
    client: httpx.AsyncClient | None = None,
) -> ClefDecision:
    """Judge the supplied discussion; never expose credentials or infer success."""
    if not account_id or not account_id.isalnum() or not token:
        return ClefDecision("unknown", error="clef_configuration_missing")
    if not isinstance(state, dict) or not state:
        return ClefDecision("unknown", error="discussion_state_missing")
    try:
        state_size = len(json.dumps(state, ensure_ascii=False).encode("utf-8"))
    except (TypeError, ValueError):
        return ClefDecision("unknown", error="discussion_state_invalid")
    # Workers AI may truncate long states; never authorize using partial evidence.
    if state_size > 16_000:
        return ClefDecision("unknown", error="discussion_state_too_large")
    payload = {
        "model": "clef-flash",
        "state": state,
        "questions": {
            "development_readiness": {
                "type": "choice",
                "instructions": (
                    "根据提供的原始讨论与确认，判断项目经理和开发负责人是否明确"
                    "同意同一个任务目标、范围、约束和可执行方案。重要分歧或未知"
                    "阻碍开发时不能通过。材料不足或确认不清楚时选择信息不足。"
                ),
                "criteria": {
                    "pass": "双方已明确确认同一方案，可开始该任务开发",
                    "reject": "存在实质分歧或阻止开发的已知问题",
                    "unknown": "材料不足，或无法确认双方对同一方案达成一致",
                },
            }
        },
    }
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{_MODEL}"
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        response = await client.post(
            url, headers={"Authorization": f"Bearer {token}"}, json=payload
        )
        response.raise_for_status()
        body = response.json()
        if not isinstance(body, dict) or body.get("success") is not True:
            return ClefDecision("unknown", error="clef_request_failed")
        result = body.get("result")
        answers = result.get("answers") if isinstance(result, dict) else None
        answer = answers.get("development_readiness") if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or answer.get("choice") not in {"pass", "reject", "unknown"}:
            return ClefDecision("unknown", error="clef_invalid_response")
        return ClefDecision(answer["choice"], answer=dict(answer))
    except (httpx.HTTPError, ValueError):
        return ClefDecision("unknown", error="clef_unavailable")
    finally:
        if own_client:
            await client.aclose()
