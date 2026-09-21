/* Main application for STM32F105RCT6 CAN gateway + trigger device.
 *
 * Responsibilities (see firmware/application/README.md for the full
 * pinout-assumption list and firmware/PROTOCOL.md for the wire protocol):
 *   - system clock (reused verbatim from firmware/bootloader/Src/main.c,
 *     since no schematic exists to justify a different HSE/PLL config)
 *   - GPIO init for CAN transceivers, OUT1-4, VBUS sense, LED
 *   - IWDG watchdog (~1s, ТЗ 12.5)
 *   - VBUS-based standalone-mode check (ТЗ 12.4): USB is only started when
 *     VBUS is present; CAN + triggers always run regardless
 *   - main loop: service IWDG, poll USB-CDC protocol, poll trigger timers
 *
 * NOTE: JTAG is disabled at boot (SWD-only debug, ТЗ 13) so PA15/PA13/PA14
 * are free for GPIO/SWD use per main.h's pin assignments.
 */

#include "main.h"
#include "usbd_core.h"
#include "usbd_cdc.h"
#include "usbd_cdc_if.h"
#include "usbd_desc.h"
#include "can_bridge.h"
#include "device_config.h"
#include "trigger.h"
#include "event_log.h"
#include "protocol.h"

#define APP_DEVICE_TYPE     0x00U /* DEVICE_TYPE_BASIC, see PROTOCOL.md 1.2 */
#define APP_DEVICE_VERSION  0x03U /* v0.3 — bump manually on release */

/* Per-channel CAN bit rates in kbit/s. Boot-time values come from the
 * persisted device config (DeviceConfig_GetCanBaud); the operator can
 * change them at runtime via CMD_CAN_SPEED, which also persists them so
 * autonomous trigger/gateway operation keeps the configured rate after
 * power cycles. Default/fallback is 500 kbit/s — see protocol.c's
 * CMD_AUTO_SPEED note and firmware/FIRMWARE_INPUT_REQUEST.md "CAN
 * auto-baud detection parameters" (ТЗ 12.1 worst-case: dual-bus @ 500k). */
uint32_t g_can_baud_kbps[2] = { 500U, 500U };

/* Аудит: два CAN-кольца (CAN_RING_DEPTH=1024 x2, can_bridge.c) занимают
 * ~35 КБ из 64 КБ RAM — между концом .bss и вершиной стека остаётся
 * всего ~5 КБ на стек+куча, а на этом Cortex-M3 без RTOS все прерывания
 * исполняются на ОДНОМ стеке с главным циклом (нет отдельного PSP).
 * Тихое переполнение стека (MPU не настроен) портило бы соседние
 * статические данные без гарантированного HardFault — ровно то, что
 * могло бы выглядеть как необъяснимое зависание/потеря связи. Вместо
 * слепого урезания буферов (регрессия по ТЗ 12.2) — измеримая защёлка:
 * область от конца .bss до текущего SP на старте закрашивается меткой,
 * `App_GetStackFreeBytes()` потом отдаёт, сколько байт метки НЕ было
 * тронуто ни разу — т.е. худший зафиксированный запас стека с момента
 * старта. Видно через CMD_SYSTEM_INFO. */
extern uint32_t _ebss;
extern uint32_t _estack;
#define STACK_CANARY_PATTERN 0xC5C5C5C5UL
#define STACK_CANARY_MARGIN  64U /* не трогаем текущий кадр вызова */

USBD_HandleTypeDef hUsbDeviceFS;
static IWDG_HandleTypeDef hiwdg;
/* Причина последнего сброса — RCC->CSR[31:24], сохранённый загрузчиком в
 * BKP->DR2 до очистки RMVF (само приложение видит CSR уже обнулённым).
 * 0 = загрузчик старый/не записал либо полный сброс backup-домена. */
static uint8_t s_reset_flags;
/* Код фолта, записанный обработчиком в BKP->DR3 перед зависанием —
 * пережил IWDG-ресет и доехал до этого старта. 0 = фолта не было. */
static uint8_t s_fault_code;

static void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_IWDG_Init(void);
static uint8_t VBUS_Present(void);
static void JTAG_Disable_SWD_Only(void);

void Error_Handler(void)
{
  __disable_irq();
  while (1) {
  }
}

/* Закрашивает свободное ОЗУ меткой на самом раннем этапе — до неё уже
 * успели исполниться HAL_Init() и пролог main(), так что первые
 * несколько кадров вызовов в измерение не попадают (небольшая, но
 * непринципиальная погрешность в сторону оптимистичной оценки). Не
 * трогает STACK_CANARY_MARGIN байт непосредственно под текущим SP,
 * чтобы не закрасить сам активный кадр этой функции. */
static void paint_stack_canary(void)
{
  register uint32_t sp;
  __asm volatile ("mov %0, sp" : "=r" (sp));
  uint32_t *p = &_ebss;
  uint32_t *limit = (uint32_t *)(sp - STACK_CANARY_MARGIN);
  while (p < limit) {
    *p++ = STACK_CANARY_PATTERN;
  }
}

/* Сколько байт метки, закрашенной paint_stack_canary(), НЕ было тронуто
 * ни разу с момента старта — т.е. худший зафиксированный запас между
 * концом .bss и фактическим пиком использования стека (включая пики
 * внутри вложенных прерываний — на этом MCU они делят стек с main()).
 * Не абсолютная гарантия (стек мог кратковременно зайти глубже и
 * вернуться, не тронув конкретно эту метку, если использованные там
 * байты случайно совпали с самим паттерном), но для полевой
 * диагностики достаточно — три случайных 0xC5C5C5C5 подряд статистически
 * исключены. */
uint32_t App_GetStackFreeBytes(void)
{
  const uint32_t *p = &_ebss;
  const uint32_t *stack_top = &_estack;
  while (p < stack_top && *p == STACK_CANARY_PATTERN) {
    p++;
  }
  return (uint32_t)((const uint8_t *)p - (const uint8_t *)&_ebss);
}

int main(void)
{
  HAL_Init();
  paint_stack_canary();
  /* Читаем до любого возможного сброса backup-домена дальше по коду. */
  __HAL_RCC_PWR_CLK_ENABLE();
  __HAL_RCC_BKP_CLK_ENABLE();
  s_reset_flags = (uint8_t)(BKP->DR2 & 0xFFU);
  s_fault_code = (uint8_t)(BKP->DR3 & 0xFFU);
  if (s_fault_code != 0U) {
    PWR->CR |= PWR_CR_DBP;
    BKP->DR3 = 0U; /* одноразовый маркер — потреблён */
  }
  SystemClock_Config();
  JTAG_Disable_SWD_Only();
  MX_GPIO_Init();
  MX_IWDG_Init();

  /* Config/triggers must be loaded before USB starts, so the very first
   * enumeration already reports the Flash-configured name/serial
   * (usbd_desc.c reads DeviceConfig_Get()). */
  DeviceConfig_Init();
  Trigger_Init();
  /* После MX_IWDG_Init (см. выше) — EventLog_Init() может стирать/писать
   * Flash (первый старт на этой странице после обновления прошивки), а
   * IWDG уже должен быть настроен на случай долгого стирания. */
  EventLog_Init();

  /* CAN + triggers must run standalone even with USB deactivated (ТЗ
   * 12.4), so bring the CAN bridge up unconditionally, before deciding
   * whether to start USB at all. Per-channel bit rates come from the
   * persisted config — an operator-set 250 kbit/s bus must already run at
   * 250 kbit/s at power-on, before any PC session. */
  g_can_baud_kbps[0] = DeviceConfig_GetCanBaud(0);
  g_can_baud_kbps[1] = DeviceConfig_GetCanBaud(1);
  if (!CanBridge_Init(g_can_baud_kbps[0], g_can_baud_kbps[1])) {
    /* Keep USB/application diagnostics available even when the board's CAN
     * transceiver, pinout or termination prevents CAN initialization. */
  }

  Protocol_Init(APP_DEVICE_TYPE, APP_DEVICE_VERSION);

  uint8_t usb_active = 0U;
  if (!APPLICATION_USE_VBUS_SENSE || VBUS_Present()) {
    USBD_Init(&hUsbDeviceFS, &FS_Desc, 0);
    USBD_RegisterClass(&hUsbDeviceFS, USBD_CDC_CLASS);
    USBD_CDC_RegisterInterface(&hUsbDeviceFS, &USBD_CDC_fops);
    USBD_Start(&hUsbDeviceFS);
    usb_active = 1U;
  }

  uint8_t last_usb_reset_count = CDC_GetUsbResetCount();
  uint8_t last_usb_disconnect_count = CDC_GetUsbDisconnectCount();
  uint32_t last_rx_overflow_bytes = CDC_GetRxOverflowCount();

  while (1) {
    HAL_IWDG_Refresh(&hiwdg);

    /* USB reset/disconnect считаются из IRQ (HAL_PCD_Reset/DisconnectCallback
     * в usbd_conf.c, см. CDC_NoteUsbEvent) — Flash-запись только отсюда,
     * из главного цикла, по дельте счётчика. */
    uint8_t usb_reset_now = CDC_GetUsbResetCount();
    if (usb_reset_now != last_usb_reset_count) {
      EventLog_Add((uint8_t)EVLOG_USB_RESET, 0xFFU, usb_reset_now);
      last_usb_reset_count = usb_reset_now;
    }
    uint8_t usb_disconnect_now = CDC_GetUsbDisconnectCount();
    if (usb_disconnect_now != last_usb_disconnect_count) {
      EventLog_Add((uint8_t)EVLOG_USB_DISCONNECT, 0xFFU, usb_disconnect_now);
      last_usb_disconnect_count = usb_disconnect_now;
    }
    /* Программный RX FIFO (usbd_cdc_if.c, 2 КБ) переполнился — главный
     * цикл не успевал вычитывать поток команд/кадров. code — байт со
     * знаком «было» (насыщение на 0xFF), точное число уже отдаёт
     * CMD_SYSTEM_INFO/rx_overflow_bytes; здесь важен факт и момент. */
    uint32_t rx_overflow_now = CDC_GetRxOverflowCount();
    if (rx_overflow_now != last_rx_overflow_bytes) {
      uint32_t delta = rx_overflow_now - last_rx_overflow_bytes;
      EventLog_Add((uint8_t)EVLOG_USB_RX_OVERFLOW, 0xFFU,
                   (delta > 0xFFU) ? 0xFFU : (uint8_t)delta);
      last_rx_overflow_bytes = rx_overflow_now;
    }

    uint8_t vbus_now = VBUS_Present();
    if (APPLICATION_USE_VBUS_SENSE && vbus_now && !usb_active) {
      /* USB plugged in while running standalone: start the stack now
       * rather than requiring a reboot. */
      USBD_Init(&hUsbDeviceFS, &FS_Desc, 0);
      USBD_RegisterClass(&hUsbDeviceFS, USBD_CDC_CLASS);
      USBD_CDC_RegisterInterface(&hUsbDeviceFS, &USBD_CDC_fops);
      USBD_Start(&hUsbDeviceFS);
      usb_active = 1U;
    } else if (APPLICATION_USE_VBUS_SENSE && !vbus_now && usb_active) {
      /* USB removed: deactivate the stack, keep CAN+triggers running
       * (ТЗ 12.4 — "device must continue CAN+triggers standalone with USB
       * deactivated"). */
      USBD_Stop(&hUsbDeviceFS);
      USBD_DeInit(&hUsbDeviceFS);
      usb_active = 0U;
    }

    /* Trigger response scheduling first (bounded, cheap), then protocol
     * I/O (also bounded per call, see protocol.c::Protocol_Poll). Both are
     * non-blocking with respect to CAN reception, which happens in the
     * CAN1/CAN2 RX IRQ handlers regardless of what the main loop is doing
     * (ТЗ 12.3). */
    CanBridge_PollHealth();
    Trigger_Poll();
    if (usb_active) {
      Protocol_Poll();
    } else {
      /* Standalone mode: still drain CAN ring buffers into the trigger
       * engine (so triggers keep firing) even though there is no USB link
       * to forward frames over. */
      can_frame_t frame;
      for (uint8_t channel = 0; channel < 2U; channel++) {
        while (CanBridge_PopRx(channel, &frame)) {
          Trigger_OnFrame(&frame);
        }
      }
    }
  }
}

static uint8_t VBUS_Present(void)
{
  return (HAL_GPIO_ReadPin(VBUS_SENSE_PORT, VBUS_SENSE_PIN) == GPIO_PIN_SET) ? 1U : 0U;
}

static void JTAG_Disable_SWD_Only(void)
{
  /* ТЗ 13: SWD-only debug, JTAG disabled, PA15/JTDI repurposed as GPIO
   * OUT4. AFIO remap bits: SWJ_CFG = 0b010 (JTAG-DP disabled, SW-DP
   * enabled) frees PA15 (JTDI), PB3 (JTDO), PB4 (JTRST) for GPIO use while
   * keeping PA13 (SWDIO) and PA14 (SWCLK) for debugging. */
  __HAL_RCC_AFIO_CLK_ENABLE();
  __HAL_AFIO_REMAP_SWJ_NOJTAG();
}

static void MX_GPIO_Init(void)
{
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_GPIOC_CLK_ENABLE();

  GPIO_InitTypeDef gpio = {0};

  /* Status LED, reused from bootloader (PC13). */
  gpio.Pin = LED_PIN;
  gpio.Mode = GPIO_MODE_OUTPUT_PP;
  gpio.Pull = GPIO_NOPULL;
  gpio.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(LED_PORT, &gpio);
  HAL_GPIO_WritePin(LED_PORT, LED_PIN, GPIO_PIN_SET);

  /* VBUS sense input (also read by USB PCD MSP in usbd_conf.c for its own
   * vbus_sensing_enable path; re-initializing as input here is harmless
   * and keeps this file self-sufficient if usbd_conf.c's init order ever
   * changes). */
  gpio.Pin = VBUS_SENSE_PIN;
  gpio.Mode = GPIO_MODE_INPUT;
  gpio.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(VBUS_SENSE_PORT, &gpio);

  /* General-purpose outputs OUT1-3 (PC10/PC11/PC12 per ТЗ 2.4), default LOW. */
  gpio.Mode = GPIO_MODE_OUTPUT_PP;
  gpio.Pull = GPIO_NOPULL;
  gpio.Speed = GPIO_SPEED_FREQ_LOW;

  gpio.Pin = OUT1_PIN;
  HAL_GPIO_Init(OUT1_PORT, &gpio);
  HAL_GPIO_WritePin(OUT1_PORT, OUT1_PIN, GPIO_PIN_RESET);

  gpio.Pin = OUT2_PIN;
  HAL_GPIO_Init(OUT2_PORT, &gpio);
  HAL_GPIO_WritePin(OUT2_PORT, OUT2_PIN, GPIO_PIN_RESET);

  gpio.Pin = OUT3_PIN;
  HAL_GPIO_Init(OUT3_PORT, &gpio);
  HAL_GPIO_WritePin(OUT3_PORT, OUT3_PIN, GPIO_PIN_RESET);

  /* OUT4 = PA15 (ex-JTDI), only usable as GPIO after JTAG_Disable_SWD_Only()
   * has run (must be called before this function — see main()). */
  gpio.Pin = OUT4_PIN;
  HAL_GPIO_Init(OUT4_PORT, &gpio);
  HAL_GPIO_WritePin(OUT4_PORT, OUT4_PIN, GPIO_PIN_RESET);

  /* CAN pins, transceiver control, USB D+/D- are initialized by
   * CanBridge_Init() / HAL_PCD_MspInit() respectively. */
}

uint8_t App_GetResetFlags(void)
{
  return s_reset_flags;
}

uint8_t App_GetFaultCode(void)
{
  return s_fault_code;
}

void App_NoteFault(uint8_t code)
{
  /* Вызывается из fault-хендлеров: только регистровые записи, без HAL
   * и без разблокировки Flash — минимум кода в контексте краха. */
  __HAL_RCC_PWR_CLK_ENABLE();
  __HAL_RCC_BKP_CLK_ENABLE();
  PWR->CR |= PWR_CR_DBP;
  BKP->DR3 = code;
}

void App_KickWatchdog(void)
{
  /* Дополнительное кормление из ограниченных циклов ожидания (USB TX
   * занят, стирание/запись Flash): ожидание ограничено таймаутом и само
   * завершится, но суммарно могло перешагнуть ~1-секундный период IWDG —
   * устройство уходило в reset и роняло USB-порт посреди серии команд
   * (полевой лог: ClearCommError PermissionError → ре-энумерация →
   * «Таймаут записи команды»). */
  HAL_IWDG_Refresh(&hiwdg);
}

static void MX_IWDG_Init(void)
{
  /* ТЗ 12.5: IWDG active, ~1s period, resets on main-loop hang. IWDG
   * clocks from the ~40kHz LSI; Prescaler=32, Reload=1249 gives
   * (32 * 1250) / 40000 ~= 1.0s timeout. */
  hiwdg.Instance = IWDG;
  hiwdg.Init.Prescaler = IWDG_PRESCALER_32;
  hiwdg.Init.Reload = 1249U;
  if (HAL_IWDG_Init(&hiwdg) != HAL_OK) {
    Error_Handler();
  }
}

static void SystemClock_Config(void)
{
  /* Identical to firmware/bootloader/Src/main.c::SystemClock_Config() —
   * reused verbatim since no schematic exists to justify a different
   * HSE/PLL configuration, and the application must produce the same
   * 72 MHz SYSCLK / 48 MHz USB clock / 36 MHz APB1 (used for CAN bit
   * timing in can_bridge.c) as the bootloader it hands off to/from. */
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};
  RCC_PeriphCLKInitTypeDef PeriphClkInit = {0};

  RCC_OscInitStruct.OscillatorType = RCC_OSCILLATORTYPE_HSE;
  RCC_OscInitStruct.HSEState = RCC_HSE_ON;
  RCC_OscInitStruct.HSEPredivValue = RCC_HSE_PREDIV_DIV1;
  RCC_OscInitStruct.Prediv1Source = RCC_PREDIV1_SOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLState = RCC_PLL_ON;
  RCC_OscInitStruct.PLL.PLLSource = RCC_PLLSOURCE_HSE;
  RCC_OscInitStruct.PLL.PLLMUL = RCC_PLL_MUL9;
  if (HAL_RCC_OscConfig(&RCC_OscInitStruct) != HAL_OK) {
    Error_Handler();
  }

  PeriphClkInit.PeriphClockSelection = RCC_PERIPHCLK_USB;
  PeriphClkInit.UsbClockSelection = RCC_USBCLKSOURCE_PLL_DIV3;
  if (HAL_RCCEx_PeriphCLKConfig(&PeriphClkInit) != HAL_OK) {
    Error_Handler();
  }

  RCC_ClkInitStruct.ClockType = RCC_CLOCKTYPE_HCLK | RCC_CLOCKTYPE_SYSCLK |
                                RCC_CLOCKTYPE_PCLK1 | RCC_CLOCKTYPE_PCLK2;
  RCC_ClkInitStruct.SYSCLKSource = RCC_SYSCLKSOURCE_PLLCLK;
  RCC_ClkInitStruct.AHBCLKDivider = RCC_SYSCLK_DIV1;
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV2; /* APB1 = 36 MHz, used by CAN bit timing */
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;
  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK) {
    Error_Handler();
  }
}
