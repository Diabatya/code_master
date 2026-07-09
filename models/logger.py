"""Настройка глобального логирования для приложения «Код Мастер»."""

import logging
import logging.handlers
import subprocess
import sys
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "code_master.log"


def get_logger(name: str) -> logging.Logger:
    """Возвращает логгер с уже настроенными обработчиками.

    Args:
        name: Имя логгера, обычно __name__.

    Returns:
        Настроенный экземпляр logging.Logger.
    """
    return logging.getLogger(name)


def setup_logging() -> None:
    """Создаёт папку с логами и настраивает ротацию файлов.

    Файловый обработчик сохраняет до 3 резервных копий по 1 МБ каждая.
    Консольный обработчик выводит сообщения уровня INFO и выше.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Очищаем старые обработчики, чтобы избежать дублирования при повторном вызове
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=1_048_576,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    logging.getLogger("code_master").info("Логирование настроено. Файл: %s", LOG_FILE)


def get_log_dir() -> Path:
    """Возвращает путь к папке с логами."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR


def open_log_folder() -> None:
    """Открывает папку с лог-файлом в стандартном проводнике системы."""
    folder = str(get_log_dir())
    if sys.platform == "win32":
        import os
        os.startfile(folder)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", folder], check=False)
    else:
        subprocess.run(["xdg-open", folder], check=False)
