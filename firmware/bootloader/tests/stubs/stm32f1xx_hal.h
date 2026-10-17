/* Host stub of the HAL/CMSIS surface used by
 * firmware/bootloader/Src/bootloader.c — lets the PRODUCTION command
 * dispatcher compile and run on a PC for tests/ without the real
 * HAL/CMSIS/CMSIS-Core.
 *
 * Peripheral blocks (PWR/BKP/RCC/SysTick/NVIC/SCB) are real globals in
 * the harness, not address constants — Bootloader_JumpToApplication and
 * Bootloader_ShouldStay stay safe to call from the host process.
 * Only declarations used by the code under test live here; the harness
 * provides the implementations (HAL_GetTick, HAL_FLASH_*,
 * CDC_Transmit_FS / CDC_GetRxAvailable / CDC_ReadRxByte). */
#ifndef __STM32F1XX_HAL_H_STUB
#define __STM32F1XX_HAL_H_STUB

#include <stdint.h>

#ifndef __IO
#define __IO volatile
#endif

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

/* ---- Backup domain / reset cause (Bootloader_ShouldStay) ---------- */
typedef struct { uint32_t CR; } PWR_TypeDef;
typedef struct { uint32_t DR1; uint32_t DR2; } BKP_TypeDef;
typedef struct { uint32_t CSR; } RCC_TypeDef;

extern PWR_TypeDef StubPWR;
extern BKP_TypeDef StubBKP;
extern RCC_TypeDef StubRCC;

#define PWR ((PWR_TypeDef *)&StubPWR)
#define BKP ((BKP_TypeDef *)&StubBKP)
#define RCC ((RCC_TypeDef *)&StubRCC)

#define PWR_CR_DBP      0x00000100U
#define RCC_CSR_SFTRSTF 0x10000000U
#define RCC_CSR_RMVF    0x01000000U

#define __HAL_RCC_PWR_CLK_ENABLE()        do { } while (0)
#define __HAL_RCC_BKP_CLK_ENABLE()        do { } while (0)
#define __HAL_RCC_USB_OTG_FS_CLK_DISABLE() do { } while (0)
#define __HAL_RCC_GPIOA_CLK_DISABLE()     do { } while (0)
#define __HAL_RCC_GPIOC_CLK_DISABLE()     do { } while (0)
#define __HAL_RCC_AFIO_CLK_DISABLE()      do { } while (0)

/* ---- Core peripherals touched by Bootloader_JumpToApplication ------ */
typedef struct { uint32_t CTRL; uint32_t LOAD; uint32_t VAL; } SysTick_TypeDef;
typedef struct { uint32_t ICER[8]; uint32_t ICPR[8]; } NVIC_Stub_TypeDef;
typedef struct { uint32_t VTOR; } SCB_Stub_TypeDef;

extern SysTick_TypeDef  StubSysTick;
extern NVIC_Stub_TypeDef StubNVIC;
extern SCB_Stub_TypeDef  StubSCB;

#define SysTick (&StubSysTick)
#define NVIC    (&StubNVIC)
#define SCB     (&StubSCB)

void Stub_set_CONTROL(uint32_t v);
void Stub_set_MSP(uint32_t v);
#define __set_CONTROL(v) Stub_set_CONTROL(v)
#define __set_MSP(v)     Stub_set_MSP(v)
#define __disable_irq()  do { } while (0)
#define __enable_irq()   do { } while (0)
#define __DSB()          do { } while (0)
#define __ISB()          do { } while (0)

/* ---- USB device disconnect (bl_cmd_go) ----------------------------- */
#define USB_OTG_FS ((void *)0)
void USB_DevDisconnect(void *instance);

#endif /* __STM32F1XX_HAL_H_STUB */
