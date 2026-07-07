"""Вкладка графического анализа CAN-трафика."""

import time
from collections import Counter, deque
from typing import Dict, List, Optional

from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.dbc_manager import DBCManager
from core.serial_manager import SerialManager
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import format_data_bytes, int_to_hex

logger = get_logger(__name__)

MAX_FRAMES = 5000
WINDOW_SECONDS = 30


def _time_label(seconds: float) -> str:
    """Возвращает метку времени HH:MM:SS."""
    return time.strftime("%H:%M:%S", time.localtime(seconds))


class CanGraphTab(QWidget):
    """Вкладка визуализации CAN-трафика в реальном времени."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        """Создаёт вкладку графика.

        Args:
            serial_manager: Общий менеджер COM-порта.
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._dbc_manager = DBCManager()
        self._frames: deque[Dict[str, object]] = deque(maxlen=MAX_FRAMES)
        self._paused = False

        self._create_widgets()
        self._build_layout()
        self._setup_timer()

    def _create_widgets(self) -> None:
        """Создаёт элементы управления вкладки."""
        font = QFont("Segoe UI", 10)

        self._title = QLabel(tr("Графический анализатор CAN"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._channel_combo = QComboBox()
        self._channel_combo.setFont(font)
        self._channel_combo.addItems([tr("CAN1"), tr("CAN2"), tr("Оба")])
        self._channel_combo.setCurrentIndex(2)
        self._channel_combo.currentIndexChanged.connect(self._update_chart)

        self._mode_combo = QComboBox()
        self._mode_combo.setFont(font)
        self._mode_combo.addItems([tr("По ID"), tr("По частоте")])
        self._mode_combo.currentIndexChanged.connect(self._update_chart)

        self._pause_button = QPushButton(tr("Пауза"))
        self._pause_button.setFixedSize(80, 28)
        self._pause_button.setFont(font)
        self._pause_button.setCheckable(True)
        self._pause_button.clicked.connect(self._on_pause_clicked)

        self._clear_button = QPushButton(tr("Очистить"))
        self._clear_button.setFixedSize(80, 28)
        self._clear_button.setFont(font)
        self._clear_button.clicked.connect(self._clear)

        try:
            from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
            from matplotlib.figure import Figure

            self._figure = Figure(figsize=(8, 4), tight_layout=True)
            self._canvas = FigureCanvas(self._figure)
            self._axes = self._figure.add_subplot(111)
            self._matplotlib_available = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Matplotlib недоступен: %s", exc)
            self._canvas = QLabel(tr("График недоступен: matplotlib не установлен"))
            self._matplotlib_available = False
            self._figure = None
            self._axes = None

        self._table = QTableWidget()
        self._table.setColumnCount(5)
        self._table.setHorizontalHeaderLabels([tr("ID"), tr("Количество"), tr("Последнее время"), tr("Данные"), tr("Описание")])
        self._table.setFont(font)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setColumnWidth(0, 80)
        self._table.setColumnWidth(1, 80)
        self._table.setColumnWidth(2, 120)
        self._table.setColumnWidth(3, 160)
        self._table.setColumnWidth(4, 150)

        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_chart)

    def _build_layout(self) -> None:
        """Собирает компоновку вкладки."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        top_layout = QHBoxLayout()
        top_layout.addWidget(self._title)
        top_layout.addStretch()
        top_layout.addWidget(QLabel(tr("Канал:")))
        top_layout.addWidget(self._channel_combo)
        top_layout.addWidget(QLabel(tr("Режим:")))
        top_layout.addWidget(self._mode_combo)
        top_layout.addWidget(self._pause_button)
        top_layout.addWidget(self._clear_button)
        layout.addLayout(top_layout)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.addWidget(self._canvas)
        splitter.addWidget(self._table)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 1)
        layout.addWidget(splitter, 1)

    def _setup_timer(self) -> None:
        """Запускает таймер периодического обновления графика."""
        self._update_timer.start(500)

    def process_frame(self, frame: Dict[str, object]) -> None:
        """Получает новый CAN-кадр и добавляет его в буфер."""
        if self._paused:
            return
        self._frames.append({
            "time": time.time(),
            "id": int(frame["id"]),
            "channel": int(frame["channel"]),
            "data": bytes(frame["data"]),
        })

    def _on_pause_clicked(self, checked: bool) -> None:
        """Приостанавливает/возобновляет обновление графика."""
        self._paused = checked
        self._pause_button.setText(tr("Продолжить") if checked else tr("Пауза"))

    def _clear(self) -> None:
        """Очищает буфер кадров, график и таблицу."""
        self._frames.clear()
        self._update_table({})
        if self._matplotlib_available and self._axes is not None:
            self._axes.clear()
            self._canvas.draw()

    def _selected_channel(self) -> Optional[int]:
        """Возвращает выбранный канал: 1, 2 или None (оба)."""
        index = self._channel_combo.currentIndex()
        if index == 0:
            return 1
        if index == 1:
            return 2
        return None

    def _update_chart(self) -> None:
        """Обновляет график и таблицу в зависимости от режима."""
        if not self._matplotlib_available or self._axes is None:
            return

        channel = self._selected_channel()
        now = time.time()
        cutoff = now - WINDOW_SECONDS
        frames = [f for f in self._frames if f["time"] >= cutoff and (channel is None or f["channel"] == channel)]

        self._axes.clear()
        mode = self._mode_combo.currentIndex()

        if mode == 0:  # По ID
            times = [f["time"] for f in frames]
            ids = [f["id"] for f in frames]
            colors = ["#6C8CFF" if f["channel"] == 1 else "#4CAF50" for f in frames]
            self._axes.scatter(times, ids, c=colors, s=15, alpha=0.7)
            self._axes.set_ylabel(tr("ID пакета"))
            self._axes.set_xlabel(tr("Время"))
            if self._dbc_manager.is_loaded() and ids:
                unique_ids = sorted(set(ids))
                labels = [(self._dbc_manager.get_message(can_id) or {}).get("name", int_to_hex(can_id, 8 if can_id > 0x7FF else 3)) for can_id in unique_ids]
                self._axes.set_yticks(unique_ids)
                self._axes.set_yticklabels(labels, fontsize=8)
        else:  # По частоте
            buckets: Dict[int, int] = Counter(int(f["time"]) for f in frames)
            if buckets:
                sorted_times = sorted(buckets.keys())
                rates = [buckets[t] for t in sorted_times]
                self._axes.plot(sorted_times, rates, color="#6C8CFF", linewidth=2)
            self._axes.set_ylabel(tr("Пакетов/с"))
            self._axes.set_xlabel(tr("Время"))

        self._axes.set_title(self._mode_combo.currentText())
        self._figure.autofmt_xdate()
        self._canvas.draw()

        self._update_stats(frames)

    def _update_stats(self, frames: List[Dict[str, object]]) -> None:
        """Обновляет таблицу статистики по ID."""
        stats: Dict[int, Dict[str, object]] = {}
        for f in frames:
            can_id = f["id"]
            if can_id not in stats:
                stats[can_id] = {"count": 0, "last_time": f["time"], "data": f["data"]}
            stats[can_id]["count"] += 1
            if f["time"] >= stats[can_id]["last_time"]:
                stats[can_id]["last_time"] = f["time"]
                stats[can_id]["data"] = f["data"]
        self._update_table(stats)

    def _update_table(self, stats: Dict[int, Dict[str, object]]) -> None:
        """Перезаполняет таблицу статистики."""
        self._table.setRowCount(len(stats))
        for i, can_id in enumerate(sorted(stats.keys())):
            info = stats[can_id]
            id_text = int_to_hex(can_id, 8 if can_id > 0x7FF else 3)
            data_text = " ".join(format_data_bytes(info["data"]))
            message = self._dbc_manager.get_message(can_id)
            description = message.get("name", "") if message else ""
            tooltip = self._dbc_manager.describe_frame(can_id, info["data"]) if self._dbc_manager.is_loaded() else ""

            id_item = QTableWidgetItem(id_text)
            id_item.setToolTip(tooltip)
            self._table.setItem(i, 0, id_item)
            count_item = QTableWidgetItem(str(info["count"]))
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(i, 1, count_item)
            self._table.setItem(i, 2, QTableWidgetItem(_time_label(info["last_time"])))
            data_item = QTableWidgetItem(data_text)
            data_item.setToolTip(tooltip)
            self._table.setItem(i, 3, data_item)
            desc_item = QTableWidgetItem(description)
            desc_item.setToolTip(tooltip)
            self._table.setItem(i, 4, desc_item)
