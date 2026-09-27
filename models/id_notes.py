"""Заметки оператора к CAN ID — кластерное хранилище на ПК.

Файл: <каталог конфигурации>/VAG/MQB/notes.json —
{ "<канал>:<hex id>": "текст заметки" }.

Кластерная структура: корень — марка (VAG), ветка — модель (MQB).
Дальше в мониторинге появится выбор марки/модели авто, и заметки (а
позже и другие данные по автомобилю) будут складываться в нужную папку
автоматически — для этого IdNotes параметризован make/model уже сейчас.

Хранилище отдельно от config.json: заметки — ручная работа оператора,
они не должны сбрасываться при обновлении приложения (config.json
часть ключей сбрасывает по смене версии) и не должны попадать в
экспорт *.kmc.
"""

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from models.config import Config
from models.logger import get_logger

logger = get_logger(__name__)


class IdNotes:
    """Синглтон-хранилище заметок по CAN ID текущей марки/модели."""

    _instance: "IdNotes | None" = None
    _lock = threading.Lock()

    # Кластер по умолчанию: <config>/VAG/MQB/notes.json.
    DEFAULT_MAKE = "VAG"
    DEFAULT_MODEL = "MQB"

    def __new__(cls) -> "IdNotes":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._make = self.DEFAULT_MAKE
        self._model = self.DEFAULT_MODEL
        self._notes: dict[str, str] = {}
        self._initialized = True
        self._load()

    @staticmethod
    def _cluster_dir(make: str, model: str) -> Path:
        return Config().config_dir() / make / model

    def notes_path(self) -> Path:
        return self._cluster_dir(self._make, self._model) / "notes.json"

    def set_vehicle(self, make: str, model: str) -> None:
        """Переключает ветку кластера (марка/модель) и перечитывает файл."""
        self._make = make or self.DEFAULT_MAKE
        self._model = model or self.DEFAULT_MODEL
        self._load()

    @staticmethod
    def _key(channel: int, can_id: int) -> str:
        return f"{channel}:{can_id:X}"

    def _load(self) -> None:
        path = self.notes_path()
        self._notes = {}
        if not path.exists():
            return
        try:
            loaded: Any = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Ошибка загрузки заметок ID (%s): %s", path, exc)
            return
        if isinstance(loaded, dict):
            self._notes = {
                str(k): str(v) for k, v in loaded.items() if str(v).strip()
            }

    def _save(self) -> None:
        path = self.notes_path()
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._notes, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("Ошибка сохранения заметок ID: %s", exc)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

    def get(self, channel: int, can_id: int) -> str:
        """Текст заметки к ID на канале (пусто, если заметки нет)."""
        return self._notes.get(self._key(channel, can_id), "")

    def has(self, channel: int, can_id: int) -> bool:
        return bool(self.get(channel, can_id))

    def set(self, channel: int, can_id: int, text: str) -> None:
        """Сохраняет заметку; пустой текст удаляет запись."""
        key = self._key(channel, can_id)
        text = text.strip()
        if text:
            self._notes[key] = text
        else:
            self._notes.pop(key, None)
        self._save()
