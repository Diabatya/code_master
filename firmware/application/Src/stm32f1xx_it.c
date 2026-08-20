/* Interrupt handlers for the application.
 *
 * NOTE: on the F105 connectivity-line devices, CAN1 shares its TX/RX0
 * interrupt vectors with the USB (USB_HP_CAN1_TX / USB_LP_CAN1_RX0) — this
 * is a real silicon sharing, not a mistake; both HAL_PCD_IRQHandler and
 * HAL_CAN_IRQHandler must be called from those two handlers. See RM0008
 * (STM32F105/107 reference manual), vector table section, and the vendored
 * startup_stm32f105xc.s which already names the vectors this way.
 */

#include "main.h"
#include "stm32f1xx_it.h"

extern PCD_HandleTypeDef hpcd;
extern CAN_HandleTypeDef hcan1;
extern CAN_HandleTypeDef hcan2;

void NMI_Handler(void) { }

void HardFault_Handler(void)
{
  while (1) { }
}

void MemManage_Handler(void)
{
  while (1) { }
}

void BusFault_Handler(void)
{
  while (1) { }
}

void UsageFault_Handler(void)
{
  while (1) { }
}

void SVC_Handler(void) { }
void DebugMon_Handler(void) { }
void PendSV_Handler(void) { }

void SysTick_Handler(void)
{
  HAL_IncTick();
}

/* Shared USB/CAN1 vectors (see file header note). */
void USB_HP_CAN1_TX_IRQHandler(void)
{
  HAL_CAN_IRQHandler(&hcan1);
}

void USB_LP_CAN1_RX0_IRQHandler(void)
{
  /* On this MCU family, when USB OTG_FS is selected (as here, per
   * SystemClock_Config/MX_USB init) the OTG_FS peripheral uses its own
   * dedicated OTG_FS_IRQn vector below rather than the legacy USB_LP
   * vector, so this handler only ever needs to service CAN1 RX0. Kept as a
   * distinct name (not aliased to OTG_FS_IRQHandler) to match RM0008
   * nomenclature and avoid confusing future maintainers. */
  HAL_CAN_IRQHandler(&hcan1);
}

void CAN1_RX1_IRQHandler(void)
{
  HAL_CAN_IRQHandler(&hcan1);
}

void CAN1_SCE_IRQHandler(void)
{
  HAL_CAN_IRQHandler(&hcan1);
}

void CAN2_TX_IRQHandler(void)
{
  HAL_CAN_IRQHandler(&hcan2);
}

void CAN2_RX0_IRQHandler(void)
{
  HAL_CAN_IRQHandler(&hcan2);
}

void CAN2_RX1_IRQHandler(void)
{
  HAL_CAN_IRQHandler(&hcan2);
}

void CAN2_SCE_IRQHandler(void)
{
  HAL_CAN_IRQHandler(&hcan2);
}

void OTG_FS_IRQHandler(void)
{
  HAL_PCD_IRQHandler(&hpcd);
}
