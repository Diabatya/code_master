"""Профессиональный диалог прошивки микроконтроллера и HEX-редактор."""

import re
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

try:
    from pyocd.core.helpers import ConnectHelper
    from pyocd.flash.builder import FlashBuilder

    _PYOCD = True
except Exception:
    _PYOCD = False

try:
    import pylink

    _PYLINK = True
except Exception:
    _PYLINK = False

try:
    import usb.core

    _PYUSB = True
except Exception:
    _PYUSB = False

from PySide6.QtCore import QObject, Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from core.bootloader import Bootloader
from core.firmware_utils import _save_intel_hex, load_firmware_bytes, validate_application_vector
from core.can_protocol import (
    CMD_CFG_READ,
    CMD_CFG_WRITE,
    DEVICE_TYPE_ANALOG,
    DEVICE_TYPE_BASIC,
    DEVICE_TYPE_CAN_FD,
)
from core.stm32_info import (
    APPLICATION_BASE_ADDR,
    BOOTLOADER_BASE_ADDR,
    CHIP_FLASH_SIZE_KB,
    DEVICE_CONFIG_PAGE_ADDR,
    DEVICE_CONFIG_PAGE_SIZE,
    DEVICE_CONFIG_NAME_MAX,
    STM32_FLASH_SIZES,
    STM32_PAGE_SIZES,
    build_device_config_page,
    merge_device_config_page,
    parse_device_config,
)
from models.config import Config
from ui.com_settings_dialog import ComSettingsDialog
from models.logger import get_logger
from models.translations import _ as tr

logger = get_logger(__name__)

PROGRAMMER_METHODS: List[Tuple[str, str]] = [
    ("stlink", "ST-Link"),
    ("jlink", "J-Link / Flasher"),
    ("uart", "UART (бутлоадер)"),
    ("usb_cdc", "USB (CDC bootloader)"),
    ("usb", "USB (DFU)"),
    ("auto", "Авто"),
]

DEVICE_TYPES: List[Tuple[int, str]] = [
    (DEVICE_TYPE_BASIC, "2 CAN"),
    (DEVICE_TYPE_ANALOG, "2 CAN +"),
    (DEVICE_TYPE_CAN_FD, "2 CAN FD"),
]

def _flash_size_for_chip_id(chip_id: Optional[int]) -> str:
    """Возвращает строку с размером флеш-памяти по chip ID или 'Неизвестно'."""
    if chip_id is None:
        return tr("Неизвестно")
    return f"{CHIP_FLASH_SIZE_KB.get(chip_id, tr('Неизвестно'))} KB"


def _format_chip_id(value: Optional[int]) -> str:
    """Форматирует chip ID как HEX-строку."""
    if value is None:
        return tr("Неизвестно")
    return f"0x{value:08X}"


class HexHighlighter(QSyntaxHighlighter):
    """Подсветка изменённых и занятых (не 0xFF) байт в HEX и ASCII представлениях."""

    def __init__(self, document: Any, changed_offsets: Set[int], occupied_offsets: Set[int], bytes_per_line: int, ascii_mode: bool = False):
        super().__init__(document)
        self._changed_offsets = changed_offsets
        self._occupied_offsets = occupied_offsets
        self._bytes_per_line = bytes_per_line
        self._ascii_mode = ascii_mode
        changed_fmt = QTextCharFormat()
        changed_fmt.setForeground(QColor("#F44336"))
        changed_fmt.setFontWeight(QFont.Weight.Bold)
        self._changed_fmt = changed_fmt
        occupied_fmt = QTextCharFormat()
        occupied_fmt.setBackground(QColor("#3A3A4A"))
        self._occupied_fmt = occupied_fmt

    def highlightBlock(self, text: str) -> None:
        block = self.currentBlock()
        block_no = block.blockNumber()
        start_offset = block_no * self._bytes_per_line
        if self._ascii_mode:
            for i in range(min(len(text), self._bytes_per_line)):
                offset = start_offset + i
                changed = offset in self._changed_offsets
                occupied = offset in self._occupied_offsets
                if changed or occupied:
                    fmt = QTextCharFormat()
                    if changed:
                        fmt.setForeground(QColor("#F44336"))
                        fmt.setFontWeight(QFont.Weight.Bold)
                    if occupied:
                        fmt.setBackground(QColor("#3A3A4A"))
                    self.setFormat(i, 1, fmt)
        else:
            for i in range(self._bytes_per_line):
                offset = start_offset + i
                pos = i * 3
                if pos + 2 > len(text):
                    continue
                changed = offset in self._changed_offsets
                occupied = offset in self._occupied_offsets
                if changed or occupied:
                    fmt = QTextCharFormat()
                    if changed:
                        fmt.setForeground(QColor("#F44336"))
                        fmt.setFontWeight(QFont.Weight.Bold)
                    if occupied:
                        fmt.setBackground(QColor("#3A3A4A"))
                    self.setFormat(pos, 2, fmt)


class HexEditorDialog(QDialog):
    """Модальный HEX-редактор с адресами, HEX и ASCII."""

    BYTES_PER_LINE = 16

    def __init__(self, file_path: Optional[str] = None, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._config = Config()
        self.setWindowTitle(tr("HEX-редактор"))
        self.resize(950, 650)
        self._current_path: Optional[Path] = None
        self._base_address = 0
        self._data = bytearray()
        self._changed_offsets: Set[int] = set()
        self._occupied_offsets: Set[int] = set()
        self._saved_path: Optional[str] = None
        self._ignore_text_changes = False
        self._create_widgets()
        self._build_layout()
        self._apply_editor_theme()
        if file_path:
            self._load_file(file_path)

    def _create_widgets(self) -> None:
        font = QFont("Consolas", 10)
        if not QFont(font).exactMatch():
            font = QFont("Courier New", 10)

        self._kb_edit = QPlainTextEdit(self)
        self._kb_edit.setReadOnly(True)
        self._kb_edit.setFont(font)
        self._kb_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._kb_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._offset_edit = QPlainTextEdit(self)
        self._offset_edit.setReadOnly(True)
        self._offset_edit.setFont(font)
        self._offset_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._offset_edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._hex_edit = QPlainTextEdit(self)
        self._hex_edit.setFont(font)
        self._hex_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        HexHighlighter(self._hex_edit.document(), self._changed_offsets, self._occupied_offsets, self.BYTES_PER_LINE, ascii_mode=False)

        self._ascii_edit = QPlainTextEdit(self)
        self._ascii_edit.setFont(font)
        self._ascii_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        HexHighlighter(self._ascii_edit.document(), self._changed_offsets, self._occupied_offsets, self.BYTES_PER_LINE, ascii_mode=True)

        for edit in (self._kb_edit, self._offset_edit, self._hex_edit, self._ascii_edit):
            sb = edit.verticalScrollBar()
            if sb is not None:
                sb.valueChanged.connect(self._sync_scroll)

        self._hex_edit.textChanged.connect(self._on_hex_text_changed)
        self._ascii_edit.textChanged.connect(self._on_ascii_text_changed)

        self._open_button = QPushButton(tr("Открыть файл"))
        self._open_button.clicked.connect(self._on_open)
        self._save_button = QPushButton(tr("Сохранить"))
        self._save_button.clicked.connect(self._on_save)
        self._save_as_button = QPushButton(tr("Сохранить как..."))
        self._save_as_button.clicked.connect(self._on_save_as)

        self._status_label = QLabel(tr("Готов"))

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        layout.setContentsMargins(12, 12, 12, 12)

        button_layout = QHBoxLayout()
        button_layout.addWidget(self._open_button)
        button_layout.addWidget(self._save_button)
        button_layout.addWidget(self._save_as_button)
        button_layout.addStretch()
        layout.addLayout(button_layout)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._kb_edit)
        splitter.addWidget(self._offset_edit)
        splitter.addWidget(self._hex_edit)
        splitter.addWidget(self._ascii_edit)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 0)
        splitter.setStretchFactor(2, 1)
        splitter.setStretchFactor(3, 1)
        splitter.setSizes([90, 120, 450, 200])
        layout.addWidget(splitter, 1)

        layout.addWidget(self._status_label)

    def _apply_editor_theme(self) -> None:
        theme = """
        QPlainTextEdit {
            background-color: #1E1E2E;
            color: #E0E0E0;
            border: 1px solid #3A3A4A;
            selection-background-color: #4A6CFF;
            selection-color: #FFFFFF;
        }
        """
        for edit in (self._kb_edit, self._offset_edit, self._hex_edit, self._ascii_edit):
            edit.setStyleSheet(theme)

    def _sync_scroll(self, value: int) -> None:
        for edit in (self._kb_edit, self._offset_edit, self._hex_edit, self._ascii_edit):
            if edit.verticalScrollBar().value() != value:
                edit.verticalScrollBar().setValue(value)

    def _format_offset_text(self) -> str:
        lines = []
        for i in range(0, max(len(self._data), 1), self.BYTES_PER_LINE):
            lines.append(f"{self._base_address + i:08X}")
        return "\n".join(lines)

    def _format_kb_text(self) -> str:
        lines = []
        for i in range(0, max(len(self._data), 1), self.BYTES_PER_LINE):
            kb = (self._base_address + i) // 1024
            if kb >= 1024:
                mb = kb / 1024
                text = f"{mb:.2f}".rstrip("0").rstrip(".") + " MB"
            else:
                text = f"{kb} KB"
            lines.append(text)
        return "\n".join(lines)

    def _format_hex_text(self) -> str:
        lines = []
        for i in range(0, len(self._data), self.BYTES_PER_LINE):
            chunk = self._data[i : i + self.BYTES_PER_LINE]
            lines.append(" ".join(f"{b:02X}" for b in chunk))
        return "\n".join(lines)

    def _format_ascii_text(self) -> str:
        lines = []
        for i in range(0, len(self._data), self.BYTES_PER_LINE):
            chunk = self._data[i : i + self.BYTES_PER_LINE]
            lines.append("".join(chr(b) if 32 <= b < 127 else "." for b in chunk))
        return "\n".join(lines)

    def _refresh_all(self) -> None:
        self._ignore_text_changes = True
        cursor_hex = self._hex_edit.textCursor().position()
        cursor_ascii = self._ascii_edit.textCursor().position()
        self._kb_edit.setPlainText(self._format_kb_text())
        self._offset_edit.setPlainText(self._format_offset_text())
        self._hex_edit.setPlainText(self._format_hex_text())
        self._ascii_edit.setPlainText(self._format_ascii_text())
        cursor = self._hex_edit.textCursor()
        cursor.setPosition(min(cursor_hex, len(self._hex_edit.toPlainText())))
        self._hex_edit.setTextCursor(cursor)
        cursor = self._ascii_edit.textCursor()
        cursor.setPosition(min(cursor_ascii, len(self._ascii_edit.toPlainText())))
        self._ascii_edit.setTextCursor(cursor)
        self._offset_edit.verticalScrollBar().setValue(self._hex_edit.verticalScrollBar().value())
        self._ascii_edit.verticalScrollBar().setValue(self._hex_edit.verticalScrollBar().value())
        self._ignore_text_changes = False
        self._update_status()

    def _update_status(self) -> None:
        size = len(self._data)
        changed = len(self._changed_offsets)
        path = self._current_path.name if self._current_path else tr("(новый)")
        self._status_label.setText(tr("{0} | Размер: {1} байт | Изменено: {2}").format(path, size, changed))

    def _load_file(self, file_path: str) -> None:
        try:
            self._data = bytearray()
            self._base_address = 0
            self._changed_offsets.clear()
            self._occupied_offsets.clear()
            data, base = load_firmware_bytes(file_path)
            self._data = bytearray(data)
            self._base_address = base
            self._occupied_offsets.update(i for i, b in enumerate(self._data) if b != 0xFF)
            self._current_path = Path(file_path)
            self._saved_path = file_path
            self._refresh_all()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось открыть файл: {0}").format(exc))

    def _on_open(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Открыть файл прошивки"),
            "",
            tr("Прошивки (*.bin *.hex *.elf);;Все файлы (*.*)"),
        )
        if path:
            self._load_file(path)

    def _on_save(self) -> None:
        if not self._current_path:
            self._on_save_as()
            return
        self._save_to_path(self._current_path)

    def _on_save_as(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить как"),
            "",
            tr("Intel HEX (*.hex);;Бинарный файл (*.bin);;Все файлы (*.*)"),
        )
        if not path:
            return
        p = Path(path)
        if not p.suffix:
            p = p.with_suffix(".hex")
        self._save_to_path(p)
        if self._saved_path is None:
            self._saved_path = str(p)

    def _save_to_path(self, path: Path) -> None:
        try:
            if path.suffix.lower() == ".hex":
                _save_intel_hex(bytes(self._data), self._base_address, path)
            else:
                path.write_bytes(bytes(self._data))
            self._current_path = path
            self._changed_offsets.clear()
            self._refresh_all()
            self._saved_path = str(path)
            QMessageBox.information(self, tr("Сохранено"), tr("Файл сохранён: {0}").format(path))
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить файл: {0}").format(exc))

    def _on_hex_text_changed(self) -> None:
        if self._ignore_text_changes:
            return
        text = self._hex_edit.toPlainText()
        raw = re.sub(r"[^0-9A-Fa-f]", "", text)
        if not raw:
            new_data = bytearray()
        else:
            if len(raw) % 2:
                raw = "0" + raw
            try:
                new_data = bytearray.fromhex(raw)
            except ValueError:
                return
        self._apply_new_data(new_data)
        self._refresh_all()

    def _on_ascii_text_changed(self) -> None:
        if self._ignore_text_changes:
            return
        new_data = bytearray()
        document = self._ascii_edit.document()
        block = document.firstBlock()
        offset = 0
        while block.isValid():
            text = block.text()
            for ch in text:
                if offset < len(self._data):
                    old = self._data[offset]
                    expected = chr(old) if 32 <= old < 127 else "."
                    if ch == expected:
                        new_data.append(old)
                    elif ch == "." and old < 32 and offset not in self._changed_offsets:
                        new_data.append(old)
                    else:
                        new_data.append(ord(ch) & 0xFF)
                else:
                    new_data.append(ord(ch) & 0xFF)
                offset += 1
            block = block.next()
        self._apply_new_data(new_data)
        self._refresh_all()

    def _apply_new_data(self, new_data: bytearray) -> None:
        old = self._data
        length = min(len(old), len(new_data))
        for i in range(length):
            if new_data[i] != old[i]:
                self._changed_offsets.add(i)
        for i in range(length, len(new_data)):
            self._changed_offsets.add(i)
        self._data = new_data
        to_remove = [o for o in self._changed_offsets if o >= len(self._data)]
        for o in to_remove:
            self._changed_offsets.discard(o)

    @property
    def current_path(self) -> Optional[str]:
        return self._saved_path or (str(self._current_path) if self._current_path else None)


class ConnectWorker(QThread):
    """Фоновая проверка подключения к выбранному программатору."""

    log_line = Signal(str)
    finished = Signal(bool, dict)

    def __init__(self, method: str, config: Config, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._method = method
        self._config = config

    def run(self) -> None:
        try:
            if self._method == "auto":
                for m in ("stlink", "jlink", "uart", "usb_cdc", "usb"):
                    self.log_line.emit(tr("Автоопределение: проверка {0}...").format(m))
                    ok, info = self._try_method(m)
                    if ok:
                        info["method"] = m
                        self.finished.emit(True, info)
                        return
                self.finished.emit(False, {"error": tr("Не найден доступный программатор")})
            else:
                ok, info = self._try_method(self._method)
                info["method"] = self._method
                self.finished.emit(ok, info)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка ConnectWorker")
            self.finished.emit(False, {"error": str(exc)})

    def _try_method(self, method: str) -> Tuple[bool, Dict[str, Any]]:
        if method == "stlink":
            return self._try_stlink()
        if method == "jlink":
            return self._try_jlink()
        if method == "uart":
            return self._try_uart()
        if method == "usb_cdc":
            return self._try_usb_cdc()
        if method == "usb":
            return self._try_usb()
        return False, {"error": tr("Неизвестный метод")}

    def _try_stlink(self) -> Tuple[bool, Dict[str, Any]]:
        if not _PYOCD:
            return False, {"error": tr("pyocd не установлен")}
        try:
            probes = ConnectHelper.list_connected_probes()
            if not probes:
                return False, {"error": tr("ST-Link не найден")}
            return True, {
                "chip_id": tr("Неизвестно"),
                "flash_size": tr("Неизвестно"),
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}

    def _try_jlink(self) -> Tuple[bool, Dict[str, Any]]:
        if not _PYLINK:
            return False, {"error": tr("pylink-square не установлен")}
        try:
            jlink = pylink.JLink()
            jlink.open()
            jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
            jlink.close()
            return True, {"chip_id": tr("Неизвестно"), "flash_size": tr("Неизвестно")}
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}

    def _try_uart(self) -> Tuple[bool, Dict[str, Any]]:
        port = self._config.get("port", "")
        baud = self._config.get("baudrate", 115200)
        if not port:
            return False, {"error": tr("COM-порт не указан")}
        try:
            bl = Bootloader.open(port, baud)
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}
        try:
            bl.reconfigure_for_bootloader()
            bl.enter_bootloader()
            bl.sync()
            chip_id = bl.get_id()
            return True, {
                "chip_id": _format_chip_id(chip_id),
                "chip_id_int": chip_id,
                "flash_size": _flash_size_for_chip_id(chip_id),
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}
        finally:
            try:
                bl.port.close()
            except Exception:  # noqa: S110
                pass

    def _try_usb_cdc(self) -> Tuple[bool, Dict[str, Any]]:
        port = Bootloader.find_device_port(Bootloader.USB_VID, Bootloader.USB_BOOTLOADER_PID)
        if not port:
            port = Bootloader.find_device_port(Bootloader.USB_VID, Bootloader.USB_APPLICATION_PID)
        if not port:
            return False, {"error": tr("USB CDC устройство не найдено")}
        try:
            bl = Bootloader.open(port, 115200)
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}
        try:
            bl.reconfigure_for_bootloader()
            bl.enter_bootloader()
            bl.sync()
            chip_id = bl.get_id()
            return True, {
                "chip_id": _format_chip_id(chip_id),
                "chip_id_int": chip_id,
                "flash_size": _flash_size_for_chip_id(chip_id),
            }
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}
        finally:
            try:
                bl.port.close()
            except Exception:  # noqa: S110
                pass

    def try_method(self, method: str) -> Tuple[bool, Dict[str, Any]]:
        """Публичная обёртка над `_try_method()` для повторного использования
        авто-определения способа программирования вне ConnectWorker (см.
        `_auto_detect_method()` ниже, используется FlashWorker/ReadWorker)."""
        return self._try_method(method)

    def _try_usb(self) -> Tuple[bool, Dict[str, Any]]:
        if not _PYUSB:
            return False, {"error": tr("pyusb/libusb не установлен")}
        try:
            from core.dfu import (
                DFU_STATUS_OK,
                DfuDevice,
                STATE_DFU_ERROR,
                STATE_DFU_MANIFEST,
                STATE_DFU_MANIFEST_WAIT_RESET,
                find_dfu_device,
            )
            dev = find_dfu_device()
            # Проверяем, что устройство реально можно открыть и не занято другой программой.
            # Не шлём set_address — это могло переводить bootloader в dfuERROR.
            with DfuDevice(dev) as dfu:
                status = dfu._status(timeout=5000)
                if len(status) < 6:
                    raise RuntimeError("Некорректный DFU статус")
                state = status[4]
                bstatus = status[0]
                if state in (STATE_DFU_MANIFEST, STATE_DFU_MANIFEST_WAIT_RESET):
                    raise RuntimeError("Устройство перезагружается после DFU, подождите")
                if state == STATE_DFU_ERROR or bstatus != DFU_STATUS_OK:
                    raise RuntimeError(f"DFU статус: state=0x{state:02X}, bStatus=0x{bstatus:02X}")
            logger.info("USB DFU устройство доступно и свободно")
            return True, {"chip_id": tr("Неизвестно"), "flash_size": tr("Неизвестно")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("USB DFU не доступен: %s", exc)
            return False, {"error": str(exc)}


# Порядок перебора при методе "auto" для прошивки/чтения (FlashWorker/ReadWorker):
# сперва самые дешёвые/быстрые для проверки способы, которыми управляет сама
# прошивка устройства (наш bootloader может войти в режим программно — не
# требует физического BOOT0/джампера), затем настоящий STM32 ROM DFU (нужен
# BOOT0), и в конце отладочные пробники (могут требовать target_mcu/железо).
AUTO_METHOD_ORDER: Tuple[str, ...] = ("usb_cdc", "uart", "usb", "stlink", "jlink")


def _auto_detect_method(
    config: Config,
    log_callback: Optional[Callable[[str], None]] = None,
    order: Tuple[str, ...] = AUTO_METHOD_ORDER,
) -> Tuple[Optional[str], Dict[str, Any]]:
    """Перебирает способы программирования из `order` и возвращает первый,
    для которого реально нашлось устройство (тот же перебор, что делает
    ConnectWorker в режиме "Авто" при подключении, но переиспользуемый и для
    прошивки/чтения — см. CanBridge... нет, см. отчёт пользователя: запись
    только «Устройство»/«Серийный номер» не должна требовать от пользователя
    вручную угадывать способ программирования).

    Returns:
        (method, info) при успехе — `info` содержит доп. данные вроде chip_id;
        (None, {"error": ...}) если не найдено ни одно устройство.
    """
    prober = ConnectWorker("", config)
    for method in order:
        if log_callback:
            log_callback(tr("Автоопределение: проверка {0}...").format(method))
        ok, info = prober.try_method(method)
        if ok:
            info["method"] = method
            return method, info
    return None, {"error": tr("Не найдено ни одно поддерживаемое устройство (UART/USB CDC/USB DFU/ST-Link/J-Link)")}


class FlashWorker(QThread):
    """Фоновое программирование списка файлов."""

    log_line = Signal(str)
    progress = Signal(int)
    finished = Signal(bool, str)

    def __init__(
        self,
        files: List[str],
        method: str,
        config: Config,
        verify: bool = True,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._files = files
        self._method = method
        self._config = config
        self._verify = verify
        self._current_index = 0
        self._total = len(files)

    def run(self) -> None:
        for i, path in enumerate(self._files):
            self._current_index = i
            self.log_line.emit(tr("Прошивка {0}/{1}: {2}").format(i + 1, self._total, path))
            self.progress.emit(int(i / self._total * 100))
            try:
                ok, msg = self._flash_one(path)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Ошибка прошивки")
                ok, msg = False, str(exc)
            self.log_line.emit(msg)
            if not ok:
                self.finished.emit(False, tr("Ошибка на файле {0}: {1}").format(path, msg))
                return
            self.progress.emit(int((i + 1) / self._total * 100))
        self.finished.emit(True, tr("Все файлы прошиты успешно"))

    def _scaled_progress(self, local: int) -> int:
        return int((self._current_index + local / 100) / self._total * 100)

    def _resolve_method(self) -> Optional[str]:
        """При методе "auto" определяет реально доступный способ один раз и
        запоминает его в self._method (чтобы остальные файлы в этом же
        запуске не проверялись заново)."""
        if self._method != "auto":
            return self._method
        method, info = _auto_detect_method(self._config, log_callback=self.log_line.emit)
        if method is None:
            return None
        self.log_line.emit(tr("Автоопределение: используется {0}").format(method))
        self._method = method
        return method

    def _flash_one(self, file_path: str) -> Tuple[bool, str]:
        method = self._resolve_method()
        if method is None:
            return False, tr("Не найдено ни одно поддерживаемое устройство (UART/USB CDC/USB DFU/ST-Link/J-Link)")
        try:
            image, base = load_firmware_bytes(file_path)
            valid, reason = validate_application_vector(image, base)
            if not valid:
                return False, tr("Проверка application до записи не пройдена: {0}").format(reason)
        except Exception as exc:  # noqa: BLE001
            return False, tr("Не удалось проверить файл перед записью: {0}").format(exc)
        if method == "stlink":
            return self._flash_stlink(file_path)
        if method == "jlink":
            return self._flash_jlink(file_path)
        if method == "uart":
            return self._flash_uart(file_path)
        if method == "usb_cdc":
            return self._flash_usb_cdc(file_path)
        if method == "usb":
            return self._flash_usb(file_path)
        return False, tr("Неизвестный способ программирования")

    def _flash_stlink(self, file_path: str) -> Tuple[bool, str]:
        if not _PYOCD:
            return False, tr("pyocd не установлен")
        target = self._config.get("target_mcu", "")
        if not target:
            return False, tr("Не указана целевая МК (target_mcu)")
        data, base = load_firmware_bytes(file_path)
        if not data:
            return False, tr("Файл прошивки пуст")
        if not base:
            base = BOOTLOADER_BASE_ADDR
        try:
            with ConnectHelper.session_with_chosen_probe(
                return_first=True,
                auto_open=True,
                target_override=target,
            ) as session:
                target_obj = session.target
                target_obj.reset_and_halt()
                flash = next(
                    (r for r in target_obj.memory_map if r.is_flash and r.start <= base < r.end),
                    None,
                )
                if flash is None:
                    return False, tr("Адрес 0x%08X вне flash") % base
                self._config.set("total_memory", int(flash.length))
                if base == DEVICE_CONFIG_PAGE_ADDR and len(data) >= DEVICE_CONFIG_PAGE_SIZE:
                    existing = bytes(target_obj.read_memory_block8(base, DEVICE_CONFIG_PAGE_SIZE))
                    data = merge_device_config_page(data, existing)
                builder = FlashBuilder(flash)
                builder.add_data(base, data)
                builder.program(chip_erase="sector")
                target_obj.reset()
            return True, tr("ST-Link: прошивка завершена")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _flash_jlink(self, file_path: str) -> Tuple[bool, str]:
        if not _PYLINK:
            return False, tr("pylink-square не установлен")
        target = self._config.get("target_mcu", "")
        if not target:
            return False, tr("Не указана целевая МК (target_mcu)")
        data, base = load_firmware_bytes(file_path)
        if not data:
            return False, tr("Файл прошивки пуст")
        if not base:
            base = BOOTLOADER_BASE_ADDR
        try:
            jlink = pylink.JLink()
            jlink.open()
            jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
            jlink.connect(target=target, interface="SWD")
            if base == DEVICE_CONFIG_PAGE_ADDR and len(data) >= DEVICE_CONFIG_PAGE_SIZE:
                existing = bytes(jlink.memory_read(base, DEVICE_CONFIG_PAGE_SIZE))
                data = merge_device_config_page(data, existing)
            flash_size = STM32_FLASH_SIZES.get(target, 256) * 1024
            if base == BOOTLOADER_BASE_ADDR and len(data) >= flash_size:
                jlink.erase()
            jlink.flash(base, data)
            if self._verify:
                read = bytes(jlink.memory_read(base, len(data)))
                ok = read == data
            else:
                ok = True
            jlink.reset()
            jlink.close()
            return ok, tr("J-Link: прошивка завершена") if ok else tr("J-Link: верификация не прошла")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)

    def _flash_uart(self, file_path: str) -> Tuple[bool, str]:
        port = self._config.get("port", "")
        baud = self._config.get("baudrate", 115200)
        if not port:
            return False, tr("COM-порт не указан")
        try:
            bl = Bootloader.open(port, baud, progress_callback=lambda p: self.progress.emit(self._scaled_progress(p)))
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        try:
            data, base = load_firmware_bytes(file_path)
            if not base:
                base = APPLICATION_BASE_ADDR
            if not data:
                return False, tr("Файл прошивки пуст")
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                tmp.write(data)
                bin_path = tmp.name
            try:
                target = self._config.get("target_mcu", "")
                page_size = STM32_PAGE_SIZES.get(target, 2048)
                # Как и в USB DFU-пути: если пишем полный образ Flash (с бутлоадером
                # и/или конфигом), стираем все страницы, иначе старые данные на
                # "пустых" страницах не совпадут с 0xFF и верификация не пройдёт.
                flash_size = STM32_FLASH_SIZES.get(target, 256) * 1024
                skip_blank = len(data) < flash_size
                logger.info(
                    "UART прошивка: %s, base=0x%08X, размер=%d, page_size=%d, skip_blank=%s",
                    file_path, base, len(data), page_size, skip_blank,
                )
                bl.flash_firmware(bin_path, base, page_size=page_size, skip_blank=skip_blank)
                ok = bl.verify(base, data) if self._verify else True
                return ok, tr("UART прошивка завершена: {0}").format(file_path)
            finally:
                try:
                    Path(bin_path).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("UART прошивка ошибка: %s", file_path)
            return False, str(exc)
        finally:
            try:
                bl.port.close()
            except Exception:  # noqa: S110
                pass

    def _flash_usb_cdc(self, file_path: str) -> Tuple[bool, str]:
        port = Bootloader.find_device_port(Bootloader.USB_VID, Bootloader.USB_BOOTLOADER_PID)
        if not port:
            port = Bootloader.find_device_port(Bootloader.USB_VID, Bootloader.USB_APPLICATION_PID)
        if not port:
            return False, tr("USB CDC устройство не найдено")
        try:
            bl = Bootloader.open(port, 115200, progress_callback=lambda p: self.progress.emit(self._scaled_progress(p)))
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        try:
            data, base = load_firmware_bytes(file_path)
            if not base:
                base = APPLICATION_BASE_ADDR
            if not data:
                return False, tr("Файл прошивки пуст")
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                tmp.write(data)
                bin_path = tmp.name
            try:
                target = self._config.get("target_mcu", "")
                page_size = STM32_PAGE_SIZES.get(target, 2048)
                flash_size = STM32_FLASH_SIZES.get(target, 256) * 1024
                skip_blank = len(data) < flash_size
                logger.info(
                    "USB CDC прошивка: %s, base=0x%08X, размер=%d, page_size=%d, skip_blank=%s",
                    file_path, base, len(data), page_size, skip_blank,
                )
                bl.flash_firmware(bin_path, base, page_size=page_size, skip_blank=skip_blank)
                ok = bl.verify(base, data) if self._verify else True
                return ok, tr("USB CDC прошивка завершена: {0}").format(file_path)
            finally:
                try:
                    Path(bin_path).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("USB CDC прошивка ошибка: %s", file_path)
            return False, str(exc)
        finally:
            try:
                bl.port.close()
            except Exception:  # noqa: S110
                pass

    def _flash_usb(self, file_path: str) -> Tuple[bool, str]:
        if not _PYUSB:
            return False, tr("pyusb/libusb не установлены")

        target = self._config.get("target_mcu", "")
        page_size = STM32_PAGE_SIZES.get(target, 2048)
        path = Path(file_path)
        try:
            from core.dfu import DfuDevice, find_dfu_device

            # Не превращаем разреженный HEX в один огромный BIN: при записи
            # прошивки + конфига передаём только реальные сегменты.
            if path.suffix.lower() == ".hex":
                from intelhex import IntelHex

                image = IntelHex(str(path))
                segments = [
                    (start, bytes(image.tobinarray(start=start, end=end - 1)))
                    for start, end in image.segments()
                ]
            else:
                data, base = load_firmware_bytes(file_path)
                if not base:
                    base = BOOTLOADER_BASE_ADDR
                segments = [(base, data)]
            segments = [(start, data) for start, data in segments if data]
            if not segments:
                return False, tr("Файл прошивки пуст")

            total_bytes = sum(len(data) for _, data in segments)
            logger.info(
                "USB DFU прошивка: файл=%s, сегментов=%d, размер=%d, page_size=%d, МК=%s",
                file_path, len(segments), total_bytes, page_size, target or "не указан",
            )
            dev = find_dfu_device()

            def _progress_for_segment(start_pct: int, end_pct: int, segment_offset: int):
                last_pct = -1

                def _progress(current: int, total: int) -> None:
                    nonlocal last_pct
                    if not total:
                        return
                    overall = (segment_offset + min(current, total)) / total_bytes
                    pct = int(start_pct + overall * (end_pct - start_pct))
                    if pct != last_pct:
                        last_pct = pct
                        self.progress.emit(pct)

                return _progress

            with DfuDevice(dev) as dfu:
                # Конфиг-запись должна сохранить VID/PID, reserved и будущие
                # поля страницы. Сначала читаем только 2-КБ страницу, затем
                # объединяем её с новым именем/serial и лишь после этого стираем.
                preserved_segments = []
                for start, data in segments:
                    incoming = parse_device_config(data)
                    if start == DEVICE_CONFIG_PAGE_ADDR and len(data) >= DEVICE_CONFIG_PAGE_SIZE and incoming:
                        try:
                            current_page = dfu.upload(start, DEVICE_CONFIG_PAGE_SIZE)
                        except Exception as exc:  # noqa: BLE001
                            logger.warning(
                                "Не удалось прочитать старую config-страницу DFU, "
                                "записываю валидную страницу по умолчанию: %s",
                                exc,
                            )
                        else:
                            data = build_device_config_page(incoming[0], incoming[1], current_page)
                    preserved_segments.append((start, data))
                segments = preserved_segments

                offset = 0
                self.log_line.emit(tr("USB DFU: стирание Flash..."))
                for start, data in segments:
                    dfu.erase_pages(
                        start,
                        data,
                        page_size=page_size,
                        skip_blank=True,
                        progress=_progress_for_segment(0, 15, offset),
                    )
                    offset += len(data)

                self.log_line.emit(tr("USB DFU: запись {0} байт...").format(total_bytes))
                offset = 0
                for start, data in segments:
                    dfu.download(
                        start,
                        data,
                        progress=_progress_for_segment(15, 50, offset),
                    )
                    dfu.abort()
                    offset += len(data)

                ok = True
                if self._verify:
                    self.log_line.emit(tr("USB DFU: верификация..."))
                    offset = 0
                    for start, data in segments:
                        read_back = dfu.upload(
                            start,
                            len(data),
                            progress=_progress_for_segment(50, 90, offset),
                        )
                        if read_back != data:
                            logger.error(
                                "DFU verify mismatch: адрес=0x%08X, ожидалось=%d, прочитано=%d",
                                start, len(data), len(read_back),
                            )
                            ok = False
                            break
                        offset += len(data)

                self.log_line.emit(tr("USB DFU: завершение..."))
                try:
                    dfu.abort()
                except (usb.core.USBError, OSError) as exc:
                    logger.info("DFU: устройство отключилось при финальном ABORT: %s", exc)
                try:
                    dfu.leave()
                except (usb.core.USBError, OSError) as exc:
                    # ROM DFU обычно отключает USB сразу после reset; это
                    # нормальный финал уже успешно проверенной записи.
                    logger.info("DFU: устройство отключилось при выходе: %s", exc)
            self.progress.emit(100)
            return ok, tr("USB DFU: прошивка завершена") if ok else tr("USB DFU: верификация не прошла")
        except usb.core.NoBackendError:
            return False, tr("USB backend не найден. Установите libusb-package или WinUSB-драйвер через Zadig.")
        except Exception as exc:  # noqa: BLE001
            logger.exception("USB DFU ошибка при прошивке %s", file_path)
            return False, tr("USB DFU ошибка: {0}").format(exc)


class ReadWorker(QThread):
    """Фоновое чтение флеш-памяти микроконтроллера."""

    log_line = Signal(str)
    finished = Signal(bool, str, object, int)

    def __init__(
        self,
        method: str,
        config: Config,
        size: int = 0x10000,
        start: int = BOOTLOADER_BASE_ADDR,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._method = method
        self._config = config
        self._size = size
        self._start = start

    def run(self) -> None:
        try:
            data, base = self._read_one()
            self.finished.emit(True, tr("Чтение завершено: {0} байт").format(len(data)), data, base)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка чтения прошивки")
            self.finished.emit(False, str(exc), b"", 0)

    def _resolve_method(self) -> str:
        """При методе "auto" определяет реально доступный способ (см.
        FlashWorker._resolve_method) и запоминает его."""
        if self._method != "auto":
            return self._method
        method, info = _auto_detect_method(self._config, log_callback=self.log_line.emit)
        if method is None:
            raise RuntimeError(tr("Не найдено ни одно поддерживаемое устройство (UART/USB CDC/USB DFU/ST-Link/J-Link)"))
        self.log_line.emit(tr("Автоопределение: используется {0}").format(method))
        self._method = method
        return method

    def _read_one(self) -> Tuple[bytes, int]:
        method = self._resolve_method()
        if method == "stlink":
            return self._read_stlink()
        if method == "jlink":
            return self._read_jlink()
        if method == "uart":
            return self._read_uart()
        if method == "usb_cdc":
            return self._read_usb_cdc()
        if method == "usb":
            return self._read_usb()
        raise RuntimeError(tr("Чтение не поддерживается для {0}").format(method))

    def _read_stlink(self) -> Tuple[bytes, int]:
        if not _PYOCD:
            raise RuntimeError(tr("pyocd не установлен"))
        target = self._config.get("target_mcu", "")
        if not target:
            raise RuntimeError(tr("Не указана целевая МК (target_mcu)"))
        with ConnectHelper.session_with_chosen_probe(
            return_first=True,
            auto_open=True,
            target_override=target,
        ) as session:
            target_obj = session.target
            target_obj.reset_and_halt()
            flash = next(
                (r for r in target_obj.memory_map if r.is_flash),
                None,
            )
            if flash is not None:
                self._config.set("total_memory", int(flash.length))
            start = self._start
            size = self._size
            data = bytes(target_obj.read_memory_block8(start, size))
            target_obj.reset()
        return data, start

    def _read_jlink(self) -> Tuple[bytes, int]:
        if not _PYLINK:
            raise RuntimeError(tr("pylink-square не установлен"))
        target = self._config.get("target_mcu", "")
        if not target:
            raise RuntimeError(tr("Не указана целевая МК (target_mcu)"))
        jlink = pylink.JLink()
        jlink.open()
        jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
        jlink.connect(target=target, interface="SWD")
        start = self._start
        data = bytes(jlink.memory_read(start, self._size))
        jlink.close()
        return data, start

    def _read_uart(self) -> Tuple[bytes, int]:
        port = self._config.get("port", "")
        baud = self._config.get("baudrate", 115200)
        if not port:
            raise RuntimeError(tr("COM-порт не указан"))
        bl = Bootloader.open(port, baud)
        try:
            bl.reconfigure_for_bootloader()
            bl.enter_bootloader()
            bl.sync()
            start = self._start
            data = bl.read_memory(start, self._size)
            return data, start
        finally:
            try:
                bl.port.close()
            except Exception:  # noqa: S110
                pass

    def _read_usb_cdc(self) -> Tuple[bytes, int]:
        port = Bootloader.find_device_port(Bootloader.USB_VID, Bootloader.USB_BOOTLOADER_PID)
        if not port:
            port = Bootloader.find_device_port(Bootloader.USB_VID, Bootloader.USB_APPLICATION_PID)
        if not port:
            raise RuntimeError(tr("USB CDC устройство не найдено"))
        bl = Bootloader.open(port, 115200)
        try:
            bl.reconfigure_for_bootloader()
            bl.enter_bootloader()
            bl.sync()
            start = self._start
            data = bl.read_memory(start, self._size)
            return data, start
        finally:
            try:
                bl.port.close()
            except Exception:  # noqa: S110
                pass

    def _read_usb(self) -> Tuple[bytes, int]:
        if not _PYUSB:
            raise RuntimeError(tr("pyusb/libusb не установлены"))
        from core.dfu import DfuDevice, find_dfu_device
        try:
            dev = find_dfu_device()
        except usb.core.NoBackendError:
            raise RuntimeError(tr("USB backend не найден. Установите libusb-package или WinUSB-драйвер через Zadig."))
        with DfuDevice(dev) as dfu:
            start = self._start
            data = dfu.upload(start, self._size)
        return data, start


class EraseWorker(QThread):
    '''Фоновое полное стирание Flash микроконтроллера.'''

    log_line = Signal(str)
    finished = Signal(bool, str)

    def __init__(
        self,
        method: str,
        config: Config,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._method = method
        self._config = config

    def run(self) -> None:
        try:
            method = self._resolve_method()
            if method is None:
                raise RuntimeError(tr('Не найдено ни одно поддерживаемое устройство'))
            if method == 'stlink':
                self._erase_stlink()
            elif method == 'jlink':
                self._erase_jlink()
            elif method == 'uart':
                self._erase_uart()
            elif method == 'usb_cdc':
                self._erase_usb_cdc()
            elif method == 'usb':
                self._erase_usb()
            else:
                raise RuntimeError(tr('Стирание не поддерживается для {0}').format(method))
            self.finished.emit(True, tr('Flash успешно стёрт'))
        except Exception as exc:  # noqa: BLE001
            logger.exception('Ошибка стирания Flash')
            self.finished.emit(False, str(exc))

    def _resolve_method(self) -> Optional[str]:
        if self._method != 'auto':
            return self._method
        method, info = _auto_detect_method(self._config, log_callback=self.log_line.emit)
        if method is None:
            return None
        self.log_line.emit(tr('Автоопределение: используется {0}').format(method))
        self._method = method
        return method

    def _erase_uart(self) -> None:
        port = self._config.get('port', '')
        if not port:
            raise RuntimeError(tr('COM-порт не указан'))
        bl = Bootloader.open(port, self._config.get('baudrate', 115200))
        try:
            bl.reconfigure_for_bootloader()
            bl.enter_bootloader()
            bl.sync()
            bl.erase(extended=True)
        finally:
            try:
                bl.port.close()
            except Exception:  # noqa: S110
                pass

    def _erase_usb_cdc(self) -> None:
        port = Bootloader.find_device_port(Bootloader.USB_VID, Bootloader.USB_BOOTLOADER_PID)
        if not port:
            port = Bootloader.find_device_port(Bootloader.USB_VID, Bootloader.USB_APPLICATION_PID)
        if not port:
            raise RuntimeError(tr('USB CDC устройство не найдено'))
        bl = Bootloader.open(port, 115200)
        try:
            bl.reconfigure_for_bootloader()
            bl.enter_bootloader()
            bl.sync()
            bl.erase(extended=True)
        finally:
            try:
                bl.port.close()
            except Exception:  # noqa: S110
                pass

    def _erase_usb(self) -> None:
        if not _PYUSB:
            raise RuntimeError(tr('pyusb/libusb не установлены'))
        from core.dfu import DfuDevice, find_dfu_device
        dev = find_dfu_device()
        with DfuDevice(dev) as dfu:
            self.log_line.emit(tr('DFU: массовое стирание Flash...'))
            dfu.mass_erase()

    def _erase_stlink(self) -> None:
        if not _PYOCD:
            raise RuntimeError(tr('pyocd не установлен'))
        target = self._config.get('target_mcu', '')
        if not target:
            raise RuntimeError(tr('Не указана целевая МК (target_mcu)'))
        with ConnectHelper.session_with_chosen_probe(
            return_first=True,
            auto_open=True,
            target_override=target,
        ) as session:
            target_obj = session.target
            target_obj.reset_and_halt()
            target_obj.mass_erase()
            target_obj.reset()

    def _erase_jlink(self) -> None:
        if not _PYLINK:
            raise RuntimeError(tr('pylink-square не установлен'))
        target = self._config.get('target_mcu', '')
        if not target:
            raise RuntimeError(tr('Не указана целевая МК (target_mcu)'))
        jlink = pylink.JLink()
        jlink.open()
        jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
        jlink.connect(target=target, interface='SWD')
        try:
            jlink.erase()
        finally:
            jlink.close()


class FlashDialog(QDialog):
    """Полноценный диалог прошивки микроконтроллера."""

    def __init__(self, serial_manager: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self.setWindowTitle(tr("Прошить микроконтроллер"))
        self.resize(900, 700)
        self._connect_worker: Optional[ConnectWorker] = None
        self._flash_worker: Optional[FlashWorker] = None
        self._read_worker: Optional[ReadWorker] = None
        self._erase_worker: Optional[EraseWorker] = None
        self._connected = False
        self._last_chip_info: Dict[str, Any] = {}
        self._log_file: Optional[Path] = None
        self._create_widgets()
        self._build_layout()
        self._connect_signals()
        self._load_defaults()

    def _is_any_worker_running(self) -> bool:
        return any(
            worker is not None and worker.isRunning()
            for worker in (self._connect_worker, self._flash_worker, self._read_worker, self._erase_worker)
        )

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        # Устройство и программатор
        self._device_name_label = QLabel(tr("Устройство"))
        self._device_name_label.setFont(font)
        self._device_name_edit = QLineEdit()
        self._device_name_edit.setFont(font)
        self._device_name_edit.setMaxLength(DEVICE_CONFIG_NAME_MAX)
        self._device_name_edit.setPlaceholderText("2CAN")

        self._serial_label = QLabel(tr("Серийный номер"))
        self._serial_label.setFont(font)
        self._serial_edit = QLineEdit()
        self._serial_edit.setFont(font)

        self._method_label = QLabel(tr("Способ программирования"))
        self._method_label.setFont(font)
        self._method_combo = QComboBox()
        self._method_combo.setFont(font)
        for value, label in PROGRAMMER_METHODS:
            self._method_combo.addItem(tr(label), value)

        self._port_button = QPushButton(tr("Выбрать порт"))
        self._port_button.setFont(font)
        self._port_button.setVisible(False)

        self._connect_button = QPushButton(tr("Подключиться"))
        self._connect_button.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))

        self._chip_info_label = QLabel(tr("Информация о чипе: не подключено"))
        self._chip_info_label.setFont(font)

        # Модель чипа
        self._chip_label = QLabel(tr("Модель чипа"))
        self._chip_label.setFont(font)
        self._chip_combo = QComboBox()
        self._chip_combo.setFont(font)
        self._chip_combo.setMinimumWidth(160)
        self._chip_combo.addItem(tr("Вручную"), "")
        for model in sorted(STM32_FLASH_SIZES):
            self._chip_combo.addItem(model, model)

        # Целевая МК (для pyocd/pylink)
        self._target_mcu_label = QLabel(tr("Целевая МК"))
        self._target_mcu_edit = QLineEdit()
        self._target_mcu_edit.setPlaceholderText(tr("Например: STM32F103RC"))

        # Размер flash-памяти (KB)
        self._read_size_label = QLabel(tr("Размер памяти (КБ)"))
        self._read_size_edit = QComboBox()
        self._read_size_edit.setEditable(True)
        self._read_size_edit.addItems(["16", "32", "64", "128", "256", "512", "1024", "2048", "4096", "8192", "16384", "32768"])
        self._read_size_edit.setCurrentText("64")
        self._read_size_edit.setMaximumWidth(90)

        self._config_button = QPushButton(tr("Записать конфигурацию устройства"))
        self._config_button.setFont(font)
        self._config_button.setCheckable(True)
        self._config_button.setEnabled(True)
        self._config_button.setToolTip(tr("Дописать имя и серийный номер в прошивку/конфиг при нажатии 'Прошить'"))

        self._verify_checkbox = QCheckBox(tr("Проверять после записи"))
        self._verify_checkbox.setChecked(True)
        self._verify_checkbox.setToolTip(tr("Отключите, чтобы ускорить прошивку за счёт пропуска чтения обратно"))

        # Файлы прошивки
        self._files_group = QGroupBox(tr("Файлы прошивки"))
        self._files_list = QListWidget()
        self._browse_button = QPushButton(tr("Обзор"))
        self._up_button = QPushButton(tr("Вверх"))
        self._down_button = QPushButton(tr("Вниз"))
        self._remove_button = QPushButton(tr("Удалить"))

        self._power_button = QPushButton(tr("Питание 3.3V ВКЛ"))
        self._reset_button = QPushButton(tr("Сброс"))
        self._identify_button = QPushButton(tr("Определить чип"))

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)

        self._log_edit = QPlainTextEdit()
        self._log_edit.setReadOnly(True)
        self._log_edit.setFont(QFont("Consolas", 9))

        self._flash_button = QPushButton(tr("Прошить"))
        self._flash_button.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        self._read_button = QPushButton(tr("Прочитать прошивку"))
        self._read_button.setFont(font)
        self._read_config_button = QPushButton(tr("Прочитать конфигурацию"))
        self._read_config_button.setFont(font)
        self._erase_button = QPushButton(tr("Стереть Flash"))
        self._erase_button.setFont(font)
        self._erase_button.setStyleSheet("background-color: #F44336; color: #FFFFFF;")
        self._hex_editor_button = QPushButton(tr("Открыть HEX-редактор"))
        self._close_button = QPushButton(tr("Закрыть"))

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        top_grid = QHBoxLayout()
        top_grid.addWidget(self._device_name_label)
        top_grid.addWidget(self._device_name_edit, 1)
        top_grid.addWidget(self._serial_label)
        top_grid.addWidget(self._serial_edit)
        top_grid.addWidget(self._method_label)
        top_grid.addWidget(self._method_combo)
        top_grid.addWidget(self._port_button)
        top_grid.addWidget(self._connect_button)
        layout.addLayout(top_grid)

        layout.addWidget(self._chip_info_label)

        target_mcu_layout = QHBoxLayout()
        target_mcu_layout.addWidget(self._chip_label)
        target_mcu_layout.addWidget(self._chip_combo)
        target_mcu_layout.addWidget(self._target_mcu_label)
        target_mcu_layout.addWidget(self._target_mcu_edit, 1)
        target_mcu_layout.addWidget(self._read_size_label)
        target_mcu_layout.addWidget(self._read_size_edit)
        layout.addLayout(target_mcu_layout)

        config_layout = QHBoxLayout()
        config_layout.addWidget(self._config_button)
        config_layout.addWidget(self._verify_checkbox)
        config_layout.addStretch()
        layout.addLayout(config_layout)

        files_main = QHBoxLayout()
        files_main.addWidget(self._files_list, 1)
        files_buttons = QVBoxLayout()
        files_buttons.addWidget(self._browse_button)
        files_buttons.addWidget(self._up_button)
        files_buttons.addWidget(self._down_button)
        files_buttons.addWidget(self._remove_button)
        files_buttons.addStretch()
        files_buttons.addWidget(self._power_button)
        files_buttons.addWidget(self._reset_button)
        files_buttons.addWidget(self._identify_button)
        files_main.addLayout(files_buttons)
        self._files_group.setLayout(files_main)
        layout.addWidget(self._files_group, 1)

        layout.addWidget(self._progress_bar)
        layout.addWidget(self._log_edit, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        bottom.addWidget(self._flash_button)
        bottom.addWidget(self._read_button)
        bottom.addWidget(self._read_config_button)
        bottom.addWidget(self._erase_button)
        bottom.addWidget(self._hex_editor_button)
        bottom.addWidget(self._close_button)
        layout.addLayout(bottom)

    def _connect_signals(self) -> None:
        self._connect_button.clicked.connect(self._on_connect)
        self._port_button.clicked.connect(self._on_select_port)
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        self._browse_button.clicked.connect(self._on_browse)
        self._up_button.clicked.connect(self._on_move_up)
        self._down_button.clicked.connect(self._on_move_down)
        self._remove_button.clicked.connect(self._on_remove)
        self._flash_button.clicked.connect(self._on_flash)
        self._read_button.clicked.connect(self._on_read_firmware)
        self._read_config_button.clicked.connect(self._on_read_config)
        self._erase_button.clicked.connect(self._on_erase_flash)
        self._hex_editor_button.clicked.connect(lambda: self.open_hex_editor())
        self._close_button.clicked.connect(self.reject)
        self._power_button.clicked.connect(self._on_power_toggle)
        self._reset_button.clicked.connect(self._on_reset)
        self._identify_button.clicked.connect(self._on_identify)

        self._target_mcu_edit.editingFinished.connect(
            lambda: self._config.set("target_mcu", self._target_mcu_edit.text().strip().upper())
        )

        self._device_name_edit.editingFinished.connect(
            lambda: self._config.set("device_type_name", self._device_name_edit.text().strip())
        )
        self._serial_edit.editingFinished.connect(
            lambda: self._config.set_bulk({
                "serial_number": self._serial_edit.text().strip(),
                "device_serial": self._serial_edit.text().strip(),
            })
        )

        self._chip_combo.currentIndexChanged.connect(self._on_chip_changed)
        self._config_button.toggled.connect(self._on_config_toggled)

    def _load_defaults(self) -> None:
        serial = self._config.get("device_serial", "")
        if not serial:
            serial = self._config.get("serial_number", "")
        self._serial_edit.setText(serial)
        method = self._config.get("programmer_method", "stlink")
        index = self._method_combo.findData(method)
        if index >= 0:
            self._method_combo.setCurrentIndex(index)
        target_mcu = self._config.get("target_mcu", "") or "STM32F105RCT6"
        self._target_mcu_edit.setText(target_mcu)
        self._config.set("target_mcu", target_mcu)
        if target_mcu in STM32_FLASH_SIZES:
            idx = self._chip_combo.findData(target_mcu)
            if idx >= 0:
                self._chip_combo.setCurrentIndex(idx)
        total_kb = self._config.get("total_memory", STM32_FLASH_SIZES.get("STM32F105RCT6", 256) * 1024) // 1024
        if total_kb > 0:
            self._read_size_edit.setCurrentText(str(total_kb))
        self._device_name_edit.setText(self._config.get("device_type_name", ""))
        self._update_power_button()
        self._on_method_changed(self._method_combo.currentIndex())

    def _on_method_changed(self, index: int) -> None:
        method = self._method_combo.itemData(index)
        if method:
            self._config.set("programmer_method", method)
        is_stlink = method == "stlink"
        is_com = method in ("uart", "usb_cdc")
        self._power_button.setVisible(False)
        self._reset_button.setVisible(is_stlink)
        self._identify_button.setVisible(is_stlink)
        self._port_button.setVisible(is_com)
        self._set_connect_status(False, {})

    def _on_chip_changed(self, index: int) -> None:
        model = self._chip_combo.itemData(index)
        if model and model in STM32_FLASH_SIZES:
            self._read_size_edit.setCurrentText(str(STM32_FLASH_SIZES[model]))
            self._target_mcu_edit.setText(model)
            self._config.set("target_mcu", model)

    def _get_flash_size_kb(self) -> int:
        text = self._read_size_edit.currentText().strip()
        if text:
            try:
                return int(text)
            except ValueError as exc:
                raise ValueError(tr("Некорректный размер памяти")) from exc
        target = self._target_mcu_edit.text().strip().upper()
        if "F105" in target:
            return 256
        model = self._chip_combo.currentData()
        if model and model in STM32_FLASH_SIZES:
            return STM32_FLASH_SIZES[model]
        return 256

    def _get_page_size(self) -> int:
        """Возвращает размер физической страницы Flash для выбранной модели МК.

        Пусть пользователь не сможет записать конфиг по фиктивному 2048 байт,
        если у чипа страница другого размера — это главный риск «окирпичивания».
        """
        target = self._target_mcu_edit.text().strip().upper()
        if target in STM32_PAGE_SIZES:
            return STM32_PAGE_SIZES[target]
        model = self._chip_combo.currentData()
        if model and model in STM32_PAGE_SIZES:
            return STM32_PAGE_SIZES[model]
        raise ValueError(
            tr("Не удалось определить размер страницы Flash для {0}. "
               "Выберите модель МК из списка или укажите вручную.").format(
                target or model or tr("неизвестно")
            )
        )

    def _prepare_config_only_hex(self) -> str:
        """Создаёт HEX только с реальной страницей конфигурации firmware."""
        name = self._device_name_edit.text().strip()
        if not name:
            raise ValueError(tr("Заполните поле «Устройство»"))
        serial = self._serial_edit.text().strip()
        if not serial:
            raise ValueError(tr("Введите серийный номер"))

        page = build_device_config_page(name, serial)
        tmp = Path(tempfile.gettempdir()) / f"config_only_{int(time.time())}.hex"
        _save_intel_hex(page, DEVICE_CONFIG_PAGE_ADDR, tmp)
        return str(tmp)

    def _on_config_toggled(self, checked: bool) -> None:
        """Переключает режим встраивания конфигурации в прошивку.

        При активации проверяются поля «Устройство» и «Серийный номер».
        Сама запись начинается только по кнопке «Прошить».
        """
        if not checked:
            self._config_button.setStyleSheet("")
            return

        name = self._device_name_edit.text().strip()
        serial = self._serial_edit.text().strip()
        if not name or not serial:
            self._config_button.setChecked(False)
            QMessageBox.warning(
                self,
                tr("Внимание"),
                tr("Заполните поля «Устройство» и «Серийный номер» для записи конфигурации"),
            )
            return
        try:
            self._get_flash_size_kb()
        except ValueError as exc:
            self._config_button.setChecked(False)
            QMessageBox.warning(self, tr("Внимание"), str(exc))
            return

        self._config_button.setStyleSheet("QPushButton { background-color: #4CAF50; color: #FFFFFF; }")

    def _set_connect_status(self, connected: bool, info: Dict[str, Any]) -> None:
        self._connected = connected
        if connected:
            self._connect_button.setText(tr("Подключено"))
            self._connect_button.setStyleSheet("background-color: #4CAF50; color: #FFFFFF;")
            chip_id = info.get("chip_id", tr("Неизвестно"))
            flash_size = info.get("flash_size", tr("Неизвестно"))
            self._chip_info_label.setText(tr("ID: {0} | Flash: {1}").format(chip_id, flash_size))
        else:
            error = info.get("error", "")
            if error:
                self._connect_button.setText(tr("Ошибка подключения"))
                self._connect_button.setStyleSheet("background-color: #F44336; color: #FFFFFF;")
            else:
                self._connect_button.setText(tr("Подключиться"))
                self._connect_button.setStyleSheet("")
            self._chip_info_label.setText(tr("Информация о чипе: {0}").format(error or tr("не подключено")))

    def _on_connect(self) -> None:
        if self._connected:
            self._disconnect()
        else:
            self._start_connect()

    def _start_connect(self) -> None:
        if self._connect_worker and self._connect_worker.isRunning():
            return
        if self._flash_worker and self._flash_worker.isRunning():
            self._log(tr("Невозможно подключиться: выполняется прошивка"))
            return
        if self._read_worker and self._read_worker.isRunning():
            self._log(tr("Невозможно подключиться: выполняется чтение"))
            return
        method = self._method_combo.currentData()
        logger.info("Начало подключения: метод=%s", method)
        self._log(tr("Подключение через {0}...").format(method))
        self._connect_button.setEnabled(False)
        self._connect_worker = ConnectWorker(method, self._config, self)
        self._connect_worker.log_line.connect(self._log)
        self._connect_worker.finished.connect(self._on_connect_finished)
        self._connect_worker.start()

    def _disconnect(self) -> None:
        if self._connect_worker and self._connect_worker.isRunning():
            self._connect_worker.terminate()
            self._connect_worker.wait(1000)
        self._connected = False
        self._last_chip_info = {}
        self._set_connect_status(False, {})
        self._log(tr("Отключено"))

    def _on_identify(self) -> None:
        self._disconnect()
        self._start_connect()

    def _on_connect_finished(self, success: bool, info: Dict[str, Any]) -> None:
        logger.info("Подключение завершено: success=%s, info=%s", success, info)
        self._connect_button.setEnabled(True)
        if success and info.get("method") and info["method"] != self._method_combo.currentData():
            index = self._method_combo.findData(info["method"])
            if index >= 0:
                self._method_combo.setCurrentIndex(index)
        if success:
            model = info.get("model", "")
            if model:
                idx = self._chip_combo.findData(model)
                if idx >= 0:
                    self._chip_combo.setCurrentIndex(idx)
            flash_size_kb = info.get("flash_size_kb")
            if flash_size_kb:
                self._read_size_edit.setCurrentText(str(flash_size_kb))
        self._last_chip_info = info
        self._set_connect_status(success, info)

    def _on_browse(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            tr("Выберите файлы прошивки"),
            "",
            tr("Прошивки (*.bin *.hex *.elf);;Все файлы (*.*)"),
        )
        for f in files:
            if self._files_list.findItems(f, Qt.MatchFlag.MatchExactly):
                continue
            self._files_list.addItem(f)
        self._update_log_file()

    def _on_move_up(self) -> None:
        row = self._files_list.currentRow()
        if row > 0:
            item = self._files_list.takeItem(row)
            self._files_list.insertItem(row - 1, item)
            self._files_list.setCurrentRow(row - 1)

    def _on_move_down(self) -> None:
        row = self._files_list.currentRow()
        if 0 <= row < self._files_list.count() - 1:
            item = self._files_list.takeItem(row)
            self._files_list.insertItem(row + 1, item)
            self._files_list.setCurrentRow(row + 1)

    def _on_remove(self) -> None:
        for item in self._files_list.selectedItems():
            self._files_list.takeItem(self._files_list.row(item))

    def _collect_files_for_flash(self) -> List[str]:
        """Возвращает список файлов для прошивки.

        - Если выбран файл и включена запись конфигурации — дописывает
          имя/серийный номер в последнюю страницу.
        - Если файлов нет, но включена запись конфигурации — готовит
          временный HEX только с последней страницей (не стирает основную
          прошивку, так как в нём нет данных).
        """
        if self._config_button.isChecked() and self._files_list.count() == 0:
            return [self._prepare_config_only_hex()]

        files: List[str] = []
        for i in range(self._files_list.count()):
            item = self._files_list.item(i)
            if item is not None:
                files.append(item.text())

        if not self._config_button.isChecked():
            return files

        prepared: List[str] = []
        for f in files:
            prepared.append(self._prepare_firmware_with_config(f))
        return prepared

    def _prepare_firmware_with_config(self, file_path: str) -> str:
        """Создаёт разреженный HEX с прошивкой и последней страницей конфига.

        Раньше сюда записывался весь размер Flash, заполненный 0xFF. Для
        небольшой прошивки это превращало 16 КБ в 256 КБ и заставляло DFU
        передавать/проверять пустые области. В HEX оставляем только реальные
        участки: прошивку и страницу конфигурации.
        """
        from intelhex import IntelHex

        name = self._device_name_edit.text().strip()
        if not name:
            raise ValueError(tr("Заполните поле «Устройство»"))
        serial = self._serial_edit.text().strip()
        if not serial:
            raise ValueError(tr("Введите серийный номер"))
        flash_size_kb = self._get_flash_size_kb()

        data, base = load_firmware_bytes(file_path)
        if base == 0:
            base = BOOTLOADER_BASE_ADDR
        flash_size = flash_size_kb * 1024
        firmware_offset = base - BOOTLOADER_BASE_ADDR
        if firmware_offset < 0 or firmware_offset + len(data) > flash_size:
            raise ValueError(tr("Прошивка не помещается в выбранный размер Flash"))

        page = build_device_config_page(name, serial)

        image = IntelHex()
        image.puts(base, data)
        image.puts(DEVICE_CONFIG_PAGE_ADDR, page)
        src = Path(file_path)
        tmp = Path(tempfile.gettempdir()) / f"{src.stem}_конфиг.hex"
        image.write_hex_file(tmp)
        return str(tmp)

    def _warn_base_address_mismatch(self, method: str, files: List[str]) -> bool:
        """Предупреждает, если базовый адрес прошивки не соответствует
        ожидаемому для выбранного способа программирования (см.
        CURSOR_FIX_PROMPT.md 3.4): UART/USB CDC работают через наш
        bootloader-протокол и ожидают образ приложения (без самого
        bootloader'а), а ST-Link/J-Link/USB DFU обычно используются для
        полного образа Flash, начиная с адреса бутлоадера.

        Returns:
            True, если можно продолжать прошивку (риска нет или пользователь
            подтвердил), False — если пользователь отменил операцию.
        """
        risky_files: List[Tuple[str, int]] = []
        for file_path in files:
            try:
                _, base = load_firmware_bytes(file_path)
            except Exception:  # noqa: BLE001
                continue
            if not base:
                continue
            if method in ("uart", "usb_cdc") and base == BOOTLOADER_BASE_ADDR:
                risky_files.append((file_path, base))
            elif method in ("stlink", "jlink", "usb") and base == APPLICATION_BASE_ADDR:
                risky_files.append((file_path, base))

        if not risky_files:
            return True

        names = "\n".join(f"  {Path(p).name} (0x{b:08X})" for p, b in risky_files)
        if method in ("uart", "usb_cdc"):
            text = tr(
                "Файл(ы) начинаются с адреса бутлоадера (0x{0:08X}):\n{1}\n\n"
                "UART/USB CDC используют bootloader-протокол устройства и обычно "
                "предназначены для прошивки области приложения. Запись по этому "
                "адресу перезапишет сам bootloader на устройстве. Продолжить?"
            ).format(BOOTLOADER_BASE_ADDR, names)
        else:
            text = tr(
                "Файл(ы) начинаются с адреса приложения (0x{0:08X}), а не с адреса "
                "бутлоадера (0x{1:08X}):\n{2}\n\n"
                "Выбранный способ (ST-Link/J-Link/USB DFU) обычно используется для "
                "прошивки полного образа Flash, включая bootloader. Если вы не "
                "собираетесь перезаписывать весь образ — убедитесь, что это "
                "ожидаемо. Продолжить?"
            ).format(APPLICATION_BASE_ADDR, BOOTLOADER_BASE_ADDR, names)

        answer = QMessageBox.warning(
            self,
            tr("Внимание: адрес прошивки"),
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _try_direct_config_write(self, method: str) -> bool:
        """Пишет конфигурацию через C1 без входа в bootloader/DFU.

        Возвращает True, если операция завершена (успешно или с ошибкой), и
        False, если нужно продолжить обычным DFU/UART-путём.
        """
        if not self._config_button.isChecked() or self._files_list.count() != 0:
            return False
        if method not in ("auto", "usb_cdc") or self._serial_manager is None:
            return False
        if not self._serial_manager.is_open():
            return False
        name = self._device_name_edit.text().strip().encode("ascii", errors="ignore")[:DEVICE_CONFIG_NAME_MAX]
        serial = self._serial_edit.text().strip().encode("ascii", errors="ignore")[:10]
        payload = bytes((len(name),)) + name + bytes((len(serial),)) + serial
        payload += bytes((0x83, 0x04, 0x40, 0x57))
        try:
            self._serial_manager.request_control(CMD_CFG_WRITE, payload)
            self._mark_operation_disconnected()
            message = tr("Конфигурация записана без перепрошивки Flash")
            self._log(message)
            QMessageBox.information(self, tr("Готово"), message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Прямая запись конфигурации недоступна, использую bootloader: %s", exc)
            return False

    def _on_flash(self) -> None:
        method = self._method_combo.currentData()
        if self._try_direct_config_write(method):
            return
        if self._is_any_worker_running():
            QMessageBox.warning(
                self,
                tr("Внимание"),
                tr("Выполняется другая операция с устройством. Дождитесь её завершения."),
            )
            return

        try:
            prepared = self._collect_files_for_flash()
        except ValueError as exc:
            QMessageBox.warning(self, tr("Внимание"), str(exc))
            return

        if not prepared:
            QMessageBox.warning(self, tr("Внимание"), tr("Добавьте файлы прошивки или включите запись конфигурации"))
            return

        if not self._warn_base_address_mismatch(method, prepared):
            return

        logger.info("Старт прошивки: метод=%s, файлы=%s", method, prepared)
        self._release_serial_port(method)
        self._flash_button.setEnabled(False)
        self._config_button.setEnabled(False)
        self._read_button.setEnabled(False)
        self._read_config_button.setEnabled(False)
        self._erase_button.setEnabled(False)
        self._progress_bar.setValue(0)
        self._flash_worker = FlashWorker(prepared, method, self._config, verify=self._verify_checkbox.isChecked(), parent=self)
        self._flash_worker.log_line.connect(self._log)
        self._flash_worker.progress.connect(self._progress_bar.setValue)
        self._flash_worker.finished.connect(self._on_flash_finished)
        self._flash_worker.start()

    def _release_serial_port(self, method: str) -> None:
        """Освобождает COM-порт перед операцией, если его держит SerialManager.

        Для UART/USB CDC порт занят приложением, и программатор не сможет его
        открыть. Запоминаем, был ли порт открыт, чтобы вернуть его обратно.
        """
        self._port_was_open = False
        # "auto" тоже может в итоге разрешиться в uart/usb_cdc (см.
        # _auto_detect_method), поэтому порт освобождаем на всякий случай и
        # для него — иначе SerialManager может держать открытым тот же COM,
        # который попробует открыть Bootloader.open().
        if method not in ("uart", "usb_cdc", "auto") or self._serial_manager is None:
            return
        if not self._serial_manager.is_open():
            return
        self._port_was_open = True
        logger.info("Закрываю SerialManager перед операцией через %s", method)
        self._serial_manager.close_port()

    def _restore_serial_port(self) -> None:
        """Возвращает COM-порт приложению, если он был закрыт перед операцией."""
        if not getattr(self, "_port_was_open", False) or self._serial_manager is None:
            return
        self._port_was_open = False
        try:
            logger.info("Восстановление SerialManager после операции")
            self._serial_manager.open_port(
                self._config.get("port", ""),
                self._config.get("baudrate", 115200),
                emulation=self._config.get("emulation", False),
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("Не удалось восстановить COM-порт: %s", exc)

    def _mark_operation_disconnected(self) -> None:
        """Сбрасывает зелёный статус после операции с USB/DFU устройством."""
        self._last_chip_info = {}
        self._set_connect_status(False, {})

    def _on_flash_finished(self, success: bool, message: str) -> None:
        logger.info("Прошивка завершена: success=%s, message=%s", success, message)
        self._mark_operation_disconnected()
        self._flash_button.setEnabled(True)
        self._config_button.setEnabled(True)
        self._read_button.setEnabled(True)
        self._read_config_button.setEnabled(True)
        self._erase_button.setEnabled(True)
        self._log(message)
        self._restore_serial_port()
        if success:
            QMessageBox.information(self, tr("Готово"), message)
        else:
            QMessageBox.critical(self, tr("Ошибка"), message)

    def _on_read_firmware(self) -> None:
        method = self._method_combo.currentData()
        try:
            size_kb = self._get_flash_size_kb()
        except ValueError as exc:
            QMessageBox.warning(self, tr("Внимание"), str(exc))
            return
        if size_kb <= 0:
            QMessageBox.warning(self, tr("Внимание"), tr("Размер чтения должен быть больше 0"))
            return
        if self._is_any_worker_running():
            QMessageBox.warning(
                self,
                tr("Внимание"),
                tr("Выполняется другая операция с устройством. Дождитесь её завершения."),
            )
            return
        self._read_button.setEnabled(False)
        self._erase_button.setEnabled(False)
        self._progress_bar.setValue(0)
        self._release_serial_port(method)
        self._read_worker = ReadWorker(method, self._config, size=size_kb * 1024, parent=self)
        self._read_worker.log_line.connect(self._log)
        self._read_worker.finished.connect(self._on_read_finished)
        self._read_worker.start()

    def _on_read_finished(self, success: bool, message: str, data: object, base: int) -> None:
        self._mark_operation_disconnected()
        self._read_button.setEnabled(True)
        self._erase_button.setEnabled(True)
        self._restore_serial_port()
        if not success or not isinstance(data, bytes) or not data:
            self._log(message)
            QMessageBox.critical(self, tr("Ошибка"), message)
            return
        self._log(message)
        try:
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                tmp.write(data)
                bin_path = tmp.name
            self.open_hex_editor(bin_path)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), str(exc))

    def _try_direct_config_read(self, method: str) -> bool:
        if method not in ("auto", "usb_cdc") or self._serial_manager is None:
            return False
        if not self._serial_manager.is_open():
            return False
        try:
            payload = self._serial_manager.request_control(CMD_CFG_READ, b"")
            if len(payload) < 2:
                raise ValueError("Неполный ответ CMD_CFG_READ")
            name_len = payload[0]
            if name_len > DEVICE_CONFIG_NAME_MAX or len(payload) < 1 + name_len + 1:
                raise ValueError("Некорректная длина имени в CMD_CFG_READ")
            pos = 1
            name = payload[pos : pos + name_len].decode("ascii", errors="ignore")
            pos += name_len
            serial_len = payload[pos]
            pos += 1
            if serial_len > 10 or len(payload) < pos + serial_len + 4:
                raise ValueError("Некорректная длина serial в CMD_CFG_READ")
            serial = payload[pos : pos + serial_len].decode("ascii", errors="ignore")
            self._device_name_edit.setText(name)
            self._serial_edit.setText(serial)
            self._config.set_bulk({
                "device_type_name": name,
                "device_serial": serial,
                "serial_number": serial,
            })
            message = tr("Прочитана конфигурация: {0} / {1}").format(name, serial)
            self._log(message)
            QMessageBox.information(self, tr("Готово"), message)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Прямое чтение конфигурации недоступно, использую Flash: %s", exc)
            return False

    def _on_read_config(self) -> None:
        method = self._method_combo.currentData()
        if self._try_direct_config_read(method):
            return
        page_size = DEVICE_CONFIG_PAGE_SIZE
        if self._is_any_worker_running():
            QMessageBox.warning(
                self,
                tr("Внимание"),
                tr("Выполняется другая операция с устройством. Дождитесь её завершения."),
            )
            return
        start = DEVICE_CONFIG_PAGE_ADDR
        self._read_config_button.setEnabled(False)
        self._erase_button.setEnabled(False)
        self._progress_bar.setValue(0)
        self._release_serial_port(method)
        self._read_worker = ReadWorker(method, self._config, size=page_size, start=start, parent=self)
        self._read_worker.log_line.connect(self._log)
        self._read_worker.finished.connect(self._on_config_read_finished)
        self._read_worker.start()

    def _on_config_read_finished(self, success: bool, message: str, data: object, base: int) -> None:
        self._mark_operation_disconnected()
        self._read_config_button.setEnabled(True)
        self._erase_button.setEnabled(True)
        self._restore_serial_port()
        if not success or not isinstance(data, bytes) or not data:
            self._log(message)
            QMessageBox.critical(self, tr("Ошибка"), message)
            return
        self._log(message)
        try:
            parsed = parse_device_config(data)
            if parsed is None:
                message = tr(
                    "Страница конфигурации не инициализирована или создана старой версией firmware. "
                    "Сначала запишите конфигурацию заново."
                )
                self._log(message)
                QMessageBox.warning(self, tr("Внимание"), message)
                return
            name, serial, _vid, _pid = parsed
            self._device_name_edit.setText(name)
            self._serial_edit.setText(serial)
            self._config.set_bulk({
                "device_type_name": name,
                "device_serial": serial,
                "serial_number": serial,
            })
            self._log(tr("Конфигурация из Flash: устройство={0}, серийный={1}").format(name, serial))
            QMessageBox.information(self, tr("Готово"), tr("Прочитана конфигурация: {0} / {1}").format(name, serial))
        except Exception as exc:  # noqa: BLE001
            self._log(tr("Не удалось распарсить конфигурацию: {0}").format(exc))
            QMessageBox.critical(self, tr("Ошибка"), str(exc))

    def _on_erase_flash(self) -> None:
        method = self._method_combo.currentData()
        if self._is_any_worker_running():
            QMessageBox.warning(
                self,
                tr('Внимание'),
                tr('Выполняется другая операция с устройством. Дождитесь её завершения.'),
            )
            return
        application_only = method in ('uart', 'usb_cdc')
        if application_only:
            warning_text = tr(
                'Этот способ стирает область приложения, но оставляет bootloader.\n'
                'Приложение перестанет работать до повторной прошивки.\n\n'
                'Продолжить?'
            )
        else:
            warning_text = tr(
                'Эта операция может стереть bootloader и всё приложение с МК.\n'
                'Устройство перестанет работать до повторной прошивки.\n\n'
                'Продолжить?'
            )
        answer = QMessageBox.warning(
            self,
            tr('Внимание: стирание Flash'),
            warning_text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        logger.info('Старт стирания Flash: метод=%s', method)
        self._release_serial_port(method)
        self._flash_button.setEnabled(False)
        self._config_button.setEnabled(False)
        self._read_button.setEnabled(False)
        self._read_config_button.setEnabled(False)
        self._erase_button.setEnabled(False)
        self._progress_bar.setValue(0)
        self._erase_worker = EraseWorker(method, self._config, self)
        self._erase_worker.log_line.connect(self._log)
        self._erase_worker.finished.connect(self._on_erase_finished)
        self._erase_worker.start()

    def _on_erase_finished(self, success: bool, message: str) -> None:
        logger.info('Стирание завершено: success=%s, message=%s', success, message)
        self._mark_operation_disconnected()
        self._flash_button.setEnabled(True)
        self._config_button.setEnabled(True)
        self._read_button.setEnabled(True)
        self._read_config_button.setEnabled(True)
        self._erase_button.setEnabled(True)
        self._log(message)
        self._restore_serial_port()
        if success:
            QMessageBox.information(self, tr('Готово'), message)
        else:
            QMessageBox.critical(self, tr('Ошибка'), message)

    def open_hex_editor(self, file_path: Optional[str] = None) -> None:
        if not file_path:
            selected = self._files_list.currentItem()
            file_path = selected.text() if selected else None
        dialog = HexEditorDialog(file_path, self)
        dialog.exec()
        saved = dialog.current_path
        if saved and not self._files_list.findItems(saved, Qt.MatchFlag.MatchExactly):
            self._files_list.addItem(saved)
            self._update_log_file()

    def _on_select_port(self) -> None:
        dialog = ComSettingsDialog(self._serial_manager, self)
        dialog.exec()

    def _on_power_toggle(self) -> None:
        self._log(tr("Управление питанием не реализовано в Python-режиме"))

    def _update_power_button(self) -> None:
        if self._power_button.property("power_on"):
            self._power_button.setText(tr("Питание 3.3V ВЫКЛ"))
        else:
            self._power_button.setText(tr("Питание 3.3V ВКЛ"))

    def _on_reset(self) -> None:
        method = self._method_combo.currentData()
        try:
            if method == "stlink" and _PYOCD:
                with ConnectHelper.session_with_chosen_probe(
                    return_first=True,
                    auto_open=True,
                    target_override=self._config.get("target_mcu", "cortex_m"),
                ) as session:
                    session.target.reset()
            elif method == "jlink" and _PYLINK:
                jlink = pylink.JLink()
                jlink.open()
                jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
                jlink.connect(target=self._config.get("target_mcu", ""), interface="SWD")
                jlink.reset()
                jlink.close()
            else:
                self._log(tr("Сброс не поддерживается для текущего способа программирования"))
                return
            self._log(tr("Сброс выполнен"))
        except Exception as exc:  # noqa: BLE001
            self._log(tr("Ошибка сброса: {0}").format(exc))

    def _log(self, text: str) -> None:
        if not text:
            return
        self._log_edit.appendPlainText(text)
        if self._log_file:
            try:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(text + "\n")
            except OSError:
                pass

    def _update_log_file(self) -> None:
        if self._files_list.count():
            first = Path(self._files_list.item(0).text())
            self._log_file = first.parent / "log.txt"
        else:
            self._log_file = None

    def closeEvent(self, event) -> None:
        if self._connect_worker and self._connect_worker.isRunning():
            self._connect_worker.terminate()
            self._connect_worker.wait(1000)
        if self._flash_worker and self._flash_worker.isRunning():
            self._flash_worker.terminate()
            self._flash_worker.wait(1000)
        if self._read_worker and self._read_worker.isRunning():
            self._read_worker.terminate()
            self._read_worker.wait(1000)
        event.accept()
