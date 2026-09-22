/* Trigger engine implementation. See trigger.h and firmware/PROTOCOL.md
 * Part 3 for the on-Flash layout/rationale. */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "trigger.h"

/* sizeof(trigger_t) must be a multiple of 2 (half-word) so that
 * flash_write_store()'s halfword-by-halfword HAL_FLASH_Program()
 * loop below never truncates the last byte, and so that the write step
 * (sizeof(trigger_t)) matches the read step used by Trigger_Init(). */
_Static_assert((sizeof(trigger_t) % 2U) == 0U, "trigger_t size must be halfword-aligned");
_Static_assert(sizeof(trigger_t) == TRIGGER_RECORD_SIZE, "trigger_t must match wire record size");
_Static_assert(sizeof(trigger_header_t) == TRIGGER_HEADER_SIZE, "trigger_header_t must be 16 bytes");

static trigger_t s_triggers[TRIGGER_MAX_RECORDS];
static trigger_t s_staged[TRIGGER_MAX_RECORDS];
static uint8_t s_stage_flags[TRIGGER_MAX_RECORDS];
static uint8_t s_staged_max;    /* наибольший staged-индекс +1 */
static uint32_t s_stage_tick;   /* тик последнего STAGE */
static uint8_t s_count;         /* активных записей в списке */
static uint32_t s_store_base;   /* адрес заголовка области в пуле, 0 — нет */
static uint32_t s_generation;   /* generation последнего коммита */

/* Кэш режима «автоматическая запись DATA»: последний кадр, попавший в
 * фильтр «Откуда читаем». Хранится в RAM, не во Flash — перезапись
 * страницы на каждый кадр исчерпала бы ресурс стираний (~10k циклов). */
static can_frame_t s_cache[TRIGGER_MAX_RECORDS];
static uint8_t s_cache_valid[TRIGGER_MAX_RECORDS];

/* Pending deferred responses (delay_ms > 0 or repeat count > 1). A
 * fixed-size list is enough since there are at most TRIGGER_COUNT
 * triggers and each can have at most one response in flight at a time
 * (a new match on the same trigger while one is already pending simply
 * re-arms it). */
typedef struct {
  uint8_t  armed;
  uint8_t  trigger_index;
  uint32_t fire_at_tick;
  uint8_t  remaining;      /* оставшиеся отправки (tx_count) */
  uint16_t interval_ms;    /* пауза между отправками (tx_interval_ms) */
  uint8_t  echo;           /* глубина TX-эха исходного кадра — передаётся
                            * в ответ, чтобы цепочка триггеров была
                            * ограничена CAN_TX_ECHO_MAX (см. can_bridge.c) */
  uint8_t  retries;        /* попытки отправки при занятых TX-ящиках */
} pending_response_t;

/* Ответ, который не удалось поставить на шину (CanBridge_Transmit
 * вернул 0 — ящики заняты арбитражем/ретраями без ACK), ретраим
 * с паузой вместо молчаливого дропа; лимит ~20 мс на отправку. */
#define TRIGGER_TX_RETRY_MAX 20U
#define TRIGGER_TX_RETRY_DELAY_MS 1U

/* STAGE без COMMIT живёт ограниченное время: оборванная сессия записи
 * (USB-обрыв между STAGE и COMMIT) держала staged-записи вооружёнными
 * бесконечно — любой поздний COMMIT (в т.ч. мусорный байт 0xCB из
 * рассинхрона потока) дописывал их во Flash «фантомным» триггером,
 * воскресавшим после каждого ребута. */
#define TRIGGER_STAGE_TIMEOUT_MS 15000U

static pending_response_t s_pending[TRIGGER_MAX_RECORDS];
static uint32_t s_fired_count;
static uint32_t s_dropped_count; /* отправки, исчерпавшие ретраи */
static uint32_t s_max_lateness_ms;
static uint8_t s_flash_valid_count; /* включённых записей, прочитанных
  из Flash при старте — диагностика «триггеры пропали после питания» */

/* Состояние «кол-во сработок до смены DATA» (формат v3): на запись —
 * счётчик сработок на неизменном содержимом и защёлка «лимит исчерпан»
 * (тот самый «бит в ОЗУ» из ТЗ). Смена DATA на уровне ID-фильтра
 * сбрасывает защёлку — триггер снова исполняет лимит сработок. */
typedef struct {
  uint16_t count;     /* сработок на текущей неизменной DATA */
  uint8_t  suppress;  /* лимит исчерпан — игнорируем до смены DATA */
  uint8_t  have;      /* last/last_dlc заполнены */
  uint8_t  last_dlc;
  uint8_t  last[8];
} fire_state_t;
static fire_state_t s_rx_state[TRIGGER_MAX_RECORDS];   /* условие приёма */
static fire_state_t s_src_state[TRIGGER_MAX_RECORDS];  /* источник кэша */

static uint8_t trigger_fields_valid(const trigger_t *trig);
static uint8_t erase_pages(uint32_t from, uint32_t to);
static uint8_t program_store(uint32_t base, uint32_t generation, uint8_t total,
                             const trigger_t **src);

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

static void load_default(trigger_t *t)
{
  memset(t, 0, sizeof(*t));
  t->magic = TRIGGER_MAGIC;
  t->enabled = 0U;
  t->reserved[0] = TRIGGER_FORMAT_VERSION;
  t->reserved[1] = TRIGGER_RECORD_SIZE;
  t->crc8 = crc8((const uint8_t *)t, offsetof(trigger_t, crc8));
}

/* Валидация записи во Flash по сырым байтам — формат v2 (82 Б) и v3
 * (90 Б) читаются с разным шагом, CRC закрывает всю запись, её длина —
 * последний байт области записи. Версия/размер лежат в reserved[0..1]:
 * 0/0 — старые записи до версионирования, 2/82 — v2, 3/90 — v3. */
static uint8_t record_valid_at(const uint8_t *p, uint32_t rec_size)
{
  uint32_t magic;
  memcpy(&magic, p, sizeof(magic));
  if (magic != TRIGGER_MAGIC) {
    return 0U;
  }
  uint8_t ver = p[49];
  uint8_t sz = p[50];
  if (!((ver == 0U && sz == 0U)
        || (ver == TRIGGER_FORMAT_VERSION_V2 && sz == TRIGGER_RECORD_SIZE_V2)
        || (ver == TRIGGER_FORMAT_VERSION && sz == TRIGGER_RECORD_SIZE))) {
    return 0U;
  }
  if (sz != 0U && sz != rec_size) {
    return 0U; /* размер записи обязан совпадать с шагом области */
  }
  return crc8(p, rec_size - 1U) == p[rec_size - 1U];
}

/* Сырые байты записи → runtime-структура v3: хвост за rec_size нулями
 * (у записей v2 новых полей нет — функции выключены/эхо слушается). */
static uint8_t load_record(const uint8_t *p, uint32_t rec_size, trigger_t *out)
{
  if (!record_valid_at(p, rec_size)) {
    return 0U;
  }
  memset(out, 0, sizeof(*out));
  memcpy(out, p, rec_size < sizeof(*out) ? rec_size : sizeof(*out));
  return trigger_fields_valid(out);
}

/* Поиск активной области хранилища: страница пула, начинающаяся с
 * валидного заголовка "TRGH". Если таких несколько (оборванная запись
 * оставила старую область ниже), выбирается больший generation.
 * Принимаются версии 3 (записи 82 Б) и 4 (записи 90 Б): хранилище v3,
 * записанное старой прошивкой, поднимается в RAM и переписывается v4
 * при ближайшем COMMIT — триггеры не теряются при обновлении. */
static uint32_t find_store(uint8_t *count_out, uint32_t *gen_out,
                           uint32_t *stride_out)
{
  uint32_t best = 0U;
  uint32_t best_gen = 0U;
  uint32_t best_stride = 0U;
  for (uint32_t page = TRIGGER_POOL_BASE; page < TRIGGER_FLASH_END;
       page += TRIGGER_FLASH_PAGE) {
    const trigger_header_t *h = (const trigger_header_t *)page;
    uint32_t stride;
    if (h->magic != TRIGGER_HEADER_MAGIC) {
      continue;
    }
    if (h->version == TRIGGER_STORE_VERSION) {
      stride = sizeof(trigger_t);
    } else if (h->version == TRIGGER_STORE_VERSION_V3) {
      stride = TRIGGER_RECORD_SIZE_V2;
    } else {
      continue;
    }
    if (h->count > TRIGGER_MAX_RECORDS) {
      continue;
    }
    if (page + TRIGGER_HEADER_SIZE + (uint32_t)h->count * stride
        > TRIGGER_FLASH_END) {
      continue;
    }
    if (crc8((const uint8_t *)h, offsetof(trigger_header_t, crc8)) != h->crc8) {
      continue;
    }
    if (best == 0U || h->generation >= best_gen) {
      best = page;
      best_gen = h->generation;
      best_stride = stride;
    }
  }
  if (best != 0U) {
    const trigger_header_t *h = (const trigger_header_t *)best;
    *count_out = h->count;
    *gen_out = best_gen;
    *stride_out = best_stride;
  }
  return best;
}

void Trigger_Init(void)
{
  memset(s_pending, 0, sizeof(s_pending));
  memset(s_staged, 0, sizeof(s_staged));
  memset(s_stage_flags, 0, sizeof(s_stage_flags));
  memset(s_cache, 0, sizeof(s_cache));
  memset(s_cache_valid, 0, sizeof(s_cache_valid));
  memset(s_rx_state, 0, sizeof(s_rx_state));
  memset(s_src_state, 0, sizeof(s_src_state));
  s_fired_count = 0U;
  s_dropped_count = 0U;
  s_max_lateness_ms = 0U;
  s_count = 0U;
  s_staged_max = 0U;
  s_stage_tick = 0U;
  s_store_base = 0U;
  s_generation = 0U;
  s_flash_valid_count = 0U;

  uint8_t count = 0U;
  uint32_t gen = 0U;
  uint32_t stride = 0U;
  uint32_t base = find_store(&count, &gen, &stride);
  if (base != 0U) {
    /* Записи идут сплошным списком за заголовком с шагом stride
     * (82 Б у хранилища v3, 90 Б у v4). Битая запись (обрыв записи/
     * повреждение) просто пропускается — остальные остаются рабочими. */
    const uint8_t *p = (const uint8_t *)(base + TRIGGER_HEADER_SIZE);
    for (uint8_t i = 0U; i < count; i++) {
      const uint8_t *rec = p + (uint32_t)i * stride;
      if (load_record(rec, stride, &s_triggers[s_count])) {
        if (s_triggers[s_count].enabled) {
          s_flash_valid_count++;
        }
        s_count++;
      }
    }
    s_store_base = base;
    s_generation = gen;
    return;
  }

  /* Миграция v2: старый фиксированный регион 0x0803E000, 49 слотов
   * по 82 Б. Записи поднимаются в RAM и работают сразу. */
  const uint8_t *legacy = (const uint8_t *)TRIGGER_LEGACY_ADDR;
  for (uint8_t i = 0U; i < TRIGGER_LEGACY_COUNT; i++) {
    const uint8_t *rec = legacy + (uint32_t)i * TRIGGER_RECORD_SIZE_V2;
    if (s_count < TRIGGER_MAX_RECORDS
        && load_record(rec, TRIGGER_RECORD_SIZE_V2, &s_triggers[s_count])) {
      if (s_triggers[s_count].enabled) {
        s_flash_valid_count++;
      }
      s_count++;
    }
  }

  /* Миграция финализируется сразу: поднятые записи пишем v3-хранилищем
   * в верх пула, легаси-страницу внизу НЕ стираем — при обрыве питания
   * посреди записи следующий старт снова найдёт источник и повторит
   * переезд. Без финализации легаси реимпортировалось при КАЖДОМ
   * старте: удалённый, но ни разу не сохранённый триггер воскресал
   * «фантомом» после каждого ребута. */
  if (s_count != 0U) {
    const trigger_t *src[TRIGGER_MAX_RECORDS];
    for (uint8_t j = 0U; j < s_count; j++) {
      src[j] = &s_triggers[j];
    }
    uint32_t size = TRIGGER_HEADER_SIZE + (uint32_t)s_count * sizeof(trigger_t);
    uint32_t pages = (size + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE;
    uint32_t base = TRIGGER_FLASH_END - pages * TRIGGER_FLASH_PAGE;
    /* Хранилище привязано к верху пула и при максимуме записей не
     * достаёт до легаси-страницы — стираем только целевой диапазон. */
    if (base > TRIGGER_LEGACY_ADDR
        && erase_pages(base, TRIGGER_FLASH_END)
        && program_store(base, 1U, s_count, src)) {
      s_store_base = base;
      s_generation = 1U;
    }
    /* Сбой записи не фатален: список уже в RAM и работает, переезд
     * повторится при следующем старте. */
  }
}

uint8_t Trigger_Get(uint8_t index, trigger_t *out)
{
  if (index >= s_count) {
    return 0U;
  }
  *out = s_triggers[index];
  return 1U;
}

uint8_t Trigger_Count(void)
{
  return s_count;
}

/* Страница целиком стёрта (все 0xFF) — её можно программировать без
 * стирания и она не занимает память в пуле. */
static uint8_t page_is_blank(uint32_t addr)
{
  const uint32_t *p = (const uint32_t *)addr;
  for (uint32_t i = 0U; i < TRIGGER_FLASH_PAGE / 4U; i++) {
    if (p[i] != 0xFFFFFFFFUL) {
      return 0U;
    }
  }
  return 1U;
}

/* Стирает все непустые страницы в диапазоне [from, to). */
static uint8_t erase_pages(uint32_t from, uint32_t to)
{
  HAL_FLASH_Unlock();
  for (uint32_t page = from; page < to; page += TRIGGER_FLASH_PAGE) {
    /* Стирание каждой страницы ~40 мс — пул до 8 КБ; кормим IWDG,
     * чтобы COMMIT большого набора триггеров не сбрасывал МК. */
    App_KickWatchdog();
    if (page_is_blank(page)) {
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

/* Стирает все непустые страницы пула триггеров. */
static uint8_t erase_pool(void)
{
  return erase_pages(TRIGGER_POOL_BASE, TRIGGER_FLASH_END);
}

/* Пишет записи + заголовок в область [base, ...) — пул уже стёрт.
 * Заголовок пишется ПОСЛЕДНИМ: он точка коммита. Обрыв посреди записи
 * (IWDG/сброс/питание) оставляет область без валидного заголовка —
 * find_store() её не видит, и при старте поднимается прежнее
 * хранилище или чистое состояние, но не урезанный список записей.
 * src[j] указывает на запись для позиции j (staged или текущая). */
static uint8_t program_store(uint32_t base, uint32_t generation, uint8_t total,
                             const trigger_t **src)
{
  trigger_header_t h;
  memset(&h, 0, sizeof(h));
  h.magic = TRIGGER_HEADER_MAGIC;
  h.version = TRIGGER_STORE_VERSION;
  h.count = total;
  h.generation = generation;
  h.crc8 = crc8((const uint8_t *)&h, offsetof(trigger_header_t, crc8));

  HAL_FLASH_Unlock();
  uint32_t addr = base + TRIGGER_HEADER_SIZE;
  for (uint8_t j = 0U; j < total; j++) {
    /* До 70 записей по ~41 halfword — сотни миллисекунд записи;
     * кормим IWDG между записями. */
    App_KickWatchdog();
    const uint16_t *src16 = (const uint16_t *)src[j];
    for (uint32_t w = 0U; w < sizeof(trigger_t) / 2U; w++) {
      if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, src16[w]) != HAL_OK) {
        HAL_FLASH_Lock();
        return 0U;
      }
      addr += 2U;
    }
  }
  /* Заголовок — в последнюю очередь: до этого момента область для
   * find_store() невидима, частично прошитая область не читается. */
  addr = base;
  const uint16_t *hw = (const uint16_t *)&h;
  for (uint32_t w = 0U; w < sizeof(h) / 2U; w++) {
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, hw[w]) != HAL_OK) {
      HAL_FLASH_Lock();
      return 0U;
    }
    addr += 2U;
  }
  HAL_FLASH_Lock();

  /* HAL_OK на halfword не гарантирует содержимое (обрыв питания даёт
   * частично прошитую страницу) — сверяем записанное с источником. */
  if (memcmp((const void *)base, &h, sizeof(h)) != 0) {
    return 0U;
  }
  const uint8_t *p = (const uint8_t *)(base + TRIGGER_HEADER_SIZE);
  for (uint8_t j = 0U; j < total; j++) {
    if (memcmp(p + (uint32_t)j * sizeof(trigger_t), src[j], sizeof(trigger_t)) != 0) {
      return 0U;
    }
  }
  return 1U;
}

void Trigger_ClearAll(void)
{
  erase_pool();
  s_count = 0U;
  s_staged_max = 0U;
  s_stage_tick = 0U;
  s_store_base = 0U;
  s_flash_valid_count = 0U;
  memset(s_stage_flags, 0, sizeof(s_stage_flags));
  memset(s_pending, 0, sizeof(s_pending));
  memset(s_cache_valid, 0, sizeof(s_cache_valid));
}

static uint8_t trigger_fields_valid(const trigger_t *trig)
{
  /* Каналы 0-based как в UI: 0=CAN1, 1=CAN2, 2=оба канала — на приём
   * матчит любой канал, на ответ шлёт в оба. src_* — матчер «Откуда
   * читаем» кэш-режима. */
  if (trig == NULL || trig->rx_channel > 2U || trig->tx_channel > 2U
      || trig->rx_extended > 1U || trig->tx_extended > 1U
      || trig->rx_dlc > 8U || trig->tx_dlc > 8U
      || trig->tx_rtr > 1U || trig->rx_rtr > 2U || trig->cache_enabled > 1U
      || trig->src_channel > 2U || trig->src_extended > 1U
      || trig->src_dlc > 8U
      || (trig->rx_flags & ~TRIGGER_F_MUTE_ECHO) != 0U
      || (trig->src_flags & ~TRIGGER_F_MUTE_ECHO) != 0U) {
    return 0U;
  }
  uint32_t rx_max = trig->rx_extended ? 0x1FFFFFFFU : 0x7FFU;
  uint32_t tx_max = trig->tx_extended ? 0x1FFFFFFFU : 0x7FFU;
  uint32_t src_max = trig->src_extended ? 0x1FFFFFFFU : 0x7FFU;
  return (trig->rx_id <= rx_max && trig->rx_id_mask <= rx_max
          && trig->tx_id <= tx_max && trig->src_id <= src_max) ? 1U : 0U;
}

/* Запись с провода → runtime-структура: принимаются 82 Б (хост/формат
 * v2 — новые поля нулями: лимиты выключены, эхо слушается) и 90 Б
 * (формат v3). Так старый ПК продолжает писать триггеры в новую
 * прошивку. */
uint8_t Trigger_FromWire(const uint8_t *src, uint32_t len, trigger_t *out)
{
  if (len != TRIGGER_RECORD_SIZE && len != TRIGGER_RECORD_SIZE_V2) {
    return 0U;
  }
  memset(out, 0, sizeof(*out));
  memcpy(out, src, len);
  return 1U;
}

uint8_t Trigger_Stage(uint8_t index, const trigger_t *trig)
{
  if (index >= TRIGGER_MAX_RECORDS || !trigger_fields_valid(trig)) {
    return 0U;
  }
  trigger_t staged = *trig;
  staged.magic = TRIGGER_MAGIC;
  staged.reserved[0] = TRIGGER_FORMAT_VERSION;
  staged.reserved[1] = TRIGGER_RECORD_SIZE;
  staged.crc8 = crc8((const uint8_t *)&staged, offsetof(trigger_t, crc8));
  s_staged[index] = staged;
  s_stage_flags[index] = 1U;
  s_stage_tick = HAL_GetTick();
  if (index >= s_staged_max) {
    s_staged_max = (uint8_t)(index + 1U);
  }
  return 1U;
}

uint8_t Trigger_Commit(uint8_t total)
{
  /* total == 0xFF — «как было»: старый хост шлёт COMMIT без длины —
   * список сохраняет размер, staged-позиции накладываются позиционно.
   * Иначе staged-записи за пределами total игнорируются (хост для
   * совместимости может гасить хвост пустыми записями — старая
   * прошивка запишет их в слоты, новая отбросит по длине списка). */
  if (total == 0xFFU) {
    total = s_count > s_staged_max ? s_count : s_staged_max;
  }
  if (total > TRIGGER_MAX_RECORDS) {
    return 0U;
  }
  if (s_staged_max == 0U && total == s_count) {
    return 1U; /* нечего коммитить */
  }

  /* Новый список: позиция = staged-запись или текущая; хвост за total
   * отбрасывается. Позиции ни staged, ни текущей — пустые записи
   * (занимают место, но не срабатывают). */
  static trigger_t empty_rec;
  load_default(&empty_rec);
  const trigger_t *src[TRIGGER_MAX_RECORDS];
  for (uint8_t j = 0U; j < total; j++) {
    if (s_stage_flags[j] != 0U) {
      src[j] = &s_staged[j];
    } else if (j < s_count) {
      src[j] = &s_triggers[j];
    } else {
      src[j] = &empty_rec;
    }
  }

  /* Область привязана к верху Flash: растёт вниз по мере добавления.
   * 0 записей — 0 страниц: хранилище отсутствует, память свободна. */
  uint32_t size = (total != 0U)
      ? TRIGGER_HEADER_SIZE + (uint32_t)total * sizeof(trigger_t)
      : 0U;
  uint32_t pages = (size + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE;
  uint32_t base_top = TRIGGER_FLASH_END - pages * TRIGGER_FLASH_PAGE;
  if (total != 0U && base_top < TRIGGER_POOL_BASE) {
    return 0U; /* не помещается даже во весь пул */
  }

  /* Атомарная замена: новая область пишется в страницы, НЕ занятые
   * действующим хранилищем (второй якорь — низ пула), и становится
   * видимой только когда дописан заголовок (program_store пишет его
   * последним). Сброс посреди коммита — IWDG, обрыв USB, питание —
   * оставляет ПРЕЖНИЙ список целым: следующий старт видит старые
   * триггеры вместо урезанных/мусорных. Рядом не помещаются (оба
   * якоря пересекаются со старым) — старое поведение: зачистка пула
   * целиком, обрыв тогда даёт пустое хранилище, а не мусор. */
  uint32_t base = 0xFFFFFFFFU;
  uint32_t old_base = s_store_base;
  uint32_t old_end = 0U;
  if (old_base != 0U) {
    /* Устаревшее хранилище могло быть записано с шагом 82 Б (store v3) —
     * sizeof(trigger_t) здесь завышает оценку, но это безопасно:
     * пересечение проверяется с запасом, а стирание зажато концом Flash. */
    uint32_t old_pages = (TRIGGER_HEADER_SIZE
                          + (uint32_t)s_count * sizeof(trigger_t)
                          + (TRIGGER_FLASH_PAGE - 1U)) / TRIGGER_FLASH_PAGE;
    old_end = old_base + old_pages * TRIGGER_FLASH_PAGE;
    if (old_end > TRIGGER_FLASH_END) {
      old_end = TRIGGER_FLASH_END;
    }
  }
  if (total != 0U && old_base != 0U) {
    const uint32_t anchors[2] = { base_top, TRIGGER_POOL_BASE };
    for (uint8_t c = 0U; c < 2U; c++) {
      uint32_t cand = anchors[c];
      uint32_t cand_end = cand + pages * TRIGGER_FLASH_PAGE;
      if (cand >= old_end || cand_end <= old_base) {
        base = cand; /* не пересекается со старым хранилищем */
        break;
      }
    }
  }

  if (total == 0U) {
    if (!erase_pool()) {
      return 0U;
    }
  } else if (base != 0xFFFFFFFFU) {
    if (!erase_pages(base, base + pages * TRIGGER_FLASH_PAGE)
        || !program_store(base, s_generation + 1U, total, src)) {
      return 0U; /* старое хранилище не тронуто — коммит можно повторить */
    }
    /* Старое стираем только ПОСЛЕ того, как новое стало валидным;
     * сбой здесь оставляет зомби-заголовок, но find_store() выбирает
     * область с большим generation — новую. */
    (void)erase_pages(old_base, old_end);
  } else {
    if (!erase_pool()) {
      return 0U;
    }
    if (!program_store(base_top, s_generation + 1U, total, src)) {
      return 0U; /* RAM-список не трогаем: на Flash старое/мусор */
    }
    base = base_top;
  }

  for (uint8_t j = 0U; j < total; j++) {
    s_triggers[j] = *src[j];
  }
  s_count = total;
  s_store_base = (total != 0U) ? base : 0U;
  s_generation++;
  s_staged_max = 0U;
  s_stage_tick = 0U;
  memset(s_stage_flags, 0, sizeof(s_stage_flags));
  /* Слоты выше новой длины больше не исполняются — кэш/отложенные
   * отправки по ним надо сбросить, а по перезаписанным — обновить. */
  memset(s_pending, 0, sizeof(s_pending));
  memset(s_cache_valid, 0, sizeof(s_cache_valid));
  /* Счётчики «сработок до смены DATA» относятся к записи — при новом
   * списке начинают с нуля, иначе переиспользованный слот наследовал
   * бы чужую защёлку и молчал до первой смены DATA. */
  memset(s_rx_state, 0, sizeof(s_rx_state));
  memset(s_src_state, 0, sizeof(s_src_state));
  return 1U;
}

uint8_t Trigger_Set(uint8_t index, const trigger_t *trig)
{
  if (!Trigger_Stage(index, trig)) {
    return 0U;
  }
  uint8_t total = s_count;
  if (index >= total) {
    total = (uint8_t)(index + 1U);
  }
  return Trigger_Commit(total);
}

uint8_t Trigger_SetEnabled(uint8_t index, uint8_t enabled)
{
  if (index >= s_count) {
    return 0U;
  }
  trigger_t updated = s_triggers[index];
  updated.enabled = enabled ? 1U : 0U;
  return Trigger_Set(index, &updated);
}

/* Условие приёма на уровне заголовка: режим RTR, канал, битность, ID по
 * маске и DLC. Отделено от сравнения Data — «смена DATA» для счётчика
 * сработок отслеживается уже на этом уровне (кадр с тем же ID, но
 * другим содержимым сбрасывает защёлку лимита, даже если под шаблон
 * данных он не попал). */
static uint8_t rx_header_matches(const trigger_t *t, const can_frame_t *frame)
{
  /* rx_rtr: 0 = любой кадр (старое поведение), 1 = только RTR-запрос,
   * 2 = только кадр с данными. */
  if (t->rx_rtr == 1U && frame->rtr == 0U) {
    return 0U;
  }
  if (t->rx_rtr == 2U && frame->rtr != 0U) {
    return 0U;
  }
  if (t->rx_channel != 2U && t->rx_channel != frame->channel) {
    return 0U;
  }
  if (t->rx_extended != frame->extended) {
    return 0U;
  }
  if ((t->rx_id & t->rx_id_mask) != (frame->id & t->rx_id_mask)) {
    return 0U;
  }
  if (t->rx_dlc != 0U && t->rx_dlc != frame->dlc) {
    /* rx_dlc == 0 means "any length" (matches ui/can_trigger_tab.py's
     * treatment of an unset/zero length as "don't care"). */
    return 0U;
  }
  return 1U;
}

static uint8_t rx_data_matches(const trigger_t *t, const can_frame_t *frame)
{
  if (t->rx_rtr != 1U) {
    /* В RTR-режиме кадр не несёт данных — сравнение по Data не имеет
     * смысла и пропускается. Байт с маской 0x00 — wildcard «X». */
    for (uint8_t i = 0; i < frame->dlc && i < 8U; i++) {
      if ((t->rx_data[i] & t->rx_data_mask[i]) != (frame->data[i] & t->rx_data_mask[i])) {
        return 0U;
      }
    }
  }
  return 1U;
}

/* Источник кэша на уровне ID (канал/битность/ID) — для «смены DATA»
 * счётчика сработок кэша. */
static uint8_t src_id_matches(const trigger_t *t, const can_frame_t *frame)
{
  if (t->src_channel != 2U && t->src_channel != frame->channel) {
    return 0U;
  }
  if (t->src_extended != frame->extended) {
    return 0U;
  }
  return t->src_id == frame->id;
}

/* Побайтовый диапазон «От/До»: каждый байт Data (src_dlc штук) должен
 * попасть в свой [src_from[i], src_to[i]]. Инвертированный диапазон
 * (from[i] > to[i]) — wildcard «X»: байт не участвует в сравнении, а при
 * захвате в кэш обнуляется (в ответе уйдёт 0x00 — см. Trigger_OnFrame).
 * src_dlc == 0 — данные не проверяются (матч только по ID). */
static uint8_t src_range_matches(const trigger_t *t, const can_frame_t *frame)
{
  for (uint8_t i = 0; i < t->src_dlc && i < 8U; i++) {
    if (t->src_from[i] > t->src_to[i]) {
      continue;
    }
    /* Кадр короче src_dlc — недостающие байты считаются нулевыми, как в
     * host-логике _cache_src_matches(). */
    uint8_t b = (i < frame->dlc) ? frame->data[i] : 0U;
    if (b < t->src_from[i] || b > t->src_to[i]) {
      return 0U;
    }
  }
  return 1U;
}

/* Участвует ли байт i в понятии «DATA» для счётчика сработок. Байты
 * wildcard «X» полностью игнорируются (отчёт мастера): их изменение —
 * не смена DATA и не должно сбрасывать защёлку лимита — иначе постоянно
 * меняющиеся байты-счётчики никогда не давали бы лимиту сработать.
 * rx-путь: «X» — это rx_data_mask[i]==0 (маска побитовая); для RTR-
 * приёма данные не сравниваются вовсе. src-путь: «X» — инвертированный
 * диапазон src_from[i] > src_to[i]; байты за пределами src_dlc фильтром
 * не охвачены и тоже не считаются. */
static uint8_t fire_byte_compared(const trigger_t *t, uint8_t is_src, uint8_t i)
{
  if (is_src != 0U) {
    return (uint8_t)((i < t->src_dlc) && (t->src_from[i] <= t->src_to[i]));
  }
  return (uint8_t)(t->rx_rtr != 1U && t->rx_data_mask[i] != 0U);
}

/* «Кол-во сработок до смены DATA»: запоминает содержимое кадра на
 * уровне ID-фильтра; при смене DATA защёлка лимита сбрасывается —
 * триггер снова исполняет до rx/src_fire_limit сработок. Вызывается
 * только когда соответствующий лимит != 0. */
static void fire_track(fire_state_t *st, const can_frame_t *frame,
                       const trigger_t *t, uint8_t is_src)
{
  uint8_t dlc = frame->dlc > 8U ? 8U : frame->dlc;
  uint8_t changed = (uint8_t)(!st->have || st->last_dlc != dlc);
  if (changed == 0U) {
    for (uint8_t i = 0; i < 8U; i++) {
      if (fire_byte_compared(t, is_src, i) == 0U) {
        continue;
      }
      uint8_t cur = (i < dlc) ? frame->data[i] : 0U;
      /* rx-маска побитовая: сравниваются только значащие биты байта. */
      uint8_t prev = st->last[i];
      if (is_src == 0U) {
        cur &= t->rx_data_mask[i];
        prev &= t->rx_data_mask[i];
      }
      if (cur != prev) {
        changed = 1U;
        break;
      }
    }
  }
  if (changed != 0U) {
    memset(st->last, 0, sizeof(st->last));
    memcpy(st->last, frame->data, dlc);
    st->last_dlc = dlc;
    st->have = 1U;
    st->count = 0U;
    st->suppress = 0U;
  }
}

static void arm_response(uint8_t index, const trigger_t *t, uint8_t echo)
{
  s_pending[index].armed = 1U;
  s_pending[index].trigger_index = index;
  s_pending[index].fire_at_tick = HAL_GetTick() + t->delay_ms;
  s_pending[index].remaining = t->tx_count ? t->tx_count : 1U;
  s_pending[index].interval_ms = t->tx_interval_ms;
  s_pending[index].echo = echo;
  s_pending[index].retries = 0U;
}

/* Одна отправка ответа триггера. В кэш-режиме шлётся последний кадр из
 * s_cache[index] (ID/DLC/Data — как приняты с шины), в обычном — поля
 * tx_* записи. tx_channel хранится 0-based (индекс комбобокса UI), а
 * CanBridge_Transmit ждёт wire-нумерацию 1/2. echo — глубина TX-эха
 * исходного кадра: ответ наследует её, иначе каждый ответ начинал бы
 * цепочку заново и пинг-понг триггеров не ограничивался. */
static uint8_t send_response(const trigger_t *t, uint8_t index, uint8_t echo)
{
  can_frame_t resp;
  if (t->cache_enabled) {
    if (!s_cache_valid[index]) {
      return 0U; /* кэш ещё пуст — отправлять нечего */
    }
    resp = s_cache[index];
    resp.channel = 0U;
  } else {
    resp.channel = 0U;
    resp.extended = t->tx_extended;
    resp.rtr = t->tx_rtr;
    resp.id = t->tx_id;
    resp.dlc = t->tx_dlc;
    memcpy(resp.data, t->tx_data, 8);
  }
  resp.echo = echo;
  uint8_t sent = 0U;
  if (t->tx_channel == 0U || t->tx_channel == 2U) {
    resp.channel = 1U;
    sent |= CanBridge_Transmit(&resp);
  }
  if (t->tx_channel >= 1U) {
    resp.channel = 2U;
    sent |= CanBridge_Transmit(&resp);
  }
  return sent;
}

void Trigger_OnFrame(const can_frame_t *frame)
{
  for (uint8_t i = 0; i < s_count; i++) {
    const trigger_t *t = &s_triggers[i];
    if (!t->enabled) {
      continue;
    }
    /* Кэш пополняется независимо от условия «Приём» — триггер постоянно
     * переписывает Data последнего подходящего кадра. Обновление идёт
     * до проверки rx-матча: кадр, попавший в оба фильтра, обновит кэш
     * до отправки. src_flags&MUTE_ECHO — источник слушает только шину
     * (собственные отправки МК не кэшируются); src_fire_limit — захват
     * прекращается после N одинаковых DATA до их смены. */
    if (t->cache_enabled
        && !(frame->echo != 0U && (t->src_flags & TRIGGER_F_MUTE_ECHO) != 0U)
        && src_id_matches(t, frame)) {
      if (t->src_fire_limit != 0U) {
        fire_track(&s_src_state[i], frame, t, 1U);
      }
      if (s_src_state[i].suppress == 0U && src_range_matches(t, frame)) {
        s_cache[i] = *frame;
        /* Wildcard-позиции (from>to) в кэше обнуляются: при ответе на
         * месте игнорированных байтов уйдёт 0x00, а не случайное значение
         * последнего кадра. */
        for (uint8_t j = 0; j < t->src_dlc && j < 8U; j++) {
          if (t->src_from[j] > t->src_to[j]) {
            s_cache[i].data[j] = 0U;
          }
        }
        s_cache_valid[i] = 1U;
        if (t->src_fire_limit != 0U
            && ++s_src_state[i].count >= t->src_fire_limit) {
          s_src_state[i].suppress = 1U;
        }
      }
    }
    /* rx_flags&MUTE_ECHO — триггер не реагирует на кадры, отправленные
     * самим МК (TX-эхо); 0 — слушает и внешние, и свои (как в v2). */
    if ((frame->echo != 0U && (t->rx_flags & TRIGGER_F_MUTE_ECHO) != 0U)
        || !rx_header_matches(t, frame)) {
      continue;
    }
    if (t->rx_fire_limit != 0U) {
      fire_track(&s_rx_state[i], frame, t, 0U);
    }
    if (s_rx_state[i].suppress != 0U || !rx_data_matches(t, frame)) {
      continue;
    }
    {
      uint8_t sends = t->tx_count ? t->tx_count : 1U;
      if (t->delay_ms == 0U && sends == 1U) {
        /* Zero delay, single shot: send immediately, no need to go
         * through the pending list — keeps the "instant echo" case as
         * low-latency as possible (still bounded by
         * CanBridge_Transmit()'s own mailbox wait). */
        if (send_response(t, i, frame->echo)) {
          s_fired_count++;
        } else {
          /* Ящики заняты — не дропаем молча: переводим отправку на
           * путь с ретраями в Trigger_Poll(). */
          arm_response(i, t, frame->echo);
        }
      } else {
        arm_response(i, t, frame->echo);
      }
      if (t->rx_fire_limit != 0U
          && ++s_rx_state[i].count >= t->rx_fire_limit) {
        s_rx_state[i].suppress = 1U;
      }
    }
  }
}

void Trigger_Poll(void)
{
  uint32_t now = HAL_GetTick();
  /* STAGE без COMMIT протухает: оборванная сессия записи (USB-обрыв
   * между STAGE и COMMIT) держала staged-записи вооружёнными
   * бесконечно — см. TRIGGER_STAGE_TIMEOUT_MS. */
  if (s_staged_max != 0U
      && (uint32_t)(now - s_stage_tick) > TRIGGER_STAGE_TIMEOUT_MS) {
    s_staged_max = 0U;
    memset(s_stage_flags, 0, sizeof(s_stage_flags));
  }
  for (uint8_t i = 0; i < TRIGGER_MAX_RECORDS; i++) {
    if (s_pending[i].armed && (int32_t)(now - s_pending[i].fire_at_tick) >= 0) {
      uint32_t lateness = now - s_pending[i].fire_at_tick;
      if (lateness > s_max_lateness_ms) {
        s_max_lateness_ms = lateness;
      }
      uint8_t index = s_pending[i].trigger_index;
      const trigger_t *t = &s_triggers[index];
      if (!send_response(t, index, s_pending[i].echo)) {
        /* Ящики заняты: попытку не сжигаем, переарм на ~1 мс. Раньше
         * провал тут молча съедал отправку — ответ триггера пропадал
         * на нагруженной шине без следа ни в одном счётчике. */
        if (s_pending[i].retries < TRIGGER_TX_RETRY_MAX) {
          s_pending[i].retries++;
          s_pending[i].fire_at_tick = now + TRIGGER_TX_RETRY_DELAY_MS;
          continue;
        }
        s_dropped_count++;
      } else {
        s_fired_count++;
      }
      s_pending[i].retries = 0U;
      /* Повторные отправки («Кол-во отправок» > 1): переарм на
       * tx_interval_ms; в кэш-режиме каждый повтор шлёт уже свежие
       * данные кэша. */
      if (s_pending[i].remaining > 1U) {
        s_pending[i].remaining--;
        s_pending[i].fire_at_tick = now + s_pending[i].interval_ms;
      } else {
        s_pending[i].armed = 0U;
      }
    }
  }
}

void Trigger_GetStats(uint32_t *fired_count, uint32_t *max_lateness_ms,
                      uint32_t *dropped_count)
{
  if (fired_count != NULL) {
    *fired_count = s_fired_count;
  }
  if (max_lateness_ms != NULL) {
    *max_lateness_ms = s_max_lateness_ms;
  }
  if (dropped_count != NULL) {
    *dropped_count = s_dropped_count;
  }
}

uint8_t Trigger_FlashValidCount(void)
{
  return s_flash_valid_count;
}
