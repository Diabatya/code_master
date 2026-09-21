/* Flash-backed событийный журнал МК. См. event_log.h для обзора формата
 * и рационale; сама схема (кольцо страниц, заголовок-коммит, halfword
 * HAL_FLASH_Program) списана с trigger.c, только запись здесь одна
 * (16 Б), а не список — коммит не нужен, каждая запись самодостаточна
 * (валидируется своим magic+crc8). */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "event_log.h"

extern uint32_t _eventlog_pool_addr; /* символ линкера, см. .ld */
#define EVENT_LOG_POOL_BASE ((uint32_t)&_eventlog_pool_addr)

#define EVENT_LOG_MAGIC 0x4C45U /* "EL" */

typedef struct __attribute__((packed)) {
  uint16_t magic;
  uint32_t seq;
  uint32_t timestamp_ms;
  uint8_t  type;
  uint8_t  channel;
  uint8_t  code;
  uint8_t  crc8;   /* покрывает байты 0..14 */
  uint8_t  pad[2];
} wire_record_t; /* 16 байт */

_Static_assert(sizeof(wire_record_t) == EVENT_LOG_RECORD_SIZE,
               "event log record must be 16 bytes");
_Static_assert((EVENT_LOG_RECORD_SIZE % 2U) == 0U,
               "event log record must be halfword-aligned for HAL_FLASH_Program");

/* Минимальный интервал между повторными записями одного (type, channel) —
 * на убитой шине CAN_ERROR/BUSOFF мог бы сыпаться сотни раз в секунду,
 * стирая страницу (128 записей) каждые доли секунды и истощая ресурс
 * Flash за часы. 500 мс даёт достаточно частую историю по времени, не
 * убивая Flash при реальном шторме ошибок. */
#define EVENT_LOG_THROTTLE_MS 500U
/* type доходит до 9 (EVLOG_USB_TX_STALL); channel 0/1/0xFF -> индекс 2. */
#define EVLOG_TYPE_SLOTS 16U
static uint32_t s_last_log_tick[EVLOG_TYPE_SLOTS][3];

static uint32_t s_next_seq = 1U;
static uint16_t s_next_slot; /* 0..EVENT_LOG_TOTAL_SLOTS-1 — куда пишем следующую запись */

static uint8_t crc8(const uint8_t *data, uint32_t len)
{
  uint8_t crc = 0x00U;
  for (uint32_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8U; bit++) {
      crc = (crc & 0x80U) ? (uint8_t)((crc << 1) ^ 0x07U) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

static uint32_t slot_addr(uint16_t slot)
{
  return EVENT_LOG_POOL_BASE + (uint32_t)slot * EVENT_LOG_RECORD_SIZE;
}

static uint8_t slot_is_blank(uint16_t slot)
{
  const uint32_t *p = (const uint32_t *)slot_addr(slot);
  for (uint32_t i = 0; i < EVENT_LOG_RECORD_SIZE / 4U; i++) {
    if (p[i] != 0xFFFFFFFFUL) {
      return 0U;
    }
  }
  return 1U;
}

static uint8_t load_slot(uint16_t slot, wire_record_t *out)
{
  const uint8_t *p = (const uint8_t *)slot_addr(slot);
  memcpy(out, p, sizeof(*out));
  if (out->magic != EVENT_LOG_MAGIC || out->seq == 0U) {
    return 0U;
  }
  return crc8((const uint8_t *)out, offsetof(wire_record_t, crc8)) == out->crc8;
}

void EventLog_Init(void)
{
  memset(s_last_log_tick, 0, sizeof(s_last_log_tick));

  /* Кольцо пишется строго последовательно (slot растёт на 1 за запись, с
   * оборотом), поэтому старший найденный seq однозначно указывает, где
   * остановились — независимо от того, сколько кругов кольцо уже прошло. */
  uint32_t best_seq = 0U;
  int32_t best_slot = -1;
  for (uint16_t slot = 0U; slot < EVENT_LOG_TOTAL_SLOTS; slot++) {
    wire_record_t rec;
    if (load_slot(slot, &rec) && rec.seq >= best_seq) {
      best_seq = rec.seq;
      best_slot = (int32_t)slot;
    }
  }
  if (best_slot >= 0) {
    s_next_seq = best_seq + 1U;
    s_next_slot = (uint16_t)(((uint32_t)best_slot + 1U) % EVENT_LOG_TOTAL_SLOTS);
  } else {
    /* Пустой журнал — либо чистое устройство, либо после обновления
     * прошивки область ещё занята кодом старого приложения (не
     * распознаётся как запись — magic/crc не совпадут). В обоих случаях
     * начинаем с нулевого слота; EventLog_Add() сам стирает страницу,
     * если слот не чист. */
    s_next_seq = 1U;
    s_next_slot = 0U;
  }
  EventLog_Add((uint8_t)EVLOG_BOOT, App_GetFaultCode(), App_GetResetFlags());
}

static uint8_t erase_page_for_slot(uint16_t slot)
{
  uint32_t page = EVENT_LOG_POOL_BASE
      + ((uint32_t)slot / EVENT_LOG_SLOTS_PER_PAGE) * EVENT_LOG_FLASH_PAGE;
  App_KickWatchdog(); /* стирание страницы ~40 мс — с запасом против IWDG */
  FLASH_EraseInitTypeDef erase_init = {
    .TypeErase   = FLASH_TYPEERASE_PAGES,
    .PageAddress = page,
    .NbPages     = 1U,
  };
  uint32_t page_error = 0U;
  /* Аудит: HAL_FLASHEx_Erase() требует разблокированный Flash — раньше
   * эта функция вызывала её без HAL_FLASH_Unlock() (Unlock происходил
   * только позже, перед программированием в EventLog_Add()), из-за
   * чего первое стирание страницы (обновление прошивки/оборот кольца)
   * не проходило. */
  HAL_FLASH_Unlock();
  uint8_t ok = (HAL_FLASHEx_Erase(&erase_init, &page_error) == HAL_OK) ? 1U : 0U;
  HAL_FLASH_Lock();
  return ok;
}

void EventLog_Add(uint8_t type, uint8_t channel, uint8_t code)
{
  uint8_t ch_idx = (channel > 1U) ? 2U : channel;
  uint8_t type_idx = (type < EVLOG_TYPE_SLOTS) ? type : 0U;
  uint32_t now = HAL_GetTick();
  uint32_t last = s_last_log_tick[type_idx][ch_idx];
  if (last != 0U && (uint32_t)(now - last) < EVENT_LOG_THROTTLE_MS) {
    return; /* троттлинг — не стираем Flash повторами на убитой шине */
  }
  s_last_log_tick[type_idx][ch_idx] = (now == 0U) ? 1U : now; /* 0 = "ещё не было" */

  wire_record_t rec;
  memset(&rec, 0, sizeof(rec));
  rec.magic = EVENT_LOG_MAGIC;
  rec.seq = s_next_seq;
  rec.timestamp_ms = now;
  rec.type = type;
  rec.channel = channel;
  rec.code = code;
  rec.crc8 = crc8((const uint8_t *)&rec, offsetof(wire_record_t, crc8));

  uint16_t slot = s_next_slot;
  if (!slot_is_blank(slot)) {
    /* Первая запись в эту страницу после ребута/оборота кольца: либо
     * старый код приложения (после обновления прошивки, сдвинувшего
     * границу Flash), либо предыдущий круг записей журнала. Оба случая
     * требуют стирания перед программированием (Flash пишет только
     * 1<-0, стереть обратно в 0xFF можно только постранично). */
    if (!erase_page_for_slot(slot)) {
      return;
    }
  }

  HAL_FLASH_Unlock();
  uint32_t addr = slot_addr(slot);
  const uint16_t *src16 = (const uint16_t *)&rec;
  uint8_t ok = 1U;
  for (uint32_t w = 0U; w < sizeof(rec) / 2U; w++) {
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, src16[w]) != HAL_OK) {
      ok = 0U;
      break;
    }
    addr += 2U;
  }
  HAL_FLASH_Lock();
  if (!ok) {
    return;
  }

  s_next_seq++;
  s_next_slot = (uint16_t)((slot + 1U) % EVENT_LOG_TOTAL_SLOTS);
}

uint8_t EventLog_Read(uint32_t after_seq, event_log_entry_t *out, uint8_t max_count)
{
  /* s_next_slot — слот, который будет перезаписан следующим, т.е. самая
   * старая из ещё живых записей (или первый нетронутый слот). Обход от
   * него по кругу идёт строго по возрастанию seq — ровно то, что нужно
   * для хронологической постраничной выдачи без сортировки. */
  uint8_t count = 0U;
  for (uint16_t i = 0U; i < EVENT_LOG_TOTAL_SLOTS && count < max_count; i++) {
    uint16_t slot = (uint16_t)(((uint32_t)s_next_slot + i) % EVENT_LOG_TOTAL_SLOTS);
    wire_record_t rec;
    if (!load_slot(slot, &rec) || rec.seq <= after_seq) {
      continue;
    }
    out[count].seq = rec.seq;
    out[count].timestamp_ms = rec.timestamp_ms;
    out[count].type = rec.type;
    out[count].channel = rec.channel;
    out[count].code = rec.code;
    count++;
  }
  return count;
}

uint32_t EventLog_LastSeq(void)
{
  return (s_next_seq > 1U) ? (s_next_seq - 1U) : 0U;
}
