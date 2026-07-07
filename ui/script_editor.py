"""Скриптовый редактор для пользовательской обработки CAN-кадров."""

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt, QRegularExpression, QRect, QSize
from PySide6.QtGui import (
    QColor,
    QFont,
    QPainter,
    QSyntaxHighlighter,
    QTextCharFormat,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from core.script_engine import ScriptEngine
from core.serial_manager import SerialManager
from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from models.utils import int_to_hex

logger = get_logger(__name__)

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "library" / "scripts"

DEFAULT_SCRIPT = (
    "# Пример обработки CAN-кадра\n"
    "if frame['id'] == 0x123:\n"
    "    log('Получен кадр 0x123')\n"
    "    send_can(1, 0x123, [0xAA, 0xBB, 0xCC])\n"
)

PYTHON_KEYWORDS = [
    "and", "as", "assert", "break", "class", "continue", "def", "del", "elif",
    "else", "except", "False", "finally", "for", "from", "global", "if",
    "import", "in", "is", "lambda", "None", "nonlocal", "not", "or", "pass",
    "raise", "return", "True", "try", "while", "with", "yield",
]


class LineNumberArea(QWidget):
    """Область с номерами строк для CodeEditor."""

    def __init__(self, editor: "CodeEditor") -> None:
        """Создаёт область номеров строк."""
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        """Возвращает рекомендуемый размер."""
        return QSize(self._editor.line_number_area_width(), 0)

    def paintEvent(self, event) -> None:  # noqa: N802
        """Перерисовывает номера строк."""
        self._editor.line_number_area_paint_event(event)


class CodeEditor(QPlainTextEdit):
    """Редактор кода с нумерацией строк."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        """Создаёт редактор кода."""
        super().__init__(parent)
        self.setFont(QFont("Consolas", 11))
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self._line_number_area = LineNumberArea(self)
        self.blockCountChanged.connect(self._update_line_number_area_width)
        self.updateRequest.connect(self._update_line_number_area)
        self._update_line_number_area_width(0)

    def line_number_area_width(self) -> int:
        """Возвращает ширину области номеров строк."""
        digits = max(1, len(str(self.blockCount())))
        return 10 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_line_number_area_width(self, _new_block_count: int) -> None:
        """Обновляет ширину области номеров строк."""
        self.setViewportMargins(self.line_number_area_width(), 0, 0, 0)

    def _update_line_number_area(self, rect: QRect, dy: int) -> None:
        """Прокручивает область номеров строк."""
        if dy:
            self._line_number_area.scroll(0, dy)
        else:
            self._line_number_area.update(0, rect.y(), self._line_number_area.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_line_number_area_width(0)

    def resizeEvent(self, event) -> None:  # noqa: N802
        """Обрабатывает изменение размера редактора."""
        super().resizeEvent(event)
        content_rect = self.contentsRect()
        self._line_number_area.setGeometry(
            QRect(content_rect.left(), content_rect.top(), self.line_number_area_width(), content_rect.height())
        )

    def line_number_area_paint_event(self, event) -> None:
        """Рисует номера строк."""
        painter = QPainter(self._line_number_area)
        painter.fillRect(event.rect(), QColor("#252526"))
        painter.setPen(QColor("#858585"))

        block = self.firstVisibleBlock()
        block_number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())

        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(
                    0,
                    top,
                    self._line_number_area.width() - 4,
                    self.fontMetrics().height(),
                    Qt.AlignmentFlag.AlignRight,
                    str(block_number + 1),
                )
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            block_number += 1


class PythonHighlighter(QSyntaxHighlighter):
    """Простая подсветка синтаксиса Python."""

    def __init__(self, document: QTextDocument) -> None:
        """Создаёт подсветчик для документа."""
        super().__init__(document)
        self._formats: Dict[str, QTextCharFormat] = {}

        keyword_format = QTextCharFormat()
        keyword_format.setForeground(QColor("#569CD6"))
        keyword_format.setFontWeight(QFont.Weight.Bold)
        self._formats["keyword"] = keyword_format

        string_format = QTextCharFormat()
        string_format.setForeground(QColor("#CE9178"))
        self._formats["string"] = string_format

        comment_format = QTextCharFormat()
        comment_format.setForeground(QColor("#6A9955"))
        self._formats["comment"] = comment_format

        number_format = QTextCharFormat()
        number_format.setForeground(QColor("#B5CEA8"))
        self._formats["number"] = number_format

        self._rules = [
            (QRegularExpression("#[^\\n]*"), "comment"),
            (QRegularExpression("\\b(?:" + "|".join(PYTHON_KEYWORDS) + ")\\b"), "keyword"),
            (QRegularExpression("\\b[0-9]+(?:\\.[0-9]+)?\\b"), "number"),
            (QRegularExpression("'(?:[^'\\\\]|\\\\.)*'"), "string"),
            (QRegularExpression('"(?:[^"\\\\]|\\\\.)*"'), "string"),
        ]

    def highlightBlock(self, text: str) -> None:  # noqa: N802
        """Подсвечивает блок текста."""
        for expression, fmt_name in self._rules:
            match_iterator = expression.globalMatch(text)
            while match_iterator.hasNext():
                match = match_iterator.next()
                self.setFormat(match.capturedStart(), match.capturedLength(), self._formats[fmt_name])


class ScriptEditor(QWidget):
    """Страница редактирования и управления Python-скриптами."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        """Создаёт редактор скриптов.

        Args:
            serial_manager: Менеджер COM-порта для отправки кадров из скриптов.
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self._serial_manager = serial_manager
        self._config = Config()
        self._engine = ScriptEngine()
        self._scripts: List[Dict[str, Any]] = []
        self._current_index = -1

        self._ensure_scripts_dir()
        self._create_widgets()
        self._build_layout()
        self._load_scripts()

    def _ensure_scripts_dir(self) -> None:
        """Создаёт папку для скриптов, если её нет."""
        SCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

    def _create_widgets(self) -> None:
        """Создаёт элементы управления."""
        font = QFont("Segoe UI", 10)

        self._title = QLabel(tr("Скриптовый редактор"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._scripts_list = QListWidget()
        self._scripts_list.setFont(font)
        self._scripts_list.currentRowChanged.connect(self._on_script_selected)
        self._scripts_list.itemChanged.connect(self._on_item_changed)

        self._add_button = QPushButton(tr("Добавить"))
        self._add_button.setFont(font)
        self._add_button.setFixedSize(90, 28)
        self._add_button.clicked.connect(self._on_add)

        self._delete_button = QPushButton(tr("Удалить"))
        self._delete_button.setFont(font)
        self._delete_button.setFixedSize(90, 28)
        self._delete_button.clicked.connect(self._on_delete)

        self._rename_button = QPushButton(tr("Переименовать"))
        self._rename_button.setFont(font)
        self._rename_button.setFixedSize(110, 28)
        self._rename_button.clicked.connect(self._on_rename)

        self._code_editor = CodeEditor()
        self._highlighter = PythonHighlighter(self._code_editor.document())
        self._code_editor.textChanged.connect(self._on_code_changed)

        self._run_button = QPushButton(tr("Запустить тест"))
        self._run_button.setFont(font)
        self._run_button.setFixedSize(120, 30)
        self._run_button.clicked.connect(self._on_run_test)

        self._output = QTextEdit()
        self._output.setFont(QFont("Consolas", 10))
        self._output.setReadOnly(True)
        self._output.setPlaceholderText(tr("Результат выполнения и ошибки"))

    def _build_layout(self) -> None:
        """Собирает компоновку страницы."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._title)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        left_panel = QWidget()
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(QLabel(tr("Скрипты:")))
        left_layout.addWidget(self._scripts_list)
        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)
        buttons_layout.addWidget(self._add_button)
        buttons_layout.addWidget(self._delete_button)
        buttons_layout.addWidget(self._rename_button)
        left_layout.addLayout(buttons_layout)

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(self._code_editor, 1)
        right_layout.addWidget(self._run_button)
        right_layout.addWidget(QLabel(tr("Вывод:")))
        right_layout.addWidget(self._output)

        splitter.addWidget(left_panel)
        splitter.addWidget(right_panel)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    def _load_scripts(self) -> None:
        """Загружает список скриптов из конфигурации."""
        self._scripts = self._config.get("scripts", [])
        if not isinstance(self._scripts, list):
            self._scripts = []
        self._refresh_list()
        if self._scripts_list.count() > 0:
            self._scripts_list.setCurrentRow(0)

    def _save_scripts(self) -> None:
        """Сохраняет список скриптов в конфигурацию."""
        self._config.set("scripts", self._scripts)

    def _refresh_list(self) -> None:
        """Обновляет список скриптов."""
        self._scripts_list.clear()
        for script in self._scripts:
            name = script.get("name", tr("Без имени"))
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if script.get("active", False) else Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, script.get("filename", ""))
            self._scripts_list.addItem(item)

    def _current_script(self) -> Optional[Dict[str, Any]]:
        """Возвращает текущий выбранный скрипт."""
        if 0 <= self._current_index < len(self._scripts):
            return self._scripts[self._current_index]
        return None

    def _current_filename(self) -> Optional[Path]:
        """Возвращает путь к файлу текущего скрипта."""
        script = self._current_script()
        if script is None:
            return None
        return SCRIPTS_DIR / script.get("filename", "")

    def _on_script_selected(self, index: int) -> None:
        """Обрабатывает смену выбранного скрипта."""
        if self._current_index >= 0 and self._current_index < len(self._scripts):
            self._save_current_code()
        self._current_index = index
        script = self._current_script()
        if script is None:
            self._code_editor.setPlainText("")
            return
        path = self._current_filename()
        if path and path.exists():
            code = path.read_text(encoding="utf-8")
        else:
            code = DEFAULT_SCRIPT
            self._save_code_to_file(code)
        self._code_editor.setPlainText(code)
        self._output.clear()

    def _save_current_code(self) -> None:
        """Сохраняет текущий код в файл."""
        path = self._current_filename()
        if path is None:
            return
        code = self._code_editor.toPlainText()
        path.write_text(code, encoding="utf-8")

    def _save_code_to_file(self, code: str) -> None:
        """Сохраняет код в файл текущего скрипта."""
        path = self._current_filename()
        if path is None:
            return
        path.write_text(code, encoding="utf-8")

    def _on_code_changed(self) -> None:
        """Автосохранение кода при изменении."""
        if self._current_index >= 0:
            self._save_current_code()

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        """Обрабатывает изменение чекбокса активности."""
        row = self._scripts_list.row(item)
        if 0 <= row < len(self._scripts):
            self._scripts[row]["active"] = item.checkState() == Qt.CheckState.Checked
            self._save_scripts()

    def _on_add(self) -> None:
        """Добавляет новый скрипт."""
        name, ok = QInputDialog.getText(self, tr("Новый скрипт"), tr("Название скрипта:"))
        if not ok or not name.strip():
            return
        filename = self._unique_filename(name.strip())
        script = {"name": name.strip(), "active": False, "filename": filename}
        self._scripts.append(script)
        self._save_scripts()
        (SCRIPTS_DIR / filename).write_text(DEFAULT_SCRIPT, encoding="utf-8")
        self._refresh_list()
        self._scripts_list.setCurrentRow(len(self._scripts) - 1)
        logger.info("Добавлен скрипт %s", filename)

    def _unique_filename(self, name: str) -> str:
        """Генерирует уникальное имя файла для скрипта."""
        base = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name)
        filename = f"{base}.py"
        counter = 1
        while (SCRIPTS_DIR / filename).exists():
            filename = f"{base}_{counter}.py"
            counter += 1
        return filename

    def _on_delete(self) -> None:
        """Удаляет выбранный скрипт."""
        if self._current_index < 0 or self._current_index >= len(self._scripts):
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите скрипт для удаления"))
            return
        script = self._scripts[self._current_index]
        reply = QMessageBox.question(
            self,
            tr("Удалить скрипт"),
            tr("Удалить скрипт «{0}»?").format(script.get("name", "")),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        path = SCRIPTS_DIR / script.get("filename", "")
        if path.exists():
            path.unlink()
        del self._scripts[self._current_index]
        self._save_scripts()
        self._current_index = -1
        self._refresh_list()
        if self._scripts_list.count() > 0:
            self._scripts_list.setCurrentRow(0)
        else:
            self._code_editor.setPlainText("")
        logger.info("Удалён скрипт %s", script.get("filename", ""))

    def _on_rename(self) -> None:
        """Переименовывает выбранный скрипт."""
        script = self._current_script()
        if script is None:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите скрипт для переименования"))
            return
        new_name, ok = QInputDialog.getText(
            self, tr("Переименовать"), tr("Новое название:"), text=script.get("name", "")
        )
        if not ok or not new_name.strip():
            return
        script["name"] = new_name.strip()
        self._save_scripts()
        self._refresh_list()
        logger.info("Скрипт переименован в %s", new_name.strip())

    def _on_run_test(self) -> None:
        """Запускает текущий скрипт на тестовом CAN-кадре."""
        script = self._current_script()
        if script is None:
            self._output.append(tr("Скрипт не выбран"))
            return
        code = self._code_editor.toPlainText()
        test_frame = {"channel": 1, "id": 0x123, "data": [0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77, 0x88]}
        result = self._engine.run(code, test_frame)
        self._output.clear()
        if result["logs"]:
            self._output.append("\n".join(result["logs"]))
        if result["error"]:
            self._output.append(f"ERROR: {result['error']}")
        for channel, can_id, data in result["send_requests"]:
            self._output.append(
                f"SEND CAN{channel} ID=0x{int_to_hex(can_id, 8)} DATA={' '.join(int_to_hex(b, 2) for b in data)}"
            )
        if not result["logs"] and not result["error"] and not result["send_requests"]:
            self._output.append(tr("Скрипт выполнен без вывода"))

    def process_frame(self, frame: Dict[str, Any]) -> None:
        """Обрабатывает входящий CAN-кадр активными скриптами.

        Args:
            frame: Распакованный CAN-кадр.
        """
        if not self._scripts:
            return
        script_frame = {
            "channel": int(frame.get("channel", 0)),
            "id": int(frame.get("id", 0)),
            "data": list(bytes(frame.get("data", b""))),
        }
        for script in self._scripts:
            if not script.get("active", False):
                continue
            path = SCRIPTS_DIR / script.get("filename", "")
            if not path.exists():
                continue
            try:
                code = path.read_text(encoding="utf-8")
                result = self._engine.run(code, script_frame)
                if result["error"]:
                    logger.warning("Ошибка в скрипте %s: %s", script.get("filename"), result["error"])
                    continue
                for channel, can_id, data in result["send_requests"]:
                    from core.can_protocol import pack_can_frame
                    packet = pack_can_frame(channel, can_id, bytes(data))
                    self._serial_manager.send_data(packet)
                    logger.info(
                        "Скрипт %s отправил кадр ch=%d id=0x%s",
                        script.get("filename"),
                        channel,
                        int_to_hex(can_id, 8),
                    )
            except Exception as exc:  # noqa: BLE001
                logger.error("Ошибка выполнения скрипта %s: %s", script.get("filename"), exc)
