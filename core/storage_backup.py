"""Резервное копирование маркерного хранилища на ПК и восстановление
из копии после обрыва обновления (CM-N9).

Обновление CDC/UART/DFU читает области хранилища в память до стирания;
обрыв связи посреди записи оставляет устройство в bootloader, а байты
в памяти процесса пропадают вместе с приложением. Резерв на диске
переживает это: манифест привязан к устройству отпечатком страницы
идентификации (имя/SN/VID/PID), целостность дампа закрыта sha256.

Восстановление — не «успешная перепрошивка»: повторный прогон
flash_firmware сканирует живую Flash, а для видов областей, которых
в ней уже нет, поднимает блобы из подходящего резерва и прогоняет их
через тот же plan_storage_relocation (force_write — страницы физически
стёрты, keep недопустим).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from core.storage_scan import region_generation, region_kind

logger = logging.getLogger(__name__)

MANIFEST_VERSION = 2


def backup_dir() -> Path:
    """Каталог резервов хранилища в пользовательских данных ПК."""
    from platformdirs import user_data_dir

    return (
        Path(user_data_dir("CodeMaster", appauthor=False, ensure_exists=True))
        / "storage_backups"
    )


def device_fingerprint(data: bytes | None) -> str | None:
    """Отпечаток страницы идентификации устройства (имя/SN/VID/PID).

    Стёртая/отсутствующая страница отпечатка не даёт — восстановление
    тогда допускается только из однозначного резерва."""
    if not data or all(b == 0xFF for b in data):
        return None
    return hashlib.sha256(bytes(data)).hexdigest()[:16]


def dump_backup(
    tail: bytes,
    scan_base: int,
    found: list[tuple[int, bytes]],
    fingerprints: set[str],
    *,
    restored_blobs: dict[str, bytes] | None = None,
    directory: Path | None = None,
) -> str:
    """Пишет дамп хвоста Flash + JSON-манифест в каталог резервов.

    fingerprints — допустимые отпечатки устройства (текущая страница
    идентификации и страница, которую запишет это обновление) — по ним
    повторная попытка найдёт подходящий резерв.
    restored_blobs — области, поднятые из старшего резерва: их байты не
    входят в tail (в живой Flash их уже нет) — вкладываются в манифест
    base64.
    Raises OSError — вызывающий решает, отменять ли обновление (честная
    политика CM-N9: без резерва деструктивных действий не делаем)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    tag = sorted(fingerprints)[0][:8] if fingerprints else "nodev"
    bdir = directory if directory is not None else backup_dir()
    bdir.mkdir(parents=True, exist_ok=True)
    bin_path = bdir / f"storage-{stamp}-{tag}.bin"
    bin_path.write_bytes(tail)
    restored_blobs = restored_blobs or {}
    regions = []
    for addr, blob in found:
        kind = region_kind(blob)
        entry = {
            "addr": f"0x{addr:08X}",
            "size": len(blob),
            "kind": kind,
            "generation": region_generation(blob),
        }
        if kind in restored_blobs and restored_blobs[kind] == blob:
            # Блоб поднят из старшего резерва — в дампе его нет,
            # вкладываем содержимое в манифест.
            entry["origin"] = "backup"
            entry["blob_b64"] = base64.b64encode(blob).decode("ascii")
        else:
            entry["origin"] = "flash"
            entry["offset"] = f"0x{addr - scan_base:X}"
        regions.append(entry)
    manifest = {
        "version": MANIFEST_VERSION,
        "created": stamp,
        "fingerprints": sorted(fingerprints),
        "dump_base": f"0x{scan_base:08X}",
        "dump_sha256": hashlib.sha256(tail).hexdigest(),
        "regions": regions,
    }
    manifest_path = bdir / f"storage-{stamp}-{tag}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return str(bin_path)


def _load_manifest(path: Path) -> dict | None:
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("Резерв %s не читается — пропуск", path)
        return None
    if not isinstance(m, dict) or m.get("version") != MANIFEST_VERSION:
        return None
    return m


def find_matching_backup(
    fingerprints: set[str], *, directory: Path | None = None
) -> list[tuple[int, bytes]] | None:
    """Новейший валидный резерв, привязанный к устройству.

    Манифест подходит, если его отпечаток пересекается с текущими
    отпечатками устройства. Если устройство сейчас не опознаётся
    (страница идентификации стёрта обрывом и образ её не несёт),
    принимается единственный резерв без отпечатка — неоднозначное
    соответствие не применяется: восстановить чужие настройки хуже,
    чем честно сообщить о потере.

    Возвращает [(addr, blob)] областей резерва или None."""
    bdir = directory if directory is not None else backup_dir()
    manifests = sorted(
        bdir.glob("storage-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    anonymous_ok = len(manifests) == 1
    for m_path in manifests:
        m = _load_manifest(m_path)
        if m is None:
            continue
        m_fps = set(m.get("fingerprints") or [])
        if m_fps & fingerprints:
            pass  # подошёл по отпечатку
        elif not m_fps and anonymous_ok:
            logger.warning(
                "Резерв %s без отпечатка устройства — принят как "
                "единственный кандидат", m_path,
            )
        else:
            continue
        bin_path = m_path.with_suffix(".bin")
        try:
            tail = bin_path.read_bytes()
        except OSError:
            logger.warning("Дамп резерва %s не найден — пропуск", m_path)
            continue
        if hashlib.sha256(tail).hexdigest() != m.get("dump_sha256"):
            logger.warning("Резерв %s повреждён (sha256) — пропуск", m_path)
            continue
        regions: list[tuple[int, bytes]] = []
        for r in m.get("regions", []):
            try:
                addr = int(r["addr"], 16)
                size = int(r["size"])
                if r.get("origin") == "backup":
                    blob = base64.b64decode(r["blob_b64"])
                else:
                    off = int(r["offset"], 16)
                    blob = tail[off : off + size]
                if len(blob) != size:
                    raise ValueError("размер области не сошёлся")
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Резерв %s: область %s повреждена — пропуск",
                    m_path, r.get("addr"),
                )
                break
            regions.append((addr, bytes(blob)))
        else:
            return regions
    return None
