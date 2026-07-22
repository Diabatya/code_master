"""Управление настройками приложения «Код Мастер».

Настройки хранятся в формате JSON в файле config.json рядом с приложением.
Класс Config реализован как синглтон, чтобы все части программы работали
с одним и тем же набором параметров.
"""

import json
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir


class Config:
    """Синглтон для хранения и автоматического сохранения настроек."""

    _instance: "Config | None" = None
    _lock: threading.Lock = threading.Lock()

    DEFAULT_CONFIG: dict[str, Any] = {
        "port": "",
        "baudrate": 115200,
        "emulation": False,
        "triggers": [],
        "gateway_rules": [],
        "ignore_list": [],
        "device_type": 0x00,
        "device_version": 0,
        "serial_number": "",
        "total_memory": 1024,
        "can1_speed": 500000,
        "can2_speed": 500000,
        "can_speed_auto": False,
        "can1_terminator": False,
        "can2_terminator": False,
        "target_mcu": "",
        "programmer_method": "stlink",
        "sleep_time": 0,
        "sleep_mode": 0,
    }

    def max_data_bytes(self) -> int:
        """Возвращает максимальную длину поля Data (8 байт для CAN 2.0)."""
        return 8

    def __new__(cls) -> "Config":
        """Создаёт или возвращает единственный экземпляр настроек."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Инициализирует путь к файлу конфигурации и загружает данные."""
        if self._initialized:
            return
        self._file_path = Path(user_data_dir("CodeMaster", appauthor=False, ensure_exists=True)) / "config.json"
        self._data = deepcopy(self.DEFAULT_CONFIG)
        self._initialized = True
        self.load()

    def load(self) -> None:
        """Загружает настройки из config.json, если файл существует."""
        if self._file_path.exists():
            try:
                with self._file_path.open("r", encoding="utf-8") as file:
                    loaded = json.load(file)
                    self._data.update(loaded)
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                print(f"Ошибка загрузки конфигурации: {exc}")

    def save(self) -> None:
        """Сохраняет текущие настройки в config.json."""
        try:
            with self._file_path.open("w", encoding="utf-8") as file:
                json.dump(self._data, file, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"Ошибка сохранения конфигурации: {exc}")

    def get(self, key: str, default: Any = None) -> Any:
        """Возвращает значение настройки по ключу."""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Устанавливает значение настройки и сразу сохраняет его."""
        self._data[key] = value
        self.save()

    def set_bulk(self, values: dict[str, Any]) -> None:
        """Обновляет несколько настроек за раз и сохраняет их."""
        self._data.update(values)
        self.save()

    def all(self) -> dict[str, Any]:
        """Возвращает полную копию текущих настроек."""
        return deepcopy(self._data)

    def save_to_file(self, path: str) -> None:
        """Экспортирует текущие настройки в указанный JSON-файл."""
        with Path(path).open("w", encoding="utf-8") as file:
            json.dump(self._data, file, ensure_ascii=False, indent=2)

    def load_from_file(self, path: str) -> None:
        """Загружает настройки из указанного JSON-файла и сохраняет их."""
        with Path(path).open("r", encoding="utf-8") as file:
            self._data = json.load(file)
        self.save()

    def reset_to_defaults(self) -> None:
        """Сбрасывает настройки к значениям по умолчанию."""
        self._data = deepcopy(self.DEFAULT_CONFIG)
