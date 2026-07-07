"""Офлайн-библиотека конфигураций (.cmm) для приложения «Код Мастер»."""

import json
import shutil
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from models.config import Config
from models.logger import get_logger
from models.translations import _ as tr
from ui.can_trigger_tab import CanTriggerTab
from ui.flexible_logic_tab import FlexibleLogicTab

logger = get_logger(__name__)


LIBRARY_ROOT = Path(__file__).resolve().parent.parent / "library"


class PreviewDialog(QDialog):
    """Модальное окно предпросмотра конфигурации .cmm."""

    def __init__(self, config_data: Dict, parent: Optional[QWidget] = None) -> None:
        """Создаёт диалог предпросмотра.

        Args:
            config_data: Словарь с содержимым .cmm файла.
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self.setWindowTitle(tr("Предпросмотр конфигурации"))
        self.setMinimumSize(500, 400)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)

        self._text = QPlainTextEdit()
        self._text.setReadOnly(True)
        self._text.setFont(QFont("Consolas", 10))
        self._text.setPlainText(json.dumps(config_data, ensure_ascii=False, indent=2))
        layout.addWidget(self._text)

        close_button = QPushButton(tr("Закрыть"))
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button)


class LibraryBrowser(QWidget):
    """Виджет для просмотра, загрузки и сохранения конфигураций .cmm."""

    def __init__(
        self,
        trigger_tab: CanTriggerTab,
        flexible_logic_tab: FlexibleLogicTab,
        parent: Optional[QWidget] = None,
    ) -> None:
        """Создаёт библиотеку конфигураций.

        Args:
            trigger_tab: Вкладка триггеров для применения загруженных настроек.
            flexible_logic_tab: Вкладка гибкой логики.
            parent: Родительский виджет.
        """
        super().__init__(parent)
        self._config = Config()
        self._trigger_tab = trigger_tab
        self._flexible_logic_tab = flexible_logic_tab
        self._current_files: Dict[str, Path] = {}
        self._create_widgets()
        self._build_layout()
        self._scan_library()

    def _create_widgets(self) -> None:
        """Создаёт элементы управления."""
        font = QFont("Segoe UI", 10)

        self._title = QLabel(tr("Библиотека конфигураций"))
        self._title.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        self._title.setProperty("title", True)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setFont(font)
        self._tree.currentItemChanged.connect(self._on_tree_selection_changed)

        self._list = QListWidget()
        self._list.setFont(font)
        self._list.currentItemChanged.connect(self._on_list_selection_changed)

        self._info_label = QLabel(tr("Выберите конфигурацию"))
        self._info_label.setFont(font)
        self._info_label.setWordWrap(True)
        self._info_label.setMinimumHeight(60)

        self._load_button = QPushButton(tr("Загрузить"))
        self._load_button.setFont(font)
        self._load_button.setFixedSize(110, 28)
        self._load_button.clicked.connect(self._on_load)

        self._preview_button = QPushButton(tr("Предпросмотр"))
        self._preview_button.setFont(font)
        self._preview_button.setFixedSize(110, 28)
        self._preview_button.clicked.connect(self._on_preview)

        self._save_button = QPushButton(tr("Сохранить текущие настройки как…"))
        self._save_button.setFont(font)
        self._save_button.setFixedSize(210, 28)
        self._save_button.clicked.connect(self._on_save_current)

        self._import_button = QPushButton(tr("Импорт .cmm"))
        self._import_button.setFont(font)
        self._import_button.setFixedSize(110, 28)
        self._import_button.clicked.connect(self._on_import)

        self._export_button = QPushButton(tr("Экспорт .cmm"))
        self._export_button.setFont(font)
        self._export_button.setFixedSize(110, 28)
        self._export_button.clicked.connect(self._on_export)

    def _build_layout(self) -> None:
        """Собирает компоновку виджета."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._title)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._tree)
        splitter.addWidget(self._build_right_panel())
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    def _build_right_panel(self) -> QWidget:
        """Создаёт правую панель со списком и кнопками."""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(QLabel(tr("Конфигурации:")))
        layout.addWidget(self._list)
        layout.addWidget(self._info_label)

        buttons_layout = QHBoxLayout()
        buttons_layout.setSpacing(8)
        buttons_layout.addWidget(self._load_button)
        buttons_layout.addWidget(self._preview_button)
        buttons_layout.addStretch()
        buttons_layout.addWidget(self._save_button)
        layout.addLayout(buttons_layout)

        file_buttons_layout = QHBoxLayout()
        file_buttons_layout.setSpacing(8)
        file_buttons_layout.addStretch()
        file_buttons_layout.addWidget(self._import_button)
        file_buttons_layout.addWidget(self._export_button)
        layout.addLayout(file_buttons_layout)

        return widget

    def _scan_library(self) -> None:
        """Сканирует папку library и заполняет дерево категорий."""
        self._tree.clear()
        if not LIBRARY_ROOT.exists():
            LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)

        root = QTreeWidgetItem(self._tree, [tr("По производителю")])
        root.setExpanded(True)
        for path in sorted(LIBRARY_ROOT.iterdir()):
            if path.is_dir() and path.name not in ("scripts", "dbc", "Пользовательские", "Универсальные"):
                QTreeWidgetItem(root, [path.name])

        if (LIBRARY_ROOT / "Универсальные").exists():
            QTreeWidgetItem(self._tree, [tr("Универсальные")])
        if (LIBRARY_ROOT / "Пользовательские").exists():
            QTreeWidgetItem(self._tree, [tr("Пользовательские")])

        self._tree.setCurrentItem(root.child(0) if root.childCount() else root)

    def _category_path(self, item: QTreeWidgetItem) -> Optional[Path]:
        """Возвращает путь к категории по элементу дерева."""
        text = item.text(0)
        if text == tr("По производителю"):
            return None
        if text == tr("Универсальные"):
            return LIBRARY_ROOT / "Универсальные"
        if text == tr("Пользовательские"):
            return LIBRARY_ROOT / "Пользовательские"
        # Производитель
        return LIBRARY_ROOT / text

    def _on_tree_selection_changed(self, current: QTreeWidgetItem, previous: QTreeWidgetItem) -> None:
        """Заполняет список конфигураций для выбранной категории."""
        self._list.clear()
        self._current_files.clear()
        self._info_label.setText(tr("Выберите конфигурацию"))
        if current is None:
            return
        path = self._category_path(current)
        if path is None or not path.exists():
            return
        for file_path in sorted(path.glob("*.cmm")):
            name = self._config_name(file_path)
            item = QListWidgetItem(name)
            item.setData(Qt.ItemDataRole.UserRole, str(file_path))
            self._list.addItem(item)
            self._current_files[name] = file_path

    def _config_name(self, file_path: Path) -> str:
        """Возвращает название конфигурации из файла или имя файла."""
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            return data.get("name", file_path.stem)
        except Exception:  # noqa: BLE001
            return file_path.stem

    def _selected_file(self) -> Optional[Path]:
        """Возвращает путь к выбранной конфигурации."""
        item = self._list.currentItem()
        if item is None:
            return None
        return Path(item.data(Qt.ItemDataRole.UserRole))

    def _on_list_selection_changed(self, current: QListWidgetItem, previous: QListWidgetItem) -> None:
        """Показывает описание выбранной конфигурации."""
        if current is None:
            self._info_label.setText(tr("Выберите конфигурацию"))
            return
        file_path = Path(current.data(Qt.ItemDataRole.UserRole))
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            description = data.get("description", tr("Нет описания"))
            version = data.get("version", "-")
            triggers = len(data.get("triggers", []))
            rules = len(data.get("flexible_rules", []))
            info = (
                f"<b>{data.get('name', file_path.stem)}</b><br>"
                f"{tr('Версия')}: {version}<br>"
                f"{tr('Триггеров')}: {triggers} | {tr('Правил')}: {rules}<br>"
                f"{description}"
            )
            self._info_label.setText(info)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка чтения конфигурации %s: %s", file_path, exc)
            self._info_label.setText(tr("Ошибка чтения файла"))

    def _on_load(self) -> None:
        """Загружает выбранную конфигурацию в приложение."""
        file_path = self._selected_file()
        if file_path is None:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите конфигурацию для загрузки"))
            return
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            reply = QMessageBox.question(
                self,
                tr("Загрузить конфигурацию"),
                tr("Заменить текущие триггеры и правила выбранной конфигурацией?"),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

            triggers = data.get("triggers", [])
            rules = data.get("flexible_rules", [])
            self._config.set("triggers", triggers)
            self._config.set("flexible_rules", rules)
            self._trigger_tab.set_config(triggers)
            self._flexible_logic_tab.set_config(rules)
            QMessageBox.information(
                self, tr("Готово"), tr("Конфигурация «{0}» загружена").format(data.get("name", file_path.stem))
            )
            logger.info("Загружена конфигурация из %s", file_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка загрузки конфигурации %s: %s", file_path, exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось загрузить конфигурацию: {0}").format(exc))

    def _on_preview(self) -> None:
        """Открывает окно предпросмотра выбранной конфигурации."""
        file_path = self._selected_file()
        if file_path is None:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите конфигурацию для предпросмотра"))
            return
        try:
            data = json.loads(file_path.read_text(encoding="utf-8"))
            dialog = PreviewDialog(data, self)
            dialog.exec()
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка предпросмотра %s: %s", file_path, exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось открыть конфигурацию: {0}").format(exc))

    def _on_save_current(self) -> None:
        """Сохраняет текущие настройки в новый .cmm файл."""
        user_dir = LIBRARY_ROOT / "Пользовательские"
        user_dir.mkdir(parents=True, exist_ok=True)
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Сохранить текущие настройки"),
            str(user_dir),
            "CMM files (*.cmm)",
        )
        if not path:
            return
        if not path.endswith(".cmm"):
            path += ".cmm"
        try:
            data = {
                "name": Path(path).stem,
                "description": "",
                "version": "1.0",
                "triggers": self._trigger_tab.get_config(),
                "flexible_rules": self._flexible_logic_tab.get_config(),
            }
            Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            QMessageBox.information(self, tr("Готово"), tr("Настройки сохранены в {0}").format(path))
            logger.info("Настройки сохранены в %s", path)
            self._scan_library()
            self._tree.setCurrentItem(self._find_tree_item(tr("Пользовательские")))
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка сохранения настроек: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось сохранить настройки: {0}").format(exc))

    def _find_tree_item(self, text: str) -> Optional[QTreeWidgetItem]:
        """Ищет элемент дерева по тексту."""
        for i in range(self._tree.topLevelItemCount()):
            item = self._tree.topLevelItem(i)
            if item.text(0) == text:
                return item
            for j in range(item.childCount()):
                child = item.child(j)
                if child.text(0) == text:
                    return child
        return None

    def _on_import(self) -> None:
        """Импортирует .cmm файл извне в библиотеку."""
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("Импорт конфигурации"),
            "",
            "CMM files (*.cmm)",
        )
        if not path:
            return
        user_dir = LIBRARY_ROOT / "Пользовательские"
        user_dir.mkdir(parents=True, exist_ok=True)
        try:
            dest = user_dir / Path(path).name
            shutil.copy2(path, dest)
            QMessageBox.information(self, tr("Готово"), tr("Файл импортирован в {0}").format(dest))
            logger.info("Импортирована конфигурация %s -> %s", path, dest)
            self._scan_library()
            self._tree.setCurrentItem(self._find_tree_item(tr("Пользовательские")))
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка импорта: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось импортировать файл: {0}").format(exc))

    def _on_export(self) -> None:
        """Экспортирует выбранную конфигурацию в указанное место."""
        file_path = self._selected_file()
        if file_path is None:
            QMessageBox.warning(self, tr("Внимание"), tr("Выберите конфигурацию для экспорта"))
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            tr("Экспорт конфигурации"),
            file_path.name,
            "CMM files (*.cmm)",
        )
        if not path:
            return
        try:
            shutil.copy2(file_path, path)
            QMessageBox.information(self, tr("Готово"), tr("Конфигурация экспортирована в {0}").format(path))
            logger.info("Экспортирована конфигурация %s -> %s", file_path, path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Ошибка экспорта: %s", exc)
            QMessageBox.critical(self, tr("Ошибка"), tr("Не удалось экспортировать файл: {0}").format(exc))
