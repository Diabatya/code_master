"""Встроенная справка приложения «Код Мастер»."""

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QPlainTextEdit,
    QPushButton,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from models.translations import _ as tr


class HelpWidget(QWidget):
    """Виджет справки с вкладками по разделам приложения."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._create_widgets()
        self._build_layout()

    def _create_widgets(self) -> None:
        self._tabs = QTabWidget(self)
        self._hotkeys_edit = QPlainTextEdit(self._hotkeys_text())
        self._monitoring_edit = QPlainTextEdit(self._monitoring_text())
        self._triggers_edit = QPlainTextEdit(self._triggers_text())
        self._gateway_edit = QPlainTextEdit(self._gateway_text())
        self._firmware_edit = QPlainTextEdit(self._firmware_text())
        self._faq_edit = QPlainTextEdit(self._faq_text())

        for edit in (
            self._hotkeys_edit,
            self._monitoring_edit,
            self._triggers_edit,
            self._gateway_edit,
            self._firmware_edit,
            self._faq_edit,
        ):
            edit.setReadOnly(True)

        self._tabs.addTab(self._hotkeys_edit, tr("Горячие клавиши"))
        self._tabs.addTab(self._monitoring_edit, tr("Мониторинг"))
        self._tabs.addTab(self._triggers_edit, tr("Триггеры"))
        self._tabs.addTab(self._gateway_edit, tr("Шлюз"))
        self._tabs.addTab(self._firmware_edit, tr("Прошивка"))
        self._tabs.addTab(self._faq_edit, tr("FAQ"))

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.addWidget(self._tabs)

    def _hotkeys_text(self) -> str:
        return tr(
            "Ctrl+S — сохранить текущие настройки\n"
            "Ctrl+Shift+P — открыть окно прошивки микроконтроллера\n"
            "F5 — запустить/остановить CAN-мониторинг\n"
            "Ctrl+Shift+T — переключиться на вкладку «Триггеры» и добавить триггер\n"
            "Ctrl+O / Ctrl+M — открыть обновление / настройки\n"
            "Esc — снять фокус с текущего поля"
        )

    def _monitoring_text(self) -> str:
        return tr(
            "Для запуска мониторинга откройте окно настроек (кнопка «Настроить») "
            "и перейдите на вкладку «Мониторинг».\n\n"
            "Запустите нужный канал кнопкой «Запустить» (или F5 в главном окне). "
            "Входящие CAN-кадры появятся в таблице с отметкой времени, ID, DLC, данными и ASCII.\n\n"
            "Используйте поле поиска для фильтрации по ID или данным. "
            "Кнопка «Фильтр» открывает диалог с правилами отображения.\n\n"
            "Кнопка «Запись в CSV» сохраняет весь принятый трафик, а чекбокс «Запись по триггеру» "
            "позволяет записывать только кадры с выбранным ID."
        )

    def _triggers_text(self) -> str:
        return tr(
            "Триггеры позволяют автоматически отвечать на выбранные CAN-кадры.\n\n"
            "1. Откройте вкладку «Триггеры».\n"
            "2. Активируйте нужный блок галочкой группы.\n"
            "3. Заполните ID условия, данные для фильтрации (опционально) и DLC.\n"
            "4. В блоке «Ответ» задайте ID, данные и задержку ответного кадра.\n"
            "5. При срабатывании условия ответный кадр отправится автоматически.\n\n"
            "Блок «Кэш» позволяет сохранять последний кадр в диапазоне и отправлять его при срабатывании."
        )

    def _gateway_text(self) -> str:
        return tr(
            "CAN-шлюз перенаправляет кадры между каналами CAN1 и CAN2.\n\n"
            "1. Откройте вкладку «Шлюз».\n"
            "2. В разделе «Игнорировать» введите ID, которые не нужно пересылать.\n"
            "3. В разделе «Правила подмены» задайте пары входной/выходной ID и данные.\n"
            "4. Нажмите «Запустить шлюз» для начала ретрансляции.\n\n"
            "Для работы шлюза оба канала должны быть активны."
        )

    def _firmware_text(self) -> str:
        return tr(
            "Для прошивки STM32 через UART bootloader:\n\n"
            "1. Подключите устройство к COM-порту и переведите его в режим bootloader (BOOT0 → VCC, RESET).\n"
            "2. В главном окне нажмите «Прошить микроконтроллер» (или Ctrl+Shift+P).\n"
            "3. Выберите способ программирования (ST-Link, J-Link, UART, USB DFU).\n"
            "4. Выберите файл прошивки .bin или .hex.\n"
            "5. Нажмите «Прошить» и дождитесь завершения.\n"
            "6. Отключите BOOT0 от VCC и нажмите RESET для запуска прошивки.\n\n"
            "Если прошивка не идёт, проверьте порт, скорость 115200 и линии RX/TX."
        )

    def _faq_text(self) -> str:
        return tr(
            "В: Как сменить язык интерфейса?\n"
            "О: Используйте выпадающий список языка в верхней панели главного окна.\n\n"
            "В: Порт не виден в списке\n"
            "О: Проверьте подключение адаптера и драйверы USB-UART (CH340, CP2102, FTDI).\n\n"
            "В: CAN-кадры не приходят\n"
            "О: Убедитесь, что скорость CAN соответствует сети, и запустите оба канала.\n\n"
            "В: Где хранятся настройки и логи?\n"
            "О: Настройки сохраняются в config.json, логи — в папке CodeMaster.\n\n"
            "В: Как открыть папку с логами?\n"
            "О: Нажмите кнопку «Логи» в главном окне."
        )

    def retranslate_ui(self) -> None:
        """Обновляет тексты вкладок справки."""
        self._tabs.setTabText(0, tr("Горячие клавиши"))
        self._tabs.setTabText(1, tr("Мониторинг"))
        self._tabs.setTabText(2, tr("Триггеры"))
        self._tabs.setTabText(3, tr("Шлюз"))
        self._tabs.setTabText(4, tr("Прошивка"))
        self._tabs.setTabText(5, tr("FAQ"))
        self._hotkeys_edit.setPlainText(self._hotkeys_text())
        self._monitoring_edit.setPlainText(self._monitoring_text())
        self._triggers_edit.setPlainText(self._triggers_text())
        self._gateway_edit.setPlainText(self._gateway_text())
        self._firmware_edit.setPlainText(self._firmware_text())
        self._faq_edit.setPlainText(self._faq_text())


def show_help(parent: Optional[QWidget] = None) -> None:
    """Открывает виджет справки в модальном диалоге."""
    dialog = QDialog(parent)
    dialog.setWindowTitle(tr("Справка"))
    dialog.resize(700, 500)
    layout = QVBoxLayout(dialog)
    help_widget = HelpWidget(dialog)
    layout.addWidget(help_widget)
    close_button = QPushButton(tr("Закрыть"))
    close_button.clicked.connect(dialog.accept)
    layout.addWidget(close_button)
    dialog.exec()
