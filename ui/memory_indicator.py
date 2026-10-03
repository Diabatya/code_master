"""Индикатор использования памяти и загрузки для вкладок приложения."""

import threading
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QWidget,
)

from models.config import Config
from models.translations import _ as tr

# ОЗУ STM32F105RC — 64 КБ (секторы .data/.bss/heap + рабочие кэши
# триггеров, переменных и шлюза).
RAM_TOTAL_BYTES = 65536


class _TelemetryPoller(QThread):
    """Фоновый опрос CMD_SYSTEM_INFO — один на все индикаторы.

    Живые «Загрузка ЦП»/«Загрузка ОЗУ» из МК (протокол 9): темп
    главного цикла и проценты занятости приходят сюда раз в секунду,
    дальше сигнал расходится на все видимые MemoryIndicator."""

    telemetry = Signal(dict)

    def __init__(self) -> None:
        super().__init__()
        self._stop_event = threading.Event()
        self._manager = None

    def set_manager(self, manager) -> None:
        self._manager = manager

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        while not self._stop_event.wait(1.0):
            mgr = self._manager
            if mgr is None:
                continue
            try:
                # Эмулятор отвечает только на команды бутлоадера —
                # SYSTEM_INFO ушёл бы в двухсекундный таймаут каждый раз.
                if getattr(mgr, "_last_emulation", False):
                    continue
                if not mgr.is_open():
                    continue
                # Не вклиниваемся в пачки команд записи/вычитки настроек:
                # request_control держит _lock, опрос между ними давал
                # бы каждой команде дополнительный таймаут (полевой баг
                # «прогрузка большого конфига с 5-10 раза»).
                if mgr.in_control_session:
                    continue
                info = mgr.read_system_info()
            except Exception:  # noqa: BLE001
                continue
            self.telemetry.emit(info)


class MemoryIndicator(QWidget):
    """Показывает процент использования памяти и загрузки устройства.

    Горизонтальная строка внизу вкладки (отчёт мастера):
    * «Память» — занятый пул Flash под конфигурацию/триггеры;
    * «ОЗУ» — онлайн: пиковая занятость RAM из CMD_SYSTEM_INFO
      (протокол 9); без связи — оценка по настроенной конфигурации;
    * «CPU» — онлайн: оценка занятости процессора по темпу главного
      цикла МК; без связи — оценка по числу активных функций."""

    _shared_poller: _TelemetryPoller | None = None

    @classmethod
    def attach_serial_manager(cls, manager) -> None:
        """Один вызов из главного окна — опрос делят все индикаторы."""
        if cls._shared_poller is None:
            cls._shared_poller = _TelemetryPoller()
            cls._shared_poller.start()
        cls._shared_poller.set_manager(manager)

    @classmethod
    def shutdown_poller(cls) -> None:
        poller = cls._shared_poller
        if poller is not None:
            poller.stop()
            # Ждём завершения run(): без wait() Qt при выходе
            # приложения падал с «QThread: Destroyed while thread is
            # still running» — поток ещё спал на _stop_event.
            poller.wait(2000)
            cls._shared_poller = None

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = Config()
        self._live = False  # приходят ли сейчас живые данные из МК
        self._create_widgets()
        if MemoryIndicator._shared_poller is not None:
            MemoryIndicator._shared_poller.telemetry.connect(self._on_telemetry)

    def _cell(self, title: str) -> QProgressBar:
        label = QLabel(title)
        label.setFont(QFont("Segoe UI", 9))
        bar = QProgressBar()
        bar.setFont(QFont("Segoe UI", 9))
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.setFixedWidth(180)
        self._row_layout.addWidget(label)
        self._row_layout.addWidget(bar)
        return bar

    def _create_widgets(self) -> None:
        self._row_layout = QHBoxLayout(self)
        self._row_layout.setSpacing(8)
        self._row_layout.setContentsMargins(0, 0, 0, 0)

        self._progress = self._cell(tr("Память:"))
        self._ram_bar = self._cell(tr("ОЗУ:"))
        self._cpu_bar = self._cell(tr("CPU:"))
        self._row_layout.addStretch()
        self._ram_bar.setToolTip(tr(
            "Онлайн с МК: пиковая занятость RAM (канарейка стека); "
            "без подключения — оценка по настроенной конфигурации"
        ))
        self._cpu_bar.setToolTip(tr(
            "Онлайн с МК: занятость процессора по темпу главного цикла; "
            "без подключения — оценка по настроенной конфигурации"
        ))

    def _on_telemetry(self, info: dict) -> None:
        """Живые проценты CPU/RAM из CMD_SYSTEM_INFO (протокол 9)."""
        if "cpu_load_pct" not in info and "ram_used_pct" not in info:
            return
        self._live = True
        cpu = min(100, max(0, int(info.get("cpu_load_pct", 0))))
        ram = min(100, max(0, int(info.get("ram_used_pct", 0))))
        self._cpu_bar.setValue(cpu)
        self._cpu_bar.setFormat(f"{cpu}%")
        self._ram_bar.setValue(ram)
        self._ram_bar.setFormat(f"{ram}%")
        rate = int(info.get("loop_rate_hz", 0))
        if rate:
            self._cpu_bar.setToolTip(
                tr("Онлайн с МК: темп главного цикла {0} ит/с").format(rate)
            )

    def update_usage(self, estimated_bytes: int) -> None:
        total = self._config.get("total_memory", 65536)
        if not total:
            total = 65536
        percent = min(100, max(0, int(estimated_bytes * 100 / total)))
        self._progress.setValue(percent)
        self._progress.setFormat(f"{percent}%")

    def update_load(
        self,
        features: int,
        ram_bytes: int,
    ) -> None:
        """Оценка текущей загрузки МК по настроенной конфигурации.

        ``features`` — число активных функций (триггеры, правила ГЛ,
        правила шлюза): каждая — ~3% циклов обработки кадра.
        ``ram_bytes`` — байты ОЗУ под кэши/буферы этих функций.
        Оценка рисуется только когда нет живой телеметрии из МК —
        иначе статическое число затирало бы онлайн-значение.
        """
        if self._live:
            return
        ram_pct = min(100, max(0, int(ram_bytes * 100 / RAM_TOTAL_BYTES)))
        cpu_pct = min(100, max(0, features * 3))
        self._ram_bar.setValue(ram_pct)
        self._ram_bar.setFormat(f"{ram_pct}%")
        self._cpu_bar.setValue(cpu_pct)
        self._cpu_bar.setFormat(f"{cpu_pct}%")

    def show_trigger_usage(
        self,
        used_slots: int,
        extra_features: int = 0,
    ) -> None:
        """Процент занятого пула Flash триггеров (8 КБ над config).

        Одинаковый смысл во всех вкладках: доля пула 0x0803E000–0x0803FFFF,
        занятая настроенными триггерами; пустое устройство — 0%.
        Попутно обновляет оценку ОЗУ/CPU: каждый триггер держит кэш
        последнего DATA и состояние условий в ОЗУ.
        """
        from core.trigger_protocol import (
            TRIGGER_HEADER_SIZE,
            TRIGGER_MAX_SLOTS,
            TRIGGER_POOL_SIZE,
            TRIGGER_SLOT_SIZE,
            trigger_usage_percent,
        )

        percent = trigger_usage_percent(used_slots)
        self._progress.setValue(percent)
        self._progress.setFormat(f"{percent}%")
        self._progress.setToolTip(
            tr("Триггеров: {0}/{1} ({2} из {3} Б Flash)").format(
                used_slots,
                TRIGGER_MAX_SLOTS,
                used_slots * TRIGGER_SLOT_SIZE + TRIGGER_HEADER_SIZE if used_slots else 0,
                TRIGGER_POOL_SIZE,
            )
        )
        self.update_load(
            used_slots + extra_features,
            # Кэш триггера в ОЗУ: последнее DATA + флаги условий ≈ 48 Б.
            used_slots * 48,
        )

    def estimate_bytes(self, data: Any) -> int:
        """Оценивает размер JSON-совместимой структуры в байтах."""
        try:
            return len(str(data).encode("utf-8"))
        except Exception:
            return 0

    def estimate_triggers(self, triggers: list[dict[str, Any]]) -> int:
        return self.estimate_bytes(triggers)

    def estimate_rules(self, rules: list[dict[str, Any]]) -> int:
        return self.estimate_bytes(rules)
