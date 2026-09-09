"""Continuous offline Russian wake-word and command recognition for MIA."""

from __future__ import annotations

import importlib.util
import json
import os
import threading
from collections import deque
from pathlib import Path
from typing import ClassVar

from PySide6.QtCore import QThread, Signal

VOICE_RUNTIME_AVAILABLE = bool(
    importlib.util.find_spec("sounddevice") and importlib.util.find_spec("vosk")
)


def vosk_model_ready(path: str | Path) -> bool:
    root = Path(path)
    return all(
        candidate.is_file()
        for candidate in (
            root / "am" / "final.mdl",
            root / "conf" / "model.conf",
            root / "graph" / "HCLr.fst",
            root / "graph" / "Gr.fst",
        )
    )


def native_model_path(path: str | Path) -> str:
    """Use a Windows short path because the Vosk C API cannot open every Unicode path."""
    resolved = str(Path(path).resolve())
    if os.name != "nt":
        return resolved
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(32768)
        length = ctypes.windll.kernel32.GetShortPathNameW(
            resolved,
            buffer,
            len(buffer),
        )
        if length:
            return buffer.value
    except (AttributeError, OSError):
        pass
    return resolved


class WakeWordWorker(QThread):
    ready = Signal()
    phrase = Signal(str, bool, bytes)
    partial = Signal(str)
    failed = Signal(str)

    _models: ClassVar[dict[str, object]] = {}
    _model_lock = threading.Lock()

    def __init__(self, model_path: str | Path, sample_rate: int = 16_000):
        super().__init__()
        self.model_path = native_model_path(model_path)
        self.sample_rate = sample_rate
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

    def stop_listening(self) -> None:
        self._stop_event.set()

    def set_paused(self, paused: bool) -> None:
        if paused:
            self._pause_event.set()
        else:
            self._pause_event.clear()

    @classmethod
    def _get_model(cls, model_path: str):
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        with cls._model_lock:
            if model_path not in cls._models:
                cls._models[model_path] = Model(model_path)
            return cls._models[model_path]

    def run(self) -> None:
        if not VOICE_RUNTIME_AVAILABLE:
            self.failed.emit(
                "Не установлены Vosk и sounddevice. Выполни setup.ps1 -WithVoice."
            )
            return
        if not vosk_model_ready(self.model_path):
            self.failed.emit(
                "Русская модель Vosk не найдена. Выполни setup.ps1 -WithVoice."
            )
            return

        try:
            import sounddevice as sounddevice_module
            from vosk import KaldiRecognizer

            model = self._get_model(self.model_path)
            recognizer = KaldiRecognizer(model, self.sample_rate)
            wake_grammar = json.dumps(["мия", "миа", "[unk]"], ensure_ascii=False)
            wake_recognizer = KaldiRecognizer(model, self.sample_rate, wake_grammar)
            last_partial = ""
            was_paused = False
            pre_roll: deque[bytes] = deque(maxlen=8)
            utterance_blocks: list[bytes] = []
            speech_active = False
            wake_pending = False

            with sounddevice_module.RawInputStream(
                samplerate=self.sample_rate,
                blocksize=4000,
                dtype="int16",
                channels=1,
            ) as stream:
                self.ready.emit()
                while not self._stop_event.is_set():
                    data, _overflowed = stream.read(4000)
                    if self._pause_event.is_set():
                        if not was_paused:
                            recognizer.Reset()
                            wake_recognizer.Reset()
                            last_partial = ""
                            pre_roll.clear()
                            utterance_blocks.clear()
                            speech_active = False
                            wake_pending = False
                        was_paused = True
                        continue
                    if was_paused:
                        recognizer.Reset()
                        wake_recognizer.Reset()
                        was_paused = False

                    audio = bytes(data)
                    pre_roll.append(audio)
                    full_done = recognizer.AcceptWaveform(audio)
                    wake_done = wake_recognizer.AcceptWaveform(audio)
                    partial = json.loads(recognizer.PartialResult()).get(
                        "partial", ""
                    ).strip()
                    wake_partial = json.loads(wake_recognizer.PartialResult()).get(
                        "partial", ""
                    ).strip()
                    if not speech_active and (partial or wake_partial):
                        utterance_blocks = list(pre_roll)
                        speech_active = True
                    elif speech_active:
                        utterance_blocks.append(audio)
                        if len(utterance_blocks) > 96:
                            utterance_blocks = utterance_blocks[-96:]

                    if wake_done:
                        wake_result = json.loads(wake_recognizer.Result()).get(
                            "text", ""
                        ).strip()
                        wake_pending = wake_pending or any(
                            word in {"мия", "миа"} for word in wake_result.split()
                        )

                    if full_done:
                        result = json.loads(recognizer.Result()).get("text", "").strip()
                        captured_audio = b"".join(utterance_blocks or pre_roll)
                        last_partial = ""
                        if result or wake_pending:
                            self.phrase.emit(result, wake_pending, captured_audio)
                        pre_roll.clear()
                        utterance_blocks.clear()
                        speech_active = False
                        wake_pending = False
                    else:
                        if partial and partial != last_partial:
                            last_partial = partial
                            self.partial.emit(partial)
        except Exception as exc:  # noqa: BLE001 - worker boundary must report device errors
            if not self._stop_event.is_set():
                self.failed.emit(f"Ошибка голосового режима: {exc}")
