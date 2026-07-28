"""Простая реализация USB DFU (STM32) поверх pyusb."""

from __future__ import annotations

import time
from typing import Callable, List, Optional, Tuple

import usb.core
import usb.util

from models.logger import get_logger

logger = get_logger(__name__)

try:
    import usb.backend.libusb1 as libusb1
    import libusb_package

    _USB_BACKEND = libusb1.get_backend(find_library=libusb_package.find_library)
except Exception:
    _USB_BACKEND = None

DFU_REQUEST_SEND = 0x21
DFU_REQUEST_RECEIVE = 0xA1

DFU_DETACH = 0
DFU_DNLOAD = 1
DFU_UPLOAD = 2
DFU_GETSTATUS = 3
DFU_CLRSTATUS = 4
DFU_ABORT = 6

STATE_DFU_IDLE = 2
STATE_DFU_DNLOAD_SYNC = 3
STATE_DFU_DNBUSY = 4
STATE_DFU_DNLOAD_IDLE = 5
STATE_DFU_MANIFEST = 7
STATE_DFU_MANIFEST_WAIT_RESET = 8
STATE_DFU_ERROR = 10

DFU_STATUS_OK = 0x00


def _xor_checksum(data: bytes) -> int:
    """Возвращает XOR всех байт (контрольная сумма STM32 DFU)."""
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum


def find_dfu_device() -> usb.core.Device:
    """Находит STM32 DFU устройство, используя libusb-package backend если доступен."""
    dev = usb.core.find(
        idVendor=0x0483,
        idProduct=0xDF11,
        backend=_USB_BACKEND,
    )
    if dev is None:
        raise RuntimeError("USB DFU устройство 0483:DF11 не найдено")
    return dev


class DfuDevice:
    """Обёртка для USB DFU устройства."""

    def __init__(self, dev: usb.core.Device) -> None:
        self.dev = dev
        self.intf: Optional[usb.core.Interface] = None

    def open(self) -> None:
        """Инициализирует устройство, отключает kernel driver и занимает интерфейс."""
        try:
            self.dev.set_configuration()
        except usb.core.USBError:
            pass
        try:
            cfg = self.dev.get_active_configuration()
        except usb.core.USBError as exc:
            raise RuntimeError(
                "Не удалось получить активную USB-конфигурацию. "
                "На Windows установите WinUSB-драйвер через Zadig (STM32 BOOTLOADER 0483:DF11)."
            ) from exc
        self.intf = usb.util.find_descriptor(
            cfg,
            bInterfaceClass=0xFE,
            bInterfaceSubClass=0x01,
        )
        if self.intf is None:
            raise RuntimeError("DFU интерфейс не найден")
        ifn = self.intf.bInterfaceNumber
        try:
            if self.dev.is_kernel_driver_active(ifn):
                self.dev.detach_kernel_driver(ifn)
        except (NotImplementedError, usb.core.USBError, ValueError):
            pass
        try:
            usb.util.claim_interface(self.dev, ifn)
        except usb.core.USBError as exc:
            raise RuntimeError(
                "Не удалось захватить DFU интерфейс. "
                "На Windows установите WinUSB-драйвер через Zadig (STM32 BOOTLOADER 0483:DF11)."
            ) from exc
        # Переводим DFU-конечный автомат в известное состояние (IDLE).
        self._reset_to_idle()

    def _reset_to_idle(self) -> None:
        """Сбрасывает dfuERROR/прочие состояния и возвращает устройство в dfuIDLE."""
        for attempt in range(3):
            try:
                status = self._status(timeout=5000)
            except usb.core.USBError as exc:
                logger.warning("DFU: не удалось прочитать статус: %s", exc)
                break
            if len(status) < 6:
                break
            state = status[4]
            bstatus = status[0]
            logger.debug("DFU статус (сброс %d): state=0x%02X bStatus=0x%02X", attempt, state, bstatus)
            if state in (STATE_DFU_IDLE, STATE_DFU_DNLOAD_IDLE):
                return
            if state in (STATE_DFU_MANIFEST, STATE_DFU_MANIFEST_WAIT_RESET):
                # Устройство завершает прошивку/перезагружается; подождём, не мешаем
                time.sleep(0.2)
                continue
            if state == STATE_DFU_ERROR:
                try:
                    self._ctrl(DFU_REQUEST_SEND, DFU_CLRSTATUS, timeout=5000)
                    time.sleep(0.01)
                    continue
                except usb.core.USBError as exc:
                    logger.debug("DFU CLRSTATUS не удался: %s", exc)
                    break
            # В остальных состояниях пробуем ABORT, который переводит в IDLE
            try:
                self._ctrl(DFU_REQUEST_SEND, DFU_ABORT, timeout=5000)
                time.sleep(0.01)
                continue
            except usb.core.USBError as exc:
                logger.debug("DFU ABORT не удался: %s", exc)
                break

    def _parse_transfer_size(self, raw) -> Optional[int]:
        """Парсит wTransferSize из сырого DFU functional descriptor."""
        if not raw:
            return None
        if isinstance(raw, (list, tuple)):
            for desc in raw:
                if len(desc) >= 9 and desc[1] == 0x21:
                    return int.from_bytes(bytes(desc[5:7]), "little")
            return None
        try:
            data = bytes(raw)
        except (TypeError, ValueError):
            return None
        i = 0
        while i + 2 <= len(data):
            length = data[i]
            if length == 0:
                break
            dtype = data[i + 1]
            if dtype == 0x21 and i + length <= len(data) and length >= 9:
                return int.from_bytes(data[i + 5:i + 7], "little")
            i += length
        return None

    def _get_transfer_size(self) -> Optional[int]:
        """Возвращает wTransferSize из DFU functional descriptor, если возможно."""
        try:
            cfg = self.dev.get_active_configuration()
            for source in (getattr(cfg, "extra", b""), getattr(cfg, "extra_descriptors", [])):
                size = self._parse_transfer_size(source)
                if size is not None:
                    return size
            for intf in cfg.interfaces():
                for alt in intf.altsettings():
                    for source in (getattr(alt, "extra", b""), getattr(alt, "extra_descriptors", [])):
                        size = self._parse_transfer_size(source)
                        if size is not None:
                            return size
        except Exception:
            logger.debug("Не удалось прочитать DFU descriptor wTransferSize", exc_info=True)
        return None

    def _ctrl(self, request_type: int, request: int, value: int = 0, data_or_wlength=0, timeout: int = 5000):
        return self.dev.ctrl_transfer(
            request_type,
            request,
            value,
            self.intf.bInterfaceNumber,
            data_or_wlength,
            timeout=timeout,
        )

    def _status(self, timeout: int = 5000) -> bytes:
        return bytes(self._ctrl(DFU_REQUEST_RECEIVE, DFU_GETSTATUS, 0, 6, timeout=timeout))

    def _wait(self, status_timeout: int = 60000) -> None:
        """Ждёт завершения операции DFU и проверяет, что статус OK."""
        deadline = time.time() + status_timeout / 1000.0
        while True:
            if time.time() > deadline:
                raise RuntimeError("Таймаут ожидания статуса DFU")
            try:
                status = self._status(timeout=5000)
            except usb.core.USBError as exc:
                raise RuntimeError(f"Ошибка чтения статуса DFU: {exc}") from exc
            if len(status) < 6:
                raise RuntimeError("Некорректный ответ DFU_GETSTATUS")
            state = status[4]
            bstatus = status[0]
            if state in (STATE_DFU_DNLOAD_SYNC, STATE_DFU_DNBUSY):
                poll_timeout = 0.001 * (status[1] | (status[2] << 8) | (status[3] << 16))
                time.sleep(max(poll_timeout, 0.001))
                continue
            if state == STATE_DFU_ERROR:
                error_code = bstatus
                try:
                    self._ctrl(DFU_REQUEST_SEND, DFU_CLRSTATUS, timeout=5000)
                except usb.core.USBError:
                    pass
                raise RuntimeError(f"DFU ошибка: state=dfuERROR, bStatus=0x{error_code:02X}")
            if bstatus != DFU_STATUS_OK:
                raise RuntimeError(f"DFU статус ошибки: 0x{bstatus:02X}, state=0x{state:02X}")
            logger.debug("DFU статус OK: state=0x%02X, bStatus=0x%02X", state, bstatus)
            return

    def mass_erase(self) -> None:
        """Полное стирание flash (STM32)."""
        logger.warning("Выполняется mass erase — будет стёрта вся flash включая bootloader")
        payload = bytes([0x41])
        self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, payload, timeout=30000)
        self._wait(status_timeout=60000)

    def page_erase(self, address: int, timeout_ms: int = 5000) -> None:
        """Стирает одну страницу flash по адресу (DfuSe/STM32)."""
        payload = bytes([
            0x41,
            address & 0xFF,
            (address >> 8) & 0xFF,
            (address >> 16) & 0xFF,
            (address >> 24) & 0xFF,
        ])
        logger.debug("DFU page erase: 0x%08X", address)
        self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, payload, timeout=max(timeout_ms, 5000))
        self._wait(status_timeout=timeout_ms)

    def abort(self) -> None:
        """Прерывает текущую DFU-операцию и возвращает устройство в dfuIDLE."""
        logger.debug("DFU ABORT")
        self._ctrl(DFU_REQUEST_SEND, DFU_ABORT, timeout=5000)
        self._wait(status_timeout=5000)

    def erase_pages(
        self,
        start: int,
        data: bytes,
        page_size: int = 2048,
        skip_blank: bool = True,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Стирает только страницы, которые будут перезаписаны.

        Args:
            start: Начальный адрес записи.
            data: Данные для записи.
            page_size: Размер страницы flash (для F105 — 2048 байт).
            skip_blank: Не стирать страницы, полностью состоящие из 0xFF.
            progress: Опциональный callback(current, total) для отслеживания прогресса.
        """
        if not data:
            return
        end = start + len(data)
        page = (start // page_size) * page_size
        pages_to_erase = []
        while page < end:
            seg_start = max(start, page)
            seg_end = min(end, page + page_size)
            page_data = data[seg_start - start : seg_end - start]
            if skip_blank and page_data and all(b == 0xFF for b in page_data):
                logger.debug("DFU: пропуск пустой страницы 0x%08X", page)
            else:
                pages_to_erase.append((page, len(page_data)))
            page += page_size

        total = len(pages_to_erase)
        logger.info("DFU: стирание страниц от 0x%08X до 0x%08X (page_size=%d, skip_blank=%s)", start, end - 1, page_size, skip_blank)
        for i, (page, page_len) in enumerate(pages_to_erase, 1):
            if progress:
                progress(i, total)
            logger.info("DFU: стирание страницы 0x%08X (%d байт данных)", page, page_len)
            self.page_erase(page)

    def _set_address(self, address: int) -> None:
        """Устанавливает Address Pointer (LSB first, 5 байт, без checksum)."""
        payload = bytes([0x21]) + address.to_bytes(4, "little")
        logger.debug("DFU set address: 0x%08X -> %s", address, payload.hex())
        self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, payload, timeout=10000)
        self._wait(status_timeout=5000)

    def _download_fast(
        self,
        address: int,
        data: bytes,
        block_size: int,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Быстрая запись через один set_address и нарастающий wBlockNum."""
        total = len(data)
        logger.info("DFU download (fast): адрес 0x%08X, размер %d байт, wTransferSize %d", address, total, block_size)
        self._set_address(address)
        for offset in range(0, total, block_size):
            chunk = data[offset:offset + block_size]
            block_num = 2 + offset // block_size
            logger.debug("DFU download fast: offset %d, блок %d, %d байт", offset, block_num, len(chunk))
            self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, block_num, chunk, timeout=20000)
            self._wait(status_timeout=10000)
            if progress:
                progress(min(offset + len(chunk), total), total)
        logger.info("DFU download fast завершён: 0x%08X (%d байт)", address, total)

    def _download_slow(
        self,
        address: int,
        data: bytes,
        block_size: int,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Безопасная медленная запись: set_address на каждый чанк."""
        total = len(data)
        logger.info("DFU download (safe/slow): адрес 0x%08X, размер %d байт, block_size %d", address, total, block_size)
        for i in range(0, total, block_size):
            chunk = data[i:i + block_size]
            chunk_addr = address + i
            self._set_address(chunk_addr)
            logger.debug("DFU download slow: адрес 0x%08X, chunk %d байт", chunk_addr, len(chunk))
            self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 2, chunk, timeout=10000)
            self._wait(status_timeout=5000)
            if progress:
                progress(min(i + len(chunk), total), total)
        logger.info("DFU download slow завершён: 0x%08X (%d байт)", address, total)

    def download(
        self,
        address: int,
        data: bytes,
        block_size: Optional[int] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> None:
        """Записывает данные по указанному адресу.

        Использует быстрый путь (один set_address + нарастающий wBlockNum),
        если удалось определить wTransferSize из DFU descriptor.
        Иначе падает на безопасный медленный путь.
        """
        if not data:
            return
        transfer_size = self._get_transfer_size() if block_size is None else None
        if transfer_size is not None and transfer_size > 0:
            self._download_fast(address, data, transfer_size, progress)
        else:
            self._download_slow(address, data, block_size or 64, progress)

    def _upload_fast(
        self,
        address: int,
        length: int,
        block_size: int,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        """Быстрое чтение через один set_address и нарастающий wBlockNum."""
        total = length
        result = bytearray()
        logger.info("DFU upload (fast): адрес 0x%08X, %d байт, wTransferSize %d", address, total, block_size)
        self._set_address(address)
        self.abort()
        offset = 0
        while offset < total:
            chunk_len = min(block_size, total - offset)
            block_num = 2 + offset // block_size
            logger.debug("DFU upload fast: offset %d, блок %d, запрошено %d байт", offset, block_num, chunk_len)
            chunk = bytes(self._ctrl(DFU_REQUEST_RECEIVE, DFU_UPLOAD, block_num, chunk_len, timeout=10000))
            if not chunk:
                break
            result.extend(chunk)
            offset += len(chunk)
            if progress:
                progress(offset, total)
            if len(chunk) < chunk_len:
                break
        if progress:
            progress(offset, total)
        logger.info("DFU upload fast завершён: 0x%08X, прочитано %d байт", address, len(result))
        return bytes(result)

    def _upload_slow(
        self,
        address: int,
        length: int,
        block_size: int,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        """Безопасное медленное чтение: set_address + abort на каждый чанк."""
        total = length
        result = bytearray()
        logger.info("DFU upload (safe/slow): адрес 0x%08X, %d байт, block_size %d", address, total, block_size)
        offset = 0
        while offset < total:
            chunk_len = min(block_size, total - offset)
            self.abort()
            self._set_address(address + offset)
            self.abort()
            logger.debug("DFU upload slow: адрес 0x%08X, запрошено %d байт", address + offset, chunk_len)
            chunk = bytes(self._ctrl(DFU_REQUEST_RECEIVE, DFU_UPLOAD, 2, chunk_len, timeout=10000))
            if not chunk:
                break
            result.extend(chunk)
            offset += len(chunk)
            if progress:
                progress(offset, total)
            if len(chunk) < chunk_len:
                break
        if progress:
            progress(offset, total)
        logger.info("DFU upload slow завершён: 0x%08X, прочитано %d байт", address, len(result))
        return bytes(result)

    def upload(
        self,
        address: int,
        length: int,
        block_size: Optional[int] = None,
        progress: Optional[Callable[[int, int], None]] = None,
    ) -> bytes:
        """Читает length байт с address."""
        if length <= 0:
            return b""
        transfer_size = self._get_transfer_size() if block_size is None else None
        if transfer_size is not None and transfer_size > 0:
            return self._upload_fast(address, length, transfer_size, progress)
        return self._upload_slow(address, length, block_size or 64, progress)

    def leave(self) -> None:
        """Выход из DFU (reset).

        Для STM32 нужно установить Address Pointer на вектор сброса,
        затем выполнить zero-length DNLOAD с wValue=0.
        """
        try:
            self._set_address(0x08000000)
        except Exception:  # noqa: BLE001
            pass
        try:
            self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, b"", timeout=1000)
        except usb.core.USBError:
            pass  # устройство перезагружается и отваливается

    def close(self) -> None:
        """Освобождает USB интерфейс и закрывает дескриптор устройства."""
        if self.intf is not None:
            try:
                usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
            except usb.core.USBError:
                pass
            self.intf = None
        try:
            usb.util.dispose_resources(self.dev)
        except Exception:  # noqa: S110
            pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
