# Agent notes

## STM32F105RCT6 USB CDC Bootloader

- Bootloader source: `firmware/bootloader/`
- Build command: `cd firmware/bootloader && make`
- Requires `arm-none-eabi-gcc` and `make`.
- This environment has `arm-none-eabi-gcc` installed (e.g., `/Applications/ArmGNUToolchain/15.2.rel1` via `/opt/homebrew/bin/arm-none-eabi-gcc`).
- Bootloader memory: `0x0800_0000`–`0x0800_7FFF` (32 KB)
- Application memory: `0x0800_8000`–`0x0803_FFFF` (224 KB)
- To enter bootloader from application, write `0xDEADBEEF` to `0x2000_4FF0` and call `NVIC_SystemReset()`.
- USB IDs:
  - Bootloader: `VID=0483`, `PID=5741`
  - Application: `VID=0483`, `PID=5740`

## Desktop Bootloader Client

- Implementation: `core/bootloader.py`
- Uses `pyserial` and `serial.tools.list_ports`.
- For USB CDC, `Bootloader.enter_bootloader()` now detects the application port (`PID=5740`) and sends `REBOOT_TO_BOOTLOADER_MAGIC` (`b"\x00REBOOT_TO_BOOTLOADER\n"`), then waits for the bootloader port (`PID=5741`) to appear.
- UART boards still use DTR/RTS fallback.
