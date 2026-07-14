"""Эмулятор COM-порта для отладки интерфейса без реального STM32.

FakeSerial имитирует интерфейс pyserial.Serial: генерация случайных CAN-кадров,
ответы на команды бутлоадера, воспроизведение CSV-дампов и стандартные методы read/write/close.
"""

import csv
import random
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QTimer

from core.can_protocol import (
    CMD_AUTO_SPEED,
    CMD_AUTO_SPEED_RESP,
    CMD_DEVICE_ID,
    CMD_DEVICE_ID_RESP,
    DEVICE_TYPE_BASIC,
    MARKER_RX,
    MARKER_RX_EXT,
    xor_checksum,
)
from models.logger import get_logger
from models.utils import hex_to_int

logger = get_logger(__name__)


def _parse_csv_time(time_str: str) -> Optional[float]:
    """Парсит строку времени из CSV в Unix timestamp."""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%H:%M:%S.%f", "%H:%M:%S"):
        try:
            return datetime.strptime(time_str, fmt).timestamp()
        except ValueError:
            continue
    try:
        return float(time_str)
    except ValueError:
        return None


class FakeSerial:
    """Фиктивный COM-порт для тестирования приложения на macOS и Windows."""

    def __init__(self, port: str = "FAKE", baudrate: int = 115200, error_probability: int = 0) -> None:
        """Создаёт эмулятор порта.

        Args:
            port: Имя порта (только для отображения).
            baudrate: Скорость обмена (только для отображения).
            error_probability: Вероятность симуляции ошибки CAN (0-100).
        """
        self.port = port
        self.baudrate = baudrate
        self._error_probability = max(0, min(100, error_probability))
        self._is_open = False
        self._rx_buffer = bytearray()
        self._buffer_lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._bootloader_mode = False
        self._last_address = 0

        self._replay_enabled = False
        self._replay_data: List[Tuple[float, int, int, bytes]] = []
        self._replay_index = 0
        self._replay_start_time = 0.0
        self._replay_timer = QTimer()
        self._replay_timer.setSingleShot(True)
        self._replay_timer.timeout.connect(self._emit_replay_frame)

    def load_replay_data(self, filepath: str) -> bool:
        """Загружает CSV-дамп с CAN-кадрами для воспроизведения.

        Поддерживает формат записи: timestamp,channel,id,dlc,data (data — hex-строка).
        """
        path = Path(filepath)
        if not path.exists():
            logger.error("Файл дампа не найден: %s", filepath)
            return False
        records: List[Tuple[float, int, int, bytes]] = []
        try:
            with path.open("r", encoding="utf-8", newline="") as file:
                reader = csv.reader(file)
                header = next(reader, None)
                if header is None:
                    return False
                header_lower = [h.strip().lower() for h in header]
                has_data_col = "data" in header_lower
                has_d0 = "d0" in header_lower
                time_idx = header_lower.index("time") if "time" in header_lower else 0
                channel_idx = header_lower.index("channel") if "channel" in header_lower else 1
                id_idx = header_lower.index("id") if "id" in header_lower else 2

                first_time: Optional[float] = None
                for row in reader:
                    if not row:
                        continue
                    ts = _parse_csv_time(row[time_idx].strip())
                    if ts is None:
                        continue
                    if first_time is None:
                        first_time = ts
                    try:
                        channel = int(row[channel_idx])
                        can_id = hex_to_int(row[id_idx])
                        if can_id is None:
                            continue
                        if has_data_col:
                            data_str = row[id_idx + 2] if len(row) > id_idx + 2 else ""
                            data = bytes(int(b, 16) for b in data_str.split() if b)
                        elif has_d0:
                            d0_idx = header_lower.index("d0")
                            data = bytes(int(b, 16) for b in row[d0_idx:d0_idx + 8] if b)
                        else:
                            data = b""
                        records.append((ts - first_time, channel, can_id, data))
                    except (ValueError, IndexError):
                        continue
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка чтения дампа %s: %s", filepath, exc)
            return False

        if not records:
            return False

        self._replay_data = records
        self._replay_index = 0
        logger.info("Загружен дамп %s: %d кадров", filepath, len(records))
        return True

    def enable_replay(self, enabled: bool) -> None:
        """Включает/отключает режим воспроизведения дампа."""
        self._replay_enabled = enabled and bool(self._replay_data)

    def _start_replay(self) -> None:
        """Запускает воспроизведение с начала."""
        self._replay_index = 0
        self._replay_start_time = time.time()
        self._emit_replay_frame()

    def _emit_replay_frame(self) -> None:
        """Вставляет в буфер следующий кадр из дампа и планирует следующий."""
        if not self._is_open or not self._replay_enabled:
            return
        if self._replay_index >= len(self._replay_data):
            logger.info("Воспроизведение дампа завершено")
            return

        rel_time, channel, can_id, data = self._replay_data[self._replay_index]
        elapsed = time.time() - self._replay_start_time
        delay = rel_time - elapsed

        if delay > 0.001:
            self._replay_timer.start(int(delay * 1000))
            return

        frame = self._build_rx_frame(channel, can_id, data)
        with self._buffer_lock:
            self._rx_buffer.extend(frame)
        self._replay_index += 1
        self._emit_replay_frame()

    def _schedule_frame(self) -> None:
        """Добавляет в буфер случайный CAN-кадр и планирует следующий."""
        if not self._is_open or self._replay_enabled:
            return

        channel = random.choice([0x01, 0x02])
        # 25% пакетов с Extended CAN-ID
        if random.random() < 0.25:
            can_id = random.randint(0x800, 0x1FFFFFFF)
        else:
            can_id = random.randint(0x000, 0x7FF)
        length = random.randint(1, 8)
        data = bytes(random.randint(0, 255) for _ in range(length))
        # В эмуляторе используем маркер приёма
        frame = self._build_rx_frame(channel, can_id, data)

        # Симуляция ошибки CAN: портится контрольная сумма
        if random.randint(1, 100) <= self._error_probability:
            frame = self._corrupt_frame(frame)
            logger.info("Симулирована ошибка CAN в канале %d", channel)

        with self._buffer_lock:
            self._rx_buffer.extend(frame)

        self._timer = threading.Timer(random.uniform(0.05, 0.3), self._schedule_frame)
        self._timer.daemon = True
        self._timer.start()

    @staticmethod
    def _build_rx_frame(channel: int, can_id: int, data: bytes) -> bytes:
        """Формирует входящий CAN-кадр с корректной XOR-контрольной суммой."""
        data = bytes(data)[:8]
        length = len(data)
        if can_id > 0x7FF:
            frame = bytes([MARKER_RX_EXT, channel & 0xFF])
            frame += can_id.to_bytes(4, "little")
            frame += bytes([length])
        else:
            frame = bytes([MARKER_RX, channel & 0xFF, can_id & 0xFF, (can_id >> 8) & 0xFF, length])
        frame += data
        frame += bytes([xor_checksum(frame)])
        return frame

    @staticmethod
    def _corrupt_frame(frame: bytes) -> bytes:
        """Портит контрольную сумму кадра для имитации ошибки шины."""
        if len(frame) < 2:
            return frame
        frame = bytearray(frame)
        frame[-1] ^= 0xFF
        return bytes(frame)

    def open(self) -> None:
        """Открывает эмулятор порта и запускает генерацию или воспроизведение."""
        self._is_open = True
        if self._replay_enabled:
            self._start_replay()
        else:
            self._schedule_frame()

    def close(self) -> None:
        """Закрывает эмулятор и останавливает таймеры."""
        self._is_open = False
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self._replay_timer.stop()

    def is_open(self) -> bool:
        """Возвращает True, если эмулятор активен."""
        return self._is_open

    def read(self, size: int = 1) -> bytes:
        """Читает указанное количество байт из буфера.

        Args:
            size: Сколько байт нужно прочитать.

        Returns:
            Байтовая строка, возможно меньше запрошенного размера.
        """
        with self._buffer_lock:
            chunk = self._rx_buffer[:size]
            self._rx_buffer = self._rx_buffer[size:]
        return bytes(chunk)

    def in_waiting(self) -> int:
        """Возвращает количество байт, готовых к чтению."""
        with self._buffer_lock:
            return len(self._rx_buffer)

    def write(self, data: bytes) -> int:
        """Записывает данные в эмулятор и формирует ответ.

        Для бутлоадера эмулирует ACK (0x79) и ответы на команды.
        Для CAN-кадров ничего не возвращает.

        Args:
            data: Байты, отправленные в порт.

        Returns:
            Количество записанных байт.
        """
        if not data:
            return 0

        # Эмуляция команд идентификации и автоопределения скорости моста
        if data[0] == CMD_DEVICE_ID:
            serial = b"DEMO"
            total_memory = 1024
            response = bytes([CMD_DEVICE_ID_RESP, DEVICE_TYPE_BASIC, 0x00, len(serial)])
            response += serial
            response += bytes([(total_memory >> 8) & 0xFF, total_memory & 0xFF])
            self._append_response(response)
            return len(data)

        if data[0] == CMD_AUTO_SPEED:
            speed = 500
            self._append_response(bytes([CMD_AUTO_SPEED_RESP, (speed >> 8) & 0xFF, speed & 0xFF]))
            return len(data)

        # Базовая эмуляция бутлоадера STM32
        if data[0] == 0x7F:
            self._bootloader_mode = True
            self._append_response(bytes([0x79]))
            return len(data)

        if not self._bootloader_mode:
            return len(data)

        command = data[0]
        if command == 0x00:  # Get
            self._append_response(bytes([0x79, 0x01, 0x00, 0x79]))
        elif command == 0x11:  # Read Memory
            self._append_response(bytes([0x79]))
        elif command == 0x21:  # Go
            self._append_response(bytes([0x79]))
        elif command == 0x31:  # Write Memory
            self._append_response(bytes([0x79]))
        elif command == 0x43:  # Erase
            self._append_response(bytes([0x79]))
        elif command == 0x44:  # Extended Erase
            self._append_response(bytes([0x79]))
        elif command == 0xFF:  # Mass erase
            self._append_response(bytes([0x79]))

        return len(data)

    def _append_response(self, response: bytes) -> None:
        """Добавляет ответ в приёмный буфер."""
        with self._buffer_lock:
            self._rx_buffer.extend(response)

    def flush(self) -> None:
        """Пустая заглушка для совместимости с pyserial."""

    def reset_input_buffer(self) -> None:
        """Очищает приёмный буфер."""
        with self._buffer_lock:
            self._rx_buffer.clear()
