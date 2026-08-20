"""Справочные данные о моделях STM32, используемых в прошивке."""

from typing import Dict

# Модель → размер Flash в КБ
STM32_FLASH_SIZES: Dict[str, int] = {
    "STM32F103C8T6": 64,
    "STM32F103RBT6": 128,
    "STM32F105RCT6": 256,
    "STM32F105VCT6": 256,
    "STM32F107VCT6": 256,
    "STM32F205RGT6": 1024,
    "STM32F303CCT6": 256,
    "STM32F407VGT6": 1024,
    "STM32F429ZIT6": 2048,
    "STM32F446RET6": 512,
    "STM32F746ZGT6": 1024,
}

# Модель → размер страницы flash в байтах. Для F1 — 1/2 КБ, F2/F4 — сектора 16+ КБ.
STM32_PAGE_SIZES: Dict[str, int] = {
    "STM32F103C8T6": 1024,
    "STM32F103RBT6": 1024,
    "STM32F105RCT6": 2048,
    "STM32F105VCT6": 2048,
    "STM32F107VCT6": 2048,
    "STM32F205RGT6": 16384,
    "STM32F303CCT6": 2048,
    "STM32F407VGT6": 16384,
    "STM32F429ZIT6": 16384,
    "STM32F446RET6": 16384,
    "STM32F746ZGT6": 16384,
}

# ST-LINK Device ID (например, 0x418) → модель по datasheet
DEVICE_ID_TO_MODEL: Dict[str, str] = {
    "0x410": "STM32F103RBT6",
    "0x412": "STM32F103C8T6",
    "0x413": "STM32F407VGT6",
    "0x414": "STM32F105RCT6",
    "0x418": "STM32F105VCT6",
    "0x421": "STM32F446RET6",
    "0x422": "STM32F303CCT6",
    "0x434": "STM32F429ZIT6",
    "0x440": "STM32F107VCT6",
    "0x449": "STM32F746ZGT6",
    "0x411": "STM32F205RGT6",
}

# Chip ID (из Get ID) → размер Flash в КБ (как строка, т.к. у некоторых чипов диапазон)
CHIP_FLASH_SIZE_KB: Dict[int, str] = {
    0x412: "64/128",
    0x410: "128/256",
    0x414: "256/512",
    0x418: "64/128",
    0x420: "128/256",
    0x430: "1024",
    0x431: "256/512",
    0x432: "512/1024",
    0x433: "1024",
    0x440: "1024",
    0x441: "2048",
    0x442: "512/1024",
    0x444: "512/1024",
    0x445: "1024",
    0x448: "1024",
    0x449: "2048",
    0x450: "1024",
    0x451: "2048",
}
