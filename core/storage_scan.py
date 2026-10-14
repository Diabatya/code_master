"""Поиск маркерных областей хранилища в дампе Flash (CM-CDC-01).

Области VARH/FLXH/EVLH/CFGH и TRGH не имеют фиксированных адресов —
storage.c находит их сканированием страниц за концом кода приложения.
Этот сканер повторяет ту же логику на стороне ПК: используется
сохранением областей при обновлении прошивки (CDC/UART/DFU) — фикс-
адресное предположение теряло данные при росте образа.

Заголовки: store_header_t (20 Б, crc8) у VARH/FLXH/EVLH/CFGH;
TRGH — свой 16-байтовый заголовок; легаси-пул триггеров TRG2 —
по фиксированному адресу без заголовка.
"""

from __future__ import annotations

import zlib

from core.stm32_info import EVENT_LOG_ADDR, FLASH_END_ADDR, TRIGGER_REGION_ADDR
from core.trigger_protocol import crc8

STORE_PAGE = 2048
# VARH, FLXH, EVLH, CFGH (настройки приложения сразу за кодом)
STORE_MAGICS = (0x56415248, 0x464C5848, 0x45564C48, 0x43464748)
STORE_MAX_PAYLOAD = 48 * 1024
TRIG_HEADER_MAGIC = 0x54524748  # "TRGH"
TRIG_MAGIC_LEGACY = 0x54524732  # "TRG2"
TRIG_MAX_RECORDS = 70
STORE_MAGIC_CFG = 0x43464748
STORE_MAGIC_FLEX = 0x464C5848
STORE_MAGIC_VAR = 0x56415248
STORE_MAGIC_EVLOG = 0x45564C48
STORE_CFG_PAGES = 1
STORE_EVLOG_PAGES = 4

_KIND_BY_MAGIC = {
    STORE_MAGIC_CFG: "cfg",
    STORE_MAGIC_FLEX: "flex",
    STORE_MAGIC_VAR: "var",
    STORE_MAGIC_EVLOG: "evlog",
    TRIG_HEADER_MAGIC: "trgh",
}


def find_storage_regions(
    blob: bytes, base_addr: int
) -> list[tuple[int, bytes]]:
    """Сканирует дамп Flash постранично и возвращает [(addr, образ)]
    всех валидных маркерных областей хранилища — для сохранения
    перед стиранием и восстановления после записи."""
    regions: list[tuple[int, bytes]] = []
    for off in range(0, len(blob) - 19, STORE_PAGE):
        magic = int.from_bytes(blob[off : off + 4], "little")
        if magic == TRIG_HEADER_MAGIC:
            version, count = blob[off + 4], blob[off + 5]
            if (
                version in (3, 4)
                and count <= TRIG_MAX_RECORDS
                and crc8(blob[off : off + 15]) == blob[off + 15]
            ):
                stride = 90 if version == 4 else 82
                size = -(-(16 + count * stride) // STORE_PAGE) * STORE_PAGE
                if off + size <= len(blob):
                    regions.append((base_addr + off, blob[off : off + size]))
        elif magic in STORE_MAGICS:
            payload_len = int.from_bytes(blob[off + 8 : off + 12], "little")
            version = int.from_bytes(blob[off + 16 : off + 18], "little")
            if (
                version == 1
                and payload_len <= STORE_MAX_PAYLOAD
                and crc8(blob[off : off + 19]) == blob[off + 19]
            ):
                size = -(-(20 + payload_len) // STORE_PAGE) * STORE_PAGE
                if off + size <= len(blob):
                    regions.append((base_addr + off, blob[off : off + size]))
    # Легаси-пул триггеров v2 — записи без заголовка по фиксированному
    # адресу; сохраняем весь старый диапазон до конца Flash (прошивка
    # при старте смигрирует записи в маркерную область).
    legacy_off = TRIGGER_REGION_ADDR - base_addr
    if (
        0 <= legacy_off <= len(blob) - 4
        and int.from_bytes(blob[legacy_off : legacy_off + 4], "little")
        == TRIG_MAGIC_LEGACY
    ):
        regions.append(
            (TRIGGER_REGION_ADDR, blob[legacy_off : FLASH_END_ADDR - base_addr])
        )
    return regions


def region_is_cfg(blob: bytes) -> bool:
    """Область начинается заголовком CFGH (настройки приложения)."""
    return (
        len(blob) >= 4
        and int.from_bytes(blob[:4], "little") == STORE_MAGIC_CFG
    )


def region_kind(blob: bytes) -> str:
    """Тип области по заголовку: cfg/flex/var/evlog/trgh/trg2 («?» — чужое)."""
    if len(blob) < 4:
        return "?"
    magic = int.from_bytes(blob[:4], "little")
    if magic == TRIG_MAGIC_LEGACY:
        return "trg2"
    return _KIND_BY_MAGIC.get(magic, "?")


def region_generation(blob: bytes) -> int:
    """Поколение области: store_header.generation (@4) / TRGH (@7)."""
    kind = region_kind(blob)
    if kind == "trgh":
        return int.from_bytes(blob[7:11], "little")
    if len(blob) >= 8 and kind in ("cfg", "flex", "var", "evlog"):
        return int.from_bytes(blob[4:8], "little")
    return 0


def region_payload_ok(blob: bytes) -> bool:
    """CRC32 полезной нагрузки store_header-области (cfg/flex/var/evlog).

    find_storage_regions проверяет только CRC8 заголовка — целостность
    данных отдельная проверка. TRGH/TRG2 возвращают True (их записи
    верифицирует прошивка — у заголовка пула есть только CRC8)."""
    kind = region_kind(blob)
    if kind not in ("cfg", "flex", "var", "evlog") or len(blob) < 20:
        return True
    payload_len = int.from_bytes(blob[8:12], "little")
    payload_crc = int.from_bytes(blob[12:16], "little")
    if 20 + payload_len > len(blob):
        return False
    return zlib.crc32(blob[20 : 20 + payload_len]) == payload_crc


class StoragePlanError(Exception):
    """Карта хранилища под новый образ противоречива — обновление
    должно быть отменено ДО первого деструктивного действия."""


def _spans_overlap(a_lo: int, a_hi: int, b_lo: int, b_hi: int) -> bool:
    return a_lo < b_hi and b_lo < a_hi


def _gap_center(lo: int, hi: int, page_size: int) -> int:
    """Центр свободного зазора — зеркало gap_center() из storage.c:
    точка стыковки TRGH (заканчивается на центре) и EVLH (начинается
    с центра), прижатая вниз так, чтобы кольцо журнала влезало до
    var_base."""
    if hi <= lo:
        return lo
    mid = lo + (hi - lo) // 2
    mid -= mid % page_size
    evlog_span = STORE_EVLOG_PAGES * page_size
    if mid + evlog_span > hi:
        mid = hi - evlog_span if hi >= lo + evlog_span else lo
    if mid < lo:
        mid = lo
    return mid


def plan_storage_relocation(
    found: list[tuple[int, bytes]],
    code_addr: int,
    code_len: int,
    *,
    page_size: int = STORE_PAGE,
    flash_end: int = FLASH_END_ADDR,
    write_limit: int = EVENT_LOG_ADDR,
    force_write: set[str] | frozenset[str] = frozenset(),
) -> dict:
    """План пересадки маркерных областей под новый образ кода.

    Учитывает ВСЕ живые области — не только накрытые образом: цель
    пересадки может сесть на живой блок, поэтому решения принимаются
    для полной карты до первого стирания.

    Якоря (storage.c):
      - CFGH — всегда 1 страница сразу за концом кода (резерв);
      - FLXH — сразу за страницей CFGH, цепочка растёт вверх;
      - VARH — прижата к концу Flash, не двигается;
      - TRGH — концом на центре зазора [flex_end, var_base);
      - EVLH — кольцо 4 страниц с центра зазора;
      - TRG2 (легаси) — фиксированный адрес, двигается только
        вместе с ним (зона вне записи — всегда остаётся).

    Поколения: переносится новейшее ЦЕЛОСТНОЕ (прошивка сканирует и
    берёт max generation — старшие копии история); битый payload не
    вытесняет валидную копию любого поколения (CM-N12). Старые
    поколения, накрытые образом или целью пересадки, помечаются
    dropped — актуальные данные при этом не теряются. Если лучшая
    копия вида повреждена и валидной альтернативы нет — отказ
    (StoragePlanError) до стирания: неподтверждённое восстановление
    не завершается записью APP1.

    force_write — виды областей, которые обязаны попасть в moves даже
    если целевой адрес совпадает с исходным: области, поднятые из
    PC-резерва после обрыва обновления (CM-N9), — физически их страницы
    могут быть стёрты, поэтому «оставить на месте» недопустимо.

    Возвращает dict:
      moves:    [(orig_addr, dst_addr, blob)] — записать после кода;
      keep:     [(addr, blob)] — области, остающиеся на месте (их
                страницы план гарантированно не стирает);
      dropped:  [(addr, blob, причина)] — устаревшие поколения,
                накрытые записью;
      warnings: [str] — нефатальные замечания.
    Raises StoragePlanError: область сохранить невозможно (нет места,
      фиксированный адрес занят образом и т.п.) — запись отменяется.
    Инвариант: каждая актуальная (новейшее поколение) область ровно
    один раз оказывается в moves или keep — «область есть, но в плане
    нет» недопустимо (CM-N8). В финале вся карта целей проверяется на
    пересечения и границы.
    """
    code_end = -(-(code_addr + code_len) // page_size) * page_size
    img_lo, img_hi = code_addr, code_end

    # Новейшее поколение каждой области; остальные — история.
    # Целостность важнее поколения (CM-N12): блок с валидным CRC8
    # заголовка, но битым CRC32 полезной нагрузки (обрыв посреди
    # записи) не вытесняет валидную копию — ни более старую в живой
    # Flash, ни поднятую из подтверждённого резерва ПК.
    newest: dict[str, tuple[int, bytes]] = {}
    dropped: list[tuple[int, bytes, str]] = []
    for addr, blob in sorted(found):
        kind = region_kind(blob)
        if kind == "?":
            continue
        if kind == "trg2":
            # Фиксированный адрес 0x0803E000 — выше зоны записи
            # AN3155, физически неперезаписываем: всегда «на месте».
            continue
        ok = region_payload_ok(blob)
        prev = newest.get(kind)
        better = prev is None or (ok and not region_payload_ok(prev[1])) or (
            ok == region_payload_ok(prev[1])
            and region_generation(blob) >= region_generation(prev[1])
        )
        if better:
            if prev is not None:
                why = (
                    f"{kind}: повреждённый payload"
                    if not region_payload_ok(prev[1])
                    else f"{kind}: старое поколение"
                )
                dropped.append((prev[0], prev[1], why))
            newest[kind] = (addr, blob)
        else:
            why = (
                f"{kind}: повреждённый payload"
                if not ok
                else f"{kind}: старое поколение"
            )
            dropped.append((addr, blob, why))

    # CM-N12: выбранная (лучшая из имеющихся) копия повреждена —
    # целостность этого вида подтвердить нельзя: явный отказ в
    # «безопасном сохранении», а не запись APP1 поверх битых данных.
    # Обновление отменяется до стирания; продолжение без данных —
    # отдельная явная операция (preserve_storage=False).
    for kind, (addr, blob) in newest.items():
        if not region_payload_ok(blob):
            raise StoragePlanError(
                f"{kind} @0x{addr:08X}: CRC32 полезной нагрузки не "
                "сошёлся и валидной копии (живая Flash или "
                "подтверждённый резерв ПК) нет — восстановление "
                "не подтверждено, обновление отменено до стирания"
            )

    warnings: list[str] = []
    moves: list[tuple[int, int, bytes]] = []
    keep: list[tuple[int, bytes]] = []
    # Занятые спаны (lo, hi): образ, резерв CFGH, уже назначенные цели.
    occupied: list[tuple[int, int]] = [(img_lo, img_hi)]

    def place(addr: int, blob: bytes, dst: int, kind: str) -> None:
        """Назначить область на dst (или оставить, если уже там) —
        с проверкой столкновений со ВСЕЙ картой."""
        dst_end = dst + len(blob)
        if dst_end > write_limit:
            raise StoragePlanError(
                f"{kind}: целевая область 0x{dst:08X}–0x{dst_end:08X} "
                f"выше границы записи 0x{write_limit:08X} — "
                "не хватает места под новым образом"
            )
        for lo, hi in occupied:
            if _spans_overlap(dst, dst_end, lo, hi):
                raise StoragePlanError(
                    f"{kind}: цель пересадки 0x{dst:08X} накрывает "
                    f"занятую область 0x{lo:08X}–0x{hi:08X}"
                )
        for addr2, blob2 in keep:
            if _spans_overlap(dst, dst_end, addr2, addr2 + len(blob2)):
                # Цель садится на живую область — её тоже надо спасти.
                raise StoragePlanError(
                    f"{kind}: цель пересадки 0x{dst:08X} накрывает "
                    f"живую область 0x{addr2:08X} — план противоречив"
                )
        if dst != addr or kind in force_write:
            # force_write: область поднята из резерва ПК — её страницы
            # физически могут быть стёрты, поэтому она должна попасть
            # в moves и быть записана, даже если адрес прежний (CM-N9).
            moves.append((addr, dst, blob))
        else:
            keep.append((addr, blob))
        occupied.append((dst, dst_end))

    def try_keep(addr: int, blob: bytes, kind: str) -> bool:
        """Область можно оставить на месте, если её спан не накрывают
        ни образ, ни резерв/цели других областей."""
        lo, hi = addr, addr + len(blob)
        for o_lo, o_hi in occupied:
            if _spans_overlap(lo, hi, o_lo, o_hi):
                return False
        keep.append((addr, blob))
        occupied.append((lo, hi))
        return True

    # 1. CFGH — каноничная страница за кодом (резерв всегда).
    cfg_slot = (code_end, code_end + STORE_CFG_PAGES * page_size)
    if "cfg" in newest:
        addr, blob = newest["cfg"]
        if len(blob) > cfg_slot[1] - cfg_slot[0]:
            raise StoragePlanError(
                f"CFGH ({len(blob)} Б) не влезает в полку "
                f"{STORE_CFG_PAGES} стр. за кодом"
            )
        place(addr, blob, code_end, "cfg")
    occupied.append(cfg_slot)  # резерв — даже без самой области

    # 2. FLXH — канонично сразу за полкой CFGH.
    flex_dst = cfg_slot[1]
    flex_end_new = flex_dst
    if "flex" in newest:
        addr, blob = newest["flex"]
        place(addr, blob, flex_dst, "flex")
        flex_end_new = flex_dst + len(blob)

    # 3. VARH — прижата к верху Flash. В семантике прошивки цепочка
    #    занимает ВЕСЬ спан [base, FLASH_END) (storage.c: s_var.end =
    #    STORE_FLASH_END — старшие поколения append-записи лежат выше
    #    base), поэтому «оставить на месте» допустимо только когда все
    #    назначенные цели лежат ниже её base — иначе цель пересадки
    #    молча сотрёт живой блок (CM-N8).
    var_base = flash_end
    if "var" in newest:
        addr, blob = newest["var"]
        conflict = any(
            _spans_overlap(addr, flash_end, o_lo, o_hi)
            for o_lo, o_hi in occupied
        )
        if conflict or "var" in force_write:
            # Пересадка на самый высокий записываемый адрес — каноника
            # «у верха Flash». Для CDC-пути верх записи ограничен
            # AN3155 (write_limit); нет места — честный отказ, а не
            # молчаливая потеря данных. Без конфликта с картой
            # force_write-область (из резерва ПК) перезаписывается по
            # прежнему адресу, если он достижим для записи.
            dst = (
                addr
                if not conflict and addr + len(blob) <= write_limit
                else min(write_limit, flash_end) - len(blob)
            )
            if dst < img_hi:
                raise StoragePlanError(
                    f"VARH @0x{addr:08X} конфликтует с картой "
                    "размещения, а пересадка у верхней границы "
                    f"(0x{dst:08X}) накрывает образ — обновление "
                    "отменено"
                )
            for o_lo, o_hi in occupied:
                if _spans_overlap(dst, dst + len(blob), o_lo, o_hi):
                    raise StoragePlanError(
                        f"VARH: цель пересадки 0x{dst:08X} накрывает "
                        f"занятую область 0x{o_lo:08X}–0x{o_hi:08X} — "
                        "обновление отменено"
                    )
            moves.append((addr, dst, blob))
            addr = dst
        else:
            keep.append((addr, blob))
        # Полный спан цепочки — резерв для будущих целей TRGH/EVLH.
        occupied.append((addr, flash_end))
        var_base = addr

    # 4. Центр зазора — якорь TRGH (конец) и EVLH (начало).
    center = _gap_center(flex_end_new, var_base, page_size)

    # 5. TRGH — остаётся или садится концом на новый центр.
    #    force_write (область из резерва ПК): обязательно в moves —
    #    по прежнему адресу, если он свободен и записываем, иначе на
    #    каноничную цель.
    if "trgh" in newest:
        addr, blob = newest["trgh"]
        if "trgh" in force_write:
            place(addr, blob, addr, "trgh") \
                if addr + len(blob) <= write_limit \
                and not any(
                    _spans_overlap(addr, addr + len(blob), o_lo, o_hi)
                    for o_lo, o_hi in occupied
                ) \
                else place(addr, blob, center - len(blob), "trgh")
        elif not try_keep(addr, blob, "trgh"):
            place(addr, blob, center - len(blob), "trgh")

    # 6. EVLH — остаётся или начинается с нового центра.
    if "evlog" in newest:
        addr, blob = newest["evlog"]
        if "evlog" in force_write:
            place(addr, blob, addr, "evlog") \
                if addr + len(blob) <= write_limit \
                and not any(
                    _spans_overlap(addr, addr + len(blob), o_lo, o_hi)
                    for o_lo, o_hi in occupied
                ) \
                else place(addr, blob, center, "evlog")
        elif not try_keep(addr, blob, "evlog"):
            place(addr, blob, center, "evlog")

    # Инвариант итоговой карты (CM-N8): каждая актуальная область
    # ровно один раз в moves/keep; все цели попарно не пересекаются и
    # лежат в границах. Спан VARH учитывается до конца Flash — как в
    # прошивке (s_var.end = STORE_FLASH_END).
    # Резерв CFGH (cfg_slot) в финальную карту не входит: сама
    # область CFGH размещается ровно на этом спане, а «резерв без
    # области» уже защищён проверками occupied при размещении.
    spans: list[tuple[int, int, str]] = [(img_lo, img_hi, "образ")]
    placed: set[str] = set()
    for _orig, dst, blob in moves:
        kind = region_kind(blob)
        placed.add(kind)
        if dst + len(blob) > write_limit:
            raise StoragePlanError(
                f"{kind}: цель 0x{dst:08X}–0x{dst + len(blob):08X} "
                f"выше границы записи 0x{write_limit:08X} — "
                "обновление отменено"
            )
        spans.append(
            (dst, flash_end if kind == "var" else dst + len(blob), kind)
        )
    for a, blob in keep:
        kind = region_kind(blob)
        placed.add(kind)
        if a + len(blob) > flash_end:
            raise StoragePlanError(
                f"{kind}: область 0x{a:08X}–0x{a + len(blob):08X} "
                f"выходит за конец Flash 0x{flash_end:08X} — "
                "обновление отменено"
            )
        spans.append(
            (a, flash_end if kind == "var" else a + len(blob), kind)
        )
    for kind in newest:
        if kind not in placed:
            raise StoragePlanError(
                f"{kind}: актуальная область не попала в план "
                "размещения — обновление отменено (молчаливая потеря "
                "данных недопустима)"
            )
    spans.sort()
    for (lo1, hi1, k1), (lo2, hi2, k2) in zip(spans, spans[1:], strict=False):
        if lo2 < hi1:
            raise StoragePlanError(
                f"карта размещения противоречива: {k1} "
                f"(0x{lo1:08X}–0x{hi1:08X}) пересекается с {k2} "
                f"(0x{lo2:08X}–0x{hi2:08X}) — обновление отменено"
            )

    return {
        "moves": sorted(moves, key=lambda m: m[1]),
        "keep": sorted(keep),
        "dropped": dropped,
        "warnings": warnings,
    }
