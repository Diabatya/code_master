"""Трэйс CAN-шины: две хронологические таблицы для CAN1 и CAN2."""

import csv
import re
import time
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QLabel,
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
from core.serial_manager import SerialManager
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import format_data_bytes, hex_to_int, int_to_hex, parse_packet_string

logger = get_logger(__name__)

MAX_TABLE_ROWS = 50_000


def _ascii_from_data(data: bytes) -> str:
    return "".join(chr(b) if 32 <= b < 127 else "." for b in data)


class CanAnalyzer(QWidget):
    """Виджет трэйса CAN-шины с двумя таблицами."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._dbc_manager = DBCManager()
        self._analyzing = False
        self._start_time = 0.0
        self._id_last_time: Dict[int, float] = {}
        # Обратная отправка строк таблицы в шину: очередь строк,
        # таймер рассылки и позиция «по кадрам» — на каждую таблицу.
        self._send_queues: Dict[QTableWidget, List[int]] = {}
        self._send_timers: Dict[QTableWidget, QTimer] = {}
        self._step_rows: Dict[QTableWidget, List[int]] = {}
        self._step_pos: Dict[QTableWidget, int] = {}
        self._table_channel: Dict[QTableWidget, int] = {}
        self._send_buttons: Dict[QTableWidget, tuple] = {}
        self._create_widgets()
        self._build_layout()

    def retranslate_ui(self) -> None:
        self._start_button.setText(tr("Начать анализ"))
        self._stop_button.setText(tr("Завершить анализ"))
        self._export_csv_button.setText(tr("Экспорт CSV"))
        self._export_custom_button.setText(tr("Экспорт .trace"))
        self._title.setText(tr("Трэйс CAN-шины"))
        for table in (self._table1, self._table2):
            table.setHorizontalHeaderLabels(
                [tr("Время"), tr("ID"), tr("DLC"), tr("DATA"), tr("Период"), tr("ASCII"), tr("Пояснение")]
            )

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

        self._table1 = self._build_table(font)
        self._table2 = self._build_table(font)
        self._table_channel[self._table1] = 1
        self._table_channel[self._table2] = 2
        self._panel1 = self._build_table_panel(self._table1)
        self._panel2 = self._build_table_panel(self._table2)

    def _build_table(self, font: QFont) -> QTableWidget:
        table = QTableWidget()
        table.setColumnCount(8)
        table.setHorizontalHeaderLabels(
            [tr("Время"), tr("ID"), tr("DLC"), tr("DATA"), tr("Период"), tr("ASCII"), tr("Пояснение"), tr("Направление")]
        )
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
        table.setColumnWidth(6, 260)
        table.setColumnWidth(7, 80)
        return table

    def _build_table_panel(self, table: QTableWidget) -> QWidget:
        """Таблица + строка кнопок отправки принятых кадров обратно в шину."""
        font = QFont("Segoe UI", 9)
        send_btn = QPushButton(tr("Отправить"))
        stop_btn = QPushButton(tr("Стоп"))
        step_btn = QPushButton(tr("По кадрам"))
        for btn in (send_btn, stop_btn, step_btn):
            btn.setFont(font)
            btn.setFixedHeight(26)
        send_btn.setToolTip(
            tr("Отправить все принятые кадры обратно в шину. "
               "Если строки выделены — только выделенные.")
        )
        step_btn.setToolTip(tr("Каждое нажатие отправляет следующий кадр"))
        stop_btn.setEnabled(False)
        send_btn.clicked.connect(lambda _c=False, t=table: self._send_all(t))
        stop_btn.clicked.connect(lambda _c=False, t=table: self._stop_sending(t))
        step_btn.clicked.connect(lambda _c=False, t=table: self._send_next_frame(t))
        self._send_buttons[table] = (send_btn, stop_btn, step_btn)

        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 4, 0, 0)
        buttons.addWidget(send_btn)
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
        self._start_button.setEnabled(True)
        self._stop_button.setEnabled(False)
        self._table1.scrollToBottom()
        self._table2.scrollToBottom()
        logger.info("Трэйс остановлен")

    def process_frame(self, frame: Dict[str, Any]) -> None:
        if not self._analyzing:
            return
        channel = int(frame.get("channel", 0))
        table = self._table1 if channel == 1 else self._table2
        self._add_trace_row(table, frame)

    def _add_trace_row(self, table: QTableWidget, frame: Dict[str, Any]) -> None:
        can_id = int(frame.get("id", 0))
        data = bytes(frame.get("data", b""))
        now = time.time()
        elapsed = now - self._start_time
        elapsed_text = f"{elapsed:.3f}"
        last_time = self._id_last_time.get(can_id)
        period_text = f"{int((now - last_time) * 1000)} ms" if last_time else ""
        self._id_last_time[can_id] = now

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
        # с шины. TX-строки подсвечиваются зелёным, как кнопка «Запущено».
        is_tx = bool(frame.get("tx_echo", False))
        dir_text = "TX" if is_tx else "RX"
        values = [elapsed_text, id_text, dlc_text, data_text, period_text, ascii_text, explanation, dir_text]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if is_tx:
                item.setBackground(QColor("#4CAF50"))
                item.setForeground(QColor("#FFFFFF"))
            table.setItem(row, col, item)
        table.scrollToBottom()

    # ---- Обратная отправка кадров в шину -------------------------------

    def _target_rows(self, table: QTableWidget) -> List[int]:
        """Выделенные строки по возрастанию; без выделения — вся таблица."""
        selected = sorted({idx.row() for idx in table.selectedIndexes()})
        return selected if selected else list(range(table.rowCount()))

    def _row_to_packet(self, table: QTableWidget, row: int) -> Optional[bytes]:
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

    def _send_all(self, table: QTableWidget) -> None:
        rows = self._target_rows(table)
        if not rows:
            logger.info(tr("Таблица пуста — нечего отправлять"))
            return
        if not self._serial_manager.is_open():
            logger.info(tr("Порт не подключен — отправка невозможна"))
            return
        self._stop_sending(table)
        self._send_queues[table] = list(rows)
        send_btn, stop_btn, _step_btn = self._send_buttons[table]
        send_btn.setEnabled(False)
        stop_btn.setEnabled(True)
        timer = QTimer(self)
        timer.setInterval(10)  # ~100 кадров/с — достаточно для USB CDC
        timer.timeout.connect(lambda t=table: self._send_tick(t))
        self._send_timers[table] = timer
        logger.info(
            "Отправка %d кадров в CAN%d", len(rows), self._table_channel.get(table, 1)
        )
        timer.start()

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
                writer.writerow(["channel", "time", "id", "dlc", "data", "period", "ascii", "explanation", "dir"])
                for table, channel in ((self._table1, 1), (self._table2, 2)):
                    for row in range(table.rowCount()):
                        writer.writerow([channel] + [table.item(row, col).text() if table.item(row, col) else "" for col in range(8)])
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
                        f.write(f"{values[0]} ID={values[1]} DLC={values[2]} DATA={values[3]} PERIOD={values[4]} ASCII={values[5]} EXPL={values[6]} DIR={values[7]}\n")
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
            if path.lower().endswith(".csv"):
                loaded = self._load_csv(path)
            else:
                loaded = self._load_trace(path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки лога %s: %s", path, exc)
            return
        logger.info("Загружено %d кадров из %s", loaded, path)

    def _append_loaded_row(self, table: QTableWidget, values: List[str]) -> None:
        # 8-я колонка «Направление» появилась позже: старые файлы без неё
        # считаются приёмом с шины (RX).
        values = list(values[:8]) + ["RX"] * max(0, 8 - len(values))
        if table.rowCount() >= MAX_TABLE_ROWS:
            table.removeRow(0)
        row = table.rowCount()
        table.insertRow(row)
        is_tx = values[7].strip().upper() == "TX"
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
                    # time,id,dlc,data,period,ascii,expl,dir — period/ascii/expl пустые
                    self._append_loaded_row(
                        table, [values[0], values[3], values[4], values[5], "", "", "", values[2]]
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
        r"^(\S+)\s+ID=(\S+)\s+DLC=(\S+)\s+DATA=(.*?)\s+PERIOD=(.*?)\s+ASCII=(.*?)\s+EXPL=(.*?)(?:\s+DIR=(\S+))?$"
    )

    def _load_trace(self, path: str) -> int:
        """Формат .trace: секции [CAN1]/[CAN2], строки «time ID=.. DLC=.. DATA=.. .. DIR=RX|TX».
        Старые строки без DIR= трактуются как приём с шины (RX)."""
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
                    if groups[7] is None:
                        groups[7] = "RX"
                    self._append_loaded_row(table, groups)
                    loaded += 1
        return loaded

    @staticmethod
    def parse_packet_string(text: str) -> Optional[Dict[str, Any]]:
        return parse_packet_string(text)
