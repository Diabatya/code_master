import sys
sys.path.insert(0, "/Users/denis/Desktop/Работа/code_master")
from PySide6.QtCore import QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from core.serial_manager import SerialManager
from ui.main_window import MainWindow

app = QApplication(sys.argv)
sm = SerialManager()
sm.open_port("FAKE", 115200, emulation=True)
w = MainWindow(sm)
w.show()
print("main ok", flush=True)
print(f"flash button size: {w._flash_button.size().width()}x{w._flash_button.size().height()}", flush=True)
print(f"flash text: {repr(w._flash_button.text())}", flush=True)

# Открываем окно настроек напрямую: _on_configure_clicked() запускает
# асинхронный диалог подключения, который нельзя проверять сразу после вызова.
w._open_settings_window()
sw = w._settings_window
print("settings opened", flush=True)
trigger_tab = sw._trigger_tab
sw._tabs.setCurrentWidget(trigger_tab)
block = trigger_tab._blocks[0]
block["group"].setChecked(True)
cache = block["cache"]
print(f"cache check text: {cache['cache_check'].text()}", flush=True)
print(f"cache fields enabled default: {cache['fields_widget'].isEnabled()}", flush=True)
print(f"response enabled default: {block['response']['group'].isEnabled()}", flush=True)
cache["cache_check"].setChecked(True)
print(f"after check fields enabled: {cache['fields_widget'].isEnabled()}", flush=True)
print(f"after check response enabled: {block['response']['group'].isEnabled()}", flush=True)

monitor_tab = sw._monitor_tab
print(f"cyclic font: {monitor_tab._monitor1._cyclic_button.font().family()} {monitor_tab._monitor1._cyclic_button.font().pointSize()}", flush=True)

# auto focus data test
edits = block["recv"]["data"]
edits[0].setFocus()
QTest.keyClicks(edits[0], "12")
app.processEvents()
print(f"focus after 2 chars: {edits[1].hasFocus()}", flush=True)
edits[1].setFocus()
QTest.keyClick(edits[1], Qt.Key.Key_Backspace)
app.processEvents()
print(f"focus after backspace: {edits[0].hasFocus()}", flush=True)

# paste packet into trigger fields
app.clipboard().setText("ID=7FF DLC=2 DATA=DE AD")
block["recv"]["id"].setFocus()
QTest.keyClick(block["recv"]["id"], Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
app.processEvents()
print(f"trigger id after paste: {block['recv']['id'].text()}", flush=True)
print(f"trigger dlc after paste: {block['recv']['dlc'].value()}", flush=True)
print(f"trigger data after paste: {[e.text() for e in block['recv']['data'][:2]]}", flush=True)

# paste packet into monitor send fields
app.clipboard().setText("ID=123 DLC=3 DATA=AA BB CC")
monitor_tab._monitor1._send_id_edit.setFocus()
QTest.keyClick(monitor_tab._monitor1._send_id_edit, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
app.processEvents()
print(f"monitor id after paste: {monitor_tab._monitor1._send_id_edit.text()}", flush=True)
print(f"monitor dlc after paste: {monitor_tab._monitor1._send_dlc_spin.value()}", flush=True)
print(f"monitor data after paste: {[e.text() for e in monitor_tab._monitor1._send_data_edits[:3]]}", flush=True)

w._language_combo.setCurrentIndex(1)
app.processEvents()
print(f"flash english: {repr(w._flash_button.text())}", flush=True)

QTimer.singleShot(500, app.quit)
app.exec()
if sw:
    sw.close()
w.close()
sm.close_port()
print("done", flush=True)
