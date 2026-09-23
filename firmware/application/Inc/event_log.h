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
  EVLOG_BOOTLOADER         = 11U, /* вход в загрузчик. channel=0xFF. code:
                                    * 0 — переход по команде ПК (пишет приложение
                                    *     прямо перед сбросом в reboot_to_bootloader);
                                    * 1 — бутлоадер остался по флагу хоста (BKP->DR1);
                                    * 2 — бутлоадер остался: приложение невалидно/
                                    *     отсутствует (метаданные/CRC не сошлись).
                                    * Записи 1/2 пишет сам бутлоадер (append-only,
                                    * страницы журнала он не стирает — см.
                                    * firmware/bootloader/Src/event_log_bl.c). */
  EVLOG_FAULT              = 12U, /* крах прошлого сеанса (пишется при старте,
                                    * если в BKP остался код фолта):
                                    * channel=код фолта (1=HardFault...),
                                    * code=байт BFSR (CFSR[15:8]) —
                                    * PRECISERR/IMPRECISERR/STKERR/UNSTKERR/
                                    * BFARVALID (imprecise-фолт в поле имел
                                    * застеканный PC=прерванная инструкция,
                                    * поэтому класс важнее точного PC),
                                    * timestamp_ms=застеканный PC (НЕ время!). */
  EVLOG_VERSION            = 13U, /* идентификатор сборки: channel=версия
                                    * приложения (APP_DEVICE_VERSION),
                                    * code=версия протокола,
                                    * timestamp_ms=CRC32 образа приложения
                                    * (НЕ время!) — по CRC видно, какая именно
                                    * сборка писала этот журнал («лечил ли фикс»
                                    * vs «на камне старая прошивка»). */
  EVLOG_INIT_STAGE         = 14U, /* до какого этапа инициализации дошла
                                    * загрузка, которая закончилась крахом:
                                    * channel=этап (см. App_NoteStage в main.c:
                                    * 1=журнал, 2=config, 3=триггеры, 4=CAN,
                                    * 5=протокол, 6=USB, 7=NVIC CAN, 8=главный
                                    * цикл). Пишется только при старте после
                                    * краха — вместе с записью EVLOG_FAULT. */
  EVLOG_FAULT_REGS         = 15U, /* регистры краха из .noinit-дампа:
                                    * channel=код фолта, code=HFSR[31:24]
                                    * (FORCED/DEBUGEVT), timestamp_ms=
                                    * полный CFSR (НЕ время!). */
  EVLOG_FAULT_ADDR         = 16U, /* адрес доступа краха: channel=EXC_RETURN
                                    * (0xF9=thread/MSP, 0xED=handler/PSP),
                                    * timestamp_ms=SCB->BFAR (валиден при
                                    * BFSR.BFARVALID). */
  EVLOG_FAULT_LR           = 17U, /* контекст краха: timestamp_ms=застеканный
                                    * LR прерванного кода, channel+code=
                                    * SCB->ICSR мл./ст. байт — VECTACTIVE
                                    * показывает, какой IRQ был активен. */
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

/* То же, но без троттлинга и с явным значением поля timestamp_ms — для
 * записей, где это поле несёт данные, а не время (EVLOG_FAULT=PC,
 * EVLOG_VERSION=CRC32 образа). Только для одноразовых записей на старте —
 * частый вызов с тем же type не прореживается. */
void EventLog_AddEx(uint8_t type, uint8_t channel, uint8_t code,
                    uint32_t aux_timestamp);

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
