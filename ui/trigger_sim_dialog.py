"""Диалог «Симуляция триггеров» — прогон кадров через движок без железа.

Берёт записи в формате firmware (unpack_trigger) у вкладки триггеров,
прогоняет через них кадры из .trace/.csv или введённые вручную и
показывает журнал: какой триггер когда сработал и что ответил —
включая цепочки через TX-эхо. Ничего не пишет в устройство.
"""

from __future__ import annotations

from typing import Any
from collections.abc import Callable

from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from core.trace_loader import parse_trace_frames
from core.trigger_simulator import simulate
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import format_data_bytes, hex_to_int

logger = get_logger(__name__)


class TriggerSimDialog(QDialog):
    """Симулятор: очередь входных кадров → журнал срабатываний."""

    def __init__(
        self,
        records_provider: Callable[[], list[dict[str, Any]]],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._records_provider = records_provider
        self._frames: list[dict[str, Any]] = []
        self.setWindowTitle(tr("Симуляция триггеров"))
        self.setMinimumSize(720, 560)
        font = QFont("Segoe UI", 9)

        layout = QVBoxLayout(self)

        # Входные кадры: из лога или вручную.
        top = QHBoxLayout()
        load_btn = QPushButton(tr("Загрузить .trace/CSV…"))
        load_btn.setFont(font)
        load_btn.clicked.connect(self._load_frames)
        clear_btn = QPushButton(tr("Очистить кадры"))
        clear_btn.setFont(font)
        clear_btn.clicked.connect(self._clear_frames)
        top.addWidget(load_btn)
        top.addWidget(clear_btn)
        top.addStretch()
        layout.addLayout(top)

        entry = QHBoxLayout()
        self._channel_combo = QComboBox()
        self._channel_combo.addItems(["CAN1", "CAN2"])
        self._channel_combo.setFont(font)
        self._id_edit = QLineEdit()
        self._id_edit.setPlaceholderText("ID (hex)")
        self._id_edit.setFixedWidth(80)
        self._id_edit.setFont(font)
        self._dlc_spin = QSpinBox()
        self._dlc_spin.setRange(0, 8)
        self._dlc_spin.setValue(8)
        self._dlc_spin.setFont(font)
        self._data_edit = QLineEdit()
        self._data_edit.setPlaceholderText("Data hex: 11 22 33 …")
        self._data_edit.setFont(font)
        add_btn = QPushButton(tr("Добавить кадр"))
        add_btn.setFont(font)
        add_btn.clicked.connect(self._add_frame)
        entry.addWidget(self._channel_combo)
        entry.addWidget(self._id_edit)
        entry.addWidget(self._dlc_spin)
        entry.addWidget(self._data_edit, 1)
        entry.addWidget(add_btn)
        layout.addLayout(entry)

        self._frames_list = QListWidget()
        self._frames_list.setFont(font)
        self._frames_list.setMaximumHeight(120)
        layout.addWidget(self._frames_list)

        run_btn = QPushButton(tr("Запустить симуляцию"))
        run_btn.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        run_btn.setStyleSheet(
            "QPushButton { background-color: #4CAF50; color: #FFFFFF;"
            "border: none; border-radius: 4px; padding: 8px; }"
        )
        run_btn.clicked.connect(self._run)
        layout.addWidget(run_btn)

        layout.addWidget(QLabel(tr("Журнал симуляции:")))
        self._log = QListWidget()
        self._log.setFont(QFont("Consolas", 9))
        layout.addWidget(self._log, 1)

    # --- входные кадры ---------------------------------------------------

    def _load_frames(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Загрузить лог"),
            "",
            tr("Trace/CSV (*.trace *.csv);;Все файлы (*)"),
        )
        if not path:
            return
        try:
            frames = parse_trace_frames(path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось прочитать лог: {0}").format(exc))
            return
        # TX-кадры из лога — это передачи самого МК/ПК: в симуляции они
        # приходят как эхо (echo=1), иначе «ответы» лога проматчили бы
        # триггеры как внешние приёмы дважды.
        for f in frames:
            f["echo"] = 1 if f.get("tx") else 0
        self._frames = frames
        self._refresh_frames_list()

    def _clear_frames(self) -> None:
        self._frames = []
        self._refresh_frames_list()

    def _add_frame(self) -> None:
        can_id = hex_to_int(self._id_edit.text())
        if can_id is None:
            QMessageBox.warning(self, tr("Внимание"), tr("Некорректный ID"))
            return
        data = b""
        text = self._data_edit.text().strip()
        if text:
            try:
                data = bytes(int(part, 16) for part in text.split())
            except ValueError:
                QMessageBox.warning(self, tr("Внимание"), tr("Некорректные Data (hex байты)"))
                return
        if len(data) > 8:
            data = data[:8]
        # Ручные кадры идут подряд с шагом 10 мс после последнего.
        t = (float(self._frames[-1]["time_ms"]) + 10.0) if self._frames else 0.0
        self._frames.append(
            {
                "time_ms": t,
                "channel": self._channel_combo.currentIndex() + 1,
                "id": can_id,
                "dlc": self._dlc_spin.value(),
                "data": data,
                "rtr": False,
                "echo": 0,
            }
        )
        self._refresh_frames_list()

    def _refresh_frames_list(self) -> None:
        self._frames_list.clear()
        for f in self._frames:
            data_hex = " ".join(format_data_bytes(bytes(f["data"])))
            QListWidgetItem(
                f"t={f['time_ms']:.0f}мс CAN{f['channel']} ID={f['id']:03X} "
                f"DLC={f['dlc']} DATA={data_hex}",
                self._frames_list,
            )

    # --- прогон -----------------------------------------------------------

    def _run(self) -> None:
        self._log.clear()
        if not self._frames:
            QMessageBox.information(
                self,
                tr("Симуляция"),
                tr("Нет входных кадров — загрузите лог или добавьте вручную."),
            )
            return
        try:
            records = self._records_provider() or []
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось собрать триггеры: {0}").format(exc))
            return
        enabled = sum(1 for r in records if r.get("enabled"))
        if not records or not enabled:
            QMessageBox.information(
                self,
                tr("Симуляция"),
                tr("Нет активных триггеров — включите хотя бы один блок."),
            )
            return
        result = simulate(records, self._frames)
        matched: dict[int, int] = {}
        for event in result["events"]:
            kind = event["kind"]
            t_ms = float(event["time_ms"])
            if kind == "rx":
                f = event["frame"]
                data_hex = " ".join(format_data_bytes(bytes(f.get("data", b""))))
                echo = tr(" [эхо МК]") if f.get("echo") else ""
                item = QListWidgetItem(
                    f"{t_ms:>8.0f}мс  RX CAN{f['channel']} ID={f['id']:03X} [{data_hex}]{echo}"
                )
                item.setForeground(QColor("#9E9E9E"))
                self._log.addItem(item)
            elif kind == "armed":
                item = QListWidgetItem(
                    tr("{0:>8.0f}мс  ТРИГГЕР {1} сработал → ответ через {2}мс").format(
                        t_ms, event["trigger"] + 1, event["delay"]
                    )
                )
                item.setForeground(QColor("#FFB74D"))
                self._log.addItem(item)
                matched[event["trigger"]] = matched.get(event["trigger"], 0) + 1
            elif kind == "tx":
                f = event["frame"]
                data_hex = " ".join(format_data_bytes(bytes(f.get("data", b""))))
                item = QListWidgetItem(
                    tr("{0:>8.0f}мс  ТРИГГЕР {1} → TX CAN{2} ID={3} [{4}]").format(
                        t_ms,
                        (event.get("trigger") or 0) + 1,
                        f["channel"],
                        f"{f['id']:03X}",
                        data_hex,
                    )
                )
                item.setForeground(QColor("#4CAF50"))
                self._log.addItem(item)
                trig = event.get("trigger")
                if trig is not None and trig not in matched:
                    matched[trig] = 1
        if result["fired_count"] == 0:
            item = QListWidgetItem(tr("Ни один триггер не сработал"))
            item.setForeground(QColor("#E53935"))
            self._log.addItem(item)
        logger.info(
            "Симуляция: %d кадров, %d записей, срабатываний %d",
            len(self._frames), len(records), result["fired_count"],
        )
