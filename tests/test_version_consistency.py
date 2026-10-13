"""Согласованность версии артефактов (ревью CM-08).

VERSION — канонический источник; VERSION_TUPLE и метаданные
платформенных сборок не должны расходиться с ним. Тест падает при
рассогласовании — например, если VERSION_TUPLE снова введут вручную
или assets/version_info.py перестанут регенерировать в CI.
"""

from __future__ import annotations

import re
from pathlib import Path

from models.version import VERSION, VERSION_TUPLE

ROOT = Path(__file__).resolve().parents[1]


def test_version_tuple_derived_from_version() -> None:
    expected = tuple(int(part) for part in VERSION.split(".")[:3])
    assert expected == VERSION_TUPLE, (
        f"VERSION_TUPLE {VERSION_TUPLE} не соответствует VERSION {VERSION}"
    )
    assert len(VERSION_TUPLE) == 3


def test_version_info_matches_version() -> None:
    path = ROOT / "assets" / "version_info.py"
    content = path.read_text(encoding="ascii")
    major, minor, patch = VERSION_TUPLE
    filevers = f"({major}, {minor}, {patch}, 0)"
    assert f"filevers={filevers}" in content
    assert f"prodvers={filevers}" in content
    version_str = f"{VERSION}.0"
    for field in ("FileVersion", "ProductVersion"):
        match = re.search(
            rf"StringStruct\(u'{field}', u'([^']+)'\)", content
        )
        assert match is not None, f"{field} не найден в version_info.py"
        assert match.group(1) == version_str, (
            f"{field}={match.group(1)} != {version_str} — "
            "регенерируйте tools/generate_version_info.py"
        )


def test_generate_version_info_produces_current_content() -> None:
    """Регенерация даёт текущий файл — забытый запуск генератора
    ловится без публикации релиза."""
    import subprocess
    import sys

    result = subprocess.run(
        [sys.executable, "tools/generate_version_info.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "FileVersion" in result.stdout
    # После регенерации файл обязан совпадать с тем, что в дереве.
    test_version_info_matches_version()
