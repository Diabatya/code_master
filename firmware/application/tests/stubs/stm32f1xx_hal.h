/* Minimal stub so trigger.h (via can_bridge.h) compiles on the host for
 * tests/test_trigger_flash.c, without pulling in the real HAL/CMSIS. Only
 * struct/type declarations are needed here, since the host test exercises
 * the trigger_t layout and the write/read stride algorithm directly,
 * not the real Flash/CAN peripheral code. */
#ifndef __STM32F1XX_HAL_H_STUB
#define __STM32F1XX_HAL_H_STUB
#endif
