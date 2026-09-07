"""USB DFU protocol tests without real USB hardware."""

from unittest.mock import MagicMock

from core.dfu import DfuDevice


class FakeDfu(DfuDevice):
    def __init__(self) -> None:
        super().__init__(MagicMock())
        self.calls = []

    def _set_address(self, address: int) -> None:
        self.calls.append(("address", address))

    def _wait(self, status_timeout: int = 60000) -> None:
        self.calls.append(("wait", status_timeout))

    def _ctrl(self, request_type, request, value=0, data_or_wlength=0, timeout=5000):
        self.calls.append(("ctrl", request_type, request, value, bytes(data_or_wlength)))
        return b""

    def _get_transfer_size(self) -> int:
        return 4


def test_download_fast_uses_sequential_dfu_blocks() -> None:
    device = FakeDfu()
    device._download_fast(0x08008000, b"abcdefghij", 4)
    blocks = [call for call in device.calls if call[0] == "ctrl" and call[2] == 1]
    assert [call[3] for call in blocks] == [2, 3, 4]
    assert [call[4] for call in blocks] == [b"abcd", b"efgh", b"ij"]


def test_leave_tolerates_device_disconnect() -> None:
    device = FakeDfu()
    device._set_address = MagicMock(side_effect=OSError(5, "device disconnected"))
    device._ctrl = MagicMock(side_effect=OSError(5, "device disconnected"))
    device.leave()
    device._ctrl.assert_called_once()
