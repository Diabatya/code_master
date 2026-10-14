"""Резервное копирование маркерного хранилища на ПК и восстановление
из копии после обрыва обновления (CM-N9, политика CM-N12/CM-N13).

Обновление CDC/UART/DFU читает области хранилища в память до стирания;
обрыв связи посреди записи оставляет устройство в bootloader, а байты
в памяти процесса пропадают вместе с приложением. Резерв на диске
переживает это: манифест привязан к устройству отпечатком страницы
идентификации (имя/SN/VID/PID) и к операции — sha256 записываемого
образа; целостность дампа и каждого вложенного блоба закрыта sha256.

Восстановление — не «успешная перепрошивка»: повторный прогон
flash_firmware сканирует живую Flash и применяет резерв только при
необходимости (merge_backup_regions):
  - живой блок с битым payload (валидный CRC8 заголовка, невалидный
    CRC32 данных — типичный обрыв посреди записи) заменяется
    резервной копией (CM-N12);
  - отсутствующий вид области и более новое поколение поднимаются из
    резерва ТОЛЬКО при незавершённой операции (APP1 инвалиден):
    исправная Flash с сознательно отсутствующим видом не воскрешает
    его из истории (CM-N13);
  - резерв без подтверждённой принадлежности (отпечаток устройства и
    образ операции) никогда не применяется автоматически — даже будучи
    единственным файлом в каталоге. Явный выбор файла оператором —
    load_backup_file().
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path

from core.storage_scan import (
    region_generation,
    region_kind,
    region_payload_ok,
)

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

    Стёртая/отсутствующая страница отпечатка не даёт — автоматическое
    восстановление тогда невозможно (принадлежность не доказана)."""
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
    image_sha256: str | None = None,
    directory: Path | None = None,
) -> str:
    """Пишет дамп хвоста Flash + JSON-манифест в каталог резервов.

    fingerprints — допустимые отпечатки устройства (текущая страница
    идентификации и страница, которую запишет это обновление) — по ним
    повторная попытка найдёт подходящий резерв.
    image_sha256 — sha256 записываемого образа операции: резерв
    привязывается к конкретной незавершённой операции, а не «к
    последнему файлу» (CM-N13).
    restored_blobs — области, поднятые из старшего резерва: их байты не
    входят в tail (в живой Flash их уже нет) — вкладываются в манифест
    base64; целостность каждого блоба фиксируется sha256 региона.
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
            "sha256": hashlib.sha256(bytes(blob)).hexdigest(),
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
        "image_sha256": image_sha256,
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


def _load_backup_regions(m: dict, m_path: Path) -> list[tuple[int, bytes]] | None:
    """Читает области резерва по манифесту с проверкой целостности.

    Внешний .bin закрыт dump_sha256; каждый блоб (и вложенный
    blob_b64, и срез дампа) дополнительно проверяется sha256 региона —
    inline и referenced происхождение верифицируются одинаково
    строго (CM-N12). Нарушение — резерв целиком непригоден (None)."""
    bin_path = m_path.with_suffix(".bin")
    try:
        tail = bin_path.read_bytes()
    except OSError:
        logger.warning("Дамп резерва %s не найден — пропуск", m_path)
        return None
    if hashlib.sha256(tail).hexdigest() != m.get("dump_sha256"):
        logger.warning("Резерв %s повреждён (sha256) — пропуск", m_path)
        return None
    regions: list[tuple[int, bytes]] = []
    for r in m.get("regions", []):
        try:
            addr = int(r["addr"], 16)
            size = int(r["size"])
            if r.get("origin") == "backup":
                blob = base64.b64decode(r["blob_b64"], validate=True)
            else:
                off = int(r["offset"], 16)
                blob = tail[off : off + size]
            if len(blob) != size:
                raise ValueError("размер области не сошёлся")
            want_sha = r.get("sha256")
            if want_sha and hashlib.sha256(bytes(blob)).hexdigest() != want_sha:
                raise ValueError("sha256 области не сошёлся")
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Резерв %s: область %s повреждена (%s) — резерв отклонён",
                m_path, r.get("addr"), exc,
            )
            return None
        regions.append((addr, bytes(blob)))
    return regions


def check_backup_regions_arg(value: object, name: str = "explicit_backup") -> None:
    """Граница явного резерва (CM-N18): допустимы None либо список пар
    (int addr, bytes blob). Всё прочее — bool от Qt-сигнала clicked,
    строка пути и т.п. — отвергается понятной ошибкой ДО любых
    операций с устройством: параметр-данные не должен молча принимать
    нерелевантное значение сигнатуры."""
    if value is None:
        return
    ok = isinstance(value, (list, tuple)) and all(
        isinstance(item, tuple)
        and len(item) == 2
        and isinstance(item[0], int)
        and not isinstance(item[0], bool)
        and isinstance(item[1], (bytes, bytearray))
        for item in value
    )
    if not ok:
        raise ValueError(
            f"{name}: ожидается None или список (addr, bytes) областей "
            f"резерва — получено {type(value).__name__}"
        )


def list_backup_manifests(directory: Path | None = None) -> list[Path]:
    """Файлы манифестов резервов, новейшие первыми — для явного
    выбора оператором (восстановление с неподтверждённой
    идентичностью, CM-N13)."""
    bdir = directory if directory is not None else backup_dir()
    return sorted(
        bdir.glob("storage-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )


def manifest_info(manifest_path: str | Path) -> dict | None:
    """Метаданные резерва для показа оператору при явном выборе
    (CM-N17): дата создания, отпечатки устройства, sha256 образа
    операции и список областей (вид/адрес/поколение/размер).
    Битый/неполный манифест — None."""
    m = _load_manifest(Path(manifest_path))
    if m is None:
        return None
    return {
        "created": m.get("created") or "",
        "fingerprints": list(m.get("fingerprints") or []),
        "image_sha256": m.get("image_sha256") or "",
        "regions": [
            {
                "kind": r.get("kind") or "?",
                "addr": r.get("addr") or "?",
                "generation": r.get("generation", 0),
                "size": r.get("size", 0),
            }
            for r in m.get("regions", [])
        ],
    }


def load_backup_file(manifest_path: str | Path) -> list[tuple[int, bytes]] | None:
    """Явно выбранный оператором резерв — БЕЗ проверки отпечатка и
    образа: соответствие устройству/операции берёт на себя выбор
    пользователя (предупреждение показывает вызывающий). Целостность
    проверяется всегда — битый файл не применяется и вручную."""
    m_path = Path(manifest_path)
    m = _load_manifest(m_path)
    if m is None:
        return None
    return _load_backup_regions(m, m_path)


def find_matching_backup(
    fingerprints: set[str],
    *,
    image_sha256: str | None = None,
    directory: Path | None = None,
) -> list[tuple[int, bytes]] | None:
    """Новейший валидный резерв, ДОКАЗАННО привязанный к устройству
    и операции (CM-N13).

    Принадлежность подтверждается только пересечением отпечатков
    страницы идентификации: анонимный резерв (без отпечатков) и
    резерв чужого устройства автоматически не применяются — даже если
    это единственный файл в каталоге. Устройство без отпечатка
    (стёртая страница идентификации) тоже не получает автоматического
    восстановления: «не могу доказать принадлежность» — явный отказ,
    а не угадывание.

    image_sha256 — sha256 записываемого образа: резерв от другой
    операции (другого образа или без привязки к образу) отклоняется —
    recovery привязан к конкретной незавершённой операции.

    Возвращает [(addr, blob)] областей резерва или None."""
    bdir = directory if directory is not None else backup_dir()
    for m_path in list_backup_manifests(bdir):
        m = _load_manifest(m_path)
        if m is None:
            continue
        m_fps = set(m.get("fingerprints") or [])
        if not (m_fps & fingerprints):
            if m_fps or fingerprints:
                logger.warning(
                    "Резерв %s не подтверждён для этого устройства "
                    "(отпечатки не пересекаются) — пропуск", m_path,
                )
            else:
                logger.warning(
                    "Резерв %s: отпечаток устройства отсутствует — "
                    "принадлежность не доказана, пропуск", m_path,
                )
            continue
        if image_sha256 is not None and m.get("image_sha256") != image_sha256:
            logger.warning(
                "Резерв %s относится к другой операции/образу — "
                "пропуск (recovery привязан к незавершённой операции)",
                m_path,
            )
            continue
        regions = _load_backup_regions(m, m_path)
        if regions is not None:
            return regions
    return None


def merge_backup_regions(
    found: list[tuple[int, bytes]],
    backup_regions: list[tuple[int, bytes]],
    *,
    interrupted: bool,
    operator_directed: bool = False,
) -> tuple[list[tuple[int, bytes]], dict[str, bytes]]:
    """Сливает области PC-резерва в карту живых областей — политика
    CM-N12/CM-N13, общая для CDC/UART и DFU путей.

    Новейшее поколение резервного вида применяется когда:
      - живых областей этого вида нет вообще — при незавершённой
        операции (interrupted) ЛИБО при явном выборе резерва
        оператором (operator_directed — CM-N17): исправная Flash с
        сознательно отсутствующим видом не воскрешает его из истории
        автоматически, но явно выбранный резерв — команда на
        восстановление;
      - все живые копии вида повреждены (payload CRC32) — битый блок
        не вытесняет валидный резерв независимо от поколения;
      - операция незавершена/выбрана оператором и поколение резерва
        старше лучшей ЖИВОЙ валидной копии — незавершённое сохранение
        дописывается.
    Битый резервный блоб замены не делает.

    Возвращает (found, restored): found дополняется блобами резерва
    (планировщик выберет целостное/новейшее поколение), restored —
    kind → blob для force_write (страницы физически могут быть
    стёрты/повреждены — «оставить на месте» недопустимо).

    При operator_directed резерв выбран ЯВНО: область с битым payload
    в нём — не «пропустить молча», а отказать до стирания (CM-N17) —
    оператор заказывал восстановление конкретных данных, частичный
    результат выдавал бы повреждённое за восстановленное. Выбрасывает
    ValueError."""
    allowed_missing = interrupted or operator_directed
    live: dict[str, list[tuple[int, bytes]]] = {}
    for addr, blob in found:
        live.setdefault(region_kind(blob), []).append((addr, blob))
    newest_bak: dict[str, tuple[int, bytes]] = {}
    for b_addr, b_blob in backup_regions:
        kind = region_kind(b_blob)
        if kind == "?":
            if operator_directed:
                raise ValueError(
                    f"область резерва 0x{b_addr:08X} не похожа на "
                    "маркерное хранилище — резерв отклонён"
                )
            continue
        prev = newest_bak.get(kind)
        if prev is None or region_generation(b_blob) >= region_generation(
            prev[1]
        ):
            newest_bak[kind] = (b_addr, b_blob)
    restored: dict[str, bytes] = {}
    for kind, (b_addr, b_blob) in newest_bak.items():
        if kind == "trg2" or not region_payload_ok(b_blob):
            if operator_directed and kind != "trg2":
                raise ValueError(
                    f"область «{kind}» резерва 0x{b_addr:08X} повреждена "
                    "(payload CRC32 не сошёлся) — резерв отклонён"
                )
            continue
        entries = live.get(kind) or []
        live_ok = [e for e in entries if region_payload_ok(e[1])]
        if not entries:
            need = allowed_missing
        else:
            need = not live_ok or (
                allowed_missing
                and region_generation(b_blob)
                > max(region_generation(b) for _, b in live_ok)
            )
        if need:
            found.append((b_addr, b_blob))
            restored[kind] = b_blob
    return found, restored
