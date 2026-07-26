"""Реализация STM32 UART bootloader по протоколу AN3155.

Класс Bootloader работает с уже открытым pyserial.Serial-портом.
Все операции выполняются в синхронном режиме и должны вызываться
из фонового потока, чтобы не блокировать интерфейс.
"""

import time
from typing import Any, Callable, Dict, Optional

import serial

try:
    from serial.tools.list_ports import comports
except Exception:  # noqa: BLE001
    def comports() -> list:
        return []

from core.firmware_utils import load_firmware_bytes
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

    def __init__(self, port: serial.Serial, progress_callback: Optional[Callable[[int], None]] = None) -> None:
        """Создаёт объект бутлоадера.

        Args:
            port: Открытый объект serial.Serial, настроенный для bootloader.
            progress_callback: Функция, принимающая процент (0–100).
        """
        self.port = port
        self._progress_callback = progress_callback
        self._stop_requested = False

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
            self.port.open()
            logger.info("Порт перенастроен для bootloader: Even, 1 стоп-бит")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось перенастроить порт для bootloader: %s", exc)

    def _read_byte(self, timeout: float = 1.0) -> int:
        """Считывает один байт из порта с таймаутом.

        Args:
            timeout: Время ожидания в секундах.

        Returns:
            Значение байта.

        Raises:
            BootloaderError: если таймаут или нет данных.
        """
        self.port.timeout = timeout
        byte = self.port.read(1)
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
        self.port.write(bytes([command, command ^ 0xFF]))
        if wait_ack:
            response = self._read_byte()
            if response != ACK:
                raise BootloaderError(f"Команда 0x{command:02X} не подтверждена (ответ 0x{response:02X})")

    def _port_info(self) -> Optional[Any]:
        """Возвращает информацию о текущем COM-порте."""
        for p in comports():
            if p.device == self.port.port:
                return p
        return None

    def request_app_reboot(self) -> None:
        """Отправляет запущенному приложению команду программной перезагрузки
        в режим bootloader.

        Прошивка приложения должна распознать REBOOT_TO_BOOTLOADER_MAGIC,
        записать флаг 0xDEADBEEF по адресу 0x20004FF0 и выполнить NVIC_SystemReset().
        """
        magic = self.REBOOT_TO_BOOTLOADER_MAGIC
        self.port.reset_output_buffer()
        self.port.write(magic)
        self.port.flush()
        logger.info("Команда перезагрузки в bootloader отправлена (%d байт)", len(magic))

    def wait_for_bootloader_port(self, timeout: float = 10.0) -> None:
        """Ждёт появления bootloader-порта (VID=0483, PID=5741) после reset."""
        logger.info("Ожидание появления bootloader-порта...")
        start = time.time()
        while time.time() - start < timeout:
            for p in comports():
                if p.vid == self.USB_VID and p.pid == self.USB_BOOTLOADER_PID:
                    logger.info("Bootloader-порт найден: %s", p.device)
                    try:
                        self.port.close()
                    except Exception:  # noqa: S110
                        pass
                    self.port.port = p.device
                    self.port.open()
                    return
            time.sleep(0.2)
        raise BootloaderError("Bootloader-порт не появился после перезагрузки")

    def reboot_to_bootloader(self, timeout: float = 10.0) -> None:
        """Программно перезагружает устройство в режим bootloader."""
        self.request_app_reboot()
        # Даём приложению время записать флаг и сброситься
        time.sleep(0.5)
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

    def erase(self, extended: bool = True) -> None:
        """Выполняет стирание памяти.

        Args:
            extended: Если True, используется команда 0x44 (Extended Erase),
                      иначе 0x43 (Mass Erase).

        Raises:
            BootloaderError: при ошибке стирания.
        """
        logger.info("Начинаю стирание памяти STM32")
        if extended:
            # Extended Erase: 0x44, затем количество страниц, затем номера и контрольная сумма
            self._send_command(0x44)
            # Массовое стирание: 0xFF 0xFF + checksum
            self.port.write(bytes([0xFF, 0xFF, 0x00]))
        else:
            self._send_command(0x43)
            # Массовое стирание
            self.port.write(bytes([0xFF, 0x00]))

        response = self._read_byte(5.0)
        if response != ACK:
            raise BootloaderError(f"Ошибка стирания (ответ 0x{response:02X})")
        logger.info("Стирание памяти завершено")

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

        # Формируем команду Write Memory 0x31
        self._send_command(0x31)

        # Адрес + контрольная сумма адреса
        addr_bytes = address.to_bytes(4, "big")
        addr_checksum = 0
        for b in addr_bytes:
            addr_checksum ^= b
        self.port.write(addr_bytes)
        self.port.write(bytes([addr_checksum]))

        response = self._read_byte()
        if response != ACK:
            raise BootloaderError(f"Адрес не подтверждён (ответ 0x{response:02X})")

        # Данные: N-1, затем байты, затем XOR
        n = length - 1
        self.port.write(bytes([n]))
        self.port.write(data)
        checksum = n
        for b in data:
            checksum ^= b
        self.port.write(bytes([checksum]))

        response = self._read_byte(5.0)
        if response != ACK:
            raise BootloaderError(f"Ошибка записи блока (ответ 0x{response:02X})")

    def verify(self, address: int, data: bytes) -> bool:
        """Сравнивает данные в памяти STM32 с ожидаемыми.

        Args:
            address: Адрес для чтения.
            data: Ожидаемые байты.

        Returns:
            True, если данные совпадают, иначе False.
        """
        read_data = self.read_memory(address, len(data))
        return read_data == data

    def _read_memory_chunk(self, address: int, length: int) -> bytes:
        """Читает один блок памяти длиной до 256 байт."""
        if length < 1 or length > self.BLOCK_SIZE:
            raise BootloaderError(f"Недопустимый размер блока чтения: {length}")

        self._send_command(0x11)
        addr_bytes = address.to_bytes(4, "big")
        addr_checksum = 0
        for b in addr_bytes:
            addr_checksum ^= b
        self.port.write(addr_bytes)
        self.port.write(bytes([addr_checksum]))
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
        result = bytearray()
        offset = 0
        while offset < length:
            chunk_len = min(self.BLOCK_SIZE, length - offset)
            result.extend(self._read_memory_chunk(address + offset, chunk_len))
            offset += chunk_len
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
        return version

    def get_id(self) -> int:
        """Возвращает идентификатор устройства (команда 0x02)."""
        self._send_command(0x02)
        length = self._read_byte()
        device_id = 0
        for _ in range(length + 1):
            device_id = (device_id << 8) | self._read_byte()
        return device_id

    def diagnostics(self) -> Dict[str, int]:
        """Выполняет синхронизацию и возвращает версию и ID устройства.

        Returns:
            Словарь с ключами 'version' и 'device_id'.
        """
        self.reconfigure_for_bootloader()
        self.enter_bootloader()
        self.sync()
        version = self.get_version()
        device_id = self.get_id()
        return {"version": version, "device_id": device_id}

    def flash_firmware(self, firmware_path: str, base_address: int = 0x08008000) -> None:
        """Записывает файл прошивки в память STM32.

        Поддерживает .bin, .hex (Intel HEX) и .elf.

        Args:
            firmware_path: Путь к файлу прошивки.
            base_address: Начальный адрес записи (по умолчанию 0x08008000).

        Raises:
            BootloaderError: при ошибке прошивки.
        """
        firmware, file_base = load_firmware_bytes(firmware_path)
        if not firmware:
            raise BootloaderError("Файл прошивки пуст")
        if file_base:
            base_address = file_base

        logger.info("Начинаю прошивку: %s, размер %d байт", firmware_path, len(firmware))
        self.reconfigure_for_bootloader()
        self.enter_bootloader()
        self.sync()
        self.erase()

        total = len(firmware)
        for offset in range(0, total, self.BLOCK_SIZE):
            if self._stop_requested:
                raise BootloaderError("Операция отменена")
            block = firmware[offset : offset + self.BLOCK_SIZE]
            # Дополняем блок до 256 байт нулями
            if len(block) < self.BLOCK_SIZE:
                block = block + bytes(self.BLOCK_SIZE - len(block))
            address = base_address + offset
            for attempt in range(self.MAX_RETRIES):
                try:
                    self.write_memory(address, block)
                    logger.debug("Записан блок по адресу 0x%08X", address)
                    break
                except BootloaderError as exc:
                    logger.warning("Повтор записи блока 0x%08X: %s", address, exc)
                    if attempt == self.MAX_RETRIES - 1:
                        raise
                    time.sleep(0.1)

            progress = min(100, int((offset + self.BLOCK_SIZE) / total * 100))
            if self._progress_callback:
                self._progress_callback(progress)

        if self._progress_callback:
            self._progress_callback(100)
        logger.info("Прошивка завершена успешно")
