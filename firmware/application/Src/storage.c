/* Маркерное хранилище конца Flash — см. Inc/storage.h для карты и
 * рационала. Порядок областей по адресам (отчёт мастера):
 *
 *   code_end | CFGH (настройки приложения, 1 стр.) | FLXH (ГЛ, растёт
 *   вверх) | ...зазор... | TRGH (триггеры — заканчиваются на центре
 *   зазора, растут вниз) | EVLH (журнал, 4 стр. — начинается от центра,
 *   лежит к VARH) | ...зазор... | VARH (переменные) | FLASH_END
 *
 * VARH прижата к верху Flash: каждый новый блоб пишется НИЖЕ прежних
 * поколений — цепочка растёт вниз, прежние поколения остаются историей
 * (и страховкой на обрыв записи: до коммита активна старая область).
 * FLXH прижата к концу страницы CFGH и растёт вверх. Когда место для
 * очередного поколения кончается, область уплотняется: все поколения
 * стираются, цепочка начинается заново с якоря. Центр зазора делит
 * середину между TRGH и EVLH — при коммите VARH/FLXH пересекающиеся
 * области пересаживаются (TRGH из RAM-копии списка, EVLH дословным
 * копированием страниц).
 *
 * Модуль держит позиции областей, сессию записи блобов VARH/FLXH и
 * сервисные функции для CFGH (запись ведёт device_config.c); сами
 * записи триггеров/журнала живут в своих модулях. */

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
static region_t s_flex;   /* FLXH — цепочка сразу за областью CFGH */
static region_t s_cfg;    /* CFGH — найденная область настроек (чужая
                           * копия может сидеть не на каноничной
                           * странице — переезд при росте/усадке кода) */
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
  /* Совместим с zlib.crc32 (CRC-32/ISO-HDLC): рефлективный полином
   * 0xEDB88320, init/xorout 0xFFFFFFFF — ПК считает именно им.
   * Раньше здесь был MSB-first вариант (полином 0x04C11DB7): COMMIT
   * отклонялся статусом 0x02, заголовок области не записывался —
   * payload лежал на Flash, но область была невидима (отчёт мастера:
   * переменные «сохранены», после перезапуска не читаются). */
  uint32_t crc = 0xFFFFFFFFUL;
  for (uint32_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8U; bit++) {
      crc = (crc & 1U) ? (crc >> 1) ^ 0xEDB88320UL : crc >> 1;
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

/* Страница CFGH резервируется за кодом ВСЕГДА (даже когда область ещё
 * не записана) — «настройки сразу после кода» держат якорь FLXH
 * стабильным и не дают чужим областям сесть на это место. */
static uint32_t cfg_end(void)
{
  return s_code_end + STORE_CFG_PAGES * STORE_FLASH_PAGE;
}

static uint32_t flex_end(void)
{
  return (s_flex.base != 0U) ? s_flex.end : cfg_end();
}

static uint32_t var_base(void)
{
  return (s_var.base != 0U) ? s_var.base : STORE_FLASH_END;
}

/* Центр свободного зазора [flex_end, var_base) — точка стыковки TRGH
 * и EVLH: триггеры заканчиваются на центре, журнал начинается с него.
 * Центр прижимается вниз так, чтобы [center, center+4p) всегда
 * влезала до var_base. */
static uint32_t gap_center(void)
{
  uint32_t lo = flex_end();
  uint32_t hi = var_base();
  if (hi <= lo) {
    return lo;
  }
  uint32_t mid = lo + ((hi - lo) / 2U);
  mid &= ~(STORE_FLASH_PAGE - 1U);
  uint32_t evlog_span = STORE_EVLOG_PAGES * STORE_FLASH_PAGE;
  if (mid + evlog_span > hi) {
    mid = (hi >= lo + evlog_span) ? hi - evlog_span : lo;
  }
  if (mid < lo) {
    mid = lo;
  }
  return mid;
}

/* Каноничная позиция журнала — центр зазора («сверху вниз от центра»,
 * отчёт мастера). Резервируется даже когда журнал ещё не создан, чтобы
 * триггеры не заняли его место: они живут ниже центра. */
static uint32_t evlog_base(void)
{
  return (s_evlog != 0U) ? s_evlog : gap_center();
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
    } else if (header_valid(h, STORE_MAGIC_CFG)) {
      uint32_t end = page + blob_pages(h->payload_len) * STORE_FLASH_PAGE;
      if (end <= STORE_FLASH_END
          && (s_cfg.base == 0U || h->generation >= s_cfg.generation)) {
        s_cfg.base = page;
        s_cfg.end = end;
        s_cfg.generation = h->generation;
        s_cfg.payload_len = h->payload_len;
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

uint32_t Storage_CfgBase(void)
{
  return s_code_end;
}

uint32_t Storage_CfgEnd(void)
{
  return cfg_end();
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
  return cfg_end();
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

uint32_t Storage_EvlogCanonicalBase(void)
{
  return gap_center();
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
  /* Страница CFGH за кодом зарезервирована всегда — будь область уже
   * записана или ещё нет. Найденная копия в другом месте (старое
   * code_end после роста кода) тоже занята до перезаписи. */
  if (from < cfg_end() && to > s_code_end) {
    return 1U;
  }
  if (s_cfg.base != 0U && s_cfg.base != s_code_end
      && from < s_cfg.end && to > s_cfg.base) {
    return 1U;
  }
  if (s_flex.base != 0U && from < s_flex.end && to > cfg_end()) {
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

/* Занятость без учёта TRGH и EVLH — они сами пересаживаются при росте
 * соседей (TRGH из RAM-копии, EVLH дословным копированием). Легаси-
 * резервы и чужие цепочки поколений при этом считаются занятыми. */
static uint8_t range_busy_except_trig_evlog(uint32_t from, uint32_t to)
{
  uint32_t tb = s_trig.base;
  uint32_t te = s_trig.end;
  uint32_t ev = s_evlog;
  s_trig.base = 0U;
  s_trig.end = 0U;
  s_evlog = 0U;
  uint8_t busy = range_busy(from, to);
  s_trig.base = tb;
  s_trig.end = te;
  s_evlog = ev;
  return busy;
}

/* Первая свободная полка под need байт внутри [from, to) — только
 * «твёрдые» области считаются занятыми (TRGH/EVLH подвижны). */
static uint32_t find_free_in(uint32_t from, uint32_t to, uint32_t need)
{
  for (uint32_t base = from; base + need <= to;
       base += STORE_FLASH_PAGE) {
    if (!range_busy_except_trig_evlog(base, base + need)) {
      return base;
    }
  }
  return 0U;
}

/* Первая свободная полка под pages страниц — для первичного размещения
 * журнала, если каноничная позиция занята. */
uint32_t Storage_FindFreeRange(uint32_t pages)
{
  uint32_t need = pages * STORE_FLASH_PAGE;
  for (uint32_t base = cfg_end(); base + need <= STORE_FLASH_END;
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

/* Каноничный центр зазора ПОСЛЕ записи нового блоба [new_from, new_to):
 * для VAR зазор снизу прижат к flex_end, для FLEX — сверху к var_base. */
static uint32_t new_gap_center(uint8_t region, uint32_t new_from,
                             uint32_t new_to)
{
  uint32_t lo = (region == STORE_REGION_VAR) ? flex_end() : new_to;
  uint32_t hi = (region == STORE_REGION_VAR) ? new_from : var_base();
  if (hi <= lo) {
    return lo;
  }
  uint32_t mid = lo + ((hi - lo) / 2U);
  mid &= ~(STORE_FLASH_PAGE - 1U);
  uint32_t span = STORE_EVLOG_PAGES * STORE_FLASH_PAGE;
  if (mid + span > hi) {
    mid = (hi >= lo + span) ? hi - span : lo;
  }
  if (mid < lo) {
    mid = lo;
  }
  return mid;
}

/* Освобождает целевой диапазон для новой области: пересекающиеся
 * подвижные регионы (TRGH — из RAM-копии, EVLH — дословным копированием)
 * пересаживаются. Центр зазора пересчитывается по НОВЫМ границам:
 * EVLH стартует от центра (сверху вниз → вверх по адресам), TRGH
 * заканчивается на центре (снизу вверх → вниз по адресам). */
static uint8_t relocate_overlaps(uint8_t region, uint32_t new_from,
                                 uint32_t new_to)
{
  uint32_t center = new_gap_center(region, new_from, new_to);
  uint32_t evlog_span = STORE_EVLOG_PAGES * STORE_FLASH_PAGE;
  uint32_t evlog_target = center; /* EVLH начинается от центра */

  /* 1) Журнал на центр. Если целевая позиция занята триггерами —
   * пересадить их СНАЧАЛА под центр (триггеры растут вниз от центра);
   * копия списка в RAM, затереть их страницы нельзя до записи копии. */
  if (s_evlog != 0U && s_evlog != evlog_target) {
    if (s_trig.base != 0U
        && s_trig.base < evlog_target + evlog_span
        && s_trig.end > evlog_target) {
      /* Триггеры наехали на цель журнала — под центр: область
       * заканчивается ровно на evlog_target («снизу вверх от центра»). */
      uint32_t tneed = s_trig.end - s_trig.base;
      uint32_t t_lo = (region == STORE_REGION_VAR) ? flex_end() : new_to;
      uint32_t tbase = (evlog_target >= t_lo + tneed)
                       ? evlog_target - tneed : 0U;
      if (tbase == 0U
          || range_busy_except_trig_evlog(tbase, tbase + tneed)
          || !Trigger_Relocate(tbase)) {
        return 0U;
      }
      s_trig.base = tbase;
      s_trig.end = tbase + tneed;
    }
    if (!EventLog_Relocate(evlog_target)) {
      return 0U;
    }
    s_evlog = evlog_target;
  }

  /* 2) Триггеры, задетые новым блобом, — под центр (заканчиваются на
   * evlog_base: журнал уже пересажен или встанет туда при Init). */
  if (s_trig.base != 0U && s_trig.base < new_to && s_trig.end > new_from) {
    uint32_t tneed = s_trig.end - s_trig.base;
    uint32_t top = (s_evlog != 0U) ? s_evlog : evlog_base();
    uint32_t t_lo = (region == STORE_REGION_VAR) ? flex_end() : new_to;
    uint32_t tbase = (top >= t_lo + tneed) ? top - tneed : 0U;
    if (tbase != 0U
        && ((tbase < new_to && tbase + tneed > new_from)
            || range_busy_except_trig_evlog(tbase, tbase + tneed))) {
      tbase = 0U;
    }
    if (tbase == 0U) {
      /* Не влезает вплотную под центр — любая свободная полка зазора. */
      uint32_t glo = (region == STORE_REGION_VAR) ? flex_end() : new_to;
      uint32_t ghi = (region == STORE_REGION_VAR) ? new_from : var_base();
      tbase = find_free_in(glo, ghi, tneed);
    }
    if (tbase == 0U || !Trigger_Relocate(tbase)) {
      return 0U;
    }
    s_trig.base = tbase;
    s_trig.end = tbase + tneed;
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
    fits = (anchor >= need && anchor - need >= cfg_end()) ? 1U : 0U;
    base = anchor - need;
  } else {
    /* FLXH якорится за областью CFGH (страница настроек сразу после
     * кода), а не за голым концом кода. */
    base = (s_flex.base != 0U) ? s_flex.end : cfg_end();
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
                                        : cfg_end();
    if (base < cfg_end() || base + need > STORE_FLASH_END
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

/* --- CFGH: настройки приложения сразу за кодом (владелец —
 * device_config.c) ------------------------------------------------- */

uint32_t Storage_CfgFoundBase(void)
{
  return s_cfg.base;
}

/* Полезная нагрузка новейшего CFGH-поколения в out (до len байт).
 * 0 — области нет или битый payload_crc. */
uint8_t Storage_CfgRead(uint8_t *out, uint32_t len)
{
  if (s_cfg.base == 0U) {
    return 0U;
  }
  const store_header_t *h = (const store_header_t *)s_cfg.base;
  uint32_t n = (len < s_cfg.payload_len) ? len : s_cfg.payload_len;
  memcpy(out, (const uint8_t *)(s_cfg.base + STORE_HEADER_SIZE), n);
  /* Полный CRC payload из заголовка — контроль целостности читаемого. */
  if (h->payload_crc != 0U
      && crc32_of((const uint8_t *)(s_cfg.base + STORE_HEADER_SIZE),
                  s_cfg.payload_len) != h->payload_crc) {
    return 0U;
  }
  return 1U;
}

/* Новое поколение настроек — каноничная страница сразу за кодом.
 * Пишется payload, потом заголовок; чужая копия (старое code_end после
 * обновления прошивки) стирается после успешной записи. */
uint8_t Storage_CfgWrite(const uint8_t *payload, uint32_t len)
{
  if (payload == NULL || len == 0U || len > STORE_MAX_PAYLOAD) {
    return 0U;
  }
  uint32_t pages = blob_pages(len);
  if (pages > STORE_CFG_PAGES) {
    return 0U; /* настройки обязаны влезать в зарезервированную полку */
  }
  uint32_t base = s_code_end;
  uint32_t span = STORE_CFG_PAGES * STORE_FLASH_PAGE;
  /* Каноничная страница может быть занята найденной копией — тогда она
   * и есть «старое поколение», стираем её же. Чужих областей там быть
   * не должно: резерв держит range_busy. */
  if (s_cfg.base != 0U && s_cfg.base != base) {
    /* Найденная копия сидит не на каноничной странице (код вырос/
     * ужался при обновлении): запишем новое поколение на якорь,
     * старую потом вытрем. */
  }
  if (!erase_pages(base, base + span)) {
    return 0U;
  }
  HAL_FLASH_Unlock();
  uint32_t addr = base + STORE_HEADER_SIZE;
  for (uint32_t i = 0U; i < len; i += 2U) {
    uint16_t hw = (uint16_t)payload[i];
    if (i + 1U < len) {
      hw |= (uint16_t)((uint16_t)payload[i + 1U] << 8);
    } else {
      hw |= 0xFF00U;
    }
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, hw) != HAL_OK) {
      HAL_FLASH_Lock();
      return 0U;
    }
    addr += 2U;
  }
  store_header_t h;
  memset(&h, 0, sizeof(h));
  h.magic = STORE_MAGIC_CFG;
  h.generation = s_cfg.generation + 1U;
  h.payload_len = len;
  h.payload_crc = crc32_of(payload, len);
  h.version = STORE_VERSION;
  h.region = STORE_REGION_CFG;
  h.crc8 = crc8((const uint8_t *)&h, offsetof(store_header_t, crc8));
  const uint16_t *hw = (const uint16_t *)&h;
  addr = base;
  for (uint32_t w = 0U; w < sizeof(h) / 2U; w++) {
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, hw[w]) != HAL_OK) {
      HAL_FLASH_Lock();
      return 0U;
    }
    addr += 2U;
  }
  HAL_FLASH_Lock();
  if (memcmp((const void *)base, &h, sizeof(h)) != 0) {
    return 0U;
  }
  /* Переезд: вытесненная копия в другом месте — зомби, вытираем. */
  uint32_t old_base = s_cfg.base;
  uint32_t old_end = s_cfg.end;
  s_cfg.base = base;
  s_cfg.end = base + span;
  s_cfg.generation = h.generation;
  s_cfg.payload_len = len;
  if (old_base != 0U && old_base != base) {
    (void)erase_pages(old_base, old_end);
  }
  return 1U;
}
