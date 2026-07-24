"""Простая реализация USB DFU (STM32) поверх pyusb."""

from __future__ import annotations

import time
from typing import List, Optional, Tuple

import usb.core
import usb.util

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

STATE_DFU_DNLOAD_SYNC = 3
STATE_DFU_DNBUSY = 4
STATE_DFU_ERROR = 10


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

    def _get_transfer_size(self) -> int:
        """Возвращает wTransferSize из DFU functional descriptor, если возможно."""
        try:
            cfg = self.dev.get_active_configuration()
            for intf in cfg.interfaces():
                for alt in intf.altsettings():
                    # pyusb может экспонировать extra_descriptors как список tuple (bLength, bDescriptorType, ...)
                    extras: List[Tuple[int, ...]] = getattr(alt, "extra_descriptors", []) or []
                    for desc in extras:
                        if len(desc) >= 9 and desc[1] == 0x21:  # DFU Functional Descriptor
                            return int.from_bytes(bytes(desc[5:7]), "little")
                    # Fallback: парсим сырой 'extra' байтовый массив
                    raw = getattr(alt, "extra", b"") or b""
                    i = 0
                    while i + 2 <= len(raw):
                        length = raw[i]
                        dtype = raw[i + 1]
                        if length == 0:
                            break
                        if dtype == 0x21 and i + length <= len(raw) and length >= 9:
                            return int.from_bytes(raw[i + 5:i + 7], "little")
                        i += length
        except Exception:
            pass
        return 64  # безопасный fallback для full-speed USB

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
        """Ждёт завершения операции DFU, сбрасывает DFU_ERROR при необходимости."""
        while True:
            try:
                status = self._status(timeout=status_timeout)
            except usb.core.USBError:
                return
            if len(status) < 6:
                return
            state = status[4]
            if state not in (STATE_DFU_DNLOAD_SYNC, STATE_DFU_DNBUSY):
                if state == STATE_DFU_ERROR:
                    try:
                        self._ctrl(DFU_REQUEST_SEND, DFU_CLRSTATUS, timeout=5000)
                    except usb.core.USBError:
                        pass
                    # Повторяем статус после сброса ошибки
                    try:
                        status = self._status(timeout=5000)
                    except usb.core.USBError:
                        return
                    if len(status) < 6:
                        return
                    state = status[4]
                return
            time.sleep(0.001 * (status[1] | (status[2] << 8) | (status[3] << 16)))

    def mass_erase(self) -> None:
        """Полное стирание flash (STM32)."""
        payload = bytes([0x41, 0xFF, 0xFF])
        payload += bytes([_xor_checksum(payload)])
        self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, payload, timeout=30000)
        self._wait(status_timeout=60000)

    def _set_address(self, address: int) -> None:
        payload = bytes([
            0x21,
            (address >> 24) & 0xFF,
            (address >> 16) & 0xFF,
            (address >> 8) & 0xFF,
            address & 0xFF,
        ])
        payload += bytes([_xor_checksum(payload)])
        self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, payload, timeout=10000)
        self._wait(status_timeout=5000)

    def download(self, address: int, data: bytes, block_size: Optional[int] = None) -> None:
        """Записывает данные по указанному адресу."""
        if block_size is None:
            block_size = self._get_transfer_size()
        self._set_address(address)
        block = 2
        for i in range(0, len(data), block_size):
            chunk = data[i:i + block_size]
            self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, block, chunk, timeout=10000)
            self._wait(status_timeout=5000)
            block += 1
        # zero-length DNLOAD для завершения программирования
        self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, block, b"", timeout=10000)
        self._wait(status_timeout=5000)

    def upload(self, address: int, length: int, block_size: Optional[int] = None) -> bytes:
        """Читает length байт с address."""
        if block_size is None:
            block_size = self._get_transfer_size()
        self._set_address(address)
        result = bytearray()
        block = 2
        remaining = length
        while remaining > 0:
            chunk_len = min(block_size, remaining)
            chunk = bytes(self._ctrl(DFU_REQUEST_RECEIVE, DFU_UPLOAD, block, chunk_len, timeout=10000))
            if not chunk:
                break
            result.extend(chunk)
            remaining -= len(chunk)
            block += 1
        return bytes(result)

    def leave(self) -> None:
        """Выход из DFU (reset).

        Для STM32 нужно установить Address Pointer на вектор сброса,
        затем выполнить zero-length DNLOAD.
        """
        try:
            self._set_address(0x08000000)
        except usb.core.USBError:
            pass
        try:
            self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, b"", timeout=1000)
        except usb.core.USBError:
            pass  # устройство перезагружается и отваливается

    def close(self) -> None:
        """Освобождает USB интерфейс."""
        if self.intf is not None:
            try:
                usb.util.release_interface(self.dev, self.intf.bInterfaceNumber)
            except usb.core.USBError:
                pass

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
