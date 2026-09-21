"""Хост-симулятор движка триггеров — порт firmware/application/Src/trigger.c.

Позволяет «прогнать» последовательность CAN-кадров через настроенные
триггеры без железа и увидеть, какой триггер когда сработает и что
ответит — включая цепочки через TX-эхо (ответ триггера A виден триггеру
B, как в прошивке с CAN_TX_ECHO).

Записи — в формате unpack_trigger() (поля firmware trigger_t). Кадры —
dict: channel (0-based, как в firmware), id, extended, rtr, dlc, data,
echo (глубина TX-эха, 0 для кадра с шины).
"""

from __future__ import annotations

from typing import Any

CAN_TX_ECHO_MAX = 8  # как в firmware/Inc/can_bridge.h


def _ch_name(ch: int) -> str:
    return f"CAN{ch}"


class TriggerSimulator:
    """Состояние движка: записи, кэш-данные, отложенные ответы."""

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self._triggers = list(records)
        self._cache: list[dict[str, Any] | None] = [None] * len(records)
        self._cache_valid = [False] * len(records)
        self._pending: list[dict[str, Any] | None] = [None] * len(records)
        # Состояние «кол-во сработок до смены DATA» — порт fire_state_t.
        self._rx_state = [self._new_fire_state() for _ in records]
        self._src_state = [self._new_fire_state() for _ in records]
        self.fired_count = 0
        # События для UI: (time_ms, текст).
        self.events: list[dict[str, Any]] = []

    @staticmethod
    def _new_fire_state() -> dict[str, Any]:
        return {"have": False, "last": b"", "count": 0, "suppress": False}

    @staticmethod
    def _fire_track(state: dict[str, Any], frame: dict[str, Any]) -> None:
        """Порт fire_track(): смена DATA (на уровне ID-фильтра) сбрасывает
        защёлку лимита — триггер снова исполняет N сработок."""
        data = bytes(frame.get("data", b""))
        dlc = min(int(frame.get("dlc", len(data))), 8)
        last = data[:dlc]
        if not state["have"] or state["last"] != last:
            state["last"] = last
            state["have"] = True
            state["count"] = 0
            state["suppress"] = False

    @staticmethod
    def _listen_echo(t: dict[str, Any], prefix: str) -> bool:
        """«Слушать отправляемое»: True (по умолчанию, как v2) — триггер
        видит и свои TX-эха; False — только кадры с шины."""
        return bool(t.get(f"{prefix}_listen_echo", True))

    # --- матчеры: точный порт trigger.c --------------------------------

    @staticmethod
    def _rx_header_matches(t: dict[str, Any], frame: dict[str, Any]) -> bool:
        """Условие приёма без Data: RTR-режим, канал, битность, ID по
        маске, DLC — порт rx_header_matches(). На этом уровне для
        счётчика сработок отслеживается «смена DATA»."""
        rx_rtr = int(t.get("rx_rtr", 0))
        if rx_rtr == 1 and not frame.get("rtr"):
            return False
        if rx_rtr == 2 and frame.get("rtr"):
            return False
        if t.get("rx_channel", 0) != 2 and t.get("rx_channel", 0) != frame["channel"]:
            return False
        if int(t.get("rx_extended", 0)) != int(frame.get("extended", 0)):
            return False
        mask = int(t.get("rx_id_mask", 0))
        if (int(t.get("rx_id", 0)) & mask) != (int(frame["id"]) & mask):
            return False
        dlc = int(frame.get("dlc", len(frame.get("data", b""))))
        if t.get("rx_dlc", 0) and int(t["rx_dlc"]) != dlc:
            return False
        return True

    @staticmethod
    def _rx_data_matches(t: dict[str, Any], frame: dict[str, Any]) -> bool:
        """Побайтовое сравнение Data по маске (маска 0 — «X») — порт
        rx_data_matches(). В режиме «только RTR» данных нет — True."""
        if int(t.get("rx_rtr", 0)) == 1:
            return True
        rx_data = bytes(t.get("rx_data", b"\x00" * 8))
        rx_mask = bytes(t.get("rx_data_mask", b"\x00" * 8))
        data = bytes(frame.get("data", b""))
        dlc = int(frame.get("dlc", len(data)))
        for i in range(min(dlc, 8)):
            b = data[i] if i < len(data) else 0
            if (rx_data[i] & rx_mask[i]) != (b & rx_mask[i]):
                return False
        return True

    @staticmethod
    def _src_id_matches(t: dict[str, Any], frame: dict[str, Any]) -> bool:
        """Источник кэша на уровне ID (канал/битность/ID) — порт
        src_id_matches(); «смена DATA» счётчика кэша на этом уровне."""
        if t.get("src_channel", 0) != 2 and t.get("src_channel", 0) != frame["channel"]:
            return False
        if int(t.get("src_extended", 0)) != int(frame.get("extended", 0)):
            return False
        return int(t.get("src_id", 0)) == int(frame["id"])

    @staticmethod
    def _src_range_matches(t: dict[str, Any], frame: dict[str, Any]) -> bool:
        """Побайтовый диапазон «От/До»: каждый байт должен попасть в
        свой [from[i], to[i]]; from[i] > to[i] — wildcard «X» (игнор).
        Порт src_range_matches()."""
        src_dlc = int(t.get("src_dlc", 0))
        if not src_dlc:
            return True
        src_from = bytes(t.get("src_from", b"\x00" * 8))
        src_to = bytes(t.get("src_to", b"\xff" * 8))
        data = bytes(frame.get("data", b""))
        dlc = int(frame.get("dlc", len(data)))
        for i in range(min(src_dlc, 8)):
            if src_from[i] > src_to[i]:
                continue
            b = data[i] if i < dlc and i < len(data) else 0
            if b < src_from[i] or b > src_to[i]:
                return False
        return True

    # --- ответы ---------------------------------------------------------

    def _send_response(self, t: dict[str, Any], index: int, echo: int, now_ms: float) -> list[dict[str, Any]]:
        """Формирует кадры ответа триггера (порт send_response)."""
        if t.get("cache_enabled"):
            if not self._cache_valid[index]:
                return []
            resp = dict(self._cache[index])
            resp["rtr"] = False
        else:
            resp = {
                "extended": int(t.get("tx_extended", 0)),
                "rtr": bool(t.get("tx_rtr", 0)),
                "id": int(t.get("tx_id", 0)),
                "dlc": int(t.get("tx_dlc", 0)),
                "data": bytes(t.get("tx_data", b"\x00" * 8))[:8],
            }
        resp["echo"] = echo
        out = []
        tx_channel = int(t.get("tx_channel", 0))
        if tx_channel in (0, 2):
            out.append({**resp, "channel": 1})
        if tx_channel >= 1:
            out.append({**resp, "channel": 2})
        for frame in out:
            frame["trigger_index"] = index
            frame["time_ms"] = now_ms
        return out

    def _arm(self, index: int, t: dict[str, Any], echo: int, now_ms: float) -> None:
        self._pending[index] = {
            "fire_at": now_ms + int(t.get("delay_ms", 0)),
            "remaining": int(t.get("tx_count", 0)) or 1,
            "interval": int(t.get("tx_interval_ms", 0)),
            "echo": echo,
        }

    # --- публичный API ---------------------------------------------------

    def on_frame(self, frame: dict[str, Any], now_ms: float) -> list[dict[str, Any]]:
        """Кадр с шины (или TX-эхо) → список отправленных кадров-ответов."""
        sent: list[dict[str, Any]] = []
        for i, t in enumerate(self._triggers):
            if not t.get("enabled"):
                continue
            echo = int(frame.get("echo", 0))
            # Кэш: MUTE_ECHO — источник слушает только шину; fire_limit —
            # захват прекращается после N одинаковых DATA до их смены.
            if (
                t.get("cache_enabled")
                and not (echo and not self._listen_echo(t, "src"))
                and self._src_id_matches(t, frame)
            ):
                src_state = self._src_state[i]
                src_limit = int(t.get("src_fire_limit", 0))
                if src_limit:
                    self._fire_track(src_state, frame)
                if not src_state["suppress"] and self._src_range_matches(t, frame):
                    cached = dict(frame)
                    # Wildcard-позиции (from>to) обнуляются при захвате —
                    # в ответе на их месте уйдёт 0x00 (порт Trigger_OnFrame).
                    src_from = bytes(t.get("src_from", b"\x00" * 8))
                    src_to = bytes(t.get("src_to", b"\xff" * 8))
                    data = bytearray(cached.get("data", b""))
                    for j in range(min(int(t.get("src_dlc", 0)), 8)):
                        if src_from[j] > src_to[j] and j < len(data):
                            data[j] = 0
                    cached["data"] = bytes(data)
                    self._cache[i] = cached
                    self._cache_valid[i] = True
                    if src_limit:
                        src_state["count"] += 1
                        if src_state["count"] >= src_limit:
                            src_state["suppress"] = True
            if (echo and not self._listen_echo(t, "rx")) or not self._rx_header_matches(t, frame):
                continue
            rx_state = self._rx_state[i]
            rx_limit = int(t.get("rx_fire_limit", 0))
            if rx_limit:
                self._fire_track(rx_state, frame)
            if rx_state["suppress"] or not self._rx_data_matches(t, frame):
                continue
            sends = int(t.get("tx_count", 0)) or 1
            if int(t.get("delay_ms", 0)) == 0 and sends == 1:
                responses = self._send_response(t, i, echo, now_ms)
                if responses:
                    self.fired_count += 1
                sent.extend(responses)
            else:
                self._arm(i, t, echo, now_ms)
                self.events.append(
                    {
                        "time_ms": now_ms,
                        "trigger": i,
                        "kind": "armed",
                        "delay": int(t.get("delay_ms", 0)),
                    }
                )
            if rx_limit:
                rx_state["count"] += 1
                if rx_state["count"] >= rx_limit:
                    rx_state["suppress"] = True
        return sent

    def poll(self, now_ms: float) -> list[dict[str, Any]]:
        """Обслуживание отложенных ответов (порт Trigger_Poll)."""
        sent: list[dict[str, Any]] = []
        for i, pending in enumerate(self._pending):
            if not pending or now_ms < pending["fire_at"]:
                continue
            t = self._triggers[i]
            responses = self._send_response(t, i, pending["echo"], now_ms)
            if responses:
                self.fired_count += 1
            sent.extend(responses)
            if pending["remaining"] > 1:
                pending["remaining"] -= 1
                pending["fire_at"] = now_ms + pending["interval"]
            else:
                self._pending[i] = None
        return sent


def simulate(
    records: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    tail_ms: float = 2000.0,
) -> dict[str, Any]:
    """Прогоняет кадры через триггеры → журнал событий.

    frames: список dict {time_ms, channel(1-based, как в логе), id, data,
    extended, rtr}. Ответы триггеров возвращаются в поток как TX-эхо —
    так работают цепочки. Возвращает {events, fired_count, tx_frames}."""
    sim = TriggerSimulator(records)
    timeline = sorted(frames, key=lambda f: float(f.get("time_ms", 0)))
    end_ms = (float(timeline[-1]["time_ms"]) if timeline else 0.0) + tail_ms
    # Очередь: исходные кадры + порождённые TX-эхо по мере прогона.
    queue = list(timeline)
    tx_frames: list[dict[str, Any]] = []
    idx = 0
    while idx < len(queue):
        item = queue[idx]
        idx += 1
        t_ms = float(item["time_ms"])
        # Обслужить отложенные ответы до этого момента.
        for tx in sim.poll(t_ms):
            _record_tx(sim, tx, queue, tx_frames, end_ms)
        channel0 = int(item.get("channel", 1)) - 1  # лог 1-based → firmware 0-based
        frame = {
            "channel": channel0,
            "id": int(item["id"]),
            "extended": int(item.get("extended", 0)),
            "rtr": bool(item.get("rtr", False)),
            "dlc": int(item.get("dlc", len(item.get("data", b"")))),
            "data": bytes(item.get("data", b"")),
            "echo": int(item.get("echo", 0)),
        }
        sim.events.append({"time_ms": t_ms, "kind": "rx", "frame": frame})
        for tx in sim.on_frame(frame, t_ms):
            _record_tx(sim, tx, queue, tx_frames, end_ms)
    # Хвост: дослужить pending до конца окна.
    for tx in sim.poll(end_ms):
        _record_tx(sim, tx, queue, tx_frames, end_ms)
    sim.events.sort(key=lambda e: float(e["time_ms"]))
    return {"events": sim.events, "fired_count": sim.fired_count, "tx_frames": tx_frames}


def _record_tx(
    sim: TriggerSimulator,
    tx: dict[str, Any],
    queue: list[dict[str, Any]],
    tx_frames: list[dict[str, Any]],
    end_ms: float,
) -> None:
    """Отправленный кадр → журнал + возврат в поток как TX-эхо.

    Порт прошивки: собственная передача МК эхом попадает в приёмный
    поток и может матчить другие триггеры, пока глубина эха <
    CAN_TX_ECHO_MAX (иначе пинг-понг ушёл бы в бесконечность)."""
    tx_frames.append(tx)
    sim.events.append(
        {
            "time_ms": float(tx["time_ms"]),
            "kind": "tx",
            "trigger": tx.get("trigger_index"),
            "frame": tx,
        }
    )
    echo = int(tx.get("echo", 0)) + 1
    if echo < CAN_TX_ECHO_MAX and float(tx["time_ms"]) <= end_ms:
        queue.append(
            {
                "time_ms": float(tx["time_ms"]),
                "channel": int(tx["channel"]),
                "id": int(tx["id"]),
                "extended": int(tx.get("extended", 0)),
                "rtr": bool(tx.get("rtr", False)),
                "dlc": int(tx.get("dlc", len(tx.get("data", b"")))),
                "data": bytes(tx.get("data", b"")),
                "echo": echo,
            }
        )
        queue.sort(key=lambda f: float(f["time_ms"]))
