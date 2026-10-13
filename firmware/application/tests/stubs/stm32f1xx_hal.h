/* Stub so firmware headers (via can_bridge.h / main.h) and the runtime
 * sources (trigger.c) compile on the host for tests/, without pulling in
 * the real HAL/CMSIS. Only the minimal declarations used by the code
 * under test live here; the test harness provides the implementations
 * (HAL_GetTick, HAL_FLASH_*, CanBridge_Transmit, Storage_* adapters). */
#ifndef __STM32F1XX_HAL_H_STUB
#define __STM32F1XX_HAL_H_STUB

#include <stdint.h>

typedef enum {
  HAL_OK      = 0x00,
  HAL_ERROR   = 0x01,
  HAL_BUSY    = 0x02,
  HAL_TIMEOUT = 0x03
} HAL_StatusTypeDef;

#define FLASH_TYPEERASE_PAGES      0x00U
#define FLASH_TYPEPROGRAM_HALFWORD 0x01U

typedef struct {
  uint32_t TypeErase;
  uint32_t PageAddress;
  uint32_t NbPages;
} FLASH_EraseInitTypeDef;

uint32_t HAL_GetTick(void);
void HAL_Delay(uint32_t Delay);
void HAL_FLASH_Unlock(void);
void HAL_FLASH_Lock(void);
HAL_StatusTypeDef HAL_FLASH_Program(uint32_t TypeProgram,
                                    uint32_t Address, uint64_t Data);
HAL_StatusTypeDef HAL_FLASHEx_Erase(FLASH_EraseInitTypeDef *pEraseInit,
                                    uint32_t *PageError);

#endif
