from mia_core import (
    ConversationStore,
    build_context,
    conversation_to_markdown,
    extract_wake_command,
    iter_sse_content,
    resolve_voice_command,
)


def test_conversation_store_round_trip_and_trim(tmp_path):
    store = ConversationStore(tmp_path / "memory" / "history.json", max_messages=2)
    store.save(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
            {"role": "system", "content": "not persisted"},
            {"role": "user", "content": "three"},
        ]
    )
    assert store.load() == [
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "three"},
    ]


def test_conversation_store_recovers_from_invalid_json(tmp_path):
    path = tmp_path / "history.json"
    path.write_text("not json", encoding="utf-8")
    assert ConversationStore(path).load() == []


def test_build_context_keeps_newest_messages():
    result = build_context(
        [
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "user", "content": "new"},
        ],
        system_prompt="system",
        max_chars=6,
    )
    assert result == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "new"},
    ]


def test_iter_sse_content_ignores_noise_and_reads_chunks():
    lines = [
        b"event: ping",
        b'data: {"choices":[{"delta":{"content":"M"}}]}',
        b'data: {"choices":[{"delta":{"content":"IA"}}]}',
        b"data: [DONE]",
    ]
    assert "".join(iter_sse_content(lines)) == "MIA"


def test_conversation_markdown():
    text = conversation_to_markdown(
        [
            {"role": "user", "content": "Привет"},
            {"role": "assistant", "content": "Здравствуйте"},
        ]
    )
    assert "## Вы" in text
    assert "## MIA" in text
    assert text.endswith("\n")


def test_extract_wake_command_from_same_phrase():
    assert extract_wake_command("Мия, расскажи про Python!") == (
        True,
        "расскажи про python",
    )


def test_extract_wake_command_supports_separate_wake_word():
    assert extract_wake_command("МИА") == (True, "")
    assert extract_wake_command("обычная фраза") == (False, "")


def test_constrained_wake_recovers_when_free_model_hears_mir():
    assert resolve_voice_command("мир расскажи анекдот", constrained_wake=True) == (
        True,
        "расскажи анекдот",
    )
