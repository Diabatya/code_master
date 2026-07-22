"""Мастер первого запуска приложения «Код Мастер»."""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWizard,
    QWizardPage,
)

from core.can_protocol import DEVICE_TYPE_ANALOG, DEVICE_TYPE_BASIC, DEVICE_TYPE_CAN_FD
from models.config import Config
from models.translations import _ as tr, set_language

try:
    from serial.tools.list_ports import comports
except Exception:  # noqa: BLE001
    def comports() -> list:
        return []


class _WelcomePage(QWizardPage):
    """Страница приветствия."""

    def __init__(self, parent: Optional[Qt.Widget] = None) -> None:
        super().__init__(parent)
        self.setTitle(tr("Добро пожаловать"))
        self.setSubTitle(tr("Этот мастер поможет настроить приложение для первого использования"))
        layout = QVBoxLayout(self)
        logo = QLabel("🛠️ " + tr("Код Мастер"))
        logo.setFont(QFont("Segoe UI", 22, QFont.Weight.Bold))
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text = QLabel(tr("Нажмите «Далее», чтобы начать настройку."))
        text.setAlignment(Qt.AlignmentFlag.AlignCenter)
        text.setWordWrap(True)
        layout.addStretch()
        layout.addWidget(logo)
        layout.addWidget(text)
        layout.addStretch()


class _LanguagePage(QWizardPage):
    """Страница выбора языка."""

    def __init__(self, wizard_ref: "SetupWizard", parent: Optional[Qt.Widget] = None) -> None:
        super().__init__(parent)
        self._wizard = wizard_ref
        self._config = Config()
        self.setTitle(tr("Язык"))
        self.setSubTitle(tr("Выберите язык интерфейса"))
        layout = QVBoxLayout(self)
        self._combo = QComboBox()
        self._combo.addItem(tr("Русский"), "ru")
        self._combo.addItem(tr("English"), "en")
        current = self._config.get("language", "ru")
        for idx in range(self._combo.count()):
            if self._combo.itemData(idx) == current:
                self._combo.setCurrentIndex(idx)
                break
        self._combo.currentIndexChanged.connect(self._on_language_changed)
        layout.addWidget(self._combo)
        layout.addStretch()

    def _on_language_changed(self, index: int) -> None:
        lang = self._combo.itemData(index)
        if lang is None:
            return
        set_language(lang)
        self._config.set("language", lang)
        self._wizard.retranslate()


class _DeviceTypePage(QWizardPage):
    """Страница выбора типа устройства."""

    def __init__(self, parent: Optional[Qt.Widget] = None) -> None:
        super().__init__(parent)
        self._config = Config()
        self.setTitle(tr("Тип устройства"))
        self.setSubTitle(tr("Выберите конфигурацию подключённого устройства"))
        layout = QVBoxLayout(self)
        self._combo = QComboBox()
        self._combo.addItem(tr("2 CAN"), DEVICE_TYPE_BASIC)
        self._combo.addItem(tr("2 CAN +"), DEVICE_TYPE_ANALOG)
        self._combo.addItem(tr("2 CAN FD"), DEVICE_TYPE_CAN_FD)
        current = self._config.get("device_type", DEVICE_TYPE_BASIC)
        for idx in range(self._combo.count()):
            if self._combo.itemData(idx) == current:
                self._combo.setCurrentIndex(idx)
                break
        self._combo.currentIndexChanged.connect(self._on_type_changed)
        layout.addWidget(self._combo)
        layout.addStretch()

    def _on_type_changed(self, index: int) -> None:
        device_type = self._combo.itemData(index)
        if device_type is not None:
            self._config.set("device_type", device_type)


class _ConnectionPage(QWizardPage):
    """Страница выбора COM-порта."""

    def __init__(self, parent: Optional[Qt.Widget] = None) -> None:
        super().__init__(parent)
        self._config = Config()
        self.setTitle(tr("Подключение"))
        self.setSubTitle(tr("Выберите COM-порт для связи с устройством"))
        layout = QVBoxLayout(self)

        combo_layout = QHBoxLayout()
        self._combo = QComboBox()
        self._combo.setMinimumWidth(240)
        self._combo.currentTextChanged.connect(self._on_port_changed)
        self._refresh_button = QPushButton(tr("Обновить список портов"))
        self._refresh_button.clicked.connect(self._refresh_ports)
        combo_layout.addWidget(self._combo)
        combo_layout.addWidget(self._refresh_button)
        layout.addLayout(combo_layout)

        self._warning = QLabel(tr("Эмуляция используется только для тестирования без реального устройства"))
        self._warning.setStyleSheet("color: #FF9800;")
        self._warning.setWordWrap(True)
        self._warning.setVisible(False)
        layout.addWidget(self._warning)
        layout.addStretch()
        self._refresh_ports()

    def _refresh_ports(self) -> None:
        current = self._combo.currentText()
        self._combo.clear()
        self._combo.addItem(tr("FAKE (эмулятор)"), "FAKE")
        for port_info in comports():
            self._combo.addItem(port_info.device, port_info.device)
        if current:
            index = self._combo.findText(current)
            if index >= 0:
                self._combo.setCurrentIndex(index)
            else:
                saved = self._config.get("port", "")
                if saved:
                    idx = self._combo.findText(saved)
                    if idx >= 0:
                        self._combo.setCurrentIndex(idx)
        self._on_port_changed(self._combo.currentText())

    def _on_port_changed(self, text: str) -> None:
        is_fake = text.startswith("FAKE")
        self._warning.setVisible(is_fake)
        port = "FAKE" if is_fake else text
        self._config.set("port", port)
        self._config.set("emulation", is_fake)

    def initializePage(self) -> None:
        super().initializePage()
        self._refresh_ports()


class _FinishPage(QWizardPage):
    """Завершающая страница мастера."""

    def __init__(self, parent: Optional[Qt.Widget] = None) -> None:
        super().__init__(parent)
        self.setTitle(tr("Настройка завершена"))
        self.setSubTitle(tr("Приложение готово к работе"))
        self.setFinalPage(True)
        layout = QVBoxLayout(self)
        label = QLabel(tr("Настройка завершена! Нажмите «Готово», чтобы открыть главное окно."))
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        label.setWordWrap(True)
        layout.addStretch()
        layout.addWidget(label)
        layout.addStretch()


class SetupWizard(QWizard):
    """Мастер первого запуска."""

    def __init__(self, parent: Optional[Qt.Widget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Мастер настройки"))
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self._config = Config()
        self._pages: list[QWizardPage] = []
        self._create_pages()
        self.retranslate()

    def _create_pages(self) -> None:
        self._welcome = _WelcomePage(self)
        self._language = _LanguagePage(self, self)
        self._device = _DeviceTypePage(self)
        self._connection = _ConnectionPage(self)
        self._finish = _FinishPage(self)
        self._pages = [
            self._welcome,
            self._language,
            self._device,
            self._connection,
            self._finish,
        ]
        for page in self._pages:
            self.addPage(page)

    def retranslate(self) -> None:
        self.setWindowTitle(tr("Мастер настройки"))
        self.setButtonText(QWizard.WizardButton.BackButton, tr("Назад"))
        self.setButtonText(QWizard.WizardButton.NextButton, tr("Далее"))
        self.setButtonText(QWizard.WizardButton.FinishButton, tr("Готово"))
        self.setButtonText(QWizard.WizardButton.CancelButton, tr("Отмена"))

        self._welcome.setTitle(tr("Добро пожаловать"))
        self._welcome.setSubTitle(tr("Этот мастер поможет настроить приложение для первого использования"))

        self._language.setTitle(tr("Язык"))
        self._language.setSubTitle(tr("Выберите язык интерфейса"))

        self._device.setTitle(tr("Тип устройства"))
        self._device.setSubTitle(tr("Выберите конфигурацию подключённого устройства"))

        self._connection.setTitle(tr("Подключение"))
        self._connection.setSubTitle(tr("Выберите COM-порт для связи с устройством"))

        self._finish.setTitle(tr("Настройка завершена"))
        self._finish.setSubTitle(tr("Приложение готово к работе"))
