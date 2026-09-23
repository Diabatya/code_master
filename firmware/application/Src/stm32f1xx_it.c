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
 * логе видно, вис ли МК от краха, а не «просто перестал отвечать».
 *
 * Дополнительно застеканный PC (точный адрес фолтящей инструкции) и
 * CFSR (класс фолта: precise/imprecise/unalign/invstate...) пишутся в
 * BKP->DR4..DR7 — SYSTEM_INFO[68..75]. Без этого полевой bootloop
 * «МК не стартует на активной CAN-шине» остаётся недоказуемым: DR3
 * говорил только ЧТО упало, но не ГДЕ. */
/* Не static и used: на символ прыгает inline-asm трамплина — компилятор
 * обязан сохранить функцию и пометить её Thumb-точкой входа. */
__attribute__((noinline, used))
void fault_capture(uint32_t *stacked, uint8_t code, uint32_t exc_ret)
{
  App_NoteFault(code); /* DR3 + включение PWR/BKP clock и DBP */
  uint32_t pc = stacked[6]; /* застеканный PC: r0,r1,r2,r3,r12,lr,pc,xpsr */
  uint32_t cfsr = SCB->CFSR;
  BKP->DR4 = (uint16_t)(pc & 0xFFFFU);
  BKP->DR5 = (uint16_t)(pc >> 16);
  BKP->DR6 = (uint16_t)(cfsr & 0xFFFFU);
  BKP->DR7 = (uint16_t)(cfsr >> 16);
  /* Полный дамп (LR/EXC_RETURN/ICSR/HFSR/BFAR) — в .noinit-RAM:
   * переживает IWDG-ресет, BKP-регистры уже все заняты. Полевой лог
   * 1.1.39 показал imprecise-фолт: застеканный PC — просто прерванная
   * инструкция, а не фолтящая — без CFSR/HFSR целиком и адреса BFAR
   * класс краха не различить. */
  App_StoreCrashDump(stacked, exc_ret);
  while (1) { }
}

/* Naked-трамплин: выбирает MSP/PSP по EXC_RETURN (lr бит 2) и передаёт
 * адрес застеканного фрейма в fault_capture. r1 — код фолта,
 * r2 — само EXC_RETURN (говорит thread/handler и MSP/PSP краха). */
#define FAULT_HANDLER(name, code)                                   \
  __attribute__((naked)) void name(void)                            \
  {                                                                 \
    __asm volatile(                                                 \
        "tst   lr, #4            \n\t"                              \
        "ite   eq                \n\t"                              \
        "mrseq r0, msp           \n\t"                              \
        "mrsne r0, psp           \n\t"                              \
        "movs  r1, #" #code "    \n\t"                              \
        "mov   r2, lr            \n\t"                              \
        "b     fault_capture     \n\t");                            \
  }

FAULT_HANDLER(HardFault_Handler, 1U)
FAULT_HANDLER(MemManage_Handler, 2U)
FAULT_HANDLER(BusFault_Handler, 3U)
FAULT_HANDLER(UsageFault_Handler, 4U)

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
