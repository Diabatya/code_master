"""Простая реализация USB DFU (STM32) поверх pyusb."""

from __future__ import annotations

import time
from typing import Optional

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

    def _wait(self, status_timeout: int = 5000) -> None:
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
                return
            time.sleep(0.001 * (status[1] | (status[2] << 8) | (status[3] << 16)))

    def mass_erase(self) -> None:
        """Полное стирание flash (STM32)."""
        self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, bytes([0x41, 0xFF, 0xFF]), timeout=30000)
        self._wait(status_timeout=10000)

    def _set_address(self, address: int) -> None:
        payload = bytes([
            0x21,
            (address >> 24) & 0xFF,
            (address >> 16) & 0xFF,
            (address >> 8) & 0xFF,
            address & 0xFF,
        ])
        self._ctrl(DFU_REQUEST_SEND, DFU_DNLOAD, 0, payload, timeout=10000)
        self._wait(status_timeout=5000)

    def download(self, address: int, data: bytes, block_size: int = 1024) -> None:
        """Записывает данные по указанному адресу."""
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

    def upload(self, address: int, length: int, block_size: int = 1024) -> bytes:
        """Читает length байт с address."""
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
        """Выход из DFU (reset)."""
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
