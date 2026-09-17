/* USB CDC interface header — application variant (identical to bootloader's). */

#ifndef __USBD_CDC_IF_H
#define __USBD_CDC_IF_H

#ifdef __cplusplus
extern "C" {
#endif

#include "usbd_cdc.h"

#define APP_RX_DATA_SIZE  64
#define APP_TX_DATA_SIZE  64
#define RX_FIFO_SIZE      2048

extern USBD_CDC_ItfTypeDef USBD_CDC_fops;

uint8_t CDC_Transmit_FS(uint8_t* Buf, uint16_t Len);
/* Ждёт, пока последняя отправка реально уйдёт хосту (TxState==0),
 * максимум timeout_ms. Нужна перед NVIC_SystemReset по командам
 * конфигурации: слепая задержка теряла ответ при занятом CDC-канале,
 * и хост считал команду невыполненной, хотя она исполнилась. */
uint8_t CDC_FlushTx(uint32_t timeout_ms);
uint16_t CDC_GetRxAvailable(void);
uint8_t CDC_ReadRxByte(void);
uint8_t CDC_PeekRxByte(uint16_t offset, uint8_t *out);
uint32_t CDC_GetTxDropped(void);
uint32_t CDC_GetTxBusyWaits(void);

/* Полевая диагностика USB (читается через CMD_SYSTEM_INFO): сколько раз
 * ядро сообщило bus reset / disconnect и сколько байт RX потеряно из-за
 * переполнения FIFO. По ним в логе видно, дёргалась ли эnumерация и
 * переполнялся ли входной буфер, без JTAG. */
#define CDC_USB_EVENT_RESET      0U
#define CDC_USB_EVENT_DISCONNECT 1U
void CDC_NoteUsbEvent(uint8_t event);
uint8_t CDC_GetUsbResetCount(void);
uint8_t CDC_GetUsbDisconnectCount(void);
uint32_t CDC_GetRxOverflowCount(void);

#ifdef __cplusplus
}
#endif

#endif /* __USBD_CDC_IF_H */
