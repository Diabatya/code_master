"""Общие гарантии изоляции для всего suite (CM-FR01).

Глобальный язык интерфейса живёт в синглтоне models.translations:
любой тест (или импортируемый при сборке модуль), переключивший его,
без возврата ломает все проверки русских строк ниже по прогону.
Фикстура прибивает язык к дефолтному «ru» на время теста и
возвращает значение, которое было до него.
"""

from __future__ import annotations

import pytest

from models.translations import get_language, set_language


@pytest.fixture(autouse=True)
def _isolate_ui_language():
    """На время теста язык — «ru» (дефолт приложения), после —
    прежнее значение. Тестам, проверяющим английскую локаль,
    достаточно вызвать set_language("en") внутри себя."""
    previous = get_language()
    set_language("ru")
    yield
    set_language(previous)
