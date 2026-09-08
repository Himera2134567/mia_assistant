# mia_dev_agent.py
# MIA — Мини-GUI ассистента: Чат / Поиск / Git / Код / ТЗ / Методологии / Правила + смена IP.
# Зависимости по максимуму: PySide6, requests, python-dotenv, ddgs, httpx, lxml, readability-lxml, GitPython
# Но при отсутствии части библиотек приложение не падает — соответствующий функционал просто отключается.

import os
import sys
import re
import urllib.parse
from typing import Optional, List, Dict, Tuple

from dotenv import load_dotenv
load_dotenv()

import requests

# --- опциональные зависимости (не должны валить приложение) ---
try:
    import httpx
except ImportError:
    httpx = None

try:
    from ddgs import DDGS
except ImportError:
    DDGS = None

try:
    from readability import Document
except ImportError:
    Document = None

try:
    from git import Repo, GitCommandError, InvalidGitRepositoryError, NoSuchPathError
except ImportError:
    Repo = None
    GitCommandError = InvalidGitRepositoryError = NoSuchPathError = Exception

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtGui import QIcon, QTextOption
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QPlainTextEdit, QTabWidget, QFileDialog, QGridLayout, QTableWidget,
    QTableWidgetItem, QGroupBox, QCheckBox, QSplitter, QMessageBox, QAbstractItemView,
    QListWidget, QListWidgetItem
)

APP_TITLE = "MIA — Мини-GUI"
APP_ICON_PATH = os.path.join("ui", "avatar_idle.png")

DEFAULT_OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL", "deepseek/deepseek-chat")
DEFAULT_OPENROUTER_KEY   = os.getenv("OPENROUTER_API_KEY", "").strip()

DEFAULT_GH_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
DEFAULT_GH_OWNER = os.getenv("GITHUB_OWNER", "").strip()

# ================== УТИЛИТЫ ==================

def ensure_dir(p: str):
    os.makedirs(p, exist_ok=True)

def write_text(path: str, text: str):
    ensure_dir(os.path.dirname(path) or ".")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

def try_get(url: str, timeout: float = 10.0) -> Tuple[int, str]:
    try:
        r = requests.get(url, timeout=timeout)
        return r.status_code, r.text
    except Exception as e:
        return 0, str(e)

def read_text(path: str, default: str = "") -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return default

def slug_repo_name(name: str) -> str:
    table = str.maketrans({
        'А':'A','Б':'B','В':'V','Г':'G','Д':'D','Е':'E','Ё':'E','Ж':'Zh','З':'Z','И':'I','Й':'Y',
        'К':'K','Л':'L','М':'M','Н':'N','О':'O','П':'P','Р':'R','С':'S','Т':'T','У':'U','Ф':'F',
        'Х':'H','Ц':'C','Ч':'Ch','Ш':'Sh','Щ':'Sch','Ъ':'','Ы':'Y','Ь':'','Э':'E','Ю':'Yu','Я':'Ya',
        'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'e','ж':'zh','з':'z','и':'i','й':'y',
        'к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r','с':'s','т':'t','у':'u','ф':'f',
        'х':'h','ц':'c','ч':'ch','ш':'sh','щ':'sch','ъ':'','ы':'y','ь':'','э':'e','ю':'yu','я':'ya',
        ' ':'-'
    })
    import re as _re
    t = name.translate(table)
    t = _re.sub(r"[^a-zA-Z0-9._-]", "-", t)
    t = _re.sub(r"-{2,}", "-", t).strip("-")
    return t or "repo"

# ================== IP / proxy ==================

class IpManager:
    def __init__(self):
        self.proxies: List[str] = []
        self.idx = -1
        path = "proxies.txt"
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                self.proxies = [x.strip() for x in f if x.strip()]

    @staticmethod
    def apply_env(url: Optional[str]):
        keys = ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"]
        if url:
            for k in keys: os.environ[k] = url
        else:
            for k in keys: os.environ.pop(k, None)

    def current_ip(self) -> str:
        code, txt = try_get("https://api.ipify.org?format=text", timeout=10.0)
        return txt.strip() if code == 200 else f"IP check error: {txt}"

    def cycle(self) -> str:
        if not self.proxies:
            self.apply_env(None)
            return "Прокси не заданы. Работаем без прокси."
        self.idx = (self.idx + 1) % len(self.proxies)
        url = self.proxies[self.idx]
        self.apply_env(url)
        return f"Прокси применён: {url}"

# ================== ОБЩИЙ WORKER ==================

class FuncWorker(QThread):
    result = Signal(object)
    error  = Signal(str)

    def __init__(self, func, *args, **kwargs):
        super().__init__()
        self.func = func
        self.args = args
        self.kw   = kwargs

    def run(self):
        try:
            res = self.func(*self.args, **self.kw)
            self.result.emit(res)
        except Exception as e:
            self.error.emit(str(e))

# ================== LLM ==================

def llm_complete(
    prompt: str,
    model: Optional[str] = None,
    key: Optional[str] = None,
    system_prompt: Optional[str] = None,
    timeout: int = 60
) -> str:
    model = model or DEFAULT_OPENROUTER_MODEL
    key = (key or DEFAULT_OPENROUTER_KEY).strip()
    if not key:
        return "Нет OPENROUTER_API_KEY — оффлайн-режим.\n\n" + prompt[:600]

    if not system_prompt:
        system_prompt = (
            "Ты помогаешь разработчику кратко и по делу. "
            "Ответ обычным текстом без Markdown-разметки."
        )

    headers = {
        "Authorization": f"Bearer {key}",
        "HTTP-Referer": "https://local.mia",
        "X-Title": "MIA-dev-agent"
    }
    data = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.4,
        "max_tokens": 800
    }
    r = requests.post("https://openrouter.ai/api/v1/chat/completions",
                      json=data, headers=headers, timeout=timeout)
    r.raise_for_status()
    js = r.json()
    return js["choices"][0]["message"]["content"]

# ================== ПОИСК / READABILITY ==================

def web_search(query: str, n: int = 10) -> List[Dict[str, str]]:
    if DDGS is None:
        raise RuntimeError("Библиотека ddgs не установлена (pip install ddgs)")
    results: List[Dict[str, str]] = []
    with DDGS() as ddgs:
        for r in ddgs.text(query, safesearch="Moderate", max_results=n):
            results.append({
                "title": r.get("title", ""),
                "href": r.get("href", ""),
                "body": r.get("body", "")
            })
    return results

def fetch_readable(url: str, timeout: float = 15.0) -> Tuple[str, str]:
    if httpx is None or Document is None:
        return "", "Для извлечения текста нужны httpx и readability-lxml."
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url)
        resp.raise_for_status()
        doc = Document(resp.text)
        return doc.short_title(), doc.summary()

# ================== СКАНЕР СЕКРЕТОВ ==================

SECRET_PATTERNS = [
    (r"gh[pous]_[A-Za-z0-9_]{36,}", "GitHub PAT"),
    (r"sk-[A-Za-z0-9\-]{20,}", "OpenAI / OpenRouter key"),
    (r"AKIA[0-9A-Z]{16}", "AWS Access Key"),
    (r"AIza[0-9A-Za-z\-_]{35}", "Google API Key"),
    (r"\d{5,}:[A-Za-z0-9_\-]{30,}", "Telegram Bot Token"),
    (r"xox[baprs]-[0-9A-Za-z\-]{10,}", "Slack Token")
]

IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__",
    ".idea", ".vscode", "node_modules", "dist", "build"
}

def scan_secrets(root: str, files: Optional[List[str]] = None) -> List[Tuple[str, str, int, str]]:
    found: List[Tuple[str, str, int, str]] = []
    paths: List[str] = []

    if files is None:
        if Repo is not None:
            try:
                r = Repo(root)
                paths = [os.path.join(root, p) for p in r.git.ls_files().splitlines() if p.strip()]
            except Exception:
                paths = []
        if not paths:
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
                for fn in filenames:
                    paths.append(os.path.join(dirpath, fn))
    else:
        paths = [os.path.join(root, p) if not os.path.isabs(p) else p for p in files]

    for p in paths:
        try:
            if os.path.getsize(p) > 2 * 1024 * 1024:
                continue
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                for i, line in enumerate(f, start=1):
                    for pat, name in SECRET_PATTERNS:
                        if re.search(pat, line):
                            found.append((p, name, i, line.strip()))
        except Exception:
            continue
    return found

# ================== ВКЛАДКА ЧАТ ==================

class ChatTab(QWidget):
    def __init__(self, model: str):
        super().__init__()
        self.model = model
        self._workers: List[QThread] = []

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        self.modelEdit = QLineEdit(self.model)
        self.modelEdit.setPlaceholderText("model (OpenRouter)")
        self.askBtn = QPushButton("Спросить")
        self.askBtn.clicked.connect(self.on_ask)
        top.addWidget(self.modelEdit, 1)
        top.addWidget(self.askBtn, 0)

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText("Ваш вопрос…")
        self.input.setMinimumHeight(80)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMaximumBlockCount(20000)
        self.out.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)

        layout.addLayout(top)
        layout.addWidget(self.input)
        layout.addWidget(self.out)

    def _start_worker(self, w: QThread):
        self._workers.append(w)
        def _cleanup():
            try: self._workers.remove(w)
            except ValueError: pass
            w.deleteLater()
        w.finished.connect(_cleanup)
        w.start()

    def _toggle_ui(self, busy: bool):
        self.askBtn.setEnabled(not busy)

    def on_ask(self):
        prompt = self.input.toPlainText().strip()
        if not prompt:
            return
        model = self.modelEdit.text().strip() or DEFAULT_OPENROUTER_MODEL
        self._toggle_ui(True)
        worker = FuncWorker(llm_complete, prompt, model, None, None, 60)
        worker.result.connect(lambda ans: self.out.appendPlainText(f"> {prompt}\n\n{ans}\n" + "-"*80))
        worker.result.connect(lambda _: self._toggle_ui(False))
        worker.error.connect(lambda e: (self.out.appendPlainText(f"Ошибка LLM: {e}"), self._toggle_ui(False)))
        self._start_worker(worker)

# ================== ВКЛАДКА ПОИСК ==================

class SearchTab(QWidget):
    def __init__(self, api_key: str, model: str):
        super().__init__()
        self.api_key = api_key
        self.model = model
        self._workers: List[QThread] = []

        v = QVBoxLayout(self)

        h = QHBoxLayout()
        self.q = QLineEdit()
        self.q.setPlaceholderText("Запрос для DuckDuckGo…")
        self.go = QPushButton("Искать")
        self.go.clicked.connect(self.do_search)
        h.addWidget(self.q, 1)
        h.addWidget(self.go, 0)

        self.grid = QTableWidget(0, 3)
        self.grid.setHorizontalHeaderLabels(["Заголовок", "Ссылка", "Сниппет"])
        self.grid.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.grid.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.grid.cellDoubleClicked.connect(self.on_open)

        self.reader = QPlainTextEdit()
        self.reader.setReadOnly(True)
        self.reader.setMaximumBlockCount(40000)

        split = QSplitter(Qt.Orientation.Vertical)
        wrap1 = QWidget(); l1 = QVBoxLayout(wrap1); l1.addWidget(self.grid)
        wrap2 = QWidget(); l2 = QVBoxLayout(wrap2); l2.addWidget(self.reader)
        split.addWidget(wrap1); split.addWidget(wrap2); split.setSizes([300, 400])

        v.addLayout(h)
        v.addWidget(split)

        tip = QLabel("Двойной клик по строке — извлечь основной текст страницы.")
        tip.setStyleSheet("color:#9aa;")
        v.addWidget(tip)

        self.results: List[Dict[str, str]] = []

        if DDGS is None:
            self.reader.setPlainText("Поиск отключён: не установлена библиотека ddgs (pip install ddgs).")

    def _start_worker(self, w: QThread):
        self._workers.append(w)
        def _cleanup():
            try: self._workers.remove(w)
            except ValueError: pass
            w.deleteLater()
        w.finished.connect(_cleanup)
        w.start()

    def _toggle_search(self, busy: bool):
        self.go.setEnabled(not busy)

    def do_search(self):
        if DDGS is None:
            self.reader.setPlainText("Поиск невозможен: нет библиотеки ddgs.")
            return
        q = self.q.text().strip()
        if not q:
            return
        self._toggle_search(True)
        worker = FuncWorker(web_search, q, 12)
        def on_res(res):
            self.results = res or []
            self.grid.setRowCount(0)
            for r in self.results:
                row = self.grid.rowCount()
                self.grid.insertRow(row)
                self.grid.setItem(row, 0, QTableWidgetItem(r.get("title","")))
                self.grid.setItem(row, 1, QTableWidgetItem(r.get("href","")))
                self.grid.setItem(row, 2, QTableWidgetItem(r.get("body","")))
            self._toggle_search(False)
        worker.result.connect(on_res)
        worker.error.connect(lambda e: (self.reader.setPlainText(f"Ошибка поиска: {e}"), self._toggle_search(False)))
        self._start_worker(worker)

    def on_open(self, row: int, col: int):
        if row < 0 or row >= len(self.results):
            return
        url = self.results[row].get("href","")
        if not url:
            return
        self.reader.setPlainText("Загружаю читабельный текст страницы…")
        worker = FuncWorker(fetch_readable, url, 20.0)
        def on_res(pair):
            title, html = pair
            self.reader.setPlainText(f"{title or url}\n\n{html}")
        worker.result.connect(on_res)
        worker.error.connect(lambda e: self.reader.setPlainText(f"Не удалось извлечь содержимое: {e}"))
        self._start_worker(worker)

# ================== ВКЛАДКА GIT ==================

GIT_HELP = """
Git / GitHub (упрощённая вкладка)

Что делает кнопка:
1) Берёт путь к проекту и имя репозитория.
2) Проверяет, нет ли в файлах токенов и других секретов.
3) Делает git init (если ещё не сделан), добавляет .gitignore (Python), делает первый коммит.
4) Через GitHub API создаёт репозиторий у владельца из переменной окружения GITHUB_OWNER
   с токеном из GITHUB_TOKEN (нужен Personal access token (classic) со scope: repo).
5) Добавляет remote origin и пушит всё в ветку main.

Настройки:
- Имя репозитория — любое латиницей/цифрами/дефисами (русское имя будет автоматом транслитерировано).
- Приватный репозиторий — если галочка включена, репозиторий создаётся как private, иначе как public.

Ограничения и ошибки:
- Если GITHUB_TOKEN или GITHUB_OWNER не заданы в .env, вкладка не сможет создать репозиторий.
- Если GitPython не установлен (pip install GitPython), вкладка работать не будет.
- Если найдены секреты (ключи, токены) — пуш блокируется, чтобы не утекли данные.
"""

PY_GITIGNORE = """# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.env
.venv/
venv/
dist/
build/
*.egg-info/
.ipynb_checkpoints/
# IDE
.idea/
.vscode/
# OS
.DS_Store
Thumbs.db
# Logs
*.log
"""

class GitTab(QWidget):
    """
    Упрощённая вкладка Git:
    — выбираем папку проекта;
    — задаём имя репозитория и приватность;
    — жмём одну кнопку «Создать репозиторий и пушнуть».
    Всё остальное делает агент.
    """
    def __init__(self):
        super().__init__()

        self.repo_path = QLineEdit()
        self.repo_path.setPlaceholderText("Путь к проекту…")
        btn_pick = QPushButton("Выбрать папку / файл")
        btn_pick.clicked.connect(self.pick_dir)

        self.repo_name = QLineEdit()
        self.repo_name.setPlaceholderText("Имя репозитория (латиницей, можно оставить пустым — возьмётся из имени папки)")
        self.is_private = QCheckBox("Приватный репозиторий (private)")
        self.is_private.setChecked(True)

        # Кнопка «сделать всё»
        self.btn_run = QPushButton("Создать репозиторий на GitHub и пушнуть")
        self.btn_run.clicked.connect(self.on_run)

        top = QGridLayout()
        top.addWidget(QLabel("Путь:"), 0, 0)
        top.addWidget(self.repo_path, 0, 1)
        top.addWidget(btn_pick, 0, 2)

        top.addWidget(QLabel("Имя репозитория:"), 1, 0)
        top.addWidget(self.repo_name, 1, 1, 1, 2)

        top.addWidget(self.is_private, 2, 1, 1, 2)

        help_box = QGroupBox("Инструкция")
        help_txt = QPlainTextEdit()
        help_txt.setReadOnly(True)
        help_txt.setPlainText(GIT_HELP.strip())
        v_help = QVBoxLayout(help_box)
        v_help.addWidget(help_txt)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(220)
        self.log.setMaximumBlockCount(40000)

        v = QVBoxLayout(self)
        v.addLayout(top)
        v.addWidget(self.btn_run)
        v.addWidget(help_box)
        v.addWidget(self.log)

        if Repo is None:
            self._log("Git-функции недоступны: не установлена библиотека GitPython (pip install GitPython).")

    # --------- утилиты ---------

    def _log(self, s: str):
        self.log.appendPlainText(s)

    def pick_dir(self):
        start_dir = self.repo_path.text().strip() or os.path.expanduser("~")
        dlg = QFileDialog(self, "Выбрать папку или файл проекта", start_dir)
        dlg.setFileMode(QFileDialog.AnyFile)
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setOption(QFileDialog.ShowDirsOnly, False)
        dlg.setNameFilter("Все файлы (*.*)")
        if dlg.exec():
            p = dlg.selectedFiles()[0]
            proj_dir = p if not os.path.isfile(p) else os.path.dirname(p)
            self.repo_path.setText(proj_dir)
            if not self.repo_name.text().strip():
                # автогенерим имя репозитория по имени папки
                self.repo_name.setText(slug_repo_name(os.path.basename(proj_dir)))
            py_count = 0
            for root, dirs, files in os.walk(proj_dir):
                dirs[:] = [dd for dd in dirs if dd not in IGNORE_DIRS]
                py_count += sum(1 for f in files if f.endswith(".py"))
            self._log(f"Выбрана папка: {proj_dir} (файлов .py: {py_count})")

    def _ensure_repo(self, path: str) -> Repo:
        """
        Инициализирует git-репозиторий, создаёт .gitignore и делает первый коммит,
        если ещё не сделано.
        """
        if Repo is None:
            raise RuntimeError("GitPython не установлен (pip install GitPython).")

        if not os.path.exists(path):
            raise RuntimeError("Папка проекта не найдена.")

        if not os.path.exists(os.path.join(path, ".git")):
            self._log("Создаю новый git-репозиторий…")
            r = Repo.init(path)
        else:
            r = Repo(path)

        # .gitignore
        gi = os.path.join(path, ".gitignore")
        if not os.path.exists(gi):
            write_text(gi, PY_GITIGNORE)
            self._log("Создан .gitignore (Python).")

        # первый коммит
        r.git.add(A=True)
        if r.is_dirty(untracked_files=True):
            r.index.commit("chore: initial commit from MIA")
            self._log("Сделан первый коммит.")
        else:
            self._log("Изменений нет, репозиторий уже проинициализирован.")

        # убедимся, что ветка main существует
        try:
            r.git.branch("-M", "main")
        except GitCommandError:
            # ветка уже есть — ok
            pass

        return r

    def _scan_for_secrets(self, path: str) -> bool:
        findings = scan_secrets(path)
        if not findings:
            self._log("Сканирование секретов: ничего не найдено.")
            return False
        self._log("НАЙДЕНЫ СЕКРЕТЫ — пуш отменён, чтобы не утекли ключи:")
        for p, name, line_no, line in findings[:200]:
            self._log(f"- {name}: {p}:{line_no} -> {line}")
        return True

    def _create_github_repo(self, owner: str, token: str, name: str, is_private: bool) -> str:
        """
        Создаёт репозиторий на GitHub. Возвращает remote URL (с вшитым токеном) либо кидает исключение.
        """
        # проверяем токен
        self._log("Проверяю токен GitHub…")
        u = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            timeout=20
        )
        if u.status_code != 200:
            raise RuntimeError(f"Ошибка профиля GitHub: {u.status_code} {u.text}")

        repo = slug_repo_name(name or "repo")

        js = {
            "name": repo,
            "private": bool(is_private),
            "auto_init": False,
            "description": "Created by MIA",
        }
        self._log(f"Создаю репозиторий {owner}/{repo} ({'private' if is_private else 'public'})…")
        r = requests.post(
            "https://api.github.com/user/repos",
            headers={"Authorization": f"token {token}", "Accept": "application/vnd.github+json"},
            json=js,
            timeout=30
        )

        if r.status_code in (200, 201):
            self._log("Репозиторий на GitHub создан.")
        elif r.status_code == 422 and "already exists" in r.text:
            self._log("Репозиторий уже существует на GitHub, используем его.")
        elif r.status_code == 404:
            raise RuntimeError(
                "GitHub вернул 404 при создании репозитория. Скорее всего, токен — fine-grained без scope repo "
                "или нет прав на создание репозиториев."
            )
        else:
            raise RuntimeError(f"Ошибка GitHub API: {r.status_code} {r.text}")

        # remote URL с токеном (для автоматического пуша)
        owner_enc = urllib.parse.quote(owner, safe="")
        repo_enc = urllib.parse.quote(repo, safe="")
        tok_enc = urllib.parse.quote(token, safe="")
        remote_url = f"https://x-access-token:{tok_enc}@github.com/{owner_enc}/{repo_enc}.git"
        return remote_url

    def _set_origin(self, repo: Repo, url: str):
        existing = [rem.name for rem in repo.remotes]
        if "origin" in existing:
            repo.delete_remote("origin")
        repo.create_remote("origin", url)
        self._log(f"Remote origin установлен: {url}")

    # --------- основной сценарий: одна кнопка ---------

    def on_run(self):
        """
        Основной сценарий: по одной кнопке:
        — проверка настроек;
        — поиск секретов;
        — git init + первый коммит;
        — создание репозитория на GitHub;
        — установка origin и push.
        """
        path = self.repo_path.text().strip()
        if not path:
            self._log("Укажи путь к проекту.")
            return

        repo_name = self.repo_name.text().strip()
        if not repo_name and os.path.isdir(path):
            repo_name = os.path.basename(os.path.abspath(path))
        repo_name = slug_repo_name(repo_name or "repo")

        owner = DEFAULT_GH_OWNER
        token = DEFAULT_GH_TOKEN

        if not owner or not token:
            self._log("Нужны GITHUB_OWNER и GITHUB_TOKEN в .env (Classic PAT со scope: repo).")
            return

        # блокируем двойной запуск
        self.btn_run.setEnabled(False)
        self._log("=== Запуск сценария GitHub-агента ===")

        try:
            # 1) ищем секреты
            self._log("Шаг 1: сканирование проекта на секреты…")
            if self._scan_for_secrets(path):
                self._log("Секреты найдены. Исправь их и запусти снова.")
                return

            # 2) git init + первый коммит
            self._log("Шаг 2: подготовка локального git-репозитория…")
            repo = self._ensure_repo(path)

            # 3) создать репозиторий на GitHub
            self._log("Шаг 3: создание репозитория на GitHub…")
            remote_url = self._create_github_repo(owner, token, repo_name, self.is_private.isChecked())

            # 4) привязать origin
            self._log("Шаг 4: настройка origin…")
            self._set_origin(repo, remote_url)

            # 5) пуш в main
            self._log("Шаг 5: пуш в ветку main…")
            try:
                repo.git.push("-u", "origin", "main")
                self._log("Пуш завершён успешно.")
            except GitCommandError as e:
                raise RuntimeError(f"Ошибка git push: {e}")

            self._log("=== Всё готово. Репозиторий создан и запушен. ===")
        except Exception as e:
            self._log(f"ОШИБКА: {e}")
        finally:
            self.btn_run.setEnabled(True)


# ================== ВКЛАДКА КОД ==================

ALLOWED_CODE_EXT = {".py", ".ipynb", ".md", ".txt", ".json", ".yml", ".yaml", ".toml"}

class CodeTab(QWidget):
    def __init__(self, model: str):
        super().__init__()
        self.model = model
        self.project_root: Optional[str] = None
        self.current_file: Optional[str] = None
        self._workers: List[QThread] = []

        main = QVBoxLayout(self)

        top = QHBoxLayout()
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("Папка проекта…")
        btn_pick = QPushButton("Папка / файл")
        btn_pick.clicked.connect(self.pick_dir)
        self.model_edit = QLineEdit(self.model)
        self.model_edit.setPlaceholderText("model (OpenRouter)")
        top.addWidget(self.path_edit, 2)
        top.addWidget(btn_pick, 0)
        top.addWidget(self.model_edit, 1)
        main.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        l_left = QVBoxLayout(left)
        self.files_list = QListWidget()
        self.files_list.itemSelectionChanged.connect(self.on_file_selected)
        l_left.addWidget(QLabel("Файлы проекта"))
        l_left.addWidget(self.files_list)

        mid = QWidget()
        l_mid = QVBoxLayout(mid)
        self.code_view = QPlainTextEdit()
        self.code_view.setReadOnly(True)
        self.code_view.setPlaceholderText("Код файла…")
        self.code_view.setMaximumBlockCount(80000)
        l_mid.addWidget(QLabel("Код"))
        l_mid.addWidget(self.code_view)

        right = QWidget()
        l_right = QVBoxLayout(right)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setPlaceholderText("Ответ ассистента…")
        self.out.setMaximumBlockCount(40000)
        buttons = QHBoxLayout()
        self.btn_explain = QPushButton("Объяснить код")
        self.btn_explain.clicked.connect(self.on_explain)
        self.btn_review = QPushButton("Найти проблемы")
        self.btn_review.clicked.connect(self.on_review)
        buttons.addWidget(self.btn_explain)
        buttons.addWidget(self.btn_review)
        l_right.addLayout(buttons)
        l_right.addWidget(self.out)

        split.addWidget(left)
        split.addWidget(mid)
        split.addWidget(right)
        split.setSizes([220, 480, 480])

        main.addWidget(split)

    def _start_worker(self, w: QThread):
        self._workers.append(w)
        def _cleanup():
            try: self._workers.remove(w)
            except ValueError: pass
            w.deleteLater()
        w.finished.connect(_cleanup)
        w.start()

    def _toggle(self, busy: bool):
        self.btn_explain.setEnabled(not busy)
        self.btn_review.setEnabled(not busy)

    def pick_dir(self):
        start_dir = self.path_edit.text().strip() or os.path.expanduser("~")
        dlg = QFileDialog(self, "Выбрать папку или файл проекта", start_dir)
        dlg.setFileMode(QFileDialog.AnyFile)
        dlg.setOption(QFileDialog.DontUseNativeDialog, True)
        dlg.setOption(QFileDialog.ShowDirsOnly, False)
        dlg.setNameFilter("Все файлы (*.*)")
        if dlg.exec():
            p = dlg.selectedFiles()[0]
            root = p if not os.path.isfile(p) else os.path.dirname(p)
            self.project_root = root
            self.path_edit.setText(root)
            self.populate_files()

    def populate_files(self):
        self.files_list.clear()
        if not self.project_root or not os.path.isdir(self.project_root):
            return
        root = self.project_root
        items: List[str] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in IGNORE_DIRS]
            for fn in filenames:
                ext = os.path.splitext(fn)[1].lower()
                if ext in ALLOWED_CODE_EXT:
                    full = os.path.join(dirpath, fn)
                    rel = os.path.relpath(full, root)
                    items.append(rel)
        items.sort()
        for rel in items:
            self.files_list.addItem(QListWidgetItem(rel))

    def on_file_selected(self):
        if not self.project_root:
            return
        item = self.files_list.currentItem()
        if not item:
            return
        rel = item.text()
        full = os.path.join(self.project_root, rel)
        self.current_file = full
        try:
            code = read_text(full, "")
            if len(code) > 200_000:
                code = code[:200_000] + "\n\n[Обрезано для отображения]"
            self.code_view.setPlainText(code)
        except Exception as e:
            self.code_view.setPlainText(f"Не удалось прочитать файл: {e}")

    def _llm_for_current(self, mode: str):
        if not self.current_file or not self.project_root:
            return "Нет выбранного файла."
        code = self.code_view.toPlainText()
        if not code.strip():
            return "Файл пуст."
        rel = os.path.relpath(self.current_file, self.project_root)
        snippet = code if len(code) <= 8000 else code[:8000] + "\n\n[Код обрезан по длине]"

        if mode == "explain":
            user_prompt = (
                f"Вот файл проекта {rel}. Объясни, что делает этот код, пошагово и по логике. "
                f"Сначала назначение модуля, затем ключевые функции и важные места.\n\n{snippet}"
            )
            system_prompt = "Ты опытный разработчик Python. Объясняешь чужой код. Без Markdown."
        else:
            user_prompt = (
                f"Проанализируй файл {rel}. Найди потенциальные проблемы и предложи улучшения. "
                f"Интересуют баги, крайние случаи, архитектурные запахи, плохие имена.\n\n{snippet}"
            )
            system_prompt = "Ты опытный ревьюер кода. Дай конкретные рекомендации. Без Markdown."
        model = self.model_edit.text().strip() or DEFAULT_OPENROUTER_MODEL
        return llm_complete(user_prompt, model=model, system_prompt=system_prompt, timeout=90)

    def on_explain(self):
        self._toggle(True)
        worker = FuncWorker(self._llm_for_current, "explain")
        worker.result.connect(lambda ans: self.out.appendPlainText(f"Объяснение:\n\n{ans}\n" + "-"*80))
        worker.result.connect(lambda _: self._toggle(False))
        worker.error.connect(lambda e: (self.out.appendPlainText(f"Ошибка: {e}"), self._toggle(False)))
        self._start_worker(worker)

    def on_review(self):
        self._toggle(True)
        worker = FuncWorker(self._llm_for_current, "review")
        worker.result.connect(lambda ans: self.out.appendPlainText(f"Разбор проблем:\n\n{ans}\n" + "-"*80))
        worker.result.connect(lambda _: self._toggle(False))
        worker.error.connect(lambda e: (self.out.appendPlainText(f"Ошибка: {e}"), self._toggle(False)))
        self._start_worker(worker)

# ================== ВКЛАДКА ТЗ / ПЛАН ==================

class SpecTab(QWidget):
    def __init__(self, model: str):
        super().__init__()
        self.model = model
        self._workers: List[QThread] = []

        v = QVBoxLayout(self)

        top = QGridLayout()
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Название проекта")
        self.model_edit = QLineEdit(self.model)
        self.model_edit.setPlaceholderText("model (OpenRouter)")
        top.addWidget(QLabel("Проект:"), 0, 0)
        top.addWidget(self.name_edit, 0, 1)
        top.addWidget(QLabel("Модель:"), 1, 0)
        top.addWidget(self.model_edit, 1, 1)

        self.desc = QPlainTextEdit()
        self.desc.setPlaceholderText("Опиши идею и контекст…")

        btns = QHBoxLayout()
        self.btn_spec = QPushButton("Сгенерировать ТЗ")
        self.btn_plan = QPushButton("Сгенерировать план")
        self.btn_spec.clicked.connect(self.on_spec)
        self.btn_plan.clicked.connect(self.on_plan)
        btns.addWidget(self.btn_spec)
        btns.addWidget(self.btn_plan)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(False)
        self.out.setMaximumBlockCount(80000)
        self.out.setPlaceholderText("Здесь появится ТЗ или план.")

        v.addLayout(top)
        v.addWidget(QLabel("Исходное описание"))
        v.addWidget(self.desc)
        v.addLayout(btns)
        v.addWidget(QLabel("Результат"))
        v.addWidget(self.out)

    def _start_worker(self, w: QThread):
        self._workers.append(w)
        def _cleanup():
            try: self._workers.remove(w)
            except ValueError: pass
            w.deleteLater()
        w.finished.connect(_cleanup)
        w.start()

    def _toggle(self, busy: bool):
        self.btn_spec.setEnabled(not busy)
        self.btn_plan.setEnabled(not busy)

    def _base_prompt(self) -> str:
        name = self.name_edit.text().strip() or "проект"
        desc = self.desc.toPlainText().strip()
        return f"Название проекта: {name}\n\nОписание / исходные мысли:\n{desc}\n\n"

    def _gen_spec(self):
        base = self._base_prompt()
        user_prompt = (
            base +
            "Сформируй полноценное текстовое техническое задание на русском языке. "
            "Разделы: цель и задачи, актуальность, пользователи и роли, "
            "функциональные требования по подсистемам, нефункциональные требования, интеграции, "
            "ограничения и предположения. Пиши цельным текстом без Markdown."
        )
        system_prompt = "Ты системный аналитик. Стиль академичный, без Markdown."
        model = self.model_edit.text().strip() or DEFAULT_OPENROUTER_MODEL
        return llm_complete(user_prompt, model=model, system_prompt=system_prompt, timeout=120)

    def _gen_plan(self):
        base = self._base_prompt()
        user_prompt = (
            base +
            "Сделай подробный план разработки. Разбей на этапы или спринты: анализ, проектирование, "
            "реализация, тестирование, отчет. Для каждого этапа укажи задачи, примерные сроки "
            "и артефакты результата. Без Markdown."
        )
        system_prompt = "Ты тимлид, фокус на реализуемости в учебных дедлайнах."
        model = self.model_edit.text().strip() or DEFAULT_OPENROUTER_MODEL
        return llm_complete(user_prompt, model=model, system_prompt=system_prompt, timeout=120)

    def on_spec(self):
        self._toggle(True)
        worker = FuncWorker(self._gen_spec)
        worker.result.connect(lambda ans: (self.out.setPlainText(ans), self._toggle(False)))
        worker.error.connect(lambda e: (self.out.setPlainText(f"Ошибка: {e}"), self._toggle(False)))
        self._start_worker(worker)

    def on_plan(self):
        self._toggle(True)
        worker = FuncWorker(self._gen_plan)
        worker.result.connect(lambda ans: (self.out.setPlainText(ans), self._toggle(False)))
        worker.error.connect(lambda e: (self.out.setPlainText(f"Ошибка: {e}"), self._toggle(False)))
        self._start_worker(worker)

# ================== ВКЛАДКА МЕТОДОЛОГИИ ==================

FAQ_TEXT = """
Методологии и быстрые ответы. Здесь собраны базовые описания подходов к разработке, которые можно использовать в отчётах
и при защите проекта. Для каждого пункта есть суть, ключевые артефакты и случаи, когда это реально помогает.
"""

class MethodologiesTab(QWidget):
    def __init__(self):
        super().__init__()
        self._workers: List[QThread] = []

        # Темы и подробная теория по каждой
        self.topics: Dict[str, str] = {
            "Scrum (спринты, роли, артефакты)": (
                "Scrum — фреймворк для командной разработки, где работа идёт короткими итерациями — спринтами.\n\n"
                "Суть Scrum:\n"
                "1) Время делится на спринты (обычно 1–2 недели). В конце каждого спринта команда должна выдать "
                "инкремент продукта, который теоретически можно выкатить пользователям.\n"
                "2) Роли:\n"
                "   Владелец продукта отвечает за видение и приоритизацию бэклога.\n"
                "   Скрам-мастер помогает команде следовать процессу и убирает блокеры.\n"
                "   Команда разработки реализует задачи, берёт на себя обязательства спринта.\n"
                "3) Артефакты: Product Backlog (общий список требований), Sprint Backlog (задачи на текущий спринт), "
                "инкремент (готовый кусок продукта).\n"
                "4) События: планирование спринта, ежедневный короткий созвон, обзор (демо) и ретроспектива.\n\n"
                "Где это применяют: команды, которые делают продукт с постоянным развитием, где важно стабильно "
                "показывать прогресс и получать обратную связь каждые 1–2 недели.\n"
                "Типичные проблемы: переоценка возможностей в спринте, отсутствие внятной цели спринта, "
                "формальные митинги без реальной пользы."
            ),
            "Kanban (поток задач и WIP-лимиты)": (
                "Kanban — метод управления потоком задач. В отличие от Scrum, здесь нет жёстких спринтов.\n\n"
                "Ключевые идеи:\n"
                "1) Визуализация работы: задачи отображаются на доске с колонками (например, To Do / In Progress / Review / Done).\n"
                "2) WIP-лимиты: ограничение количества задач в работе. Это снижает переключение контекста и помогает доводить задачи до конца.\n"
                "3) Фокус на времени прохождения задачи через процесс, а не на количестве задач за спринт.\n\n"
                "Где применяется: сопровождение, поддержка, DevOps-команды, где поток задач непрерывен и плохо делится на спринты.\n"
                "Типичные проблемы: слишком широкие колонки, отсутствие реальных WIP-лимитов, попытка совместить Kanban и хаос."
            ),
            "Code review (зачем и как делать нормально)": (
                "Code review — системная проверка изменений другим разработчиком перед вливайнием кода в основную ветку.\n\n"
                "Зачем нужно:\n"
                "1) Поиск багов и странных решений на раннем этапе.\n"
                "2) Распространение знаний о кодовой базе внутри команды.\n"
                "3) Поддержание единого стиля и архитектурных договорённостей.\n\n"
                "Хороший pull-request:\n"
                "— небольшой по объёму (лучше 50–200 строк логических изменений);\n"
                "— с понятным описанием: что сделано и почему;\n"
                "— без лишнего мусора (отладочные принты, временные файлы);\n"
                "— содержит тесты для критичных изменений.\n\n"
                "Типичные проблемы: гигантские PR, ревью «по диагонали», формальные LGTM без чтения кода, "
                "обсуждения стиля вместо логики."
            ),
            "CI/CD (проверки, сборка, деплой)": (
                "CI/CD — набор практик и инструментов, которые позволяют держать main-ветку постоянно в рабочем состоянии.\n\n"
                "CI (Continuous Integration):\n"
                "— каждый коммит проходит автоматические проверки: линтеры, тесты, сборка;\n"
                "— цель — ловить ошибки максимально близко к моменту их появления.\n\n"
                "CD (Continuous Delivery / Deployment):\n"
                "— Delivery: код всегда готов к выкатыванию, но сам деплой запускается вручную;\n"
                "— Deployment: деплой происходит автоматически после успешных проверок.\n\n"
                "Где используется: продуктовые команды, сервисы с частыми релизами, микросервисы.\n"
                "Проблемы: слишком сложные пайплайны, нестабильные тесты, зависимые друг от друга пайплайны, "
                "отсутствие быстрой диагностики, почему пайплайн упал."
            ),
            "Git-workflow (ветки, PR, релизы)": (
                "Git-workflow — договорённость команды, как использовать ветки и pull-request-ы.\n\n"
                "Базовая схема:\n"
                "1) main/master — стабильная ветка, только проверенный код.\n"
                "2) feature/xxx — ветки для фич;\n"
                "   bugfix/xxx или hotfix/xxx — ветки для исправления ошибок.\n"
                "3) Любое попадание кода в main идёт через PR и code review.\n"
                "4) Релизы помечаются тегами (v1.0.0, v1.1.0 и т.д.).\n\n"
                "Где это применяют: практически в любом серьёзном проекте на GitHub / GitLab.\n"
                "Проблемы: коммиты прямо в main, отсутствие веток под отдельные задачи, "
                "огромные PR, смешивающие фичи, фиксы и рефакторинг."
            ),
            "Работа с багами и техдолгом": (
                "Работа с багами:\n"
                "1) Сначала воспроизведение. Без стабильного репро фикса не существует.\n"
                "2) Минимальный пример: вырезать всё лишнее и оставить только то, что вызывает баг.\n"
                "3) Анализ логов, входных данных и состояния системы.\n"
                "4) Фикс + защита: добавление теста, который проваливается до фикса и проходит после.\n\n"
                "Технический долг:\n"
                "— накопившиеся проблемы архитектуры, кода и инфраструктуры, которые мешают развивать систему;\n"
                "— его нужно учитывать в планировании, а не «когда-нибудь потом».\n\n"
                "Где это важно: долгоживущие проекты, где уже несколько версий и команда менялась.\n"
                "Типичные проблемы: бесконечное откладывание рефакторинга, «затычки» вместо нормальных решений."
            ),
            "UML и архитектурные схемы": (
                "UML-диаграммы и архитектурные схемы помогают объяснить структуру системы без чтения кода.\n\n"
                "Минимальный набор для учебного/дипломного проекта:\n"
                "1) Диаграмма вариантов использования (Use Case) — кто и что делает в системе.\n"
                "2) Диаграмма компонентов/развёртывания — какие модули и сервисы есть, как они связаны.\n"
                "3) При необходимости — диаграмма последовательностей для ключевого сценария.\n\n"
                "Где используется: документация по проекту, защита диплома/курсовой, общение с не-технарями.\n"
                "Проблемы: слишком детализированные диаграммы, которые никто не обновляет; несоответствие схем реальному коду."
            ),
            "Командная работа и процессы": (
                "Командная работа — это не только код, но и процессы вокруг него.\n\n"
                "Здоровый процесс включает:\n"
                "1) Общие цели и понятные критерии готовности задач.\n"
                "2) Видимость статуса задач (доска, Jira, Trello, GitHub Projects).\n"
                "3) Регулярные синки: короткие созвоны по прогрессу и блокерам.\n"
                "4) Документацию: README, инструкции по запуску, описание API.\n\n"
                "Использование: любые проекты, где больше одного разработчика.\n"
                "Проблемы: всё держится в голове одного человека, нет прозрачности, внезапные задачи «сверху» без приоритета."
            ),
            "Общее FAQ": FAQ_TEXT.strip(),
        }

        layout = QVBoxLayout(self)

        # Верх: выбор модели
        top = QHBoxLayout()
        self.model_edit = QLineEdit(DEFAULT_OPENROUTER_MODEL)
        self.model_edit.setPlaceholderText("model (OpenRouter)")
        top.addWidget(QLabel("Модель:"))
        top.addWidget(self.model_edit, 1)
        layout.addLayout(top)

        # Центральная часть: список тем + теория
        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        l_left = QVBoxLayout(left)
        self.topic_list = QListWidget()
        for title in self.topics.keys():
            self.topic_list.addItem(QListWidgetItem(title))
        self.topic_list.currentItemChanged.connect(self.on_topic_selected)
        l_left.addWidget(QLabel("Темы"))
        l_left.addWidget(self.topic_list)

        right = QWidget()
        l_right = QVBoxLayout(right)
        self.theory_view = QPlainTextEdit()
        self.theory_view.setReadOnly(True)
        self.theory_view.setMaximumBlockCount(40000)
        self.theory_view.setPlaceholderText("Выбери тему слева, чтобы показать теорию.")
        l_right.addWidget(QLabel("Теория"))
        l_right.addWidget(self.theory_view)

        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([280, 720])

        layout.addWidget(split)

        # Низ: вопрос по выбранной теме
        query_box = QGroupBox("Вопрос по выбранной теме")
        q_layout = QVBoxLayout(query_box)

        self.question_edit = QPlainTextEdit()
        self.question_edit.setPlaceholderText("Напиши вопрос по текущей теме: как применить её к твоему проекту, "
                                              "как описать в отчёте или как оформить артефакты.")
        self.question_edit.setMaximumBlockCount(4000)

        btn_row = QHBoxLayout()
        self.ask_btn = QPushButton("Спросить по теме")
        self.ask_btn.clicked.connect(self.on_ask_topic)
        btn_row.addStretch(1)
        btn_row.addWidget(self.ask_btn)

        self.answer_view = QPlainTextEdit()
        self.answer_view.setReadOnly(True)
        self.answer_view.setMaximumBlockCount(20000)
        self.answer_view.setPlaceholderText("Ответ модели по выбранной теме появится здесь.")

        q_layout.addWidget(self.question_edit)
        q_layout.addLayout(btn_row)
        q_layout.addWidget(self.answer_view)

        layout.addWidget(query_box)

        if self.topic_list.count() > 0:
            self.topic_list.setCurrentRow(0)

    def _start_worker(self, w: QThread):
        self._workers.append(w)
        def _cleanup():
            try:
                self._workers.remove(w)
            except ValueError:
                pass
            w.deleteLater()
        w.finished.connect(_cleanup)
        w.start()

    def _toggle(self, busy: bool):
        self.ask_btn.setEnabled(not busy)

    def on_topic_selected(self, current: QListWidgetItem, previous: QListWidgetItem):
        if not current:
            return
        title = current.text()
        text = self.topics.get(title, "")
        self.theory_view.setPlainText(text)

    def _llm_for_topic(self) -> str:
        current_item = self.topic_list.currentItem()
        if not current_item:
            return "Тема не выбрана."
        title = current_item.text()
        theory = self.topics.get(title, "")
        question = self.question_edit.toPlainText().strip()
        if not question:
            return "Вопрос пуст."

        user_prompt = (
            f"Тема: {title}\n\n"
            f"Теоретическое описание:\n{theory}\n\n"
            f"Вопрос студента по этой теме:\n{question}\n\n"
            "Дай чёткий, практический ответ на русском. Можно использовать примеры из типичных учебных и "
            "проектных ситуаций. Ответ без Markdown, обычный текст."
        )
        system_prompt = (
            "Ты опытный инженер и наставник. Объясняешь методологии разработки и процессы "
            "простым, но точным языком. Без Markdown."
        )
        model = self.model_edit.text().strip() or DEFAULT_OPENROUTER_MODEL
        return llm_complete(user_prompt, model=model, system_prompt=system_prompt, timeout=120)

    def on_ask_topic(self):
        self._toggle(True)
        worker = FuncWorker(self._llm_for_topic)
        def on_res(ans: str):
            self.answer_view.appendPlainText(ans + "\n" + "-"*80)
            self._toggle(False)
        worker.result.connect(on_res)
        worker.error.connect(lambda e: (self.answer_view.appendPlainText(f"Ошибка: {e}"), self._toggle(False)))
        self._start_worker(worker)

# ================== ВКЛАДКА ПРАВИЛА ==================

RULES_DATA: List[Dict[str, str]] = [
    {
        "title": "Пиши код так, как будто его будет поддерживать злой человек",
        "body": (
            "Смысл правила: код должен быть понятным для любого разработчика, а не только для автора в день написания.\n\n"
            "Теория: читаемость и простота важнее микро-оптимизаций. Хороший код проще поддерживать, тестировать и "
            "расширять. Большая часть времени в реальных проектах тратится не на написание нового кода, а на "
            "чтение и модификацию уже существующего.\n\n"
            "Основные проблемы:\n"
            "— сложные, переплетённые конструкции, которые невозможно понять без отладки;\n"
            "— скрытые зависимости и глобальное состояние;\n"
            "— отсутствие комментариев в действительно неочевидных местах.\n\n"
            "Где используется: любой долгоживущий проект (коммерческий, учебный, open-source), где код пишут более одного человека "
            "или планируется сопровождение в будущем."
        )
    },
    {
        "title": "Сначала понятность, потом скорость",
        "body": (
            "Смысл: преждевременная оптимизация часто ломает архитектуру и делает код хрупким.\n\n"
            "Теория: сначала нужно получить корректное и понятное решение, а только потом, при наличии измеренной "
            "производительной проблемы, оптимизировать узкие места. Профилирование важнее догадок.\n\n"
            "Типичные проблемы: усложнение алгоритмов ради мнимой скорости, оптимизация того, что не является bottleneck.\n"
            "Где важно: backend-сервисы, обработка данных, любая система, где балансируются читаемость и производительность."
        )
    },
    {
        "title": "DRY без фанатизма: читаемость важнее",
        "body": (
            "Смысл: не нужно бездумно выносить всё в общие функции ради принципа DRY.\n\n"
            "Теория: дублирование кода плохо, когда оно мешает изменению логики и порождает рассинхрон. "
            "Но чрезмерное обобщение создаёт сложные абстракции, которые труднее понять, чем два почти одинаковых "
            "простых блока кода.\n\n"
            "Проблемы: «боже-функции», которые делают всё; запутанные уровни абстракции; общие helper-ы без чёткой ответственности.\n"
            "Где используется: большие проекты на Python, Java, C#, где соблазн сделать один общий «утилитный» модуль особенно велик."
        )
    },
    {
        "title": "Имена должны объяснять логику и контекст",
        "body": (
            "Смысл: по имени переменной/функции должно быть понятно, что она делает и в каком контексте используется.\n\n"
            "Теория: хорошие имена уменьшают необходимость в комментариях. Имя — часть интерфейса. "
            "Название типа process_data или do_work почти бесполезно, а validate_user_token или "
            "calculate_order_total сразу задают ожидание от функции.\n\n"
            "Проблемы: имя не отражает реальную логику, копипаста имён, сокращения, понятные только автору.\n"
            "Где критично: публичные API, библиотеки, точки интеграции между командами."
        )
    },
    {
        "title": "Одна зона ответственности на модуль или класс",
        "body": (
            "Смысл: модуль или класс должен отвечать за одну логически целостную часть системы.\n\n"
            "Теория: принцип единственной ответственности (SRP). Если модуль одновременно ходит в базу, рендерит UI "
            "и пишет логи, он становится трудно расширяемым и тестируемым.\n\n"
            "Проблемы: «боже-объекты», куча связанных функций в одном файле, циклические зависимости.\n"
            "Где важно: слоистые приложения (UI / бизнес-логика / данные), микросервисы, доменно-ориентированный дизайн."
        )
    },
    {
        "title": "Не оптимизируй без замера",
        "body": (
            "Смысл: прежде чем переписывать код ради производительности, нужно измерить, где реально узкое место.\n\n"
            "Теория: человеческая интуиция плохо оценивает, где программа тратит время. Профилировщики и метрики "
            "дают реальную картину. Иногда наибольшие потери происходят не в алгоритме, а в блокирующем I/O или "
            "лишних запросах к базе.\n\n"
            "Проблемы: ненужные сложные структуры данных, опасная микро-оптимизация, ухудшение читаемости.\n"
            "Где применяется: высоконагруженные сервисы, численные расчёты, data-pipeline-ы."
        )
    },
    {
        "title": "Логи по делу, без мусора",
        "body": (
            "Смысл: логирование должно помогать разбирать проблемы, а не заливать систему тоннами бесполезного текста.\n\n"
            "Теория: уровни логов (DEBUG, INFO, WARNING, ERROR) должны использоваться осознанно. В логах нужен контекст: "
            "идентификатор запроса, пользователя, операции.\n\n"
            "Проблемы: спам в логах, логирование в цикле, логирование секретов и персональных данных.\n"
            "Где важно: backend-сервисы, системы с распределённой архитектурой, где без логов сложно понять, что пошло не так."
        )
    },
    {
        "title": "Думай о границах и нулевых значениях",
        "body": (
            "Смысл: большинство багов живёт на крайних значениях параметров.\n\n"
            "Теория: нужно явно продумывать, что будет при пустых списках, нулевых длинах, максимальных размерах, "
            "отрицательных значениях и т.п. Это касается и входных данных, и ответов внешних сервисов.\n\n"
            "Проблемы: IndexError, деление на ноль, некорректная работа на пустых наборах, переполнение.\n"
            "Где используется: всё, что связано с пользовательским вводом, сетевыми протоколами, парсингом данных."
        )
    },
    {
        "title": "Не доверяй входным данным",
        "body": (
            "Смысл: всё, что приходит извне (пользователь, другой сервис, файл), потенциально сломано или злонамеренно.\n\n"
            "Теория: валидация, нормализация и фильтрация данных — обязательный шаг перед обработкой. "
            "Нельзя напрямую использовать пользовательский ввод в SQL-запросах, файловых путях, shell-командах.\n\n"
            "Проблемы: SQL-инъекции, XSS, path traversal, падения сервиса из-за неожиданных значений.\n"
            "Где особенно важно: web-приложения, API, любые интерфейсы с внешним миром."
        )
    },
    {
        "title": "Секреты держи вне кода и репозитория",
        "body": (
            "Смысл: токены, пароли, ключи не должны попадать в git и исходники.\n\n"
            "Теория: секреты должны храниться в .env, менеджерах секретов, переменных окружения или специальных хранилищах. "
            "Репозиторий должен содержать только шаблоны конфигов.\n\n"
            "Проблемы: утечка токенов, блокировка аккаунтов, компрометация базы данных.\n"
            "Где критично: любые проекты, использующие внешние сервисы (GitHub, облака, платёжные системы)."
        )
    },
    {
        "title": "Конфиг — в .env и настройках, а не в коде",
        "body": (
            "Смысл: параметры окружения (URL, ключи, пути, режимы) должны быть вынесены из кода.\n\n"
            "Теория: это позволяет запускать один и тот же код в dev / test / prod, меняя только конфигурацию. "
            "Код становится более переносимым и предсказуемым.\n\n"
            "Проблемы: жёстко прошитые в коде пути и адреса, необходимость править исходники при каждом развёртывании.\n"
            "Где актуально: веб-сервисы, микросервисы, приложения, которые крутятся на разных серверах."
        )
    },
    {
        "title": "Маленькие, логически цельные коммиты",
        "body": (
            "Смысл: каждый коммит должен решать одну понятную задачу.\n\n"
            "Теория: мелкие коммиты проще ревьюить, проще откатывать и проще искать, где появился баг. "
            "Сообщение коммита должно объяснять суть изменения.\n\n"
            "Проблемы: «залил всё сразу», смешивание фич, фиксов и рефакторинга в одном коммите.\n"
            "Где важно: работа в команде, особенно при использовании pull-request-ов."
        )
    },
    {
        "title": "Одна задача — одна ветка",
        "body": (
            "Смысл: ветка должна соответствовать конкретной задаче или фиче.\n\n"
            "Теория: такой подход упрощает управление релизами, ревью и откаты. "
            "Названия веток обычно привязаны к номеру задачи в трекере.\n\n"
            "Проблемы: вечные ветки со смешанными изменениями, которые невозможно нормально вмержить.\n"
            "Где используется: GitHub/GitLab-потоки разработки, Scrum и Kanban-команды."
        )
    },
    {
        "title": "Тестируй критичную бизнес-логику",
        "body": (
            "Смысл: ключевые сценарии должны быть покрыты автоматическими тестами.\n\n"
            "Теория: юнит-тесты и интеграционные тесты фиксируют ожидаемое поведение. "
            "Они позволяют безопаснее рефакторить и обновлять зависимости.\n\n"
            "Проблемы: тесты есть только на тривиальные куски, а реальные сценарии проверяются руками.\n"
            "Где особенно важно: платежи, безопасность, расчёты, авторизация."
        )
    },
    {
        "title": "Тесты должны быть быстрыми и детерминированными",
        "body": (
            "Смысл: тесты, которые иногда падают, а иногда проходят, бесполезны.\n\n"
            "Теория: результаты тестов не должны зависеть от текущего времени, сетевых задержек, случайных чисел. "
            "Для внешних сервисов используют моки и заглушки.\n\n"
            "Проблемы: флаки-тесты, красный пайплайн «иногда», отсутствие доверия к CI.\n"
            "Где важно: любые проекты с CI/CD."
        )
    },
    {
        "title": "Сначала воспроизведи баг, потом чини",
        "body": (
            "Смысл: нельзя считать баг исправленным, если нет стабильного сценария его воспроизведения.\n\n"
            "Теория: нужен минимальный репро-пример. После фикса этот сценарий должен стать тестом. "
            "Иначе баг может вернуться после следующего рефакторинга.\n\n"
            "Проблемы: хаотичные правки «на глаз», которые создают новые баги и не решают старый.\n"
            "Где применяется: backend, UI, игры, любые сложные системы с состоянием."
        )
    },
    {
        "title": "Логи и трассировки помогают воспроизводить проблемы",
        "body": (
            "Смысл: грамотное логирование делает баги воспроизводимыми даже на проде.\n\n"
            "Теория: вместе с ошибкой логируется контекст: что пришло на вход, кто пользователь, какой был запрос. "
            "Используются корреляционные id, чтобы связать события между сервисами.\n\n"
            "Проблемы: «ошибка в логах без деталей», отсутствие информации о пользовательском контексте.\n"
            "Где важно: распределённые системы, микросервисы, очереди сообщений."
        )
    },
    {
        "title": "Документируй неочевидное",
        "body": (
            "Смысл: если решение нетривиальное или продиктовано ограничениями, нужно коротко зафиксировать, почему так.\n\n"
            "Теория: комментарии и документация должны отвечать на вопрос «зачем», а не «что делает этот if». "
            "Для «что» есть сам код.\n\n"
            "Проблемы: магические числа, хак-обходы без объяснений, невозможность понять причину решения через полгода.\n"
            "Где критично: архитектурные решения, временные костыли, интеграции с чужими системами."
        )
    },
    {
        "title": "Хороший README экономит часы",
        "body": (
            "Смысл: новый человек должен поднять проект по README, не задавая десяток вопросов автору.\n\n"
            "Теория: минимальный README содержит: назначение проекта, требования, шаги запуска, примеры команд, "
            "описание конфигурации и ссылки на доп. документацию.\n\n"
            "Проблемы: README с одной строкой, устаревшие инструкции, отсылка к «спроси у Васи».\n"
            "Где важно: курсовые, дипломы, open-source, внутренние сервисы."
        )
    },
    {
        "title": "Линтеры и форматтеры по умолчанию",
        "body": (
            "Смысл: автоматические инструменты должны отвечать за стиль и базовую статику.\n\n"
            "Теория: линтеры ловят типичные ошибки, форматтеры выравнивают стиль. Это уменьшает шум на ревью и "
            "делает код визуально единообразным.\n\n"
            "Проблемы: отключённые линтеры, игнорирование предупреждений, ручное форматирование.\n"
            "Где используется: Python (ruff/flake8 + black), JS/TS (eslint + prettier) и т.п."
        )
    },
    {
        "title": "Не игнорируй предупреждения компилятора и линтера",
        "body": (
            "Смысл: большинство предупреждений — потенциальные баги или запахи.\n\n"
            "Теория: если предупреждение не несёт смысла, его лучше отключить целенаправленно, "
            "а не оставлять вечным шумом.\n\n"
            "Проблемы: консоль, заваленная предупреждениями; реальные ошибки теряются среди «шума».\n"
            "Где важно: большие проекты, которые живут годами."
        )
    },
    {
        "title": "Безопасность не делается постфактум",
        "body": (
            "Смысл: невозможно «насыпать безопасности» в готовый хаотичный продукт.\n\n"
            "Теория: нужно сразу учитывать права доступа, шифрование, хранение паролей, работу с токенами и логирование. "
            "Модели угроз должны обсуждаться ещё на этапе проектирования.\n\n"
            "Проблемы: пароли в базе в открытом виде, отсутствие разделения ролей, открытые админки.\n"
            "Где критично: web-приложения, системы с персональными данными, деньги, медицинские данные."
        )
    },
    {
        "title": "Асинхронность не лечит плохую архитектуру",
        "body": (
            "Смысл: переход на async/await не решит проблем блокирующих запросов и плохой структуры кода.\n\n"
            "Теория: асинхронность помогает масштабировать I/O-нагрузку, но усложняет профиль ошибок и отладку. "
            "Сначала нужно навести порядок в слоях, разделить ответственность и только потом думать о async.\n\n"
            "Проблемы: хаос из смешанных sync/async вызовов, блокирующий код внутри async-функций, дедлоки.\n"
            "Где встречается: Python-сервисы, Node.js-приложения, GUI с фоновой работой."
        )
    },
    {
        "title": "Планируй рефакторинг и техдолг",
        "body": (
            "Смысл: долг не исчезнет сам, его нужно учитывать в планах.\n\n"
            "Теория: задачи на улучшение кода и архитектуры должны попадать в бэклог и отдельным пунктом идти в план спринтов. "
            "Рефакторинг часто делается перед добавлением крупных фич.\n\n"
            "Проблемы: «потом как-нибудь перепишем», в итоге — паралич изменений.\n"
            "Где актуально: любой проект старше полугода."
        )
    },
    {
        "title": "Автоматизируй повторяемые ручные действия",
        "body": (
            "Смысл: если ты делаешь одно и то же руками больше двух-трёх раз — это кандидат на скрипт.\n\n"
            "Теория: автоматизация снижает количество человеческих ошибок и ускоряет цикл разработки. "
            "Всё, что связано с запуском, сборкой, миграциями, тестами, желательно упаковать в скрипты/Makefile/CI.\n\n"
            "Проблемы: сложные многошаговые инструкции «на память», ошибки при релизах, зависимости от конкретного человека.\n"
            "Где используется: сборка фронта, деплой, генерация документации, миграции БД."
        )
    },
    {
        "title": "Не усложняй CI/CD сверх необходимости",
        "body": (
            "Смысл: пайплайн должен быть надёжным и понятным, а не демонстрацией мастерства в YAML.\n\n"
            "Теория: лучше несколько простых шагов, чем монстр на сотни строк, который никто не понимает. "
            "Важнее прозрачность: какой шаг за что отвечает и что делать, если он упал.\n\n"
            "Проблемы: трудно поддерживаемые конфигурации, завязанные на конкретные машины и людей.\n"
            "Где особенно заметно: большие монорепозитории, сложные многоступенчатые релизы."
        )
    },
    {
        "title": "Следи за выгоранием и режимом",
        "body": (
            "Смысл: усталый разработчик производит больше багов и принимает худшие решения.\n\n"
            "Теория: методологии и правила не работают, если человек выжат. "
            "Нормальный темп, паузы и предсказуемый процесс важны так же, как и технические практики.\n\n"
            "Проблемы: ночные релизы без откатов, фиксы на проде в состоянии «я уже ничего не понимаю».\n"
            "Где используется: любые долгие проекты, особенно в сессии и перед дедлайнами."
        )
    },
    {
        "title": "Система должна быть ремонтопригодной ночью",
        "body": (
            "Смысл: если система упадёт в 3 ночи, её можно будет поднять по документации и логам.\n\n"
            "Теория: важны понятные сообщения об ошибках, сценарии отката миграций, инструкции по рестарту сервисов. "
            "Это проверка зрелости проекта.\n\n"
            "Проблемы: «оно как-то крутится», отсутствие документации, ручные шаманские действия.\n"
            "Где критично: прод-сервисы, на которые завязаны пользователи и деньги."
        )
    },
    {
        "title": "Удаляй мёртвый код",
        "body": (
            "Смысл: неиспользуемый код создаёт шум и мешает понимать реальную картину.\n\n"
            "Теория: если функция или модуль не используются, их лучше удалить, чем держать «на всякий случай». "
            "Git хранит историю, всегда можно достать старую версию.\n\n"
            "Проблемы: старые ветви логики, которые никто не тестирует; ветки feature-flags, которые никогда не отключили.\n"
            "Где важно: большие проекты, которые пережили несколько поколений разработчиков."
        )
    },
    {
        "title": "Сделай так, чтобы будущий ты сказал спасибо",
        "body": (
            "Смысл: при каждом решении думай, что будет через полгода-год, когда ты вернёшься к этому коду.\n\n"
            "Теория: это мета-правило: читаемый код, нормальная структура, документация, адекватные тесты и истории коммитов. "
            "Тогда будущий ты (или другой разработчик) не будет тратить день на восстановление контекста.\n\n"
            "Где используется: везде, где проект живёт дольше пары недель и должен хотя бы раз пережить переотчёт/рефакторинг."
        )
    },
]

class RulesTab(QWidget):
    def __init__(self):
        super().__init__()
        self._workers: List[QThread] = []

        layout = QVBoxLayout(self)

        # верх: модель
        top = QHBoxLayout()
        self.model_edit = QLineEdit(DEFAULT_OPENROUTER_MODEL)
        self.model_edit.setPlaceholderText("model (OpenRouter)")
        top.addWidget(QLabel("Модель:"))
        top.addWidget(self.model_edit, 1)
        layout.addLayout(top)

        # центральная часть: список правил + теория
        split = QSplitter(Qt.Orientation.Horizontal)

        left = QWidget()
        l_left = QVBoxLayout(left)
        self.rules_list = QListWidget()
        for i, rule in enumerate(RULES_DATA, start=1):
            short = f"{i}) {rule['title']}"
            if len(short) > 90:
                short = short[:90] + "..."
            self.rules_list.addItem(QListWidgetItem(short))
        self.rules_list.currentRowChanged.connect(self.on_rule_selected)
        l_left.addWidget(QLabel("Правила"))
        l_left.addWidget(self.rules_list)

        right = QWidget()
        l_right = QVBoxLayout(right)
        self.rule_view = QPlainTextEdit()
        self.rule_view.setReadOnly(True)
        self.rule_view.setMaximumBlockCount(16000)
        self.rule_view.setPlaceholderText("Выбери правило слева, чтобы увидеть развернутое объяснение.")
        l_right.addWidget(QLabel("Текст правила и теория"))
        l_right.addWidget(self.rule_view)

        split.addWidget(left)
        split.addWidget(right)
        split.setSizes([320, 680])

        layout.addWidget(split)

        # низ: вопрос по правилу
        query_box = QGroupBox("Вопрос по выбранному правилу")
        q_layout = QVBoxLayout(query_box)

        self.question_edit = QPlainTextEdit()
        self.question_edit.setPlaceholderText("Напиши вопрос: как применить это правило в твоём проекте, "
                                              "как описать его в отчёте или как привести пример.")
        self.question_edit.setMaximumBlockCount(4000)

        btn_row = QHBoxLayout()
        self.ask_btn = QPushButton("Спросить по правилу")
        self.ask_btn.clicked.connect(self.on_ask_rule)
        btn_row.addStretch(1)
        btn_row.addWidget(self.ask_btn)

        self.answer_view = QPlainTextEdit()
        self.answer_view.setReadOnly(True)
        self.answer_view.setMaximumBlockCount(20000)
        self.answer_view.setPlaceholderText("Ответ модели по текущему правилу появится здесь.")

        q_layout.addWidget(self.question_edit)
        q_layout.addLayout(btn_row)
        q_layout.addWidget(self.answer_view)

        layout.addWidget(query_box)

        if self.rules_list.count() > 0:
            self.rules_list.setCurrentRow(0)

    def _start_worker(self, w: QThread):
        self._workers.append(w)
        def _cleanup():
            try:
                self._workers.remove(w)
            except ValueError:
                pass
            w.deleteLater()
        w.finished.connect(_cleanup)
        w.start()

    def _toggle(self, busy: bool):
        self.ask_btn.setEnabled(not busy)

    def on_rule_selected(self, index: int):
        if index < 0 or index >= len(RULES_DATA):
            self.rule_view.clear()
            return
        rule = RULES_DATA[index]
        text = f"{rule['title']}\n\n{rule['body']}"
        self.rule_view.setPlainText(text)

    def _llm_for_rule(self) -> str:
        idx = self.rules_list.currentRow()
        if idx < 0 or idx >= len(RULES_DATA):
            return "Правило не выбрано."
        rule = RULES_DATA[idx]
        question = self.question_edit.toPlainText().strip()
        if not question:
            return "Вопрос пуст."

        user_prompt = (
            f"Правило разработки:\n{rule['title']}\n\n"
            f"Подробное пояснение правила:\n{rule['body']}\n\n"
            f"Вопрос студента по этому правилу:\n{question}\n\n"
            "Ответь по-русски, чётко и прикладно: как применять это правило в реальных проектах, "
            "как его можно обосновать в пояснительной записке и какие примеры уместно привести. "
            "Ответ без Markdown, обычный текст."
        )
        system_prompt = (
            "Ты опытный разработчик и наставник. Объясняешь практические правила разработки простым, но точным языком. "
            "Без Markdown."
        )
        model = self.model_edit.text().strip() or DEFAULT_OPENROUTER_MODEL
        return llm_complete(user_prompt, model=model, system_prompt=system_prompt, timeout=120)

    def on_ask_rule(self):
        self._toggle(True)
        worker = FuncWorker(self._llm_for_rule)
        def on_res(ans: str):
            self.answer_view.appendPlainText(ans + "\n" + "-"*80)
            self._toggle(False)
        worker.result.connect(on_res)
        worker.error.connect(lambda e: (self.answer_view.appendPlainText(f"Ошибка: {e}"), self._toggle(False)))
        self._start_worker(worker)



# ================== MAIN WINDOW ==================

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        if os.path.exists(APP_ICON_PATH):
            self.setWindowIcon(QIcon(APP_ICON_PATH))

        self.ipm = IpManager()
        self.ip_autoswitch = QCheckBox("Сменять IP при старте ассистента")
        self.ip_now = QPushButton("Сменить IP сейчас")
        self.ip_label = QLabel("Текущий IP: —")
        self.ip_now.clicked.connect(self.on_ip_cycle)

        top = QHBoxLayout()
        top.addWidget(self.ip_autoswitch)
        top.addWidget(self.ip_now)
        top.addStretch(1)
        top.addWidget(self.ip_label)

        tabs = QTabWidget()
        tabs.addTab(ChatTab(DEFAULT_OPENROUTER_MODEL), "Чат")
        tabs.addTab(SearchTab(DEFAULT_OPENROUTER_KEY, DEFAULT_OPENROUTER_MODEL), "Поиск")
        tabs.addTab(GitTab(), "Git")
        tabs.addTab(CodeTab(DEFAULT_OPENROUTER_MODEL), "Код")
        tabs.addTab(SpecTab(DEFAULT_OPENROUTER_MODEL), "ТЗ / План")
        tabs.addTab(MethodologiesTab(), "Методологии")
        tabs.addTab(RulesTab(), "Правила")

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.addLayout(top)
        lay.addWidget(tabs)
        self.setCentralWidget(central)

        QTimer.singleShot(500, self._after_show)

    def _after_show(self):
        if self.ip_autoswitch.isChecked():
            self.on_ip_cycle()
        else:
            self.ip_label.setText(f"Текущий IP: {self.ipm.current_ip()}")

    def on_ip_cycle(self):
        msg = self.ipm.cycle()
        ip = self.ipm.current_ip()
        self.ip_label.setText(f"Текущий IP: {ip}")
        QMessageBox.information(self, "Смена IP", f"{msg}\nТекущий IP: {ip}")

# ================== main() с ловлей краша ==================

def main():
    app = QApplication(sys.argv)
    if os.path.exists(APP_ICON_PATH):
        app.setWindowIcon(QIcon(APP_ICON_PATH))
    w = MainWindow()
    w.resize(1280, 720)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback, datetime
        tb = traceback.format_exc()
        print("MIA упала с исключением:\n")
        print(tb)
        try:
            with open("mia_crash.log", "w", encoding="utf-8") as f:
                f.write(tb)
        except Exception:
            pass
        input("\nОшибка при запуске MIA. Лог сохранён в mia_crash.log (рядом с скриптом).\n"
              "Нажми Enter, чтобы закрыть окно...")
