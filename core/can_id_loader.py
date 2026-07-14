"""Загрузчик офлайн-библиотеки CAN ID."""

import csv
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models.logger import get_logger
from models.utils import hex_to_int, int_to_hex

logger = get_logger(__name__)


class CanIdLoader:
    """Сканирует library/can_id и строит единый словарь CAN ID."""

    def __init__(self, root: Optional[Path] = None) -> None:
        if root is None:
            from models.utils import get_library_root
            root = get_library_root() / "can_id"
        self._roots = [root]
        # Fallback to bundled project-root library/can_id during development
        project_root = Path(__file__).resolve().parent.parent / "library" / "can_id"
        if project_root.exists() and project_root not in self._roots:
            self._roots.append(project_root)
        self._data: Dict[str, Dict[str, Dict[int, List[Dict[str, Any]]]]] = {}

    @property
    def data(self) -> Dict[str, Dict[str, Dict[int, List[Dict[str, Any]]]]]:
        return self._data

    def load(self) -> None:
        """Загружает все доступные источники."""
        self._data = {}
        for root in self._roots:
            if not root.exists():
                logger.warning("Папка библиотеки CAN ID не найдена: %s", root)
                continue
            for path in sorted(root.rglob("*")):
                if path.is_dir():
                    continue
                try:
                    if path.suffix.lower() == ".dbc":
                        self._load_dbc(path)
                    elif path.suffix.lower() == ".json":
                        self._load_json(path)
                    elif path.suffix.lower() == ".csv":
                        self._load_csv(path)
                    elif path.suffix.lower() == ".md":
                        self._load_md(path)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("Ошибка загрузки %s: %s", path, exc)

    def _ensure_make_model_year(self, make: str, model: str, year: int) -> None:
        make = make.strip()
        model = model.strip()
        if not make or not model:
            return
        self._data.setdefault(make, {}).setdefault(model, {}).setdefault(year, [])

    def _add(self, make: str, model: str, year: int, message: Dict[str, Any]) -> None:
        make = make.strip()
        model = model.strip()
        if not make or not model:
            return
        if not message.get("id"):
            return
        self._data.setdefault(make, {}).setdefault(model, {}).setdefault(year, []).append(message)

    def _parse_id(self, value: Any) -> Optional[int]:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            value = value.strip().replace("0x", "").replace("0X", "")
            if not value:
                return None
            try:
                return int(value, 16)
            except ValueError:
                try:
                    return int(value, 10)
                except ValueError:
                    return None
        return None

    def _parse_dlc(self, value: Any) -> int:
        if isinstance(value, int):
            return max(1, min(8, value))
        if isinstance(value, str):
            try:
                return max(1, min(8, int(value)))
            except ValueError:
                return 8
        return 8

    def _parse_bit(self, value: Any, can_id: int) -> int:
        if isinstance(value, int):
            return 1 if value == 1 else 0
        if isinstance(value, str):
            if "29" in value or "ext" in value.lower():
                return 1
            if "11" in value or "std" in value.lower():
                return 0
        return 1 if can_id > 0x7FF else 0

    def _build_message(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        can_id = self._parse_id(raw.get("id") or raw.get("can_id") or raw.get("canid") or raw.get("address"))
        if can_id is None:
            return None
        dlc = self._parse_dlc(raw.get("dlc") or raw.get("len") or raw.get("length"))
        bit = self._parse_bit(raw.get("bit") or raw.get("bitrate") or raw.get("bitness"), can_id)
        data = raw.get("data") or raw.get("payload") or raw.get("example") or ""
        if not isinstance(data, str):
            data = " ".join(f"{b:02X}" for b in data) if isinstance(data, (list, bytes, bytearray)) else str(data)
        return {
            "id": can_id,
            "bit": bit,
            "dlc": dlc,
            "name": str(raw.get("name") or raw.get("message") or "").strip(),
            "description": str(raw.get("description") or raw.get("comment") or raw.get("desc") or "").strip(),
            "data": data.strip(),
            "signals": raw.get("signals", []),
        }

    def _load_dbc(self, path: Path) -> None:
        try:
            import cantools
            db = cantools.db.load_file(str(path))
        except Exception as exc:  # noqa: BLE001
            logger.warning("cantools не смог загрузить %s: %s", path, exc)
            return
        make = path.parent.name or path.stem
        model = path.stem
        year = 0
        for msg in db.messages:
            can_id = msg.frame_id
            bit = 1 if can_id > 0x7FF else 0
            dlc = msg.length
            if dlc > 8:
                dlc = 8
            data = " ".join("00" for _ in range(dlc))
            self._add(make, model, year, {
                "id": can_id,
                "bit": bit,
                "dlc": dlc,
                "name": msg.name,
                "description": msg.comment or "",
                "data": data,
                "signals": [s.name for s in msg.signals],
            })

    def _load_json(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        try:
            payload = json.loads(text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("JSON parse error %s: %s", path, exc)
            return
        if isinstance(payload, list):
            for item in payload:
                self._load_json_item(item)
        elif isinstance(payload, dict):
            if any(isinstance(v, list) for v in payload.values()):
                self._load_json_item(payload)
            else:
                for make, models in payload.items():
                    self._load_json_item({"make": make, **(models if isinstance(models, dict) else {})}, path)

    def _load_json_item(self, raw: Dict[str, Any], path: Optional[Path] = None) -> None:
        make = raw.get("make") or raw.get("brand") or (path.stem if path else "Unknown")
        model = raw.get("model") or (path.stem if path else "Unknown")
        year = raw.get("year", 0)
        if isinstance(year, str):
            try:
                year = int(year)
            except ValueError:
                year = 0
        messages = raw.get("messages") or raw.get("ids") or raw.get("can_ids") or raw.get("data") or []
        if isinstance(messages, dict):
            for k, v in messages.items():
                if isinstance(v, dict):
                    v["id"] = v.get("id", k)
                    self._add(make, model, year, self._build_message(v))
                elif isinstance(v, list):
                    for m in v:
                        self._add(make, model, year, self._build_message(m))
        elif isinstance(messages, list):
            for m in messages:
                if isinstance(m, dict):
                    self._add(make, model, year, self._build_message(m))
                elif isinstance(m, int):
                    self._add(make, model, year, self._build_message({"id": m}))

    def _load_csv(self, path: Path) -> None:
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                make = row.get("make") or row.get("brand") or path.parent.name or path.stem
                model = row.get("model") or path.stem
                year = 0
                try:
                    year = int(row.get("year", 0))
                except ValueError:
                    year = 0
                msg = self._build_message(row)
                if msg is not None:
                    self._add(make, model, year, msg)

    def _load_md(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        if path.name.lower() == "readme.md":
            self._parse_readme_md(text)
            return
        self._parse_md_table(text, path)

    def _parse_readme_md(self, text: str) -> None:
        """Парсит README awesome-списка: заголовки make/model создают узлы дерева."""
        make = "Unknown"
        model = "Unknown"
        year = 0
        for line in text.splitlines():
            if line.startswith("## "):
                make = line[3:].strip()
                model = "Unknown"
                year = 0
                self._ensure_make_model_year(make, model, year)
                continue
            if line.startswith("### "):
                model = line[4:].strip()
                year = 0
                self._ensure_make_model_year(make, model, year)
                continue
        if make and model:
            logger.info("README.md parsed: %s / %s", make, model)

    def _parse_md_table(self, text: str, path: Path) -> None:
        """Парсит markdown-таблицы в файлах .md."""
        make = path.parent.name or path.stem
        model = path.stem
        year = 0
        headers: List[str] = []
        in_table = False
        for line in text.splitlines():
            if "|" in line:
                parts = [p.strip().lower() for p in line.split("|") if p.strip()]
                if not parts:
                    continue
                if not headers:
                    if any(h in parts for h in ("id", "can id", "description", "name", "data")):
                        headers = parts
                    continue
                if set(parts) <= {"-", ":"}:
                    continue
                if headers:
                    in_table = True
                    row = dict(zip(headers, parts))
                    make = row.get("make") or row.get("brand") or make
                    model = row.get("model") or model
                    try:
                        year = int(row.get("year", 0))
                    except ValueError:
                        year = 0
                    msg = self._build_message(row)
                    if msg is not None:
                        self._add(make, model, year, msg)
            else:
                headers = []
                in_table = False

    def demo_fallback(self) -> None:
        """Добавляет демо-записи, если библиотека пуста."""
        if self._data:
            return
        demo = {
            "Toyota": {
                "Camry": {
                    2018: [
                        {"id": 0x0C0, "bit": 0, "dlc": 8, "name": "EngineSpeed", "description": "Обороты двигателя", "data": "00 00 00 00 00 00 00 00", "signals": []},
                        {"id": 0x0B0, "bit": 0, "dlc": 8, "name": "VehicleSpeed", "description": "Скорость автомобиля", "data": "00 00 00 00 00 00 00 00", "signals": []},
                    ]
                }
            },
            "Kia": {
                "Rio": {
                    2019: [
                        {"id": 0x130, "bit": 0, "dlc": 8, "name": "DoorStatus", "description": "Статус дверей", "data": "00 00 00 00 00 00 00 00", "signals": []},
                        {"id": 0x3A0, "bit": 0, "dlc": 8, "name": "LightStatus", "description": "Статус освещения", "data": "00 00 00 00 00 00 00 00", "signals": []},
                    ]
                }
            },
        }
        for make, models in demo.items():
            for model, years in models.items():
                for year, messages in years.items():
                    for m in messages:
                        self._add(make, model, year, m)

    def get_tree(self) -> Dict[str, Dict[str, Dict[int, List[Dict[str, Any]]]]]:
        return self._data

    def make_flat(self) -> List[Dict[str, Any]]:
        """Возвращает плоский список всех сообщений с полями make/model/year."""
        result: List[Dict[str, Any]] = []
        for make, models in self._data.items():
            for model, years in models.items():
                for year, messages in years.items():
                    for m in messages:
                        item = dict(m)
                        item["make"] = make
                        item["model"] = model
                        item["year"] = year
                        result.append(item)
        return result
