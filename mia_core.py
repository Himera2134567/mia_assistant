"""Core services for MIA that do not depend on the Qt user interface."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import requests

OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
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

    @classmethod
    def from_env(cls) -> MIAConfig:
        return cls(
            api_key=os.getenv("OPENROUTER_API_KEY", "").strip(),
            model=os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat").strip()
            or "deepseek/deepseek-chat",
            system_prompt=os.getenv("MIA_SYSTEM_PROMPT", "").strip()
            or DEFAULT_SYSTEM_PROMPT,
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
