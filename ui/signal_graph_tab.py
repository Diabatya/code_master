"""Вкладка графиков сигналов в реальном времени на pyqtgraph."""

import time
from typing import Any, Dict, List, Optional, Tuple

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.dbc_manager import DBCManager
from core.dbc_parser import decode_frame
from core.serial_manager import SerialManager
from models.logger import get_logger
from models.translations import _ as tr
from ui.ui_utils import setup_button

logger = get_logger(__name__)

MAX_GRAPH_POINTS = 2000
WINDOW_SECONDS = 60


class SignalGraphTab(QWidget):
    """Вкладка построения графиков сигналов DBC в реальном времени."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._dbc_manager = DBCManager()
        self._start_time = time.time()
        self._paused = False
        self._graphs: List[Dict[str, Any]] = []
        self._nodes: List[str] = []
        self._messages_by_node: Dict[str, List[Any]] = {}

        self._create_widgets()
        self._build_layout()
        self._setup_update_timer()
        self._populate_dbc()

    def retranslate_ui(self) -> None:
        """Обновляет статические строки графиков (заглушка)."""
        pass

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        self._node_combo = QComboBox()
        self._node_combo.setFont(font)
        self._node_combo.setMinimumWidth(150)
        self._node_combo.currentIndexChanged.connect(self._on_node_changed)

        self._message_combo = QComboBox()
        self._message_combo.setFont(font)
        self._message_combo.setMinimumWidth(200)
        self._message_combo.currentIndexChanged.connect(self._on_message_changed)

        self._signal_combo = QComboBox()
        self._signal_combo.setFont(font)
        self._signal_combo.setMinimumWidth(180)

        self._add_button = QPushButton(tr("Добавить график"))
        setup_button(self._add_button, height=30)
        self._add_button.clicked.connect(self._add_graph)

        self._pause_button = QPushButton(tr("Пауза"))
        setup_button(self._pause_button, height=30)
        self._pause_button.setCheckable(True)
        self._pause_button.clicked.connect(self._on_pause)

        self._clear_button = QPushButton(tr("Очистить"))
        setup_button(self._clear_button, height=30)
        self._clear_button.clicked.connect(self._clear_graphs)

        self._plot_widget = pg.PlotWidget()
        self._plot_widget.setLabel("left", tr("Значение"))
        self._plot_widget.setLabel("bottom", tr("Время, с"))
        self._plot_widget.showGrid(x=True, y=True, alpha=0.3)
        self._plot_widget.addLegend()
        self._plot_widget.setYRange(0, 1, padding=0.1)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        controls = QHBoxLayout()
        controls.addWidget(QLabel(tr("Узел:")))
        controls.addWidget(self._node_combo)
        controls.addWidget(QLabel(tr("Сообщение:")))
        controls.addWidget(self._message_combo)
        controls.addWidget(QLabel(tr("Сигнал:")))
        controls.addWidget(self._signal_combo)
        controls.addWidget(self._add_button)
        controls.addStretch()
        controls.addWidget(self._pause_button)
        controls.addWidget(self._clear_button)
        layout.addLayout(controls)
        layout.addWidget(self._plot_widget, 1)

    def _setup_update_timer(self) -> None:
        self._update_timer = QTimer(self)
        self._update_timer.timeout.connect(self._update_graphs)
        self._update_timer.start(100)

    def _populate_dbc(self) -> None:
        db = self._dbc_manager.get_cantools_db()
        self._node_combo.blockSignals(True)
        self._message_combo.blockSignals(True)
        self._signal_combo.blockSignals(True)
        self._node_combo.clear()
        self._message_combo.clear()
        self._signal_combo.clear()
        self._nodes = []
        self._messages_by_node = {}

        if db is not None:
            nodes = set()
            for message in db.messages:
                for node in message.senders:
                    nodes.add(node)
                # Если отправителей нет, попадает в общий узел
                if not message.senders:
                    nodes.add("—")
            self._nodes = sorted(nodes)
            for node in self._nodes:
                self._node_combo.addItem(node, node)
                self._messages_by_node[node] = []
            for message in db.messages:
                for node in (message.senders or ["—"]):
                    self._messages_by_node.setdefault(node, []).append(message)
            self._node_combo.setEnabled(True)
            self._message_combo.setEnabled(True)
            self._signal_combo.setEnabled(True)
        else:
            self._node_combo.addItem("—")
            self._message_combo.addItem("—")
            self._signal_combo.addItem("—")
            self._node_combo.setEnabled(False)
            self._message_combo.setEnabled(False)
            self._signal_combo.setEnabled(False)

        self._node_combo.blockSignals(False)
        self._message_combo.blockSignals(False)
        self._signal_combo.blockSignals(False)
        if db is not None:
            self._on_node_changed(0)

    def _on_node_changed(self, index: int) -> None:
        node = self._node_combo.itemData(index)
        self._message_combo.blockSignals(True)
        self._message_combo.clear()
        for message in self._messages_by_node.get(node, []):
            self._message_combo.addItem(message.name, message)
        self._message_combo.blockSignals(False)
        self._on_message_changed(0)

    def _on_message_changed(self, index: int) -> None:
        message = self._message_combo.itemData(index)
        self._signal_combo.blockSignals(True)
        self._signal_combo.clear()
        if message is not None:
            for signal in message.signals:
                self._signal_combo.addItem(signal.name, signal)
        self._signal_combo.blockSignals(False)

    def _add_graph(self) -> None:
        message = self._message_combo.currentData()
        signal = self._signal_combo.currentData()
        if message is None or signal is None:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите сообщение и сигнал"))
            return

        title = f"{message.name}::{signal.name}"
        for g in self._graphs:
            if g["title"] == title:
                QMessageBox.information(self, tr("Информация"), tr("График уже добавлен"))
                return

        color = pg.intColor(len(self._graphs))
        pen = pg.mkPen(color=color, width=2)
        plot_data = self._plot_widget.plot([], [], pen=pen, name=title)
        self._graphs.append({
            "title": title,
            "message_name": message.name,
            "signal_name": signal.name,
            "plot": plot_data,
            "x": [],
            "y": [],
        })
        logger.info("Добавлен график сигнала: %s", title)

    def _on_pause(self, checked: bool) -> None:
        self._paused = checked
        self._pause_button.setText(tr("Продолжить") if checked else tr("Пауза"))

    def _clear_graphs(self) -> None:
        for graph in self._graphs:
            self._plot_widget.removeItem(graph["plot"])
        self._graphs.clear()
        self._plot_widget.clear()
        self._plot_widget.addLegend()
        self._plot_widget.setYRange(0, 1, padding=0.1)

    def _update_graphs(self) -> None:
        if self._paused or not self._graphs:
            return
        now = time.time() - self._start_time
        for graph in self._graphs:
            graph["plot"].setData(graph["x"], graph["y"])
        # Автоматический масштаб по времени
        if self._graphs and self._graphs[0]["x"]:
            min_x = max(0, now - WINDOW_SECONDS)
            self._plot_widget.setXRange(min_x, max(min_x + 1, now), padding=0.05)

    def process_frame(self, frame: Dict[str, Any]) -> None:
        if self._paused or not self._graphs:
            return
        db = self._dbc_manager.get_cantools_db()
        if db is None:
            return
        can_id = int(frame["id"])
        data = bytes(frame["data"])
        try:
            message = db.get_message_by_frame_id(can_id)
        except KeyError:
            return
        decoded = decode_frame(db, can_id, data)
        if decoded is None:
            return

        now = time.time() - self._start_time
        for graph in self._graphs:
            if graph["message_name"] != message.name:
                continue
            value = decoded.get(graph["signal_name"])
            if isinstance(value, dict):
                value = value.get("value")
            if value is None:
                continue
            try:
                y = float(value)
            except (TypeError, ValueError):
                continue
            graph["x"].append(now)
            graph["y"].append(y)
            if len(graph["x"]) > MAX_GRAPH_POINTS:
                graph["x"].pop(0)
                graph["y"].pop(0)

    def set_dbc(self, dbc_manager) -> None:
        """Обновляет списки при смене DBC."""
        self._clear_graphs()
        self._populate_dbc()
