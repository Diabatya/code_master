"""Singleton-менеджер для загруженных DBC-файлов."""

import shutil
from pathlib import Path
from typing import Dict, Optional

from core.dbc_parser import parse_dbc
from models.config import Config
from models.logger import get_logger

logger = get_logger(__name__)

DBC_DIR = Path(__file__).resolve().parent.parent / "library" / "dbc"


class DBCManager:
    """Хранит и управляет загруженными DBC-описаниями CAN-сообщений."""

    _instance: Optional["DBCManager"] = None

    def __new__(cls) -> "DBCManager":
        """Создаёт или возвращает единственный экземпляр."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Инициализирует менеджер."""
        if self._initialized:
            return
        self._initialized = True
        self._config = Config()
        self._data: Dict[int, Dict[str, object]] = {}
        self._loaded_path: Optional[str] = None
        self._ensure_dir()
        self._load_last()

    def _ensure_dir(self) -> None:
        """Создаёт папку library/dbc, если её нет."""
        DBC_DIR.mkdir(parents=True, exist_ok=True)

    def _load_last(self) -> None:
        """Загружает последний использованный DBC при старте."""
        last_path = self._config.get("dbc_path", "")
        if last_path and Path(last_path).exists():
            self.load_dbc(last_path)

    def load_dbc(self, filepath: str) -> bool:
        """Загружает DBC-файл и копирует его в library/dbc.

        Args:
            filepath: Путь к .dbc файлу.

        Returns:
            True при успешной загрузке.
        """
        path = Path(filepath)
        if not path.exists():
            logger.error("DBC файл не найден: %s", filepath)
            return False

        try:
            data = parse_dbc(filepath)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка парсинга DBC %s: %s", filepath, exc)
            return False

        if not data:
            logger.warning("DBC %s не содержит сообщений", filepath)
            return False

        self._data = data
        self._loaded_path = str(path)
        try:
            dest = DBC_DIR / path.name
            shutil.copy2(path, dest)
            self._loaded_path = str(dest)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось скопировать DBC в библиотеку: %s", exc)

        self._config.set("dbc_path", self._loaded_path)
        logger.info("Загружен DBC: %s (%d сообщений)", self._loaded_path, len(data))
        return True

    def get_message(self, can_id: int) -> Optional[Dict[str, object]]:
        """Возвращает описание сообщения по ID."""
        return self._data.get(can_id)

    def get_message_by_name(self, name: str) -> Optional[Dict[str, object]]:
        """Возвращает описание сообщения по имени."""
        for message in self._data.values():
            if message.get("name") == name:
                return message
        return None

    def get_id_by_name(self, name: str) -> Optional[int]:
        """Возвращает CAN ID по имени сообщения."""
        for can_id, message in self._data.items():
            if message.get("name") == name:
                return can_id
        return None

    def get_signal_by_name(self, can_id: int, name: str) -> Optional[Dict[str, object]]:
        """Возвращает сигнал по ID сообщения и имени сигнала."""
        message = self._data.get(can_id)
        if message is None:
            return None
        for signal in message.get("signals", []):
            if signal.get("name") == name:
                return signal
        return None

    def get_all(self) -> Dict[int, Dict[str, object]]:
        """Возвращает полный словарь DBC."""
        return self._data

    def is_loaded(self) -> bool:
        """Возвращает True, если загружен хотя бы один DBC."""
        return bool(self._data)

    def loaded_path(self) -> Optional[str]:
        """Возвращает путь к загруженному DBC."""
        return self._loaded_path

    def describe_frame(self, can_id: int, data: bytes) -> str:
        """Формирует текстовое описание кадра на основе DBC."""
        message = self._data.get(can_id)
        if message is None:
            return ""
        lines = [f"{message.get('name', 'Unknown')} (ID 0x{can_id:X})"]
        for signal in message.get("signals", []):
            raw = _extract_raw(data, signal.get("start", 0), signal.get("length", 1), signal.get("byte_order", 1))
            if signal.get("signed", False):
                if raw >= 2 ** (signal.get("length", 1) - 1):
                    raw -= 2 ** signal.get("length", 1)
            physical = raw * signal.get("factor", 1.0) + signal.get("offset", 0.0)
            unit = signal.get("unit", "")
            enum = signal.get("values", {}).get(raw)
            value_text = f"{enum}" if enum else f"{physical:.2f} {unit}"
            lines.append(f"  {signal.get('name')}: {value_text} (raw {raw})")
        return "\n".join(lines)


def _extract_raw(data: bytes, start_bit: int, length: int, byte_order: int) -> int:
    """Извлекает raw-значение сигнала из байт кадра.

    Args:
        data: Байты CAN-кадра.
        start_bit: Стартовый бит.
        length: Длина бит.
        byte_order: 1 = little endian, 0 = big endian.

    Returns:
        Целое значение сигнала.
    """
    if not data:
        return 0
    total_bits = len(data) * 8
    if byte_order == 1:
        # Intel: LSB first
        value = 0
        for i in range(length):
            bit = start_bit + i
            if bit >= total_bits:
                break
            byte_index = bit // 8
            bit_index = bit % 8
            if (data[byte_index] >> bit_index) & 1:
                value |= 1 << i
        return value
    else:
        # Motorola: MSB first (simplified)
        value = 0
        for i in range(length):
            bit = start_bit + i
            if bit >= total_bits:
                break
            byte_index = bit // 8
            bit_index = 7 - (bit % 8)
            if (data[byte_index] >> bit_index) & 1:
                value |= 1 << (length - 1 - i)
        return value
