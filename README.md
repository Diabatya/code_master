# 🛠️ Код Мастер (Code Master)

<p align="center">
  <img src="assets/icon.png" width="64" alt="Код Мастер">
</p>

<p align="center"><strong>Разработано «КОД МАСТЕР»</strong></p>

Десктопное приложение для прошивки микроконтроллеров **STM32** через UART-загрузчик и работы с **CAN-шиной** через UART-мост. Построено на **PySide6**, работает на Windows, macOS и Linux.

[![Build and Release](https://github.com/GrafKD/code_master/actions/workflows/build.yml/badge.svg)](https://github.com/GrafKD/code_master/actions/workflows/build.yml)
[![Release](https://img.shields.io/github/v/release/GrafKD/code_master)](https://github.com/GrafKD/code_master/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

---

## ✨ Возможности

- 🔥 **Прошивка STM32** — загрузка `.bin` и `.hex` файлов через UART bootloader (AN3155).
- 🚗 **CAN Тригеры** — автоматические ответы на заданные CAN-кадры с задержкой.
- 📊 **CAN Мониторинг** — просмотр трафика в реальном времени, фильтрация по ID, запись в CSV.
- 🔄 **CAN Шлюз** — перенаправление и подмена CAN-кадров между каналами.
- 🩺 **Диагностика bootloader** — чтение версии загрузчика и ID устройства.
- 📝 **Логирование** — сохранение всех событий и ошибок.
- 🌗 **Темы** — светлая и тёмная тема интерфейса.
- 🌍 **Локализация** — русский и английский язык.
- 🆕 **Проверка обновлений** — быстрая проверка новых версий на GitHub.
- 📦 **Портативный .exe** — один файл, без установки, запускается на Windows 10/11.
- � **macOS** — сборка в `.dmg` (скачал-запустил).
- 🐧 **Linux** — единый исполняемый файл для Ubuntu (без установки, без root).
- �📚 **Библиотека конфигураций** — офлайн-хранилище профилей `.cmm` с категориями, предпросмотром и импортом/экспортом.
- 📝 **Скриптовый редактор** — Python-скрипты с подсветкой синтаксиса и sandbox-исполнением для обработки CAN-кадров.
- 🔬 **Автоанализатор CAN-шины** — сбор статистики по ID, тепловая карта частот и экспорт в CSV/HTML.
- 📄 **Импорт DBC** — загрузка описаний CAN-сообщений и сигналов для отображения имён в мониторинге и графике.

---

## 🚀 Быстрый старт

### Для пользователей (Windows)

1. Открой страницу [Releases](https://github.com/GrafKD/code_master/releases).
2. Скачай последний релиз (например, `Release v1.0.0` или `Nightly build ...`).
3. Распакуй архив и запусти `CodeMaster.exe`.
4. Готово — приложение не требует установки и прав администратора.

### Для разработчиков

```bash
git clone https://github.com/GrafKD/code_master.git
cd code_master
pip install -r requirements.txt
python main.py
```

---

## 🖼️ Скриншот интерфейса

> Скриншот будет добавлен позже.

---

## 🧩 Командная строка

Приложение поддерживает прошивку из командной строки:

```bash
python main.py --firmware firmware.bin --port COM3 --baudrate 115200
```

Полный список параметров:

```bash
python main.py --help
```

---

## 📖 Документация

Подробное руководство по использованию — см. [`USER_GUIDE.md`](USER_GUIDE.md).

Инструкция по сборке из исходников также описана в [`USER_GUIDE.md`](USER_GUIDE.md#-сборка-из-исходников).

---

## 🏗️ Сборка из исходников

Для сборки портативного `.exe` под Windows используется **PyInstaller** и **GitHub Actions**. При каждом пуше в `main` GitHub Actions автоматически собирает `.exe` и публикует его в Releases.

Для ручной сборки:

```bash
pip install pyinstaller
pyinstaller build_win.spec
```

Готовый файл появится в папке `dist/`.

---

## 📄 Лицензия

Проект распространяется под лицензией **MIT**. Подробнее — см. файл [`LICENSE`](LICENSE).

---

## 🙏 Благодарности

Проект использует открытые библиотеки, инструменты и CAN-данные, включая [OpenDBC](https://github.com/commaai/opendbc). Полный список с лицензиями — в файле [NOTICE.md](NOTICE.md).

## 🤝 Контакты

Если у вас есть вопросы или предложения, создайте [Issue](https://github.com/GrafKD/code_master/issues) в репозитории.

---

_Сделано с 💙 для работы с STM32 и CAN-шиной._
