"""Управление настройками приложения «Код Мастер».

Настройки хранятся в формате JSON в файле config.json в пользовательской
папке данных (platformdirs.user_data_dir("CodeMaster"): ~/Library/
Application Support/CodeMaster на macOS, %LOCALAPPDATA%\\CodeMaster на
Windows, ~/.local/share/CodeMaster на Linux) — не рядом с приложением.
Класс Config реализован как синглтон, чтобы все части программы работали
с одним и тем же набором параметров.
"""

import json
import os
import struct
import threading
import time
import zlib
from copy import deepcopy
from pathlib import Path
from typing import Any

from platformdirs import user_data_dir

from models.logger import get_logger
from models.version import VERSION

logger = get_logger(__name__)

# Бинарный формат экспорта конфигурации (*.kmc):
#   magic 6Б | name_len 1Б | name | serial_len 1Б | serial |
#   payload_len u32 LE | payload (JSON utf-8) | crc32 u32 LE
# Имя и серийный номер вынесены в заголовок отдельно от payload — при
# загрузке программа сверяет их с подключённым устройством до применения
# настроек, даже если структура payload изменится между версиями.
CONFIG_FILE_MAGIC = b"KMCFG\x01"
CONFIG_FILE_FILTER = "CodeMaster config (*.kmc)"


def pack_config_file(data: dict, device_name: str, device_serial: str) -> bytes:
    """Упаковывает настройки в бинарный файл конфигурации."""
    payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
    name = str(device_name or "").encode("utf-8")[:255]
    serial = str(device_serial or "").encode("utf-8")[:255]
    body = (
        CONFIG_FILE_MAGIC
        + bytes((len(name),)) + name
        + bytes((len(serial),)) + serial
        + struct.pack("<I", len(payload))
        + payload
    )
    return body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)


def unpack_config_file(raw: bytes) -> "tuple[dict, str, str]":
    """Разбирает бинарный файл конфигурации → (payload, имя, серийник).

    Старые экспорты были чистым JSON без заголовка — их тоже принимаем,
    идентичность тогда достаётся из ключей самого payload.
    """
    if not raw.startswith(CONFIG_FILE_MAGIC):
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("файл конфигурации не содержит объект настроек")
        name = str(payload.get("device_name") or payload.get("device_type_name") or "")
        serial = str(payload.get("device_serial") or payload.get("serial_number") or "")
        return payload, name, serial

    pos = len(CONFIG_FILE_MAGIC)
    if len(raw) < pos + 1:
        raise ValueError("повреждённый файл конфигурации")
    name_len = raw[pos]
    pos += 1
    if len(raw) < pos + name_len + 1:
        raise ValueError("повреждённый файл конфигурации")
    name = raw[pos : pos + name_len].decode("utf-8", errors="replace")
    pos += name_len
    serial_len = raw[pos]
    pos += 1
    if len(raw) < pos + serial_len + 8:
        raise ValueError("повреждённый файл конфигурации")
    serial = raw[pos : pos + serial_len].decode("utf-8", errors="replace")
    pos += serial_len
    payload_len = struct.unpack_from("<I", raw, pos)[0]
    pos += 4
    if len(raw) < pos + payload_len + 4:
        raise ValueError("повреждённый файл конфигурации")
    payload_raw = raw[pos : pos + payload_len]
    pos += payload_len
    crc = struct.unpack_from("<I", raw, pos)[0]
    if (zlib.crc32(raw[:pos]) & 0xFFFFFFFF) != crc:
        raise ValueError("контрольная сумма файла конфигурации не сошлась")
    payload = json.loads(payload_raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("файл конфигурации не содержит объект настроек")
    return payload, name, serial


class Config:
    """Синглтон для хранения и автоматического сохранения настроек."""

    _instance: "Config | None" = None
    _lock: threading.Lock = threading.Lock()

    DEFAULT_CONFIG: dict[str, Any] = {
        "port": "",
        "baudrate": 115200,
        "emulation": False,
        "error_probability": 0,
        "triggers": [],
        "gateway_rules": [],
        "gateway_ignore": [],
        "ignore_list": [],
        "device_type": 0x00,
        "device_version": 0,
        "serial_number": "",
        "device_serial": "",
        "total_memory": 65536,
        "can1_speed": 500000,
        "can2_speed": 500000,
        "can_speed_auto": False,
        "can1_terminator": False,
        "can2_terminator": False,
        "target_mcu": "",
        "programmer_method": "uart",
        "sleep_time": 0,
        "sleep_mode": 0,
        "setup_completed": False,
        "theme": "dark",
        "last_config_dir": "",
    }

    def max_data_bytes(self) -> int:
        """Возвращает максимальную длину поля Data (8 байт для CAN 2.0)."""
        return 8

    def __new__(cls) -> "Config":
        """Создаёт или возвращает единственный экземпляр настроек."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        """Инициализирует путь к файлу конфигурации и загружает данные."""
        if self._initialized:
            return
        self._file_path = Path(user_data_dir("CodeMaster", appauthor=False, ensure_exists=True)) / "config.json"
        self._backup_dir = self._file_path.parent / "backups"
        self._data = deepcopy(self.DEFAULT_CONFIG)
        self._initialized = True
        self.load()

    # Ключи-зеркала программы, зашитой в МК: после обновления приложения
    # они протухшие (устройство могли перешить/сбросить) и не должны
    # подтягиваться в поля — их заполнит вычитка с МК. ЧЁРНЫЙ список, а
    # не белый: идентичность (device_name/device_serial/port_names) должна
    # пережить обновление — диалог прошивки предзаполняет из неё поля,
    # иначе config-страница прошивается с ПУСТЫМ именем и порт показывает
    # безликий «CodeMaster» вместо имени устройства.
    _VERSIONED_RESET_KEYS = (
        "triggers",
        "gateway_rules",
        "gateway_ignore",
        "ignore_list",
        "flexible_rules",
        "logic",
        "analog_ports",
        "can1_speed",
        "can2_speed",
        "can_speed_auto",
        "can1_terminator",
        "can2_terminator",
        "sleep_time",
        "sleep_mode",
    )

    def load(self) -> None:
        """Загружает настройки из config.json, если файл существует."""
        if self._file_path.exists():
            try:
                with self._file_path.open("r", encoding="utf-8") as file:
                    loaded = json.load(file)
            except (json.JSONDecodeError, OSError, TypeError) as exc:
                logger.error("Ошибка загрузки конфигурации: %s", exc)
                # Файл побился (обрыв записи, сбой диска) — поднимаем
                # последний снапшот из backups/, а не стартуем с нуля.
                loaded = self._load_latest_backup()
                if loaded is None:
                    return
            if not isinstance(loaded, dict):
                return
            if loaded.get("app_version") != VERSION:
                for key in self._VERSIONED_RESET_KEYS:
                    if key in self.DEFAULT_CONFIG:
                        loaded[key] = deepcopy(self.DEFAULT_CONFIG[key])
                    else:
                        loaded.pop(key, None)
                loaded["app_version"] = VERSION
                logger.info(
                    "Кэш программы устройства сброшен: записан версией %s, приложение %s",
                    loaded.get("app_version") or "<?>",
                    VERSION,
                )
                self._data.update(loaded)
                self.save()
                return
            self._data.update(loaded)

    def save(self) -> None:
        """Атомарно сохраняет текущие настройки в config.json.

        Пишем во временный файл и подменяем целевой через os.replace: при сбое
        посреди записи прежний config.json остаётся целым, а не обрезается.
        """
        tmp_path = self._file_path.with_name(self._file_path.name + ".tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as file:
                json.dump(self._data, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(tmp_path, self._file_path)
        except OSError as exc:
            logger.error("Ошибка сохранения конфигурации: %s", exc)
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    _BACKUP_KEEP = 10

    def backup_dir(self) -> Path:
        """Каталог снапшотов конфигурации (создаётся по требованию)."""
        self._backup_dir.mkdir(parents=True, exist_ok=True)
        return self._backup_dir

    def backup_snapshot(
        self,
        reason: str = "auto",
        data: "dict | None" = None,
        prefix: str = "config",
    ) -> "Path | None":
        """Снапшот настроек в backups/ перед разрушительной операцией.

        Вызывается перед загрузкой файла конфигурации, «Заводскими
        настройками» и записью в устройство — если сессия пошла не так,
        прежний конфиг всегда можно вернуть. Хранятся последние
        _BACKUP_KEEP снапшотов. data=None — снимок текущих настроек;
        можно передать другой dict (например, вычитку устройства —
        тогда prefix «device»)."""
        try:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            safe_reason = "".join(c if c.isalnum() or c in "-_" else "_" for c in reason)[:40]
            path = self.backup_dir() / f"{prefix}_{stamp}_{safe_reason}.json"
            payload = deepcopy(data) if data is not None else deepcopy(self._data)
            payload["_backup_reason"] = reason
            payload["_backup_time"] = stamp
            path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            # Ротация: старые снапшоты сверх лимита удаляем.
            snapshots = sorted(
                self._backup_dir.glob(f"{prefix}_*.json"),
                key=lambda p: p.name,
            )
            for old in snapshots[: max(0, len(snapshots) - self._BACKUP_KEEP)]:
                try:
                    old.unlink()
                except OSError:
                    pass
            return path
        except OSError as exc:
            logger.warning("Не удалось создать снапшот конфигурации: %s", exc)
            return None

    def _load_latest_backup(self) -> "dict | None":
        """Новейший снапшот из backups/ — fallback при битом config.json."""
        try:
            snapshots = sorted(self._backup_dir.glob("config_*.json"))
        except OSError:
            return None
        for path in reversed(snapshots):
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(loaded, dict):
                loaded.pop("_backup_reason", None)
                loaded.pop("_backup_time", None)
                logger.warning("config.json восстановлен из снапшота %s", path.name)
                return loaded
        return None

    def get(self, key: str, default: Any = None) -> Any:
        """Возвращает значение настройки по ключу."""
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Устанавливает значение настройки и сразу сохраняет его."""
        self._data[key] = value
        self.save()

    def set_bulk(self, values: dict[str, Any]) -> None:
        """Обновляет несколько настроек за раз и сохраняет их."""
        self._data.update(values)
        self.save()

    def all(self) -> dict[str, Any]:
        """Возвращает полную копию текущих настроек."""
        return deepcopy(self._data)

    def save_to_file(self, path: str) -> None:
        """Экспортирует настройки в бинарный файл конфигурации (*.kmc)."""
        name = self._data.get("device_name") or self._data.get("device_type_name") or ""
        serial = self._data.get("device_serial") or self._data.get("serial_number") or ""
        Path(path).write_bytes(pack_config_file(self._data, name, serial))

    def load_from_file(self, path: str) -> "tuple[str, str]":
        """Загружает файл конфигурации и возвращает (имя, серийник) из него.

        Идентичность текущего устройства сохраняется: файл может быть
        выгружен с другого экземпляра — его имя/серийник используются
        только для сверки, а не подменяют прошитые значения.
        """
        payload, _name, _serial = unpack_config_file(Path(path).read_bytes())
        self.import_data(payload)
        return _name, _serial

    def import_data(self, payload: dict) -> None:
        """Применяет настройки из payload, сохраняя идентичность устройства."""
        # Файл затирает текущие настройки — сначала снапшот на откат.
        self.backup_snapshot("before_import")
        for key in self._RESET_PRESERVE_KEYS:
            if key in self._data:
                payload[key] = deepcopy(self._data[key])
        self._data.update(payload)
        self.save()

    # Идентичность устройства и параметры связи «Заводские настройки»
    # не трогают: имя/серийник/тип прошиваются при программировании и не
    # являются настройкой оператора.
    _RESET_PRESERVE_KEYS = (
        "device_name",
        "device_type_name",
        "device_serial",
        "serial_number",
        "device_type",
        "device_version",
        "port_names",
        "total_memory",
        "port",
        "baudrate",
        "emulation",
    )

    def reset_to_defaults(self) -> None:
        """Сбрасывает настройки к значениям по умолчанию и сохраняет.

        Имя устройства, серийный номер и тип сохраняются — они заданы при
        программировании МК и не относятся к пользовательским настройкам.
        """
        self.backup_snapshot("before_factory_reset")
        preserved = {k: deepcopy(self._data[k]) for k in self._RESET_PRESERVE_KEYS if k in self._data}
        self._data = deepcopy(self.DEFAULT_CONFIG)
        self._data.update(preserved)
        self.save()
