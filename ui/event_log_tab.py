"""Вкладка «Лог МК»: постраничное чтение Flash-журнала событий устройства.

Журнал (см. firmware/application/Inc/event_log.h) переживает и мягкий
сброс, и отключение питания — в отличие от обычных счётчиков статистики
(CMD_CAN_STATS/CMD_USB_STATS), он хранит хронологию: что происходило на
устройстве, когда связь с ПК уже пропала (полевой симптом: разрыв CAN/USB,
устраняется только отключением одного из CAN-проводов). Вкладка только
читает журнал по требованию — устройство ничего не стирает по команде
с ПК, журнал живёт своей жизнью в кольце Flash независимо от того, читают
его или нет.
"""


from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.can_protocol import (
    EVLOG_BOOT,
    EVLOG_BOOTLOADER,
    EVLOG_FAULT,
    EVLOG_FAULT_ADDR,
    EVLOG_FAULT_LR,
    EVLOG_FAULT_REGS,
    EVLOG_INIT_STAGE,
    EVLOG_VERSION,
    EVLOG_CAN_BUSOFF,
    EVLOG_CAN_BUSOFF_RECOVER,
    EVLOG_CAN_ERROR,
    EVLOG_CAN_FIFO_POLL,
    EVLOG_CAN_OVERFLOW,
    EVLOG_USB_DISCONNECT,
    EVLOG_USB_RESET,
    EVLOG_USB_RX_OVERFLOW,
    EVLOG_USB_TX_STALL,
)
from core.serial_manager import SerialManager
from models.logger import get_logger
from models.translations import _ as tr
from ui.ui_utils import setup_button

logger = get_logger(__name__)

# LEC[2:0] (см. RM CAN_ESR) — расшифровка code для EVLOG_CAN_ERROR.
_LEC_NAMES = {
    1: "Stuff",
    2: "Form",
    3: "ACK",
    4: "Bit recessive",
    5: "Bit dominant",
    6: "CRC",
    7: "-",
}
# Причина сброса МК (RCC->CSR[31:24]) — расшифровка code для EVLOG_BOOT.
_RESET_FLAG_NAMES = (
    (0x80, "LPWR"),
    (0x40, "WWDG"),
    (0x20, "IWDG"),
    (0x10, "SOFT"),
    (0x08, "POR"),
    (0x04, "PIN"),
)
_FAULT_NAMES = {0: None, 1: "HardFault", 2: "MemManage", 3: "BusFault", 4: "UsageFault"}
# События без привязки к каналу CAN — колонка «Канал» показывает "-".
_CHANNELLESS_TYPES = (EVLOG_BOOT, EVLOG_BOOTLOADER, EVLOG_FAULT, EVLOG_VERSION,
                      EVLOG_INIT_STAGE, EVLOG_FAULT_REGS, EVLOG_FAULT_ADDR,
                      EVLOG_FAULT_LR)
# У этих типов поле timestamp несёт данные (PC / CRC32 / CFSR / BFAR / LR),
# а не время — колонка «Время» для них не имеет смысла.
_AUX_TIMESTAMP_TYPES = (EVLOG_FAULT, EVLOG_VERSION, EVLOG_FAULT_REGS,
                        EVLOG_FAULT_ADDR, EVLOG_FAULT_LR)


def _format_channel(channel: int) -> str:
    if channel == 0:
        return "CAN1"
    if channel == 1:
        return "CAN2"
    return "-"


def _decode_reset_flags(flags: int) -> str:
    names = [name for bit, name in _RESET_FLAG_NAMES if flags & bit]
    return "+".join(names) if names else "?"


class EventLogTab(QWidget):
    """Читает и показывает Flash-журнал событий МК (CMD_EVENT_LOG)."""

    def __init__(self, serial_manager: SerialManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._entries: list[dict] = []
        self._auto_read_done = False
        self._create_widgets()
        self._build_layout()

    def _create_widgets(self) -> None:
        self._read_button = QPushButton(tr("Считать лог"))
        setup_button(self._read_button)
        self._read_button.clicked.connect(self._on_read_clicked)

        self._export_button = QPushButton(tr("Экспорт в файл"))
        setup_button(self._export_button)
        self._export_button.clicked.connect(self._on_export_clicked)
        self._export_button.setEnabled(False)

        self._clear_button = QPushButton(tr("Очистить таблицу"))
        setup_button(self._clear_button)
        self._clear_button.clicked.connect(self._on_clear_clicked)

        self._status_label = QLabel("")

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels([
            "#", tr("Время МК"), tr("Событие"), tr("Канал"), tr("Детали"),
        ])
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self._table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        top.addWidget(self._read_button)
        top.addWidget(self._export_button)
        top.addWidget(self._clear_button)
        top.addStretch(1)
        layout.addLayout(top)
        layout.addWidget(self._status_label)
        layout.addWidget(self._table)

        note = QLabel(
            tr(
                "Журнал хранится в Flash устройства и переживает отключение "
                "питания. Читается только по кнопке — устройство его не "
                "стирает и не останавливает запись новых событий."
            )
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: #888;")
        layout.addWidget(note)

    def retranslate_ui(self) -> None:
        self._read_button.setText(tr("Считать лог"))
        self._export_button.setText(tr("Экспорт в файл"))
        self._clear_button.setText(tr("Очистить таблицу"))
        self._table.setHorizontalHeaderLabels([
            "#", tr("Время МК"), tr("Событие"), tr("Канал"), tr("Детали"),
        ])
        self._render_table()

    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        # Авто-чтение при первом открытии вкладки с живым портом: иначе
        # оператор видит пустую таблицу и делает вывод «логов нет», хотя
        # МК их записал ещё при подаче питания. Порт мог быть подключён
        # до открытия вкладки — connection_changed ловить не нужно.
        super().showEvent(event)
        if not self._auto_read_done and self._serial_manager.is_open():
            self._read_log(auto=True)

    # ------------------------------------------------------------------

    def _on_read_clicked(self) -> None:
        self._read_log(auto=False)

    def _read_log(self, auto: bool) -> None:
        # auto=True — авто-чтение при открытии вкладки: ошибки только в
        # статус-строку, без модального диалога (старые прошивки не знают
        # CMD_EVENT_LOG и отвечают «неизвестная команда» — это не авария).
        self._auto_read_done = True
        if not self._serial_manager.is_open():
            if not auto:
                QMessageBox.warning(self, tr("Ошибка"), tr("Порт не подключен"))
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            entries = self._serial_manager.read_full_event_log()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Не удалось прочитать журнал МК: %s", exc)
            QApplication.restoreOverrideCursor()
            if auto:
                self._status_label.setText(tr("Не удалось прочитать журнал МК: {0}").format(exc))
            else:
                QMessageBox.warning(
                    self,
                    tr("Ошибка"),
                    tr("Не удалось прочитать журнал МК: {0}").format(exc),
                )
            return
        QApplication.restoreOverrideCursor()
        self._entries = entries
        self._render_table()
        self._export_button.setEnabled(bool(self._entries))
        self._status_label.setText(tr("Записей: {0}").format(len(self._entries)))

    def _on_clear_clicked(self) -> None:
        self._entries = []
        self._render_table()
        self._export_button.setEnabled(False)
        self._status_label.setText("")

    def _on_export_clicked(self) -> None:
        if not self._entries:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self, tr("Экспорт в файл"), "event_log.txt", "Text (*.txt)"
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as fh:
                for entry in self._entries:
                    fh.write(self._format_row(entry, sep=" | ") + "\n")
        except OSError as exc:
            QMessageBox.warning(self, tr("Ошибка"), tr("Не удалось открыть файл") + f": {exc}")

    # ------------------------------------------------------------------

    def _format_row(self, entry: dict, sep: str = "\t") -> str:
        seq = entry["seq"]
        ts_ms = entry["timestamp_ms"]
        etype = entry["type"]
        channel = entry["channel"]
        code = entry["code"]
        time_str = "-" if etype in _AUX_TIMESTAMP_TYPES else self._format_uptime(ts_ms)
        name, detail = self._decode_event(etype, channel, code, ts_ms)
        chan_str = _format_channel(channel) if etype not in _CHANNELLESS_TYPES else "-"
        return sep.join((str(seq), time_str, name, chan_str, detail))

    @staticmethod
    def _format_uptime(ts_ms: int) -> str:
        total_s, ms = divmod(ts_ms, 1000)
        h, rem = divmod(total_s, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

    def _decode_event(self, etype: int, channel: int, code: int, ts_ms: int = 0) -> tuple:
        if etype == EVLOG_BOOT:
            fault = _FAULT_NAMES.get(channel)
            detail = tr("Причина: {0}").format(_decode_reset_flags(code))
            if fault:
                detail += f" | {tr('Крах перед сбросом')}: {fault}"
            return tr("Старт МК"), detail
        if etype == EVLOG_CAN_ERROR:
            return tr("Ошибка CAN"), _LEC_NAMES.get(code, f"0x{code:02X}")
        if etype == EVLOG_CAN_BUSOFF:
            return tr("Bus-off"), ""
        if etype == EVLOG_CAN_BUSOFF_RECOVER:
            return tr("Восстановление после bus-off"), ""
        if etype == EVLOG_CAN_OVERFLOW:
            return tr("Переполнение приёмного кольца"), ""
        if etype == EVLOG_CAN_FIFO_POLL:
            return tr("Backstop FIFO (IRQ пропустил кадр)"), ""
        if etype == EVLOG_USB_RESET:
            return tr("USB reset (реэнумерация)"), f"#{code}"
        if etype == EVLOG_USB_DISCONNECT:
            return tr("USB отключение (физическое)"), f"#{code}"
        if etype == EVLOG_USB_TX_STALL:
            return tr("USB TX голодал >100 мс"), ""
        if etype == EVLOG_USB_RX_OVERFLOW:
            detail = f"+{code}" + (tr(" байт (насыщение)") if code == 0xFF else tr(" байт"))
            return tr("Переполнение RX-буфера USB"), detail
        if etype == EVLOG_BOOTLOADER:
            detail = {
                0: tr("по команде ПК (прошивка)"),
                1: tr("запрошен хостом (флаг BKP)"),
                2: tr("приложение невалидно или отсутствует"),
            }.get(code, tr("код {0}").format(code))
            return tr("Вход в загрузчик"), detail
        if etype == EVLOG_FAULT:
            fault = _FAULT_NAMES.get(channel, tr("код {0}").format(channel))
            # Поле timestamp несёт застеканный PC, code — байт BFSR
            # (CFSR[15:8]: PRECISERR/IMPRECISERR/STKERR/UNSTKERR/BFARVALID).
            bfsr_bits = []
            if code & 0x80:
                bfsr_bits.append("BFARVALID")
            if code & 0x08:
                bfsr_bits.append("STKERR")
            if code & 0x10:
                bfsr_bits.append("UNSTKERR")
            if code & 0x02:
                bfsr_bits.append("IMPRECISERR")
            if code & 0x01:
                bfsr_bits.append("PRECISERR")
            bfsr = ",".join(bfsr_bits) if bfsr_bits else f"0x{code:02X}"
            return tr("Крах МК"), f"{fault} | PC=0x{ts_ms:08X} | BFSR={bfsr}"
        if etype == EVLOG_FAULT_REGS:
            fault = _FAULT_NAMES.get(channel, tr("код {0}").format(channel))
            # timestamp — полный CFSR, code — HFSR[31:24] (FORCED/DEBUGEVT).
            hfsr_hi = []
            if code & 0x40:
                hfsr_hi.append("FORCED")
            if code & 0x80:
                hfsr_hi.append("DEBUGEVT")
            hfsr = ",".join(hfsr_hi) if hfsr_hi else f"0x{code:02X}"
            return tr("Регистры краха"), f"{fault} | CFSR=0x{ts_ms:08X} | HFSR={hfsr}"
        if etype == EVLOG_FAULT_ADDR:
            # timestamp — BFAR (адрес доступа), channel — EXC_RETURN.
            return tr("Адрес краха"), f"BFAR=0x{ts_ms:08X} | EXC=0x{channel:02X}"
        if etype == EVLOG_FAULT_LR:
            # timestamp — застеканный LR, channel+code — ICSR (VECTACTIVE).
            vect = (code << 8) | channel
            return tr("Контекст краха"), (
                f"LR=0x{ts_ms:08X} | IRQ={vect & 0x1FF} (ICSR=0x{vect:03X})"
            )
        if etype == EVLOG_VERSION:
            # Поле timestamp несёт CRC32 образа приложения — отпечаток сборки.
            return tr("Прошивка МК"), (
                f"app v{channel} | {tr('протокол')} {code} | CRC32=0x{ts_ms:08X}"
            )
        if etype == EVLOG_INIT_STAGE:
            stage_names = {
                0: tr("до журнала (ранняя инициализация)"),
                1: tr("после журнала"),
                2: tr("после загрузки конфигурации"),
                3: tr("после загрузки триггеров"),
                4: tr("после инициализации CAN"),
                5: tr("после инициализации протокола"),
                6: tr("после запуска USB"),
                7: tr("после включения CAN-прерываний"),
                8: tr("главный цикл"),
            }
            return tr("Этап краха"), stage_names.get(channel, tr("этап {0}").format(channel))
        return tr("Неизвестное событие"), f"type={etype} code=0x{code:02X}"

    def _render_table(self) -> None:
        self._table.setRowCount(len(self._entries))
        for row, entry in enumerate(self._entries):
            seq = entry["seq"]
            etype = entry["type"]
            channel = entry["channel"]
            code = entry["code"]
            name, detail = self._decode_event(etype, channel, code, entry["timestamp_ms"])
            chan_str = _format_channel(channel) if etype not in _CHANNELLESS_TYPES else "-"
            ts_ms = entry["timestamp_ms"]
            time_str = "-" if etype in _AUX_TIMESTAMP_TYPES else self._format_uptime(ts_ms)
            values = (str(seq), time_str, name, chan_str, detail)
            for col, value in enumerate(values):
                self._table.setItem(row, col, QTableWidgetItem(value))
