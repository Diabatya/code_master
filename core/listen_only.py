"""Режим «Только слушать» для COM-логгера (embedded).

Декодирует SLIP/CAN-поток, пишет CSV-лог и предоставляет готовые пакеты UI.
"""

import csv
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, List, Optional

from PySide6.QtCore import QObject, QThread, Signal

from models.logger import get_logger
from models.translations import _ as tr

logger = get_logger(__name__)

SLIP_END = 0xC0
SLIP_ESC = 0xDB
SLIP_ESC_XOR = 0x20


@dataclass
class CanPacket:
    """Распарсенный CAN-пакет из SLIP-протокола."""

    timestamp: int = 0
    flags: int = 0
    can_id: int = 0
    dlc: int = 0
    data: bytes = b""
    is_rx: bool = True
    valid: bool = False
    bus: str = "CAN1"

    @property
    def is_extended(self) -> bool:
        return bool(self.flags & 0x02)

    @property
    def type_str(self) -> str:
        return "Ext" if self.is_extended else "Std"

    def data_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.data)


class SlipDecoder:
    """SLIP-декодер с накопительным буфером."""

    def __init__(self) -> None:
        self._buf: bytearray = bytearray()
        self._escape = False

    def feed(self, data: bytes, callback: Callable[[bytearray], None]) -> None:
        """Обрабатывает сырые байты и вызывает callback на каждый завершённый пакет."""
        for byte in data:
            if byte == SLIP_END:
                if self._buf:
                    callback(self._buf)
                    self._buf.clear()
            elif byte == SLIP_ESC:
                self._escape = True
            else:
                if self._escape:
                    self._buf.append(byte ^ SLIP_ESC_XOR)
                    self._escape = False
                else:
                    self._buf.append(byte)

    @staticmethod
    def parse_packet(buffer: bytearray) -> CanPacket:
        """Парсит SLIP-пакет в CanPacket."""
        pkt = CanPacket()
        if len(buffer) < 10:
            return pkt
        off = 0
        try:
            pkt.timestamp = struct.unpack_from("<I", buffer, off)[0]
            off += 4
            pkt.flags = buffer[off]
            off += 1
            pkt.can_id = struct.unpack_from("<I", buffer, off)[0]
            off += 4
            pkt.dlc = buffer[off]
            off += 1
            if pkt.dlc <= 8 and off + pkt.dlc <= len(buffer):
                pkt.data = bytes(buffer[off : off + pkt.dlc])
                pkt.valid = True
            else:
                pkt.dlc = 0
        except struct.error:
            pass
        return pkt


class _DecoderWorker(QThread):
    """Поток, который забирает сырые данные, декодирует SLIP и эмитит пакеты."""

    packet_ready = Signal(object)

    def __init__(self, raw_queue: Queue, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._raw_queue = raw_queue
        self._running = True
        self._decoder = SlipDecoder()

    def run(self) -> None:
        logger.info("Поток декодирования логера запущен")
        while self._running:
            try:
                item = self._raw_queue.get(timeout=0.1)
            except Empty:
                continue
            if not self._running:
                break
            is_rx, raw = item
            self._decoder.feed(raw, lambda buf: self._emit_packet(buf, is_rx))
        logger.info("Поток декодирования логера остановлен")

    def _emit_packet(self, buffer: bytearray, is_rx: bool) -> None:
        pkt = SlipDecoder.parse_packet(buffer)
        if pkt.valid:
            pkt.is_rx = is_rx
            self.packet_ready.emit(pkt)

    def stop(self) -> None:
        self._running = False
        self.wait(2000)


class ListenOnlyMode(QObject):
    """Встроенный режим «Только слушать».

    Подписывается на сырые RX/TX данные SerialManager,
    декодирует SLIP/CAN и пишет CSV-лог.
    """

    packet_ready = Signal(object)
    is_active_changed = Signal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._serial_manager: Optional[Any] = None
        self._raw_queue: Queue = Queue()
        self._decoder: Optional[_DecoderWorker] = None
        self._log_file: Optional[Any] = None
        self._csv_writer: Optional[Any] = None
        self._log_path: Optional[Path] = None
        self._active = False
        self._lock = threading.Lock()
        self._packet_count = 0

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def packet_count(self) -> int:
        with self._lock:
            return self._packet_count

    @property
    def log_path(self) -> Optional[str]:
        return str(self._log_path) if self._log_path else None

    def enable(self, serial_manager: Any, log_dir: Path, device_name: str = "") -> bool:
        """Включает режим: открывает CSV, запускает декодер, подписывается на данные."""
        if self._active:
            return True
        if not serial_manager.is_open():
            return False
        self._serial_manager = serial_manager
        name = (device_name or "Device").replace(" ", "_").replace("/", "_")
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / f"{name}_{stamp}.csv"
        try:
            self._log_file = open(self._log_path, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._log_file)
            self._csv_writer.writerow(["Timestamp_us", "Bus", "Type", "ID", "DLC", "Data"])
        except OSError as exc:
            logger.error("Не удалось создать CSV %s: %s", self._log_path, exc)
            return False

        self._raw_queue = Queue()
        self._decoder = _DecoderWorker(self._raw_queue, self)
        self._decoder.packet_ready.connect(self._on_packet)
        self._decoder.start()

        serial_manager.raw_data.connect(self._on_raw_data)
        serial_manager.raw_tx.connect(self._on_raw_tx)

        self._active = True
        self._packet_count = 0
        self.is_active_changed.emit(True)
        logger.info("Режим 'Только слушать' включён, CSV: %s", self._log_path)
        return True

    def disable(self) -> None:
        """Выключает режим и закрывает CSV."""
        if not self._active:
            return
        if self._serial_manager is not None:
            try:
                self._serial_manager.raw_data.disconnect(self._on_raw_data)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._serial_manager.raw_tx.disconnect(self._on_raw_tx)
            except Exception:  # noqa: BLE001
                pass

        if self._decoder is not None:
            self._decoder.stop()
            self._decoder = None

        # Опустошаем очередь
        while not self._raw_queue.empty():
            try:
                self._raw_queue.get_nowait()
            except Empty:
                break

        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_file = None

        self._active = False
        self.is_active_changed.emit(False)
        logger.info("Режим 'Только слушать' выключён")

    def _on_raw_data(self, data: bytes, _timestamp: float) -> None:
        self._raw_queue.put((True, data))

    def _on_raw_tx(self, data: bytes, _timestamp: float) -> None:
        self._raw_queue.put((False, data))

    def _on_packet(self, pkt: CanPacket) -> None:
        with self._lock:
            self._packet_count += 1
        if self._csv_writer is not None and self._log_file is not None:
            self._csv_writer.writerow([
                pkt.timestamp,
                pkt.bus,
                pkt.type_str,
                f"0x{pkt.can_id:X}",
                pkt.dlc,
                pkt.data_hex(),
            ])
        self._log_file.flush()
        self.packet_ready.emit(pkt)
