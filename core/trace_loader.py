"""Разбор .trace/.csv логов в список кадров с таймингами.

Форматы (как в ui/can_analyzer.py):
- .trace: секции [CAN1]/[CAN2], строки
  «HH:MM:SS.mmm ID=.. DLC=.. DATA=.. PERIOD=.. ASCII=.. EXPL=.. [DIR=RX|TX]»
- CSV экспорта анализатора: channel,time,id,dlc,data,period,ascii,expl[,dir]
- потоковый CSV монитора: timestamp,channel,dir,id,dlc,data

Результат — list[dict]: time_ms (относительно первого кадра), channel
(1-based), id, dlc, data (bytes), rtr, tx (bool — направление TX).
"""

from __future__ import annotations

import csv
import re
from typing import Any, Dict, List, Optional

_TRACE_LINE_RE = re.compile(
    r"^(\S+)\s+ID=(\S+)\s+DLC=(\S+)\s+DATA=(.*?)\s+PERIOD=(.*?)\s+ASCII=(.*?)\s+EXPL=(.*?)(?:\s+DIR=(\S+))?$"
)


def _time_to_ms(text: str) -> Optional[float]:
    """«HH:MM:SS.mmm» → миллисекунды."""
    try:
        hms, _, frac = text.partition(".")
        h, m, s = hms.split(":")
        return (int(h) * 3600 + int(m) * 60 + int(s)) * 1000 + float(f"0.{frac or 0}") * 1000
    except (ValueError, AttributeError):
        return None


def _parse_data(text: str) -> bytes:
    text = text.strip()
    if not text:
        return b""
    try:
        return bytes(int(part, 16) for part in text.split())
    except ValueError:
        return b""


def _parse_id(text: str) -> Optional[int]:
    try:
        return int(text.strip(), 16)
    except ValueError:
        return None


def _frame(time_ms, channel, can_id, dlc, data, tx) -> Dict[str, Any]:
    return {
        "time_ms": time_ms,
        "channel": channel,
        "id": can_id,
        "dlc": dlc,
        "data": data,
        "rtr": False,
        "tx": tx,
    }


def parse_trace_frames(path: str) -> List[Dict[str, Any]]:
    """Читает .trace/.csv в кадры с относительными time_ms."""
    if path.lower().endswith(".csv"):
        frames = _parse_csv(path)
    else:
        frames = _parse_trace(path)
    frames = [f for f in frames if f["id"] is not None]
    if frames:
        t0 = float(frames[0]["time_ms"])
        for f in frames:
            f["time_ms"] = float(f["time_ms"]) - t0
            if f["dlc"] in (None, 0):
                f["dlc"] = len(f["data"])
    return frames


def _parse_trace(path: str) -> List[Dict[str, Any]]:
    frames: List[Dict[str, Any]] = []
    channel = 1
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if line.startswith("[CAN"):
                channel = 1 if "CAN1" in line else 2
                continue
            m = _TRACE_LINE_RE.match(line)
            if not m:
                continue
            time_s, id_s, dlc_s, data_s, _period, _ascii, _expl, dir_s = m.groups()
            can_id = _parse_id(id_s)
            if can_id is None:
                continue
            try:
                dlc = int(dlc_s)
            except ValueError:
                dlc = 0
            frames.append(
                _frame(
                    _time_to_ms(time_s) or 0.0,
                    channel,
                    can_id,
                    dlc,
                    _parse_data(data_s),
                    (dir_s or "RX").strip().upper() == "TX",
                )
            )
    return frames


def _parse_csv(path: str) -> List[Dict[str, Any]]:
    frames: List[Dict[str, Any]] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for values in csv.reader(f):
            if len(values) < 4 or values[0].lower() == "channel":
                continue
            # Потоковый CSV монитора: timestamp,channel,dir,id,dlc,data
            if len(values) >= 6 and values[2].strip().upper() in ("RX", "TX"):
                can_id = _parse_id(values[3])
                if can_id is None:
                    continue
                try:
                    channel = int(values[1])
                    dlc = int(values[4])
                except ValueError:
                    continue
                frames.append(
                    _frame(
                        _time_to_ms(values[0]) or 0.0,
                        channel,
                        can_id,
                        dlc,
                        _parse_data(values[5]),
                        values[2].strip().upper() == "TX",
                    )
                )
                continue
            # CSV анализатора: channel,time,id,dlc,data,...
            try:
                channel = int(values[0])
            except ValueError:
                continue
            can_id = _parse_id(values[2])
            if can_id is None:
                continue
            try:
                dlc = int(values[3])
            except ValueError:
                dlc = 0
            tx = len(values) >= 9 and values[8].strip().upper() == "TX"
            frames.append(
                _frame(
                    _time_to_ms(values[1]) or 0.0,
                    channel,
                    can_id,
                    dlc,
                    _parse_data(values[4]),
                    tx,
                )
            )
    return frames
