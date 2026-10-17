"""CM-FR12: реальный FLXB из core/flex_compiler.py прогоняется через
настоящий C-рантайм trigger.c (харнесс test_trigger_flex.c в
argv-режиме воспроизведения вектора).

Сценарий ревью: dyn_cache-переменная с именованными состояниями,
у «Драйв» два сырых алиаса (8/72) — переход между ними внутри
состояния не должен пересрабатывать; переход состояние→состояние и
выход→повторный вход дают ровно один фронт, как _dyn_flags на ПК.
Соседняя численная программа и задержанное действие проверяют, что
групповая защёлка не задела прочие записи.

Харнесс собирается cc прямо в тесте (нужен host-компилятор)."""

from __future__ import annotations

import os
import shutil
import struct
import subprocess

import pytest

from core.flex_compiler import (
    build_flex_blob,
    compile_rules,
    parse_flex_blob,
)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS_SRC = os.path.join(
    REPO, "firmware", "application", "tests", "test_trigger_flex.c"
)


@pytest.fixture(scope="module")
def harness(tmp_path_factory) -> str:
    cc = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if not cc:
        pytest.skip("нет host-компилятора C для харнесса trigger.c")
    out = str(tmp_path_factory.mktemp("flexvec") / "test_trigger_flex")
    cmd = [
        cc,
        "-I", os.path.join(REPO, "firmware/application/tests/stubs"),
        "-I", os.path.join(REPO, "firmware/application/Inc"),
        "-I", os.path.join(REPO, "firmware/application/Src"),
        HARNESS_SRC,
        "-o", out,
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


def _step(delta_ms, kind=0, ch=0, ext=0, rtr=0, can_id=0, dlc=8,
          data=b"") -> bytes:
    """Шаг вектора: delta до шага, kind 0 — кадр в OnFrame,
    1 — только Poll (ход времени для отложенных отправок)."""
    payload = (data + b"\x00" * 8)[:8]
    return struct.pack(
        "<IBBBBIB8s", delta_ms, kind, ch, ext, rtr, can_id, dlc, payload,
    )


def _run_vector(harness: str, blob: bytes, steps: list[bytes],
                tmp_path) -> list[tuple]:
    """Записывает файл вектора, гоняет харнесс, разбирает SEND-строки
    → [(tick, channel, id, dlc, data_bytes)]."""
    vf = tmp_path / "vector.bin"
    vf.write_bytes(
        struct.pack("<I", len(blob)) + blob
        + struct.pack("<I", len(steps)) + b"".join(steps)
    )
    r = subprocess.run(
        [harness, str(vf)],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"harness failed: {r.stderr}"
    sends = []
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] != "SEND":
            continue
        tick, ch = int(parts[1]), int(parts[2])
        can_id, dlc = int(parts[3], 16), int(parts[4])
        data = bytes(int(b, 16) for b in parts[5:])
        sends.append((tick, ch, can_id, dlc, data))
    return sends


def _frame_rule(state_or_thr, can_id, var="Передача", dyn=True,
                delay=0) -> dict:
    event = (
        {"type": "dyn", "var": var, "value": state_or_thr}
        if dyn else
        {"type": "num", "var": var, "value": state_or_thr, "dir": "gt"}
    )
    return {
        "title": f"P{can_id}", "active": True,
        "events": [event],
        "conditions": [],
        "actions": [{
            "type": "frame", "frame_enabled": True, "channel": 0,
            "extended": False, "id": can_id, "dlc": 8,
            "data": "DE AD", "delay": delay, "count": 1, "between": 0,
        }],
    }


def _vars() -> dict:
    gear = {
        "name": "Передача", "type": "dyn_cache", "storage": "ram",
        "channel": 2, "extended": False, "id": "200",
        "bytes": [2],
        "points": [[8, "Драйв"], [72, "Драйв"],
                   [9, "Спорт"], [73, "Спорт"]],
    }
    gear2 = {
        "name": "Передача2", "type": "dyn_cache", "storage": "ram",
        "channel": 2, "extended": False, "id": "210",
        "bytes": [2, 3],
        "points": [[0x0488, "Драйв"], [0x0208, "Драйв"]],
    }
    num = {
        "name": "V", "type": "num", "storage": "ram",
        "channel": 2, "extended": False, "id": "100",
        "bytes": [0, 1], "sblp": "le",
        "points": [[0, 0.0], [65535, 65535.0]],
    }
    return {"read": [gear, gear2, num], "control": [], "aux": []}


def test_alias_records_share_edge_group() -> None:
    """Компилятор: все RAW-алиасы одного состояния — одна группа
    (mask[5]), разные состояния — разные группы, прочие записи — 0."""
    rules = [
        _frame_rule("Драйв", "321"),
        _frame_rule("Спорт", "322"),
        _frame_rule("100", "323", var="V", dyn=False),
    ]
    recs, managed = compile_rules(rules, protocol=14, variables=_vars())
    assert managed == [True] * 3
    assert len(recs) == 5
    drive = [r for r in recs if r["tx_id"] == 0x321]
    sport = [r for r in recs if r["tx_id"] == 0x322]
    num = [r for r in recs if r["tx_id"] == 0x323]
    assert len(drive) == 2 and len(sport) == 2 and len(num) == 1
    assert {r["rx_data_mask"][5] for r in drive} != {0}
    assert len({r["rx_data_mask"][5] for r in drive}) == 1
    assert len({r["rx_data_mask"][5] for r in sport}) == 1
    assert {r["rx_data_mask"][5] for r in drive}.isdisjoint(
        {r["rx_data_mask"][5] for r in sport})
    assert num[0]["rx_data_mask"][5] == 0
    # значения алиасов на проводе — по одному eq на RAW
    assert sorted(
        int.from_bytes(r["rx_data"], "little") for r in drive
    ) == [8, 72]


def test_flex_c_alias_edge_vector(harness: str, tmp_path) -> None:
    """Реальный FLXB через компилятор → C-рантайм: алиасы «Драйв»
    8→72 — один фронт на состояние; «Драйв»→«Спорт», выход→вход —
    по фронту; численная программа и отложенная отправка не задеты."""
    rules = [
        _frame_rule("Драйв", "321"),
        _frame_rule("Спорт", "322"),
        # двухбайтовые алиасы одного состояния второй переменной
        _frame_rule("Драйв", "324", var="Передача2"),
        _frame_rule("100", "323", var="V", dyn=False),
        # отложенное действие — фронт «Драйв» также планирует серию
        _frame_rule("Спорт", "325", delay=10),
    ]
    variables = _vars()
    blob = build_flex_blob(rules, protocol=14, variables=variables)
    assert parse_flex_blob(blob) is not None

    def g(raw: int, delta: int = 10, dlc: int = 8) -> bytes:
        return _step(delta, can_id=0x200, dlc=dlc,
                     data=bytes([0, 0, raw & 0xFF, (raw >> 8) & 0xFF]))

    def g2(raw: int, delta: int = 10) -> bytes:
        return _step(delta, can_id=0x210, dlc=8,
                     data=bytes([0, 0, (raw >> 8) & 0xFF, raw & 0xFF]))

    def n(val: int, delta: int = 10) -> bytes:
        return _step(delta, can_id=0x100, dlc=8,
                     data=bytes([val & 0xFF, (val >> 8) & 0xFF]))

    steps = [
        g(8),            # холодный вход в «Драйв» → 0x321
        g(8),            # удержание — без повтора
        g(72),           # алиас того же состояния — НЕ фронт
        g(72),           # удержание
        g(9),            # «Драйв»→«Спорт»: фронт 0x322 + отложенный 0x325
        _step(1, kind=1),  # poll: серия 0x325 ждёт ещё 9 мс
        _step(10, kind=1),  # poll: 0x325 уходит
        g(73),           # алиас «Спорта» — не фронт
        g(0),            # выход из состояний
        g(72),           # повторный вход в «Драйв» — фронт
        g2(0x0488),      # двухбайтовый алиас второго состояния — 0x324
        g2(0x0208),      # второй алиас того же состояния — не фронт
        g2(0x0001),      # неизвестный RAW — выход
        g2(0x0208),      # повторный вход — фронт
        n(200),          # численный порог — фронт 0x323
        n(250),          # удержание
        n(50),           # выход
        n(120),          # повторный фронт
    ]
    sends = _run_vector(harness, blob, steps, tmp_path)
    assert [s[2] for s in sends] == [
        0x321, 0x322, 0x325, 0x321, 0x324, 0x324, 0x323, 0x323,
    ], sends
    # Отложенная серия 0x325: кадр-событие на t=50, delay_ms=10 →
    # срок 60, первый Poll с tick≥60 — шаг +10 после события → t=61.
    delayed = [s for s in sends if s[2] == 0x325]
    assert len(delayed) == 1
    assert delayed[0][0] == 61, sends


def test_flex_c_cold_start_and_dup_frames(harness: str, tmp_path) -> None:
    """Первый кадр в состоянии — сработка (флаг ПК тоже стартует с 0);
    повторные одинаковые кадры не пересрабатывают; кадр чужого ID
    защёлку группы не трогает."""
    rules = [_frame_rule("Драйв", "321")]
    blob = build_flex_blob(rules, protocol=14, variables=_vars())

    def g(raw: int, delta: int = 5, can_id: int = 0x200) -> bytes:
        return _step(delta, can_id=can_id, dlc=8,
                     data=bytes([0, 0, raw & 0xFF]))

    steps = [
        g(8),            # холодный вход → фронт
        g(8), g(8),      # дубли кадра — удержание, без повторов
        g(0, can_id=0x7FF),  # чужой ID — группу не касается
        g(8),            # состояние держится — снова без фронта
        g(0),            # выход
        g(0),            # снаружи — без фронта
        g(72),           # вход через второй алиас — фронт
    ]
    sends = _run_vector(harness, blob, steps, tmp_path)
    assert [s[2] for s in sends] == [0x321, 0x321], sends
