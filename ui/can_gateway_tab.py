"""Заглушка вкладки CAN-шлюза."""

from typing import Optional

from PySide6.QtGui import QFont
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from core.serial_manager import SerialManager
from models.translations import _ as tr


class CanGatewayTab(QWidget):
    """Вкладка CAN-шлюза (в разработке)."""

    def __init__(self, serial_manager: SerialManager, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        label = QLabel(tr("Страница CAN-шлюза в разработке"))
        label.setFont(QFont("Segoe UI", 14, QFont.Weight.Bold))
        label.setAlignment(0x0004 | 0x0080)
        layout.addWidget(label)
