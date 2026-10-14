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

/* ---- GPIO/AFIO stubs (aux_out.c, can_bridge.c host harnesses) ---- */
typedef struct { uint32_t regs[8]; } GPIO_TypeDef;
typedef struct {
  uint32_t Pin;
  uint32_t Mode;
  uint32_t Pull;
  uint32_t Speed;
} GPIO_InitTypeDef;
typedef enum { GPIO_PIN_RESET = 0, GPIO_PIN_SET = 1 } GPIO_PinState;

#define GPIOA ((GPIO_TypeDef *)0x40010800UL)
#define GPIOB ((GPIO_TypeDef *)0x40010C00UL)
#define GPIOC ((GPIO_TypeDef *)0x40011000UL)
#define GPIOD ((GPIO_TypeDef *)0x40011400UL)

#define GPIO_PIN_2  0x0004U
#define GPIO_PIN_3  0x0008U
#define GPIO_PIN_10 0x0400U
#define GPIO_PIN_11 0x0800U
#define GPIO_PIN_12 0x1000U
#define GPIO_PIN_13 0x2000U
#define GPIO_PIN_14 0x4000U
#define GPIO_PIN_15 0x8000U

#define GPIO_MODE_INPUT     0x00U
#define GPIO_MODE_OUTPUT_PP 0x10U
#define GPIO_NOPULL         0x00U
#define GPIO_SPEED_FREQ_LOW 0x02U

void HAL_GPIO_Init(GPIO_TypeDef *GPIOx, const GPIO_InitTypeDef *init);
void HAL_GPIO_WritePin(GPIO_TypeDef *GPIOx, uint16_t pin,
                       GPIO_PinState state);
GPIO_PinState HAL_GPIO_ReadPin(GPIO_TypeDef *GPIOx, uint16_t pin);

/* RCC/AFIO macros become observable calls: the harness counts SWJ
 * remaps to verify the PA14 release/restore contract (CM-N4). */
void    StubAfio_RemapSwj(uint32_t mapr_swj_cfg);
#define __HAL_RCC_AFIO_CLK_ENABLE()  do { } while (0)
#define __HAL_RCC_GPIOA_CLK_ENABLE() do { } while (0)
#define __HAL_RCC_GPIOB_CLK_ENABLE() do { } while (0)
#define __HAL_RCC_GPIOC_CLK_ENABLE() do { } while (0)
#define __HAL_RCC_GPIOD_CLK_ENABLE() do { } while (0)
#define __HAL_AFIO_REMAP_SWJ_DISABLE() StubAfio_RemapSwj(0x07000000UL)
#define __HAL_AFIO_REMAP_SWJ_NOJTAG()  StubAfio_RemapSwj(0x02000000UL)

#endif
