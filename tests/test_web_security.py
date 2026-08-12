import asyncio

import httpx
import pytest

import research_assistant.tools as tools_module
from research_assistant.tools import WebReadTool, _validate_public_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.1/internal",
        "http://[::1]/",
    ],
)
def test_validate_public_url_rejects_unsafe_targets(url):
    with pytest.raises(ValueError):
        asyncio.run(_validate_public_url(url))


def test_web_reader_revalidates_redirect_targets(monkeypatch):
    checked: list[str] = []

    async def validate(url: str) -> None:
        checked.append(url)
        if "127.0.0.1" in url:
            raise ValueError("private target")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})

    monkeypatch.setattr(tools_module, "_validate_public_url", validate)
    reader = WebReadTool(transport=httpx.MockTransport(handler))

    result = asyncio.run(reader.read("https://public.example/start"))

    assert result.ok is False
    assert "private target" in (result.error or "")
    assert checked == ["https://public.example/start", "http://127.0.0.1/secret"]
