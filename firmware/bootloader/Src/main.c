/* Bootloader main for STM32F105RCT6 USB CDC bootloader. */

#include "main.h"
#include "usbd_core.h"
#include "usbd_cdc.h"
#include "usbd_cdc_if.h"
#include "usbd_desc.h"
#include "bootloader.h"
#include "event_log_bl.h"

USBD_HandleTypeDef hUsbDeviceFS;
static IWDG_HandleTypeDef hiwdg;

static void SystemClock_Config(void);
static void MX_GPIO_Init(void);
static void MX_IWDG_Init(void);

void Error_Handler(void)
{
  __disable_irq();
  while (1) {
  }
}

void App_KickWatchdog(void)
{
  HAL_IWDG_Refresh(&hiwdg);
}

int main(void)
{
  HAL_Init();
  SystemClock_Config();
  MX_GPIO_Init();
  MX_IWDG_Init();

  uint8_t stay_reason = Bootloader_ShouldStay();
  if (!stay_reason) {
    Bootloader_JumpToApplication(APP_START_ADDRESS);
  }

  /* Отметка в журнале приложения до поднятия USB: сеанс загрузчика иначе
   * остаётся слепым промежутком — «прошили, подключились, а лог пустой».
   * Append-only: стереть страницу журнала бутлоадер не может. */
  BlEventLog_NoteBoot(stay_reason);

  USBD_Init(&hUsbDeviceFS, &FS_Desc, 0);
  USBD_RegisterClass(&hUsbDeviceFS, USBD_CDC_CLASS);
  USBD_CDC_RegisterInterface(&hUsbDeviceFS, &USBD_CDC_fops);
  USBD_Start(&hUsbDeviceFS);

  while (1) {
    App_KickWatchdog();
    Bootloader_Task();
  }
}

static void MX_IWDG_Init(void)
{
  /* Аудит: раньше бутлоадер не имел watchdog вообще — зависший
   * USB-обмен или застрявшая операция Flash требовали ручного
   * отключения питания. Период выбран большим намеренно: ~26 с
   * (Prescaler=256, Reload=4095, LSI~40 кГц) — с большим запасом
   * покрывает самую длинную блокирующую операцию, mass-erase 112
   * страниц приложения (~4.5 с одним вызовом HAL_FLASHEx_Erase, внутри
   * которого кормить IWDG невозможно — вызов не возвращает управление
   * до завершения), и не должен ложно сработать во время обычного
   * протокола AN3155 (таймауты чтения байт — 200/1000 мс). Кормится
   * из главного цикла и явно вокруг erase (см. bootloader.c). */
  hiwdg.Instance = IWDG;
  hiwdg.Init.Prescaler = IWDG_PRESCALER_256;
  hiwdg.Init.Reload = 4095U;
  if (HAL_IWDG_Init(&hiwdg) != HAL_OK) {
    Error_Handler();
  }
}

static void MX_GPIO_Init(void)
{
  __HAL_RCC_GPIOA_CLK_ENABLE();
  __HAL_RCC_AFIO_CLK_ENABLE();

  GPIO_InitTypeDef GPIO_InitStruct = {0};

  /* LED on PC13 (common on Blue Pill style boards) - optional */
  __HAL_RCC_GPIOC_CLK_ENABLE();
  GPIO_InitStruct.Pin = GPIO_PIN_13;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOC, &GPIO_InitStruct);
  HAL_GPIO_WritePin(GPIOC, GPIO_PIN_13, GPIO_PIN_SET);
}

static void SystemClock_Config(void)
{
  RCC_OscInitTypeDef RCC_OscInitStruct = {0};
  RCC_ClkInitTypeDef RCC_ClkInitStruct = {0};
  RCC_PeriphCLKInitTypeDef PeriphClkInit = {0};

  /* HSE = 8 MHz, PREDIV1 = /1, PLL x9 => 72 MHz SYSCLK
   * USB OTG FS clock: (2 x PLLCLK) / 3 = 48 MHz */
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
  RCC_ClkInitStruct.APB1CLKDivider = RCC_HCLK_DIV2;
  RCC_ClkInitStruct.APB2CLKDivider = RCC_HCLK_DIV1;
  if (HAL_RCC_ClockConfig(&RCC_ClkInitStruct, FLASH_LATENCY_2) != HAL_OK) {
    Error_Handler();
  }
}
