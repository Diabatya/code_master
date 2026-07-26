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

void Error_Handler(void);

#ifdef __cplusplus
}
#endif

#endif /* __MAIN_H */
