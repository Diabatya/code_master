"""Автоанализатор CAN-шины с тепловой картой и экспортом."""

import csv
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontMetrics
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.serial_manager import SerialManager
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import format_data_bytes, int_to_hex

logger = get_logger(__name__)


class CanAnalyzer(QWidget):
    """Виджет автоматического анализа CAN-трафика."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        """Создаёт анализатор.

        Args:
            serial_manager: Менеджер COM-порта (для получения кадров).
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._create_widgets()
        self._build_layout()
        self._reset_stats()

    def _create_widgets(self) -> None:
        """Создаёт элементы управления."""
        font = QFont("Segoe UI", 10)

        self._title = QLabel(tr("Автоанализатор CAN-шины"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._duration_label = QLabel(tr("Длительность анализа, с:"))
        self._duration_label.setFont(font)
        self._duration_spin = QSpinBox()
        self._duration_spin.setRange(5, 300)
        self._duration_spin.setValue(30)
        self._duration_spin.setFont(font)
        self._duration_spin.setSuffix(tr(" с"))

        self._start_button = QPushButton(tr("Начать анализ"))
        self._start_button.setFont(font)
        self._start_button.setFixedSize(120, 30)
        self._start_button.clicked.connect(self._on_start_stop)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFont(font)
        self._progress.setTextVisible(True)

        self._track_changes_check = QPushButton(tr("Отслеживать изменения"))
        self._track_changes_check.setFont(font)
        self._track_changes_check.setCheckable(True)
        self._track_changes_check.setChecked(True)
        self._track_changes_check.setFixedSize(160, 28)

        self._export_csv_button = QPushButton(tr("Экспорт CSV"))
        self._export_csv_button.setFont(font)
        self._export_csv_button.setFixedSize(110, 28)
        self._export_csv_button.clicked.connect(self._export_csv)

        self._export_html_button = QPushButton(tr("Экспорт HTML"))
        self._export_html_button.setFont(font)
        self._export_html_button.setFixedSize(110, 28)
        self._export_html_button.clicked.connect(self._export_html)

        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(
            [tr("ID"), tr("Частота, пак/с"), tr("Min DLC"), tr("Max DLC"), tr("Пример данных"), tr("Изменения")]
        )
        self._table.setFont(font)
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self._table.setColumnWidth(0, 90)
        self._table.setColumnWidth(1, 120)
        self._table.setColumnWidth(2, 70)
        self._table.setColumnWidth(3, 70)
        self._table.setColumnWidth(4, 180)
        self._table.setColumnWidth(5, 90)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)

    def _build_layout(self) -> None:
        """Собирает компоновку виджета."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._title)

        top_layout = QHBoxLayout()
        top_layout.setSpacing(8)
        top_layout.addWidget(self._duration_label)
        top_layout.addWidget(self._duration_spin)
        top_layout.addWidget(self._start_button)
        top_layout.addWidget(self._progress)
        top_layout.addStretch()
        top_layout.addWidget(self._track_changes_check)
        top_layout.addWidget(self._export_csv_button)
        top_layout.addWidget(self._export_html_button)
        layout.addLayout(top_layout)
        layout.addWidget(self._table, 1)

    def _reset_stats(self) -> None:
        """Сбрасывает статистику анализа."""
        self._stats: Dict[int, Dict[str, Any]] = defaultdict(
            lambda: {
                "count": 0,
                "first_time": None,
                "last_time": None,
                "min_dlc": 8,
                "max_dlc": 0,
                "sample_data": b"",
                "data_changed": False,
                "last_data": None,
            }
        )
        self._analyzing = False
        self._start_time = 0.0
        self._duration = 0.0

    def process_frame(self, frame: Dict[str, Any]) -> None:
        """Получает новый CAN-кадр и обновляет статистику.

        Args:
            frame: Распакованный CAN-кадр.
        """
        if not self._analyzing:
            return
        can_id = int(frame.get("id", 0))
        data = bytes(frame.get("data", b""))
        now = time.time()
        info = self._stats[can_id]
        info["count"] += 1
        if info["first_time"] is None:
            info["first_time"] = now
        info["last_time"] = now
        dlc = len(data)
        info["min_dlc"] = min(info["min_dlc"], dlc)
        info["max_dlc"] = max(info["max_dlc"], dlc)
        if info["sample_data"] == b"" or self._track_changes_check.isChecked():
            if info["last_data"] is not None and info["last_data"] != data:
                info["data_changed"] = True
            info["last_data"] = data
        if info["sample_data"] == b"":
            info["sample_data"] = data

    def _on_start_stop(self) -> None:
        """Запускает или останавливает анализ."""
        if self._analyzing:
            self._stop_analysis()
        else:
            self._start_analysis()

    def _start_analysis(self) -> None:
        """Начинает сбор статистики."""
        self._reset_stats()
        self._analyzing = True
        self._start_time = time.time()
        self._duration = self._duration_spin.value()
        self._progress.setValue(0)
        self._start_button.setText(tr("Остановить анализ"))
        self._table.setRowCount(0)
        self._timer.start(100)
        logger.info("Анализ CAN-шины запущен на %d секунд", self._duration)

    def _stop_analysis(self) -> None:
        """Останавливает анализ и обновляет таблицу."""
        self._analyzing = False
        self._timer.stop()
        self._progress.setValue(100)
        self._start_button.setText(tr("Начать анализ"))
        self._update_table()
        logger.info("Анализ CAN-шины завершён, уникальных ID: %d", len(self._stats))

    def _on_tick(self) -> None:
        """Обновляет прогресс-бар и таблицу во время анализа."""
        elapsed = time.time() - self._start_time
        progress = min(100, int(elapsed / self._duration * 100))
        self._progress.setValue(progress)
        if elapsed >= self._duration:
            self._stop_analysis()
            return
        if self._table.rowCount() == 0 or progress % 10 == 0:
            self._update_table()

    def _update_table(self) -> None:
        """Перезаполняет таблицу статистики."""
        stats = dict(self._stats)
        if not stats:
            self._table.setRowCount(0)
            return

        elapsed = time.time() - self._start_time if self._analyzing else self._duration
        elapsed = max(1e-6, elapsed)
        max_rate = max(info["count"] / elapsed for info in stats.values())

        self._table.setRowCount(len(stats))
        for row, can_id in enumerate(sorted(stats.keys())):
            info = stats[can_id]
            rate = info["count"] / elapsed

            id_item = QTableWidgetItem(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
            id_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 0, id_item)

            rate_item = QTableWidgetItem(f"{rate:.1f}")
            rate_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 1, rate_item)
            self._color_rate_cell(rate_item, rate, max_rate)

            min_item = QTableWidgetItem(str(info["min_dlc"]))
            min_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 2, min_item)

            max_item = QTableWidgetItem(str(info["max_dlc"]))
            max_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self._table.setItem(row, 3, max_item)

            data_text = " ".join(format_data_bytes(info["sample_data"]))
            self._table.setItem(row, 4, QTableWidgetItem(data_text))

            changed_text = tr("Да") if info["data_changed"] else tr("Нет")
            changed_item = QTableWidgetItem(changed_text)
            changed_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if info["data_changed"]:
                changed_item.setForeground(QColor("#FF9800"))
                font = changed_item.font()
                font.setBold(True)
                changed_item.setFont(font)
            self._table.setItem(row, 5, changed_item)

    def _color_rate_cell(self, item: QTableWidgetItem, rate: float, max_rate: float) -> None:
        """Раскрашивает ячейку частоты в зависимости от нагрузки."""
        ratio = rate / max_rate if max_rate > 0 else 0
        # от синего (холодный) к красному (горячий)
        r = int(255 * ratio)
        b = int(255 * (1 - ratio))
        g = int(128 * (1 - abs(ratio - 0.5) * 2))
        color = QColor(r, g, b)
        item.setBackground(color)
        # текст контрастный
        luminance = 0.299 * r + 0.587 * g + 0.114 * b
        item.setForeground(QColor("#FFFFFF") if luminance < 128 else QColor("#000000"))

    def _export_csv(self) -> None:
        """Экспортирует таблицу в CSV-файл."""
        path, _ = QFileDialog.getSaveFileName(self, tr("Экспорт CSV"), "", "CSV files (*.csv)")
        if not path:
            return
        if not path.endswith(".csv"):
            path += ".csv"
        try:
            with Path(path).open("w", encoding="utf-8", newline="") as file:
                writer = csv.writer(file)
                writer.writerow([tr("ID"), tr("Частота, пак/с"), tr("Min DLC"), tr("Max DLC"), tr("Пример данных"), tr("Изменения")])
                for row in range(self._table.rowCount()):
                    writer.writerow([self._table.item(row, col).text() for col in range(self._table.columnCount())])
            QMessageBox.information(self, tr("Готово"), tr("Таблица экспортирована в {0}").format(path))
            logger.info("Анализ экспортирован в CSV: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка экспорта CSV: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось экспортировать CSV: {0}").format(exc))

    def _export_html(self) -> None:
        """Экспортирует таблицу в HTML-файл."""
        path, _ = QFileDialog.getSaveFileName(self, tr("Экспорт HTML"), "", "HTML files (*.html)")
        if not path:
            return
        if not path.endswith(".html"):
            path += ".html"
        try:
            lines = [
                "<!DOCTYPE html>",
                "<html><head><meta charset='utf-8'><title>CAN Bus Analysis</title></head><body>",
                "<h1>CAN Bus Analysis</h1>",
                "<table border='1' cellpadding='4' cellspacing='0'>",
                "<tr><th>ID</th><th>Rate</th><th>Min DLC</th><th>Max DLC</th><th>Sample Data</th><th>Changed</th></tr>",
            ]
            for row in range(self._table.rowCount()):
                cells = [self._table.item(row, col).text() for col in range(self._table.columnCount())]
                bg = self._table.item(row, 1).background().color().name() if self._table.item(row, 1) else "#FFFFFF"
                changed = self._table.item(row, 5)
                changed_style = "font-weight:bold; color:#FF9800;" if changed and changed.text() == tr("Да") else ""
                lines.append(
                    f"<tr style='background-color:{bg}; {changed_style}'>"
                    f"<td>{cells[0]}</td><td>{cells[1]}</td><td>{cells[2]}</td><td>{cells[3]}</td><td>{cells[4]}</td><td>{cells[5]}</td></tr>"
                )
            lines.append("</table></body></html>")
            Path(path).write_text("\n".join(lines), encoding="utf-8")
            QMessageBox.information(self, tr("Готово"), tr("Таблица экспортирована в {0}").format(path))
            logger.info("Анализ экспортирован в HTML: %s", path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка экспорта HTML: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось экспортировать HTML: {0}").format(exc))
