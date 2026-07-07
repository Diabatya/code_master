"""Sandbox-исполнение Python-скриптов для CAN-обработки."""

import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from models.logger import get_logger

logger = get_logger(__name__)

EXEC_TIMEOUT = 3.0


class ScriptTimeoutError(Exception):
    """Скрипт превысил допустимое время выполнения."""


class ScriptEngine:
    """Изолированный движок для выполнения пользовательских скриптов."""

    def __init__(self) -> None:
        """Создаёт движок."""
        self._send_requests: List[Tuple[int, int, List[int]]] = []
        self._log_lines: List[str] = []

    def _send_can(self, channel: int, can_id: int, data: List[int]) -> None:
        """Добавляет запрос на отправку CAN-кадра."""
        self._send_requests.append((int(channel), int(can_id), list(int(b) for b in data)[:8]))

    def _log(self, message: str) -> None:
        """Добавляет строку в лог скрипта."""
        self._log_lines.append(str(message))

    def _sleep(self, ms: int) -> None:
        """Засыпает на указанное количество миллисекунд."""
        time.sleep(max(0, int(ms)) / 1000.0)

    def _build_safe_globals(self, frame: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Формирует безопасное глобальное окружение для скрипта."""
        import struct  # noqa: PLC0415
        import time as _time  # noqa: PLC0415

        safe_globals = {
            "__builtins__": {},
            "struct": struct,
            "time": _time,
            "send_can": self._send_can,
            "log": self._log,
            "sleep": self._sleep,
        }
        if frame is not None:
            safe_globals["frame"] = frame
        return safe_globals

    def _execute(self, code: str, frame: Optional[Dict[str, Any]]) -> None:
        """Выполняет код в текущем потоке."""
        compiled = compile(code, "<script>", "exec")
        exec(compiled, self._build_safe_globals(frame))  # noqa: S102

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
        self._send_requests = []
        self._log_lines = []
        result: Dict[str, Any] = {"success": False, "logs": [], "send_requests": [], "error": None}

        exception_holder: List[Optional[BaseException]] = [None]

        def target() -> None:
            try:
                self._execute(code, frame)
            except BaseException as exc:  # noqa: BLE001
                exception_holder[0] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join(timeout=EXEC_TIMEOUT)

        if thread.is_alive():
            result["error"] = "Script execution timed out (3 seconds)"
            logger.warning("Скрипт превысил время выполнения")
            return result

        exc = exception_holder[0]
        if exc is not None:
            result["error"] = f"{type(exc).__name__}: {exc}"
            logger.warning("Ошибка выполнения скрипта: %s", result["error"])
            return result

        result["success"] = True
        result["logs"] = self._log_lines[:]
        result["send_requests"] = self._send_requests[:]
        return result
