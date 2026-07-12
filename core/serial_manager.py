"""Менеджер COM-порта с отдельным потоком чтения.

SerialManager инкапсулирует работу с pyserial.Serial (или FakeSerial),
запускает поток чтения, парсит CAN-кадры и испускает сигналы для UI.
"""

import threading
import time
from typing import Optional, Union

from PySide6.QtCore import QObject, QThread, Signal, QTimer

from core.can_protocol import parse_all_frames
from core.fake_serial import FakeSerial

import serial

from models.config import Config
from models.logger import get_logger

logger = get_logger(__name__)


SerialPort = Union[serial.Serial, FakeSerial]


class SerialReader(QThread):
    """Поток непрерывного чтения данных из COM-порта."""

    new_frame = Signal(dict)
    error = Signal(str)
    heartbeat = Signal()

    def __init__(self, port: SerialPort, parent: Optional[QObject] = None) -> None:
        """Создаёт поток чтения.

        Args:
            port: Открытый объект порта (реальный или эмулятор).
            parent: Родительский QObject.
        """
        super().__init__(parent)
        self._port = port
        self._running = True
        self._buffer = bytearray()
        self._last_heartbeat = 0.0
        self._error_count = 0

    def _is_open(self) -> bool:
        """Возвращает True, если порт открыт, независимо от типа объекта."""
        try:
            return bool(self._port.is_open)
        except TypeError:
            return self._port.is_open()

    def _in_waiting(self) -> int:
        """Возвращает количество байт в буфере, независимо от типа объекта."""
        try:
            return self._port.in_waiting()
        except TypeError:
            return self._port.in_waiting

    def run(self) -> None:
        """Цикл чтения: накапливает байты, парсит CAN-кадры и эмитит сигналы."""
        logger.info("Поток чтения COM-порта запущен")
        while self._running:
            try:
                now = time.time()
                if now - self._last_heartbeat > 0.5:
                    self._last_heartbeat = now
                    self.heartbeat.emit()

                if not self._is_open():
                    time.sleep(0.05)
                    continue

                available = self._in_waiting()
                if available > 0:
                    chunk = self._port.read(min(available, 256))
                    if chunk:
                        self._buffer.extend(chunk)
                        self._error_count = 0
                        # Парсим все полные кадры из буфера и сдвигаем буфер
                        frames, self._buffer = parse_all_frames(self._buffer)
                        for frame in frames:
                            logger.debug(
                                "Принят CAN-кадр: ch=%s id=0x%08X dlc=%d",
                                frame["channel"],
                                frame["id"],
                                len(bytes(frame["data"])),
                            )
                            self.new_frame.emit(frame)
                else:
                    self._error_count = 0
                    self.msleep(5)
            except Exception as exc:  # noqa: BLE001
                self._error_count += 1
                logger.exception("Ошибка в потоке чтения COM-порта (подряд %d)", self._error_count)
                self.error.emit(str(exc))
                if self._error_count >= 5:
                    logger.error("Превышено допустимое количество ошибок чтения, поток остановлен")
                    self._running = False
                self.msleep(100)
        logger.info("Поток чтения COM-порта остановлен")

    def stop(self) -> None:
        """Запрашивает остановку потока."""
        self._running = False
        self.wait(2000)


class SerialManager(QObject):
    """Высокоуровневый менеджер для работы с COM-портом.

    Сигналы:
        new_can_frame(dict): получен новый CAN-кадр.
        error_occurred(str): произошла ошибка.
        connection_changed(bool): изменилось состояние подключения.
    """

    new_can_frame = Signal(dict)
    error_occurred = Signal(str)
    connection_changed = Signal(bool)
    heartbeat = Signal()

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """Создаёт менеджер без открытого порта."""
        super().__init__(parent)
        self._port: Optional[SerialPort] = None
        self._reader: Optional[SerialReader] = None
        self._lock = threading.Lock()
        self._config = Config()
        self._auto_reconnect = False
        self._reconnect_timer: Optional[QTimer] = None
        self._last_port_name = ""
        self._last_baudrate = 115200
        self._last_emulation = False
        self._closing = False
        self._replay_path: Optional[str] = None

    def is_open(self) -> bool:
        """Возвращает True, если порт открыт."""
        if self._port is None:
            return False
        is_open = getattr(self._port, "is_open", False)
        if callable(is_open):
            is_open = is_open()
        return bool(is_open)

    def current_port_name(self) -> str:
        """Возвращает имя текущего порта или пустую строку."""
        if self._port is None:
            return ""
        return getattr(self._port, "port", "")

    def open_port(self, port_name: str, baudrate: int, emulation: bool = False, auto_reconnect: bool = False, error_probability: int = 0) -> bool:
        """Открывает COM-порт (реальный или эмулированный).

        Args:
            port_name: Имя порта, например «COM3» или «/dev/tty.usbserial».
            baudrate: Скорость обмена.
            emulation: Если True, используется FakeSerial.
            auto_reconnect: Если True, автоматически переподключаться при ошибке.
            error_probability: Вероятность симуляции ошибки CAN в эмуляторе (0-100).

        Returns:
            True при успешном открытии, иначе False.
        """
        self._auto_reconnect = auto_reconnect
        self._last_port_name = port_name
        self._last_baudrate = baudrate
        self._last_emulation = emulation
        self._stop_reconnect_timer()
        self.close_port()
        try:
            if emulation:
                self._port = FakeSerial(port_name, baudrate, error_probability)
                if self._replay_path:
                    self._port.load_replay_data(self._replay_path)
                    self._port.enable_replay(True)
                self._port.open()
                logger.info("Открыт эмулированный порт %s (ошибки %d%%)", port_name, error_probability)
            else:
                self._port = serial.Serial(
                    port=port_name,
                    baudrate=baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.1,
                    write_timeout=1,
                )
                logger.info("Открыт реальный порт %s на скорости %d", port_name, baudrate)

            self._reader = SerialReader(self._port, self)
            self._reader.new_frame.connect(self.new_can_frame)
            self._reader.error.connect(self.error_occurred)
            self._reader.heartbeat.connect(self.heartbeat)
            self._reader.finished.connect(self._on_reader_finished)
            self._reader.start()
            self._config.set_bulk(
                {"port": port_name, "baudrate": baudrate, "emulation": emulation, "auto_reconnect": auto_reconnect, "error_probability": error_probability}
            )
            self.connection_changed.emit(True)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось открыть порт %s: %s", port_name, exc)
            self.error_occurred.emit(f"Не удалось открыть порт {port_name}: {exc}")
            self._port = None
            self.connection_changed.emit(False)
            self._schedule_reconnect()
            return False

    def close_port(self) -> None:
        """Закрывает порт и останавливает поток чтения."""
        self._closing = True
        self._stop_reconnect_timer()
        if self._reader is not None:
            self._reader.stop()
            self._reader = None

        if self._port is not None:
            try:
                self._port.close()
                logger.info("Порт %s закрыт", self.current_port_name())
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка при закрытии порта: %s", exc)
            self._port = None

        self.connection_changed.emit(False)
        self._closing = False

    def set_replay_path(self, path: Optional[str]) -> None:
        """Устанавливает путь к CSV-дампу для эмулятора."""
        self._replay_path = path
        logger.info("Установлен путь к дампу: %s", path)

    def send_data(self, data: bytes) -> bool:
        """Отправляет байты в порт в потокобезопасном режиме.

        Args:
            data: Байты для отправки.

        Returns:
            True, если отправка выполнена, иначе False.
        """
        if self._port is None or not self.is_open():
            logger.warning("Попытка отправки в закрытый порт")
            return False

        with self._lock:
            try:
                self._port.write(data)
                logger.debug("Отправлено в порт %d байт", len(data))
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка отправки в порт: %s", exc)
                self.error_occurred.emit(f"Ошибка отправки: {exc}")
                return False

    def __del__(self) -> None:
        """Гарантирует закрытие порта при удалении менеджера."""
        self._closing = True
        try:
            if self._reader is not None:
                self._reader.stop()
                self._reader = None
        except Exception:  # noqa: S110
            pass
        try:
            if self._port is not None:
                self._port.close()
                self._port = None
        except Exception:  # noqa: S110
            pass

    def _on_reader_finished(self) -> None:
        """Вызывается при завершении потока чтения; планирует переподключение."""
        if self._reader is not None:
            self._reader = None
        if self._port is not None:
            try:
                self._port.close()
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка закрытия порта при завершении потока: %s", exc)
            self._port = None
        self.connection_changed.emit(False)
        if not self._closing:
            self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Запускает таймер для автоматического переподключения."""
        if not self._auto_reconnect or self.is_open():
            return
        if self._reconnect_timer is not None and self._reconnect_timer.isActive():
            return
        logger.info("Планируется автоматическое переподключение к %s", self._last_port_name)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._do_reconnect)
        self._reconnect_timer.start(3000)

    def _stop_reconnect_timer(self) -> None:
        """Останавливает таймер переподключения."""
        if self._reconnect_timer is not None and self._reconnect_timer.isActive():
            self._reconnect_timer.stop()
            self._reconnect_timer = None

    def _do_reconnect(self) -> None:
        """Пытается восстановить соединение с COM-портом."""
        if self.is_open():
            return
        logger.info("Попытка автоматического переподключения к %s", self._last_port_name)
        if self.open_port(self._last_port_name, self._last_baudrate, self._last_emulation, self._auto_reconnect):
            logger.info("Автоматическое переподключение к %s успешно", self._last_port_name)
        else:
            logger.warning("Автоматическое переподключение к %s не удалось, будет повторная попытка", self._last_port_name)
