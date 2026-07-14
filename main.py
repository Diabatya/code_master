"""Точка входа в приложение «Код Мастер».

Создаёт QApplication, применяет тёмную тему, инициализирует логирование,
запускает главное окно и корректно завершает работу при закрытии.
Поддерживает режим командной строки для прошивки STM32.
"""

import argparse
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QDialog

from core.bootloader import Bootloader, BootloaderError
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import setup_logging
from ui.dark_theme import apply_dark_theme, apply_light_theme
from ui.disclaimer_dialog import DisclaimerDialog
from ui.main_window import MainWindow


def _parse_args() -> argparse.Namespace:
    """Парсит аргументы командной строки."""
    parser = argparse.ArgumentParser(description="Код Мастер — прошивка STM32 и работа с CAN")
    parser.add_argument("--firmware", help="Путь к файлу .bin для прошивки в режиме CLI")
    parser.add_argument("--port", help="Имя COM-порта (например, COM3)")
    parser.add_argument("--baudrate", type=int, default=115200, help="Скорость COM-порта")
    parser.add_argument("--emulation", action="store_true", help="Использовать эмулятор порта")
    return parser.parse_args()


def _cli_flash(args: argparse.Namespace) -> int:
    """Выполняет прошивку STM32 из командной строки.

    Returns:
        Код завершения: 0 при успехе, 1 при ошибке.
    """
    config = Config()
    port_name = args.port or config.get("port", "")
    if not port_name:
        print("Ошибка: не указан COM-порт. Используйте --port или сохраните порт в GUI.", file=sys.stderr)
        return 1

    serial_manager = SerialManager()
    if not serial_manager.open_port(port_name, args.baudrate, args.emulation):
        print(f"Ошибка: не удалось открыть порт {port_name}", file=sys.stderr)
        return 1

    try:
        port = serial_manager._port
        if port is None:
            raise BootloaderError("COM-порт не открыт")
        import serial
        if not isinstance(port, serial.Serial):
            raise BootloaderError("Прошивка требует реальный COM-порт")
        bootloader = Bootloader(port)
        bootloader.flash_firmware(args.firmware)
        print("Прошивка завершена успешно")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка прошивки: {exc}", file=sys.stderr)
        return 1
    finally:
        serial_manager.close_port()


def main() -> int:
    """Запускает приложение.

    Returns:
        Код завершения программы.
    """
    setup_logging()
    args = _parse_args()

    if args.firmware:
        return _cli_flash(args)

    # Включаем масштабирование интерфейса на экранах высокого разрешения
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("Код Мастер")
    app.setApplicationVersion("1.0.0")

    config = Config()
    if config.get("light_theme", False):
        apply_light_theme(app)
    else:
        apply_dark_theme(app)

    serial_manager = SerialManager()

    disclaimer = DisclaimerDialog()
    if disclaimer.exec() != QDialog.DialogCode.Accepted:
        return 0

    main_window = MainWindow(serial_manager)
    main_window.show()

    result = app.exec()
    serial_manager.close_port()
    return result


if __name__ == "__main__":
    sys.exit(main())
