from concurrent.futures import ThreadPoolExecutor

from mybot.core.events import CliEventSource
from mybot.core.history import HistoryMessage, HistoryStore


def test_history_store_does_not_lose_concurrent_messages(tmp_path):
    store = HistoryStore(tmp_path / "history")
    store.create_session("assistant", "session", CliEventSource())

    def save(index: int) -> None:
        store.save_message(
            "session", HistoryMessage(role="user", content=f"message {index}")
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(save, range(40)))

    assert len(store.get_messages("session")) == 40
    assert store.get_session_info("session").message_count == 40
