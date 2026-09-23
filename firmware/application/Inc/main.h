/* Main header for the STM32F105 CAN-gateway application firmware.
 *
 * Pinout below follows the owner-provided ТЗ table (section 2.4):
 * CAN1 PB8/PB9 + RS PB7 + TERM PB3, CAN2 PB5/PB6 + RS PB4 + TERM PD2,
 * USB PA9..PA12, OUT1..OUT4 = PC10/PC11/PC12/PA15, LED PC13.
 * See firmware/PINOUT.md for the full table and free-pin notes.
 */

#ifndef __MAIN_H
#define __MAIN_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32f1xx_hal.h"

/* Shared with the bootloader project — must stay bit-for-bit identical so the
 * existing bootloader's soft-reset detection keeps working (see
 * firmware/bootloader/Inc/main.h and firmware/PROTOCOL.md 1.5). */
#define BOOTLOADER_FLAG_ADDRESS 0x20004FF0U
#define BOOTLOADER_FLAG_VALUE   0xDEADBEEFU

/* Primary handoff flag lives in backup register BKP->DR1 (see the
 * matching note in firmware/bootloader/Inc/main.h): immune to RAM
 * content — the legacy address above sits inside the CAN ring buffer,
 * so a received frame could overwrite the flag before the reset runs,
 * or leave a stray magic that would latch the bootloader after an
 * unrelated software reset (e.g. the CMD_CFG_WRITE re-enumeration). */
#define BOOTLOADER_BKP_VALUE    0xBEEFU

/* Блок целостности приложения, пишется tools/add_app_metadata.py в
 * последнюю страницу Flash: [0..3]=magic "APP1", [4..7]=версия формата,
 * [8..11]=размер образа, [12..15]=CRC32 образа. Используется в
 * CMD_SYSTEM_INFO (protocol.c) и в записи EVLOG_VERSION журнала. */
#define APP_METADATA_ADDR  0x0803D000U
#define APP_METADATA_MAGIC 0x41505031U

/* ---- Board pinout for the custom CAN1/CAN2 TJA1050 board ----------------- */

/* CAN1: remapped to PB8/PB9 (see AFIO remap in can_bridge.c) */
#define CAN1_RX_PORT   GPIOB
#define CAN1_RX_PIN    GPIO_PIN_8
#define CAN1_TX_PORT   GPIOB
#define CAN1_TX_PIN    GPIO_PIN_9

/* CAN2: remapped to PB5/PB6 (see AFIO remap in can_bridge.c) */
#define CAN2_RX_PORT   GPIOB
#define CAN2_RX_PIN    GPIO_PIN_5
#define CAN2_TX_PORT   GPIOB
#define CAN2_TX_PIN    GPIO_PIN_6

/* TJA1050 Normal(0)/Silent(1) select pin per channel. 0 = Normal, 1 = Silent */
#define CAN1_TXRX_S_PORT  GPIOB
#define CAN1_TXRX_S_PIN   GPIO_PIN_7
#define CAN2_TXRX_S_PORT  GPIOB
#define CAN2_TXRX_S_PIN   GPIO_PIN_4

/* 120R bus-termination enable per channel. 1 = enabled */
#define CAN1_TERM_PORT GPIOB
#define CAN1_TERM_PIN  GPIO_PIN_3
#define CAN2_TERM_PORT GPIOD
#define CAN2_TERM_PIN  GPIO_PIN_2

/* Silent->Normal switch settle time before first TX (ТЗ note: needs
 * oscilloscope calibration against real TJA1050 + board; a few microseconds
 * is typical per the datasheet). PLACEHOLDER, conservative. */
#define CAN_TRANSCEIVER_SWITCH_DELAY_US 5U

/* General-purpose outputs OUT1..OUT4 per ТЗ 2.4: PC10/PC11/PC12/PA15.
 * OUT4 = PA15, which is normally JTDI; JTAG is disabled (SWD-only debug,
 * ТЗ 13) to free this pin. */
#define OUT1_PORT GPIOC
#define OUT1_PIN  GPIO_PIN_10
#define OUT2_PORT GPIOC
#define OUT2_PIN  GPIO_PIN_11
#define OUT3_PORT GPIOC
#define OUT3_PIN  GPIO_PIN_12
#define OUT4_PORT GPIOA
#define OUT4_PIN  GPIO_PIN_15

/* VBUS sense: HIGH = USB powered, LOW = standalone mode (ТЗ 12.4). Same pin
 * already used as the USB PCD VBUS-sense input in usbd_conf.c; read directly
 * via GPIO here for the standalone-mode decision in the main loop. */
#define VBUS_SENSE_PORT GPIOA
#define VBUS_SENSE_PIN  GPIO_PIN_9
/* Set to 1 only when the real board connects PA9 to USB VBUS. */
#define APPLICATION_USE_VBUS_SENSE 0U

/* Status LED, reused from the bootloader project. */
#define LED_PORT GPIOC
#define LED_PIN  GPIO_PIN_13

void Error_Handler(void);
/* Кормит IWDG из ограниченных циклов ожидания (USB TX занят, стирание
 * Flash): ожидание само по себе выходит по таймауту, но суммарно может
 * перешагнуть ~1-секундный период вотчдога — без кормления устройство
 * уходило в reset, роняя USB-порт (в полевом логе — ClearCommError
 * PermissionError и «Таймаут записи команды» при прогрузке конфига). */
void App_KickWatchdog(void);
/* Причина последнего сброса МК: RCC->CSR[31:24], снятый загрузчиком в
 * BKP->DR2 до RMVF. Биты байта: 0x04 PIN (NRST), 0x08 POR, 0x10 soft
 * (NVIC), 0x20 IWDG, 0x40 WWDG, 0x80 LPWR. 0 = загрузчик не записал. */
uint8_t App_GetResetFlags(void);
/* Код фолта, оставленный обработчиком в BKP->DR3 до зависания:
 * 1=HardFault, 2=MemManage, 3=BusFault, 4=UsageFault, 0=фолта не было.
 * Одноразовый — читается и очищается при старте приложения. */
uint8_t App_GetFaultCode(void);
/* Застеканный PC и CFSR последнего фолта (BKP->DR4..DR7): точный адрес
 * инструкции краха + класс фолта для полевой диагностики. 0 = не было. */
uint32_t App_GetFaultPc(void);
uint32_t App_GetFaultCfsr(void);
void App_NoteFault(uint8_t code);
/* Полный дамп краха в .noinit-RAM: BKP-регистров всего десять, и они
 * уже заняты (флаг загрузчика DR1, PC DR4/5, CFSR DR6/7, этап DR8) —
 * HFSR/BFAR/LR/EXC_RETURN туда не помещаются, а RAM переживает системный
 * и IWDG-ресет. Пишется из fault-хендлера (только записи в RAM/регистры),
 * читается и очищается при старте — содержимое уходит в журнал записями
 * EVLOG_FAULT_*. */
typedef struct {
  uint32_t magic;    /* CRASH_DUMP_MAGIC — валидность дампа */
  uint32_t pc;       /* застеканный PC прерванного контекста */
  uint32_t lr;       /* застеканный LR — точка вызова прерванного кода */
  uint32_t exc_ret;  /* EXC_RETURN на входе в фолт (thread/handler, MSP/PSP) */
  uint32_t icsr;     /* SCB->ICSR — VECTACTIVE = какой IRQ был активен */
  uint32_t cfsr;     /* SCB->CFSR полный (MMFSR/BFSR/UFSR) */
  uint32_t hfsr;     /* SCB->HFSR (FORCED/VECTTBL/DEBUGEVT) */
  uint32_t bfar;     /* SCB->BFAR — адрес доступа (валиден при BFSR.BFARVALID) */
  uint32_t sum;      /* XOR всех полей выше — защита от мусора в RAM после
                      * подачи питания (магик может случайно совпасть) */
} crash_dump_t;
#define CRASH_DUMP_MAGIC 0x43525348U /* "CRSH" */
/* Вызывается из fault-хендлера: stacked — база застеканного фрейма
 * (r0..xpsr), exc_ret — значение LR на входе в исключение. */
void App_StoreCrashDump(const uint32_t *stacked, uint32_t exc_ret);
/* Дамп последнего краха или NULL, если магик/сумма не сошлись. */
const crash_dump_t *App_CrashDump(void);
void App_ClearCrashDump(void);
/* Худший зафиксированный запас между концом .bss и пиком использования
 * стека с момента старта (см. paint_stack_canary() в main.c) — байты.
 * Диагностика тесноты RAM (два CAN-кольца съедают ~35 КБ из 64 КБ);
 * отдаётся в CMD_SYSTEM_INFO. */
uint32_t App_GetStackFreeBytes(void);

#ifdef __cplusplus
}
#endif

#endif /* __MAIN_H */
