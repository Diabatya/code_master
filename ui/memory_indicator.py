"""Индикатор использования памяти и загрузки для вкладок приложения."""

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from models.config import Config
from models.translations import _ as tr

# ОЗУ STM32F105RC — 64 КБ (секторы .data/.bss/heap + рабочие кэши
# триггеров, переменных и шлюза).
RAM_TOTAL_BYTES = 65536


class MemoryIndicator(QWidget):
    """Показывает процент использования памяти и загрузки устройства.

    Три строки (отчёт мастера):
    * «Память» — занятый пул Flash под конфигурацию/триггеры;
    * «ОЗУ» — оценка оперативной памяти под кэши и буферы
      настроенных функций (телеметрии RAM в протоколе МК нет —
      значение расчётное, по конфигурации);
    * «Процессор» — оценка доли вычислений на обработку кадров:
      триггеры, гибкая логика и шлюз (тоже расчётная)."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._config = Config()
        self._create_widgets()

    def _row(self, title: str) -> QProgressBar:
        row = QHBoxLayout()
        row.setSpacing(8)
        label = QLabel(title)
        label.setFont(QFont("Segoe UI", 9))
        label.setFixedWidth(64)
        bar = QProgressBar()
        bar.setFont(QFont("Segoe UI", 9))
        bar.setRange(0, 100)
        bar.setValue(0)
        bar.setTextVisible(True)
        bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        bar.setFixedWidth(180)
        row.addWidget(label)
        row.addWidget(bar)
        row.addStretch()
        self._rows_layout.addLayout(row)
        return bar

    def _create_widgets(self) -> None:
        self._rows_layout = QVBoxLayout(self)
        self._rows_layout.setSpacing(2)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)

        self._progress = self._row(tr("Память:"))
        self._ram_bar = self._row(tr("ОЗУ:"))
        self._cpu_bar = self._row(tr("CPU:"))
        self._ram_bar.setToolTip(tr(
            "Оценка загрузки оперативной памяти по настроенной "
            "конфигурации (кэши триггеров, переменные, шлюз)"
        ))
        self._cpu_bar.setToolTip(tr(
            "Оценка загрузки процессора работой триггеров, "
            "гибкой логики и шлюза"
        ))

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
        """
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
