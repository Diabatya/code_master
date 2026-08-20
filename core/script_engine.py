"""Sandbox-исполнение Python-скриптов для CAN-обработки.

ВАЖНО (см. CURSOR_FIX_PROMPT.md 4.1): урезанный ``__builtins__`` ниже — это
НЕ настоящая security-песочница, а просто защита от неосторожных ошибок в
доверенных пользовательских скриптах (опечатки, использование не тех
функций и т.п.). Изоляция обходится классическими приёмами интроспекции
объектов Python (например, через
``().__class__.__bases__[0].__subclasses__()`` можно добраться до
произвольных классов, загруженных где-либо в процессе, включая файловые/
подпроцессные, если соответствующие модули уже импортированы). НЕ
запускайте здесь скрипты из недоверенных источников. Полноценная изоляция
потребовала бы либо `RestrictedPython`, либо запуска в отдельном процессе
с урезанными правами — см. TODO ниже.
"""

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from models.logger import get_logger

logger = get_logger(__name__)

EXEC_TIMEOUT = 3.0

# TODO(security): exec() в текущем процессе не является реальной изоляцией
# (см. предупреждение в модульном докстринге выше). Если потребуется
# запускать недоверенные скрипты, рассмотреть RestrictedPython или вынос
# исполнения в отдельный subprocess с урезанными правами/окружением.


class ScriptTimeoutError(Exception):
    """Скрипт превысил допустимое время выполнения."""


class _ScriptRunState:
    """Изолированное хранилище результатов одного запуска скрипта.

    Раньше `send_can`/`log` писали напрямую в `self._send_requests`/
    `self._log_lines` движка. При таймауте поток-исполнитель не
    останавливается принудительно (Python threads не поддерживают
    безопасное прерывание) и продолжает работать в фоне (daemon=True).
    Если пользователь тем временем запускал следующий скрипт, эти списки
    пересоздавались — и "протухший" поток предыдущего запуска мог начать
    писать в списки уже нового запуска (гонка данных). Передавая каждому
    запуску свой собственный `_ScriptRunState`, даже поток, переживший
    таймаут, продолжает писать только в свою изолированную структуру,
    не влияя на последующие запуски.
    """

    def __init__(self) -> None:
        self.send_requests: List[Tuple[int, int, List[int]]] = []
        self.log_lines: List[str] = []


class ScriptEngine:
    """Изолированный движок для выполнения пользовательских скриптов."""

    def __init__(self) -> None:
        """Создаёт движок."""

    @staticmethod
    def _build_safe_globals(state: _ScriptRunState, frame: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Формирует безопасное глобальное окружение для скрипта.

        `send_can`/`log`/`sleep` замыкаются на `state` конкретного запуска
        (см. `_ScriptRunState`), а не на атрибуты `self`.
        """
        import struct  # noqa: PLC0415
        import time as _time  # noqa: PLC0415

        def _send_can(channel: int, can_id: int, data: List[int]) -> None:
            state.send_requests.append((int(channel), int(can_id), list(int(b) for b in data)[:8]))

        def _log(message: str) -> None:
            state.log_lines.append(str(message))

        def _sleep(ms: int) -> None:
            time.sleep(max(0, int(ms)) / 1000.0)

        safe_globals = {
            "__builtins__": {
                "abs": abs,
                "all": all,
                "any": any,
                "bin": bin,
                "bool": bool,
                "bytes": bytes,
                "bytearray": bytearray,
                "chr": chr,
                "dict": dict,
                "enumerate": enumerate,
                "filter": filter,
                "float": float,
                "format": format,
                "frozenset": frozenset,
                "hasattr": hasattr,
                "hex": hex,
                "int": int,
                "isinstance": isinstance,
                "issubclass": issubclass,
                "iter": iter,
                "len": len,
                "list": list,
                "map": map,
                "max": max,
                "min": min,
                "next": next,
                "ord": ord,
                "pow": pow,
                "print": _log,
                "range": range,
                "repr": repr,
                "reversed": reversed,
                "round": round,
                "set": set,
                "slice": slice,
                "sorted": sorted,
                "str": str,
                "sum": sum,
                "tuple": tuple,
                "zip": zip,
            },
            "struct": struct,
            "time": _time,
            "send_can": _send_can,
            "log": _log,
            "sleep": _sleep,
        }
        if frame is not None:
            safe_globals["frame"] = frame
        return safe_globals

    def _execute(self, code: str, frame: Optional[Dict[str, Any]], state: _ScriptRunState) -> None:
        """Выполняет код в текущем потоке."""
        compiled = compile(code, "<script>", "exec")
        exec(compiled, self._build_safe_globals(state, frame))  # noqa: S102

    def run(
        self, code: str, frame: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """Запускает скрипт в sandbox с ограничением по времени.

        Args:
            code: Исходный код Python-скрипта.
            frame: Опциональный CAN-кадр для обработки.

        Returns:
            Словарь с ключами:
                - success: bool,
                - logs: List[str],
                - send_requests: List[Tuple[int, int, List[int]]],
                - error: Optional[str],
        """
        state = _ScriptRunState()
        result: Dict[str, Any] = {"success": False, "logs": [], "send_requests": [], "error": None}

        exception_holder: List[Optional[BaseException]] = [None]

        def target() -> None:
            try:
                self._execute(code, frame, state)
            except BaseException as exc:  # noqa: BLE001
                exception_holder[0] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=EXEC_TIMEOUT)

        if thread.is_alive():
            result["error"] = "Script execution timed out (3 seconds)"
            logger.warning(
                "Скрипт не завершился за отведённое время (%.1f с) и продолжает "
                "работать в фоне; его результаты (send_can/log) будут отброшены "
                "и не повлияют на последующие запуски.",
                EXEC_TIMEOUT,
            )
            return result

        exc = exception_holder[0]
        if exc is not None:
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Ошибка выполнения скрипта: %s", result["error"])
            return result

        result["success"] = True
        result["logs"] = state.log_lines[:]
        result["send_requests"] = state.send_requests[:]
        return result
