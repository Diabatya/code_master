/* USB CDC interface header for the bootloader. */

#ifndef __USBD_CDC_IF_H
#define __USBD_CDC_IF_H

#ifdef __cplusplus
extern "C" {
#endif

#include "usbd_cdc.h"

#define APP_RX_DATA_SIZE  64
#define APP_TX_DATA_SIZE  64
#define RX_FIFO_SIZE      1024

extern USBD_CDC_ItfTypeDef USBD_CDC_fops;

uint8_t CDC_Transmit_FS(uint8_t* Buf, uint16_t Len);
uint16_t CDC_GetRxAvailable(void);
uint8_t CDC_ReadRxByte(void);

#ifdef __cplusplus
}
#endif

#endif /* __USBD_CDC_IF_H */
