/**
  * usbd_conf.c — application variant. Identical to the bootloader's version
  * except VBUS is read via GPIO input in main.c for the ТЗ 12.4 standalone
  * mode decision (this file only configures the PCD MSP; it does not itself
  * branch on VBUS — see main.c::Check_Standalone_Mode()).
  */

#include "stm32f1xx_hal.h"
#include "usbd_core.h"

static uint8_t usbd_pool[1024U] __attribute__((aligned(4)));
static int usbd_pool_busy = 0;

void *usbd_pool_malloc(size_t size)
{
  if (usbd_pool_busy || (size > sizeof(usbd_pool))) {
    return NULL;
  }
  usbd_pool_busy = 1;
  return usbd_pool;
}

void usbd_pool_free(void *p)
{
  if (p == usbd_pool) {
    usbd_pool_busy = 0;
  }
}

PCD_HandleTypeDef hpcd;

void HAL_PCD_MspInit(PCD_HandleTypeDef * hpcd)
{
  GPIO_InitTypeDef GPIO_InitStruct;

  __HAL_RCC_GPIOA_CLK_ENABLE();

  GPIO_InitStruct.Pin = (GPIO_PIN_11 | GPIO_PIN_12);
  GPIO_InitStruct.Mode = GPIO_MODE_AF_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_HIGH;
  HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

  /* VBUS Pin (PA9) — also re-read as plain GPIO input in main.c for the
   * ТЗ 12.4 standalone-mode check; HAL_PCD's own vbus_sensing_enable=1
   * (set in USBD_LL_Init below) uses this same physical pin/peripheral
   * path for USB connect detection. */
  GPIO_InitStruct.Pin = GPIO_PIN_9;
  GPIO_InitStruct.Mode = GPIO_MODE_INPUT;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);

  __HAL_RCC_USB_OTG_FS_CLK_ENABLE();

  HAL_NVIC_SetPriority(OTG_FS_IRQn, 6, 0);
  HAL_NVIC_EnableIRQ(OTG_FS_IRQn);
}

void HAL_PCD_MspDeInit(PCD_HandleTypeDef * hpcd)
{
  __HAL_RCC_USB_OTG_FS_CLK_DISABLE();
}

void HAL_PCD_SetupStageCallback(PCD_HandleTypeDef * hpcd)
{
  USBD_LL_SetupStage((USBD_HandleTypeDef *) hpcd->pData, (uint8_t *) hpcd->Setup);
}

void HAL_PCD_DataOutStageCallback(PCD_HandleTypeDef * hpcd, uint8_t epnum)
{
  USBD_LL_DataOutStage((USBD_HandleTypeDef *) hpcd->pData, epnum, hpcd->OUT_ep[epnum].xfer_buff);
}

void HAL_PCD_DataInStageCallback(PCD_HandleTypeDef * hpcd, uint8_t epnum)
{
  USBD_LL_DataInStage((USBD_HandleTypeDef *) hpcd->pData, epnum, hpcd->IN_ep[epnum].xfer_buff);
}

void HAL_PCD_SOFCallback(PCD_HandleTypeDef * hpcd)
{
  USBD_LL_SOF((USBD_HandleTypeDef *) hpcd->pData);
}

void HAL_PCD_ResetCallback(PCD_HandleTypeDef * hpcd)
{
  USBD_SpeedTypeDef speed = USBD_SPEED_FULL;

  switch (hpcd->Init.speed)
  {
  case PCD_SPEED_FULL:
    speed = USBD_SPEED_FULL;
    break;

  default:
    speed = USBD_SPEED_FULL;
    break;
  }
  USBD_LL_SetSpeed((USBD_HandleTypeDef *) hpcd->pData, speed);
  USBD_LL_Reset((USBD_HandleTypeDef *) hpcd->pData);
}

void HAL_PCD_SuspendCallback(PCD_HandleTypeDef * hpcd)
{
  USBD_LL_Suspend((USBD_HandleTypeDef *) hpcd->pData);
}

void HAL_PCD_ResumeCallback(PCD_HandleTypeDef * hpcd)
{
  USBD_LL_Resume((USBD_HandleTypeDef *) hpcd->pData);
}

void HAL_PCD_ISOOUTIncompleteCallback(PCD_HandleTypeDef * hpcd, uint8_t epnum)
{
  USBD_LL_IsoOUTIncomplete((USBD_HandleTypeDef *) hpcd->pData, epnum);
}

void HAL_PCD_ISOINIncompleteCallback(PCD_HandleTypeDef * hpcd, uint8_t epnum)
{
  USBD_LL_IsoINIncomplete((USBD_HandleTypeDef *) hpcd->pData, epnum);
}

void HAL_PCD_ConnectCallback(PCD_HandleTypeDef * hpcd)
{
  USBD_LL_DevConnected((USBD_HandleTypeDef *) hpcd->pData);
}

void HAL_PCD_DisconnectCallback(PCD_HandleTypeDef * hpcd)
{
  USBD_LL_DevDisconnected((USBD_HandleTypeDef *) hpcd->pData);
}

USBD_StatusTypeDef USBD_LL_Init(USBD_HandleTypeDef * pdev)
{
  hpcd.Instance = USB_OTG_FS;
  hpcd.Init.dev_endpoints = 4;
  hpcd.Init.low_power_enable = 0;
  hpcd.Init.Sof_enable = 0;
  hpcd.Init.speed = PCD_SPEED_FULL;
  hpcd.Init.vbus_sensing_enable = 1;
  hpcd.pData = pdev;
  pdev->pData = &hpcd;
  HAL_PCD_Init(&hpcd);

  HAL_PCDEx_SetRxFiFo(&hpcd, 0x80);
  HAL_PCDEx_SetTxFiFo(&hpcd, 0, 0x40);
  HAL_PCDEx_SetTxFiFo(&hpcd, 1, 0x80);
  HAL_PCDEx_SetTxFiFo(&hpcd, 2, 0x40);

  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_DeInit(USBD_HandleTypeDef * pdev)
{
  HAL_PCD_DeInit((PCD_HandleTypeDef *) pdev->pData);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_Start(USBD_HandleTypeDef * pdev)
{
  HAL_PCD_Start((PCD_HandleTypeDef *) pdev->pData);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_Stop(USBD_HandleTypeDef * pdev)
{
  HAL_PCD_Stop((PCD_HandleTypeDef *) pdev->pData);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_OpenEP(USBD_HandleTypeDef * pdev, uint8_t ep_addr, uint8_t ep_type, uint16_t ep_mps)
{
  HAL_PCD_EP_Open((PCD_HandleTypeDef *) pdev->pData, ep_addr, ep_mps, ep_type);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_CloseEP(USBD_HandleTypeDef * pdev, uint8_t ep_addr)
{
  HAL_PCD_EP_Close((PCD_HandleTypeDef *) pdev->pData, ep_addr);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_FlushEP(USBD_HandleTypeDef * pdev, uint8_t ep_addr)
{
  HAL_PCD_EP_Flush((PCD_HandleTypeDef *) pdev->pData, ep_addr);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_StallEP(USBD_HandleTypeDef * pdev, uint8_t ep_addr)
{
  HAL_PCD_EP_SetStall((PCD_HandleTypeDef *) pdev->pData, ep_addr);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_ClearStallEP(USBD_HandleTypeDef * pdev, uint8_t ep_addr)
{
  HAL_PCD_EP_ClrStall((PCD_HandleTypeDef *) pdev->pData, ep_addr);
  return USBD_OK;
}

uint8_t USBD_LL_IsStallEP(USBD_HandleTypeDef * pdev, uint8_t ep_addr)
{
  PCD_HandleTypeDef *hpcd = (PCD_HandleTypeDef *) pdev->pData;

  if ((ep_addr & 0x80) == 0x80)
  {
    return hpcd->IN_ep[ep_addr & 0x7F].is_stall;
  }
  else
  {
    return hpcd->OUT_ep[ep_addr & 0x7F].is_stall;
  }
}

USBD_StatusTypeDef USBD_LL_SetUSBAddress(USBD_HandleTypeDef * pdev, uint8_t dev_addr)
{
  HAL_PCD_SetAddress((PCD_HandleTypeDef *) pdev->pData, dev_addr);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_Transmit(USBD_HandleTypeDef * pdev, uint8_t ep_addr, uint8_t * pbuf, uint16_t size)
{
  HAL_PCD_EP_Transmit((PCD_HandleTypeDef *) pdev->pData, ep_addr, pbuf, size);
  return USBD_OK;
}

USBD_StatusTypeDef USBD_LL_PrepareReceive(USBD_HandleTypeDef * pdev, uint8_t ep_addr, uint8_t * pbuf, uint16_t size)
{
  HAL_PCD_EP_Receive((PCD_HandleTypeDef *) pdev->pData, ep_addr, pbuf, size);
  return USBD_OK;
}

uint32_t USBD_LL_GetRxDataSize(USBD_HandleTypeDef * pdev, uint8_t ep_addr)
{
  return HAL_PCD_EP_GetRxCount((PCD_HandleTypeDef *) pdev->pData, ep_addr);
}

void USBD_LL_Delay(uint32_t Delay)
{
  HAL_Delay(Delay);
}
