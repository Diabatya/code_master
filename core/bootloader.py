"""Реализация STM32 UART bootloader по протоколу AN3155.

Класс Bootloader работает с уже открытым pyserial.Serial-портом.
Все операции выполняются в синхронном режиме и должны вызываться
из фонового потока, чтобы не блокировать интерфейс.
"""

import contextlib
import time
from typing import Any
from collections.abc import Callable

import serial

try:
    from serial.tools.list_ports import comports
except Exception:  # noqa: BLE001
    def comports() -> list:
        return []

from core.firmware_utils import (
    guess_firmware_base,
    load_firmware_bytes,
    trim_to_application_region,
    validate_write_region,
)
from core.stm32_info import (
    APPLICATION_BASE_ADDR,
    APP_METADATA_PAGE_ADDR,
    APP_METADATA_PAGE_SIZE,
    BOOTLOADER_BASE_ADDR,
    DEVICE_CONFIG_PAGE_ADDR_LEGACY,
    DEVICE_CONFIG_PAGE_SIZE,
    DEVICE_INFO_PAGE_ADDR,
    build_app_metadata,
    merge_device_config_page,
)
from models.logger import get_logger

logger = get_logger(__name__)

ACK = 0x79
NACK = 0x1F


class BootloaderError(Exception):
    """Ошибка на этапе работы с бутлоадером STM32."""


class Bootloader:
    """Класс для записи прошивки в STM32 через UART bootloader."""

    BLOCK_SIZE = 256
    MAX_RETRIES = 3

    # USB VID/PID для собственных CDC-устройств
    USB_VID = 0x0483
    USB_BOOTLOADER_PID = 0x5741
    USB_APPLICATION_PID = 0x5740

    # Команда перезагрузки из приложения в bootloader (принимается прошивкой приложения)
    REBOOT_TO_BOOTLOADER_MAGIC = b"\x00REBOOT_TO_BOOTLOADER\n"

    def __init__(self, port: serial.Serial, progress_callback: Callable[[int], None] | None = None) -> None:
        """Создаёт объект бутлоадера.

        Args:
            port: Открытый объект serial.Serial, настроенный для bootloader.
            progress_callback: Функция, принимающая процент (0–100).
        """
        self.port = port
        self._progress_callback = progress_callback
        self._stop_requested = False

    # Windows держит «Cannot configure port / PermissionError(13,
    # ERROR_GEN_FAILURE)» заметно дольше пары секунд после
    # пере-энумерации CDC-устройства — окно повторов ~30 с
    # (повторный отчёт мастера: обновление через CDC падало с этой
    # ошибкой даже при 10-секундном окне).
    OPEN_RETRIES = 120
    OPEN_RETRY_DELAY = 0.25

    @classmethod
    def open(
        cls,
        port_name: str,
        baudrate: int = 115200,
        timeout: float = 1.0,
        progress_callback: Callable[[int], None] | None = None,
    ) -> "Bootloader":
        """Открывает COM-порт с параметрами bootloader-протокола AN3155
        (8 бит, чётность Even, 1 стоп-бит) и возвращает готовый Bootloader.

        Единая точка открытия порта для UART/USB CDC-прошивки — используется
        и GUI (``ui/flash_dialog.py``: ``ConnectWorker``/``FlashWorker``/
        ``ReadWorker``), и CLI (``main.py``), чтобы serial.Serial(...) с
        одинаковыми параметрами не дублировался в нескольких местах и чтобы
        таймауты/обработка ошибок открытия порта не расходились между путями.

        На Windows порт USB CDC может числиться в ``comports()``, но ещё
        недолго быть неготовым сразу после reset/пере-энумерации устройства —
        ``SetCommState`` в этом окне падает с ERROR_GEN_FAILURE (31,
        «устройство не работает») или ERROR_ACCESS_DENIED. Поэтому открытие
        повторяется с короткой паузой.

        Raises:
            serial.SerialException: если порт не удалось открыть.
        """
        last_exc: Exception | None = None
        for attempt in range(cls.OPEN_RETRIES):
            try:
                port = serial.Serial(
                    port_name,
                    baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_EVEN,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=timeout,
                )
                return cls(port, progress_callback=progress_callback)
            except (serial.SerialException, OSError) as exc:
                last_exc = exc
                logger.debug(
                    "Открытие порта %s: попытка %d/%d неудачна: %s",
                    port_name, attempt + 1, cls.OPEN_RETRIES, exc,
                )
                time.sleep(cls.OPEN_RETRY_DELAY)
        assert last_exc is not None
        raise last_exc

    def request_stop(self) -> None:
        """Запрашивает остановку текущей операции прошивки."""
        self._stop_requested = True

    def reconfigure_for_bootloader(self) -> None:
        """Переключает порт на параметры, требуемые bootloader STM32.

        Согласно AN3155: чётность Even, 1 стоп-бит, 8 бит данных.
        """
        try:
            if self.port.is_open:
                self.port.close()
            self.port.parity = serial.PARITY_EVEN
            self.port.stopbits = serial.STOPBITS_ONE
            self.port.bytesize = serial.EIGHTBITS
            self.port.baudrate = 115200
            self._open_port_with_retry()
            logger.info("Порт перенастроен для bootloader: Even, 1 стоп-бит")
        except Exception as exc:  # noqa: BLE001
            # Раньше здесь был только warning: порт оставался закрытым, и
            # следующая команда падала с невнятной SerialException.
            logger.error("Не удалось перенастроить порт для bootloader: %s", exc)
            raise BootloaderError(
                f"Не удалось открыть порт {getattr(self.port, 'port', '?')} для бутлоадера: {exc}"
            ) from exc

    def _open_port_with_retry(self) -> None:
        """Открывает уже настроенный ``self.port`` с повторами.

        Та же причина, что и в ``open()``: на Windows после пере-энумерации
        USB CDC устройства CreateFile/SetCommState временно возвращает
        ERROR_GEN_FAILURE (31)/ERROR_ACCESS_DENIED, хотя порт уже виден в
        ``comports()``.
        """
        last_exc: Exception | None = None
        for attempt in range(self.OPEN_RETRIES):
            try:
                self.port.open()
                return
            except (serial.SerialException, OSError) as exc:
                last_exc = exc
                logger.debug(
                    "Открытие порта %s: попытка %d/%d неудачна: %s",
                    getattr(self.port, "port", "?"), attempt + 1, self.OPEN_RETRIES, exc,
                )
                time.sleep(self.OPEN_RETRY_DELAY)
        assert last_exc is not None
        raise last_exc

    def _read_byte(self, timeout: float = 1.0) -> int:
        """Считывает один байт из порта с таймаутом.

        Args:
            timeout: Время ожидания в секундах.

        Returns:
            Значение байта.

        Raises:
            BootloaderError: если таймаут или нет данных.
        """
        previous_timeout = self.port.timeout
        self.port.timeout = timeout
        try:
            byte = self.port.read(1)
        finally:
            self.port.timeout = previous_timeout
        if not byte:
            raise BootloaderError("Таймаут ожидания ответа от бутлоадера")
        return byte[0]

    def _send_command(self, command: int, wait_ack: bool = True) -> None:
        """Отправляет команду и её инверсию, ожидает ACK.

        Args:
            command: Байт команды.
            wait_ack: Если True, ждёт ответа 0x79.

        Raises:
            BootloaderError: при получении NACK или таймауте.
        """
        logger.debug("BL TX: команда 0x%02X", command)
        self.port.write(bytes([command, command ^ 0xFF]))
        if wait_ack:
            response = self._read_byte()
            logger.debug("BL RX: команда 0x%02X -> ответ 0x%02X", command, response)
            if response != ACK:
                raise BootloaderError(f"Команда 0x{command:02X} не подтверждена (ответ 0x{response:02X})")

    @classmethod
    def find_device_port(cls, vid: int = 0, pid: int = 0, timeout: float = 0.0) -> str | None:
        """Ищет COM-порт устройства по VID/PID.

        Args:
            vid: USB Vendor ID (0 — любой).
            pid: USB Product ID (0 — любой).
            timeout: Время ожидания появления порта, секунд (0 — без ожидания).

        Returns:
            Имя COM-порта или None.
        """
        start = time.time()
        while True:
            for p in comports():
                logger.debug("find_device_port: проверка %s vid=0x%04X pid=0x%04X", p.device, p.vid or 0, p.pid or 0)
                if vid and p.vid != vid:
                    continue
                if pid and p.pid != pid:
                    continue
                if p.vid is None and p.pid is None:
                    continue
                logger.info("Найден порт %s (VID=0x%04X, PID=0x%04X)", p.device, p.vid or 0, p.pid or 0)
                return p.device
            if timeout <= 0 or time.time() - start >= timeout:
                logger.warning("Порт VID=0x%04X PID=0x%04X не найден (timeout=%.1f)", vid, pid, timeout)
                return None
            time.sleep(0.05)

    def _port_info(self) -> Any | None:
        """Возвращает информацию о текущем COM-порте."""
        for p in comports():
            if p.device == self.port.port:
                logger.info("Информация о порте: %s (VID=0x%04X PID=0x%04X)", p.device, p.vid or 0, p.pid or 0)
                return p
        logger.warning("Информация о порте %s не найдена", self.port.port)
        return None

    def request_app_reboot(self) -> None:
        """Отправляет запущенному приложению команду программной перезагрузки
        в режим bootloader.

        Прошивка приложения должна распознать REBOOT_TO_BOOTLOADER_MAGIC,
        записать флаг 0xDEADBEEF по адресу 0x20004FF0 и выполнить NVIC_SystemReset().
        """
        magic = self.REBOOT_TO_BOOTLOADER_MAGIC
        # Порт может умереть прямо между открытием и записью (устройство
        # уже перезагружается/отключено): reset_output_buffer/write на
        # мёртвом хендле дают PermissionError(13) — в этом случае просто
        # идём ждать bootloader-порт, устройство уже ушло на перезагрузку
        # (отчёт мастера: «Cannot configure port … устройство не
        # работает» при обновлении по CDC).
        try:
            self.port.reset_output_buffer()
            self.port.write(magic)
            self.port.flush()
        except (serial.SerialException, OSError) as exc:
            logger.warning(
                "Команда перезагрузки не ушла (%s) — порт уже недоступен, "
                "жду появления bootloader-устройства",
                exc,
            )
            return
        logger.info("Команда перезагрузки в bootloader отправлена (%d байт)", len(magic))

    def wait_for_bootloader_port(self, timeout: float = 20.0) -> None:
        """Ждёт появления bootloader-порта (VID=0483, PID=5741) после reset."""
        logger.info("Ожидание появления bootloader-порта...")
        start = time.time()
        while time.time() - start < timeout:
            for p in comports():
                if p.vid == self.USB_VID and p.pid == self.USB_BOOTLOADER_PID:
                    logger.info("Bootloader-порт найден: %s", p.device)
                    with contextlib.suppress(Exception):
                        self.port.close()
                    self.port.port = p.device
                    try:
                        self._open_port_with_retry()
                    except (serial.SerialException, OSError) as exc:
                        # Порт появился в списке, но устройство ещё
                        # конфигурируется — продолжаем ждать.
                        logger.debug("Порт %s пока не открывается: %s", p.device, exc)
                    else:
                        return
            time.sleep(0.05)
        raise BootloaderError("Bootloader-порт не появился после перезагрузки")

    def reboot_to_bootloader(self, timeout: float = 10.0) -> None:
        """Программно перезагружает устройство в режим bootloader."""
        self.request_app_reboot()
        # wait_for_bootloader_port() уже опрашивает порты в цикле каждые 0.2с
        # и ищет именно bootloader PID, так что отдельная фиксированная пауза
        # перед началом опроса не нужна — она просто добавляла 0.5с к каждому
        # входу в bootloader, не влияя на результат.
        self.wait_for_bootloader_port(timeout)

    def enter_bootloader(self) -> None:
        """Переводит STM32 в режим bootloader.

        Для USB CDC сначала пытается программно перезагрузить запущенное
        приложение в bootloader. Если устройство уже bootloader или используется
        UART-адаптер, применяется управление DTR/RTS.
        """
        info = self._port_info()
        if info is not None and info.vid == self.USB_VID:
            if info.pid == self.USB_BOOTLOADER_PID:
                logger.info("Порт уже в режиме bootloader (%s)", info.device)
                return
            if info.pid == self.USB_APPLICATION_PID:
                logger.info("Обнаружено приложение (%s), перезагружаю в bootloader", info.device)
                self.reboot_to_bootloader()
                return

        # Fallback: классическое управление BOOT0/RESET через DTR/RTS
        logger.info("Перевод STM32 в режим bootloader через DTR/RTS")
        try:
            self.port.setDTR(False)
            self.port.setRTS(True)
            time.sleep(0.1)
            self.port.setRTS(False)
            time.sleep(0.5)
            self.port.setRTS(True)
            time.sleep(0.2)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Управление DTR/RTS не поддерживается: %s", exc)

    def sync(self, retries: int = 3) -> None:
        """Выполняет синхронизацию с бутлоадером командой 0x7F.

        Args:
            retries: Количество попыток при получении NACK.

        Raises:
            BootloaderError: если синхронизация не удалась.
        """
        for attempt in range(retries):
            logger.info("Попытка синхронизации с бутлоадером %d/%d", attempt + 1, retries)
            self.port.reset_input_buffer()
            self.port.write(bytes([0x7F]))
            try:
                response = self._read_byte(1.0)
                if response == ACK:
                    logger.info("Бутлоадер ответил ACK")
                    return
                if response == NACK:
                    logger.warning("Бутлоадер ответил NACK")
                    time.sleep(0.1)
                    continue
            except BootloaderError:
                time.sleep(0.2)
        raise BootloaderError("Не удалось синхронизироваться с бутлоадером")

    def _wait_erase_ack(self, timeout: float) -> None:
        """Ждёт ACK на команду стирания, переживая отвал USB CDC.

        Во время массового стирания STM32F1 глушит USB на несколько
        секунд — Windows отвечает ERROR_GEN_FAILURE и порт уходит в
        пере-энумерацию. Само стирание в МК при этом продолжается
        автономно. Ловим падение порта, ждём возвращения устройства,
        открываем порт заново и синхронизируемся — повторный 0x7F
        безопасен, а стирание к этому моменту уже завершилось.
        """
        try:
            response = self._read_byte(timeout)
        except (serial.SerialException, OSError) as exc:
            logger.warning(
                "Порт отвалился во время стирания (%s) — жду пере-энумерацию устройства",
                exc,
            )
            self._recover_port()
            logger.info("Связь восстановлена после стирания")
            return
        if response != ACK:
            raise BootloaderError(f"Ошибка стирания (ответ 0x{response:02X})")

    def _recover_port(self) -> None:
        """Восстанавливает связь с бутлоадером после отвала USB CDC.

        STM32F1 глушит USB на время длинных flash-операций, Windows
        гоняет устройство через пере-энумерацию — открытый хендл порта
        умирает с PermissionError(13)/ERROR_GEN_FAILURE посередине
        команды. Закрываем труп, ждём возврата bootloader-порта (он
        может появиться под ДРУГИМ именем COM) и синхронизируемся
        заново. Для UART-адаптеров (порт не наш) — просто переоткрываем
        тот же порт.
        """
        with contextlib.suppress(Exception):
            self.port.close()
        info = self._port_info()
        if info is None or info.vid == self.USB_VID:
            self.wait_for_bootloader_port(timeout=15.0)
        else:
            self._open_port_with_retry()
        # Порт мог умереть посередине команды — бутлоадер ждёт остаток
        # кадра (адрес: 5 байт, данные: до 258). Выливаем запас 0x7F:
        # они добьют любой недописанный кадр (контрольная сумма не
        # сойдётся → NACK → бутлоадер вернётся в ожидание команды),
        # а попавшие в command-режим 0x7F дадут NACK-пары.
        with contextlib.suppress(Exception):
            self.port.write(bytes([0x7F]) * 300)
            time.sleep(0.05)
        # Запасные 0x7F могли породить пачку NACK (0x1F) в ответ — они
        # доезжают по CDC с задержкой и попадали в ответы следующих
        # команд как фантомные отказы (отчёт мастера). Дочитываем
        # до тишины перед синхронизацией.
        with contextlib.suppress(Exception):
            prev_timeout = self.port.timeout
            try:
                self.port.timeout = 0.05
                for _ in range(8):
                    if not self.port.read(256):
                        break
                    time.sleep(0.02)
            finally:
                self.port.timeout = prev_timeout
        self.sync(retries=5)
        logger.info("Связь с бутлоадером восстановлена")

    def erase(self, extended: bool = True) -> None:
        """Выполняет массовое стирание памяти (осторожно — сносит bootloader!)."""
        logger.warning("Выполняется массовое стирание памяти STM32 (bootloader будет стёрт)")
        try:
            if extended:
                self._send_command(0x44)
                self.port.write(bytes([0xFF, 0xFF, 0x00]))
            else:
                self._send_command(0x43)
                self.port.write(bytes([0xFF, 0x00]))
        except (serial.SerialException, OSError):
            # Порт умер на отправке команды — команда могла не уйти;
            # восстанавливаем связь и посылаем стирание повторно
            # (повторное стирание тех же страниц безвредно).
            logger.warning("Порт отвалился на команде стирания — восстанавливаю связь")
            self._recover_port()
            if extended:
                self._send_command(0x44)
                self.port.write(bytes([0xFF, 0xFF, 0x00]))
            else:
                self._send_command(0x43)
                self.port.write(bytes([0xFF, 0x00]))

        self._wait_erase_ack(5.0)
        logger.info("Массовое стирание завершено")

    def erase_pages(
        self,
        start: int,
        data: bytes,
        page_size: int = 2048,
        flash_base: int = BOOTLOADER_BASE_ADDR,
        skip_blank: bool = True,
    ) -> None:
        """Стирает страницы, которые будут перезаписаны.

        Args:
            start: Начальный адрес записи.
            data: Данные для записи (для определения пустых страниц).
            page_size: Размер страницы flash.
            flash_base: Базовый адрес flash.
            skip_blank: Не стирать страницы, для которых записываемые данные
                полностью состоят из 0xFF (по аналогии с core/dfu.py). При
                False стираются все страницы диапазона, даже пустые —
                это увеличивает число циклов erase, но иногда нужно
                (например, чтобы гарантированно снести старые данные).
        """
        if not data:
            return
        end = start + len(data)
        page = (start // page_size) * page_size
        pages_to_erase: list[int] = []
        while page < end:
            seg_start = max(start, page)
            seg_end = min(end, page + page_size)
            page_data = data[seg_start - start : seg_end - start]
            if not page_data:
                page += page_size
                continue
            if skip_blank and all(b == 0xFF for b in page_data):
                logger.debug("Пропуск пустой страницы 0x%08X", page)
            else:
                pages_to_erase.append((page - flash_base) // page_size)
            page += page_size

        if not pages_to_erase:
            logger.info("Нет страниц для стирания (все данные 0xFF)")
            return

        logger.info("Стирание %d страниц: %s", len(pages_to_erase), pages_to_erase[:10])
        if len(pages_to_erase) > 10:
            logger.info("... и ещё %d страниц", len(pages_to_erase) - 10)

        n = len(pages_to_erase) - 1
        payload = bytearray()
        payload.append((n >> 8) & 0xFF)
        payload.append(n & 0xFF)
        for p in pages_to_erase:
            payload.append((p >> 8) & 0xFF)
            payload.append(p & 0xFF)
        checksum = 0
        for b in payload:
            checksum ^= b
        payload.append(checksum)

        try:
            self._send_command(0x44)
            self.port.write(bytes(payload))
        except (serial.SerialException, OSError):
            logger.warning("Порт отвалился на команде стирания страниц — восстанавливаю связь")
            self._recover_port()
            self._send_command(0x44)
            self.port.write(bytes(payload))
        self._wait_erase_ack(30.0)
        logger.info("Стирание страниц завершено")

    def write_memory(self, address: int, data: bytes) -> None:
        """Записывает блок данных по указанному адресу.

        Args:
            address: 32-битный адрес в памяти (little-endian).
            data: Блок данных, длиной до 256 байт.

        Raises:
            BootloaderError: при ошибке записи.
        """
        length = len(data)
        if length > self.BLOCK_SIZE:
            raise BootloaderError(f"Блок данных слишком большой: {length} байт")

        logger.debug("BL write_memory: адрес 0x%08X, %d байт", address, length)
        # Формируем команду Write Memory 0x31
        self._send_command(0x31)

        # Адрес + контрольная сумма адреса — один write() вместо двух: тот же
        # байтовый поток на проводе, но на один системный вызов/USB-пакет
        # меньше на каждый из ~1000+ блоков полного образа (заметно на
        # write()-с-задержкой драйверах, особенно на Windows/USB CDC).
        addr_bytes = address.to_bytes(4, "big")
        addr_checksum = 0
        for b in addr_bytes:
            addr_checksum ^= b
        self.port.write(addr_bytes + bytes([addr_checksum]))

        response = self._read_byte()
        if response != ACK:
            raise BootloaderError(f"Адрес не подтверждён (ответ 0x{response:02X})")

        # Данные: N-1, затем байты, затем XOR — тоже одним write().
        n = length - 1
        checksum = n
        for b in data:
            checksum ^= b
        self.port.write(bytes([n]) + data + bytes([checksum]))

        response = self._read_byte(5.0)
        if response != ACK:
            raise BootloaderError(f"Ошибка записи блока (ответ 0x{response:02X})")

    def go(self, address: int = APPLICATION_BASE_ADDR) -> None:
        """Отправляет команду Go (0x21) — запускает application из bootloader.

        Устройство сразу пере-энumerируется как application (PID 5740) —
        не нужно передёргивать питание после прошивки. Если образ
        невалиден, bootloader молча остаётся в режиме прошивки
        (bl_app_is_valid), что само по себе сигнал о неудачной записи.
        """
        self._send_command(0x21)
        addr_bytes = address.to_bytes(4, "big")
        checksum = 0
        for b in addr_bytes:
            checksum ^= b
        self.port.write(addr_bytes + bytes([checksum]))
        response = self._read_byte()
        if response != ACK:
            raise BootloaderError(f"Команда запуска не подтверждена (ответ 0x{response:02X})")
        logger.info("GO 0x%08X: запуск application", address)

    def verify(self, address: int, data: bytes, skip_blank: bool = True) -> bool:
        """Сравнивает данные в памяти STM32 с ожидаемыми.

        При skip_blank блоки, целиком состоящие из 0xFF, не проверяются:
        такие страницы разреженного образа не стирались и не
        записывались — там может лежать прежнее содержимое, и строгое
        сравнение давало бы ложное «верификация не прошла».

        Returns:
            True, если данные совпадают, иначе False.
        """
        logger.info("BL verify: адрес 0x%08X, %d байт", address, len(data))
        for offset in range(0, len(data), self.BLOCK_SIZE):
            block = data[offset : offset + self.BLOCK_SIZE]
            if skip_blank and all(b == 0xFF for b in block):
                continue
            try:
                read_back = self.read_memory(address + offset, len(block))
            except (serial.SerialException, OSError) as exc:
                # Отвал USB CDC и на верификации — восстанавливаем связь
                # и читаем тот же блок повторно.
                logger.warning(
                    "Порт отвалился при верификации 0x%08X (%s) — восстанавливаю связь",
                    address + offset, exc,
                )
                self._recover_port()
                read_back = self.read_memory(address + offset, len(block))
            if read_back != block:
                logger.warning("BL verify: mismatch at 0x%08X", address + offset)
                return False
        logger.info("BL verify: OK")
        return True

    def _read_memory_chunk(self, address: int, length: int) -> bytes:
        """Читает один блок памяти длиной до 256 байт."""
        if length < 1 or length > self.BLOCK_SIZE:
            raise BootloaderError(f"Недопустимый размер блока чтения: {length}")

        logger.debug("BL read chunk: 0x%08X, %d байт", address, length)
        self._send_command(0x11)
        addr_bytes = address.to_bytes(4, "big")
        addr_checksum = 0
        for b in addr_bytes:
            addr_checksum ^= b
        self.port.write(addr_bytes + bytes([addr_checksum]))
        if self._read_byte() != ACK:
            raise BootloaderError(f"Адрес чтения 0x{address:08X} не подтверждён")

        self.port.write(bytes([length - 1]))
        if self._read_byte() != ACK:
            raise BootloaderError("Команда чтения не подтверждена")

        self.port.timeout = 2.0 + length / 1000
        read_data = self.port.read(length)
        if len(read_data) < length:
            raise BootloaderError(f"Прочитано {len(read_data)} из {length} байт")
        return read_data

    def read_memory(self, address: int, length: int) -> bytes:
        """Читает length байт из памяти STM32 по команде 0x11.

        Args:
            address: Начальный адрес.
            length: Количество байт для чтения.

        Returns:
            Прочитанные байты.

        Raises:
            BootloaderError: при ошибке чтения.
        """
        logger.info("BL read_memory: 0x%08X, %d байт", address, length)
        result = bytearray()
        offset = 0
        while offset < length:
            chunk_len = min(self.BLOCK_SIZE, length - offset)
            result.extend(self._read_memory_chunk(address + offset, chunk_len))
            offset += chunk_len
        logger.info("BL read_memory завершено: 0x%08X, прочитано %d байт", address, len(result))
        return bytes(result)

    def get_version(self) -> int:
        """Возвращает версию бутлоадера (команда 0x01)."""
        self._send_command(0x01)
        version = self._read_byte()
        # После версии идут разрешенные команды, заканчивающиеся ACK
        while True:
            byte = self._read_byte()
            if byte == ACK:
                break
        logger.info("BL version: 0x%02X", version)
        return version

    def get_id(self) -> int:
        """Возвращает идентификатор устройства (команда 0x02)."""
        self._send_command(0x02)
        length = self._read_byte()
        device_id = 0
        for _ in range(length + 1):
            device_id = (device_id << 8) | self._read_byte()
        logger.info("BL device ID: 0x%08X", device_id)
        return device_id

    def diagnostics(self) -> dict[str, int]:
        """Выполняет синхронизацию и возвращает версию и ID устройства.

        Returns:
            Словарь с ключами 'version' и 'device_id'.
        """
        logger.info("BL diagnostics: старт")
        self.reconfigure_for_bootloader()
        self.enter_bootloader()
        self.sync()
        version = self.get_version()
        device_id = self.get_id()
        logger.info("BL diagnostics: версия=0x%02X, ID=0x%08X", version, device_id)
        return {"version": version, "device_id": device_id}

    def _write_region(
        self, address: int, data: bytes, written: int, total: int,
        skip_blank: bool = True,
    ) -> int:
        """Пишет data по адресу блоками BLOCK_SIZE с повторами и прогрессом.

        При skip_blank блоки, целиком состоящие из 0xFF, не отправляются:
        запись 0xFF в NOR-flash в любом случае ничего не меняет (ячейки
        умеют только сбрасывать биты 1→0), а в объединённом образе между
        application и страницей метаданных — десятки КБ «пустого» padding,
        пропуск которого заметно ускоряет прошивку.

        Returns:
            Обновлённое число уже записанных байт (для прогресса).
        """
        for offset in range(0, len(data), self.BLOCK_SIZE):
            if self._stop_requested:
                raise BootloaderError("Операция отменена")
            block = data[offset : offset + self.BLOCK_SIZE]
            block_addr = address + offset
            if skip_blank and all(b == 0xFF for b in block):
                written += len(block)
                if self._progress_callback:
                    self._progress_callback(min(100, int(written / total * 100)))
                continue
            for attempt in range(self.MAX_RETRIES):
                try:
                    self.write_memory(block_addr, block)
                    logger.debug("Записан блок 0x%08X, %d байт", block_addr, len(block))
                    break
                except BootloaderError as exc:
                    logger.warning("Повтор записи блока 0x%08X: %s", block_addr, exc)
                    # NACK (0x1F) после восстановления связи: первый
                    # проход мог записать блок полностью, а ACK умер
                    # вместе с портом. На STM32F1 повторное
                    # программирование тех же полуслов без стирания —
                    # PGERR → NACK. Если блок уже лежит во flash как
                    # надо — считаем записанным (отчёт мастера:
                    # «Ошибка записи блока 0x… (ответ 0x1F)»).
                    try:
                        if self.read_memory(block_addr, len(block)) == block:
                            logger.info(
                                "Блок 0x%08X уже записан корректно — принят по read-back",
                                block_addr,
                            )
                            break
                    except (serial.SerialException, OSError, BootloaderError):
                        # Чтение тоже упало — ниже стандартный путь
                        # восстановления порта через retry.
                        pass
                    if attempt == self.MAX_RETRIES - 1:
                        raise BootloaderError(
                            f"Ошибка записи блока 0x{block_addr:08X} ({len(block)} байт): {exc}"
                        ) from exc
                    time.sleep(0.1)
                except (serial.SerialException, OSError) as exc:
                    # Порт умер посередине записи (пере-энумерация USB CDC
                    # на Windows — PermissionError(13)/ERROR_GEN_FAILURE).
                    # Ждём возврата bootloader-порта, синхронизируемся и
                    # пишем ЭТОТ ЖЕ блок заново — запись идемпотентна,
                    # страница уже стёрта (отчёт мастера).
                    logger.warning(
                        "Порт отвалился при записи блока 0x%08X (%s) — восстанавливаю связь",
                        block_addr, exc,
                    )
                    if attempt == self.MAX_RETRIES - 1:
                        raise BootloaderError(
                            f"Ошибка записи блока 0x{block_addr:08X}: порт недоступен ({exc})"
                        ) from exc
                    try:
                        self._recover_port()
                    except Exception as rec_exc:  # noqa: BLE001
                        if attempt == self.MAX_RETRIES - 2:
                            raise BootloaderError(
                                f"Блок 0x{block_addr:08X}: связь не восстановлена ({rec_exc})"
                            ) from rec_exc
                        logger.warning("Восстановление порта не удалось: %s", rec_exc)
            written += len(block)
            if self._progress_callback:
                self._progress_callback(min(100, int(written / total * 100)))
        return written

    def flash_firmware(
        self,
        firmware_path: str,
        base_address: int = APPLICATION_BASE_ADDR,
        page_size: int = 2048,
        skip_blank: bool = True,
        status_callback: Callable[[str], None] | None = None,
    ) -> list[tuple[int, bytes]]:
        """Записывает файл прошивки в память STM32.

        Поддерживает .bin, .hex (Intel HEX) и .elf.
        Возвращает список фактически записанных регионов (адрес, данные) —
        для верификации: config-страница могла быть объединена с
        существующей, а метаданные — синтезированы на ПК, поэтому
        сравнивать с исходным образом нельзя.

        Порядок обновления application (base_address == APPLICATION_BASE_ADDR):
          1. инвалидация метаданных (запись 4 нулевых байт) — при обрыве
             устройство остаётся в bootloader, а не запускает обрывок кода;
          2. стирание и запись кода;
          3. запись метаданных ПОСЛЕДНИМИ — валидный magic+CRC служит
             «флагом завершённого обновления» (bl_metadata_is_valid).
        Если образ не содержит страницу метаданных, запись генерируется
        на стороне ПК (build_app_metadata) и дописывается тем же способом.

        Args:
            firmware_path: Путь к файлу прошивки.
            base_address: Начальный адрес записи (по умолчанию APPLICATION_BASE_ADDR).
            page_size: Размер страницы flash (для F105 — 2048 байт).
            skip_blank: Не стирать страницы, для которых записываемые данные
                полностью 0xFF (см. erase_pages()).
            status_callback: Функция для текстового статуса этапа
                (подключение/стирание/запись/метаданные).

        Raises:
            BootloaderError: при ошибке прошивки или запрещённой области.
        """
        firmware, file_base = load_firmware_bytes(firmware_path)
        if not firmware:
            raise BootloaderError("Файл прошивки пуст")
        if file_base:
            base_address = file_base
        elif base_address == APPLICATION_BASE_ADDR:
            # .bin без адреса: если образ начинается с векторов
            # bootloader'а — это полный образ с 0x08000000, а не
            # application. Иначе запись со смещением +0x8000 убила бы
            # таблицу векторов приложения.
            guessed = guess_firmware_base(firmware)
            if guessed != base_address:
                logger.info(
                    "BIN без адреса: по reset-вектору база=0x%08X, а не 0x%08X",
                    guessed, base_address,
                )
                base_address = guessed

        if base_address < DEVICE_INFO_PAGE_ADDR:
            # Объединённый образ (bootloader + application): область
            # bootloader через AN3155 незаписываема — работающий
            # загрузчик не может перезаписать сам себя. Отрезаем её и
            # пишем только info-страницу + metadata + application.
            firmware, base_address = trim_to_application_region(firmware, base_address)
            if not firmware:
                raise BootloaderError(
                    "Образ не содержит записываемой области (0x08008000+) — "
                    "через bootloader записать нечего"
                )
            if status_callback:
                status_callback(
                    "Область bootloader пропущена (AN3155 пишет только 0x08008000+)"
                )
            logger.info("Обрезана область bootloader: base=0x%08X, %d байт", base_address, len(firmware))

        # Разреженный образ по новой карте памяти разбивается на
        # служебные сегменты: страница идентификации устройства
        # (0x08008000 — тип/s/n/версия), метаданные целостности APP1
        # (0x08008800) и код приложения (0x08009000+).
        info_seg: bytes | None = None
        meta_seg: bytes | None = None
        code = firmware
        code_addr = base_address
        if base_address == DEVICE_CONFIG_PAGE_ADDR_LEGACY:
            # Образ config-страницы старой раскладки (0x0803D800):
            # переносим на новую страницу идентификации.
            info_seg = firmware[:DEVICE_CONFIG_PAGE_SIZE]
            code = b""
        else:
            info_rel = DEVICE_INFO_PAGE_ADDR - base_address
            if 0 <= info_rel < len(firmware):
                info_seg = firmware[info_rel : info_rel + DEVICE_CONFIG_PAGE_SIZE]
            meta_rel = APP_METADATA_PAGE_ADDR - base_address
            if 0 <= meta_rel < len(firmware):
                meta_seg = firmware[meta_rel : meta_rel + APP_METADATA_PAGE_SIZE]
            if base_address < APPLICATION_BASE_ADDR:
                cut = APPLICATION_BASE_ADDR - base_address
                code_addr = APPLICATION_BASE_ADDR
                code = firmware[cut:]

        if code:
            ok, reason = validate_write_region(code_addr, len(code))
            if not ok:
                raise BootloaderError(f"Запрещённая область записи: {reason}")

        def status(text: str) -> None:
            logger.info("Этап: %s", text)
            if status_callback:
                status_callback(text)

        logger.info(
            "Начинаю прошивку: %s, base=0x%08X, размер %d байт, page_size=%d",
            firmware_path, base_address, len(firmware), page_size,
        )
        status("Подключение к bootloader")
        self.reconfigure_for_bootloader()
        self.enter_bootloader()
        self.sync()

        # Страница идентификации из образа объединяется с существующей:
        # имя/serial/VID/PID устройства сохраняются, версионная запись
        # VER1 берётся из нового образа (отчёт мастера).
        if info_seg is not None and any(b != 0xFF for b in info_seg):
            existing = self.read_memory(DEVICE_INFO_PAGE_ADDR, DEVICE_CONFIG_PAGE_SIZE)
            info_seg = merge_device_config_page(info_seg, existing)
            logger.info("Страница идентификации объединена с существующей (VID/PID сохранены)")
        elif info_seg is not None:
            info_seg = None  # пустая прослойка разреженного образа — не пишем

        # Метаданные из одних 0xFF (padding разреженного образа)
        # считаем отсутствующими — их сгенерирует ПК.
        if meta_seg is not None and not any(b != 0xFF for b in meta_seg):
            meta_seg = None

        # Обновление application — только когда в образе есть реальный код.
        # Config-only запись (код отсутствует/целиком 0xFF) не должна
        # трогать метаданные: инвалидация без последующей записи валидных
        # метаданных убила бы загрузку приложения.
        is_app_update = bool(code) and any(b != 0xFF for b in code)
        meta = meta_seg
        if is_app_update:
            status("Подготовка обновления")
            # UPDATE_STARTED: гасим magic метаданных. Запись нулей работает
            # и на стёртой странице, и поверх старого magic — flash умеет
            # только гасить биты.
            self.write_memory(APP_METADATA_PAGE_ADDR, b"\x00" * 4)
            if meta is None:
                meta = build_app_metadata(code)

        status("Стирание Flash")
        # При обновлении application страницы кода стираем полностью:
        # пропущенная «пустая» (0xFF) страница сохранила бы старые данные,
        # а верификация и CRC метаданных покрывают весь диапазон — иначе
        # ложный провал верификации или невалидный образ после reboot.
        if code:
            self.erase_pages(
                code_addr,
                code,
                page_size=page_size,
                skip_blank=skip_blank and not is_app_update,
            )
        if info_seg is not None:
            self.erase_pages(
                DEVICE_INFO_PAGE_ADDR, info_seg, page_size=page_size, skip_blank=skip_blank
            )
        if meta is not None:
            self.erase_pages(
                APP_METADATA_PAGE_ADDR, meta, page_size=page_size, skip_blank=skip_blank
            )

        regions: list[tuple[int, bytes]] = []
        total = len(code) + (len(info_seg) if info_seg else 0) + (len(meta) if meta else 0)
        written = 0
        if code:
            status(f"Запись {len(code)} байт с 0x{code_addr:08X}")
            written = self._write_region(code_addr, code, 0, total, skip_blank)
            regions.append((code_addr, code))
        if info_seg is not None:
            status(f"Запись страницы идентификации ({len(info_seg)} байт)")
            written = self._write_region(
                DEVICE_INFO_PAGE_ADDR, info_seg, written, total, skip_blank
            )
            regions.append((DEVICE_INFO_PAGE_ADDR, info_seg))
        if meta is not None:
            # Метаданные — последним шагом: валидный APP1 служит «флагом
            # завершённого обновления» (bl_metadata_is_valid).
            status(f"Запись метаданных ({len(meta)} байт)")
            self._write_region(APP_METADATA_PAGE_ADDR, meta, written, total, skip_blank)
            regions.append((APP_METADATA_PAGE_ADDR, meta))

        if self._progress_callback:
            self._progress_callback(100)
        logger.info("Прошивка завершена успешно")
        return regions
