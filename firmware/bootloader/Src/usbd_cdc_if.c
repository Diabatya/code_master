/* USB CDC interface for the bootloader.
 * Uses a small ring buffer for RX and a blocking TX helper.
 */

#include "main.h"
#include "usbd_cdc_if.h"
#include "usbd_cdc.h"
#include "usbd_core.h"

/* USB Device handle declared in main.c */
extern USBD_HandleTypeDef hUsbDeviceFS;

static uint8_t UserRxBufferFS[APP_RX_DATA_SIZE];
static uint8_t UserTxBufferFS[APP_TX_DATA_SIZE];

static uint8_t rx_fifo[RX_FIFO_SIZE];
static volatile uint16_t rx_head = 0;
static volatile uint16_t rx_tail = 0;

static USBD_CDC_LineCodingTypeDef LineCoding = {
  115200, /* baud rate */
  0x00,   /* stop bits-1 */
  0x00,   /* parity - none */
  0x08    /* nb. of bits 8 */
};

static int8_t CDC_Init_FS(void);
static int8_t CDC_DeInit_FS(void);
static int8_t CDC_Control_FS(uint8_t cmd, uint8_t *pbuf, uint16_t length);
static int8_t CDC_Receive_FS(uint8_t *pbuf, uint32_t *Len);

USBD_CDC_ItfTypeDef USBD_CDC_fops = {
  CDC_Init_FS,
  CDC_DeInit_FS,
  CDC_Control_FS,
  CDC_Receive_FS
};

uint16_t CDC_GetRxAvailable(void)
{
  return (uint16_t)((uint16_t)(rx_head - rx_tail) & (RX_FIFO_SIZE - 1U));
}

uint8_t CDC_ReadRxByte(void)
{
  if (rx_head == rx_tail) {
    return 0U;
  }
  uint8_t b = rx_fifo[rx_tail];
  rx_tail = (rx_tail + 1U) & (RX_FIFO_SIZE - 1U);
  return b;
}

uint8_t CDC_Transmit_FS(uint8_t *Buf, uint16_t Len)
{
  USBD_CDC_HandleTypeDef *hcdc = (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
  uint16_t sent = 0;

  while (sent < Len) {
    uint16_t chunk = Len - sent;
    if (chunk > APP_TX_DATA_SIZE) {
      chunk = APP_TX_DATA_SIZE;
    }
    if (hcdc != NULL) {
      while (hcdc->TxState == 1U) {
        /* wait for previous IN transfer to complete */
      }
    }
    memcpy(UserTxBufferFS, Buf + sent, chunk);
    USBD_CDC_SetTxBuffer(&hUsbDeviceFS, UserTxBufferFS, chunk);
    if (USBD_CDC_TransmitPacket(&hUsbDeviceFS) != USBD_OK) {
      return 1U;
    }
    sent += chunk;
  }
  return 0U;
}

static int8_t CDC_Init_FS(void)
{
  USBD_CDC_SetRxBuffer(&hUsbDeviceFS, UserRxBufferFS);
  rx_head = 0;
  rx_tail = 0;
  return (USBD_CDC_ReceivePacket(&hUsbDeviceFS) == USBD_OK) ? 0 : -1;
}

static int8_t CDC_DeInit_FS(void)
{
  return 0;
}

static int8_t CDC_Control_FS(uint8_t cmd, uint8_t *pbuf, uint16_t length)
{
  switch (cmd) {
    case CDC_SEND_ENCAPSULATED_COMMAND:
    case CDC_GET_ENCAPSULATED_RESPONSE:
    case CDC_SET_COMM_FEATURE:
    case CDC_GET_COMM_FEATURE:
    case CDC_CLEAR_COMM_FEATURE:
    case CDC_SET_CONTROL_LINE_STATE:
    case CDC_SEND_BREAK:
      break;

    case CDC_SET_LINE_CODING:
      if (length == 7U) {
        LineCoding.bitrate    = (uint32_t)(pbuf[0] | (pbuf[1] << 8) |
                                         (pbuf[2] << 16) | (pbuf[3] << 24));
        LineCoding.format     = pbuf[4];
        LineCoding.paritytype = pbuf[5];
        LineCoding.datatype   = pbuf[6];
      }
      break;

    case CDC_GET_LINE_CODING:
      if (length == 7U) {
        pbuf[0] = (uint8_t)(LineCoding.bitrate);
        pbuf[1] = (uint8_t)(LineCoding.bitrate >> 8);
        pbuf[2] = (uint8_t)(LineCoding.bitrate >> 16);
        pbuf[3] = (uint8_t)(LineCoding.bitrate >> 24);
        pbuf[4] = LineCoding.format;
        pbuf[5] = LineCoding.paritytype;
        pbuf[6] = LineCoding.datatype;
      }
      break;

    default:
      break;
  }
  return 0;
}

static int8_t CDC_Receive_FS(uint8_t *Buf, uint32_t *Len)
{
  uint32_t i;
  for (i = 0; i < *Len; i++) {
    uint16_t next = (rx_head + 1U) & (RX_FIFO_SIZE - 1U);
    if (next == rx_tail) {
      /* FIFO overflow: drop remaining bytes */
      break;
    }
    rx_fifo[rx_head] = Buf[i];
    rx_head = next;
  }
  USBD_CDC_ReceivePacket(&hUsbDeviceFS);
  return 0;
}
