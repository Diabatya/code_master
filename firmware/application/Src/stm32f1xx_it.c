/* Interrupt handlers for the application.
 *
 * CORRECTION (audit finding): an earlier version of this note claimed
 * CAN1 "shares" its TX/RX0 vectors with USB on the F105 and required
 * HAL_PCD_IRQHandler to be called from USB_HP_CAN1_TX/USB_LP_CAN1_RX0.
 * That is NOT the case for this board: the legacy names
 * USB_HP_CAN1_TX_IRQn/USB_LP_CAN1_RX0_IRQn are just CMSIS aliases for
 * CAN1_TX_IRQn/CAN1_RX0_IRQn (see stm32f105xc.h: "#define
 * USB_LP_CAN1_RX0_IRQn CAN1_RX0_IRQn") — they exist for source
 * compatibility with F103-style code where the legacy USB device
 * peripheral truly does share those vectors with CAN1. This firmware
 * uses the F105/F107 OTG_FS peripheral instead, which has its own,
 * entirely separate OTG_FS_IRQn (67) — see usbd_conf.c's
 * HAL_NVIC_EnableIRQ(OTG_FS_IRQn). So on THIS hardware there is no real
 * vector sharing: CAN1_RX0 (priority 5, can_bridge.c) and OTG_FS
 * (priority 6) are two independent NVIC lines, and CAN1's higher
 * priority means a running OTG_FS handler cannot even delay it via
 * preemption. The handlers below only ever service CAN, never USB.
 *
 * This means the original justification for CanBridge_PollHealth()'s
 * RX FIFO0 backstop poll ("USB starves the shared CAN1_RX0 vector") is
 * probably wrong for this board — the poll itself is still a harmless,
 * cheap safety net (see can_bridge.c), but the real cause of any
 * observed CAN frame loss under USB/CAN load is more likely elsewhere
 * (e.g. Flash erase/program stalling code fetch — STM32F1 is a single
 * Flash bank, execution from Flash is not guaranteed during an
 * erase/program cycle — see trigger.c/device_config.c/event_log.c,
 * which all write Flash from the main loop). Worth correlating
 * fifo_poll/lost_count with CMD_CFG_WRITE/CMD_TRIGGER_COMMIT timing and
 * with the event log's own page-erase-on-wrap before assuming this is
 * fully explained.
 */

#include "main.h"
#include "stm32f1xx_it.h"

extern PCD_HandleTypeDef hpcd;
extern CAN_HandleTypeDef hcan1;
extern CAN_HandleTypeDef hcan2;

void NMI_Handler(void) { }

/* Код фолта в BKP->DR3: регистр переживает IWDG/софт-ресет, следующий
 * старт приложения читает его и отдаёт в SYSTEM_INFO[59] — в полевом
 * логе видно, вис ли МК от краха, а не «просто перестал отвечать». */
void HardFault_Handler(void)
{
  App_NoteFault(1U);
  while (1) { }
}

void MemManage_Handler(void)
{
  App_NoteFault(2U);
  while (1) { }
}

void BusFault_Handler(void)
{
  App_NoteFault(3U);
  while (1) { }
}

void UsageFault_Handler(void)
{
  App_NoteFault(4U);
  while (1) { }
}

void SVC_Handler(void) { }
void DebugMon_Handler(void) { }
void PendSV_Handler(void) { }

void SysTick_Handler(void)
{
  HAL_IncTick();
}

/* CAN1 TX/RX0 vectors. Named USB_HP_CAN1_TX/USB_LP_CAN1_RX0 only because
 * that's the CMSIS legacy alias name (see file header note) — with the
 * OTG_FS peripheral in use here, these vectors carry ONLY CAN1, never
 * USB (OTG_FS_IRQHandler below is the real, separate USB vector). */
void USB_HP_CAN1_TX_IRQHandler(void)
{
  HAL_CAN_IRQHandler(&hcan1);
}

void USB_LP_CAN1_RX0_IRQHandler(void)
{
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
