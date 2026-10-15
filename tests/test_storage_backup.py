"""Тесты core/storage_backup — PC-резерв маркерного хранилища (CM-N9).

Контракт: резерв привязан к устройству отпечатком страницы
идентификации, целостность дампа — sha256, имя файла уникально
(µs + тег), области «не из дампа» вкладываются в манифест base64.
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path

from core import storage_backup
from core.storage_scan import region_kind
from core.trigger_protocol import crc8

BASE = 0x08009000
CFG_MAGIC = 0x43464748
FLX_MAGIC = 0x464C5848
PAGE = 2048


def _region(magic: int, payload_len: int, fill: int = 0x5A,
            gen: int = 1) -> bytes:
    hdr = bytearray(20)
    hdr[0:4] = magic.to_bytes(4, "little")
    hdr[4:8] = gen.to_bytes(4, "little")
    hdr[8:12] = payload_len.to_bytes(4, "little")
    payload = bytes([fill] * payload_len)
    hdr[12:16] = zlib.crc32(payload).to_bytes(4, "little")
    hdr[16:18] = (1).to_bytes(2, "little")
    hdr[19] = crc8(bytes(hdr[:19]))
    size = -(-(20 + payload_len) // PAGE) * PAGE
    return bytes(hdr + payload + bytes([0xFF] * (size - 20 - payload_len)))


def test_dump_and_find_roundtrip(tmp_path: Path) -> None:
    cfg = _region(CFG_MAGIC, 40, fill=0x42)
    flx = _region(FLX_MAGIC, 60, fill=0x77)
    tail = cfg + flx + b"\xFF" * 100
    storage_backup.dump_backup(
        tail, BASE, [(BASE, cfg), (BASE + PAGE, flx)],
        {"fp-dev-1"}, directory=tmp_path,
    )
    found = storage_backup.find_matching_backup(
        {"fp-dev-1"}, directory=tmp_path
    )
    assert found is not None
    blobs = {region_kind(b): b for _a, b in found}
    assert blobs["cfg"] == cfg and blobs["flex"] == flx


def test_wrong_device_fingerprint_rejected(tmp_path: Path) -> None:
    cfg = _region(CFG_MAGIC, 40)
    storage_backup.dump_backup(
        cfg, BASE, [(BASE, cfg)], {"fp-dev-A"}, directory=tmp_path,
    )
    assert storage_backup.find_matching_backup(
        {"fp-dev-B"}, directory=tmp_path
    ) is None


def test_corrupt_dump_rejected(tmp_path: Path) -> None:
    cfg = _region(CFG_MAGIC, 40)
    bin_path = Path(storage_backup.dump_backup(
        cfg, BASE, [(BASE, cfg)], {"fp-dev-1"}, directory=tmp_path,
    ))
    data = bytearray(bin_path.read_bytes())
    data[30] ^= 0xFF  # байт внутри области CFGH
    bin_path.write_bytes(bytes(data))
    assert storage_backup.find_matching_backup(
        {"fp-dev-1"}, directory=tmp_path
    ) is None


def test_missing_dump_file_rejected(tmp_path: Path) -> None:
    cfg = _region(CFG_MAGIC, 40)
    bin_path = Path(storage_backup.dump_backup(
        cfg, BASE, [(BASE, cfg)], {"fp-dev-1"}, directory=tmp_path,
    ))
    bin_path.unlink()
    assert storage_backup.find_matching_backup(
        {"fp-dev-1"}, directory=tmp_path
    ) is None


def test_unique_names_same_device(tmp_path: Path) -> None:
    """Два резерва подряд не перезаписывают друг друга — имя
    включает микросекунды и тег отпечатка."""
    cfg = _region(CFG_MAGIC, 40)
    p1 = storage_backup.dump_backup(
        cfg, BASE, [(BASE, cfg)], {"fp"}, directory=tmp_path,
    )
    p2 = storage_backup.dump_backup(
        cfg, BASE, [(BASE, cfg)], {"fp"}, directory=tmp_path,
    )
    assert p1 != p2
    assert Path(p1).exists() and Path(p2).exists()


def test_restored_blobs_embedded_in_manifest(tmp_path: Path) -> None:
    """Область, поднятая из старшего резерва, в дампе отсутствует —
    её байты вкладываются в манифест и возвращаются при поиске."""
    cfg = _region(CFG_MAGIC, 40, fill=0x42)
    var = _region(0x56415248, 30, fill=0x55)
    # В хвосте только CFGH; VARH — «виртуальная» (из резерва).
    storage_backup.dump_backup(
        cfg, BASE, [(BASE, cfg), (BASE + PAGE, var)], {"fp"},
        restored_blobs={"var": var}, directory=tmp_path,
    )
    found = storage_backup.find_matching_backup(
        {"fp"}, directory=tmp_path
    )
    assert found is not None
    blobs = {region_kind(b): b for _a, b in found}
    assert blobs["var"] == var


def test_anonymous_backup_single_candidate(tmp_path: Path) -> None:
    """CM-N13: анонимный резерв («единственный файл») больше НЕ
    принимается автоматически — принадлежность устройства не
    доказана. Явный выбор файла оператором работает через
    load_backup_file (целостность проверяется всегда)."""
    cfg = _region(CFG_MAGIC, 40)
    storage_backup.dump_backup(
        cfg, BASE, [(BASE, cfg)], set(), directory=tmp_path,
    )
    assert storage_backup.find_matching_backup(
        set(), directory=tmp_path
    ) is None
    assert storage_backup.find_matching_backup(
        {"fp-board-B"}, directory=tmp_path
    ) is None
    manifest = storage_backup.list_backup_manifests(tmp_path)[0]
    regions = storage_backup.load_backup_file(manifest)
    assert regions is not None
    assert any(
        region_kind(b) == "cfg" for _a, b in regions
    )


def test_manifest_contains_provenance(tmp_path: Path) -> None:
    cfg = _region(CFG_MAGIC, 40, gen=7)
    storage_backup.dump_backup(
        cfg, BASE, [(BASE, cfg)], {"fp-x"}, directory=tmp_path,
    )
    manifest = next(tmp_path.glob("storage-*.json"))
    m = json.loads(manifest.read_text(encoding="utf-8"))
    assert m["version"] == storage_backup.MANIFEST_VERSION
    assert m["fingerprints"] == ["fp-x"]
    assert len(m["dump_sha256"]) == 64
    r = m["regions"][0]
    assert r["kind"] == "cfg" and r["generation"] == 7
    assert r["origin"] == "flash" and "offset" in r


def test_newest_manifest_wins(tmp_path: Path) -> None:
    """Несколько резервов устройства — применяется новейший (по
    mtime манифеста)."""
    import os

    old_cfg = _region(CFG_MAGIC, 40, fill=0x11)
    new_cfg = _region(CFG_MAGIC, 40, fill=0x22)
    p_old = Path(storage_backup.dump_backup(
        old_cfg, BASE, [(BASE, old_cfg)], {"fp"}, directory=tmp_path,
    )).with_suffix(".json")
    storage_backup.dump_backup(
        new_cfg, BASE, [(BASE, new_cfg)], {"fp"}, directory=tmp_path,
    )
    # Гарантируем старший mtime старому файлу — «новейший»
    # определяется по времени, а не по имени.
    st = p_old.stat()
    os.utime(p_old, (st.st_atime, st.st_mtime - 3600))
    found = storage_backup.find_matching_backup(
        {"fp"}, directory=tmp_path
    )
    assert found is not None
    blob = next(b for _a, b in found if region_kind(b) == "cfg")
    assert blob == new_cfg
