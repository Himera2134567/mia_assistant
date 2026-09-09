"""Core services for MIA that do not depend on the Qt user interface."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

import requests

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"
OLLAMA_API_URL = "http://127.0.0.1:11434/api/chat"
DEFAULT_LOCAL_MODEL_PATH = Path("models/llm/qwen2.5-3b-instruct-q4_k_m.gguf")

_LOCAL_LLM = None
_LOCAL_LLM_LOAD_LOCK = threading.Lock()
_LOCAL_LLM_GENERATION_LOCK = threading.Lock()
DEFAULT_SYSTEM_PROMPT = (
    "Ты MIA — персональный ИИ-ассистент в духе Джарвиса. "
    "Отвечай на языке пользователя, будь инициативной, точной и практичной. "
    "Сначала давай прямой ответ, затем детали, если они нужны. "
    "Не выдумывай факты и честно отмечай неопределённость. "
    "Для кода используй Markdown и указывай, куда вставлять изменения."
)


class MIAError(RuntimeError):
    """An actionable error that can be safely shown in the UI."""


@dataclass(frozen=True)
class MIAConfig:
    api_key: str
    model: str = "deepseek/deepseek-chat"
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    temperature: float = 0.45
    max_tokens: int = 2000
    provider: str = "auto"

    @classmethod
    def from_env(cls) -> MIAConfig:
        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            model=os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat").strip()
            or "deepseek/deepseek-chat",
            system_prompt=os.getenv("MIA_SYSTEM_PROMPT", "").strip()
            or DEFAULT_SYSTEM_PROMPT,
            provider=os.getenv("MIA_PROVIDER", "auto").strip().casefold() or "auto",
        )


def _headers(api_key: str) -> dict[str, str]:
    if not api_key.strip():
        raise MIAError(
            "Не задан OPENROUTER_API_KEY. Скопируй .env.example в .env, "
            "добавь свой ключ и перезапусти MIA."
        )
    return {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://github.com/Himera2134567/mia_assistant",
        "X-Title": "MIA Assistant",
    }


def _api_error(response: requests.Response) -> str:
    message = ""
    try:
        payload = response.json()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            message = str(error.get("message", "")).strip()
        elif error:
            message = str(error).strip()
    except (ValueError, TypeError):
        pass

    if response.status_code == 401:
        return "OpenRouter отклонил API-ключ. Проверь OPENROUTER_API_KEY в .env."
    if response.status_code == 402:
        return "На аккаунте OpenRouter недостаточно средств или исчерпан лимит."
    if response.status_code == 429:
        return "OpenRouter временно ограничил частоту запросов. Попробуй чуть позже."
    suffix = f": {message}" if message else ""
    return f"Ошибка OpenRouter HTTP {response.status_code}{suffix}"


def build_context(
    history: Sequence[Mapping[str, str]],
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    max_chars: int = 60_000,
) -> list[dict[str, str]]:
    """Build a bounded model context while preserving the newest messages."""
    valid: list[dict[str, str]] = []
    used = 0
    for item in reversed(history):
        role = str(item.get("role", ""))
        content = str(item.get("content", "")).strip()
        if role not in {"user", "assistant"} or not content:
            continue
        if valid and used + len(content) > max_chars:
            break
        valid.append({"role": role, "content": content})
        used += len(content)
    valid.reverse()
    return [{"role": "system", "content": system_prompt}, *valid]


def complete_openrouter(
    messages: Sequence[Mapping[str, str]],
    config: MIAConfig,
    timeout: int = 90,
) -> str:
    try:
        response = requests.post(
            OPENROUTER_API_URL,
            headers=_headers(config.api_key),
            json={
                "model": config.model,
                "messages": list(messages),
                "temperature": config.temperature,
                "max_tokens": config.max_tokens,
            },
            timeout=timeout,
        )
    except requests.Timeout as exc:
        raise MIAError("OpenRouter не ответил вовремя. Повтори запрос.") from exc
    except requests.RequestException as exc:
        raise MIAError(f"Нет связи с OpenRouter: {exc}") from exc
    if not response.ok:
        raise MIAError(_api_error(response))
    try:
        answer = response.json()["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise MIAError("OpenRouter вернул ответ в неизвестном формате.") from exc
    if not isinstance(answer, str) or not answer.strip():
        raise MIAError("Модель вернула пустой ответ.")
    return answer.strip()


def iter_sse_content(lines: Iterable[str | bytes]) -> Iterator[str]:
    """Yield text deltas from OpenAI-compatible SSE lines."""
    for raw_line in lines:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="replace")
        else:
            line = raw_line
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("error"):
            error = payload["error"]
            detail = error.get("message", str(error)) if isinstance(error, dict) else str(error)
            raise MIAError(f"Ошибка OpenRouter: {detail}")
        try:
            content = payload["choices"][0]["delta"].get("content")
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(content, str) and content:
            yield content


def stream_openrouter(
    messages: Sequence[Mapping[str, str]],
    config: MIAConfig,
    on_chunk: Callable[[str], None],
    cancel_event: threading.Event | None = None,
    timeout: int = 120,
) -> str:
    cancel_event = cancel_event or threading.Event()
    chunks: list[str] = []
    try:
        with requests.post(
            OPENROUTER_API_URL,
            headers=_headers(config.api_key),
            json={
                "model": config.model,
                "messages": list(messages),
                "temperature": config.temperature,
                "max_tokens": config.max_tokens,
                "stream": True,
            },
            timeout=(15, timeout),
            stream=True,
        ) as response:
            if not response.ok:
                raise MIAError(_api_error(response))
            for chunk in iter_sse_content(response.iter_lines()):
                if cancel_event.is_set():
                    break
                chunks.append(chunk)
                on_chunk(chunk)
    except requests.Timeout as exc:
        raise MIAError("OpenRouter не ответил вовремя. Повтори запрос.") from exc
    except requests.RequestException as exc:
        raise MIAError(f"Нет связи с OpenRouter: {exc}") from exc
    answer = "".join(chunks).strip()
    if not answer and not cancel_event.is_set():
        raise MIAError("Модель вернула пустой ответ.")
    return answer


def fetch_openrouter_models(api_key: str, timeout: int = 25) -> list[str]:
    try:
        response = requests.get(
            OPENROUTER_MODELS_URL,
            headers=_headers(api_key),
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise MIAError(f"Не удалось загрузить список моделей: {exc}") from exc
    if not response.ok:
        raise MIAError(_api_error(response))
    try:
        model_ids = {
            str(item["id"])
            for item in response.json().get("data", [])
            if isinstance(item, dict) and item.get("id")
        }
    except (AttributeError, TypeError, ValueError) as exc:
        raise MIAError("OpenRouter вернул некорректный список моделей.") from exc
    return sorted(model_ids, key=str.casefold)


def _direct_key(config: MIAConfig) -> str:
    key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if (
        not key
        and config.api_key.startswith("sk-")
        and not config.api_key.startswith("sk-or-v1-")
    ):
        # Earlier MIA versions documented only OPENROUTER_API_KEY. Accept a
        # direct DeepSeek key stored there and route it to the correct endpoint.
        key = config.api_key
    return key


def _provider_order(config: MIAConfig) -> list[str]:
    requested = config.provider.strip().casefold()
    if requested and requested != "auto":
        return [requested]
    configured = os.getenv(
        "MIA_PROVIDER_ORDER",
        "deepseek,openrouter,local,ollama",
    )
    allowed = {"deepseek", "openrouter", "local", "ollama"}
    result = [item.strip().casefold() for item in configured.split(",")]
    result = [item for item in result if item in allowed]
    return result or ["deepseek", "openrouter", "local", "ollama"]


def provider_display_name(provider: str) -> str:
    return {
        "deepseek": "DeepSeek API",
        "openrouter": "OpenRouter",
        "local": "Qwen2.5 · локально",
        "ollama": "Ollama · локально",
    }.get(provider, provider)


def local_model_path() -> Path:
    configured = os.getenv("MIA_LOCAL_MODEL_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path
    else:
        path = DEFAULT_LOCAL_MODEL_PATH
    data_dir = Path(os.getenv("MIA_DATA_DIR", Path.cwd()))
    return data_dir / path


def local_model_ready() -> bool:
    path = local_model_path()
    return (
        importlib.util.find_spec("llama_cpp") is not None
        and path.is_file()
        and path.stat().st_size > 100 * 1024 * 1024
    )


def ollama_ready(timeout: float = 0.25) -> bool:
    chat_url = os.getenv("OLLAMA_API_URL", OLLAMA_API_URL).strip() or OLLAMA_API_URL
    tags_url = chat_url.rsplit("/api/", 1)[0] + "/api/tags"
    try:
        return requests.get(tags_url, timeout=timeout).ok
    except requests.RequestException:
        return False


def configured_provider_names(config: MIAConfig) -> list[str]:
    names: list[str] = []
    direct_key = _direct_key(config)
    openrouter_key = (
        config.api_key if config.api_key.startswith("sk-or-v1-") else ""
    )
    for provider in _provider_order(config):
        available = (
            (provider == "deepseek" and bool(direct_key))
            or (provider == "openrouter" and bool(openrouter_key))
            or (provider == "local" and local_model_ready())
            or (provider == "ollama" and ollama_ready())
        )
        if available:
            names.append(provider_display_name(provider))
    return names


def _provider_error(response: requests.Response, provider: str) -> str:
    message = ""
    try:
        payload = response.json()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            message = str(error.get("message", "")).strip()
        elif error:
            message = str(error).strip()
    except (ValueError, TypeError):
        pass
    label = provider_display_name(provider)
    if response.status_code == 401:
        return f"{label}: API-ключ отклонён"
    if response.status_code == 402:
        return f"{label}: недостаточно средств или исчерпан лимит"
    if response.status_code == 429:
        return f"{label}: превышен лимит запросов"
    suffix = f" · {message}" if message else ""
    return f"{label}: HTTP {response.status_code}{suffix}"


def _openai_headers(api_key: str, provider: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {api_key.strip()}",
        "Content-Type": "application/json",
    }
    if provider == "openrouter":
        headers.update(
            {
                "HTTP-Referer": "https://github.com/Himera2134567/mia_assistant",
                "X-Title": "MIA Assistant",
            }
        )
    return headers


def _provider_target(config: MIAConfig, provider: str) -> tuple[str, str, str]:
    if provider == "deepseek":
        configured_model = os.getenv("DEEPSEEK_MODEL", "").strip()
        model = (
            config.model
            if config.model.startswith("deepseek-v4-")
            else configured_model or "deepseek-v4-flash"
        )
        return (
            DEEPSEEK_API_URL,
            _direct_key(config),
            model,
        )
    if provider == "openrouter":
        key = config.api_key if config.api_key.startswith("sk-or-v1-") else ""
        return OPENROUTER_API_URL, key, config.model
    if provider == "ollama":
        return (
            os.getenv("OLLAMA_API_URL", OLLAMA_API_URL).strip() or OLLAMA_API_URL,
            "",
            os.getenv("OLLAMA_MODEL", "qwen3:4b").strip() or "qwen3:4b",
        )
    raise MIAError(f"Неизвестный ИИ-провайдер: {provider}")


def _stream_openai_compatible(
    messages: Sequence[Mapping[str, str]],
    config: MIAConfig,
    provider: str,
    on_chunk: Callable[[str], None],
    cancel_event: threading.Event,
    timeout: int,
) -> str:
    api_url, api_key, model = _provider_target(config, provider)
    if not api_key:
        raise MIAError(f"{provider_display_name(provider)}: API-ключ не настроен")
    chunks: list[str] = []
    try:
        with requests.post(
            api_url,
            headers=_openai_headers(api_key, provider),
            json={
                "model": model,
                "messages": list(messages),
                "temperature": config.temperature,
                "max_tokens": config.max_tokens,
                "stream": True,
            },
            timeout=(15, timeout),
            stream=True,
        ) as response:
            if not response.ok:
                raise MIAError(_provider_error(response, provider))
            for chunk in iter_sse_content(response.iter_lines()):
                if cancel_event.is_set():
                    break
                chunks.append(chunk)
                on_chunk(chunk)
    except requests.Timeout as exc:
        raise MIAError(f"{provider_display_name(provider)} не ответил вовремя") from exc
    except requests.RequestException as exc:
        raise MIAError(f"Нет связи с {provider_display_name(provider)}: {exc}") from exc
    answer = "".join(chunks).strip()
    if not answer and not cancel_event.is_set():
        raise MIAError(f"{provider_display_name(provider)} вернул пустой ответ")
    return answer


def _get_local_llm():
    global _LOCAL_LLM
    if not local_model_ready():
        raise MIAError(
            "Локальная модель не установлена · выполни setup.ps1 -WithLocalAI"
        )
    with _LOCAL_LLM_LOAD_LOCK:
        if _LOCAL_LLM is None:
            from llama_cpp import Llama

            threads = max(4, min(12, (os.cpu_count() or 8) - 2))
            _LOCAL_LLM = Llama(
                model_path=str(local_model_path().resolve()),
                n_ctx=int(os.getenv("MIA_LOCAL_CONTEXT", "8192")),
                n_batch=512,
                n_threads=threads,
                verbose=False,
            )
    return _LOCAL_LLM


def _stream_local(
    messages: Sequence[Mapping[str, str]],
    config: MIAConfig,
    on_chunk: Callable[[str], None],
    cancel_event: threading.Event,
) -> str:
    chunks: list[str] = []
    try:
        model = _get_local_llm()
        with _LOCAL_LLM_GENERATION_LOCK:
            response = model.create_chat_completion(
                messages=[dict(message) for message in messages],
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                stream=True,
            )
            for event in response:
                if cancel_event.is_set():
                    break
                try:
                    chunk = event["choices"][0]["delta"].get("content", "")
                except (KeyError, IndexError, TypeError, AttributeError):
                    continue
                if isinstance(chunk, str) and chunk:
                    chunks.append(chunk)
                    on_chunk(chunk)
    except Exception as exc:
        raise MIAError(f"Ошибка локальной модели: {exc}") from exc
    answer = "".join(chunks).strip()
    if not answer and not cancel_event.is_set():
        raise MIAError("Локальная модель вернула пустой ответ")
    return answer


def _stream_ollama(
    messages: Sequence[Mapping[str, str]],
    config: MIAConfig,
    on_chunk: Callable[[str], None],
    cancel_event: threading.Event,
    timeout: int,
) -> str:
    api_url, _api_key, model = _provider_target(config, "ollama")
    chunks: list[str] = []
    try:
        with requests.post(
            api_url,
            json={
                "model": model,
                "messages": list(messages),
                "stream": True,
                "think": False,
                "options": {
                    "temperature": config.temperature,
                    "num_predict": config.max_tokens,
                },
            },
            timeout=(3, timeout),
            stream=True,
        ) as response:
            if not response.ok:
                raise MIAError(_provider_error(response, "ollama"))
            for raw_line in response.iter_lines():
                if cancel_event.is_set():
                    break
                if not raw_line:
                    continue
                try:
                    payload = json.loads(raw_line)
                    chunk = payload.get("message", {}).get("content", "")
                except (json.JSONDecodeError, AttributeError, TypeError):
                    continue
                if isinstance(chunk, str) and chunk:
                    chunks.append(chunk)
                    on_chunk(chunk)
    except requests.Timeout as exc:
        raise MIAError("Локальная Ollama не ответила вовремя") from exc
    except requests.RequestException as exc:
        raise MIAError("Локальная Ollama не запущена или недоступна") from exc
    answer = "".join(chunks).strip()
    if not answer and not cancel_event.is_set():
        raise MIAError("Локальная модель Ollama вернула пустой ответ")
    return answer


def stream_ai(
    messages: Sequence[Mapping[str, str]],
    config: MIAConfig,
    on_chunk: Callable[[str], None],
    on_provider: Callable[[str], None] | None = None,
    cancel_event: threading.Event | None = None,
    timeout: int = 120,
) -> str:
    """Stream from the first working provider, falling back before output starts."""
    cancel_event = cancel_event or threading.Event()
    errors: list[str] = []
    for provider in _provider_order(config):
        if cancel_event.is_set():
            return ""
        if provider == "deepseek" and not _direct_key(config):
            continue
        if provider == "openrouter" and not config.api_key.startswith("sk-or-v1-"):
            continue
        if provider == "local" and not local_model_ready():
            if config.provider == "local":
                errors.append("локальная модель не установлена")
            continue
        if on_provider is not None:
            on_provider(provider)
        emitted = False

        def emit(chunk: str) -> None:
            nonlocal emitted
            emitted = True
            on_chunk(chunk)

        try:
            if provider == "local":
                return _stream_local(messages, config, emit, cancel_event)
            if provider == "ollama":
                return _stream_ollama(
                    messages, config, emit, cancel_event, timeout
                )
            return _stream_openai_compatible(
                messages, config, provider, emit, cancel_event, timeout
            )
        except MIAError as exc:
            if emitted:
                raise
            errors.append(str(exc))
    if not errors:
        errors.append("не настроено ни одного ИИ-провайдера")
    raise MIAError("Не удалось получить ответ: " + " → ".join(errors))


def complete_ai(
    messages: Sequence[Mapping[str, str]],
    config: MIAConfig,
    timeout: int = 90,
) -> str:
    chunks: list[str] = []
    return stream_ai(messages, config, chunks.append, timeout=timeout)


class ConversationStore:
    """Small local JSON store. The memory directory is excluded from Git."""

    def __init__(self, path: str | Path, max_messages: int = 100):
        self.path = Path(path)
        self.max_messages = max(2, max_messages)

    def load(self) -> list[dict[str, str]]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []
        if not isinstance(raw, list):
            return []
        messages: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            content = item.get("content")
            if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content.strip()})
        return messages[-self.max_messages :]

    def save(self, messages: Sequence[Mapping[str, str]]) -> None:
        clean = [
            {"role": str(item["role"]), "content": str(item["content"]).strip()}
            for item in messages
            if item.get("role") in {"user", "assistant"}
            and str(item.get("content", "")).strip()
        ][-self.max_messages :]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temp_path, self.path)

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def conversation_to_markdown(messages: Sequence[Mapping[str, str]]) -> str:
    parts = ["# Диалог с MIA", ""]
    for item in messages:
        role = "Вы" if item.get("role") == "user" else "MIA"
        content = str(item.get("content", "")).strip()
        if content:
            parts.extend((f"## {role}", "", content, ""))
    return "\n".join(parts).rstrip() + "\n"


def extract_wake_command(
    text: str,
    wake_words: Sequence[str] = ("мия", "миа", "мие"),
) -> tuple[bool, str]:
    """Return whether a wake word was heard and the command after it."""
    normalized = re.sub(r"[^\w\s-]", " ", text.casefold(), flags=re.UNICODE)
    words = normalized.split()
    wake_set = {word.casefold() for word in wake_words}
    for index, word in enumerate(words):
        if word in wake_set:
            return True, " ".join(words[index + 1 :]).strip()
    return False, ""


def resolve_voice_command(text: str, constrained_wake: bool = False) -> tuple[bool, str]:
    """Combine free recognition with the dedicated wake-word recognizer."""
    heard_wake, command = extract_wake_command(text)
    if constrained_wake and not heard_wake:
        words = text.split()
        return True, " ".join(words[1:]).strip() if len(words) > 1 else ""
    return heard_wake, command


def is_safe_transcript_correction(original: str, candidate: str) -> bool:
    """Reject a language-model correction if it likely changed the command."""
    original = original.strip()
    candidate = candidate.strip()
    if not original or not candidate or "\n" in candidate:
        return False
    if "?" in original and "?" not in candidate:
        return False
    def normalize(value: str) -> str:
        return re.sub(r"[^\w]+", " ", value.casefold(), flags=re.UNICODE).strip()

    source = normalize(original)
    corrected = normalize(candidate)
    if not source or not corrected:
        return False
    length_ratio = len(corrected) / len(source)
    similarity = SequenceMatcher(None, source, corrected).ratio()
    return 0.55 <= length_ratio <= 1.8 and similarity >= 0.45
