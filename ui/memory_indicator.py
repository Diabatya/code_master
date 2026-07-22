"""Индикатор использования памяти для вкладок приложения."""

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import QHBoxLayout, QLabel, QProgressBar, QWidget

from models.config import Config
from models.translations import _ as tr


class MemoryIndicator(QWidget):
    """Показывает процент использования памяти устройства."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = Config()
        self._create_widgets()

    def _create_widgets(self) -> None:
        layout = QHBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QLabel(tr("Память:"))
        self._label.setFont(QFont("Segoe UI", 9))

        self._progress = QProgressBar()
        self._progress.setFont(QFont("Segoe UI", 9))
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setTextVisible(True)
        self._progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._progress.setFixedWidth(180)

        layout.addWidget(self._label)
        layout.addWidget(self._progress)
        layout.addStretch()

    def update_usage(self, estimated_bytes: int) -> None:
        total = self._config.get("total_memory", 65536)
        if not total:
            total = 65536
        percent = min(100, max(0, int(estimated_bytes * 100 / total)))
        self._progress.setValue(percent)
        self._progress.setFormat(f"{percent}%")

    def estimate_bytes(self, data: Any) -> int:
        """Оценивает размер JSON-совместимой структуры в байтах."""
        try:
            return len(str(data).encode("utf-8"))
        except Exception:
            return 0

    def estimate_triggers(self, triggers: List[Dict[str, Any]]) -> int:
        return self.estimate_bytes(triggers)

    def estimate_rules(self, rules: List[Dict[str, Any]]) -> int:
        return self.estimate_bytes(rules)
