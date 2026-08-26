"""Настройка глобального логирования для приложения «Код Мастер»."""

import logging
import logging.handlers
import subprocess
import sys
from pathlib import Path

from platformdirs import user_log_dir


LOG_DIR = Path(user_log_dir("CodeMaster", appauthor=False, ensure_exists=True))
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

    # logging.StreamHandler/FileHandler.emit() вызывает self.flush() (=fsync-подобный
    # сброс на диск) после КАЖДОЙ записи. Протокол бутлоадера (core/bootloader.py)
    # пишет несколько debug-строк на каждый 256-байтный блок прошивки — на полном
    # образе 256 КБ это тысячи синхронных сбросов на диск, которые на медленном
    # диске/антивирусе на Windows заметно тормозят саму прошивку. Буферизуем через
    # MemoryHandler: пишем на диск пачками по 200 записей, а любую ERROR-запись —
    # немедленно, чтобы не терять диагностику при сбое.
    buffered_file_handler = logging.handlers.MemoryHandler(
        capacity=200,
        flushLevel=logging.ERROR,
        target=file_handler,
        flushOnClose=True,
    )
    buffered_file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)

    root_logger.addHandler(buffered_file_handler)
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
