"""Режим «Только слушать» для COM-логгера.

Поддерживает два режима:
- embedded — перехват через SerialManager внутри приложения;
- proxy — двунаправленный проброс двух COM-портов (com0com).

Декодирует SLIP/CAN-поток, пишет CSV-лог и предоставляет готовые пакеты UI.
"""

import csv
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Callable, List, Optional

import serial
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


class _ProxyWorker(QThread):
    """Поток проброса из одного COM-порта в другой."""

    def __init__(
        self,
        src: serial.Serial,
        dst: serial.Serial,
        is_rx: bool,
        raw_queue: Queue,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._src = src
        self._dst = dst
        self._is_rx = is_rx
        self._raw_queue = raw_queue
        self._running = True

    def run(self) -> None:
        direction = "RX" if self._is_rx else "TX"
        logger.info("Поток проброса %s запущен", direction)
        while self._running:
            try:
                chunk = self._src.read(self._src.in_waiting or 1)
            except (serial.SerialException, OSError):
                break
            if chunk:
                try:
                    self._dst.write(chunk)
                except (serial.SerialException, OSError):
                    break
                self._raw_queue.put((self._is_rx, chunk))
            else:
                self.msleep(5)
        logger.info("Поток проброса %s остановлен", direction)

    def stop(self) -> None:
        self._running = False
        self.wait(2000)


class ListenOnlyMode(QObject):
    """Режим «Только слушать» с поддержкой embedded и proxy."""

    packet_ready = Signal(object)
    is_active_changed = Signal(bool)
    error = Signal(str)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._mode: str = ""
        self._serial_manager: Optional[Any] = None
        self._real_ser: Optional[serial.Serial] = None
        self._virtual_ser: Optional[serial.Serial] = None
        self._proxy_workers: List[_ProxyWorker] = []
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

    @property
    def mode(self) -> str:
        return self._mode

    def _open_csv(self, log_dir: Path, device_name: str) -> bool:
        name = (device_name or "Device").replace(" ", "_").replace("/", "_")
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        log_dir.mkdir(parents=True, exist_ok=True)
        self._log_path = log_dir / f"{name}_{stamp}.csv"
        try:
            self._log_file = open(self._log_path, "w", newline="", encoding="utf-8")
            self._csv_writer = csv.writer(self._log_file)
            self._csv_writer.writerow(["Timestamp_us", "Bus", "Type", "ID", "DLC", "Data"])
            return True
        except OSError as exc:
            logger.error("Не удалось создать CSV %s: %s", self._log_path, exc)
            self._log_path = None
            return False

    def _start_decoder(self) -> None:
        self._raw_queue = Queue()
        self._decoder = _DecoderWorker(self._raw_queue, self)
        self._decoder.packet_ready.connect(self._on_packet)
        self._decoder.start()

    def _stop_decoder(self) -> None:
        if self._decoder is not None:
            self._decoder.stop()
            self._decoder = None
        while not self._raw_queue.empty():
            try:
                self._raw_queue.get_nowait()
            except Empty:
                break

    def _close_csv(self) -> None:
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_file = None

    def enable(
        self,
        log_dir: Path,
        device_name: str = "",
        mode: str = "embedded",
        serial_manager: Optional[Any] = None,
        real_port: Optional[str] = None,
        virtual_port: Optional[str] = None,
        baudrate: int = 115200,
    ) -> bool:
        """Включает режим 'Только слушать' в выбранном режиме."""
        if self._active:
            return True

        if not self._open_csv(log_dir, device_name):
            return False

        self._start_decoder()

        if mode == "embedded":
            if serial_manager is None or not serial_manager.is_open():
                self._stop_decoder()
                self._close_csv()
                return False
            self._mode = "embedded"
            self._serial_manager = serial_manager
            serial_manager.raw_data.connect(self._on_raw_data)
            serial_manager.raw_tx.connect(self._on_raw_tx)

        elif mode == "proxy":
            if not real_port or not virtual_port:
                self._stop_decoder()
                self._close_csv()
                return False
            try:
                self._real_ser = serial.Serial(
                    real_port,
                    baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.05,
                    write_timeout=1,
                )
                self._virtual_ser = serial.Serial(
                    virtual_port,
                    baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.05,
                    write_timeout=1,
                )
            except (serial.SerialException, OSError) as exc:
                self._stop_decoder()
                self._close_csv()
                self._close_proxy_ports()
                self.error.emit(tr("Не удалось открыть COM-порт: {0}").format(exc))
                return False

            self._mode = "proxy"
            self._proxy_workers = [
                _ProxyWorker(self._real_ser, self._virtual_ser, True, self._raw_queue, self),
                _ProxyWorker(self._virtual_ser, self._real_ser, False, self._raw_queue, self),
            ]
            for worker in self._proxy_workers:
                worker.start()
        else:
            self._stop_decoder()
            self._close_csv()
            return False

        self._active = True
        self._packet_count = 0
        self.is_active_changed.emit(True)
        logger.info("Режим 'Только слушать' [%s] включён, CSV: %s", self._mode, self._log_path)
        return True

    def disable(self) -> None:
        """Выключает режим и закрывает CSV/порты."""
        if not self._active:
            return

        if self._mode == "embedded" and self._serial_manager is not None:
            try:
                self._serial_manager.raw_data.disconnect(self._on_raw_data)
            except Exception:  # noqa: BLE001
                pass
            try:
                self._serial_manager.raw_tx.disconnect(self._on_raw_tx)
            except Exception:  # noqa: BLE001
                pass

        for worker in self._proxy_workers:
            worker.stop()
        self._proxy_workers.clear()
        self._close_proxy_ports()

        self._stop_decoder()
        self._close_csv()

        self._active = False
        self._mode = ""
        self.is_active_changed.emit(False)
        logger.info("Режим 'Только слушать' выключён")

    def _close_proxy_ports(self) -> None:
        for ser in (self._real_ser, self._virtual_ser):
            if ser is not None and ser.is_open:
                try:
                    ser.close()
                except Exception:  # noqa: BLE001
                    pass
        self._real_ser = None
        self._virtual_ser = None

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
