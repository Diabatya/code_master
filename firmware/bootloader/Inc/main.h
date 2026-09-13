/* Main header for the STM32F105 USB CDC bootloader. */

#ifndef __MAIN_H
#define __MAIN_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32f1xx_hal.h"

#define APP_START_ADDRESS   0x08008000U
#define APP_VECTOR_TABLE    APP_START_ADDRESS

#define BOOTLOADER_FLAG_ADDRESS 0x20004FF0U
#define BOOTLOADER_FLAG_VALUE   0xDEADBEEFU

/* Primary handoff flag lives in backup register BKP->DR1: it survives
 * NVIC_SystemReset (backup domain is not reset by it), is cleared on real
 * power loss, and unlike the RAM word above cannot collide with
 * application data — BOOTLOADER_FLAG_ADDRESS sits inside the app's CAN
 * ring buffer, so bus traffic could leave a stray 0xDEADBEEF there and
 * latch the bootloader after any unrelated software reset (the app
 * NVIC-resets on every CMD_CFG_WRITE). The RAM flag is kept only for
 * compatibility with bootloaders already flashed on shipped boards. */
#define BOOTLOADER_BKP_VALUE    0xBEEFU

void Error_Handler(void);

#ifdef __cplusplus
}
#endif

#endif /* __MAIN_H */
