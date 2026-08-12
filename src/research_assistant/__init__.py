"""Stage 2 research assistant package."""

__all__ = ["ResearchAssistant", "ResearchService"]


def __getattr__(name: str):
    if name == "ResearchAssistant":
        from research_assistant.assistant import ResearchAssistant

        return ResearchAssistant
    if name == "ResearchService":
        from research_assistant.service import ResearchService

        return ResearchService
    raise AttributeError(name)
