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
#include "protocol.h"

#define APP_DEVICE_TYPE     0x00U /* DEVICE_TYPE_BASIC, see PROTOCOL.md 1.2 */
#define APP_DEVICE_VERSION  0x01U /* v0.1 — bump manually on release */

/* Default/fallback CAN bit rate used until a real auto-baud sweep or a
 * config command sets otherwise (see protocol.c's CMD_AUTO_SPEED note and
 * firmware/FIRMWARE_INPUT_REQUEST.md "CAN auto-baud detection parameters",
 * still unresolved without hardware to validate against). 500 kbit/s
 * matches the ТЗ 12.1 worst-case load example (100% dual-bus @ 500kbit/s). */
uint32_t g_can_baud_kbps = 500U;

USBD_HandleTypeDef hUsbDeviceFS;
static IWDG_HandleTypeDef hiwdg;

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

int main(void)
{
  HAL_Init();
  SystemClock_Config();
  JTAG_Disable_SWD_Only();
  MX_GPIO_Init();
  MX_IWDG_Init();

  /* Config/triggers must be loaded before USB starts, so the very first
   * enumeration already reports the Flash-configured name/serial
   * (usbd_desc.c reads DeviceConfig_Get()). */
  DeviceConfig_Init();
  Trigger_Init();

  /* CAN + triggers must run standalone even with USB deactivated (ТЗ
   * 12.4), so bring the CAN bridge up unconditionally, before deciding
   * whether to start USB at all. */
  if (!CanBridge_Init(g_can_baud_kbps)) {
    Error_Handler();
  }

  Protocol_Init(APP_DEVICE_TYPE, APP_DEVICE_VERSION);

  uint8_t usb_active = 0U;
  if (VBUS_Present()) {
    USBD_Init(&hUsbDeviceFS, &FS_Desc, 0);
    USBD_RegisterClass(&hUsbDeviceFS, USBD_CDC_CLASS);
    USBD_CDC_RegisterInterface(&hUsbDeviceFS, &USBD_CDC_fops);
    USBD_Start(&hUsbDeviceFS);
    usb_active = 1U;
  }

  while (1) {
    HAL_IWDG_Refresh(&hiwdg);

    uint8_t vbus_now = VBUS_Present();
    if (vbus_now && !usb_active) {
      /* USB plugged in while running standalone: start the stack now
       * rather than requiring a reboot. */
      USBD_Init(&hUsbDeviceFS, &FS_Desc, 0);
      USBD_RegisterClass(&hUsbDeviceFS, USBD_CDC_CLASS);
      USBD_CDC_RegisterInterface(&hUsbDeviceFS, &USBD_CDC_fops);
      USBD_Start(&hUsbDeviceFS);
      usb_active = 1U;
    } else if (!vbus_now && usb_active) {
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

  /* General-purpose outputs OUT1-3 (PC2-4), default LOW. */
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
