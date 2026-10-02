"""Трэйс CAN-шины: две хронологические таблицы для CAN1 и CAN2."""

import csv
import re
import time
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import pack_can_frame
from core.dbc_manager import DBCManager
from ui.can_monitor_tab import _tx_echo_colors
from core.serial_manager import SerialManager
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import format_data_bytes, hex_to_int, int_to_hex, parse_packet_string

logger = get_logger(__name__)

# Кольцевой лимит строк трейса: 8 QTableWidgetItem на строку —
# 50k строк это ~400k объектов и заметная доля ОЗУ на длинном приёме.
# 20k строк по-прежнему покрывают ~20 с шины на 1000 кадр/с; старый
# лог при этом остаётся доступным через экспорт .trace/CSV.
MAX_TABLE_ROWS = 20_000


def _ascii_from_data(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


def translate_to_bytes(text: str) -> bytes:
    """Ввод оператора → последовательность байт для поиска в DATA.

    Правила (в порядке приоритета):
      • «0x…», hex с пробелами («46 4E») или hex с буквами A–F («5464E»)
        — разбор как hex, нечётную длину добиваем нулём слева
        («5464E» → 05 46 4E);
      • только цифры («345678») — десятичное число в минимальные
        big-endian байты (345678 → 05 46 4E);
      • остальное (латиница/символы, VIN и т.п.) — UTF-8 байты текста.
    """
    s = text.strip()
    if not s:
        return b""
    if s.lower().startswith("0x"):
        s = s[2:]
        if not s:
            return b""
    if " " in s or "," in s:
        # hex-токены через пробел/запятую
        try:
            return bytes(int(tok, 16) & 0xFF for tok in re.split(r"[ ,]+", s) if tok)
        except ValueError:
            return s.encode("utf-8")
    if re.fullmatch(r"[0-9A-Fa-f]+", s):
        if re.search(r"[A-Fa-f]", s):
            # hex-строка с буквами — однозначно байты
            h = s if len(s) % 2 == 0 else "0" + s
            return bytes.fromhex(h)
        # чистые цифры — десятичное число → big-endian
        value = int(s, 10)
        if value == 0:
            return b"\x00"
        length = max(1, (value.bit_length() + 7) // 8)
        return value.to_bytes(length, "big")
    # текст/VIN — байты символов
    return s.encode("utf-8")


class _TranslatedSearchDialog(QDialog):
    """«Поиск с переводом»: ввод → автоперевод в байты → подсветка
    совпадений в трейсе. Немодальный — оператор листает лог параллельно."""

    def __init__(self, analyzer: "CanAnalyzer") -> None:
        super().__init__(analyzer)
        self._analyzer = analyzer
        self.setWindowTitle(tr("Поиск с переводом"))
        self.setMinimumWidth(420)
        font = QFont("Segoe UI", 9)
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel(tr(
            "Введите латинские символы или цифры для поиска"
        )))
        self._edit = QLineEdit()
        self._edit.setFont(font)
        self._edit.textChanged.connect(self._update_preview)
        self._edit.returnPressed.connect(self._find)
        layout.addWidget(self._edit)

        self._preview = QLabel("→ —")
        self._preview.setFont(QFont("Consolas", 10))
        layout.addWidget(self._preview)

        self._status = QLabel("")
        self._status.setFont(font)
        layout.addWidget(self._status)

        row = QHBoxLayout()
        find_btn = QPushButton(tr("Найти"))
        next_btn = QPushButton(tr("Следующее"))
        close_btn = QPushButton(tr("Закрыть"))
        for b in (find_btn, next_btn, close_btn):
            b.setFont(font)
        find_btn.clicked.connect(self._find)
        next_btn.clicked.connect(self._next)
        close_btn.clicked.connect(self.close)
        row.addWidget(find_btn)
        row.addWidget(next_btn)
        row.addStretch()
        row.addWidget(close_btn)
        layout.addLayout(row)

    def _update_preview(self, text: str) -> None:
        seq = translate_to_bytes(text)
        self._preview.setText(
            "→ " + " ".join(f"{b:02X}" for b in seq) if seq else "→ —"
        )

    def _find(self) -> None:
        found = self._analyzer.search_translated(self._edit.text())
        if found == 0:
            self._status.setText(tr("Не найдено"))
        elif self._edit.text().strip():
            self._status.setText(tr("Совпадений: {0}").format(found))

    def _next(self) -> None:
        self._analyzer.scroll_to_next_hit()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._analyzer.clear_search_marks()
        super().closeEvent(event)


class CanAnalyzer(QWidget):
    """Виджет трэйса CAN-шины с двумя таблицами."""

    def __init__(self, serial_manager: SerialManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._dbc_manager = DBCManager()
        self._analyzing = False
        self._start_time = 0.0
        # Период по паре (канал, ID) — одинаковый ID в CAN1 и CAN2 имеет
        # свои метки времени, и «Очистить» канала сбрасывает только его.
        self._id_last_time: dict[tuple[int, int], float] = {}
        # Обратная отправка строк таблицы в шину: очередь строк,
        # таймер рассылки и позиция «по кадрам» — на каждую таблицу.
        self._send_queues: dict[QTableWidget, list[int]] = {}
        self._send_timers: dict[QTableWidget, QTimer] = {}
        self._step_rows: dict[QTableWidget, list[int]] = {}
        self._step_pos: dict[QTableWidget, int] = {}
        self._table_channel: dict[QTableWidget, int] = {}
        self._send_buttons: dict[QTableWidget, tuple] = {}
        self._send_timed: dict[QTableWidget, bool] = {}
        self._send_prev_ms: dict[QTableWidget, float | None] = {}
        self._clear_buttons: dict[QTableWidget, QPushButton] = {}
        # Приём батчится: per-frame insertRow + scrollToBottom на
        # насыщенной шине (сотни кадров/с) вместе с removeRow(0) по
        # кольцевому буферу давали главную долю нагрузки UI-потока.
        # Кадры копятся в очереди и вставляются одной пачкой ~10 раз/с.
        self._pending_rows: dict[QTableWidget, list[list[str]]] = {}
        self._pending_dirs: dict[QTableWidget, list[bool]] = {}
        self._trace_timer = QTimer(self)
        self._trace_timer.setInterval(100)
        self._trace_timer.timeout.connect(self._flush_trace_rows)
        # Подсветка найденных «Поиском с переводом» строк: item→фон,
        # чтобы вернуть исходный цвет при следующем поиске.
        self._search_marks: list[tuple[QTableWidgetItem, Any]] = []
        self._search_hits: list[tuple[QTableWidget, int]] = []
        self._search_hit_pos = 0
        self._search_dialog: QWidget | None = None
        self._create_widgets()
        self._build_layout()

    def retranslate_ui(self) -> None:
        self._start_button.setText(tr("Начать анализ"))
        self._stop_button.setText(tr("Завершить анализ"))
        self._export_csv_button.setText(tr("Экспорт CSV"))
        self._export_custom_button.setText(tr("Экспорт .trace"))
        self._search_button.setText(tr("Поиск с переводом"))
        for table, btn in self._clear_buttons.items():
            btn.setText(tr("Очистить"))
            btn.setToolTip(
                tr("Очистить принятые пакеты CAN{0}").format(
                    self._table_channel.get(table, "?")
                )
            )
        self._title.setText(tr("Трэйс CAN-шины"))
        for table in (self._table1, self._table2):
            table.setHorizontalHeaderLabels(self._column_titles())

    def set_dbc(self, dbc_manager) -> None:
        self._dbc_manager = dbc_manager

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)
        self._title = QLabel(tr("Трэйс CAN-шины"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._start_button = QPushButton(tr("Начать анализ"))
        self._start_button.setFont(font)
        self._start_button.setMinimumHeight(32)
        self._start_button.clicked.connect(self._start_analysis)

        self._stop_button = QPushButton(tr("Завершить анализ"))
        self._stop_button.setFont(font)
        self._stop_button.setMinimumHeight(32)
        self._stop_button.clicked.connect(self._stop_analysis)

        self._export_csv_button = QPushButton(tr("Экспорт CSV"))
        self._export_csv_button.setFont(font)
        self._export_csv_button.setMinimumHeight(32)
        self._export_csv_button.clicked.connect(self._export_csv)

        self._export_custom_button = QPushButton(tr("Экспорт .trace"))
        self._export_custom_button.setFont(font)
        self._export_custom_button.setMinimumHeight(32)
        self._export_custom_button.clicked.connect(self._export_custom)

        self._load_button = QPushButton(tr("Загрузить лог…"))
        self._load_button.setFont(font)
        self._load_button.setMinimumHeight(32)
        self._load_button.setToolTip(
            tr("Загрузить .trace/CSV в таблицы — дальше «Отправить»/«По кадрам» воспроизводят его в шину")
        )
        self._load_button.clicked.connect(self._load_log)

        self._search_button = QPushButton(tr("Поиск с переводом"))
        self._search_button.setFont(font)
        self._search_button.setMinimumHeight(32)
        self._search_button.setToolTip(
            tr("Введите число (345678 → 05 46 4E), hex (5464E / 46 4E) "
               "или текст (VIN — ищется как ASCII-байты) — совпадения "
               "подсвечиваются в трейсе")
        )
        self._search_button.clicked.connect(self._open_translated_search)

        self._table1 = self._build_table(font)
        self._table2 = self._build_table(font)
        self._table_channel[self._table1] = 1
        self._table_channel[self._table2] = 2
        self._panel1 = self._build_table_panel(self._table1)
        self._panel2 = self._build_table_panel(self._table2)

    @staticmethod
    def _column_titles() -> list[str]:
        """Заголовки колонок трейса. Последняя колонка — «Пояснение»
        (отчёт мастера: у неё не было имени)."""
        return [
            tr("Время"), tr("ID"), tr("DLC"), tr("DATA"), tr("Период"),
            tr("ASCII"), tr("Направление"), tr("Пояснение"),
        ]

    def _build_table(self, font: QFont) -> QTableWidget:
        table = QTableWidget()
        table.setColumnCount(8)
        table.setHorizontalHeaderLabels(self._column_titles())
        table.setFont(font)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(self._show_context_menu)
        # Разрешаем выделение нескольких строк: выделенный сегмент
        # отправляется в шину кнопкой «Отправить».
        table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        table.setColumnWidth(0, 90)
        table.setColumnWidth(1, 90)
        table.setColumnWidth(2, 50)
        table.setColumnWidth(3, 220)
        table.setColumnWidth(4, 90)
        table.setColumnWidth(5, 90)
        table.setColumnWidth(6, 80)
        table.setColumnWidth(7, 260)
        return table

    def _build_table_panel(self, table: QTableWidget) -> QWidget:
        """Таблица + строка кнопок отправки принятых кадров обратно в шину."""
        font = QFont("Segoe UI", 9)
        channel = self._table_channel.get(table, 1)
        send_btn = QPushButton(tr("Отправить"))
        replay_btn = QPushButton(tr("Replay ⏱"))
        stop_btn = QPushButton(tr("Стоп"))
        step_btn = QPushButton(tr("По кадрам"))
        clear_btn = QPushButton(tr("Очистить"))
        for btn in (send_btn, replay_btn, stop_btn, step_btn, clear_btn):
            btn.setFont(font)
            btn.setFixedHeight(26)
        send_btn.setToolTip(
            tr("Отправить все принятые кадры обратно в шину. "
               "Если строки выделены — только выделенные.")
        )
        replay_btn.setToolTip(
            tr("Воспроизвести кадры с исходными таймингами из колонки времени")
        )
        step_btn.setToolTip(tr("Каждое нажатие отправляет следующий кадр"))
        clear_btn.setToolTip(
            tr("Очистить принятые пакеты CAN{0}").format(channel)
        )
        stop_btn.setEnabled(False)
        send_btn.clicked.connect(lambda _c=False, t=table: self._send_all(t, timed=False))
        replay_btn.clicked.connect(lambda _c=False, t=table: self._send_all(t, timed=True))
        stop_btn.clicked.connect(lambda _c=False, t=table: self._stop_sending(t))
        step_btn.clicked.connect(lambda _c=False, t=table: self._send_next_frame(t))
        clear_btn.clicked.connect(lambda _c=False, t=table: self._clear_channel(t))
        self._send_buttons[table] = (send_btn, stop_btn, step_btn)
        self._send_timed[table] = False
        self._clear_buttons[table] = clear_btn

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 4, 0, 0)
        buttons.addWidget(clear_btn)
        buttons.addWidget(send_btn)
        buttons.addWidget(replay_btn)
        buttons.addWidget(stop_btn)
        buttons.addWidget(step_btn)
        buttons.addStretch()

        panel = QWidget()
        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        panel_layout.addLayout(buttons)
        panel_layout.addWidget(table)
        return panel

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._title)

        top_layout = QHBoxLayout()
        top_layout.setSpacing(8)
        top_layout.addWidget(self._start_button)
        top_layout.addWidget(self._stop_button)
        top_layout.addWidget(self._export_csv_button)
        top_layout.addWidget(self._export_custom_button)
        top_layout.addWidget(self._load_button)
        top_layout.addWidget(self._search_button)
        top_layout.addStretch()
        layout.addLayout(top_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._panel1)
        splitter.addWidget(self._panel2)
        splitter.setSizes([450, 450])
        layout.addWidget(splitter, 1)

    def _start_analysis(self) -> None:
        if not self._analyzing:
            self._start_time = time.time()
            self._id_last_time.clear()
        self._analyzing = True
        self._start_button.setEnabled(False)
        self._stop_button.setEnabled(True)
        logger.info("Трэйс запущен")

    def _stop_analysis(self) -> None:
        self._analyzing = False
        # Остаток очереди — в таблицы, чтобы «Поиск с переводом» видел
        # все пойманные кадры, а не только выведенные.
        self._flush_trace_rows()
        self._start_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._table1.scrollToBottom()
        self._table2.scrollToBottom()
        logger.info("Трэйс остановлен")

    # ---- «Поиск с переводом» -------------------------------------------

    def _open_translated_search(self) -> None:
        if self._search_dialog is not None:
            try:
                self._search_dialog.raise_()
                self._search_dialog.activateWindow()
                return
            except RuntimeError:
                self._search_dialog = None
        self._search_dialog = _TranslatedSearchDialog(self)
        self._search_dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._search_dialog.show()

    def clear_search_marks(self) -> None:
        """Возвращает исходные фоны подсвеченным ячейкам."""
        for item, brush in self._search_marks:
            try:
                item.setBackground(brush)
            except RuntimeError:
                continue
        self._search_marks.clear()
        self._search_hits.clear()
        self._search_hit_pos = 0

    def search_translated(self, text: str) -> int:
        """Ищет переведённые байты в DATA обеих таблиц и подсвечивает
        строки с совпадениями. Возвращает число найденных строк."""
        self._flush_trace_rows()  # слить очередь — ищем по всем кадрам
        self.clear_search_marks()
        seq = translate_to_bytes(text)
        if not seq:
            return 0
        mark = QColor("#4A6B9E")
        for table in (self._table1, self._table2):
            for row in range(table.rowCount()):
                data_item = table.item(row, 3)
                if data_item is None:
                    continue
                try:
                    row_bytes = bytes(
                        int(part, 16) for part in data_item.text().split()
                    )
                except ValueError:
                    continue
                if seq not in row_bytes:
                    continue
                self._search_hits.append((table, row))
                for col in range(table.columnCount()):
                    item = table.item(row, col)
                    if item is None:
                        continue
                    self._search_marks.append((item, item.background()))
                    item.setBackground(mark)
        if self._search_hits:
            table, row = self._search_hits[0]
            item = table.item(row, 3)
            if item is not None:
                table.scrollToItem(item)
        self._search_hit_pos = 0
        return len(self._search_hits)

    def scroll_to_next_hit(self) -> None:
        if not self._search_hits:
            return
        self._search_hit_pos = (self._search_hit_pos + 1) % len(self._search_hits)
        table, row = self._search_hits[self._search_hit_pos]
        item = table.item(row, 3)
        if item is not None:
            table.scrollToItem(item)

    def process_frame(self, frame: dict[str, Any]) -> None:
        if not self._analyzing:
            return
        channel = int(frame.get("channel", 0))
        table = self._table1 if channel == 1 else self._table2
        can_id = int(frame.get("id", 0))
        data = bytes(frame.get("data", b""))
        now = time.time()
        elapsed_text = f"{now - self._start_time:.3f}"
        last_time = self._id_last_time.get((channel, can_id))
        period_text = f"{int((now - last_time) * 1000)} ms" if last_time else ""
        self._id_last_time[(channel, can_id)] = now

        id_text = int_to_hex(can_id, 8 if can_id > 0x7FF else 3)
        data_text = " ".join(format_data_bytes(data))
        ascii_text = _ascii_from_data(data.ljust(8, b"\x00"))
        explanation = ""
        if self._dbc_manager.is_loaded():
            explanation = self._dbc_manager.describe_frame(can_id, data)

        is_tx = bool(frame.get("tx_echo", False))
        dir_text = "TX" if is_tx else "RX"
        self._pending_rows.setdefault(table, []).append(
            [elapsed_text, id_text, str(len(data)), data_text,
             period_text, ascii_text, dir_text, explanation]
        )
        self._pending_dirs.setdefault(table, []).append(is_tx)
        if not self._trace_timer.isActive():
            self._trace_timer.start()

    def _flush_trace_rows(self) -> None:
        """Сливает накопленные кадры в таблицы одной пачкой: вставки без
        промежуточных перерисовок, одна прокрутка на всю порцию."""
        any_flush = False
        for table in (self._table1, self._table2):
            rows = self._pending_rows.pop(table, None)
            dirs = self._pending_dirs.pop(table, None)
            if not rows:
                continue
            any_flush = True
            bg, fg = _tx_echo_colors()
            table.setUpdatesEnabled(False)
            try:
                for values, is_tx in zip(rows, dirs, strict=True):
                    if table.rowCount() >= MAX_TABLE_ROWS:
                        table.removeRow(0)
                    row = table.rowCount()
                    table.insertRow(row)
                    for col, text in enumerate(values):
                        item = QTableWidgetItem(text)
                        item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                        if is_tx:
                            item.setBackground(bg)
                            item.setForeground(fg)
                        table.setItem(row, col, item)
            finally:
                table.setUpdatesEnabled(True)
            scrollbar = table.verticalScrollBar()
            if scrollbar.value() >= scrollbar.maximum() - 4 - len(rows) * 30:
                table.scrollToBottom()
        if not any_flush:
            self._trace_timer.stop()

    def _add_trace_row(self, table: QTableWidget, frame: dict[str, Any]) -> None:
        """Прямая вставка строки (холодный путь — загрузка логов)."""
        can_id = int(frame.get("id", 0))
        data = bytes(frame.get("data", b""))
        now = time.time()
        elapsed = now - self._start_time
        elapsed_text = f"{elapsed:.3f}"
        channel = self._table_channel.get(table, 1)
        last_time = self._id_last_time.get((channel, can_id))
        period_text = f"{int((now - last_time) * 1000)} ms" if last_time else ""
        self._id_last_time[(channel, can_id)] = now

        id_width = 8 if can_id > 0x7FF else 3
        id_text = int_to_hex(can_id, id_width)
        dlc_text = str(len(data))
        data_text = " ".join(format_data_bytes(data))
        ascii_text = _ascii_from_data(data.ljust(8, b"\x00"))
        explanation = ""
        if self._dbc_manager.is_loaded():
            explanation = self._dbc_manager.describe_frame(can_id, data)

        if table.rowCount() >= MAX_TABLE_ROWS:
            table.removeRow(0)
        row = table.rowCount()
        table.insertRow(row)
        # Направление: TX — кадр отправлен самим МК (tx_echo — ответ
        # триггера/программы МК или ретрансляция кадра ПК); RX — приём
        # с шины. Цвет TX-строк — тот же, что в мониторинге:
        # тёмная тема — оранжевый фон, светлая — чёрный.
        is_tx = bool(frame.get("tx_echo", False))
        dir_text = "TX" if is_tx else "RX"
        bg, fg = _tx_echo_colors()
        values = [elapsed_text, id_text, dlc_text, data_text, period_text, ascii_text, dir_text, explanation]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_tx:
                item.setBackground(bg)
                item.setForeground(fg)
            table.setItem(row, col, item)
        table.scrollToBottom()

    # ---- Обратная отправка кадров в шину -------------------------------

    def _target_rows(self, table: QTableWidget) -> list[int]:
        """Выделенные строки по возрастанию; без выделения — вся таблица."""
        selected = sorted({idx.row() for idx in table.selectedIndexes()})
        return selected if selected else list(range(table.rowCount()))

    def _row_to_packet(self, table: QTableWidget, row: int) -> bytes | None:
        """Строка таблицы → проводной CAN-кадр для send_data()."""
        id_item = table.item(row, 1)
        if id_item is None:
            return None
        can_id = hex_to_int(id_item.text())
        if can_id is None:
            return None
        data_item = table.item(row, 3)
        data_text = (data_item.text() if data_item is not None else "").strip()
        try:
            data = bytes(int(part, 16) for part in data_text.split()) if data_text else b""
        except ValueError:
            data = b""
        channel = self._table_channel.get(table, 1)
        try:
            return pack_can_frame(channel, can_id, data)
        except ValueError:
            return None

    def _send_all(self, table: QTableWidget, timed: bool = False) -> None:
        rows = self._target_rows(table)
        if not rows:
            logger.info(tr("Таблица пуста — нечего отправлять"))
            return
        if not self._serial_manager.is_open():
            logger.info(tr("Порт не подключен — отправка невозможна"))
            return
        self._stop_sending(table)
        self._send_queues[table] = list(rows)
        self._send_timed[table] = timed
        self._send_prev_ms[table] = None
        send_btn, stop_btn, _step_btn = self._send_buttons[table]
        send_btn.setEnabled(False)
        stop_btn.setEnabled(True)
        timer = QTimer(self)
        timer.setInterval(10)  # ~100 кадров/с — достаточно для USB CDC
        timer.timeout.connect(lambda t=table: self._send_tick(t))
        self._send_timers[table] = timer
        logger.info(
            "Отправка %d кадров в CAN%d%s",
            len(rows),
            self._table_channel.get(table, 1),
            " (с таймингами лога)" if timed else "",
        )
        timer.start()

    @staticmethod
    def _row_time_ms(table: QTableWidget, row: int) -> "float | None":
        """Время строки в мс: «SS.mmm» (elapsed) или «HH:MM:SS.mmm» (лог)."""
        item = table.item(row, 0)
        if item is None:
            return None
        text = item.text().strip()
        try:
            parts = text.split(":")
            if len(parts) == 3:
                return (int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])) * 1000
            return float(text) * 1000
        except ValueError:
            return None

    def _send_tick(self, table: QTableWidget) -> None:
        queue = self._send_queues.get(table) or []
        if not queue or not self._serial_manager.is_open():
            self._stop_sending(table)
            if not self._serial_manager.is_open():
                logger.info(tr("Передача остановлена: порт закрыт"))
            return
        row = queue.pop(0)
        packet = self._row_to_packet(table, row)
        if packet is not None:
            self._serial_manager.send_data(packet)
        # Replay: интервал до следующего кадра — по разнице меток
        # времени строк лога (между 1 мс и 5 с, дальше не ждём).
        if self._send_timed.get(table) and queue:
            prev = self._row_time_ms(table, row)
            nxt = self._row_time_ms(table, queue[0])
            if prev is not None and nxt is not None:
                timer = self._send_timers.get(table)
                if timer is not None:
                    timer.setInterval(max(1, min(5000, int(nxt - prev))))
        if not queue:
            self._stop_sending(table)
            logger.info(tr("Передача кадров завершена"))

    def _stop_sending(self, table: QTableWidget) -> None:
        timer = self._send_timers.pop(table, None)
        if timer is not None:
            timer.stop()
            timer.deleteLater()
        self._send_queues.pop(table, None)
        buttons = self._send_buttons.get(table)
        if buttons is not None:
            send_btn, stop_btn, _step_btn = buttons
            send_btn.setEnabled(True)
            stop_btn.setEnabled(False)

    def _clear_channel(self, table: QTableWidget) -> None:
        """«Очистить» канала трейса: снимает принятые кадры только этой
        таблицы — видимые строки, невылитую очередь батчинга, метки
        периода её ID, покадровую позицию и очередь обратной отправки
        (идущая рассылка останавливается — строки уйдут из-под неё)."""
        channel = self._table_channel.get(table)
        self._stop_sending(table)
        self._pending_rows.pop(table, None)
        self._pending_dirs.pop(table, None)
        self._step_rows.pop(table, None)
        self._step_pos.pop(table, None)
        self._send_prev_ms.pop(table, None)
        table.setRowCount(0)
        if channel is not None:
            for key in [
                k for k in self._id_last_time if k[0] == channel
            ]:
                del self._id_last_time[key]
        # Строки этой таблицы выпали из списка подсветки поиска —
        # их айтемы уже уничтожены setRowCount(0).
        self._search_hits = [
            hit for hit in self._search_hits if hit[0] is not table
        ]
        kept_marks = []
        for item, brush in self._search_marks:
            try:
                if item.tableWidget() is table:
                    continue
            except RuntimeError:
                continue  # айтем уничтожен вместе со строкой
            kept_marks.append((item, brush))
        self._search_marks = kept_marks
        if self._search_hit_pos >= len(self._search_hits):
            self._search_hit_pos = 0
        logger.info(
            tr("Трейс CAN{0} очищен").format(channel if channel else "?")
        )

    def _send_next_frame(self, table: QTableWidget) -> None:
        rows = self._target_rows(table)
        if not rows:
            logger.info(tr("Таблица пуста — нечего отправлять"))
            return
        if not self._serial_manager.is_open():
            logger.info(tr("Порт не подключен — отправка невозможна"))
            return
        # Состав кадров (выделение) изменился — начинаем покадровую
        # отправку с первого кадра нового набора.
        if self._step_rows.get(table) != rows:
            self._step_rows[table] = rows
            self._step_pos[table] = 0
        pos = self._step_pos.get(table, 0)
        row = rows[pos]
        packet = self._row_to_packet(table, row)
        if packet is not None:
            self._serial_manager.send_data(packet)
        pos += 1
        if pos >= len(rows):
            pos = 0
            logger.info(tr("Конец списка — следующий шаг начнёт с первого кадра"))
        self._step_pos[table] = pos
        table.selectRow(row)

    def _show_context_menu(self, position) -> None:
        table = self.sender()
        if not isinstance(table, QTableWidget):
            return
        row = table.currentRow()
        if row < 0:
            return
        menu = QMenu(self)
        menu.addAction(tr("Копировать пакет"), lambda: self._copy_packet(table, row))
        menu.addAction(tr("Копировать для отправки"), lambda: self._copy_for_send(table, row))
        menu.addAction(tr("Копировать для триггера"), lambda: self._copy_for_trigger(table, row))
        menu.exec(table.viewport().mapToGlobal(position))

    def _copy_packet(self, table: QTableWidget, row: int) -> None:
        id_item = table.item(row, 1)
        dlc_item = table.item(row, 2)
        data_item = table.item(row, 3)
        if id_item is None or dlc_item is None or data_item is None:
            return
        text = f"ID={id_item.text()} DLC={dlc_item.text()} DATA={data_item.text()}"
        QApplication.clipboard().setText(text)

    def _copy_for_send(self, table: QTableWidget, row: int) -> None:
        self._copy_packet(table, row)

    def _copy_for_trigger(self, table: QTableWidget, row: int) -> None:
        self._copy_packet(table, row)

    def _export_csv(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("Экспорт трейса в CSV"), "", "CSV files (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(["channel", "time", "id", "dlc", "data", "period", "ascii", "dir", "explanation"])
                for table, channel in ((self._table1, 1), (self._table2, 2)):
                    for row in range(table.rowCount()):
                        writer.writerow(
                            [channel]
                            + [table.item(row, col).text() if table.item(row, col) else "" for col in range(8)]
                        )
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка экспорта CSV: %s", exc)

    def _export_custom(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, tr("Экспорт трейса в .trace"), "", "Trace files (*.trace)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                for table, channel in ((self._table1, 1), (self._table2, 2)):
                    f.write(f"[CAN{channel}]\n")
                    for row in range(table.rowCount()):
                        values = [table.item(row, col).text() if table.item(row, col) else "" for col in range(8)]
                        f.write(
                            f"{values[0]} ID={values[1]} DLC={values[2]} DATA={values[3]} "
                            f"PERIOD={values[4]} ASCII={values[5]} DIR={values[6]} EXPL={values[7]}\n"
                        )
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка экспорта .trace: %s", exc)

    def _load_log(self) -> None:
        """Загружает .trace или наш CSV в таблицы — дальше кнопки
        «Отправить»/«По кадрам» воспроизводят лог в шину."""
        path, _ = QFileDialog.getOpenFileName(
            self, tr("Загрузить лог"), "",
            tr("Trace/CSV (*.trace *.csv);;Все файлы (*)"),
        )
        if not path:
            return
        loaded = 0
        try:
            loaded = self._load_csv(path) if path.lower().endswith(".csv") else self._load_trace(path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки лога %s: %s", path, exc)
            return
        logger.info("Загружено %d кадров из %s", loaded, path)

    def _append_loaded_row(self, table: QTableWidget, values: list[str]) -> None:
        # Колонка «Направление» (RX/TX) появилась позже: старые файлы
        # без неё считаются приёмом с шины (RX). Старые файлы держали
        # порядок «… EXPL DIR» — новый: «… DIR EXPL» (Пояснение —
        # последняя колонка по отчёту мастера); распознаём оба.
        values = list(values[:8]) + [""] * max(0, 8 - len(values))
        if values[7].strip().upper() in ("RX", "TX"):
            # Старый порядок: пояснение@6, направление@7.
            values[6], values[7] = values[7], values[6]
        if not values[6].strip():
            values[6] = "RX"
        if table.rowCount() >= MAX_TABLE_ROWS:
            table.removeRow(0)
        row = table.rowCount()
        table.insertRow(row)
        is_tx = values[6].strip().upper() == "TX"
        for col, text in enumerate(values[:8]):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_tx:
                item.setBackground(QColor("#4CAF50"))
                item.setForeground(QColor("#FFFFFF"))
            table.setItem(row, col, item)

    def _load_csv(self, path: str) -> int:
        """CSV нашего экспорта: channel,time,id,dlc,data,period,ascii,explanation[,dir].
        Стриминговый CSV монитора: timestamp,channel,dir,id,dlc,data — тоже
        принимаем, по колонке dir."""
        loaded = 0
        with open(path, newline="", encoding="utf-8-sig") as f:
            for values in csv.reader(f):
                if len(values) < 4 or values[0].lower() == "channel":
                    continue
                # Стриминговый CSV монитора: timestamp,channel,dir,id,dlc,data
                if len(values) >= 6 and values[2].strip().upper() in ("RX", "TX"):
                    try:
                        channel = int(values[1])
                    except ValueError:
                        continue
                    table = self._table1 if channel == 1 else self._table2
                    # time,id,dlc,data,period,ascii,dir,expl — period/ascii/expl пустые
                    self._append_loaded_row(
                        table, [values[0], values[3], values[4], values[5], "", "", values[2], ""]
                    )
                    loaded += 1
                    continue
                try:
                    channel = int(values[0])
                except ValueError:
                    continue
                table = self._table1 if channel == 1 else self._table2
                self._append_loaded_row(table, values[1:9])
                loaded += 1
        return loaded

    _TRACE_LINE_RE = re.compile(
        r"^(\S+)\s+ID=(\S+)\s+DLC=(\S+)\s+DATA=(.*?)\s+PERIOD=(.*?)\s+ASCII=(.*?)\s+DIR=(\S+)\s+EXPL=(.*)$"
    )
    _TRACE_LINE_RE_LEGACY = re.compile(
        r"^(\S+)\s+ID=(\S+)\s+DLC=(\S+)\s+DATA=(.*?)\s+PERIOD=(.*?)\s+ASCII=(.*?)\s+EXPL=(.*?)(?:\s+DIR=(\S+))?$"
    )

    def _load_trace(self, path: str) -> int:
        """Формат .trace: секции [CAN1]/[CAN2], строки «time ID=.. DLC=.. DATA=.. .. DIR=.. EXPL=..».
        Старые строки без DIR= или в порядке «EXPL .. DIR» тоже
        принимаются (приём с шины — RX по умолчанию)."""
        loaded = 0
        table = self._table1
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if line.startswith("[CAN"):
                    table = self._table1 if "CAN1" in line else self._table2
                    continue
                match = self._TRACE_LINE_RE.match(line)
                if match:
                    groups = list(match.groups())
                    # Новый порядок: [t,id,dlc,data,period,ascii,dir,expl].
                    self._append_loaded_row(table, groups)
                    loaded += 1
                    continue
                match = self._TRACE_LINE_RE_LEGACY.match(line)
                if match:
                    groups = list(match.groups())
                    # Старый порядок: [t,id,dlc,data,period,ascii,expl,dir?] —
                    # _append_loaded_row сам развернёт колонки.
                    if groups[7] is None:
                        groups[7] = "RX"
                    self._append_loaded_row(table, groups)
                    loaded += 1
        return loaded

    @staticmethod
    def parse_packet_string(text: str) -> dict[str, Any] | None:
        return parse_packet_string(text)
