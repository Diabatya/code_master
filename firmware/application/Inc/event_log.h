/* Flash-backed событийный журнал МК: переживает и мягкий сброс, и
 * отключение питания — обычные счётчики (CanBridge_GetStats(),
 * CMD_USB_STATS, CMD_SYSTEM_INFO) показывают только «как сейчас», а этот
 * журнал хронологию «что происходило», когда связь с ПК пропадала (полевой
 * симптом: разрыв CAN/USB, устраняется только отключением одного из
 * CAN-проводов — см. firmware/PROTOCOL.md и CanBridge_PollHealth()).
 *
 * Хранилище: кольцо на EVENT_LOG_POOL_PAGES страницах Flash
 * (0x0803B000..0x0803CFFF, см. STM32F105RCTx_APP.ld::_eventlog_pool_addr),
 * запись — фиксированные 16 байт, 128 записей на страницу. Пишется строго
 * последовательно (seq растёт монотонно); при заполнении кольцо оборачивается
 * и стирает следующую страницу целиком (128 самых старых записей разом) —
 * дешёвая амортизация числа стираний вместо стирания на каждую запись.
 *
 * ВАЖНО: запись — только из главного цикла (EventLog_Add вызывает
 * HAL_FLASH_Program/Erase), никогда из IRQ-контекста. */

#ifndef __EVENT_LOG_H
#define __EVENT_LOG_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define EVENT_LOG_POOL_PAGES      4U
#define EVENT_LOG_FLASH_PAGE      2048U
#define EVENT_LOG_RECORD_SIZE     16U
#define EVENT_LOG_SLOTS_PER_PAGE  (EVENT_LOG_FLASH_PAGE / EVENT_LOG_RECORD_SIZE)
#define EVENT_LOG_TOTAL_SLOTS     (EVENT_LOG_POOL_PAGES * EVENT_LOG_SLOTS_PER_PAGE)

/* Типы событий. code/channel — доп. данные, специфичные для типа (см.
 * комментарии), совпадают с семантикой, которую core/serial_manager.py
 * (read_event_log) и ui/event_log_tab.py декодируют для отображения. */
typedef enum {
  EVLOG_BOOT               = 1U, /* channel=App_GetFaultCode(), code=App_GetResetFlags() */
  EVLOG_CAN_ERROR          = 2U, /* channel=0/1 (CAN1/2), code=младший байт HAL_CAN_ERROR_* */
  EVLOG_CAN_BUSOFF         = 3U, /* channel=0/1 */
  EVLOG_CAN_BUSOFF_RECOVER = 4U, /* channel=0/1 */
  EVLOG_CAN_OVERFLOW       = 5U, /* channel=0/1 — RX-кольцо переполнилось (drop-oldest) */
  EVLOG_CAN_FIFO_POLL      = 6U, /* channel=0/1 — backstop вычитал кадр(ы) из FIFO0:
                                   * IRQ доставку пропустил, см. CanBridge_PollHealth() */
  EVLOG_USB_RESET          = 7U, /* USB bus reset (ре-энумерация) */
  EVLOG_USB_DISCONNECT     = 8U, /* физический disconnect (SEDET/просадка VBUS) */
  EVLOG_USB_TX_STALL       = 9U, /* CDC IN-эндпоинт голодал >100 мс — хост не читает */
  EVLOG_USB_RX_OVERFLOW    = 10U, /* программный RX FIFO (usbd_cdc_if.c) переполнен —
                                    * главный цикл не успевал вычитывать входящий поток */
} event_log_type_t;

typedef struct {
  uint32_t seq;
  uint32_t timestamp_ms; /* HAL_GetTick() в момент записи */
  uint8_t  type;         /* event_log_type_t */
  uint8_t  channel;      /* смысл зависит от type; 0xFF = не применимо */
  uint8_t  code;         /* смысл зависит от type */
} event_log_entry_t;

/* Сканирует пул, восстанавливает позицию записи (переживает мягкий сброс —
 * seq/указатель не хранятся в ОЗУ отдельно, только выводятся из Flash) и
 * добавляет запись EVLOG_BOOT. Вызывать один раз при старте, после
 * MX_IWDG_Init() (пишет Flash — при первом старте после обновления
 * прошивки область может быть занята старым кодом приложения и требует
 * стирания). */
void EventLog_Init(void);

/* Добавляет событие. Троттлинг: одинаковый (type, channel) не пишется
 * повторно чаще, чем раз в ~500 мс — иначе шторм ошибок на убитой шине
 * истощил бы ресурс стираний Flash за часы вместо лет (см. .c). */
void EventLog_Add(uint8_t type, uint8_t channel, uint8_t code);

/* Читает до max_count записей с seq > after_seq, в порядке возрастания
 * seq (хронологически) — используется CMD_EVENT_LOG для постраничного
 * вычитывания журнала хостом. Возвращает реально записанное количество. */
uint8_t EventLog_Read(uint32_t after_seq, event_log_entry_t *out, uint8_t max_count);

/* Последний записанный seq (0 — журнал пуст). */
uint32_t EventLog_LastSeq(void);

#ifdef __cplusplus
}
#endif

#endif /* __EVENT_LOG_H */
