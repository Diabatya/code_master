"""Утилиты для загрузки и подготовки файлов прошивки (.bin/.hex/.elf)."""

import tempfile
from pathlib import Path
from typing import Dict, Optional, Tuple


def _parse_intel_hex(text: str) -> Tuple[bytes, int]:
    """Парсит Intel HEX в бинарные данные и базовый адрес (fallback без intelhex)."""
    records: Dict[int, int] = {}
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
        except ValueError:
            continue
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
    current_high: Optional[int] = None
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


def _load_elf(path: Path) -> Tuple[bytes, int]:
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
        with open(path, "r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                return line.startswith(":")
    except Exception:
        pass
    return False


def load_firmware_bytes(file_path: str) -> Tuple[bytes, int]:
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


def prepare_bin_file(file_path: str, default_base: int = 0x08000000) -> Tuple[Optional[str], int]:
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
