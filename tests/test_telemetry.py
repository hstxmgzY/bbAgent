from research_assistant.telemetry import classify_error, sanitize_attributes


def test_telemetry_attributes_drop_sensitive_and_high_cardinality_values():
    attributes = sanitize_attributes(
        {
            "status": "ok",
            "provider": "test",
            "topic": "private question",
            "url": "https://secret.example/path",
            "session_id": "session-123",
            "api_key": "secret",
            "error_type": "full exception message with user input",
        }
    )

    assert attributes == {
        "status": "ok",
        "provider": "test",
        "error_type": "unknown",
    }
    assert classify_error("HTTP 503 from provider") == "http_5xx"
