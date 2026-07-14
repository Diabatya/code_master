"""Модальное окно юридического предупреждения при запуске приложения."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from models.translations import _ as tr


class DisclaimerDialog(QDialog):
    """Диалог отказа от ответственности, показываемый при каждом запуске."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("ВНИМАНИЕ!"))
        self.setModal(True)
        self.setWindowFlags(self.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint)
        self.setMinimumWidth(540)
        self.setMinimumHeight(320)

        self._build_layout()
        self._apply_style()

    def _build_layout(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(18)
        layout.setContentsMargins(24, 24, 24, 24)

        title = QLabel(tr("ВНИМАНИЕ!"))
        title.setFont(QFont("Segoe UI", 18, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setObjectName("disclaimer_title")

        text = QLabel(
            tr(
                "Используя данное программное обеспечение, вы принимаете на себя все риски, "
                "связанные с возможным повреждением электронных блоков управления (ЭБУ) автомобиля, "
                "CAN-оборудования, микроконтроллеров STM32, а также любых других устройств и систем автомобиля.\n\n"
                "Вы несёте полную и исключительную ответственность за любой ущерб, причинённый в результате "
                "прошивки, отправки CAN-сообщений, изменения конфигурации или иных действий, совершённых "
                "с помощью данного ПО.\n\n"
                'Нажимая кнопку "Принимаю", вы подтверждаете, что осознаёте возможные последствия и принимаете '
                "всю ответственность на себя."
            )
        )
        text.setFont(QFont("Segoe UI", 11))
        text.setWordWrap(True)
        text.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        text.setObjectName("disclaimer_text")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(tr("Принимаю"))
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText(tr("Выход"))
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("disclaimer_accept")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setObjectName("disclaimer_exit")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout.addWidget(title)
        layout.addWidget(text)
        layout.addStretch()
        layout.addWidget(buttons, alignment=Qt.AlignmentFlag.AlignCenter)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QDialog {
                background-color: #252538;
                color: #FFFFFF;
            }
            QLabel {
                color: #FFFFFF;
            }
            QLabel#disclaimer_title {
                color: #FF9800;
            }
            QPushButton#disclaimer_accept,
            QPushButton#disclaimer_exit {
                background-color: #000000;
                color: #FFFFFF;
                border: 1px solid #3A3A5A;
                border-radius: 14px;
                padding: 8px 24px;
                min-width: 90px;
                font: 11pt "Segoe UI";
            }
            QPushButton#disclaimer_accept:hover,
            QPushButton#disclaimer_exit:hover {
                background-color: #FF9800;
                color: #000000;
            }
            QPushButton#disclaimer_accept:pressed,
            QPushButton#disclaimer_exit:pressed {
                background-color: #E68900;
            }
        """
        )
