"""Проверка наличия обновлений приложения через GitHub Releases."""

import json
import ssl
import urllib.error
import urllib.request
from typing import Tuple

from models.logger import get_logger
from models.version import VERSION

logger = get_logger(__name__)

CURRENT_VERSION = VERSION
RELEASES_URL = "https://api.github.com/repos/GrafKD/code_master/releases/latest"


def _create_ssl_context() -> ssl.SSLContext:
    """Создаёт SSL-контекст с проверкой сертификатов."""
    context = ssl.create_default_context()
    return context


def _parse_version(version: str) -> Tuple[int, ...]:
    """Парсит версию в кортеж чисел для корректного сравнения."""
    parts = version.strip().lstrip("v").split(".")
    result: list[int] = []
    for part in parts:
        # Берём только числовую часть до первого не-цифрового символа
        numeric = ""
        for ch in part:
            if ch.isdigit():
                numeric += ch
            else:
                break
        result.append(int(numeric) if numeric else 0)
    return tuple(result)


def check_for_updates() -> Tuple[bool, str]:
    """Проверяет, доступна ли новая версия приложения.

    Returns:
        Кортеж (доступно_обновление, сообщение).
    """
    try:
        req = urllib.request.Request(
            RELEASES_URL,
            headers={
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "CodeMaster-UpdateChecker",
            },
        )
        with urllib.request.urlopen(req, timeout=10, context=_create_ssl_context()) as response:
            data = json.loads(response.read().decode("utf-8"))
        latest = data.get("tag_name", "").lstrip("v")
        if not latest:
            return False, "Не удалось определить последнюю версию"
        if _parse_version(latest) > _parse_version(CURRENT_VERSION):
            url = data.get("html_url", "")
            return True, f"Доступна новая версия {latest}.\nТекущая версия: {CURRENT_VERSION}.\n{url}"
        return False, f"Используется последняя версия ({CURRENT_VERSION})"
    except urllib.error.HTTPError as exc:
        logger.error("HTTP ошибка при проверке обновлений: %s", exc)
        return False, f"Ошибка проверки обновлений: {exc.code}"
    except urllib.error.URLError as exc:
        logger.error("Ошибка сети при проверке обновлений: %s", exc)
        return False, "Нет подключения к интернету"
    except Exception as exc:  # noqa: BLE001
        logger.exception("Ошибка при проверке обновлений")
        return False, f"Ошибка проверки обновлений: {exc}"


def get_current_version() -> str:
    """Возвращает текущую версию приложения."""
    return CURRENT_VERSION
