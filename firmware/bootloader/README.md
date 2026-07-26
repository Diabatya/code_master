# CodeMaster Bootloader для STM32F105RCT6

USB CDC bootloader, совместимый с протоколом AN3155 и с существующим `core/bootloader.py` из приложения «Код Мастер».

## Что делает

- Запускается при включении питания.
- Если прошивка приложения валидна и не запрошен bootloader — прыгает в приложение по адресу `0x0800_8000`.
- Иначе инициализирует USB CDC и ждёт команды по COM-порту.
- Поддерживает команды: `0x7F`, `0x00`, `0x01`, `0x02`, `0x11`, `0x21`, `0x31`, `0x43`, `0x44`.
- Позволяет стирать и прошивать flash приложения (`0x0800_8000` — `0x0803_FFFF`).
- Bootloader занимает `0x0800_0000` — `0x0800_7FFF` (32 КБ).

## Структура памяти

| Регион       | Адрес                    | Размер |
|--------------|--------------------------|--------|
| Bootloader   | `0x0800_0000`            | 32 КБ  |
| Application  | `0x0800_8000`            | 224 КБ |

## Требования

- `arm-none-eabi-gcc`
- `make`
- Подмодуль `firmware/libs/STM32CubeF1` (уже добавлен)

> **В окружении Devin отсутствует `arm-none-eabi-gcc`**, поэтому сборку firmware нужно выполнять на машине с установленным ARM GNU Toolchain (например, `xpack-arm-none-eabi-gcc` или `brew install arm-none-eabi-gcc`).

## Сборка

```bash
cd firmware/bootloader
make
```

Результат:
- `build/codemaster_bootloader.elf`
- `build/codemaster_bootloader.bin`
- `build/codemaster_bootloader.hex`

## Открытие в STM32CubeIDE

1. File → Open Projects from File System...
2. Указать `firmware/bootloader`.
3. Если CubeIDE не распознаёт Makefile — импортировать как **Existing Code as Makefile Project**.

## Первая прошивка bootloader

1. Поставить перемычку BOOT0 = 1, BOOT1 = 0.
2. Подключить USB, подать питание/reset.
3. В STM32CubeProgrammer выбрать **USB** → найдётся `STM32 BOOTLOADER` (`0483:DF11`).
4. Загрузить `build/codemaster_bootloader.hex`.
5. Start Address должен быть `0x0800_0000`.
6. Program & Verify.
7. Убрать перемычку BOOT0 = 0, перезапустить устройство.

После этого устройство определится как COM-порт `CodeMaster Bootloader` (`0483:5741`), и приложение «Код Мастер» сможет прошивать его по COM-порту без DFU-драйверов.

## Команды приложения

Для обновления прошивки из основного приложения:
- приложение должно записать magic-значение `0xDEADBEEF` по адресу `0x2000_4FF0` и вызвать `NVIC_SystemReset()`;
- после reset запустится bootloader, и ПК сможет прошить новую версию по USB CDC.

## PID/VID

- Bootloader: `VID=0483`, `PID=5741`
- Рекомендуемый PID для основного приложения: `PID=5740` (стандартный STM32 Virtual COM Port).
