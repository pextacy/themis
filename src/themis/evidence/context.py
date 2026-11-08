"""Repository grounding through MCP. Informational, so it can never contradict anything.

The prompt names only the repository. Submission text does not go near this call.
"""

from __future__ import annotations

from themis.evidence.base import EvidenceContext, make_check
from themis.models import Check, CheckStatus, Role
from themis.otari.errors import OtariBudgetExceeded, OtariConfigError, OtariError

MODULE = "context"
MAX_SUMMARY_CHARS = 1500


async def run(ctx: EvidenceContext) -> list[Check]:
    prompt = f"Use your tools to describe the main modules of the GitHub repository {ctx.repo} in five lines."
    try:
        reply = await ctx.stage.client.messages_stream(
            Role.CLERK,
            [{"role": "user", "content": prompt}],
            mcp_servers=ctx.stage.settings.mcp_servers,
            user=ctx.stage.submission.contributor_key,
            conversation_id=ctx.stage.conversation_id,
            max_tokens=400,
            stage="context",
        )
    except (OtariBudgetExceeded, OtariConfigError):
        raise
    except OtariError as error:
        return [make_check(MODULE, CheckStatus.UNVERIFIED, reason=str(error))]
    used = [call for call in reply.mcp_calls if not call.is_error]
    if not used:
        return [make_check(MODULE, CheckStatus.UNVERIFIED, reason="no successful MCP call")]
    check = make_check(MODULE, CheckStatus.NOT_APPLICABLE, summary=reply.content[:MAX_SUMMARY_CHARS], calls=len(used))
    return [check.model_copy(update={"otari_request_id": reply.call.request_id})]
