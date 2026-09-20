/* Trigger engine: up to TRIGGER_COUNT independently configurable triggers
 * (ТЗ 10.2; количество ограничено только местом в странице Flash), each
 * with a receive condition
 * (ID/mask/data/data-mask on a given channel) and a response (CAN frame +
 * delay). Storage is Flash-backed (see firmware/PROTOCOL.md Part 3, trigger
 * page 0x0803E000). Field set is a superset inferred from the existing
 * PC-side ui/can_trigger_tab.py logic, since ТЗ section 8 (exact trigger
 * fields) was not provided — see firmware/PROTOCOL.md 2.4 note.
 *
 * Latency: Trigger_OnFrame() must be called from the main loop immediately
 * after popping a frame from the CAN ring buffer (see main.c), never from
 * IRQ context, so it can never delay CAN reception (ТЗ 12.3). Delayed
 * responses (delay_ms > 0) are deferred using HAL_GetTick() (see trigger.c)
 * so a requested delay is honored without busy-waiting, keeping the main
 * loop free to keep draining CAN ring buffers while a response is pending.
 * NOTE: HAL_GetTick() only has 1ms (SysTick) resolution, which is borderline
 * against the ТЗ 12.3 "±1ms" latency target — see the caveat in README.md;
 * a hardware timer (TIM) would be needed for a tighter guarantee.
 */

#ifndef __TRIGGER_H
#define __TRIGGER_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "can_bridge.h"

/* Хранилище триггеров v3 — динамическое: записи упакованы в суффикс
 * пула страниц над config-страницей (0x0803E000..0x0803FFFF) и
 * привязаны к ВЕРХУ Flash. Пустое устройство не занимает ни страницы;
 * область растёт вниз по мере добавления триггеров (1 страница на
 * каждые ~24 записи + заголовок). Заголовок "TRGH" в начале области
 * находится сканированием при старте — фиксированного адреса области
 * больше нет, прошивка может расти, не сдвигая формат хранения.
 * Записи v2 из старой фиксированной области 0x0803E000 импортируются
 * при первой загрузке и переписываются в новый формат при ближайшем
 * COMMIT. Запись по-прежнему 82 Б (rx_rtr занял байт reserved_pad):
 * 0 = матч любого кадра (как раньше), 1 = только RTR-запрос,
 * 2 = только кадр с данными. */
#define TRIGGER_POOL_BASE     0x0803E000U /* первая страница над config */
#define TRIGGER_FLASH_END     0x08040000U /* конец Flash F105RCT6 */
#define TRIGGER_FLASH_PAGE    2048U
#define TRIGGER_POOL_PAGES    ((TRIGGER_FLASH_END - TRIGGER_POOL_BASE) / TRIGGER_FLASH_PAGE)
#define TRIGGER_HEADER_MAGIC  0x54524748U /* "TRGH" */
#define TRIGGER_HEADER_SIZE   16U
#define TRIGGER_STORE_VERSION 3U
#define TRIGGER_MAGIC         0x54524732U /* "TRG2" */
#define TRIGGER_FORMAT_VERSION 2U
#define TRIGGER_RECORD_SIZE    82U
/* Предел по RAM, а не по пулу: триггеры, staging-буфер и кэши живут в
 * ОЗУ — 70 записей ≈ 3 страницы пула из 4 (четвёртая остаётся запасом
 * на рост записи/пула). При росте RAM-бюджета можно поднять до 99. */
#define TRIGGER_MAX_RECORDS   70U
/* Легаси-регион v2: 49 слотов × 82 Б с 0x0803E000 (читается один раз
 * при миграции, затем страницы возвращаются в пул). */
#define TRIGGER_LEGACY_ADDR   0x0803E000U
#define TRIGGER_LEGACY_COUNT  49U

typedef struct __attribute__((packed)) {
  uint32_t magic;       /* "TRGH" */
  uint8_t  version;     /* TRIGGER_STORE_VERSION */
  uint8_t  count;       /* число записей, идущих следом */
  uint8_t  flags;       /* зарезервировано, 0 */
  uint32_t generation;  /* монотонный счётчик — выбор новейшей области */
  uint32_t reserved;
  uint8_t  crc8;        /* crc8 байт 0..14 */
} trigger_header_t; /* 16 B */

typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint8_t  enabled;
  uint8_t  rx_channel;      /* «Приём»: 0=CAN1, 1=CAN2, 2=любой из двух */
  uint8_t  rx_extended;
  uint32_t rx_id;
  uint32_t rx_id_mask;      /* bits set = must match; bits clear = don't-care */
  uint8_t  rx_dlc;
  uint8_t  rx_data[8];
  uint8_t  rx_data_mask[8]; /* per-byte don't-care mask, 0x00 = ignore byte */
  uint8_t  tx_channel;      /* «Куда отправляем»: 0=CAN1, 1=CAN2, 2=оба */
  uint8_t  tx_extended;
  uint32_t tx_id;
  uint8_t  tx_dlc;
  uint8_t  tx_data[8];
  uint16_t delay_ms;        /* пауза перед отправкой, 0..~65s */
  uint8_t  reserved[2];     /* [0]=FORMAT_VERSION, [1]=RECORD_SIZE */
  uint8_t  tx_rtr;          /* 0=data response, 1=Remote Transmission Request */
  uint8_t  cache_enabled;   /* режим «автоматическая запись DATA в кэш» */
  uint8_t  src_channel;     /* «Откуда читаем»: 0=CAN1, 1=CAN2, 2=любой */
  uint8_t  src_extended;
  uint32_t src_id;          /* кадр кэшируется при точном совпадении ID */
  uint8_t  src_dlc;
  uint8_t  src_from[8];     /* побайтовая нижняя граница Data (src_dlc байт) */
  uint8_t  src_to[8];       /* побайтовая верхняя граница; from>to = wildcard «X» (байт игнорируется, в кэше = 0x00) */
  uint16_t tx_interval_ms;  /* пауза между повторными отправками */
  uint8_t  tx_count;        /* кол-во отправок (0 трактуется как 1) */
  uint8_t  rx_rtr;          /* приём: 0=любой кадр, 1=только RTR, 2=только data */
  uint8_t  reserved_pad;
  uint8_t  crc8;
} trigger_t; /* 82 bytes */

/* Loads all triggers from Flash into RAM (call once at boot). Any slot
 * with a bad magic/CRC is treated as "disabled, all zero". */
void Trigger_Init(void);

/* Must be called once per main-loop iteration to service pending delayed
 * responses (uses HAL_GetTick(), millisecond resolution — see the ±1ms
 * latency caveat in README.md regarding SysTick-based timing vs. a
 * dedicated hardware timer). */
void Trigger_Poll(void);

/* Returns cumulative trigger timing counters since boot. dropped_count —
 * отправки ответа, исчерпавшие ретраи на занятых TX-ящиках (можно NULL). */
void Trigger_GetStats(uint32_t *fired_count, uint32_t *max_lateness_ms,
                      uint32_t *dropped_count);

/* Сколько включённых триггеров прошло валидацию Flash при старте —
 * диагностика «триггеры пропали после выключения питания». */
uint8_t Trigger_FlashValidCount(void);

/* Evaluates all enabled triggers against a freshly received frame and
 * arms any matching response (respecting its configured delay_ms). Must
 * be called from the main loop only, never from IRQ context. */
void Trigger_OnFrame(const can_frame_t *frame);

/* Read one trigger slot (index 0..Trigger_Count()-1) into *out. Returns 1
 * if index valid. */
uint8_t Trigger_Get(uint8_t index, trigger_t *out);

/* Число записей в активном списке триггеров (0 на пустом устройстве). */
uint8_t Trigger_Count(void);

/* Validates and writes one trigger slot to Flash + RAM. Returns 1 on
 * success. NOTE: STM32F1 Flash стирается только постранично — запись
 * переписывает весь активный список, но только реально занятые страницы
 * (пустые страницы пула не стираются). */
uint8_t Trigger_Set(uint8_t index, const trigger_t *trig);

/* Stages one trigger in RAM and commits the list in one Flash pass.
 * COMMIT принимает итоговую длину списка: позиции без staged-записи
 * берутся из текущего списка, хвост за total отбрасывается — так
 * выражается и изменение, и удаление, и добавление. */
uint8_t Trigger_Stage(uint8_t index, const trigger_t *trig);
uint8_t Trigger_Commit(uint8_t total);

/* Полностью стирает хранилище триггеров (все страницы пула) и очищает
 * RAM-список — для «Заводских настроек». */
void Trigger_ClearAll(void);

/* Enables/disables a trigger without touching its other fields. */
uint8_t Trigger_SetEnabled(uint8_t index, uint8_t enabled);

#ifdef __cplusplus
}
#endif

#endif /* __TRIGGER_H */
