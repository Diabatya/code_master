"""Менеджер COM-порта с отдельным потоком чтения.

SerialManager инкапсулирует работу с pyserial.Serial (или FakeSerial),
запускает поток чтения, парсит CAN-кадры и испускает сигналы для UI.
"""

import threading
import time
from typing import Optional, Union

from PySide6.QtCore import QObject, QThread, Signal, QTimer

from core.can_protocol import (
    CMD_AUTO_SPEED,
    CMD_AUTO_SPEED_RESP,
    CMD_CAN_MODE,
    CMD_CAN_SPEED,
    CMD_CAN_STATS,
    CMD_TRIGGER_ENABLE,
    CMD_TRIGGER_STATS,
    CMD_SYSTEM_INFO,
    CMD_USB_STATS,
    CMD_CFG_FACTORY_RESET,
    CMD_CFG_READ,
    CMD_CFG_WRITE,
    CMD_DEVICE_ID,
    CMD_DEVICE_ID_RESP,
    CMD_DEVICE_INFO,
    CMD_DEVICE_INFO_RESP,
    DEVICE_TYPE_BASIC,
    MARKER_RX,
    MARKER_RX_EXT,
    MARKER_RX_RTR,
    MARKER_RX_RTR_EXT,
    parse_all_frames,
    unpack_can_frame,
    xor_checksum,
)
from core.fake_serial import FakeSerial

import serial
from serial.tools.list_ports import comports

from models.config import Config
from models.logger import get_logger

logger = get_logger(__name__)

# Команды, после которых прошивка перезагружает МК для пере-энумерации
# USB (новое имя/серийник, заводской сброс): порт умрёт через ~200 мс
# после ответа. Закрываем его сразу сами — иначе до обнаружения обрыва
# reader'ом следующие команды уходят в мёртвый порт таймаутами
# («прогрузка конфига после заводского сброса не работала ни разу»).
_REBOOTING_COMMANDS = frozenset((CMD_CFG_WRITE, CMD_CFG_FACTORY_RESET))

# USB IDs приложения (загрузчик — PID 0x5741, см. core/bootloader.py).
# Нужны для поиска устройства при пере-энумерации на другой COM.
USB_VID_CODEMASTER = 0x0483
USB_PID_APPLICATION = 0x5740


class _PortWriteTimeout(TimeoutError):
    """Устройство перестало принимать данные в OUT-эндпоинт.

    Отдельный тип, чтобы request_control мог отличить «порт мёртв»
    (запись не проходит вообще) от «ответ не дождались» — в первом случае
    повторная попытка бессмысленна, а дескриптор нужно закрывать и
    переподключаться немедленно.
    """


SerialPort = Union[serial.Serial, FakeSerial]

# Предохранитель от бесконечного роста буфера при потоке мусора без валидных кадров
MAX_BUFFER_SIZE = 65536

# Маркеры кадров МК→ПК — request_control пропускает их при ожидании ответа
# на управляющую команду (см. wire-формат в core/can_protocol.py).
_RX_FRAME_MARKERS = frozenset(
    (MARKER_RX, MARKER_RX_EXT, MARKER_RX_RTR, MARKER_RX_RTR_EXT)
)


class _ControlSession:
    """Пачка управляющих команд с одной остановкой reader'а.

    Создаётся через SerialManager.control_session(). Пока сеанс активен,
    request_control не гоняет QThread stop/start на каждый запрос —
    это десятки миллисекунд на команду, которые на 49 слотах триггеров
    складывались в десятки секунд ожидания оператора.
    """

    def __init__(self, manager: "SerialManager") -> None:
        self._manager = manager

    def __enter__(self) -> "_ControlSession":
        manager = self._manager
        with manager._lock:
            if manager._port is None or not manager.is_open():
                raise RuntimeError("Порт не подключен")
            if manager._control_session_depth == 0:
                manager._closing = True
                manager._stop_reader()
            manager._control_session_depth += 1
            manager._control_session_active = True
        return self

    def __exit__(self, *_exc) -> None:
        manager = self._manager
        with manager._lock:
            if manager._control_session_depth > 0:
                manager._control_session_depth -= 1
            if manager._control_session_depth == 0:
                manager._control_session_active = False
                manager._start_reader()
                manager._closing = False


def _parse_cfg_read_payload(data: bytes) -> "tuple[str, str]":
    """Разбирает payload ответа CMD_CFG_READ → (device_name, serial)."""
    name, serial = "", ""
    try:
        p = 0
        name_len = data[p]
        p += 1
        name = data[p : p + name_len].decode("utf-8", errors="ignore").strip()
        p += name_len
        serial_len = data[p]
        p += 1
        serial = data[p : p + serial_len].decode("utf-8", errors="ignore").strip()
    except (IndexError, UnicodeError):
        pass
    return name, serial


def _rx_frame_wire_len(buf: Union[bytes, bytearray]) -> Optional[int]:
    """Длина CAN-кадра МК→ПК в начале буфера, байт.

    None — кадр ещё не собран (ждать данные); -1 — маркер есть, но кадр
    заведомо битый (DLC>8 или неверная контрольная сумма) — пропустить
    один байт для ре-синхронизации.
    """
    marker = buf[0]
    extended = marker in (MARKER_RX_EXT, MARKER_RX_RTR_EXT)
    rtr = marker in (MARKER_RX_RTR, MARKER_RX_RTR_EXT)
    header = 7 if extended else 5  # marker + channel + id + dlc
    if len(buf) < header:
        return None
    dlc = buf[header - 1]
    if dlc > 8:
        return -1
    total = header + (0 if rtr else dlc) + 1  # +1 = checksum
    if len(buf) < total:
        return None
    if xor_checksum(buf[: total - 1]) != buf[total - 1]:
        return -1
    return total


def _scan_for_response(buffer: bytearray, marker: int) -> bool:
    """Прокручивает буфер до маркера ответа, пропуская CAN-кадры МК→ПК.

    Возвращает True, когда marker оказался в buffer[0] (ответ можно
    разбирать). Ложный маркер внутри данных CAN-кадра не срабатывает —
    кадры пропускаются целиком с проверкой контрольной суммы.
    """
    while buffer:
        first = buffer[0]
        if first in _RX_FRAME_MARKERS:
            wire_len = _rx_frame_wire_len(buffer)
            if wire_len is None:
                return False  # неполный кадр — ждём остаток
            del buffer[: max(wire_len, 1)]
            continue
        if first != marker:
            del buffer[0]
            continue
        return True
    return False


class SerialReader(QThread):
    """Поток непрерывного чтения данных из COM-порта."""

    new_frame = Signal(dict)
    new_raw_data = Signal(bytes, float)
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
        self._last_data_time = time.time()
        self._error_count = 0

    def _is_open(self) -> bool:
        """Возвращает True, если порт открыт, независимо от типа объекта."""
        is_open = self._port.is_open
        if callable(is_open):
            is_open = is_open()
        return bool(is_open)

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

                # Безусловный read(), а не опрос in_waiting: usbser.sys на
                # Windows не забирает данные с bulk-IN устройства без
                # ожидающего ReadFile — байты копились в МК и «приезжали»
                # с задержкой в секунды (команды уходили в таймаут, а
                # опоздавшие ответы съедались здесь). timeout=0.1 у порта:
                # пустой read() сам ждёт первый байт до ~100 мс — это и
                # есть постоянный опрос шины.
                chunk = self._port.read(256)
                if chunk:
                    logger.debug("SerialReader: прочитано %d байт", len(chunk))
                    self._last_data_time = now
                    self.new_raw_data.emit(chunk, time.time())
                    self._buffer.extend(chunk)
                    self._error_count = 0
                    # Парсим все полные кадры из буфера и сдвигаем буфер
                    frames, remainder = parse_all_frames(self._buffer)
                    if len(remainder) > MAX_BUFFER_SIZE:
                        logger.warning(
                            "Буфер приёма превысил %d байт, отбрасываю накопленный мусор",
                            MAX_BUFFER_SIZE,
                        )
                        remainder = remainder[-MAX_BUFFER_SIZE:]
                    self._buffer = bytearray(remainder)
                    for frame in frames:
                        logger.debug(
                            "Принят CAN-кадр: ch=%s id=0x%08X dlc=%d",
                            frame["channel"],
                            frame["id"],
                            len(bytes(frame["data"])),
                        )
                        self.new_frame.emit(frame)
                else:
                    # Пустое чтение реального порта уже подождало до
                    # 100 мс; у FakeSerial read() неблокирующий — пауза
                    # нужна, чтобы не крутить цикл впустую.
                    self._error_count = 0
                    self.msleep(5)
            except (serial.SerialException, OSError) as exc:
                logger.error("Ошибка COM-порта, соединение разорвано: %s", exc)
                self.error.emit(str(exc))
                self._running = False
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

    def pending_tail(self) -> bytes:
        """Неразобранный остаток буфера — возможный кусок кадра."""
        return bytes(self._buffer)

    def seed_buffer(self, data: bytes) -> None:
        """Подкладывает неразобранный хвост от предыдущего reader'а."""
        self._buffer.extend(data)


class SerialManager(QObject):
    """Высокоуровневый менеджер для работы с COM-портом.

    Сигналы:
        new_can_frame(dict): получен новый CAN-кадр.
        error_occurred(str): произошла ошибка.
        connection_changed(bool): изменилось состояние подключения.
    """

    new_can_frame = Signal(dict)
    raw_data = Signal(bytes, float)
    raw_tx = Signal(bytes, float)
    error_occurred = Signal(str)
    connection_changed = Signal(bool)
    # Порт открыт, началась идентификация устройства (CMD_DEVICE_ID/
    # CFG_READ занимают ~1-2 с и блокируют вызывающий поток). UI показывает
    # по этому сигналу оверлей «Загрузка настроек» — иначе до
    # connection_changed оператор видит устаревшие данные.
    connecting = Signal()
    heartbeat = Signal()
    device_identified = Signal(int, int)
    can_speed_detected = Signal(int)
    # Запланирована попытка автопереподключения: (задержка_сек, номер
    # попытки) — UI показывает «Переподключение через N с…» вместо
    # молчаливого шторма попыток каждые 3 секунды.
    reconnect_scheduled = Signal(int, int)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        """Создаёт менеджер без открытого порта."""
        super().__init__(parent)
        self._port: Optional[SerialPort] = None
        self._reader: Optional[SerialReader] = None
        # Недособранный хвост кадра, переносимый между остановкой и
        # запуском reader'а (и через предкомандную очистку порта) —
        # иначе кадр, разрезанный остановкой потока, терялся целиком.
        self._reader_carry = b""
        self._lock = threading.RLock()
        self._config = Config()
        self._auto_reconnect = False
        self._reconnect_timer: Optional[QTimer] = None
        self._reconnect_attempts = 0
        self._last_port_name = ""
        self._last_baudrate = 115200
        self._last_emulation = False
        self._closing = False
        # Финальное закрытие менеджера (ручное «Отключить», выход
        # приложения): после него expect_reboot/_do_reconnect не должны
        # воскрешать порт — иначе на закрытии окна запускалась вычитка
        # и зеркалила триггеры устройства в config.json (полевой баг:
        # «при закрытии приложения что-то отрабатывает»). open_port
        # снимает флаг — явное подключение снова разрешает реконнект.
        self._shutdown = False
        self._replay_path: Optional[str] = None
        # Пачка управляющих команд делит одну остановку reader'а
        # (см. control_session) — без этого каждый request_control
        # гонял бы QThread stop/start, и вычитка 49 слотов триггеров
        # занимала десятки секунд.
        self._control_session_active = False
        # Сессии реентерабельны: вложенный control_session (или
        # request_control из другого потока посреди пачки) не должен
        # перезапускать reader, пока внешняя сессия ещё жива — иначе
        # reader съедал бы ответы чужих команд.
        self._control_session_depth = 0

    def is_open(self) -> bool:
        """Возвращает True, если порт открыт."""
        with self._lock:
            if self._port is None:
                return False
            is_open = getattr(self._port, "is_open", False)
            if callable(is_open):
                is_open = is_open()
            return bool(is_open)

    def current_port_name(self) -> str:
        """Возвращает имя текущего порта или пустую строку."""
        with self._lock:
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
        with self._lock:
            self._auto_reconnect = auto_reconnect
            self._last_port_name = port_name
            self._last_baudrate = baudrate
            self._last_emulation = emulation
            self._stop_reconnect_timer()
            self.close_port()
            # Явное открытие — менеджер жив, реконнект снова разрешён
            # (close_port выше выставил _shutdown для финального пути).
            self._shutdown = False
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
                    logger.info("Открыт реальный порт %s на скорости %d (dtr=%s, rts=%s)", port_name, baudrate, self._port.dtr, self._port.rts)

                self._start_reader()
                # Сообщаем UI, что порт открыт и началась идентификация —
                # пока она идёт (и потом, пока идёт вычитка настроек),
                # оператор должен видеть «Загрузка настроек», а не
                # устаревшие поля из локального кэша.
                self.connecting.emit()
                self._detect_device_id()
                # В полевой лог — какая сборка прошивки реально стоит на
                # МК (commit + дата сборки): «прошили последней» без неё
                # не отличить от реально зашитой версии.
                if not emulation:
                    try:
                        info = self.read_system_info()
                        logger.info(
                            "Прошивка МК: app v%d, протокол %d, сборка «%s», commit %s",
                            int(info.get("application_version") or 0),
                            int(info.get("protocol_version") or 0),
                            info.get("build_datetime") or "?",
                            info.get("git_commit") or "?",
                        )
                        if "reset_flags" in info:
                            # Диагностика прошивки: почему МК перезагружался
                            # в прошлый раз и дёргалась ли эnumерация USB —
                            # ответ на «устройство отваливалось» без JTAG.
                            logger.info(
                                "Диагностика МК: сброс=0x%02X, usb_rst=%d, usb_disc=%d, rx_ovf=%d",
                                int(info.get("reset_flags") or 0),
                                int(info.get("usb_reset_count") or 0),
                                int(info.get("usb_disconnect_count") or 0),
                                int(info.get("rx_overflow_bytes") or 0),
                            )
                    except Exception:  # noqa: BLE001
                        logger.debug("Устройство не отдало SYSTEM_INFO")
                self._config.set_bulk(
                    {"port": port_name, "baudrate": baudrate, "emulation": emulation, "auto_reconnect": auto_reconnect, "error_probability": error_probability}
                )
                self._reconnect_attempts = 0
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
        """Закрывает порт и останавливает поток чтения.

        Это финальное закрытие: после него реконнект не планируется,
        пока кто-то явно не вызовет open_port (тот снимет _shutdown)."""
        with self._lock:
            self._shutdown = True
            self._closing = True
            self._stop_reconnect_timer()
            if self._reader is not None:
                try:
                    self._reader.finished.disconnect(self._on_reader_finished)
                except RuntimeError:
                    pass
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
        with self._lock:
            if self._port is None or not self.is_open():
                logger.warning("Попытка отправки в закрытый порт")
                return False
            try:
                preview = data[:16].hex(" ")
                self._port.write(data)
                self.raw_tx.emit(data, time.time())
                logger.info("Отправлено в порт %s: %d байт, preview=%s", self.current_port_name(), len(data), preview)
                return True
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка отправки в порт %s: %s", self.current_port_name(), exc)
                self.error_occurred.emit(f"Ошибка отправки: {exc}")
                return False

    @property
    def in_control_session(self) -> bool:
        """Идёт ли сейчас пачка управляющих команд.

        Периодическим опросам (CAN/USB-статистика монитора) нельзя
        вклиниваться между командами записи/чтения настроек: на живой
        шине каждая такая команда может отработать только за таймаут,
        держа _lock секундами и подвешивая всю серию — «прогрузка
        большого конфига с 5-10 раза».
        """
        return self._control_session_active

    def control_session(self):
        """Контекстный менеджер: одна остановка reader'а на пачку команд.

        Использование::

            with serial_manager.control_session():
                for i in range(49):
                    serial_manager.request_control(CMD_TRIGGER_READ, bytes((i,)))

        Без сеанса каждый request_control останавливает и пересоздаёт
        поток чтения (QThread stop/start) — вычитка всех слотов триггеров
        занимала десятки секунд. Внутри сеанса reader остановлен один раз.
        """
        return _ControlSession(self)

    def request_control(self, command: int, payload: bytes = b"", timeout: float = 2.0) -> bytes:
        """Выполняет синхронную команду конфигурационного протокола C0-C6.

        На время запроса останавливает общий reader, чтобы ответ не был
        разобран как CAN-кадр. Формат ответа: [command|0x10, status, len, data].
        """
        if len(payload) > 255:
            raise ValueError("Слишком длинный payload управляющей команды")
        with self._lock:
            if self._port is None or not self.is_open():
                raise RuntimeError("Порт не подключен")
            owns_session = not self._control_session_active
            if owns_session:
                self._closing = True
                self._stop_reader()
                self._control_session_active = True
                # Неявная сессия занимает уровень глубины — вложенный
                # control_session не перезапустит reader посреди обмена.
                self._control_session_depth += 1
            try:
                # Одна повторная попытка: сразу после переподключения USB
                # устройство может ещё доинициализироваться и проглотить
                # первую команду молча — вместо «Таймаут ответа на 0xCA»
                # повтор запроса спасает цикл сохранения.
                last_timeout: Optional[TimeoutError] = None
                for _attempt in range(2):
                    try:
                        result = self._control_roundtrip(command, payload, timeout)
                    except TimeoutError as exc:
                        last_timeout = exc
                        logger.warning(
                            "Команда 0x%02X: попытка %d без ответа (%s)",
                            command, _attempt + 1, exc,
                        )
                        if isinstance(exc, _PortWriteTimeout):
                            break  # OUT-эндпоинт мёртв — повтор бессмысленен
                        continue
                    except (serial.SerialException, OSError):
                        # Дескриптор порта умер прямо во время команды —
                        # та же обработка, что при ожидаемом ребуте МК.
                        self.expect_reboot()
                        raise
                    # Ответ дошёл — дальше прошивка перезагружает МК:
                    # порт закроем сразу, пока reader не споткнулся
                    # об уже мёртвый USB-дескриптор.
                    if command in _REBOOTING_COMMANDS:
                        self.expect_reboot()
                    return result
                if isinstance(last_timeout, _PortWriteTimeout):
                    # Устройство не принимает данные, хотя порт ещё
                    # «открыт»: закрываем дескриптор и переподключаемся
                    # сразу — иначе менеджер десятки секунд долбит
                    # зомби-хендл таймаутами, пока Windows не снимет
                    # устройство с шины (в поле — минута мёртвого порта).
                    self.expect_reboot()
                raise last_timeout  # type: ignore[misc]
            finally:
                if owns_session:
                    if self._control_session_depth > 0:
                        self._control_session_depth -= 1
                    if self._control_session_depth == 0:
                        self._control_session_active = False
                        self._start_reader()
                        self._closing = False

    def expect_reboot(self) -> None:
        """Устройство уходит в перезагрузку/пере-энумерацию USB.

        Закрывает порт сразу и чисто — без ожидания, пока reader сам
        споткнётся об уже мёртвый дескриптор (PermissionError 13). Так
        следующие команды не попадают в окно мёртвого порта, а
        авто-переподключение стартует немедленно.
        """
        with self._lock:
            self._closing = True
            self._stop_reader()
            if self._port is not None:
                try:
                    self._port.close()
                except Exception:  # noqa: BLE001
                    pass
                self._port = None
                self.connection_changed.emit(False)
            self._closing = False
        self._schedule_reconnect()

    def _drain_input_preserving_frames(self) -> None:
        """Чистит входной буфер порта, не теряя пришедшие CAN-кадры.

        Слепой reset_input_buffer() уничтожал кадры, приехавшие в паузу
        между остановкой reader'а и командой (каждый опрос статистики
        мониторинга останавливает поток чтения): МК кадр принял (RX в
        статусе рос) и переслал по USB, а ПК стёр его до парсинга —
        «монитор молчит, триггер будто не ответил». Дочитываем
        накопленное, целые кадры отдаём в мониторинг, прочее (мусор,
        устаревшие ответы) отбрасываем как и раньше.
        """
        # Хвост убитого reader'а склеиваем с дочитанным из порта —
        # кадр мог быть разрезан остановкой потока пополам.
        data = bytearray(self._reader_carry)
        self._reader_carry = b""
        for _ in range(8):  # кадры могут долетать прямо во время чистки
            try:
                pending = self._port.in_waiting
            except (serial.SerialException, OSError):
                break
            if not pending:
                break
            try:
                chunk = self._port.read(pending)
            except (serial.SerialException, OSError):
                break
            if not chunk:
                break
            data.extend(chunk)
        if data:
            frames, remainder = parse_all_frames(bytes(data))
            for frame in frames:
                self.new_can_frame.emit(frame)
            self._reader_carry = bytes(remainder)
        self._port.reset_input_buffer()

    def _control_roundtrip(self, command: int, payload: bytes, timeout: float) -> bytes:
        """Одна попытка команда→ответ. Ответ: [command|0x10, status, len, data]."""
        self._drain_input_preserving_frames()
        # pyserial.write() при занятом USB-канале может вернуть меньше
        # байт, чем попросили — обрезанная команда навсегда клала парсер
        # МК (ждал «ещё байты» бесконечно, все команды за ней уходили в
        # таймаут до физического переподключения порта). Дописываем до
        # конца, чтобы устройство видело только целые структуры.
        data = bytes((command & 0xFF, len(payload))) + payload
        written = 0
        write_deadline = time.time() + timeout
        while written < len(data):
            try:
                chunk = self._port.write(data[written:])
            except serial.SerialTimeoutException:
                chunk = 0
            if chunk is None:
                chunk = len(data) - written  # некоторые порты возвращают None
            if chunk == 0:
                if time.time() > write_deadline:
                    raise _PortWriteTimeout(f"Таймаут записи команды 0x{command:02X}")
                time.sleep(0.005)
                continue
            written += chunk
        deadline = time.time() + timeout
        response_marker = (command | 0x10) & 0xFF
        buffer = bytearray()
        while time.time() < deadline:
            # Явный read(), а не опрос in_waiting: usbser.sys на Windows
            # не держит постоянно поднятый запрос на bulk-IN — без
            # ожидающего ReadFile байты ответа сидят в МК при
            # in_waiting == 0, команда уходила в таймаут, а опоздавший
            # ответ подбирал перезапущенный reader. Порт открыт с
            # timeout=0.1: read() возвращает накопленное мгновенно либо
            # ждёт первый байт до ~100 мс — и держит канал чтения живым.
            try:
                chunk = self._port.read(256)
            except (serial.SerialException, OSError):
                # Команды, после которых МК сразу перезагружается
                # (CFG_WRITE, FACTORY_RESET): ack уходит в CDC, но
                # teardown порта обгоняет его чтение хостом —
                # «Device not configured» при исполненной команде,
                # а UI показывал ложную ошибку и уходил на
                # bootloader-fallback. Мёртвый порт здесь и есть
                # подтверждение: при отказе прошивка ответила бы
                # статусом и НЕ перезагружалась. Смерть в фазе
                # записи сюда не попадает — write() выше ловит свои
                # исключения отдельно.
                if command in _REBOOTING_COMMANDS:
                    self.expect_reboot()
                    return b""
                raise
            if chunk:
                buffer.extend(chunk)
            # Разбираем поток по кадрам: CAN-кадры МК→ПК пропускаем
            # целиком — иначе байт внутри данных кадра, совпавший с
            # маркером ответа, давал ложное срабатывание, и хост ждал
            # «ответ» мусорной длины до таймаута (наблюдалось как
            # «Таймаут ответа на команду 0xCA» на живой шине CAN).
            while buffer:
                first = buffer[0]
                if first in _RX_FRAME_MARKERS:
                    wire_len = _rx_frame_wire_len(buffer)
                    if wire_len is None:
                        break  # неполный кадр — ждём остаток
                    if wire_len > 0:
                        # Кадр не теряем: мониторинг и триггеры
                        # продолжают видеть шину во время команды.
                        frame = unpack_can_frame(bytes(buffer[:wire_len]))
                        if frame is not None:
                            self.new_can_frame.emit(frame)
                    del buffer[: max(wire_len, 1)]  # -1 → resync на 1 байт
                    continue
                if first != response_marker:
                    del buffer[0]
                    continue
                if len(buffer) < 3:
                    break
                status = buffer[1]
                if status > 0x03:
                    del buffer[0]  # ложный маркер — у протокола статусы 0..3
                    continue
                length = buffer[2]
                if len(buffer) < 3 + length:
                    break  # неполный ответ — ждём остаток
                result = bytes(buffer[3 : 3 + length])
                # Кадры, приехавшие следом за ответом в том же
                # USB-буре, тоже не теряем — после return они пропали
                # бы вместе с локальным буфером.
                frames, _rest = parse_all_frames(bytes(buffer[3 + length :]))
                for frame in frames:
                    self.new_can_frame.emit(frame)
                if status != 0:
                    raise RuntimeError(f"Устройство отклонило команду 0x{command:02X}: статус 0x{status:02X}")
                return result
            if not chunk:
                # FakeSerial.read() неблокирующий — без паузы цикл
                # крутится впустую; у реального порта пустой read()
                # уже подождал до 100 мс сам.
                time.sleep(0.005)
        raise TimeoutError(f"Таймаут ответа на команду 0x{command:02X}")

    def read_can_stats(self, channel: int) -> dict[str, int]:
        """Возвращает накопительные RX/TX/lost-счётчики CAN-канала.

        channel задаётся в формате wire-протокола: 1 = CAN1, 2 = CAN2.
        """
        payload = self.request_control(CMD_CAN_STATS, bytes((channel & 0xFF,)))
        if len(payload) < 12:
            raise RuntimeError("Некорректный ответ CMD_CAN_STATS")
        return {
            "rx_count": int.from_bytes(payload[0:4], "little"),
            "tx_count": int.from_bytes(payload[4:8], "little"),
            "lost_count": int.from_bytes(payload[8:12], "little"),
            "error_count": int.from_bytes(payload[12:16], "little") if len(payload) >= 16 else 0,
            "busoff_count": int.from_bytes(payload[16:20], "little") if len(payload) >= 20 else 0,
            "recovery_count": int.from_bytes(payload[20:24], "little") if len(payload) >= 24 else 0,
            "ready": int(payload[24]) if len(payload) >= 25 else 0,
            # Фактический бод-рейт канала (kbit/s), прошивки v2+ отдают
            # его в хвосте — по нему видно, применилась ли настройка.
            "baud_kbps": int.from_bytes(payload[25:27], "little") if len(payload) >= 27 else 0,
            # Кадры, не отправленные из-за занятых TX-ящиков (прошивки v3+):
            # пропавшие ответы триггеров под нагрузкой раньше были невидимы.
            "tx_fail_count": int.from_bytes(payload[27:31], "little") if len(payload) >= 31 else 0,
        }

    def set_trigger_enabled(self, index: int, enabled: bool) -> None:
        """Включает/выключает trigger без перезаписи всей структуры."""
        from core.trigger_protocol import TRIGGER_MAX_SLOTS

        if not 0 <= index < TRIGGER_MAX_SLOTS:
            raise ValueError("Некорректный индекс trigger")
        self.request_control(CMD_TRIGGER_ENABLE, bytes((index, int(enabled))))

    def set_can_mode(self, channel: int, mode: int, terminator: bool) -> None:
        """Устанавливает режим CAN (Normal=0/Silent=1) и состояние терминатора.

        channel: 1 = CAN1, 2 = CAN2.
        """
        self.request_control(CMD_CAN_MODE, bytes((channel & 0xFF, mode & 0xFF, int(terminator))))

    # Бод-рейты, которые умеет выставить bxCAN при APB1 = 36 МГц
    # (configure_bit_timing в firmware/application/Src/can_bridge.c).
    SUPPORTED_CAN_BAUD_KBPS = (1000, 500, 250, 125, 100, 50, 20, 10)

    def set_can_speed(self, channel: int, baud_kbps: int) -> int:
        """Применяет бод-рейт CAN-канала к периферии МК и персистит его.

        channel: 1 = CAN1, 2 = CAN2. baud_kbps — в кбит/с из списка
        SUPPORTED_CAN_BAUD_KBPS (иначе прошивка ответит ошибкой 0x02).
        Возвращает фактически применённый бод-рейт из ответа устройства.
        """
        if baud_kbps not in self.SUPPORTED_CAN_BAUD_KBPS:
            raise ValueError(
                f"Неподдерживаемая скорость CAN: {baud_kbps} кбит/с "
                f"(доступны {', '.join(map(str, self.SUPPORTED_CAN_BAUD_KBPS))})"
            )
        payload = bytes((channel & 0xFF,)) + (baud_kbps & 0xFFFF).to_bytes(2, "little")
        resp = self.request_control(CMD_CAN_SPEED, payload)
        return int.from_bytes(resp[0:2], "little") if len(resp) >= 2 else baud_kbps

    def read_trigger_stats(self) -> dict[str, int]:
        """Возвращает счётчик срабатываний и максимальную задержку trigger.

        Расширенные прошивки отдают 12 байт: [8]=сколько включённых
        триггеров прочитано из Flash при старте, [9]=валидность страницы
        конфигурации — диагностика «настройки пропали после питания».
        """
        payload = self.request_control(CMD_TRIGGER_STATS, b"")
        if len(payload) < 8:
            raise RuntimeError("Некорректный ответ CMD_TRIGGER_STATS")
        stats = {
            "fired_count": int.from_bytes(payload[0:4], "little"),
            "max_lateness_ms": int.from_bytes(payload[4:8], "little"),
        }
        if len(payload) >= 10:
            stats["flash_valid_count"] = payload[8]
            stats["config_valid"] = payload[9]
        if len(payload) >= 16:
            # Отправки ответа, исчерпавшие ретраи на занятых TX-ящиках —
            # полевой симптом «ответ триггера то был, то не был».
            stats["dropped_count"] = int.from_bytes(payload[12:16], "little")
        return stats

    def read_usb_stats(self) -> dict[str, int]:
        """Возвращает счётчики USB CDC.

        Базовый ответ — 4 байта (tx_dropped). Расширенные прошивки отдают
        ещё tx_busy_waits (сколько раз IN-эндпоинт ждал хост >100 мс),
        cmd_count (команд реально дошло до обработчика), last_cmd_ms
        (время обработки последней команды на МК) и poll_count (итерации
        главного цикла — замирает при голодании/IRQ-шторме). По дельтам
        между опросами в полевом логе видно, где теряется время. """
        payload = self.request_control(CMD_USB_STATS, b"")
        if len(payload) < 4:
            raise RuntimeError("Некорректный ответ CMD_USB_STATS")
        stats = {"tx_dropped": int.from_bytes(payload[0:4], "little")}
        if len(payload) >= 20:
            stats["tx_busy_waits"] = int.from_bytes(payload[4:8], "little")
            stats["cmd_count"] = int.from_bytes(payload[8:12], "little")
            stats["last_cmd_ms"] = int.from_bytes(payload[12:16], "little")
            stats["poll_count"] = int.from_bytes(payload[16:20], "little")
        return stats

    def read_system_info(self) -> dict[str, object]:
        """Возвращает версию application, протокола и config-формата.

        Расширенные прошивки дополнительно отдают UID96 кристалла,
        дату/время сборки и git commit (payload 56 байт); старые — 16.
        """
        payload = self.request_control(CMD_SYSTEM_INFO, b"")
        if len(payload) < 16:
            raise RuntimeError("Некорректный ответ CMD_SYSTEM_INFO")
        info: dict[str, object] = {
            "application_version": payload[0],
            "protocol_version": payload[1],
            "flash_size_kb": int.from_bytes(payload[2:4], "little"),
            "config_format_version": payload[4],
            "config_record_length": payload[5],
            "trigger_count": payload[6],
            "config_valid": bool(payload[7]),
            "application_size": int.from_bytes(payload[8:12], "little"),
            "application_crc32": int.from_bytes(payload[12:16], "little"),
        }
        if len(payload) >= 28:
            info["mcu_uid"] = payload[16:28].hex().upper()
        if len(payload) >= 48:
            info["build_datetime"] = payload[28:48].decode("ascii", errors="ignore").strip("\x00 ")
        if len(payload) >= 56:
            commit = payload[48:56].split(b"\x00")[0].decode("ascii", errors="ignore")
            if commit:
                info["git_commit"] = commit
        # [56..63] — диагностический хвост прошивки: причина последнего
        # сброса МК (RCC->CSR[31:24]), счётчики USB bus reset/disconnect и
        # потерянные байты RX FIFO. Отвечает на вопрос «почему устройство
        # пропадало» по полевому логу без доступа к железу.
        if len(payload) >= 60:
            info["reset_flags"] = payload[56]
            info["usb_reset_count"] = payload[57]
            info["usb_disconnect_count"] = payload[58]
        if len(payload) >= 64:
            info["rx_overflow_bytes"] = int.from_bytes(payload[60:64], "little")
        return info

    def ping_device(self) -> bool:
        """Отправляет устройству запрос ID и возвращает True, если есть ответ."""
        with self._lock:
            if self._port is None or not self.is_open():
                return False
            # Пока идёт пачка команд настроек — пинг не вклиниваем:
            # устройство занято записями во Flash, ответ может прийти
            # за границей 0.5 с и будет ложно прочитан как «отвал».
            if self._control_session_active:
                return True
            self._closing = True
            self._stop_reader()
            try:
                self._port.reset_input_buffer()
                self._port.write(bytes([CMD_DEVICE_ID]))
                deadline = time.time() + 0.5
                buffer = bytearray()
                while time.time() < deadline:
                    # Реальный read() — см. _control_roundtrip: без
                    # ожидающего чтения usbser.sys может не забирать
                    # ответ с bulk-IN устройства.
                    chunk = self._port.read(256)
                    if chunk:
                        buffer.extend(chunk)
                        if _scan_for_response(buffer, CMD_DEVICE_ID_RESP):
                            return True
                    else:
                        time.sleep(0.01)
                return False
            except Exception:  # noqa: BLE001
                return False
            finally:
                self._start_reader()
                self._closing = False

    def __del__(self) -> None:
        """Гарантирует закрытие порта при удалении менеджера."""
        self._closing = True
        try:
            if self._reader is not None:
                try:
                    self._reader.finished.disconnect(self._on_reader_finished)
                except RuntimeError:
                    pass
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

    def _detect_device_id(self) -> None:
        """Определяет тип, версию, серийный номер и объём памяти устройства."""
        with self._lock:
            if self._port is None:
                return
            self._closing = True
            self._stop_reader()
            try:
                device_type = DEVICE_TYPE_BASIC
                device_version = 0
                device_serial = ""
                memory_kb = 0

                # 1. Запрос типа/версии. Сканируем поток по кадрам:
                # байт-маркер внутри данных CAN-кадра не должен давать
                # ложное срабатывание (шина может быть живая при
                # подключении).
                self._port.reset_input_buffer()
                self._port.write(bytes([CMD_DEVICE_ID]))
                deadline = time.time() + 0.5
                buffer = bytearray()
                while time.time() < deadline:
                    chunk = self._port.read(256)
                    if chunk:
                        buffer.extend(chunk)
                        if _scan_for_response(buffer, CMD_DEVICE_ID_RESP) and len(buffer) >= 3:
                            device_type = buffer[1]
                            device_version = buffer[2]
                            break
                    else:
                        time.sleep(0.01)

                # 2. Запрос серийного номера и объёма памяти
                self._port.reset_input_buffer()
                self._port.write(bytes([CMD_DEVICE_INFO]))
                deadline = time.time() + 0.5
                buffer = bytearray()
                while time.time() < deadline:
                    chunk = self._port.read(256)
                    if chunk:
                        buffer.extend(chunk)
                        if _scan_for_response(buffer, CMD_DEVICE_INFO_RESP) and len(buffer) >= 2:
                            serial_len = buffer[1]
                            expected = 2 + serial_len + 2
                            if len(buffer) >= expected:
                                serial_bytes = bytes(buffer[2 : 2 + serial_len])
                                device_serial = serial_bytes.decode("utf-8", errors="ignore").strip()
                                device_type = buffer[2 + serial_len]
                                memory_kb = buffer[2 + serial_len + 1]
                                break
                    time.sleep(0.01)

                # 3. Имя устройства из страницы конфигурации (CMD_CFG_READ).
                # Ответ: [0xD0, status, len, name_len, name, serial_len,
                # serial, vid_lo, vid_hi, pid_lo, pid_hi]. Windows показывает
                # CDC-порт как «Устройство с последовательным интерфейсом»
                # независимо от iProduct — поэтому имя берём с самого МК и
                # запоминаем в карте серийник→имя для списка портов.
                device_name = ""
                cfg_marker = (CMD_CFG_READ | 0x10) & 0xFF
                # После power cycle/рестарта приложению нужно время на
                # инициализацию до ответа — одна повторная попытка спасает
                # от потери имени устройства при спешке первого опроса.
                for _attempt in range(2):
                    if device_name:
                        break
                    self._port.reset_input_buffer()
                    # Формат нового протокола [cmd][len][payload] — голый
                    # 0xC0 без байта длины вешает парсер МК: он ждёт len,
                    # а потом считает payload_len=0xC0 и поглощает ~190
                    # байт следующих команд (чтения триггеров тонули
                    # в таймаутах и слоты показывались пустыми).
                    self._port.write(bytes((CMD_CFG_READ, 0)))
                    deadline = time.time() + 0.6
                    buffer = bytearray()
                    while time.time() < deadline:
                        chunk = self._port.read(256)
                        if chunk:
                            buffer.extend(chunk)
                            if _scan_for_response(buffer, cfg_marker) and len(buffer) >= 3:
                                status = buffer[1]
                                length = buffer[2]
                                if status != 0:
                                    del buffer[0]
                                    continue
                                if len(buffer) >= 3 + length:
                                    data = bytes(buffer[3 : 3 + length])
                                    device_name, cfg_serial = _parse_cfg_read_payload(data)
                                    if cfg_serial:
                                        device_serial = cfg_serial
                                    break
                        time.sleep(0.01)

                total_memory = memory_kb * 1024 if memory_kb else 65536
                port_names = dict(self._config.get("port_names", {}) or {})
                if device_serial and device_name:
                    port_names[device_serial] = device_name
                if device_name:
                    # USB-серийник, который видит ОС, может отличаться от
                    # серийника в странице конфигурации (Windows кэширует
                    # дескрипторы). Маппим и его — иначе список портов
                    # покажет «устройство с последовательным интерфейсом».
                    try:
                        current = self.current_port_name()
                        for port_info in comports():
                            if port_info.device == current:
                                usb_serial = (port_info.serial_number or "").strip()
                                if usb_serial and usb_serial != device_serial:
                                    port_names[usb_serial] = device_name
                                break
                    except Exception:  # noqa: BLE001
                        pass
                # Имя, заданное при программировании, хранится и в
                # device_type_name: не затираем device_name пустым
                # значением, если вычитка со страницы конфигурации не
                # удалась (старая прошивка без CMD_CFG_READ, таймаут и т.п.).
                if not device_name:
                    device_name = self._config.get("device_name", "") or self._config.get(
                        "device_type_name", ""
                    )
                self._config.set_bulk({
                    "device_type": device_type,
                    "device_version": device_version,
                    "device_serial": device_serial,
                    "serial_number": device_serial,
                    "device_name": device_name,
                    "port_names": port_names,
                    "total_memory": total_memory,
                })
                self.device_identified.emit(device_type, device_version)
                logger.info("Устройство идентифицировано: type=0x%02X version=%d serial=%s mem=%d", device_type, device_version, device_serial or "-", total_memory)
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка определения устройства: %s", exc)
                self._config.set_bulk({"device_type": DEVICE_TYPE_BASIC, "device_version": 0, "device_serial": "", "total_memory": 65536})
                self.device_identified.emit(DEVICE_TYPE_BASIC, 0)
            finally:
                self._start_reader()
                self._closing = False

    def auto_detect_can_speed(self) -> Optional[int]:
        """Останавливает чтение, отправляет 0xA0, ждёт 0xA1 с определённой скоростью.

        Returns:
            Скорость CAN в кбит/с или None.
        """
        with self._lock:
            if self._port is None or not self.is_open():
                return None
            self._closing = True
            self._stop_reader()
            try:
                self._port.reset_input_buffer()
                self._port.write(bytes([CMD_AUTO_SPEED]))
                deadline = time.time() + 3.0
                buffer = bytearray()
                while time.time() < deadline:
                    chunk = self._port.read(256)
                    if chunk:
                        buffer.extend(chunk)
                        if _scan_for_response(buffer, CMD_AUTO_SPEED_RESP) and len(buffer) >= 3:
                            speed = (buffer[1] << 8) | buffer[2]
                            self._config.set("can_speed_auto", True)
                            self.can_speed_detected.emit(speed)
                            logger.info("Скорость CAN определена: %d кбит/с", speed)
                            return speed
                    time.sleep(0.01)
                logger.info("Автоопределение скорости не дало результата")
                return None
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка автоопределения скорости: %s", exc)
                return None
            finally:
                self._start_reader()
                self._closing = False

    def _stop_reader(self) -> None:
        """Останавливает поток чтения и ждёт его завершения."""
        with self._lock:
            if self._reader is not None:
                try:
                    self._reader.finished.disconnect(self._on_reader_finished)
                except RuntimeError:
                    pass
                self._reader.stop()
                tail = self._reader.pending_tail()
                if tail:
                    self._reader_carry = tail
                self._reader = None

    def _start_reader(self) -> None:
        """Создаёт и запускает поток чтения повторно.

        Все сигналы читателя подключаются здесь, чтобы после перезапуска
        (ping_device, _detect_device_id, auto_detect_can_speed) не терялся
        ни один из них — в частности raw_data, на котором работает COM-логгер.
        """
        with self._lock:
            if self._port is None or not self.is_open():
                return
            # Повторный запуск при живом reader'е обязан его остановить:
            # перезапись self._reader осиротевала старый поток — он
            # продолжал read() и крал у команд их ответы (в полевом логе
            # «Поток чтения запущен» дважды подряд без остановки →
            # таймауты команд, пока сирота жил).
            if self._reader is not None:
                try:
                    self._reader.finished.disconnect(self._on_reader_finished)
                except RuntimeError:
                    pass
                self._reader.stop()
                tail = self._reader.pending_tail()
                if tail:
                    self._reader_carry = tail
                self._reader = None
            self._reader = SerialReader(self._port, self)
            if self._reader_carry:
                self._reader.seed_buffer(self._reader_carry)
                self._reader_carry = b""
            self._reader.new_frame.connect(self.new_can_frame)
            self._reader.new_raw_data.connect(self.raw_data)
            self._reader.error.connect(self.error_occurred)
            self._reader.heartbeat.connect(self.heartbeat)
            self._reader.finished.connect(self._on_reader_finished)
            self._reader.start()

    def _on_reader_finished(self) -> None:
        """Вызывается при завершении потока чтения; планирует переподключение."""
        with self._lock:
            reader = self.sender()
            if reader is None or reader is not self._reader:
                return
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
        if self._shutdown or not self._auto_reconnect or self.is_open():
            return
        if self._reconnect_timer is not None and self._reconnect_timer.isActive():
            return
        # Экспоненциальный бэкоф: 3с → 6 → 12 → 24 → 48 → 60с потолок.
        # Без него занятый порт (PermissionError «Отказано в доступе»)
        # долбился попытками каждые 3 секунды бесконечно — шторм в логе
        # и лишняя нагрузка на GUI-поток.
        delay_ms = min(3000 << self._reconnect_attempts, 60000)
        self._reconnect_attempts += 1
        logger.info(
            "Планируется автоматическое переподключение к %s через %d с (попытка %d)",
            self._last_port_name, delay_ms // 1000, self._reconnect_attempts,
        )
        self.reconnect_scheduled.emit(delay_ms // 1000, self._reconnect_attempts)
        self._reconnect_timer = QTimer(self)
        self._reconnect_timer.setSingleShot(True)
        self._reconnect_timer.timeout.connect(self._do_reconnect)
        self._reconnect_timer.start(delay_ms)

    def _stop_reconnect_timer(self) -> None:
        """Останавливает таймер переподключения."""
        if self._reconnect_timer is not None and self._reconnect_timer.isActive():
            self._reconnect_timer.stop()
            self._reconnect_timer = None

    def _find_app_port_by_usb(self) -> Optional[str]:
        """Ищет наш адаптер среди портов по USB VID/PID приложения.

        Нужно, когда устройство пере-энумеровалось на другом COM — Windows
        может выдать новый номер, пока старый ещё держит мёртвый handle;
        тогда перебор по имени порта бесконечно мимо (FileNotFoundError).
        """
        for p in comports():
            if p.vid == USB_VID_CODEMASTER and p.pid == USB_PID_APPLICATION:
                return p.device
        return None

    def _do_reconnect(self) -> None:
        """Пытается восстановить соединение с COM-портом."""
        if self._shutdown or self.is_open():
            return
        port_name = self._last_port_name
        if port_name and port_name not in {p.device for p in comports()}:
            alt = self._find_app_port_by_usb()
            if alt:
                logger.info("Устройство пере-энумеровано на другой порт: %s -> %s", port_name, alt)
                port_name = alt
        logger.info("Попытка автоматического переподключения к %s", port_name)
        if self.open_port(port_name, self._last_baudrate, self._last_emulation, self._auto_reconnect):
            logger.info("Автоматическое переподключение к %s успешно", port_name)
        else:
            logger.warning("Автоматическое переподключение к %s не удалось, будет повторная попытка", port_name)
