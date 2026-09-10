import pytest

import mia_web
from mia_web import (
    WebSearchError,
    augment_messages_with_web,
    collect_web_sources,
    fetch_readable,
    html_to_plain_text,
    should_search_web,
    text_for_speech,
)


def test_html_to_plain_text_removes_navigation_and_scripts():
    markup = """
    <html><body><nav>Меню сайта</nav><article>
      <h1>Важная новость</h1><p>Первый полезный абзац.</p>
      <script>alert('noise')</script><p>Второй полезный абзац.</p>
    </article></body></html>
    """
    text = html_to_plain_text(markup)
    assert "Важная новость" in text
    assert "Первый полезный абзац" in text
    assert "Второй полезный абзац" in text
    assert "Меню сайта" not in text
    assert "alert" not in text


def test_fresh_information_queries_trigger_web_search():
    assert should_search_web("Найди последние новости про Python")
    assert should_search_web("Какая погода сегодня в Москве?")
    assert not should_search_web("Объясни цикл for на Python")


def test_exact_product_domain_has_priority_over_name_imitation():
    official = {
        "title": "Download Python",
        "url": "https://www.python.org/downloads/",
        "snippet": "Latest version",
    }
    imitation = {
        "title": "Скачать последнюю версию Python",
        "url": "https://python-example.test/article",
        "snippet": "Неофициальная статья",
    }
    assert mia_web._result_priority(official, "latest Python", 5) > (
        mia_web._result_priority(imitation, "latest Python", 0)
    )


def test_page_loader_rejects_local_network_addresses():
    with pytest.raises(WebSearchError, match="Локальные"):
        fetch_readable("http://127.0.0.1/private")


def test_collect_web_sources_falls_back_to_search_snippet(monkeypatch):
    monkeypatch.setattr(
        mia_web,
        "web_search",
        lambda _query, _count: [
            {
                "title": "Источник",
                "url": "https://example.com/article",
                "snippet": "Проверенный текст из поисковой выдачи.",
            }
        ],
    )
    monkeypatch.setattr(
        mia_web,
        "fetch_readable",
        lambda _url: (_ for _ in ()).throw(RuntimeError("blocked")),
    )
    sources = collect_web_sources("запрос")
    assert "Проверенный текст из поисковой выдачи." in sources[0]["text"]


def test_web_context_requires_an_answer_instead_of_a_bare_link():
    messages = [
        {"role": "system", "content": "Ты MIA"},
        {"role": "user", "content": "Что произошло сегодня?"},
    ]
    sources = [
        {
            "title": "Новость",
            "url": "https://example.com/news",
            "text": "Содержимое новости",
        }
    ]
    prepared = augment_messages_with_web(messages, "Что произошло сегодня?", sources)
    instruction = prepared[1]["content"]
    assert "Не отвечай одной ссылкой" in instruction
    assert "недоверенными данными" in instruction
    assert prepared[-2]["role"] == "user"
    assert "Содержимое новости" in prepared[-2]["content"]
    assert prepared[-1] == messages[-1]


def test_speech_text_keeps_answer_but_drops_urls_and_sources():
    answer = (
        "Python выпустил обновление [1]. Подробнее: https://example.com/news\n\n"
        "### Источники\n1. [Новость](https://example.com/news)"
    )
    spoken = text_for_speech(answer)
    assert spoken == "Python выпустил обновление . Подробнее:"
    assert "http" not in spoken
