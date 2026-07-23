"""Профессиональный диалог прошивки микроконтроллера и HEX-редактор."""

import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

try:
    from pyocd.core.helpers import ConnectHelper
    from pyocd.flash.flash_builder import FlashBuilder

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
from core.can_protocol import (
    DEVICE_TYPE_ANALOG,
    DEVICE_TYPE_BASIC,
    DEVICE_TYPE_CAN_FD,
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
    ("usb", "USB (DFU)"),
    ("auto", "Авто"),
]

DEVICE_TYPES: List[Tuple[int, str]] = [
    (DEVICE_TYPE_BASIC, "2 CAN"),
    (DEVICE_TYPE_ANALOG, "2 CAN +"),
    (DEVICE_TYPE_CAN_FD, "2 CAN FD"),
]

STM32_FLASH_SIZES: Dict[str, int] = {
    "STM32F103C8T6": 64,
    "STM32F103RBT6": 128,
    "STM32F105RCT6": 256,
    "STM32F105VCT6": 256,
    "STM32F107VCT6": 256,
    "STM32F205RGT6": 1024,
    "STM32F303CCT6": 256,
    "STM32F407VGT6": 1024,
    "STM32F429ZIT6": 2048,
    "STM32F446RET6": 512,
    "STM32F746ZGT6": 1024,
}

# ST-LINK Device ID (например, 0x418) → модель по datasheet
DEVICE_ID_TO_MODEL: Dict[str, str] = {
    "0x410": "STM32F103RBT6",
    "0x412": "STM32F103C8T6",
    "0x413": "STM32F407VGT6",
    "0x414": "STM32F105RCT6",
    "0x418": "STM32F105VCT6",
    "0x421": "STM32F446RET6",
    "0x422": "STM32F303CCT6",
    "0x434": "STM32F429ZIT6",
    "0x440": "STM32F107VCT6",
    "0x449": "STM32F746ZGT6",
    "0x411": "STM32F205RGT6",
}

CHIP_FLASH_SIZE_KB: Dict[int, str] = {
    0x412: "64/128",
    0x410: "128/256",
    0x414: "256/512",
    0x418: "64/128",
    0x420: "128/256",
    0x430: "1024",
    0x431: "256/512",
    0x432: "512/1024",
    0x433: "1024",
    0x440: "1024",
    0x441: "2048",
    0x442: "512/1024",
    0x444: "512/1024",
    0x445: "1024",
    0x448: "1024",
    0x449: "2048",
    0x450: "1024",
    0x451: "2048",
}


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


def _parse_intel_hex(text: str) -> Tuple[bytes, int]:
    """Парсит Intel HEX в бинарные данные и базовый адрес."""
    records: Dict[int, int] = {}
    base = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        try:
            count = int(line[1:3], 16)
            addr = int(line[3:7], 16)
            rtype = int(line[7:9], 16)
            payload = bytes.fromhex(line[9 : 9 + count * 2])
        except ValueError:
            continue
        if rtype == 0x00:
            for i, b in enumerate(payload):
                records[base + addr + i] = b
        elif rtype == 0x04 and len(payload) == 2:
            base = (payload[0] << 8 | payload[1]) << 16
        elif rtype == 0x01:
            break
    if not records:
        return b"", 0
    min_addr = min(records)
    max_addr = max(records)
    data = bytearray(b"\xFF" * (max_addr - min_addr + 1))
    for addr, b in records.items():
        data[addr - min_addr] = b
    return bytes(data), min_addr


def _save_intel_hex(data: bytes, base_address: int, path: Path) -> None:
    """Сохраняет данные в файл Intel HEX."""
    lines: List[str] = []
    pos = 0
    current_high: Optional[int] = None
    while pos < len(data):
        addr = base_address + pos
        high = (addr >> 16) & 0xFFFF
        low = addr & 0xFFFF
        if high != current_high:
            cs = (2 + 0 + 0 + 4 + (high >> 8) + (high & 0xFF)) & 0xFF
            cs = (-cs) & 0xFF
            lines.append(f":02000004{high:04X}{cs:02X}")
            current_high = high
        chunk = data[pos : pos + 16]
        count = len(chunk)
        cs = count + (low >> 8) + (low & 0xFF) + 0
        for b in chunk:
            cs += b
        cs = (-cs) & 0xFF
        hex_data = chunk.hex().upper()
        lines.append(f":{count:02X}{low:04X}00{hex_data}{cs:02X}")
        pos += count
    lines.append(":00000001FF")
    path.write_text("\n".join(lines), encoding="utf-8")


def _load_elf(path: Path) -> Tuple[bytes, int]:
    """Пытается прочитать ELF-файл через pyelftools; иначе как raw."""
    try:
        from elftools.elf.elffile import ELFFile

        data = bytearray()
        base = 0
        first = True
        with open(path, "rb") as fp:
            elf = ELFFile(fp)
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_LOAD":
                    seg_data = seg.data()
                    seg_addr = seg["p_paddr"] if seg["p_paddr"] else seg["p_vaddr"]
                    if first:
                        base = seg_addr
                        data.extend(seg_data)
                        first = False
                    else:
                        if seg_addr > base + len(data):
                            data.extend(b"\x00" * (seg_addr - base - len(data)))
                        data.extend(seg_data)
        return bytes(data), base
    except Exception:  # noqa: BLE001
        return path.read_bytes(), 0


def _load_firmware_bytes(file_path: str) -> Tuple[bytes, int]:
    """Загружает прошивку (.bin/.hex/.elf) и возвращает (данные, базовый адрес)."""
    path = Path(file_path)
    ext = path.suffix.lower()
    if ext == ".bin":
        return path.read_bytes(), 0
    if ext == ".hex":
        from intelhex import IntelHex

        ih = IntelHex(str(path))
        data = bytes(ih.tobinarray())
        return data, ih.minaddr()
    if ext == ".elf":
        return _load_elf(path)
    return path.read_bytes(), 0


def _prepare_bin_file(file_path: str, default_base: int = 0x08000000) -> Tuple[Optional[str], int]:
    """Подготавливает временный .bin для утилит, которым нужен бинарный файл."""
    data, base = _load_firmware_bytes(file_path)
    if not data:
        return None, 0
    if base == 0:
        base = default_base
    suffix = Path(file_path).suffix
    if suffix.lower() == ".bin":
        return file_path, base
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp.write(data)
        return tmp.name, base


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

        for edit in (self._offset_edit, self._hex_edit, self._ascii_edit):
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
        splitter.addWidget(self._offset_edit)
        splitter.addWidget(self._hex_edit)
        splitter.addWidget(self._ascii_edit)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 1)
        splitter.setSizes([120, 450, 200])
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
        for edit in (self._offset_edit, self._hex_edit, self._ascii_edit):
            edit.setStyleSheet(theme)

    def _sync_scroll(self, value: int) -> None:
        for edit in (self._offset_edit, self._hex_edit, self._ascii_edit):
            if edit.verticalScrollBar().value() != value:
                edit.verticalScrollBar().setValue(value)

    def _format_offset_text(self) -> str:
        lines = []
        for i in range(0, max(len(self._data), 1), self.BYTES_PER_LINE):
            lines.append(f"{self._base_address + i:08X}")
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
            data, base = _load_firmware_bytes(file_path)
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
                for m in ("stlink", "jlink", "uart", "usb"):
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
        if method == "usb":
            return self._try_usb()
        return False, {"error": tr("Неизвестный метод")}

    def _try_stlink(self) -> Tuple[bool, Dict[str, Any]]:
        stlink_cli = shutil.which("ST-LINK_CLI.exe")
        if stlink_cli:
            try:
                result = subprocess.run(
                    [stlink_cli, "-c"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                output = result.stdout + result.stderr
                match = re.search(
                    r"Device\s+ID\s*[:=]\s*(0x[0-9A-Fa-f]{3,4})",
                    output,
                    re.IGNORECASE,
                )
                if match:
                    chip_id = match.group(1).upper()
                    model = DEVICE_ID_TO_MODEL.get(chip_id)
                    if model:
                        size_kb = STM32_FLASH_SIZES.get(model)
                        return True, {
                            "chip_id": chip_id,
                            "model": model,
                            "flash_size": f"{size_kb} KB" if size_kb else tr("Неизвестно"),
                            "flash_size_kb": size_kb,
                        }
                    return True, {"chip_id": chip_id, "flash_size": tr("Неизвестно")}
            except Exception as exc:  # noqa: BLE001
                self.log_line.emit(tr("ST-LINK_CLI.exe не удалось запустить: {0}").format(exc))
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
            import serial as serial_module
        except Exception as exc:  # noqa: BLE001
            return False, {"error": tr("pyserial не установлен: {0}").format(exc)}
        try:
            ser = serial_module.Serial(
                port,
                baud,
                bytesize=serial_module.EIGHTBITS,
                parity=serial_module.PARITY_EVEN,
                stopbits=serial_module.STOPBITS_ONE,
                timeout=1,
            )
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}
        try:
            bl = Bootloader(ser)
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
                ser.close()
            except Exception:  # noqa: S110
                pass

    def _try_usb(self) -> Tuple[bool, Dict[str, Any]]:
        if not _PYUSB:
            return False, {"error": tr("pyusb/libusb не установлен")}
        try:
            dev = usb.core.find(idVendor=0x0483, idProduct=0xDF11)
            if dev is None:
                return False, {"error": tr("USB DFU устройство не найдено")}
            return True, {"chip_id": tr("Неизвестно"), "flash_size": tr("Неизвестно")}
        except Exception as exc:  # noqa: BLE001
            return False, {"error": str(exc)}


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
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._files = files
        self._method = method
        self._config = config
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

    def _flash_one(self, file_path: str) -> Tuple[bool, str]:
        if self._method == "stlink":
            return self._flash_stlink(file_path)
        if self._method == "jlink":
            return self._flash_jlink(file_path)
        if self._method == "uart":
            return self._flash_uart(file_path)
        if self._method == "usb":
            return self._flash_usb(file_path)
        return False, tr("Неизвестный способ программирования")

    def _flash_stlink(self, file_path: str) -> Tuple[bool, str]:
        if not _PYOCD:
            return False, tr("pyocd не установлен")
        target = self._config.get("target_mcu", "")
        if not target:
            return False, tr("Не указана целевая МК (target_mcu)")
        data, base = _load_firmware_bytes(file_path)
        if not data:
            return False, tr("Файл прошивки пуст")
        if not base:
            base = 0x08000000
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
        data, base = _load_firmware_bytes(file_path)
        if not data:
            return False, tr("Файл прошивки пуст")
        if not base:
            base = 0x08000000
        try:
            jlink = pylink.JLink()
            jlink.open()
            jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
            jlink.connect(target=target, interface="SWD")
            jlink.erase()
            jlink.flash(base, data)
            read = bytes(jlink.memory_read(base, len(data)))
            ok = read == data
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
            import serial as serial_module
        except Exception as exc:  # noqa: BLE001
            return False, tr("pyserial не установлен: {0}").format(exc)
        try:
            ser = serial_module.Serial(
                port,
                baud,
                bytesize=serial_module.EIGHTBITS,
                parity=serial_module.PARITY_EVEN,
                stopbits=serial_module.STOPBITS_ONE,
                timeout=1,
            )
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        try:
            data, base = _load_firmware_bytes(file_path)
            if not base:
                base = 0x08000000
            if not data:
                return False, tr("Файл прошивки пуст")
            with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                tmp.write(data)
                bin_path = tmp.name
            try:
                bl = Bootloader(ser, progress_callback=lambda p: self.progress.emit(self._scaled_progress(p)))
                bl.flash_firmware(bin_path, base)
                ok = bl.verify(base, data)
                return ok, tr("UART прошивка завершена: {0}").format(file_path)
            finally:
                try:
                    Path(bin_path).unlink(missing_ok=True)
                except OSError:
                    pass
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)
        finally:
            try:
                ser.close()
            except Exception:  # noqa: S110
                pass

    def _flash_usb(self, file_path: str) -> Tuple[bool, str]:
        bin_path, base = _prepare_bin_file(file_path)
        if not bin_path:
            return False, tr("Не удалось подготовить BIN-файл из прошивки")
        if not base:
            base = 0x08000000
        dfu_util = shutil.which("dfu-util") or shutil.which("dfu-util.exe")
        if dfu_util:
            cmd = [
                dfu_util,
                "-d", "0483:df11",
                "-a", "0",
                "-s", f"0x{base:08X}:leave",
                "-D", bin_path,
            ]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or "").strip()
                    return False, tr("dfu-util ошибка: {0}").format(err)
                return True, tr("USB DFU: прошивка dfu-util завершена")
            except subprocess.TimeoutExpired:
                return False, tr("dfu-util: превышено время ожидания")
            except FileNotFoundError:
                return False, tr("dfu-util не найден в PATH")
            except Exception as exc:  # noqa: BLE001
                return False, tr("dfu-util ошибка: {0}").format(exc)
            finally:
                if bin_path != file_path:
                    try:
                        Path(bin_path).unlink(missing_ok=True)
                    except OSError:
                        pass
        if not _PYUSB:
            if bin_path != file_path:
                try:
                    Path(bin_path).unlink(missing_ok=True)
                except OSError:
                    pass
            return False, tr("dfu-util не найден в PATH и pyusb/libusb не установлены")
        try:
            dev = usb.core.find(idVendor=0x0483, idProduct=0xDF11)
            if dev is None:
                if bin_path != file_path:
                    try:
                        Path(bin_path).unlink(missing_ok=True)
                    except OSError:
                        pass
                return False, tr("USB DFU устройство не найдено")
            from core.dfu import DfuDevice
            data = Path(bin_path).read_bytes()
            with DfuDevice(dev) as dfu:
                dfu.mass_erase()
                dfu.download(base, data)
                ok = dfu.upload(base, len(data)) == data
                dfu.leave()
            return ok, tr("USB DFU: прошивка завершена") if ok else tr("USB DFU: верификация не прошла")
        except Exception as exc:  # noqa: BLE001
            return False, tr("USB DFU ошибка: {0}").format(exc)
        finally:
            if bin_path != file_path:
                try:
                    Path(bin_path).unlink(missing_ok=True)
                except OSError:
                    pass


class ReadWorker(QThread):
    """Фоновое чтение флеш-памяти микроконтроллера."""

    log_line = Signal(str)
    finished = Signal(bool, str, object, int)

    def __init__(
        self,
        method: str,
        config: Config,
        size: int = 0x10000,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._method = method
        self._config = config
        self._size = size

    def run(self) -> None:
        try:
            data, base = self._read_one()
            self.finished.emit(True, tr("Чтение завершено: {0} байт").format(len(data)), data, base)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Ошибка чтения прошивки")
            self.finished.emit(False, str(exc), b"", 0)

    def _read_one(self) -> Tuple[bytes, int]:
        if self._method == "stlink":
            return self._read_stlink()
        if self._method == "jlink":
            return self._read_jlink()
        if self._method == "uart":
            return self._read_uart()
        if self._method == "usb":
            return self._read_usb()
        raise RuntimeError(tr("Чтение не поддерживается для {0}").format(self._method))

    def _read_stlink(self) -> Tuple[bytes, int]:
        stlink_cli = shutil.which("ST-LINK_CLI.exe")
        if stlink_cli:
            try:
                with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                    bin_path = tmp.name
                result = subprocess.run(
                    [stlink_cli, "-r", bin_path],
                    capture_output=True,
                    text=True,
                    timeout=120,
                )
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or "").strip()
                    raise RuntimeError(tr("ST-LINK_CLI ошибка: {0}").format(err))
                data = Path(bin_path).read_bytes()
                Path(bin_path).unlink(missing_ok=True)
                return data, 0x08000000
            except subprocess.TimeoutExpired:
                raise RuntimeError(tr("ST-LINK_CLI: превышено время ожидания"))
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                if not isinstance(exc, RuntimeError):
                    raise RuntimeError(tr("ST-LINK_CLI ошибка: {0}").format(exc))
                raise
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
            start = flash.start if flash else 0x08000000
            size = flash.length if flash else self._size
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
        start = 0x08000000
        data = bytes(jlink.memory_read(start, self._size))
        jlink.close()
        return data, start

    def _read_uart(self) -> Tuple[bytes, int]:
        port = self._config.get("port", "")
        baud = self._config.get("baudrate", 115200)
        if not port:
            raise RuntimeError(tr("COM-порт не указан"))
        import serial as serial_module
        ser = serial_module.Serial(
            port,
            baud,
            bytesize=serial_module.EIGHTBITS,
            parity=serial_module.PARITY_EVEN,
            stopbits=serial_module.STOPBITS_ONE,
            timeout=1,
        )
        try:
            bl = Bootloader(ser)
            bl.reconfigure_for_bootloader()
            bl.enter_bootloader()
            bl.sync()
            start = 0x08000000
            data = bl.read_memory(start, self._size)
            return data, start
        finally:
            try:
                ser.close()
            except Exception:  # noqa: S110
                pass

    def _read_usb(self) -> Tuple[bytes, int]:
        dfu_util = shutil.which("dfu-util") or shutil.which("dfu-util.exe")
        if dfu_util:
            try:
                with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
                    bin_path = tmp.name
                size = self._size
                cmd = [
                    dfu_util,
                    "-d", "0483:df11",
                    "-a", "0",
                    "-s", f"0x08000000:0x{size:X}",
                    "-U", bin_path,
                ]
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
                if result.returncode != 0:
                    err = (result.stderr or result.stdout or "").strip()
                    raise RuntimeError(tr("dfu-util ошибка: {0}").format(err))
                data = Path(bin_path).read_bytes()
                Path(bin_path).unlink(missing_ok=True)
                return data, 0x08000000
            except subprocess.TimeoutExpired:
                raise RuntimeError(tr("dfu-util: превышено время ожидания"))
            except FileNotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001
                if not isinstance(exc, RuntimeError):
                    raise RuntimeError(tr("dfu-util ошибка: {0}").format(exc))
                raise
        if not _PYUSB:
            raise RuntimeError(tr("dfu-util не найден в PATH и pyusb/libusb не установлены"))
        dev = usb.core.find(idVendor=0x0483, idProduct=0xDF11)
        if dev is None:
            raise RuntimeError(tr("USB DFU устройство не найдено"))
        from core.dfu import DfuDevice
        with DfuDevice(dev) as dfu:
            start = 0x08000000
            data = dfu.upload(start, self._size)
        return data, start


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
        self._connected = False
        self._last_chip_info: Dict[str, Any] = {}
        self._log_file: Optional[Path] = None
        self._create_widgets()
        self._build_layout()
        self._connect_signals()
        self._load_defaults()

    def _create_widgets(self) -> None:
        font = QFont("Segoe UI", 10)

        # Устройство и программатор
        self._device_type_label = QLabel(tr("Тип устройства"))
        self._device_type_label.setFont(font)
        self._device_type_combo = QComboBox()
        self._device_type_combo.setFont(font)
        for value, label in DEVICE_TYPES:
            self._device_type_combo.addItem(tr(label), value)

        self._device_name_label = QLabel(tr("Устройство"))
        self._device_name_label.setFont(font)
        self._device_name_edit = QLineEdit()
        self._device_name_edit.setFont(font)
        self._device_name_edit.setMaxLength(10)

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
        self._read_size_edit = QLineEdit("64")
        self._read_size_edit.setMaximumWidth(80)

        self._config_button = QPushButton(tr("Записать конфигурацию устройства"))
        self._config_button.setFont(font)
        self._config_button.setCheckable(True)
        self._config_button.setEnabled(True)
        self._config_button.setToolTip(tr("Записать имя и серийный номер в последнюю страницу Flash"))

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
        self._hex_editor_button = QPushButton(tr("Открыть HEX-редактор"))
        self._close_button = QPushButton(tr("Закрыть"))

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        top_grid = QHBoxLayout()
        top_grid.addWidget(self._device_type_label)
        top_grid.addWidget(self._device_type_combo)
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
        self._hex_editor_button.clicked.connect(self._on_hex_editor)
        self._close_button.clicked.connect(self.reject)
        self._power_button.clicked.connect(self._on_power_toggle)
        self._reset_button.clicked.connect(self._on_reset)
        self._identify_button.clicked.connect(self._on_identify)

        self._target_mcu_edit.editingFinished.connect(
            lambda: self._config.set("target_mcu", self._target_mcu_edit.text().strip().upper())
        )

        self._device_type_combo.currentIndexChanged.connect(
            lambda idx: self._config.set(
                "device_type", self._device_type_combo.itemData(idx)
            )
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
        self._config_button.toggled.connect(self._on_config_button_toggled)

    def _load_defaults(self) -> None:
        device_type = self._config.get("device_type", DEVICE_TYPE_BASIC)
        index = self._device_type_combo.findData(device_type)
        if index >= 0:
            self._device_type_combo.setCurrentIndex(index)
        serial = self._config.get("device_serial", "")
        if not serial:
            serial = self._config.get("serial_number", "")
        self._serial_edit.setText(serial)
        method = self._config.get("programmer_method", "stlink")
        index = self._method_combo.findData(method)
        if index >= 0:
            self._method_combo.setCurrentIndex(index)
        target_mcu = self._config.get("target_mcu", "")
        self._target_mcu_edit.setText(target_mcu)
        if target_mcu in STM32_FLASH_SIZES:
            idx = self._chip_combo.findData(target_mcu)
            if idx >= 0:
                self._chip_combo.setCurrentIndex(idx)
        total_kb = self._config.get("total_memory", 65536) // 1024
        if total_kb > 0:
            self._read_size_edit.setText(str(total_kb))
        self._device_name_edit.setText(self._config.get("device_type_name", ""))
        if not self._device_name_edit.text().strip():
            idx = self._device_type_combo.currentIndex()
            self._device_name_edit.setText(self._device_type_combo.currentText().replace(" ", ""))
        self._update_power_button()
        self._on_method_changed(self._method_combo.currentIndex())

    def _on_method_changed(self, index: int) -> None:
        method = self._method_combo.itemData(index)
        if method:
            self._config.set("programmer_method", method)
        is_stlink = method == "stlink"
        is_uart = method == "uart"
        self._power_button.setVisible(False)
        self._reset_button.setVisible(is_stlink)
        self._identify_button.setVisible(is_stlink)
        self._port_button.setVisible(is_uart)
        self._set_connect_status(False, {})

    def _on_chip_changed(self, index: int) -> None:
        model = self._chip_combo.itemData(index)
        if model and model in STM32_FLASH_SIZES:
            self._read_size_edit.setText(str(STM32_FLASH_SIZES[model]))
            self._target_mcu_edit.setText(model)
            self._config.set("target_mcu", model)

    def _get_flash_size_kb(self) -> int:
        text = self._read_size_edit.text().strip()
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

    def _on_config_button_toggled(self, checked: bool) -> None:
        if checked:
            self._config_button.setText(tr("✓ Записать конфигурацию"))
            self._config_button.setStyleSheet("background-color: #4CAF50; color: #FFFFFF;")
        else:
            self._config_button.setText(tr("Записать конфигурацию устройства"))
            self._config_button.setStyleSheet("")

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
        method = self._method_combo.currentData()
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
                self._read_size_edit.setText(str(flash_size_kb))
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

    def _prepare_firmware_with_config(self, file_path: str) -> str:
        """Дополняет бинарник до размера Flash и записывает конфиг в последнюю страницу."""
        if not self._config_button.isChecked():
            return file_path
        name = self._device_name_edit.text().strip()
        if not name:
            raise ValueError(tr("Заполните поле «Устройство»"))
        serial = self._serial_edit.text().strip()
        if not serial:
            raise ValueError(tr("Введите серийный номер"))
        try:
            flash_size_kb = self._get_flash_size_kb()
        except ValueError as exc:
            raise

        data, base = _load_firmware_bytes(file_path)
        if base == 0:
            base = 0x08000000
        flash_size = flash_size_kb * 1024
        firmware_offset = base - 0x08000000
        if firmware_offset < 0 or firmware_offset + len(data) > flash_size:
            raise ValueError(tr("Прошивка не помещается в выбранный размер Flash"))

        image = bytearray(b"\xFF") * flash_size
        image[firmware_offset:firmware_offset + len(data)] = data

        page_size = 2048
        last_page_offset = flash_size - page_size
        name_bytes = name.encode("ascii", errors="ignore")[:10].ljust(10)
        serial_bytes = serial.encode("ascii", errors="ignore")[:10].ljust(10)
        image[last_page_offset + 8 : last_page_offset + 18] = name_bytes
        image[last_page_offset + 18 : last_page_offset + 28] = serial_bytes

        src = Path(file_path)
        tmp = Path(tempfile.gettempdir()) / f"{src.stem}_конфиг.bin"
        tmp.write_bytes(bytes(image))
        return str(tmp)

    def _on_flash(self) -> None:
        files = [self._files_list.item(i).text() for i in range(self._files_list.count())]
        if not files:
            QMessageBox.warning(self, tr("Внимание"), tr("Добавьте файлы прошивки"))
            return
        method = self._method_combo.currentData()
        if method == "auto":
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите конкретный способ программирования"))
            return
        try:
            prepared = [self._prepare_firmware_with_config(f) for f in files]
        except ValueError as exc:
            QMessageBox.warning(self, tr("Внимание"), str(exc))
            return
        self._flash_button.setEnabled(False)
        self._progress_bar.setValue(0)
        self._flash_worker = FlashWorker(prepared, method, self._config, self)
        self._flash_worker.log_line.connect(self._log)
        self._flash_worker.progress.connect(self._progress_bar.setValue)
        self._flash_worker.finished.connect(self._on_flash_finished)
        self._flash_worker.start()

    def _on_flash_finished(self, success: bool, message: str) -> None:
        self._flash_button.setEnabled(True)
        self._log(message)
        if success:
            QMessageBox.information(self, tr("Готово"), message)
        else:
            QMessageBox.critical(self, tr("Ошибка"), message)

    def _on_read_firmware(self) -> None:
        method = self._method_combo.currentData()
        if method == "auto":
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите конкретный способ программирования"))
            return
        try:
            size_kb = self._get_flash_size_kb()
        except ValueError as exc:
            QMessageBox.warning(self, tr("Внимание"), str(exc))
            return
        if size_kb <= 0:
            QMessageBox.warning(self, tr("Внимание"), tr("Размер чтения должен быть больше 0"))
            return
        self._read_button.setEnabled(False)
        self._progress_bar.setValue(0)
        self._read_worker = ReadWorker(method, self._config, size=size_kb * 1024, parent=self)
        self._read_worker.log_line.connect(self._log)
        self._read_worker.finished.connect(self._on_read_finished)
        self._read_worker.start()

    def _on_read_finished(self, success: bool, message: str, data: object, base: int) -> None:
        self._read_button.setEnabled(True)
        if not success or not isinstance(data, bytes) or not data:
            self._log(message)
            QMessageBox.critical(self, tr("Ошибка"), message)
            return
        self._log(message)
        try:
            hex_path = Path(tempfile.gettempdir()) / "read_firmware.hex"
            _save_intel_hex(data, base, hex_path)
            dialog = HexEditorDialog(str(hex_path), self)
            dialog.exec()
        except Exception as exc:  # noqa: BLE001
            QMessageBox.critical(self, tr("Ошибка"), str(exc))

    def _on_hex_editor(self) -> None:
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
