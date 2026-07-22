"""Профессиональный диалог прошивки микроконтроллера и HEX-редактор."""

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
        return _parse_intel_hex(path.read_text(encoding="utf-8", errors="ignore"))
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
    """Подсветка изменённых байт в HEX и ASCII представлениях."""

    def __init__(self, document: Any, changed_offsets: Set[int], bytes_per_line: int, ascii_mode: bool = False):
        super().__init__(document)
        self._changed_offsets = changed_offsets
        self._bytes_per_line = bytes_per_line
        self._ascii_mode = ascii_mode
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#F44336"))
        fmt.setFontWeight(QFont.Weight.Bold)
        self._fmt = fmt

    def highlightBlock(self, text: str) -> None:
        block = self.currentBlock()
        block_no = block.blockNumber()
        start_offset = block_no * self._bytes_per_line
        if self._ascii_mode:
            for i in range(min(len(text), self._bytes_per_line)):
                if start_offset + i in self._changed_offsets:
                    self.setFormat(i, 1, self._fmt)
        else:
            for i in range(self._bytes_per_line):
                offset = start_offset + i
                if offset in self._changed_offsets:
                    pos = i * 3
                    if pos + 2 <= len(text):
                        self.setFormat(pos, 2, self._fmt)


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
        self._saved_path: Optional[str] = None
        self._ignore_text_changes = False
        self._create_widgets()
        self._build_layout()
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
        HexHighlighter(self._hex_edit.document(), self._changed_offsets, self.BYTES_PER_LINE, ascii_mode=False)

        self._ascii_edit = QPlainTextEdit(self)
        self._ascii_edit.setFont(font)
        self._ascii_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        HexHighlighter(self._ascii_edit.document(), self._changed_offsets, self.BYTES_PER_LINE, ascii_mode=True)

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
            data, base = _load_firmware_bytes(file_path)
            self._data = bytearray(data)
            self._base_address = base
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
        if not _PYOCD:
            return False, {"error": tr("pyocd не установлен")}
        try:
            probes = ConnectHelper.list_connected_probes()
            if not probes:
                return False, {"error": tr("ST-Link не найден")}
            return True, {
                "chip_id": tr("Неизвестно"),
                "chip_id_int": None,
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
        if not _PYUSB:
            return False, tr("pyusb/libusb не установлен")
        data, base = _load_firmware_bytes(file_path)
        if not data:
            return False, tr("Файл прошивки пуст")
        if not base:
            base = 0x08000000
        try:
            from core.dfu import DfuDevice
            dev = usb.core.find(idVendor=0x0483, idProduct=0xDF11)
            if dev is None:
                return False, tr("USB DFU устройство не найдено")
            with DfuDevice(dev) as dfu:
                dfu.mass_erase()
                dfu.download(base, data)
                ok = dfu.upload(base, len(data)) == data
                dfu.leave()
            return ok, tr("USB DFU: прошивка завершена") if ok else tr("USB DFU: верификация не прошла")
        except Exception as exc:  # noqa: BLE001
            return False, str(exc)


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
        self._device_type_label = QLabel(tr("Устройство"))
        self._device_type_label.setFont(font)
        self._device_type_combo = QComboBox()
        self._device_type_combo.setFont(font)
        for value, label in DEVICE_TYPES:
            self._device_type_combo.addItem(tr(label), value)

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

        self._connect_button = QPushButton(tr("Connect"))
        self._connect_button.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))

        self._chip_info_label = QLabel(tr("Информация о чипе: не подключено"))
        self._chip_info_label.setFont(font)

        # Целевая МК (для pyocd/pylink)
        self._target_mcu_label = QLabel(tr("Целевая МК"))
        self._target_mcu_edit = QLineEdit()
        self._target_mcu_edit.setPlaceholderText(tr("Например: STM32F103RC"))

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
        self._hex_editor_button = QPushButton(tr("Открыть HEX-редактор"))
        self._close_button = QPushButton(tr("Закрыть"))

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(12, 12, 12, 12)

        top_grid = QHBoxLayout()
        top_grid.addWidget(self._device_type_label)
        top_grid.addWidget(self._device_type_combo)
        top_grid.addWidget(self._serial_label)
        top_grid.addWidget(self._serial_edit, 1)
        top_grid.addWidget(self._method_label)
        top_grid.addWidget(self._method_combo)
        top_grid.addWidget(self._connect_button)
        layout.addLayout(top_grid)

        layout.addWidget(self._chip_info_label)

        target_mcu_layout = QHBoxLayout()
        target_mcu_layout.addWidget(self._target_mcu_label)
        target_mcu_layout.addWidget(self._target_mcu_edit, 1)
        layout.addLayout(target_mcu_layout)

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
        bottom.addWidget(self._hex_editor_button)
        bottom.addWidget(self._close_button)
        layout.addLayout(bottom)

    def _connect_signals(self) -> None:
        self._connect_button.clicked.connect(self._on_connect)
        self._method_combo.currentIndexChanged.connect(self._on_method_changed)
        self._browse_button.clicked.connect(self._on_browse)
        self._up_button.clicked.connect(self._on_move_up)
        self._down_button.clicked.connect(self._on_move_down)
        self._remove_button.clicked.connect(self._on_remove)
        self._flash_button.clicked.connect(self._on_flash)
        self._hex_editor_button.clicked.connect(self._on_hex_editor)
        self._close_button.clicked.connect(self.reject)
        self._power_button.clicked.connect(self._on_power_toggle)
        self._reset_button.clicked.connect(self._on_reset)
        self._identify_button.clicked.connect(self._on_connect)

        self._target_mcu_edit.editingFinished.connect(
            lambda: self._config.set("target_mcu", self._target_mcu_edit.text().strip().upper())
        )

        self._device_type_combo.currentIndexChanged.connect(
            lambda idx: self._config.set(
                "device_type", self._device_type_combo.itemData(idx)
            )
        )
        self._serial_edit.editingFinished.connect(
            lambda: self._config.set("serial_number", self._serial_edit.text().strip())
        )

    def _load_defaults(self) -> None:
        device_type = self._config.get("device_type", DEVICE_TYPE_BASIC)
        index = self._device_type_combo.findData(device_type)
        if index >= 0:
            self._device_type_combo.setCurrentIndex(index)
        self._serial_edit.setText(self._config.get("serial_number", ""))
        method = self._config.get("programmer_method", "stlink")
        index = self._method_combo.findData(method)
        if index >= 0:
            self._method_combo.setCurrentIndex(index)
        self._target_mcu_edit.setText(self._config.get("target_mcu", ""))
        self._update_power_button()
        self._on_method_changed(self._method_combo.currentIndex())

    def _on_method_changed(self, index: int) -> None:
        method = self._method_combo.itemData(index)
        if method:
            self._config.set("programmer_method", method)
        is_stlink = method == "stlink"
        self._power_button.setVisible(False)
        self._reset_button.setVisible(is_stlink)
        self._identify_button.setVisible(is_stlink)
        self._set_connect_status(False, {})

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
                self._connect_button.setText(tr("Connect"))
                self._connect_button.setStyleSheet("")
            self._chip_info_label.setText(tr("Информация о чипе: {0}").format(error or tr("не подключено")))

    def _on_connect(self) -> None:
        if self._connect_worker and self._connect_worker.isRunning():
            return
        method = self._method_combo.currentData()
        self._log(tr("Подключение через {0}...").format(method))
        self._connect_button.setEnabled(False)
        self._connect_worker = ConnectWorker(method, self._config, self)
        self._connect_worker.log_line.connect(self._log)
        self._connect_worker.finished.connect(self._on_connect_finished)
        self._connect_worker.start()

    def _on_connect_finished(self, success: bool, info: Dict[str, Any]) -> None:
        self._connect_button.setEnabled(True)
        if success and info.get("method") and info["method"] != self._method_combo.currentData():
            index = self._method_combo.findData(info["method"])
            if index >= 0:
                self._method_combo.setCurrentIndex(index)
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

    def _on_flash(self) -> None:
        files = [self._files_list.item(i).text() for i in range(self._files_list.count())]
        if not files:
            QMessageBox.warning(self, tr("Внимание"), tr("Добавьте файлы прошивки"))
            return
        method = self._method_combo.currentData()
        if method == "auto":
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите конкретный способ программирования"))
            return
        self._flash_button.setEnabled(False)
        self._progress_bar.setValue(0)
        self._flash_worker = FlashWorker(files, method, self._config, self)
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

    def _on_hex_editor(self) -> None:
        selected = self._files_list.currentItem()
        file_path = selected.text() if selected else None
        dialog = HexEditorDialog(file_path, self)
        dialog.exec()
        saved = dialog.current_path
        if saved and not self._files_list.findItems(saved, Qt.MatchFlag.MatchExactly):
            self._files_list.addItem(saved)
            self._update_log_file()

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
        event.accept()
