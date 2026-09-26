/* USB CDC interface for the application. Same ring-buffer design as the
 * bootloader's usbd_cdc_if.c, doubled in size (RX_FIFO_SIZE=2048) to absorb
 * bursts of interleaved CAN-frame + command bytes at high bus load (ТЗ 12.1),
 * plus CDC_PeekRxByte() added so protocol.c can look ahead in the FIFO
 * without consuming bytes (needed to detect multi-byte markers/framing
 * without an extra copy, and to scan for the REBOOT_TO_BOOTLOADER magic
 * string that can start at any offset — see firmware/PROTOCOL.md 1.5).
 */

#include "main.h"
#include "usbd_cdc_if.h"
#include "usbd_cdc.h"
#include "usbd_core.h"

extern USBD_HandleTypeDef hUsbDeviceFS;

static uint8_t UserRxBufferFS[APP_RX_DATA_SIZE];
static uint8_t UserTxBufferFS[APP_TX_DATA_SIZE];

static uint8_t rx_fifo[RX_FIFO_SIZE];
static volatile uint16_t rx_head = 0;
static volatile uint16_t rx_tail = 0;
/* TX-кольцо для CAN-потока: push мгновенный, USB забирает чанками по
 * 64 Б через CDC_PumpTx() из главного цикла. Пишется только из главного
 * цикла (send_can_frame в Protocol_Poll) — head/tail не нуждаются в
 * примитивной синхронизации против USB-IRQ, которое TX-кольцо не трогает
 * (TxState/TransmitPacket защищены критической секцией в pump). */
static uint8_t tx_fifo[TX_FIFO_SIZE];
static volatile uint16_t tx_head = 0;
static volatile uint16_t tx_tail = 0;
static volatile uint32_t tx_dropped = 0U;
static volatile uint32_t tx_busy_waits = 0U;
static volatile uint32_t rx_overflow_bytes = 0U;
static volatile uint8_t usb_reset_count = 0U;
static volatile uint8_t usb_disconnect_count = 0U;

static USBD_CDC_LineCodingTypeDef LineCoding = {
  115200,
  0x00,
  0x00,
  0x08
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

uint8_t CDC_PeekRxByte(uint16_t offset, uint8_t *out)
{
  if (offset >= CDC_GetRxAvailable()) {
    return 0U;
  }
  *out = rx_fifo[(rx_tail + offset) & (RX_FIFO_SIZE - 1U)];
  return 1U;
}

uint16_t CDC_Transmit_FS_Resume(uint8_t *Buf, uint16_t offset, uint16_t Len)
{
  uint16_t sent = offset;

  while (sent < Len) {
    uint16_t chunk = Len - sent;
    if (chunk > APP_TX_DATA_SIZE) {
      chunk = APP_TX_DATA_SIZE;
    }
    /* pClassData == NULL, пока хост не выдал SET_CONFIGURATION (класс
     * ещё не инициализирован), и снова NULL после USB-ресета —
     * USBD_CDC_DeInit() обнуляет его прямо из OTG-прерывания.
     * USBD_CDC_SetTxBuffer() пишет hcdc->TxBuffer/TxLength БЕЗ проверки:
     * запись по NULL+0x208 давала imprecise bus fault — застеканный PC
     * показывал инструкцию ПОСЛЕ возврата SetTxBuffer (write buffer),
     * CFSR[7:0]=0. Полевой лог 1.1.39: bootloop HardFault в главном
     * цикле ровно на первом CAN-кадре при активной шине — кадры сразу
     * стримятся на хост, а энумерация ещё не завершена. Без CAN нет
     * TX-трафика — окно гонки просто не задевалось. */
    USBD_CDC_HandleTypeDef *hcdc =
        (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
    if (hcdc == NULL) {
      tx_dropped++;
      return sent;
    }
    /* Bounded wait for the previous IN transfer to complete. Unbounded
     * busy-waiting here would hang the whole main loop (including CAN RX
     * draining via Protocol_Poll()) if the host ever stops reading the
     * CDC port, contradicting the "CAN reception must never be blocked"
     * requirement (ТЗ 12.3). On timeout, give up on this transmit rather
     * than deadlock; the PC-side protocol already tolerates dropped/lost
     * responses (commands can be retried, CAN frames are best-effort). */
    uint32_t wait_start = HAL_GetTick();
    while (hcdc->TxState == 1U) {
      /* Ожидание освобождения IN-конечной точки ограничено и само по
       * себе завершится — кормим IWDG, чтобы занятость USB не
       * принималась за зависание главного цикла (устройство уходило
       * в reset и роняло порт посреди серии команд). */
      App_KickWatchdog();
      if ((HAL_GetTick() - wait_start) >= 100U) {
        /* Отдельный счётчик «хост не забирал IN» — в поле отвечает на
         * вопрос «МК медленный или ПК не читает»: растёт именно когда
         * TxState занят >100 мс, т.е. на шине не было IN-токенов.
         * Возвращаем sent (а не 0/ошибку) — вызывающая сторона (см.
         * send_new_cmd_response) продолжает досылку С ЭТОЙ позиции, а
         * не пересылает уже ушедшие чанки заново: раньше повтор с
         * начала того же буфера дублировал байты на линии и ломал
         * позиционный разбор многочанкового ответа на ПК (SYSTEM_INFO,
         * TRIGGER_READ, EVENT_LOG — всё, что длиннее 64 байт). */
        tx_busy_waits++;
        tx_dropped++;
        return sent;
      }
      /* USB-ресет мог обнулить pClassData прямо во время ожидания —
       * перечитываем, чтобы не дальше ждать по устаревшему указателю. */
      hcdc = (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
      if (hcdc == NULL) {
        tx_dropped++;
        return sent;
      }
    }
    memcpy(UserTxBufferFS, Buf + sent, chunk);
    /* OTG-прерывание (USB reset -> USBD_CDC_DeInit -> pClassData=NULL)
     * может ударить между проверкой и программированием IN-передачи —
     * держим секцию атомарной: перечитывание pClassData + SetTxBuffer +
     * TransmitPacket (последний программирует регистры эндпоинта и
     * пишет FIFO — тоже неделимо относительно HAL_PCD_IRQHandler).
     * Критическая секция — микросекунды (регистры + до 16 слов в FIFO),
     * аппаратный CAN FIFO0 (3 слота) и backstop-опрос это переживают. */
    uint32_t primask = __get_PRIMASK();
    __disable_irq();
    hcdc = (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
    uint8_t tx_rc = USBD_BUSY;
    if (hcdc != NULL && hcdc->TxState == 0U) {
      USBD_CDC_SetTxBuffer(&hUsbDeviceFS, UserTxBufferFS, chunk);
      tx_rc = USBD_CDC_TransmitPacket(&hUsbDeviceFS);
    }
    __set_PRIMASK(primask);
    if (tx_rc != USBD_OK) {
      tx_dropped++;
      return sent;
    }
    sent += chunk;
  }
  return sent;
}

uint8_t CDC_Transmit_FS(uint8_t *Buf, uint16_t Len)
{
  return (CDC_Transmit_FS_Resume(Buf, 0U, Len) == Len) ? 0U : 1U;
}

void CDC_QueueTx(const uint8_t *Buf, uint16_t Len)
{
  /* Best-effort: место нет — хвост отбрасываем и считаем, кадр не
   * ждёт свободного эндпоинта. Команды сюда не ходят — их ответы идут
   * напрямую через CDC_Transmit_FS_Resume() с гарантированной досылкой;
   * в одном байтовом потоке это легально (host-парсер marker-based, он
   * уже сегодня видит ответы команд между CAN-кадрами). */
  for (uint16_t i = 0; i < Len; i++) {
    uint16_t next = (uint16_t)((tx_head + 1U) & (TX_FIFO_SIZE - 1U));
    if (next == tx_tail) {
      tx_dropped += (uint32_t)(Len - i);
      break;
    }
    tx_fifo[tx_head] = Buf[i];
    tx_head = next;
  }
  CDC_PumpTx();
}

void CDC_PumpTx(void)
{
  if (tx_head == tx_tail) {
    return;
  }
  USBD_CDC_HandleTypeDef *hcdc =
      (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
  if (hcdc == NULL || hcdc->TxState != 0U) {
    return; /* эндпоинт занят — кольцо подождёт следующий проход */
  }
  uint16_t chunk = (uint16_t)((tx_head - tx_tail) & (TX_FIFO_SIZE - 1U));
  if (chunk > APP_TX_DATA_SIZE) {
    chunk = APP_TX_DATA_SIZE;
  }
  for (uint16_t i = 0; i < chunk; i++) {
    UserTxBufferFS[i] = tx_fifo[(tx_tail + i) & (TX_FIFO_SIZE - 1U)];
  }
  /* Та же атомарная секция, что в CDC_Transmit_FS_Resume: между проверкой
   * и программированием IN-передачи не должен вклиниться OTG-IRQ. */
  uint32_t primask = __get_PRIMASK();
  __disable_irq();
  hcdc = (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
  uint8_t tx_rc = USBD_BUSY;
  if (hcdc != NULL && hcdc->TxState == 0U) {
    USBD_CDC_SetTxBuffer(&hUsbDeviceFS, UserTxBufferFS, chunk);
    tx_rc = USBD_CDC_TransmitPacket(&hUsbDeviceFS);
  }
  __set_PRIMASK(primask);
  if (tx_rc != USBD_OK) {
    return; /* байты остаются в кольце — следующий pump повторит */
  }
  tx_tail = (uint16_t)((tx_tail + chunk) & (TX_FIFO_SIZE - 1U));
}

uint32_t CDC_GetTxDropped(void)
{
  return tx_dropped;
}

uint32_t CDC_GetTxBusyWaits(void)
{
  return tx_busy_waits;
}

void CDC_NoteUsbEvent(uint8_t event)
{
  /* Счётчики насыщающиеся (не сбрасываются на 255) — для полевой
   * диагностики важен факт и порядок величины, а не точное число. */
  if (event == CDC_USB_EVENT_RESET) {
    if (usb_reset_count < 0xFFU) { usb_reset_count++; }
  } else if (event == CDC_USB_EVENT_DISCONNECT) {
    if (usb_disconnect_count < 0xFFU) { usb_disconnect_count++; }
  }
}

uint8_t CDC_GetUsbResetCount(void)
{
  return usb_reset_count;
}

uint8_t CDC_GetUsbDisconnectCount(void)
{
  return usb_disconnect_count;
}

uint32_t CDC_GetRxOverflowCount(void)
{
  return rx_overflow_bytes;
}

uint8_t CDC_FlushTx(uint32_t timeout_ms)
{
  USBD_CDC_HandleTypeDef *hcdc = (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
  if (hcdc == NULL) {
    return 1U;
  }
  uint32_t start = HAL_GetTick();
  /* Ждём и эндпоинт, и TX-кольцо: reset после CFG_WRITE не должен
   * отрезать ещё не ушедшие хосту CAN-кадры/ответ. */
  while (hcdc->TxState != 0U || tx_head != tx_tail) {
    App_KickWatchdog();
    CDC_PumpTx();
    if ((HAL_GetTick() - start) >= timeout_ms) {
      return 1U;
    }
    /* USB-ресет из OTG-IRQ может обнулить pClassData на ходу —
     * перечитываем, чтобы не поллить поле освобождённого дескриптора. */
    hcdc = (USBD_CDC_HandleTypeDef *)hUsbDeviceFS.pClassData;
    if (hcdc == NULL) {
      return 1U;
    }
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
      /* FIFO overflow: drop remaining bytes. This should not normally
       * happen given RX_FIFO_SIZE=2048 and the main loop draining it every
       * iteration; if it does, the affected command/frame will fail its
       * checksum/marker check downstream and be resynchronized safely. */
      rx_overflow_bytes += *Len - i;
      break;
    }
    rx_fifo[rx_head] = Buf[i];
    rx_head = next;
  }
  USBD_CDC_ReceivePacket(&hUsbDeviceFS);
  return 0;
}
