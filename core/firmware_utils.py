"""Утилиты для загрузки и подготовки файлов прошивки (.bin/.hex/.elf)."""

import tempfile
from pathlib import Path


def _parse_intel_hex(text: str) -> tuple[bytes, int]:
    """Парсит Intel HEX в бинарные данные и базовый адрес (fallback без intelhex)."""
    records: dict[int, int] = {}
    base = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(":"):
            continue
        try:
            count = int(line[1:3], 16)
            addr = int(line[3:7], 16)
            rtype = int(line[7:9], 16)
            payload = bytes.fromhex(line[9 : 9 + count * 2])
            checksum = int(line[9 + count * 2 : 9 + count * 2 + 2], 16)
        except ValueError:
            continue
        # Контрольная сумма Intel HEX: дополнение до двух суммы
        # count+addr_hi+addr_lo+rtype+данные (см. _save_intel_hex ниже,
        # где она считается так же при записи). Раньше строка с битым
        # checksum молча пропускалась через continue при ValueError, но
        # НЕ проверялась вовсе, если хекс-цифры валидны, а сама сумма
        # повреждена — испорченный байт прошивки тихо принимался.
        computed = (-(count + (addr >> 8) + (addr & 0xFF) + rtype + sum(payload))) & 0xFF
        if computed != checksum:
            raise ValueError(
                f"Неверная контрольная сумма строки Intel HEX: {line!r} "
                f"(ожидалось 0x{computed:02X}, получено 0x{checksum:02X})"
            )
        if rtype == 0x00:
            for i, b in enumerate(payload):
                records[base + addr + i] = b
        elif rtype == 0x04 and len(payload) == 2:
            base = (payload[0] << 8 | payload[1]) << 16
        elif rtype == 0x01:
            break
    if not records:
        return b"", 0
    min_addr = min(records)
    max_addr = max(records)
    data = bytearray(b"\xFF" * (max_addr - min_addr + 1))
    for addr, b in records.items():
        data[addr - min_addr] = b
    return bytes(data), min_addr


def _save_intel_hex(data: bytes, base_address: int, path: Path) -> None:
    """Сохраняет данные в файл Intel HEX."""
    lines: list[str] = []
    pos = 0
    current_high: int | None = None
    while pos < len(data):
        addr = base_address + pos
        high = (addr >> 16) & 0xFFFF
        low = addr & 0xFFFF
        if high != current_high:
            cs = (2 + 0 + 0 + 4 + (high >> 8) + (high & 0xFF)) & 0xFF
            cs = (-cs) & 0xFF
            lines.append(f":02000004{high:04X}{cs:02X}")
            current_high = high
        chunk = data[pos : pos + 16]
        count = len(chunk)
        cs = count + (low >> 8) + (low & 0xFF) + 0
        for b in chunk:
            cs += b
        cs = (-cs) & 0xFF
        hex_data = chunk.hex().upper()
        lines.append(f":{count:02X}{low:04X}00{hex_data}{cs:02X}")
        pos += count
    lines.append(":00000001FF")
    path.write_text("\n".join(lines), encoding="utf-8")


def _load_elf(path: Path) -> tuple[bytes, int]:
    """Пытается прочитать ELF-файл через pyelftools; иначе как raw."""
    try:
        from elftools.elf.elffile import ELFFile

        data = bytearray()
        base = 0
        first = True
        with open(path, "rb") as fp:
            elf = ELFFile(fp)
            for seg in elf.iter_segments():
                if seg["p_type"] == "PT_LOAD":
                    seg_data = seg.data()
                    seg_addr = seg["p_paddr"] if seg["p_paddr"] else seg["p_vaddr"]
                    if first:
                        base = seg_addr
                        data.extend(seg_data)
                        first = False
                    else:
                        if seg_addr > base + len(data):
                            data.extend(b"\x00" * (seg_addr - base - len(data)))
                        data.extend(seg_data)
        return bytes(data), base
    except Exception:
        return path.read_bytes(), 0


def _is_intel_hex(path: Path) -> bool:
    """Проверяет, что файл по содержимому является Intel HEX."""
    try:
        with open(path, encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                return line.startswith(":")
    except Exception:
        pass
    return False


def load_firmware_bytes(file_path: str) -> tuple[bytes, int]:
    """Загружает прошивку (.bin/.hex/.elf) и возвращает (данные, базовый адрес)."""
    path = Path(file_path)
    if path.suffix.lower() == ".elf":
        return _load_elf(path)
    if path.suffix.lower() == ".hex" or _is_intel_hex(path):
        try:
            from intelhex import IntelHex

            ih = IntelHex(str(path))
            data = bytes(ih.tobinarray())
            return data, ih.minaddr() if ih.minaddr() is not None else 0
        except Exception:
            # Fallback на собственный парсер
            return _parse_intel_hex(path.read_text(encoding="utf-8"))
    return path.read_bytes(), 0


def guess_firmware_base(data: bytes) -> int:
    """Определяет базовый адрес .bin-образа по таблице векторов.

    У .bin нет адресной информации. Единственный надёжный признак —
    reset-вектор (data[4:8], Thumb): у объединённого образа там точка
    входа bootloader'а (< 0x08008000), у образа приложения — его
    собственные векторы (>= 0x08009000, см. новую карту памяти).
    Раньше любой .bin считался application-образом: при прошивке
    codemaster_full.bin через UART/USB CDC весь блоб писался со
    смещением, векторы приложения оказывались кодом bootloader'а
    и МК не запускался.
    """
    from core.stm32_info import (
        APPLICATION_BASE_ADDR,
        APP_METADATA_MAGIC,
        APP_METADATA_PAGE_ADDR,
        BOOTLOADER_BASE_ADDR,
        BOOTLOADER_SIZE,
    )

    if len(data) >= 8:
        # BIN, выписанный add_app_metadata.py, начинается со страницы
        # метаданных: первое слово — магик "APP1" (значение SP векторов
        # таким быть не может — SP всегда 0x2001xxxx).
        if int.from_bytes(data[0:4], "little") == APP_METADATA_MAGIC:
            return APP_METADATA_PAGE_ADDR
        reset = int.from_bytes(data[4:8], "little") & ~1
        if BOOTLOADER_BASE_ADDR <= reset < BOOTLOADER_BASE_ADDR + BOOTLOADER_SIZE:
            return BOOTLOADER_BASE_ADDR
    return APPLICATION_BASE_ADDR


def trim_to_application_region(data: bytes, base: int) -> tuple[bytes, int]:
    """Отрезает область bootloader'а (ниже 0x08008000) для записи через AN3155.

    Объединённый образ (bootloader + application) через UART/USB-CDC
    загрузчик прошить целиком нельзя: область bootloader защищена от
    записи (работающий загрузчик не может перезаписать сам себя).
    Страница идентификации устройства (0x08008000) — уже перезаписываемая,
    она в образе сохраняется. Для ROM DFU / ST-Link / J-Link обрезка
    не нужна — там пишется всё.

    Возвращает (данные, base) с base >= DEVICE_INFO_PAGE_ADDR; если в
    образе нет записываемой части — (b"", base)."""
    from core.stm32_info import DEVICE_INFO_PAGE_ADDR

    if base >= DEVICE_INFO_PAGE_ADDR:
        return data, base
    cut = DEVICE_INFO_PAGE_ADDR - base
    if len(data) <= cut:
        return b"", base
    return data[cut:], DEVICE_INFO_PAGE_ADDR


def validate_application_vector(data: bytes, base_address: int) -> tuple[bool, str]:
    """Проверяет MSP/reset vector application до начала Flash erase."""
    from core.stm32_info import (
        APPLICATION_BASE_ADDR,
        EVENT_LOG_ADDR,
    )

    # Векторы приложения лежат по APPLICATION_BASE_ADDR независимо от
    # того, с какого адреса начинается файл (полный образ — 0x08000000,
    # образ с info-страницей — 0x08008000, application-only — 0x08009000).
    if base_address > APPLICATION_BASE_ADDR:
        return True, ""
    offset = APPLICATION_BASE_ADDR - base_address
    if len(data) < offset + 8:
        # Application в образе нет (config-only прошивка) — нечего
        # валидировать.
        return True, ""
    vector = data[offset : offset + 8]
    if vector == b"\xFF" * 8:
        # Образ не содержит application (например, bootloader + config-
        # страница из _prepare_firmware_with_config): область векторов —
        # пустая прослойка разреженного HEX, валидировать нечего.
        return True, ""
    sp = int.from_bytes(vector[0:4], "little")
    reset = int.from_bytes(data[offset + 4 : offset + 8], "little")
    if not 0x20000000 <= sp <= 0x20010000:
        return False, f"Некорректный MSP: 0x{sp:08X}"
    if (reset & 1) == 0:
        return False, f"Reset vector не является Thumb-адресом: 0x{reset:08X}"
    reset &= ~1
    if not APPLICATION_BASE_ADDR <= reset < EVENT_LOG_ADDR:
        return False, f"Reset vector вне application: 0x{reset:08X}"
    return True, ""


def validate_write_region(base: int, size: int) -> tuple[bool, str]:
    """Проверяет, что область записи [base, base+size) допустима для AN3155.

    Новая карта Flash (отчёт мастера): через bootloader разрешена запись
    страницы идентификации устройства, метаданных приложения и кода —
    непрерывный диапазон 0x08008000–0x0803BFFF. Журнал событий
    (0x0803C000+) и хранилище триггеров/переменных у конца Flash через
    AN3155 не пишутся — они обновляются командами приложения.
    """
    from core.stm32_info import (
        DEVICE_INFO_PAGE_ADDR,
        EVENT_LOG_ADDR,
        FLASH_END_ADDR,
        TRIGGER_REGION_ADDR,
    )

    end = base + size
    if base < DEVICE_INFO_PAGE_ADDR:
        return False, (
            f"Адрес 0x{base:08X} ниже страницы идентификации "
            f"(0x{DEVICE_INFO_PAGE_ADDR:08X})"
        )
    if base >= EVENT_LOG_ADDR:
        return False, (
            f"Адрес 0x{base:08X} попадает в область журнала/хранилища "
            f"(0x{EVENT_LOG_ADDR:08X}–0x{FLASH_END_ADDR:08X}) — запись запрещена"
        )
    if end > EVENT_LOG_ADDR:
        return False, (
            f"Образ выходит за 0x{EVENT_LOG_ADDR:08X} в область журнала и "
            f"хранилища (до 0x{TRIGGER_REGION_ADDR:08X} включительно) — запись запрещена"
        )
    return True, ""


def prepare_bin_file(file_path: str, default_base: int = 0x08000000) -> tuple[str | None, int]:
    """Подготавливает временный .bin для утилит, которым нужен бинарный файл."""
    path = Path(file_path)
    data, base = load_firmware_bytes(file_path)
    if not data:
        return None, 0
    if base == 0:
        base = default_base
    if path.suffix.lower() == ".bin" and not _is_intel_hex(path):
        return file_path, base
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tmp:
        tmp.write(data)
        return tmp.name, base
