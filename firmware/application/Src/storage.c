/* Маркерное хранилище конца Flash — см. Inc/storage.h для карты и
 * рационала. Порядок областей по адресам:
 *
 *   code_end | FLXH (ГЛ, растёт вверх) | ...зазор... | TRGH (триггеры,
 *   «посередине») | EVLH (журнал, 4 страницы) | VARH (переменные) |
 *   FLASH_END
 *
 * VARH прижата к верху Flash: каждый новый блоб пишется НИЖЕ прежних
 * поколений — цепочка растёт вниз, прежние поколения остаются историей
 * (и страховкой на обрыв записи: до коммита активна старая область).
 * FLXH зеркально прижата к концу кода и растёт вверх. Когда место для
 * очередного поколения кончается, область уплотняется: все поколения
 * стираются, цепочка начинается заново с якоря. EVLH всегда стоит ровно
 * под VARH, TRGH свободно плавает в зазоре — при коммите VARH/FLXH
 * пересекающиеся области пересаживаются (TRGH из RAM-копии списка,
 * EVLH дословным копированием страниц).
 *
 * Модуль держит только позиции областей и сессию записи блобов
 * VARH/FLXH; сами записи триггеров/журнала живут в своих модулях. */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "storage.h"
#include "trigger.h"
#include "event_log.h"

extern uint32_t _app_code_end; /* символ линкера: конец кода во Flash */

typedef struct {
  uint32_t base;        /* страница заголовка НОВЕЙШЕГО поколения */
  uint32_t end;         /* конец цепочки: VAR → FLASH_END, FLEX →
                         * конец новейшего блоба */
  uint32_t generation;
  uint32_t payload_len; /* байт полезной нагрузки новейшего блоба */
} region_t;

static region_t s_var;    /* VARH — цепочка у верхних адресов */
static region_t s_flex;   /* FLXH — цепочка сразу за кодом */
static region_t s_trig;   /* TRGH — найденная область триггеров */
static uint32_t s_evlog;  /* база EVLH (0 — область пока не создана) */
static uint32_t s_legacy_trig_end; /* легаси TRG2-регион резервируется
  до миграции, чтобы журнал/блобы на него не сели */
static uint32_t s_legacy_evlog_end; /* легаси-пул журнала 0x0803C000 —
  резерв до миграции записей в область EVLH */

/* Активная сессия записи блоба. */
static uint8_t  s_write_active;
static uint8_t  s_write_region;
static uint32_t s_write_base;
static uint32_t s_write_len;
static uint32_t s_write_tick;
#define STORE_WRITE_TIMEOUT_MS 15000U

static uint32_t s_code_end;

static uint8_t erase_pages(uint32_t from, uint32_t to);

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

static uint32_t crc32_of(const uint8_t *data, uint32_t len)
{
  uint32_t crc = 0xFFFFFFFFUL;
  for (uint32_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8U; bit++) {
      crc = (crc & 0x80000000UL) ? (crc << 1) ^ 0x04C11DB7UL : crc << 1;
    }
  }
  return crc ^ 0xFFFFFFFFUL;
}

static uint32_t round_page_up(uint32_t addr)
{
  return (addr + STORE_FLASH_PAGE - 1U) & ~(STORE_FLASH_PAGE - 1U);
}

static uint8_t header_valid(const store_header_t *h, uint32_t magic)
{
  if (h->magic != magic || h->version != STORE_VERSION) {
    return 0U;
  }
  if (h->payload_len > STORE_MAX_PAYLOAD) {
    return 0U;
  }
  return crc8((const uint8_t *)h, offsetof(store_header_t, crc8)) == h->crc8;
}

/* Размер области блоба в страницах (заголовок + payload). */
static uint32_t blob_pages(uint32_t payload_len)
{
  return (STORE_HEADER_SIZE + payload_len + STORE_FLASH_PAGE - 1U)
         / STORE_FLASH_PAGE;
}

static uint32_t flex_end(void)
{
  return (s_flex.base != 0U) ? s_flex.end : s_code_end;
}

static uint32_t var_base(void)
{
  return (s_var.base != 0U) ? s_var.base : STORE_FLASH_END;
}

/* Каноничная позиция журнала — ровно под областью переменных.
 * Резервируется даже когда журнал ещё не создан, чтобы триггеры не
 * заняли его место. */
static uint32_t evlog_base(void)
{
  return (s_evlog != 0U) ? s_evlog
                         : (var_base() - STORE_EVLOG_PAGES * STORE_FLASH_PAGE);
}

static uint32_t evlog_end(void)
{
  return evlog_base() + STORE_EVLOG_PAGES * STORE_FLASH_PAGE;
}

void Storage_Init(void)
{
  /* Конец кода приложения: LOADADDR(.data)+SIZEOF(.data) — за ним
   * идёт свободное пространство до конца Flash. */
  s_code_end = round_page_up((uint32_t)&_app_code_end);
  if (s_code_end < STORE_APP_CODE_START + STORE_FLASH_PAGE) {
    s_code_end = STORE_APP_CODE_START + STORE_FLASH_PAGE;
  }
  if (s_code_end >= STORE_FLASH_END) {
    s_code_end = STORE_APP_CODE_START; /* страховка от битой линковки */
  }

  /* Скан по страницам: заголовки VARH/FLXH/EVLH/TRGH различаются по
   * magic. Дубликаты поколений одной области (append-запись, обрыв
   * уплотнения) разрешаются большим generation — модуль запоминает
   * только новейшее, старшие поколения остаются историей. */
  uint32_t best_trig_gen = 0U;
  for (uint32_t page = s_code_end; page < STORE_FLASH_END;
       page += STORE_FLASH_PAGE) {
    const store_header_t *h = (const store_header_t *)page;
    if (header_valid(h, STORE_MAGIC_VAR)) {
      uint32_t end = page + blob_pages(h->payload_len) * STORE_FLASH_PAGE;
      if (end <= STORE_FLASH_END
          && (s_var.base == 0U || h->generation >= s_var.generation)) {
        s_var.base = page;
        s_var.end = STORE_FLASH_END; /* цепочка занимает верх до конца */
        s_var.generation = h->generation;
        s_var.payload_len = h->payload_len;
      }
    } else if (header_valid(h, STORE_MAGIC_FLEX)) {
      uint32_t end = page + blob_pages(h->payload_len) * STORE_FLASH_PAGE;
      if (end <= STORE_FLASH_END
          && (s_flex.base == 0U || h->generation >= s_flex.generation)) {
        s_flex.base = page;
        s_flex.end = end;
        s_flex.generation = h->generation;
        s_flex.payload_len = h->payload_len;
      }
    } else if (header_valid(h, STORE_MAGIC_EVLOG)) {
      s_evlog = page;
    } else {
      /* TRGH имеет свой 16-байтный заголовок (trigger.h) — для
       * раскладки достаточно magic+version+count: шаг записи выбирается
       * по версии хранилища, crc8 закрывает заголовок. */
      const trigger_header_t *th = (const trigger_header_t *)page;
      if (th->magic == TRIGGER_HEADER_MAGIC
          && (th->version == TRIGGER_STORE_VERSION
              || th->version == TRIGGER_STORE_VERSION_V3)
          && th->count <= TRIGGER_MAX_RECORDS
          && crc8((const uint8_t *)th, offsetof(trigger_header_t, crc8))
                 == th->crc8) {
        uint32_t stride = (th->version == TRIGGER_STORE_VERSION)
                              ? TRIGGER_RECORD_SIZE : TRIGGER_RECORD_SIZE_V2;
        uint32_t end = page + TRIGGER_HEADER_SIZE
                     + (uint32_t)th->count * stride;
        end = round_page_up(end);
        if (end <= STORE_FLASH_END
            && (s_trig.base == 0U || th->generation >= best_trig_gen)) {
          s_trig.base = page;
          s_trig.end = end;
          s_trig.generation = th->generation;
          best_trig_gen = th->generation;
        }
      } else if (page == TRIGGER_LEGACY_ADDR
                 && *(const uint32_t *)page == TRIGGER_MAGIC) {
        /* Легаси-регион триггеров v2 (сырые записи TRG2 без заголовка)
         * — резервируем его страницы до миграции в Trigger_Init, чтобы
         * журнал/блобы на него не сели. */
        s_legacy_trig_end = round_page_up(
            TRIGGER_LEGACY_ADDR
            + TRIGGER_LEGACY_COUNT * TRIGGER_RECORD_SIZE_V2);
      } else if (page == EVENT_LOG_LEGACY_BASE
                 && *(const uint16_t *)page == 0x4C45U /* "EL" */) {
        /* Легаси-пул журнала (записи без заголовка EVLH) — резерв до
         * миграции в EventLog_Init: размещение триггеров в зазоре не
         * должно стереть ещё не скопированную историю. */
        s_legacy_evlog_end = EVENT_LOG_LEGACY_BASE
            + EVENT_LOG_LEGACY_SLOTS * EVENT_LOG_RECORD_SIZE;
      }
    }
  }
}

uint32_t Storage_CodeEnd(void)
{
  return s_code_end;
}

uint32_t Storage_VarBase(void)
{
  return s_var.base;
}

uint32_t Storage_VarEnd(void)
{
  return (s_var.base != 0U) ? STORE_FLASH_END : 0U;
}

uint32_t Storage_FlexBase(void)
{
  return s_code_end;
}

uint32_t Storage_FlexEnd(void)
{
  return flex_end();
}

uint32_t Storage_EvlogBase(void)
{
  return evlog_base();
}

uint32_t Storage_EvlogEnd(void)
{
  return evlog_end();
}

uint8_t Storage_EvlogFound(void)
{
  return (s_evlog != 0U) ? 1U : 0U;
}

uint32_t Storage_TrigBase(void)
{
  return s_trig.base;
}

uint32_t Storage_TrigEnd(void)
{
  return s_trig.end;
}

/* Зазор «посередине», где живут триггеры: [flex_end, evlog_base). */
uint32_t Storage_TrigGapBase(void)
{
  return flex_end();
}

uint32_t Storage_TrigGapEnd(void)
{
  return evlog_base();
}

/* Занят ли диапазон [from, to) чужой областью. Цепочки поколений
 * считаются единым спаном: FLXH занимает [code_end, flex_end), VARH —
 * [var_base, FLASH_END) — история поколений не отдаётся под чужие
 * размещения, пока область не уплотнена/стёрта. */
static uint8_t range_busy(uint32_t from, uint32_t to)
{
  if (from >= to) {
    return 0U;
  }
  if (s_flex.base != 0U && from < s_flex.end && to > s_code_end) {
    return 1U;
  }
  if (s_var.base != 0U && to > s_var.base) {
    return 1U;
  }
  if (s_evlog != 0U && from < evlog_end() && to > s_evlog) {
    return 1U;
  }
  if (s_trig.base != 0U && from < s_trig.end && to > s_trig.base) {
    return 1U;
  }
  if (s_legacy_trig_end != 0U && from < s_legacy_trig_end
      && to > TRIGGER_LEGACY_ADDR) {
    return 1U;
  }
  if (s_legacy_evlog_end != 0U && from < s_legacy_evlog_end
      && to > EVENT_LOG_LEGACY_BASE) {
    return 1U;
  }
  return 0U;
}

uint8_t Storage_RangeFree(uint32_t from, uint32_t to)
{
  return range_busy(from, to) ? 0U : 1U;
}

/* Занятость диапазона без учёта собственной области триггеров — для
 * выбора места пересадки TRGH (своя старая область перезаписывается). */
static uint8_t range_busy_except_trig(uint32_t from, uint32_t to)
{
  uint32_t save_base = s_trig.base;
  uint32_t save_end = s_trig.end;
  s_trig.base = 0U;
  s_trig.end = 0U;
  uint8_t busy = range_busy(from, to);
  s_trig.base = save_base;
  s_trig.end = save_end;
  return busy;
}

/* Первая свободная полка под pages страниц — для первичного размещения
 * журнала, если каноничная позиция занята. */
uint32_t Storage_FindFreeRange(uint32_t pages)
{
  uint32_t need = pages * STORE_FLASH_PAGE;
  for (uint32_t base = s_code_end; base + need <= STORE_FLASH_END;
       base += STORE_FLASH_PAGE) {
    if (!range_busy(base, base + need)) {
      return base;
    }
  }
  return 0U;
}

void Storage_NoteTrig(uint32_t base, uint32_t pages)
{
  s_trig.base = base;
  s_trig.end = base + pages * STORE_FLASH_PAGE;
  if (base == 0U) {
    s_trig.end = 0U;
  }
}

void Storage_NoteEvlogBase(uint32_t base)
{
  s_evlog = base;
}

/* trigger.c сообщает, что легаси-регион смигрирован/вытёрт — резерв
 * снимается, страницы возвращаются зазору. */
void Storage_NoteLegacyTrigDone(void)
{
  s_legacy_trig_end = 0U;
}

/* event_log.c сообщает, что легаси-пул журнала смигрирован/вытёрт —
 * резерв снимается. */
void Storage_NoteLegacyEvlogDone(void)
{
  s_legacy_evlog_end = 0U;
}

/* Вынос легаси-пула журнала: записи уже в области EVLH — страницы
 * пула стираются и резерв снимается. Стирание только если на пул не
 * села другая область (TRGH могла встать на пустой пул до создания
 * резерва у старой прошивки) — тогда пул остаётся чужой областью и
 * резерв тоже снимаем: область теперь принадлежит триггерам. */
uint8_t Storage_LegacyEvlogCleanup(void)
{
  uint32_t end = EVENT_LOG_LEGACY_BASE
      + EVENT_LOG_LEGACY_SLOTS * EVENT_LOG_RECORD_SIZE;
  if (*(const uint16_t *)EVENT_LOG_LEGACY_BASE == 0x4C45U /* "EL" */) {
    uint8_t overlaps = 0U;
    if (s_trig.base != 0U && s_trig.base < end
        && s_trig.end > EVENT_LOG_LEGACY_BASE) {
      overlaps = 1U;
    }
    if (s_evlog != 0U && s_evlog < end
        && evlog_end() > EVENT_LOG_LEGACY_BASE) {
      overlaps = 1U;
    }
    if ((s_var.base != 0U && s_var.base < end)
        || (s_flex.base != 0U && s_flex.end > EVENT_LOG_LEGACY_BASE)) {
      overlaps = 1U;
    }
    if (!overlaps) {
      (void)erase_pages(EVENT_LOG_LEGACY_BASE, round_page_up(end));
    }
  }
  s_legacy_evlog_end = 0U;
  return 1U;
}

uint8_t Storage_Read(uint8_t region, uint32_t offset, uint8_t *out,
                     uint32_t len)
{
  const region_t *r = (region == STORE_REGION_VAR) ? &s_var : &s_flex;
  if (r->base == 0U
      || (uint64_t)offset + len > (uint64_t)r->payload_len) {
    return 0U;
  }
  const uint8_t *src = (const uint8_t *)(r->base + STORE_HEADER_SIZE + offset);
  memcpy(out, src, len);
  return 1U;
}

uint8_t Storage_RegionInfo(uint8_t region, uint32_t *base_out,
                           uint32_t *len_out, uint32_t *gen_out)
{
  const region_t *r = (region == STORE_REGION_VAR) ? &s_var : &s_flex;
  if (r->base == 0U) {
    return 0U;
  }
  *base_out = r->base;
  *len_out = r->payload_len;
  *gen_out = r->generation;
  return 1U;
}

static uint8_t erase_pages(uint32_t from, uint32_t to)
{
  HAL_FLASH_Unlock();
  for (uint32_t page = from; page < to; page += STORE_FLASH_PAGE) {
    App_KickWatchdog(); /* стирание страницы ~40 мс — кормим IWDG */
    const uint32_t *p = (const uint32_t *)page;
    uint8_t blank = 1U;
    for (uint32_t i = 0U; i < STORE_FLASH_PAGE / 4U; i++) {
      if (p[i] != 0xFFFFFFFFUL) {
        blank = 0U;
        break;
      }
    }
    if (blank) {
      continue;
    }
    FLASH_EraseInitTypeDef erase_init = {
      .TypeErase   = FLASH_TYPEERASE_PAGES,
      .PageAddress = page,
      .NbPages     = 1U,
    };
    uint32_t page_error = 0U;
    if (HAL_FLASHEx_Erase(&erase_init, &page_error) != HAL_OK) {
      HAL_FLASH_Lock();
      return 0U;
    }
  }
  HAL_FLASH_Lock();
  return 1U;
}

/* Центр зазора для триггеров — «где-то посередине», выравнивание вниз. */
static uint32_t middle_base(uint32_t gap_from, uint32_t gap_to,
                            uint32_t need_pages)
{
  uint32_t need = need_pages * STORE_FLASH_PAGE;
  if (gap_to < gap_from + need) {
    return 0U;
  }
  return (gap_from + ((gap_to - gap_from - need) / 2U))
         & ~(STORE_FLASH_PAGE - 1U);
}

/* Освобождает целевой диапазон для новой области: пересекающиеся
 * подвижные регионы (TRGH — из RAM-копии, EVLH — дословным копированием)
 * пересаживаются. */
static uint8_t relocate_overlaps(uint8_t region, uint32_t new_from,
                                 uint32_t new_to)
{
  if (region == STORE_REGION_VAR) {
    /* VAR прижата к верху: журнал обязан сидеть прямо под новой
     * областью, триггеры — ниже журнала. */
    uint32_t evlog_target = new_from - STORE_EVLOG_PAGES * STORE_FLASH_PAGE;
    if (evlog_target < flex_end()) {
      return 0U; /* переменные съели бы всё пространство — отказ */
    }
    if (s_trig.base != 0U && s_trig.end > evlog_target) {
      /* Триггеры наехали на позицию журнала — пересадить в зазор
       * [flex_end, evlog_target). */
      uint32_t need_pages = (s_trig.end - s_trig.base) / STORE_FLASH_PAGE;
      uint32_t trig_new = middle_base(flex_end(), evlog_target, need_pages);
      if (trig_new == 0U
          || range_busy_except_trig(trig_new,
                                    trig_new + need_pages * STORE_FLASH_PAGE)
          || !Trigger_Relocate(trig_new)) {
        return 0U;
      }
      s_trig.base = trig_new;
      s_trig.end = trig_new + need_pages * STORE_FLASH_PAGE;
    }
    if (s_evlog != 0U && s_evlog != evlog_target) {
      if (!EventLog_Relocate(evlog_target)) {
        return 0U;
      }
      s_evlog = evlog_target;
    }
  } else {
    /* FLXH прижата к концу кода: новая область растёт вверх. Сначала
     * поднимаем журнал в каноничную позицию под VAR (если его задела
     * область), потом пересаживаем триггеры в зазор [new_to,
     * evlog_base) — по старой позиции журнала зазор мог оказаться
     * нулевым, хотя после подъёма журнала место есть. Триггеры,
     * сидящие на месте будущего журнала, пересаживаем ДО копирования
     * EVLH — иначе дословный перенос журнала затрёт их страницы. */
    uint32_t evlog_target = 0U;
    if (s_evlog != 0U && s_evlog < new_to) {
      evlog_target = var_base() - STORE_EVLOG_PAGES * STORE_FLASH_PAGE;
      if (evlog_target < new_to) {
        return 0U;
      }
      uint32_t evlog_end_target = evlog_target
          + STORE_EVLOG_PAGES * STORE_FLASH_PAGE;
      if (s_trig.base != 0U && s_trig.base < evlog_end_target
          && s_trig.end > evlog_target) {
        uint32_t need_pages =
            (s_trig.end - s_trig.base) / STORE_FLASH_PAGE;
        /* Зазор под триггеры не должен пересекать СТАРУЮ позицию
         * журнала — записи EVLH ещё не скопированы, область там
         * живая и служит источником переноса. */
        uint32_t gap_from = new_to;
        uint32_t evlog_old_end = s_evlog
            + STORE_EVLOG_PAGES * STORE_FLASH_PAGE;
        if (evlog_old_end > gap_from) {
          gap_from = evlog_old_end;
        }
        uint32_t trig_new =
            middle_base(gap_from, evlog_target, need_pages);
        if (trig_new == 0U
            || range_busy_except_trig(
                trig_new, trig_new + need_pages * STORE_FLASH_PAGE)
            || !Trigger_Relocate(trig_new)) {
          return 0U;
        }
        s_trig.base = trig_new;
        s_trig.end = trig_new + need_pages * STORE_FLASH_PAGE;
      }
      if (!EventLog_Relocate(evlog_target)) {
        return 0U;
      }
      s_evlog = evlog_target;
    }
    if (s_trig.base != 0U && s_trig.base < new_to) {
      uint32_t need_pages = (s_trig.end - s_trig.base) / STORE_FLASH_PAGE;
      uint32_t trig_new = middle_base(new_to, evlog_base(), need_pages);
      if (trig_new == 0U
          || range_busy_except_trig(trig_new,
                                    trig_new + need_pages * STORE_FLASH_PAGE)
          || !Trigger_Relocate(trig_new)) {
        return 0U;
      }
      s_trig.base = trig_new;
      s_trig.end = trig_new + need_pages * STORE_FLASH_PAGE;
    }
  }
  return 1U;
}

/* Свободен ли целевой диапазон после пересадки пересекающихся областей. */
static uint8_t prepare_range(uint8_t region, uint32_t from, uint32_t to)
{
  if (range_busy(from, to)) {
    if (!relocate_overlaps(region, from, to) || range_busy(from, to)) {
      return 0U;
    }
  }
  return 1U;
}

/* Уплотнение области: стирает ВСЕ поколения (скан по маркерам, включая
 * осиротевшие) и сбрасывает позицию. Вызывается когда место для
 * очередного поколения кончилось — цепочка начинается с якоря заново. */
static uint8_t compact_region(uint8_t region)
{
  uint32_t magic = (region == STORE_REGION_VAR) ? STORE_MAGIC_VAR
                                              : STORE_MAGIC_FLEX;
  for (uint32_t page = s_code_end; page < STORE_FLASH_END;
       page += STORE_FLASH_PAGE) {
    const store_header_t *h = (const store_header_t *)page;
    if (!header_valid(h, magic)) {
      continue;
    }
    uint32_t end = page + blob_pages(h->payload_len) * STORE_FLASH_PAGE;
    if (end > STORE_FLASH_END) {
      end = STORE_FLASH_END;
    }
    if (!erase_pages(page, end)) {
      return 0U;
    }
  }
  if (region == STORE_REGION_VAR) {
    memset(&s_var, 0, sizeof(s_var));
  } else {
    memset(&s_flex, 0, sizeof(s_flex));
  }
  return 1U;
}

uint8_t Storage_BeginWrite(uint8_t region, uint32_t payload_len)
{
  if (payload_len == 0U || payload_len > STORE_MAX_PAYLOAD
      || (region != STORE_REGION_VAR && region != STORE_REGION_FLEX)
      || s_write_active) {
    return 0U;
  }
  uint32_t pages = blob_pages(payload_len);
  uint32_t need = pages * STORE_FLASH_PAGE;
  /* Append: новый блоб — соседней полкой к новейшему поколению (VAR вниз
   * от нижнего края цепочки, FLEX вверх от верхнего). Прежние поколения
   * до коммита остаются целыми и рабочими — обрыв записи не теряет
   * конфигурацию. */
  uint32_t base;
  uint8_t fits;
  if (region == STORE_REGION_VAR) {
    uint32_t anchor = (s_var.base != 0U) ? s_var.base : STORE_FLASH_END;
    fits = (anchor >= need && anchor - need >= s_code_end) ? 1U : 0U;
    base = anchor - need;
  } else {
    base = (s_flex.base != 0U) ? s_flex.end : s_code_end;
    fits = (base + need <= STORE_FLASH_END) ? 1U : 0U;
  }
  if (fits) {
    fits = prepare_range(region, base, base + need);
  }
  if (!fits) {
    /* Места для очередного поколения нет — уплотняем область и пишем
     * на якорь. Единственная точка, где история поколений сбрасывается;
     * обрыв записи после уплотнения теряет блоб (ПК перезапишет). */
    if (!compact_region(region)) {
      return 0U;
    }
    base = (region == STORE_REGION_VAR) ? STORE_FLASH_END - need
                                        : s_code_end;
    if (base < s_code_end || base + need > STORE_FLASH_END
        || !prepare_range(region, base, base + need)) {
      return 0U;
    }
  }
  if (!erase_pages(base, base + need)) {
    return 0U;
  }
  s_write_active = 1U;
  s_write_region = region;
  s_write_base = base;
  s_write_len = payload_len;
  s_write_tick = HAL_GetTick();
  return 1U;
}

/* База активной сессии записи — для ответа CMD_STORAGE_BEGIN. */
uint32_t Storage_WriteBase(void)
{
  return s_write_active ? s_write_base : 0U;
}

uint8_t Storage_WriteChunk(uint32_t offset, const uint8_t *data,
                           uint32_t len)
{
  if (!s_write_active || len == 0U || offset + len > s_write_len) {
    return 0U;
  }
  if ((HAL_GetTick() - s_write_tick) > STORE_WRITE_TIMEOUT_MS) {
    s_write_active = 0U;
    return 0U;
  }
  uint32_t addr = s_write_base + STORE_HEADER_SIZE + offset;
  HAL_FLASH_Unlock();
  for (uint32_t i = 0U; i < len; i += 2U) {
    uint16_t hw = (uint16_t)data[i];
    if (i + 1U < len) {
      hw |= (uint16_t)((uint16_t)data[i + 1U] << 8);
    } else {
      hw |= 0xFF00U; /* хвост нечётной длины — стёртое состояние */
    }
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, hw) != HAL_OK) {
      HAL_FLASH_Lock();
      s_write_active = 0U;
      return 0U;
    }
    addr += 2U;
  }
  HAL_FLASH_Lock();
  s_write_tick = HAL_GetTick();
  return 1U;
}

uint8_t Storage_Commit(uint32_t payload_crc)
{
  if (!s_write_active) {
    return 0U;
  }
  s_write_active = 0U;
  /* CRC полезной нагрузки проверяется до записи заголовка — битая
   * запись не становится видимой. */
  const uint8_t *payload =
      (const uint8_t *)(s_write_base + STORE_HEADER_SIZE);
  if (crc32_of(payload, s_write_len) != payload_crc) {
    return 0U;
  }
  store_header_t h;
  memset(&h, 0, sizeof(h));
  h.magic = (s_write_region == STORE_REGION_VAR) ? STORE_MAGIC_VAR
                                               : STORE_MAGIC_FLEX;
  const region_t *own = (s_write_region == STORE_REGION_VAR) ? &s_var
                                                             : &s_flex;
  h.generation = own->generation + 1U;
  h.payload_len = s_write_len;
  h.payload_crc = payload_crc;
  h.version = STORE_VERSION;
  h.region = s_write_region;
  h.crc8 = crc8((const uint8_t *)&h, offsetof(store_header_t, crc8));

  HAL_FLASH_Unlock();
  uint32_t addr = s_write_base;
  const uint16_t *hw = (const uint16_t *)&h;
  for (uint32_t w = 0U; w < sizeof(h) / 2U; w++) {
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, hw[w]) != HAL_OK) {
      HAL_FLASH_Lock();
      return 0U;
    }
    addr += 2U;
  }
  HAL_FLASH_Lock();
  if (memcmp((const void *)s_write_base, &h, sizeof(h)) != 0) {
    return 0U;
  }
  if (s_write_region == STORE_REGION_VAR) {
    s_var.base = s_write_base;
    s_var.end = STORE_FLASH_END;
    s_var.generation = h.generation;
    s_var.payload_len = s_write_len;
  } else {
    s_flex.base = s_write_base;
    s_flex.end = s_write_base + blob_pages(s_write_len) * STORE_FLASH_PAGE;
    s_flex.generation = h.generation;
    s_flex.payload_len = s_write_len;
  }
  return 1U;
}

void Storage_Poll(void)
{
  if (s_write_active
      && (HAL_GetTick() - s_write_tick) > STORE_WRITE_TIMEOUT_MS) {
    s_write_active = 0U;
  }
}

uint8_t Storage_Clear(uint8_t region)
{
  if (region != STORE_REGION_VAR && region != STORE_REGION_FLEX) {
    return 0U;
  }
  return compact_region(region);
}
