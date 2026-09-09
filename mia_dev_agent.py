# mia_dev_agent.py
# MIA — Мини-GUI ассистента: Чат / Поиск / Git / Код / ТЗ / Методологии / Правила + смена IP.
# Зависимости по максимуму: PySide6, requests, python-dotenv, ddgs, httpx, lxml, readability-lxml, GitPython
# Но при отсутствии части библиотек приложение не падает — соответствующий функционал просто отключается.

import os
import sys
import re
import base64
import importlib.util
import threading
from typing import Optional, List, Dict, Tuple

from dotenv import load_dotenv

BUNDLE_DIR = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else BUNDLE_DIR
os.environ.setdefault("MIA_DATA_DIR", DATA_DIR)
load_dotenv(os.path.join(DATA_DIR, ".env"))

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

VOICE_INPUT_AVAILABLE = bool(
    importlib.util.find_spec("sounddevice")
    and importlib.util.find_spec("faster_whisper")
)
sd = None
WhisperModel = None

from PySide6.QtCore import QLocale, Qt, QTimer, QThread, Signal
from PySide6.QtGui import QIcon, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QApplication, QWidget, QMainWindow, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QPlainTextEdit, QTabWidget, QFileDialog, QGridLayout, QTableWidget,
    QTableWidgetItem, QGroupBox, QCheckBox, QSplitter, QMessageBox, QAbstractItemView,
    QListWidget, QListWidgetItem, QComboBox, QTextBrowser, QProgressBar
)

try:
    from PySide6.QtTextToSpeech import QTextToSpeech
except ImportError:
    QTextToSpeech = None

from mia_core import (
    DEFAULT_SYSTEM_PROMPT,
    ConversationStore,
    MIAConfig,
    build_context,
    complete_ai,
    configured_provider_names,
    conversation_to_markdown,
    extract_wake_command,
    fetch_openrouter_models,
    is_safe_transcript_correction,
    provider_display_name,
    resolve_voice_command,
    stream_ai,
)
from mia_voice import VOICE_RUNTIME_AVAILABLE, WakeWordWorker, vosk_model_ready

APP_TITLE = "MIA Assistant 2.2"
APP_ICON_PATH = os.path.join(BUNDLE_DIR, "ui", "avatar_mia_v3.png")
CHAT_HISTORY_PATH = os.path.join(DATA_DIR, "memory", "chat_history.json")
WHISPER_MODELS_PATH = os.path.join(DATA_DIR, "models", "whisper")
WHISPER_MODEL_NAME = os.getenv("MIA_WHISPER_MODEL", "large-v3-turbo").strip() or "large-v3-turbo"
VOSK_MODEL_PATH = os.getenv("MIA_VOSK_MODEL_PATH", "").strip() or os.path.join(
    DATA_DIR,
    "models",
    "vosk-ru",
)

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


class ChatStreamWorker(QThread):
    chunk = Signal(str)
    provider = Signal(str)
    completed = Signal(str, bool)
    failed = Signal(str)

    def __init__(self, messages: List[Dict[str, str]], config: MIAConfig):
        super().__init__()
        self.messages = messages
        self.config = config
        self._cancel_event = threading.Event()

    def cancel(self):
        self._cancel_event.set()

    def run(self):
        try:
            answer = stream_ai(
                self.messages,
                self.config,
                self.chunk.emit,
                on_provider=self.provider.emit,
                cancel_event=self._cancel_event,
            )
            self.completed.emit(answer, self._cancel_event.is_set())
        except Exception as exc:
            self.failed.emit(str(exc))


class VoiceInputWorker(QThread):
    transcribed = Signal(str)
    stage = Signal(int, str)
    failed = Signal(str)
    _models = {}
    _model_lock = threading.Lock()

    def __init__(
        self,
        seconds: int = 6,
        sample_rate: int = 16_000,
        pcm_audio: bytes | None = None,
    ):
        super().__init__()
        self.seconds = seconds
        self.sample_rate = sample_rate
        self.pcm_audio = pcm_audio

    @staticmethod
    def _load_dependencies():
        global sd, WhisperModel
        if not VOICE_INPUT_AVAILABLE:
            raise RuntimeError(
                "Для диктовки установи зависимости: pip install -r requirements-voice.txt"
            )
        if sd is None or WhisperModel is None:
            import sounddevice as sounddevice_module
            from faster_whisper import WhisperModel as whisper_model_class

            sd = sounddevice_module
            WhisperModel = whisper_model_class

    @classmethod
    def _get_model(cls):
        with cls._model_lock:
            if WHISPER_MODEL_NAME not in cls._models:
                ensure_dir(WHISPER_MODELS_PATH)
                cls._models[WHISPER_MODEL_NAME] = WhisperModel(
                    WHISPER_MODEL_NAME,
                    device="cpu",
                    compute_type="int8",
                    download_root=WHISPER_MODELS_PATH,
                )
            return cls._models[WHISPER_MODEL_NAME]

    def run(self):
        try:
            self._load_dependencies()
            if self.pcm_audio is None:
                self.stage.emit(20, "Записываю речь…")
                recording = sd.rec(
                    int(self.seconds * self.sample_rate),
                    samplerate=self.sample_rate,
                    channels=1,
                    dtype="float32",
                )
                sd.wait()
                audio = recording.reshape(-1)
            else:
                import numpy as np

                audio = np.frombuffer(self.pcm_audio, dtype=np.int16).astype(np.float32)
                audio /= 32768.0
            self.stage.emit(32, f"Загружаю Whisper {WHISPER_MODEL_NAME}…")
            model = self._get_model()
            self.stage.emit(42, "Точно распознаю русскую речь…")
            segments, _ = model.transcribe(
                audio,
                vad_filter=False,
                beam_size=5,
                language="ru",
                temperature=0.0,
                condition_on_previous_text=False,
                initial_prompt=(
                    "Русская команда персональному ассистенту Мия. "
                    "Термины: Мия, DeepSeek, OpenRouter, Ollama, Python, GitHub, Windows."
                ),
                hotwords="Мия DeepSeek OpenRouter Ollama Python GitHub Windows",
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
            if not text:
                raise RuntimeError("Речь не распознана. Попробуй говорить ближе к микрофону.")
            self.transcribed.emit(text)
        except Exception as exc:
            self.failed.emit(f"Ошибка диктовки: {exc}")


class TranscriptCorrectionWorker(QThread):
    corrected = Signal(str)
    failed = Signal(str)

    def __init__(self, text: str, config: MIAConfig):
        super().__init__()
        self.text = text
        self.config = config

    def run(self):
        correction_config = MIAConfig(
            api_key=self.config.api_key,
            model=self.config.model,
            system_prompt=(
                "Ты корректор расшифровки русской речи. Исправь только ошибки "
                "распознавания, орфографию и пунктуацию. Не отвечай на команду, "
                "не добавляй факты и не меняй смысл. Верни одну исправленную строку."
            ),
            temperature=0.0,
            max_tokens=160,
            provider=self.config.provider,
        )
        messages = [
            {"role": "system", "content": correction_config.system_prompt},
            {"role": "user", "content": self.text},
        ]
        try:
            result = complete_ai(messages, correction_config, timeout=45)
            result = result.strip().strip("`\"'«»").strip()
            if (
                not is_safe_transcript_correction(self.text, result)
                or len(result) > max(500, len(self.text) * 3)
            ):
                raise RuntimeError("корректор вернул неподходящий текст")
            self.corrected.emit(result)
        except Exception as exc:  # noqa: BLE001 - original transcript is a safe fallback
            self.failed.emit(str(exc))

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
    if not system_prompt:
        system_prompt = DEFAULT_SYSTEM_PROMPT
    config = MIAConfig(
        api_key=key,
        model=model,
        system_prompt=system_prompt,
        max_tokens=2000,
        provider=os.getenv("MIA_PROVIDER", "auto").strip().casefold() or "auto",
    )
    messages = build_context(
        [{"role": "user", "content": prompt}],
        system_prompt=system_prompt,
        max_chars=50_000,
    )
    return complete_ai(messages, config, timeout=timeout)

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
    (r"github_pat_[A-Za-z0-9_]{20,}", "GitHub fine-grained PAT"),
    (r"sk-[A-Za-z0-9\-]{20,}", "OpenAI / OpenRouter / DeepSeek key"),
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
                    if fn == ".env":
                        # New repositories get a mandatory .env ignore rule before git add.
                        continue
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


def redact_secrets(text: str) -> str:
    redacted = text
    for pattern, name in SECRET_PATTERNS:
        redacted = re.sub(pattern, f"<{name}: скрыто>", redacted)
    return redacted

# ================== ВКЛАДКА ЧАТ ==================

class ChatTab(QWidget):
    def __init__(self, model: str):
        super().__init__()
        self.config = MIAConfig.from_env()
        self.config = MIAConfig(
            api_key=self.config.api_key,
            model=model or self.config.model,
            system_prompt=self.config.system_prompt,
            temperature=self.config.temperature,
            max_tokens=2500,
            provider=self.config.provider,
        )
        self.store = ConversationStore(CHAT_HISTORY_PATH)
        self.history = self.store.load()
        self._workers: List[QThread] = []
        self._chat_worker: Optional[ChatStreamWorker] = None
        self._voice_worker: Optional[VoiceInputWorker] = None
        self._correction_worker: Optional[TranscriptCorrectionWorker] = None
        self._wake_worker: Optional[WakeWordWorker] = None
        self._voice_request_pending = False
        self._voice_waiting_for_tts = False
        self._wake_armed = False
        self._voice_transcription_context: tuple[bool, str] | None = None
        self._pending_transcript = ""
        self._active_provider = ""
        self._stream_text = ""
        self._error_text = ""

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_history)

        self._wake_timer = QTimer(self)
        self._wake_timer.setSingleShot(True)
        self._wake_timer.setInterval(10_000)
        self._wake_timer.timeout.connect(self._disarm_wake_word)

        self._voice_resume_timer = QTimer(self)
        self._voice_resume_timer.setSingleShot(True)
        self._voice_resume_timer.setInterval(180_000)
        self._voice_resume_timer.timeout.connect(self._resume_voice_listening)

        self.tts = QTextToSpeech(self) if QTextToSpeech is not None else None
        if self.tts is not None:
            russian_locale = QLocale("ru_RU")
            if russian_locale in self.tts.availableLocales():
                self.tts.setLocale(russian_locale)
                russian_voices = self.tts.availableVoices()
                preferred = next(
                    (voice for voice in russian_voices if "Irina" in voice.name()),
                    russian_voices[0] if russian_voices else None,
                )
                if preferred is not None:
                    self.tts.setVoice(preferred)
            self.tts.stateChanged.connect(self._on_tts_state_changed)

        layout = QVBoxLayout(self)

        top = QHBoxLayout()
        top.addWidget(QLabel("Модель:"))
        provider_names = configured_provider_names(self.config)
        displayed_model = self.config.model
        if "DeepSeek API" in provider_names and "OpenRouter" not in provider_names:
            displayed_model = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        elif not any(name in provider_names for name in ("DeepSeek API", "OpenRouter")):
            if "Qwen2.5 · локально" in provider_names:
                displayed_model = "local/qwen2.5-3b-instruct"
            elif "Ollama · локально" in provider_names:
                displayed_model = os.getenv("OLLAMA_MODEL", "qwen3:4b")
        self.modelEdit = QComboBox()
        self.modelEdit.setEditable(True)
        self.modelEdit.addItem(displayed_model)
        self.modelEdit.setCurrentText(displayed_model)
        self.modelEdit.setMinimumWidth(280)
        self.refreshModelsBtn = QPushButton("Обновить список")
        self.refreshModelsBtn.setObjectName("secondaryButton")
        self.refreshModelsBtn.clicked.connect(self.refresh_models)
        self.modelStatus = QLabel(
            "Авто: " + " → ".join(provider_names)
            if provider_names
            else "ИИ-провайдер не настроен"
        )
        self.modelStatus.setMinimumWidth(190)
        self.modelStatus.setObjectName("statusOk" if provider_names else "statusError")
        top.addWidget(self.modelEdit, 1)
        top.addWidget(self.refreshModelsBtn)
        top.addWidget(self.modelStatus)

        actions = QHBoxLayout()
        self.newChatBtn = QPushButton("Новый диалог")
        self.newChatBtn.setObjectName("secondaryButton")
        self.newChatBtn.clicked.connect(self.new_chat)
        self.exportBtn = QPushButton("Экспорт .md")
        self.exportBtn.setObjectName("secondaryButton")
        self.exportBtn.clicked.connect(self.export_chat)
        self.copyBtn = QPushButton("Копировать ответ")
        self.copyBtn.setObjectName("secondaryButton")
        self.copyBtn.clicked.connect(self.copy_last_answer)
        self.speakBtn = QPushButton("Озвучить ответ")
        self.speakBtn.setObjectName("secondaryButton")
        self.speakBtn.clicked.connect(self.speak_last_answer)
        self.autoSpeak = QCheckBox("Озвучивать автоматически")
        self.speakBtn.setEnabled(self.tts is not None)
        self.autoSpeak.setEnabled(self.tts is not None)
        actions.addWidget(self.newChatBtn)
        actions.addWidget(self.exportBtn)
        actions.addWidget(self.copyBtn)
        actions.addWidget(self.speakBtn)
        actions.addWidget(self.autoSpeak)
        actions.addStretch(1)

        voice_row = QHBoxLayout()
        self.voiceModeBtn = QPushButton("Голосовой режим: ВЫКЛ")
        self.voiceModeBtn.setObjectName("voiceModeButton")
        self.voiceModeBtn.setCheckable(True)
        self.voiceModeBtn.setEnabled(
            VOICE_RUNTIME_AVAILABLE and vosk_model_ready(VOSK_MODEL_PATH)
        )
        self.voiceModeBtn.setToolTip(
            "После включения скажи «Мия», затем команду. Обработка речи выполняется локально."
        )
        self.voiceModeBtn.toggled.connect(self.toggle_voice_mode)
        self.voiceStatus = QLabel(
            "Скажи «Мия» после включения"
            if self.voiceModeBtn.isEnabled()
            else "Установи русскую модель: setup.ps1 -WithVoice"
        )
        self.voiceStatus.setObjectName("voiceStatus")
        voice_row.addWidget(self.voiceModeBtn)
        voice_row.addWidget(self.voiceStatus, 1)

        progress_row = QHBoxLayout()
        self.actionStage = QLabel("Готово к работе")
        self.actionStage.setObjectName("actionStage")
        self.actionProgress = QProgressBar()
        self.actionProgress.setRange(0, 100)
        self.actionProgress.setValue(0)
        self.actionProgress.setFormat("Ожидание")
        self.actionProgress.setTextVisible(True)
        progress_row.addWidget(self.actionStage)
        progress_row.addWidget(self.actionProgress, 1)

        self.out = QTextBrowser()
        self.out.setOpenExternalLinks(True)
        self.out.setPlaceholderText("Здесь появится диалог с MIA.")

        self.input = QPlainTextEdit()
        self.input.setPlaceholderText(
            "Напиши задачу для MIA. Можно просить объяснить, придумать, проверить или составить план…"
        )
        self.input.setMinimumHeight(100)
        self.input.setMaximumHeight(190)

        send_row = QHBoxLayout()
        hint = QLabel("Ctrl+Enter — отправить")
        self.micBtn = QPushButton("Диктовать 6 сек")
        self.micBtn.setObjectName("secondaryButton")
        self.micBtn.setEnabled(VOICE_INPUT_AVAILABLE)
        self.micBtn.setToolTip(
            "Записать голос и преобразовать его в текст локальной моделью Whisper"
        )
        self.micBtn.clicked.connect(self.start_dictation)
        self.stopBtn = QPushButton("Остановить")
        self.stopBtn.setObjectName("dangerButton")
        self.stopBtn.setEnabled(False)
        self.stopBtn.clicked.connect(self.stop_generation)
        self.askBtn = QPushButton("Отправить MIA")
        self.askBtn.clicked.connect(self.on_ask)
        send_row.addWidget(hint)
        send_row.addStretch(1)
        send_row.addWidget(self.micBtn)
        send_row.addWidget(self.stopBtn)
        send_row.addWidget(self.askBtn)

        self.sendShortcut = QShortcut(QKeySequence("Ctrl+Return"), self)
        self.sendShortcut.activated.connect(self.on_ask)

        layout.addLayout(top)
        layout.addLayout(actions)
        layout.addLayout(voice_row)
        layout.addLayout(progress_row)
        layout.addWidget(self.out)
        layout.addWidget(self.input)
        layout.addLayout(send_row)

        self._render_history()

    def _set_action_progress(self, value: int, text: str):
        value = max(0, min(100, int(value)))
        self.actionProgress.setRange(0, 100)
        self.actionProgress.setValue(value)
        self.actionProgress.setFormat(f"{value}% · {text}")
        self.actionStage.setText(text)

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
        self.newChatBtn.setEnabled(not busy)
        self.refreshModelsBtn.setEnabled(not busy)
        self.stopBtn.setEnabled(busy)

    def _current_model(self) -> str:
        return self.modelEdit.currentText().strip() or DEFAULT_OPENROUTER_MODEL

    def _render_history(self):
        messages = list(self.history)
        if self._stream_text:
            messages.append({"role": "assistant", "content": self._stream_text + " ▌"})
        if messages:
            markdown = conversation_to_markdown(messages)
            if self._error_text:
                safe_error = self._error_text.replace("\n", " ")
                markdown += f"\n---\n\n> ⚠️ {safe_error}\n"
            self.out.setMarkdown(markdown)
        else:
            self.out.setHtml(
                "<div style='margin:40px;color:#8b949e'>"
                "<h2 style='color:#58a6ff'>MIA готова к работе</h2>"
                "<p>История диалога хранится только на этом компьютере и не попадает в GitHub.</p>"
                "<p>Спроси что угодно или дай задачу. MIA помнит контекст текущего диалога.</p>"
                "</div>"
            )
        bar = self.out.verticalScrollBar()
        bar.setValue(bar.maximum())

    def refresh_models(self):
        provider_names = configured_provider_names(self.config)
        direct_deepseek = "DeepSeek API" in provider_names
        openrouter_ready = self.config.api_key.startswith("sk-or-v1-")
        if direct_deepseek and not openrouter_ready:
            self.modelEdit.clear()
            self.modelEdit.addItems(["deepseek-v4-flash", "deepseek-v4-pro"])
            self.modelEdit.setCurrentText(
                os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
            )
            self.modelStatus.setText("Прямой DeepSeek настроен")
            self.modelStatus.setObjectName("statusOk")
            return
        if not openrouter_ready:
            self.modelStatus.setText(
                "Список облачных моделей недоступен · локальная Qwen готова"
                if "Qwen2.5 · локально" in provider_names
                else "Добавь DEEPSEEK_API_KEY или OPENROUTER_API_KEY"
            )
            self.modelStatus.setObjectName(
                "statusOk" if "Qwen2.5 · локально" in provider_names else "statusError"
            )
            self.modelStatus.style().unpolish(self.modelStatus)
            self.modelStatus.style().polish(self.modelStatus)
            return
        self.refreshModelsBtn.setEnabled(False)
        self.modelStatus.setText("Проверяю OpenRouter…")
        worker = FuncWorker(fetch_openrouter_models, self.config.api_key)

        def on_models(models):
            current = self._current_model()
            self.modelEdit.blockSignals(True)
            self.modelEdit.clear()
            self.modelEdit.addItems(models)
            self.modelEdit.setCurrentText(current)
            self.modelEdit.blockSignals(False)
            self.modelStatus.setText(f"Онлайн · моделей: {len(models)}")
            self.modelStatus.setObjectName("statusOk")
            self.modelStatus.style().unpolish(self.modelStatus)
            self.modelStatus.style().polish(self.modelStatus)
            self.refreshModelsBtn.setEnabled(True)

        def on_error(message):
            self.modelStatus.setText(message)
            self.modelStatus.setObjectName("statusError")
            self.modelStatus.style().unpolish(self.modelStatus)
            self.modelStatus.style().polish(self.modelStatus)
            self.refreshModelsBtn.setEnabled(True)

        worker.result.connect(on_models)
        worker.error.connect(on_error)
        self._start_worker(worker)

    def on_ask(self):
        if self._chat_worker is not None:
            return
        prompt = self.input.toPlainText().strip()
        if not prompt:
            return
        self._error_text = ""
        self._stream_text = ""
        self.history.append({"role": "user", "content": prompt})
        self.store.save(self.history)
        self.input.clear()
        self._render_history()

        config = MIAConfig(
            api_key=self.config.api_key,
            model=self._current_model(),
            system_prompt=self.config.system_prompt,
            temperature=self.config.temperature,
            max_tokens=self.config.max_tokens,
            provider=self.config.provider,
        )
        messages = build_context(self.history, config.system_prompt)
        worker = ChatStreamWorker(messages, config)
        self._chat_worker = worker
        worker.provider.connect(self._on_provider_selected)
        worker.chunk.connect(self._on_chunk)
        worker.completed.connect(self._on_completed)
        worker.failed.connect(self._on_failed)
        worker.finished.connect(worker.deleteLater)
        if self._wake_worker is not None:
            self._wake_worker.set_paused(True)
        self._toggle_ui(True)
        self.modelStatus.setText("MIA думает…")
        self._set_action_progress(60, "Выбираю нейросеть…")
        worker.start()

    def _on_provider_selected(self, provider: str):
        self._active_provider = provider
        label = provider_display_name(provider)
        self.modelStatus.setText(f"Подключаю: {label}")
        self._set_action_progress(68, f"Подключаю {label}…")

    def _on_chunk(self, chunk: str):
        self._stream_text += chunk
        progress = min(92, 74 + len(self._stream_text) // 120)
        self._set_action_progress(progress, "Формирую ответ…")
        if not self._render_timer.isActive():
            self._render_timer.start(70)

    def _on_completed(self, answer: str, cancelled: bool):
        voice_request = self._voice_request_pending
        self._voice_request_pending = False
        self._render_timer.stop()
        self._stream_text = ""
        if answer:
            self.history.append({"role": "assistant", "content": answer})
            self.store.save(self.history)
        self._chat_worker = None
        self._toggle_ui(False)
        self._render_history()
        if cancelled:
            self.modelStatus.setText("Генерация остановлена")
            self._set_action_progress(100, "Остановлено")
            self._resume_voice_listening()
        else:
            provider = provider_display_name(self._active_provider) if self._active_provider else self._current_model()
            self.modelStatus.setText(f"Готово · {provider}")
            should_speak = bool(answer) and (voice_request or self.autoSpeak.isChecked())
            if should_speak:
                self._set_action_progress(95, "Озвучиваю ответ…")
                self._speak(answer, resume_voice=self.voiceModeBtn.isChecked())
            else:
                self._set_action_progress(100, "Готово")
                self._resume_voice_listening()

    def _on_failed(self, message: str):
        self._render_timer.stop()
        self._stream_text = ""
        self._error_text = message
        self._voice_request_pending = False
        self._chat_worker = None
        self._toggle_ui(False)
        self.modelStatus.setText("Ошибка подключения")
        self.modelStatus.setObjectName("statusError")
        self.modelStatus.style().unpolish(self.modelStatus)
        self.modelStatus.style().polish(self.modelStatus)
        self._set_action_progress(100, "Ошибка — подробности в диалоге")
        self._render_history()
        self._resume_voice_listening()

    def stop_generation(self):
        if self._chat_worker is not None:
            self.modelStatus.setText("Останавливаю…")
            self._set_action_progress(95, "Останавливаю генерацию…")
            self._chat_worker.cancel()

    def new_chat(self):
        if not self.history:
            return
        answer = QMessageBox.question(
            self,
            "Новый диалог",
            "Очистить локальную историю текущего диалога?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        self.history = []
        self._error_text = ""
        self.store.clear()
        self._render_history()
        self.modelStatus.setText("Новый диалог создан")

    def export_chat(self):
        if not self.history:
            self.modelStatus.setText("Диалог пока пуст")
            return
        default_path = os.path.join(os.path.expanduser("~/Documents"), "mia_dialog.md")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Экспорт диалога",
            default_path,
            "Markdown (*.md);;Text (*.txt)",
        )
        if path:
            write_text(path, conversation_to_markdown(self.history))
            self.modelStatus.setText(f"Диалог сохранён: {os.path.basename(path)}")

    def _last_answer(self) -> str:
        for message in reversed(self.history):
            if message.get("role") == "assistant":
                return message.get("content", "")
        return ""

    def copy_last_answer(self):
        answer = self._last_answer()
        if answer:
            QApplication.clipboard().setText(answer)
            self.modelStatus.setText("Ответ скопирован")
        else:
            self.modelStatus.setText("Ответа пока нет")

    def _speak(self, text: str, resume_voice: bool = False):
        if self.tts is None:
            self.modelStatus.setText("Синтез речи недоступен")
            self._set_action_progress(100, "Ответ готов без озвучивания")
            if resume_voice:
                self._resume_voice_listening()
            return
        if self._wake_worker is not None:
            self._wake_worker.set_paused(True)
        self.tts.stop()
        self._voice_waiting_for_tts = resume_voice
        if resume_voice:
            self.voiceStatus.setText("MIA отвечает голосом…")
            self._voice_resume_timer.start()
        self.tts.say(text[:6000])

    def speak_last_answer(self):
        answer = self._last_answer()
        if answer:
            self._speak(answer, resume_voice=self.voiceModeBtn.isChecked())
            self.modelStatus.setText("Озвучиваю ответ")
        else:
            self.modelStatus.setText("Ответа пока нет")

    def start_dictation(self):
        if self._voice_worker is not None:
            return
        if not VOICE_INPUT_AVAILABLE:
            self.modelStatus.setText("Установи requirements-voice.txt")
            return
        self.micBtn.setEnabled(False)
        self.micBtn.setText("Говори…")
        self.modelStatus.setText("Записываю голос 6 секунд…")
        worker = VoiceInputWorker(seconds=6)
        self._voice_worker = worker

        def on_text(text):
            current = self.input.toPlainText().strip()
            self.input.setPlainText(f"{current} {text}".strip())
            self.input.setFocus()
            self.modelStatus.setText("Диктовка распознана — проверь текст и отправь")
            self._set_action_progress(100, "Диктовка распознана")

        def on_error(message):
            self._error_text = message
            self.modelStatus.setText("Ошибка микрофона")
            self._render_history()

        def cleanup_voice():
            self._voice_worker = None
            self.micBtn.setText("Диктовать 6 сек")
            self.micBtn.setEnabled(True)
            worker.deleteLater()

        worker.transcribed.connect(on_text)
        worker.stage.connect(self._set_action_progress)
        worker.failed.connect(on_error)
        worker.finished.connect(cleanup_voice)
        worker.start()

    def toggle_voice_mode(self, enabled: bool):
        if enabled:
            if not VOICE_RUNTIME_AVAILABLE or not vosk_model_ready(VOSK_MODEL_PATH):
                self.voiceModeBtn.blockSignals(True)
                self.voiceModeBtn.setChecked(False)
                self.voiceModeBtn.blockSignals(False)
                self.voiceStatus.setText("Выполни setup.ps1 -WithVoice")
                return
            self.voiceModeBtn.setText("Голосовой режим: ВКЛ")
            self.voiceStatus.setText("Загружаю русскую модель Vosk…")
            worker = WakeWordWorker(VOSK_MODEL_PATH)
            self._wake_worker = worker
            worker.ready.connect(self._on_voice_mode_ready)
            worker.partial.connect(self._on_voice_partial)
            worker.phrase.connect(self._on_voice_phrase)
            worker.failed.connect(self._on_voice_mode_failed)

            def cleanup_wake():
                if self._wake_worker is worker:
                    self._wake_worker = None
                worker.deleteLater()

            worker.finished.connect(cleanup_wake)
            worker.start()
        else:
            self._wake_timer.stop()
            self._voice_resume_timer.stop()
            self._wake_armed = False
            self._voice_request_pending = False
            self._voice_waiting_for_tts = False
            self._voice_transcription_context = None
            self._pending_transcript = ""
            if self.tts is not None:
                self.tts.stop()
            worker = self._wake_worker
            if worker is not None:
                worker.stop_listening()
                worker.wait(1500)
            self.voiceModeBtn.setText("Голосовой режим: ВЫКЛ")
            self.voiceStatus.setText("Микрофон выключен")
            self._set_action_progress(0, "Голосовой режим выключен")

    def _on_voice_mode_ready(self):
        self.voiceStatus.setText("Слушаю локально · скажи «Мия»")
        self.modelStatus.setText("Русский голосовой режим готов")
        self._set_action_progress(0, "Ожидаю слово «Мия»")

    def _on_voice_partial(self, text: str):
        if self._wake_worker is not None and not self._voice_request_pending:
            self.voiceStatus.setText(f"Слышу: {text[:90]}")

    def _on_voice_phrase(
        self,
        text: str,
        constrained_wake: bool = False,
        pcm_audio: bytes = b"",
    ):
        if (
            not self.voiceModeBtn.isChecked()
            or self._chat_worker is not None
            or self._voice_worker is not None
            or self._correction_worker is not None
        ):
            return
        heard_wake, command = resolve_voice_command(text, constrained_wake)
        if heard_wake:
            self._wake_armed = False
            self._wake_timer.stop()
            self._set_action_progress(18, "Слово «Мия» услышано")
            self._start_accurate_transcription(pcm_audio, True, command)
            return
        if self._wake_armed:
            self._wake_timer.stop()
            self._wake_armed = False
            self._start_accurate_transcription(pcm_audio, False, text)
        else:
            self.voiceStatus.setText("Слушаю локально · скажи «Мия»")

    def _start_accurate_transcription(
        self,
        pcm_audio: bytes,
        wake_expected: bool,
        fallback_command: str,
    ):
        if not pcm_audio:
            if fallback_command:
                self._start_transcript_correction(fallback_command)
            elif wake_expected:
                self._arm_voice_command()
            return
        if self._wake_worker is not None:
            self._wake_worker.set_paused(True)
        self._voice_transcription_context = (wake_expected, fallback_command)
        self.voiceStatus.setText("Распознаю команду точной моделью Whisper…")
        self._set_action_progress(28, "Подготавливаю запись…")
        worker = VoiceInputWorker(pcm_audio=pcm_audio)
        self._voice_worker = worker
        worker.stage.connect(self._set_action_progress)
        worker.transcribed.connect(self._on_accurate_transcript)
        worker.failed.connect(self._on_accurate_transcript_failed)

        def cleanup_voice():
            if self._voice_worker is worker:
                self._voice_worker = None
            worker.deleteLater()

        worker.finished.connect(cleanup_voice)
        worker.start()

    def _on_accurate_transcript(self, text: str):
        if not self.voiceModeBtn.isChecked():
            return
        wake_expected, fallback = self._voice_transcription_context or (False, "")
        self._voice_transcription_context = None
        command = text.strip()
        if wake_expected:
            heard_wake, whisper_command = extract_wake_command(command)
            if heard_wake:
                command = whisper_command
            elif fallback:
                command = fallback
            else:
                _heard, command = resolve_voice_command(command, constrained_wake=True)
        if not command:
            self._arm_voice_command()
            return
        self.voiceStatus.setText(f"Распознано: {command[:100]}")
        self._start_transcript_correction(command)

    def _on_accurate_transcript_failed(self, message: str):
        wake_expected, fallback = self._voice_transcription_context or (False, "")
        self._voice_transcription_context = None
        if fallback:
            self._start_transcript_correction(fallback)
        elif wake_expected:
            self._arm_voice_command()
        else:
            self._error_text = message
            self._render_history()
            self._resume_voice_listening()

    def _arm_voice_command(self):
        self._wake_armed = True
        self._wake_timer.start()
        if self._wake_worker is not None:
            self._wake_worker.set_paused(False)
        self.voiceStatus.setText("Слушаю команду…")
        self._set_action_progress(22, "Говорите команду после сигнала")
        QApplication.beep()

    def _start_transcript_correction(self, command: str):
        self._pending_transcript = command.strip()
        if not self._pending_transcript:
            self._resume_voice_listening()
            return
        self.voiceStatus.setText("Исправляю ошибки распознавания…")
        self._set_action_progress(50, "Исправляю текст по контексту…")
        worker = TranscriptCorrectionWorker(self._pending_transcript, self.config)
        self._correction_worker = worker
        worker.corrected.connect(self._on_transcript_corrected)
        worker.failed.connect(self._on_transcript_correction_failed)

        def cleanup_correction():
            if self._correction_worker is worker:
                self._correction_worker = None
            worker.deleteLater()

        worker.finished.connect(cleanup_correction)
        worker.start()

    def _on_transcript_corrected(self, command: str):
        self._pending_transcript = ""
        if self.voiceModeBtn.isChecked():
            self._submit_voice_command(command)

    def _on_transcript_correction_failed(self, _message: str):
        command = self._pending_transcript
        self._pending_transcript = ""
        if not self.voiceModeBtn.isChecked():
            return
        self.voiceStatus.setText("Корректор недоступен · использую точную расшифровку")
        self._submit_voice_command(command)

    def _submit_voice_command(self, command: str):
        command = command.strip()
        if not command:
            return
        self._wake_armed = False
        self._wake_timer.stop()
        if self._wake_worker is not None:
            self._wake_worker.set_paused(True)
        self.voiceStatus.setText(f"Команда: {command[:100]}")
        self._set_action_progress(58, "Команда подготовлена")
        self.input.setPlainText(command)
        self._voice_request_pending = True
        self.on_ask()
        if self._chat_worker is None:
            self._voice_request_pending = False
            self._resume_voice_listening()

    def _disarm_wake_word(self):
        self._wake_armed = False
        self.voiceStatus.setText("Команда не услышана · скажи «Мия» ещё раз")
        self._resume_voice_listening()

    def _on_tts_state_changed(self, state):
        if (
            self.tts is not None
            and state == QTextToSpeech.State.Ready
            and self._voice_waiting_for_tts
        ):
            self._voice_waiting_for_tts = False
            self._voice_resume_timer.stop()
            self._set_action_progress(100, "Ответ озвучен")
            QTimer.singleShot(350, self._resume_voice_listening)

    def _resume_voice_listening(self):
        self._voice_waiting_for_tts = False
        self._voice_resume_timer.stop()
        if self.voiceModeBtn.isChecked() and self._wake_worker is not None:
            self._wake_worker.set_paused(False)
            self.voiceStatus.setText("Слушаю локально · скажи «Мия»")
            if self._chat_worker is None and self._voice_worker is None:
                self._set_action_progress(0, "Ожидаю слово «Мия»")

    def _on_voice_mode_failed(self, message: str):
        self._error_text = message
        self._render_history()
        self.voiceStatus.setText(message)
        self.voiceModeBtn.setChecked(False)

    def closeEvent(self, event):
        if self._wake_worker is not None:
            self._wake_worker.stop_listening()
            self._wake_worker.wait(1500)
        if self._chat_worker is not None:
            self._chat_worker.cancel()
            self._chat_worker.wait(1500)
        if self.tts is not None:
            self.tts.stop()
        if self._voice_worker is not None and sd is not None:
            sd.stop()
            self._voice_worker.wait(1500)
        if self._correction_worker is not None:
            self._correction_worker.wait(1500)
        super().closeEvent(event)

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
logs/
memory/
models/
tts/
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

        # .gitignore: mandatory local/private paths are always enforced before git add.
        gi = os.path.join(path, ".gitignore")
        if not os.path.exists(gi):
            write_text(gi, PY_GITIGNORE)
            self._log("Создан .gitignore (Python).")
        else:
            current_ignore = read_text(gi)
            required_rules = [".env", ".venv/", "logs/", "memory/", "models/"]
            missing_rules = [rule for rule in required_rules if rule not in current_ignore.splitlines()]
            if missing_rules:
                suffix = "\n# Added by MIA for local/private data\n" + "\n".join(missing_rules) + "\n"
                write_text(gi, current_ignore.rstrip() + suffix)
                self._log("В .gitignore добавлены правила защиты локальных данных.")

        if r.git.ls_files(".env").strip():
            raise RuntimeError(
                ".env уже добавлен в индекс Git. Удали его из индекса командой "
                "git rm --cached .env и повтори попытку."
            )

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
            self._log(f"- {name}: {p}:{line_no} -> {redact_secrets(line)}")
        return True

    def _create_github_repo(self, owner: str, token: str, name: str, is_private: bool) -> str:
        """
        Создаёт репозиторий на GitHub и возвращает безопасный remote URL без токена.
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
        authenticated_owner = str(u.json().get("login", "")).strip()
        if authenticated_owner and authenticated_owner.casefold() != owner.casefold():
            raise RuntimeError(
                f"Токен принадлежит аккаунту {authenticated_owner}, а GITHUB_OWNER задан как {owner}."
            )

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

        return f"https://github.com/{owner}/{repo}.git"

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
                # Передаём PAT только через окружение дочернего git-процесса.
                # Токен не сохраняется в .git/config и не попадает в логи/аргументы команды.
                basic_token = base64.b64encode(
                    f"x-access-token:{token}".encode("utf-8")
                ).decode("ascii")
                with repo.git.custom_environment(
                    GIT_CONFIG_COUNT="1",
                    GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                    GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic_token}",
                    GIT_TERMINAL_PROMPT="0",
                ):
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
        self.ip_now = QPushButton("Проверить сеть / сменить прокси")
        self.ip_now.setObjectName("secondaryButton")
        self.ip_label = QLabel("Сеть: не проверена")
        self.ip_now.clicked.connect(self.on_ip_cycle)

        top = QHBoxLayout()
        if os.path.exists(APP_ICON_PATH):
            avatar = QLabel()
            avatar.setPixmap(
                QPixmap(APP_ICON_PATH).scaled(
                    46,
                    46,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            top.addWidget(avatar)
        brand = QVBoxLayout()
        title = QLabel("MIA Assistant")
        title.setObjectName("brandTitle")
        subtitle = QLabel("Персональный ИИ-помощник · память · голос · инструменты разработчика")
        subtitle.setObjectName("brandSubtitle")
        brand.addWidget(title)
        brand.addWidget(subtitle)
        top.addLayout(brand)
        top.addStretch(1)
        top.addWidget(self.ip_label)
        top.addWidget(self.ip_now)

        tabs = QTabWidget()
        self.chat_tab = ChatTab(DEFAULT_OPENROUTER_MODEL)
        tabs.addTab(self.chat_tab, "MIA / Чат")
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
        providers = configured_provider_names(MIAConfig.from_env())
        self.statusBar().showMessage(
            "ИИ: " + " → ".join(providers)
            if providers
            else "Добавь DEEPSEEK_API_KEY или OPENROUTER_API_KEY в .env"
        )

    def on_ip_cycle(self):
        msg = self.ipm.cycle()
        ip = self.ipm.current_ip()
        self.ip_label.setText(f"IP: {ip}")
        QMessageBox.information(self, "Смена IP", f"{msg}\nТекущий IP: {ip}")

# ================== main() с ловлей краша ==================

def main():
    app = QApplication(sys.argv)
    app.setApplicationName("MIA Assistant")
    app.setApplicationVersion("2.2.0")
    app.setStyle("Fusion")
    if os.path.exists(APP_ICON_PATH):
        app.setWindowIcon(QIcon(APP_ICON_PATH))
    style_path = os.path.join(BUNDLE_DIR, "ui", "style.qss")
    if os.path.exists(style_path):
        app.setStyleSheet(read_text(style_path))
    w = MainWindow()
    w.resize(1380, 820)
    w.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
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
