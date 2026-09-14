from mybot.utils.config import Config


def test_config_resolves_environment_secrets_and_masks_repr(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_LLM_API_KEY", "top-secret")
    (tmp_path / "config.user.yaml").write_text(
        """llm:
  provider: openai
  model: test-model
  api_key: ${TEST_LLM_API_KEY}
default_agent: assistant
""",
        encoding="utf-8",
    )

    config = Config.load(tmp_path)

    assert config.llm.api_key.get_secret_value() == "top-secret"
    assert "top-secret" not in repr(config.llm)
    assert config.research.vector_store.path == tmp_path / ".research/qdrant"
    assert config.research.metadata_store.path(tmp_path) == (
        tmp_path / ".research/research.db"
    )
    assert config.dispatch.path(tmp_path) == tmp_path / ".event/dispatch.db"
