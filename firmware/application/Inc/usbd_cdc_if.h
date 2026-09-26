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
/* TX-кольцо CAN-кадров → USB: кадры складываются в очередь мгновенно
 * (без ожидания свободного IN-эндпоинта до 100 мс за кадр — это и был
 * источник «тормозов при двух CAN»: серия send_can_frame() ставила
 * главный цикл на секунды). CDC_PumpTx() в главном цикле выгребает
 * очередь пакетами по APP_TX_DATA_SIZE — несколько CAN-кадров едут
 * в одном USB-пакете. */
#define TX_FIFO_SIZE      2048

extern USBD_CDC_ItfTypeDef USBD_CDC_fops;

uint8_t CDC_Transmit_FS(uint8_t* Buf, uint16_t Len);
/* Досылает Buf[offset..Len) — используется для повторной попытки без
 * дублирования уже ушедших на шину байт (см. protocol.c::send_new_cmd_response
 * и комментарий в usbd_cdc_if.c: CDC_Transmit_FS() может остановиться
 * посреди многочанкового ответа, если хост >100 мс не забирает IN-пакет;
 * простой повтор с начала того же буфера отправлял бы уже переданные
 * чанки второй раз, ломая позиционный разбор ответа на ПК). Возвращает
 * итоговое количество успешно переданных байт (offset, если не сдвинулось
 * ни на байт, вплоть до Len при полном успехе) — вызывающий код продолжает
 * с этой позиции. */
uint16_t CDC_Transmit_FS_Resume(uint8_t *Buf, uint16_t offset, uint16_t Len);
/* CAN-кадр (или любой лучший-effort поток) → TX-кольцо. Никогда не
 * ждёт: при переполнении хвост отбрасывается и считается в tx_dropped.
 * В конце сам дёргает CDC_PumpTx(), чтобы пустой линк стартовал сразу. */
void CDC_QueueTx(const uint8_t *Buf, uint16_t Len);
/* Выгребает TX-кольцо в USB одним пакетом до APP_TX_DATA_SIZE за вызов.
 * Дешёвый при пустом кольце/занятом эндпоинте — зовётся каждой
 * итерацией главного цикла и из CDC_QueueTx(). */
void CDC_PumpTx(void);
/* Ждёт, пока последняя отправка реально уйдёт хосту (TxState==0 и
 * TX-кольцо пусто), максимум timeout_ms. Нужна перед NVIC_SystemReset по
 * командам конфигурации: слепая задержка теряла ответ при занятом
 * CDC-канале, и хост считал команду невыполненной, хотя она исполнилась. */
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
