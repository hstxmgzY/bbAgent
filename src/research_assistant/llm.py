"""LLM synthesis with an extractive fallback."""

import os

from research_assistant.models import Chunk


class LiteLLMAnswerSynthesizer:
    async def synthesize(
        self, topic: str, evidence: list[Chunk], warnings: list[str]
    ) -> str:
        return await synthesize_answer(topic, evidence, warnings)


async def synthesize_answer(
    topic: str, evidence: list[Chunk], warnings: list[str]
) -> str:
    if not evidence:
        return (
            "没有找到足够证据来回答这个主题。请换一个更具体的主题，"
            "或配置 BRAVE_API_KEY 后重试。"
        )

    if not _has_llm_key():
        return _fallback_answer(topic, evidence, warnings)

    context = "\n\n".join(
        f"[{chunk.source_id}] {chunk.title}\nURL: {chunk.url}\n{chunk.text[:1800]}"
        for chunk in evidence
    )
    messages = [
        {
            "role": "system",
            "content": (
                "You are a careful research assistant. Answer in Chinese. "
                "Use only the provided evidence. Cite every factual claim with "
                "source ids like [S1]. If evidence is weak, say so."
            ),
        },
        {
            "role": "user",
            "content": f"Topic: {topic}\n\nEvidence:\n{context}\n\nWrite a concise research answer.",
        },
    ]
    try:
        from litellm import acompletion

        response = await acompletion(
            model=os.getenv("LITELLM_MODEL", "gpt-4.1-mini"),
            messages=messages,
            temperature=0.2,
        )
        return response.choices[0].message.content or _fallback_answer(
            topic, evidence, warnings
        )
    except Exception as exc:
        warnings.append(
            f"LLM synthesis failed ({type(exc).__name__}); used extractive fallback"
        )
        return _fallback_answer(topic, evidence, warnings)


def _fallback_answer(topic: str, evidence: list[Chunk], warnings: list[str]) -> str:
    lines = [
        f"下面是关于“{topic}”的资料摘录式总结。当前没有可用 LLM API，",
        "所以我只基于检索片段做保守归纳：",
        "",
    ]
    for chunk in evidence[:5]:
        excerpt = " ".join(chunk.text.split())[:360]
        lines.append(f"- {excerpt}... [{chunk.source_id}]")
    if warnings:
        lines.extend(["", "注意：" + "；".join(warnings)])
    return "\n".join(lines)


def fallback_answer(topic: str, evidence: list[Chunk], warnings: list[str]) -> str:
    """Build a deterministic answer whose citations come from retrieved evidence."""
    if not evidence:
        return (
            "没有找到足够证据来回答这个主题。请换一个更具体的主题，"
            "或配置 BRAVE_API_KEY 后重试。"
        )
    return _fallback_answer(topic, evidence, warnings)


def _has_llm_key() -> bool:
    key_names = (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "LITELLM_API_KEY",
    )
    return any(os.getenv(name) for name in key_names)
