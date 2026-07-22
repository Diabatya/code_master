"""Визуализация топологии CAN-сети."""

from typing import Dict, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QBrush, QColor, QFont, QPen
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsLineItem,
    QGraphicsProxyWidget,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QLabel,
    QVBoxLayout,
    QWidget,
)

from models.translations import _ as tr
from models.utils import int_to_hex


class _PacketDot:
    """Точка, анимирующая движение пакета."""

    def __init__(
        self,
        item: QGraphicsEllipseItem,
        start: Tuple[float, float],
        end: Tuple[float, float],
        steps: int = 30,
    ) -> None:
        self.item = item
        self.start = start
        self.end = end
        self.steps = steps
        self.current = 0

    def advance(self) -> bool:
        """Сдвигает точку на следующий шаг. Возвращает True, если анимация завершена."""
        self.current += 1
        if self.current >= self.steps:
            return True
        t = self.current / self.steps
        x = self.start[0] + (self.end[0] - self.start[0]) * t
        y = self.start[1] + (self.end[1] - self.start[1]) * t
        self.item.setPos(x - 3, y - 3)
        return False


class CanTopologyWidget(QWidget):
    """Графическая сцена с двумя шинами, устройством и динамическими узлами."""

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self._view = QGraphicsView(self._scene, self)
        self._view.setRenderHints(self._view.renderHints() | Qt.RenderHints.Antialiasing)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self._view)

        self._scene.setSceneRect(0, 0, 800, 420)

        self._nodes: Dict[Tuple[int, int], QGraphicsEllipseItem] = {}
        self._node_texts: Dict[Tuple[int, int], QGraphicsProxyWidget] = {}
        self._next_node_y: Dict[int, float] = {1: 80.0, 2: 80.0}
        self._packets: list[_PacketDot] = []

        self._create_static_scene()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._animate_packets)
        self._timer.start(40)

    def _create_static_scene(self) -> None:
        pen = QPen(QColor("#6C8CFF"))
        pen.setWidth(3)

        # CAN1 шина слева
        self._can1_bus = QGraphicsLineItem(100, 50, 100, 370)
        self._can1_bus.setPen(pen)
        self._scene.addItem(self._can1_bus)

        # CAN2 шина справа
        self._can2_bus = QGraphicsLineItem(700, 50, 700, 370)
        self._can2_bus.setPen(pen)
        self._scene.addItem(self._can2_bus)

        # Устройство по центру
        device_brush = QBrush(QColor("#FF8C00"))
        device = QGraphicsEllipseItem(360, 170, 80, 80)
        device.setBrush(device_brush)
        device.setPen(QPen(QColor("#FFFFFF")))
        self._scene.addItem(device)

        device_label = QGraphicsTextItem(tr("Код Мастер"))
        device_label.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        device_label.setDefaultTextColor(QColor("#FFFFFF"))
        device_label.setPos(362, 200)
        device_label.setTextWidth(76)
        device_label.document().setDocumentMargin(0)
        device_label.setHtml(
            f'<div align="center" style="color:#FFFFFF;font-size:9pt;">{tr("Код Мастер")}</div>'
        )
        self._scene.addItem(device_label)

        # Линии связи устройства с шинами
        line_pen = QPen(QColor("#6C8CFF"))
        line_pen.setWidth(2)
        line_pen.setStyle(Qt.PenStyle.DashLine)
        left_line = QGraphicsLineItem(100, 210, 360, 210)
        left_line.setPen(line_pen)
        self._scene.addItem(left_line)
        right_line = QGraphicsLineItem(440, 210, 700, 210)
        right_line.setPen(line_pen)
        self._scene.addItem(right_line)

        # Подписи шин
        can1_label = QGraphicsTextItem(tr("CAN1"))
        can1_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        can1_label.setDefaultTextColor(QColor("#E0E0E0"))
        can1_label.setPos(80, 25)
        self._scene.addItem(can1_label)

        can2_label = QGraphicsTextItem(tr("CAN2"))
        can2_label.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        can2_label.setDefaultTextColor(QColor("#E0E0E0"))
        can2_label.setPos(680, 25)
        self._scene.addItem(can2_label)

    def add_frame(self, frame: Dict[str, object]) -> None:
        """Добавляет узел по ID и запускает анимацию пакета."""
        channel = int(frame.get("channel", 1))
        can_id = int(frame.get("id", 0))
        if can_id == 0:
            return
        self._add_or_update_node(channel, can_id)
        self._spawn_packet(channel, can_id)

    def _add_or_update_node(self, channel: int, can_id: int) -> None:
        key = (channel, can_id)
        if key in self._nodes:
            return
        x = 100 if channel == 1 else 700
        y = self._next_node_y.get(channel, 80.0)
        self._next_node_y[channel] = min(y + 40, 340.0)

        ellipse = QGraphicsEllipseItem(x - 8, y - 8, 16, 16)
        ellipse.setBrush(QBrush(QColor("#4CAF50")))
        ellipse.setPen(QPen(QColor("#FFFFFF")))
        self._scene.addItem(ellipse)
        self._nodes[key] = ellipse

        label = QLabel(int_to_hex(can_id, 8 if can_id > 0x7FF else 3))
        label.setStyleSheet("color: #E0E0E0; background: transparent; font-size: 9pt;")
        label.adjustSize()
        proxy = self._scene.addWidget(label)
        if channel == 1:
            proxy.setPos(x + 16, y - 10)
        else:
            proxy.setPos(x - 16 - label.width(), y - 10)
        self._node_texts[key] = proxy

    def _spawn_packet(self, channel: int, can_id: int) -> None:
        key = (channel, can_id)
        node = self._nodes.get(key)
        if node is None:
            return
        node_rect = node.rect()
        start_x = node_rect.x() + node_rect.width() / 2
        start_y = node_rect.y() + node_rect.height() / 2
        end_x = 400.0
        end_y = 210.0

        dot = QGraphicsEllipseItem(0, 0, 6, 6)
        dot.setBrush(QBrush(QColor("#FF8C00")))
        dot.setPen(QPen(QColor("#FFFFFF")))
        dot.setPos(start_x - 3, start_y - 3)
        self._scene.addItem(dot)
        self._packets.append(_PacketDot(dot, (start_x, start_y), (end_x, end_y)))

    def _animate_packets(self) -> None:
        finished: list[_PacketDot] = []
        for packet in self._packets:
            if packet.advance():
                finished.append(packet)
        for packet in finished:
            self._scene.removeItem(packet.item)
            self._packets.remove(packet)

    def retranslate_ui(self) -> None:
        """Обновляет подписи на сцене при смене языка."""
        pass
