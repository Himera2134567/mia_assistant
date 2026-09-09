import threading

import mia_core
from mia_core import (
    ConversationStore,
    MIAConfig,
    MIAError,
    build_context,
    configured_provider_names,
    conversation_to_markdown,
    extract_wake_command,
    is_safe_transcript_correction,
    iter_sse_content,
    resolve_voice_command,
    stream_ai,
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


def test_direct_deepseek_key_in_legacy_variable_is_detected(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("MIA_PROVIDER_ORDER", "deepseek,openrouter")
    config = MIAConfig(api_key="sk-direct-test-value")
    assert configured_provider_names(config) == ["DeepSeek API"]


def test_stream_ai_falls_back_before_first_output(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-direct-test-value")
    monkeypatch.setenv("MIA_PROVIDER_ORDER", "deepseek,openrouter,local")
    monkeypatch.setattr(mia_core, "local_model_ready", lambda: True)
    attempts = []

    def fail_cloud(_messages, _config, provider, _emit, _cancel, _timeout):
        attempts.append(provider)
        raise MIAError(f"{provider} unavailable")

    def local_answer(_messages, _config, emit, _cancel):
        attempts.append("local")
        emit("готово")
        return "готово"

    monkeypatch.setattr(mia_core, "_stream_openai_compatible", fail_cloud)
    monkeypatch.setattr(mia_core, "_stream_local", local_answer)
    chunks = []
    providers = []
    answer = stream_ai(
        [{"role": "user", "content": "тест"}],
        MIAConfig(api_key="sk-or-v1-test-value"),
        chunks.append,
        providers.append,
        threading.Event(),
    )
    assert answer == "готово"
    assert chunks == ["готово"]
    assert attempts == ["deepseek", "openrouter", "local"]
    assert providers == ["deepseek", "openrouter", "local"]


def test_transcript_correction_must_preserve_command_meaning():
    assert is_safe_transcript_correction(
        "расскажы пра питон и гит хап",
        "Расскажи про Python и Git Hub",
    )
    assert not is_safe_transcript_correction(
        "Сколько будет 2 плюс 3?",
        "Два плюс три равно пять.",
    )
