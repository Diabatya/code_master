# Распиновка STM32F105RCT6 — «Код Мастер»

Источник: ТЗ раздел 2.4 (предоставлено заказчиком) + фактическая реализация в
`firmware/application/Inc/main.h`, `can_bridge.c`, `main.c`, `usbd_conf.c`,
`firmware/bootloader/`.

Корпус: **LQFP-64** (RCT6, 256 КБ Flash, 64 КБ RAM).

## 1. Занятые выводы (реализовано в прошивке)

| Вывод | Функция | Тип | Где в коде | Примечание |
|-------|---------|-----|------------|------------|
| PB8   | CAN1_RX | Input (AF remap) | `main.h`, `can_bridge.c` | `__HAL_AFIO_REMAP_CAN1_2()` |
| PB9   | CAN1_TX | AF push-pull | `main.h`, `can_bridge.c` | |
| PB7   | CAN1_RS (S) | Output | `main.h`, `CanBridge_SetTransceiverMode` | TJA1050: 0=Normal, 1=Silent |
| PB5   | CAN2_RX | Input (AF remap) | `main.h`, `can_bridge.c` | `__HAL_AFIO_REMAP_CAN2_ENABLE()` |
| PB6   | CAN2_TX | AF push-pull | `main.h`, `can_bridge.c` | |
| PB4   | CAN2_RS (S) | Output | `main.h` | TJA1050: 0=Normal, 1=Silent |
| PB3   | CAN1_TERM | Output | `main.h` | 1 = терминатор 120 Ом включён |
| PD2   | CAN2_TERM | Output | `main.h` | 1 = терминатор 120 Ом включён |
| PA9   | USB_VBUS | Input | `main.h` (`VBUS_SENSE`), `usbd_conf.c` | 5V-tolerant, без делителя |
| PA10  | USB_ID | Input | ТЗ; на плате подтяжка к GND | Прошивкой не инициализируется |
| PA11  | USB_DM | AF | `usbd_conf.c` | USB D− |
| PA12  | USB_DP | AF | `usbd_conf.c` | USB D+, подтяжка 1.5 кОм |
| PC10  | OUT1 | Output PP | `main.h`, `main.c` | Дискретный выход (резерв) |
| PC11  | OUT2 | Output PP | `main.h`, `main.c` | Дискретный выход (резерв) |
| PC12  | OUT3 | Output PP | `main.h`, `main.c` | Дискретный выход (резерв) |
| PA15  | OUT4 | Output PP | `main.h`, `main.c` | Ex-JTDI; JTAG отключён (`SWJ_NOJTAG`), только SWD |
| PC13  | LED | Output PP | `main.h`, `main.c`, bootloader | Статусный светодиод |
| PA13  | SWDIO | AF (отладка) | — | Отладка/программирование SWD |
| PA14  | SWCLK | AF (отладка) | — | Отладка/программирование SWD |
| PD0   | OSC_IN | — | `SystemClock_Config` (HSE) | Кварц HSE (8 МГц), PLL×9 → 72 МГц |
| PD1   | OSC_OUT | — | `SystemClock_Config` (HSE) | Кварц HSE |
| NRST  | Reset | — | — | Сброс |
| BOOT0 | Boot mode | — | — | 1 = ROM DFU / вход в загрузчик |
| PB2/BOOT1 | Boot mode | — | — | Требуется подтяжка к GND для загрузки из Flash |

## 2. Свободные выводы с аналоговыми возможностями

STM32F105 содержит **2 АЦП (ADC1/ADC2), 12 бит**; ЦАП (DAC) на этом МК **нет** —
аналоговый выход возможен только через **PWM (таймер) + RC-фильтр**.

Выводы ниже **не используются текущей прошивкой** и аппаратно поддерживают
каналы ADC1/ADC2 (аналоговый вход) и/или вывод каналов таймеров (PWM):

| Вывод | ADC-канал | Альтернативные функции (выборочно) |
|-------|-----------|------------------------------------|
| PA0   | ADC_IN0   | TIM2_CH1, TIM5_CH1, WKUP, USART2_CTS |
| PA1   | ADC_IN1   | TIM2_CH2, TIM5_CH2, USART2_RTS |
| PA2   | ADC_IN2   | TIM2_CH3, TIM5_CH3, USART2_TX |
| PA3   | ADC_IN3   | TIM2_CH4, TIM5_CH4, USART2_RX |
| PA4   | ADC_IN4   | SPI1_NSS, USART2_CK |
| PA5   | ADC_IN5   | SPI1_SCK |
| PA6   | ADC_IN6   | TIM3_CH1, SPI1_MISO |
| PA7   | ADC_IN7   | TIM3_CH2, SPI1_MOSI |
| PB0   | ADC_IN8   | TIM3_CH3 |
| PB1   | ADC_IN9   | TIM3_CH4 |
| PC0   | ADC_IN10  | — |
| PC1   | ADC_IN11  | — |
| PC2   | ADC_IN12  | — |
| PC3   | ADC_IN13  | — |
| PC4   | ADC_IN14  | — |
| PC5   | ADC_IN15  | — |

Итого свободных аналоговых входов: **16** (PA0–PA7, PB0–PB1, PC0–PC5).

## 3. Свободные выводы без аналоговых каналов (только цифра / PWM / AF)

| Вывод | Возможные функции | Примечание |
|-------|-------------------|------------|
| PA8   | TIM1_CH1, MCO, USART1_CK | PWM / вывод тактовой MCO |
| PB10  | I2C2_SCL, USART3_TX, TIM2_CH3 | |
| PB11  | I2C2_SDA, USART3_RX, TIM2_CH4 | |
| PB12  | SPI2_NSS, I2S2_WS, TIM1_BKIN | |
| PB13  | SPI2_SCK, I2S2_CK, TIM1_CH1N | |
| PB14  | SPI2_MISO, TIM1_CH2N | |
| PB15  | SPI2_MOSI, I2S2_SD, TIM1_CH3N | |
| PC6   | TIM3_CH1 (remap), I2S2_MCK | |
| PC7   | TIM3_CH2 (remap), I2S3_MCK | |
| PC8   | TIM3_CH3 (remap) | |
| PC9   | TIM3_CH4 (remap) | |
| PC14  | OSC32_IN / GPIO | Свободен только если нет кварца LSE 32.768 кГц |
| PC15  | OSC32_OUT / GPIO | Свободен только если нет кварца LSE 32.768 кГц |
| PB2   | GPIO | Условно: это BOOT1 — на время сброса должен быть в нужном состоянии |

## 4. Важные замечания

- **PA15/OUT4**: используется как GPIO только потому, что в `main.c` выполнено
  `__HAL_AFIO_REMAP_SWJ_NOJTAG()` — JTAG выключен, SWD (PA13/PA14) работает.
- **PB3/CAN1_TERM, PB4/CAN2_RS**: это бывшие JTDO/NJTRST — освобождены тем же
  отключением JTAG.
- «Аналоговый выход» на F105 реализуется как **PWM на канале таймера +
  внешний RC-фильтр** (выводы с TIMx_CHy из таблиц 2 и 3). Разрешение и частота
  зависят от выбранного таймера и настроек делителей.
- Пины ADC не 5V-толерантны: диапазон входа аналогового сигнала 0…VDDA
  (обычно 3.3 В). Для сигналов выше нужен делитель/защита.
- Перед перепрограммированием любого вывода сверяйтесь с принципиальной
  схемой платы: часть «свободных» пинов может быть заведена на пятаки,
  светодиоды, pull-up/pull-down или разъёмы.
