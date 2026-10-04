/* Flash-backed событийный журнал МК. См. event_log.h для обзора формата
 * и рационала. Область — маркерная (заголовок EVLH в начале региона,
 * storage.c держит её позицию): журнал сидит ровно под областью
 * переменных VARH и переносится вместе с ней при росте хранилищ —
 * фиксированного адреса нет, EventLog_Init() получает базу у
 * storage.c. Перенос (EventLog_Relocate) копирует записи дословно —
 * история не теряется, seq не сбрасывается. */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "event_log.h"
#include "storage.h"

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
 * стирая страницу каждые доли секунды и истощая ресурс Flash за часы.
 * 500 мс даёт достаточно частую историю по времени, не убивая Flash при
 * реальном шторме ошибок. */
#define EVENT_LOG_THROTTLE_MS 500U
/* type доходит до 17 (EVLOG_FAULT_LR); channel 0/1/0xFF -> индекс 2. */
#define EVLOG_TYPE_SLOTS 24U
static uint32_t s_last_log_tick[EVLOG_TYPE_SLOTS][3];

static uint32_t s_next_seq = 1U;
static uint16_t s_next_slot; /* 0..EVENT_LOG_TOTAL_SLOTS-1 — куда пишем следующую запись */
static uint32_t s_base;      /* база области EVLH; 0 — журнала нет */
static uint32_t s_generation;
static uint8_t  s_moving;    /* идёт EventLog_Relocate — записи отбрасываются */

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
  return s_base + EVENT_LOG_HEADER_BYTES
       + (uint32_t)slot * EVENT_LOG_RECORD_SIZE;
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

static uint32_t slot_page(uint16_t slot)
{
  uint32_t offset = EVENT_LOG_HEADER_BYTES
      + (uint32_t)slot * EVENT_LOG_RECORD_SIZE;
  return s_base + (offset / EVENT_LOG_FLASH_PAGE) * EVENT_LOG_FLASH_PAGE;
}

static void program_u16(uint32_t addr, uint16_t v)
{
  (void)HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, v);
}

/* Пишет заголовок EVLH в начало области (страница уже стёрта). */
static void write_region_header(uint32_t base, uint32_t generation)
{
  store_header_t h;
  memset(&h, 0, sizeof(h));
  h.magic = STORE_MAGIC_EVLOG;
  h.generation = generation;
  h.payload_len = EVENT_LOG_POOL_PAGES * EVENT_LOG_FLASH_PAGE
                - EVENT_LOG_HEADER_BYTES;
  h.payload_crc = 0U; /* записи журнала валидируются поштучно своим crc8 */
  h.version = STORE_VERSION;
  h.region = 0xFFU;
  h.crc8 = crc8((const uint8_t *)&h, offsetof(store_header_t, crc8));
  HAL_FLASH_Unlock();
  const uint16_t *hw = (const uint16_t *)&h;
  for (uint32_t w = 0U; w < sizeof(h) / 2U; w++) {
    program_u16(base + w * 2U, hw[w]);
  }
  HAL_FLASH_Lock();
}

static uint8_t erase_page_at(uint32_t page)
{
  App_KickWatchdog(); /* стирание страницы ~40 мс — с запасом против IWDG */
  FLASH_EraseInitTypeDef erase_init = {
    .TypeErase   = FLASH_TYPEERASE_PAGES,
    .PageAddress = page,
    .NbPages     = 1U,
  };
  uint32_t page_error = 0U;
  HAL_FLASH_Unlock();
  uint8_t ok = (HAL_FLASHEx_Erase(&erase_init, &page_error) == HAL_OK) ? 1U : 0U;
  HAL_FLASH_Lock();
  return ok;
}

/* Восстанавливает позицию записи по уже размещённой области: старший
 * seq однозначно указывает, где кольцо остановилось. */
static void rescan_ring(void)
{
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
    s_next_seq = 1U;
    s_next_slot = 0U;
  }
}

void EventLog_Init(void)
{
  memset(s_last_log_tick, 0, sizeof(s_last_log_tick));
  s_next_seq = 1U;
  s_next_slot = 0U;
  s_base = 0U;
  s_moving = 0U;

  /* Область уже есть? Storage_Init нашёл EVLH-заголовок — берём базу
   * оттуда (переживает и перенос, и обновление прошивки). */
  if (Storage_EvlogFound()) {
    s_base = Storage_EvlogBase();
    rescan_ring();
    /* Легаси-пул 0x0803C000 с живой областью — старые дубликаты:
     * выносим его (стирание + снятие резерва у storage.c). */
    (void)Storage_LegacyEvlogCleanup();
    EventLog_Add((uint8_t)EVLOG_BOOT, App_GetFaultCode(), App_GetResetFlags());
    return;
  }

  /* Области нет — создаём на каноничной позиции (ровно под VARH) или
   * на первой свободной полке, если каноничная занята триггерами. */
  uint32_t base = Storage_EvlogBase();
  if (base == 0U || !Storage_RangeFree(base, base + EVENT_LOG_POOL_PAGES
                                                   * EVENT_LOG_FLASH_PAGE)) {
    base = Storage_FindFreeRange(EVENT_LOG_POOL_PAGES);
  }
  if (base == 0U) {
    EventLog_Add((uint8_t)EVLOG_BOOT, App_GetFaultCode(), App_GetResetFlags());
    return; /* журнала нет — нестрашно, система работает без него */
  }
  uint8_t created = 1U;
  for (uint32_t p = 0U; p < EVENT_LOG_POOL_PAGES; p++) {
    if (!erase_page_at(base + p * EVENT_LOG_FLASH_PAGE)) {
      created = 0U;
      break;
    }
  }
  if (created) {
    s_base = base;
    s_generation = 1U;
    write_region_header(base, s_generation);
    Storage_NoteEvlogBase(base);

    /* Миграция легаси-журнала (0x0803C000, записи без заголовка):
     * валидные записи переписываем в новую область теми же слотами —
     * хронология и seq сохраняются. */
    uint32_t max_seq = 0U;
    for (uint16_t i = 0U; i < EVENT_LOG_LEGACY_SLOTS && i < EVENT_LOG_TOTAL_SLOTS; i++) {
      const wire_record_t *lr =
          (const wire_record_t *)(EVENT_LOG_LEGACY_BASE
                                  + (uint32_t)i * EVENT_LOG_RECORD_SIZE);
      if (lr->magic != EVENT_LOG_MAGIC || lr->seq == 0U
          || crc8((const uint8_t *)lr, offsetof(wire_record_t, crc8))
                 != lr->crc8) {
        continue;
      }
      HAL_FLASH_Unlock();
      const uint16_t *hw = (const uint16_t *)lr;
      for (uint32_t w = 0U; w < sizeof(wire_record_t) / 2U; w++) {
        program_u16(slot_addr(i) + w * 2U, hw[w]);
      }
      HAL_FLASH_Lock();
      if (lr->seq >= max_seq) {
        max_seq = lr->seq;
        s_next_slot = (uint16_t)((i + 1U) % EVENT_LOG_TOTAL_SLOTS);
      }
    }
    if (max_seq != 0U) {
      s_next_seq = max_seq + 1U;
    }
    /* Миграция завершена (полная или частичная): легаси-пул выносим —
     * иначе бутлоадер продолжал бы писать в мёртвый пул, а его страницы
     * висели бы резервом навсегда. */
    (void)Storage_LegacyEvlogCleanup();
  }
  EventLog_Add((uint8_t)EVLOG_BOOT, App_GetFaultCode(), App_GetResetFlags());
}

static uint8_t erase_page_for_slot(uint16_t slot)
{
  uint32_t page = slot_page(slot);
  if (!erase_page_at(page)) {
    return 0U;
  }
  /* Страница с заголовком EVLH после стирания получает его заново —
   * иначе область потеряла бы маркер и стала невидимой для скана. */
  if (page == s_base) {
    write_region_header(s_base, s_generation);
  }
  return 1U;
}

static void write_record(uint8_t type, uint8_t channel, uint8_t code,
                         uint32_t timestamp_ms)
{
  if (s_base == 0U || s_moving) {
    return;
  }
  wire_record_t rec;
  memset(&rec, 0, sizeof(rec));
  rec.magic = EVENT_LOG_MAGIC;
  rec.seq = s_next_seq;
  rec.timestamp_ms = timestamp_ms;
  rec.type = type;
  rec.channel = channel;
  rec.code = code;
  rec.crc8 = crc8((const uint8_t *)&rec, offsetof(wire_record_t, crc8));

  uint16_t slot = s_next_slot;
  if (!slot_is_blank(slot)) {
    /* Первая запись в эту страницу после ребута/оборота кольца: либо
     * чужие данные (страница только что перешла журналу), либо прошлый
     * круг записей. Оба случая требуют стирания перед программированием. */
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
  write_record(type, channel, code, now);
}

void EventLog_AddEx(uint8_t type, uint8_t channel, uint8_t code,
                    uint32_t aux_timestamp)
{
  write_record(type, channel, code, aux_timestamp);
}

uint8_t EventLog_Read(uint32_t after_seq, event_log_entry_t *out, uint8_t max_count)
{
  if (s_base == 0U) {
    return 0U;
  }
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

/* Дословный перенос области на new_base. Перекрытие src/dst разруливается
 * направлением обхода: смещение вниз — страницы по возрастанию (стираемый
 * dst уже скопирован либо свободен), вверх — по убыванию. Хвост старой
 * области вне новой стирается. */
uint8_t EventLog_Relocate(uint32_t new_base)
{
  if (s_base == 0U || new_base == s_base) {
    return 1U;
  }
  if (new_base % EVENT_LOG_FLASH_PAGE != 0U) {
    return 0U;
  }
  uint32_t bytes = EVENT_LOG_POOL_PAGES * EVENT_LOG_FLASH_PAGE;
  uint8_t ok = 1U;
  s_moving = 1U;
  if (new_base < s_base) {
    for (uint32_t i = 0U; i < EVENT_LOG_POOL_PAGES && ok; i++) {
      uint32_t dst = new_base + i * EVENT_LOG_FLASH_PAGE;
      uint32_t src = s_base + i * EVENT_LOG_FLASH_PAGE;
      if (!erase_page_at(dst)) {
        ok = 0U;
        break;
      }
      HAL_FLASH_Unlock();
      const uint16_t *src16 = (const uint16_t *)src;
      for (uint32_t w = 0U; w < EVENT_LOG_FLASH_PAGE / 2U; w++) {
        if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, dst + w * 2U,
                              src16[w]) != HAL_OK) {
          ok = 0U;
          break;
        }
      }
      HAL_FLASH_Lock();
    }
    /* Хвост старой области выше новой — зомби, вытираем. */
    if (ok && new_base + bytes < s_base + bytes) {
      for (uint32_t p = new_base + bytes; p < s_base + bytes;
           p += EVENT_LOG_FLASH_PAGE) {
        if (p >= s_base) {
          (void)erase_page_at(p);
        }
      }
    }
  } else {
    for (int32_t i = (int32_t)EVENT_LOG_POOL_PAGES - 1; i >= 0 && ok; i--) {
      uint32_t dst = new_base + (uint32_t)i * EVENT_LOG_FLASH_PAGE;
      uint32_t src = s_base + (uint32_t)i * EVENT_LOG_FLASH_PAGE;
      if (!erase_page_at(dst)) {
        ok = 0U;
        break;
      }
      HAL_FLASH_Unlock();
      const uint16_t *src16 = (const uint16_t *)src;
      for (uint32_t w = 0U; w < EVENT_LOG_FLASH_PAGE / 2U; w++) {
        if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, dst + w * 2U,
                              src16[w]) != HAL_OK) {
          ok = 0U;
          break;
        }
      }
      HAL_FLASH_Lock();
    }
    /* Голова старой области ниже новой — зомби. */
    if (ok && new_base > s_base) {
      for (uint32_t p = s_base; p < new_base && p < s_base + bytes;
           p += EVENT_LOG_FLASH_PAGE) {
        (void)erase_page_at(p);
      }
    }
  }
  if (ok) {
    s_base = new_base;
    Storage_NoteEvlogBase(new_base);
  }
  s_moving = 0U;
  return ok;
}
