/* Trigger engine implementation. See trigger.h and firmware/PROTOCOL.md
 * Part 3 for the on-Flash layout/rationale. */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "trigger.h"
#include "storage.h"

/* sizeof(trigger_t) must be a multiple of 2 (half-word) so that
 * flash_write_store()'s halfword-by-halfword HAL_FLASH_Program()
 * loop below never truncates the last byte, and so that the write step
 * (sizeof(trigger_t)) matches the read step used by Trigger_Init(). */
_Static_assert((sizeof(trigger_t) % 2U) == 0U, "trigger_t size must be halfword-aligned");
_Static_assert(sizeof(trigger_t) == TRIGGER_RECORD_SIZE, "trigger_t must match wire record size");
_Static_assert(sizeof(trigger_header_t) == TRIGGER_HEADER_SIZE, "trigger_header_t must be 16 bytes");

static trigger_t s_triggers[TRIGGER_ALL_RECORDS];
static trigger_t s_staged[TRIGGER_MAX_RECORDS];
static uint8_t s_stage_flags[TRIGGER_MAX_RECORDS];
static uint8_t s_staged_max;    /* наибольший staged-индекс +1 */
static uint32_t s_stage_tick;   /* тик последнего STAGE */
static uint8_t s_count;         /* активных записей в списке
                                 * (пользовательские + хвост ГЛ) */
static uint8_t s_user_count;    /* записей из хранилища TRGH — граница,
                                 * до которой работают STAGE/COMMIT/
                                 * Get/Count; хвост за ней — записи ГЛ */

static uint32_t s_store_base;   /* адрес заголовка области во Flash, 0 — нет */
static uint32_t s_store_pages;  /* страниц, занятых текущей областью */
static uint32_t s_generation;   /* generation последнего коммита */

/* Кэш режима «автоматическая запись DATA»: последний кадр, попавший в
 * фильтр «Откуда читаем». Хранится в RAM, не во Flash — перезапись
 * страницы на каждый кадр исчерпала бы ресурс стираний (~10k циклов).
 * Только пользовательские записи [0..TRIGGER_MAX_RECORDS): записи ГЛ
 * кэшем не пользуются — снимок DATA события для X-подстановки хранит
 * сама очередь запусков (s_flex_queue). */
static can_frame_t s_cache[TRIGGER_MAX_RECORDS];
static uint8_t s_cache_valid[TRIGGER_MAX_RECORDS];

/* Pending deferred responses (delay_ms > 0 or repeat count > 1). A
 * fixed-size list is enough since there are at most TRIGGER_COUNT
 * triggers and each can have at most one response in flight at a time
 * (a new match on the same trigger while one is already pending simply
 * re-arms it). Только пользовательские записи — у записей ГЛ каждое
 * совпадение события ставит ОТДЕЛЬНУЮ серию в s_flex_queue (на ПК
 * _send_frame/QTimer планирует каждый запуск независимо, ревью CM-R1). */
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

/* Очередь запусков записей ГЛ (хвост списка): каждый элемент — одна
 * серия отправок с собственным сроком, числом повторов, интервалом и
 * снимком DATA кадра-события для X-подстановки. На ПК то же самое
 * делает множество независимых QTimer; здесь глубина ограничена RAM —
 * переполнение не теряет команду молча: серия отбрасывается с учётом
 * в s_dropped_count (ревью CM-R1). */
typedef struct {
  uint32_t fire_at_tick;   /* ПЛАНОВЫЙ срок следующей отправки серии —
                            * ретраи его не трогают: ближайшая попытка
                            * = fire_at_tick + retries*RETRY_DELAY
                            * (ревью CM-R8: отказ не сдвигает серию) */
  uint8_t  data[8];        /* pad-8 снимок DATA кадра-события */
  uint16_t interval_ms;
  uint8_t  trigger_index;
  uint8_t  remaining;      /* оставшиеся отправки серии */
  uint8_t  echo;
  uint8_t  retries;        /* попытки отправки при занятых TX-ящиках */
  uint8_t  used;
} flex_firing_t;
static flex_firing_t s_flex_queue[FLEX_QUEUE_DEPTH];

static uint32_t s_fired_count;
static uint32_t s_dropped_count; /* отправки, исчерпавшие ретраи или
                                  * потерянные при переполнении очереди */
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
static fire_state_t s_rx_state[TRIGGER_ALL_RECORDS];   /* условие приёма
                                 * (записям ГЛ нужен трекинг смены DATA) */
static fire_state_t s_src_state[TRIGGER_MAX_RECORDS];  /* источник кэша —
                                 * только пользовательские записи */

static uint8_t trigger_fields_valid(const trigger_t *trig);
static uint8_t erase_pages(uint32_t from, uint32_t to);
static uint8_t program_store(uint32_t base, uint32_t generation, uint8_t total,
                             const trigger_t **src);

/* Позиция области триггеров для раскладки storage.c. */
uint32_t Trigger_StoreBase(void)
{
  return s_store_base;
}

uint32_t Trigger_StorePages(void)
{
  return s_store_pages;
}

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
  /* Сквозной скан всего свободного пространства за кодом: область
   * триггеров плавает (storage.c пересаживает её при росте
   * переменных/ГЛ), жёсткого адреса нет — только маркер TRGH. */
  for (uint32_t page = Storage_CodeEnd(); page < TRIGGER_FLASH_END;
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

/* --- Автономная гибкая логика (протокол 12) -------------------------
 * ПК при сохранении ГЛ упаковывает в полезную нагрузку блоба FLXH
 * контейнер FLXB:
 *   [0..3]  магия «FLXB», [4] версия контейнера = 1,
 *   [5]     число скомпилированных записей N,
 *   [6..7]  зарезервировано, [8..11] длина JSON u32 LE,
 *   [12..12+J) JSON-конфигурация (для вычитки настройки на ПК),
 *   далее   N записей trigger_t (90 Б с crc8 — как в TRGH).
 * Программа ГЛ компилируется в записи, когда ВСЕ события — «Фрейм»
 * или «Включение устройства», условий нет, а все действия — отправки
 * кадров; остальные программы исполняет ПК, как раньше. */
static void append_flex(void)
{
  /* Хвост списка пересобирается прямо из Flash — отдельная
   * мастер-копия в ОЗУ не нужна (RAM почти исчерпана кольцевыми
   * буферами CAN). Очередь запусков старых записей не переживает
   * пересборку: индексы хвоста могли сместиться. */
  s_count = s_user_count;
  memset(s_flex_queue, 0, sizeof(s_flex_queue));
  uint32_t len = 0U;
  const uint8_t *p = Storage_FlexPayload(&len);
  if (p == NULL || len < 12U) {
    return;
  }
  if (p[0] != 'F' || p[1] != 'L' || p[2] != 'X' || p[3] != 'B'
      || p[4] != 1U) {
    return; /* блог старого приложения — чистый JSON без записей */
  }
  uint8_t count = p[5];
  uint32_t json_len = (uint32_t)p[8] | ((uint32_t)p[9] << 8)
                      | ((uint32_t)p[10] << 16) | ((uint32_t)p[11] << 24);
  /* Точный контракт ИСПОЛНЯЕМОГО хвоста, одинаковый с ПК (ревью
   * CM-R3/CM-R6): count ≤ FLEX_MAX_RECORDS (отказ, не обрезание),
   * json_len > 0 и 12 + json_len + count*sizeof(trigger_t) == len
   * ровно — без хвоста и без недостачи. Содержимое JSON прошивка
   * НЕ проверяет и не использует — контракт исполнителя определяется
   * только геометрией и записями; валидность JSON-конфигурации —
   * зона ПК (см. core/flex_compiler.py). Сложение защищено: json_len
   * сравнивается с len-12 ДО вычисления смещения — иначе при
   * json_len≈0xFFFFFFFF rec_off переполнялся в 32 битах и
   * «проверка границ» принимала мусор (ревью CM-R6). */
  if (count > FLEX_MAX_RECORDS || json_len == 0U || json_len > len - 12U) {
    return;
  }
  uint32_t rec_off = 12U + json_len;
  if ((uint32_t)count * sizeof(trigger_t) != len - rec_off) {
    return;
  }
  /* Валидация ВСЕХ записей до загрузки: одна битая запись (CRC/поля)
   * отклоняет контейнер целиком — иначе часть программ исполнялась бы
   * МК, а ПК считал бы managed по правилам всё: искомые программы
   * оказались бы мёртвыми либо исполнялись бы вдвойне. ПК валидирует
   * тем же набором проверок (core/flex_compiler.py::parse_flex_blob). */
  for (uint8_t i = 0U; i < count; i++) {
    trigger_t probe;
    if (!load_record(p + rec_off + (uint32_t)i * sizeof(trigger_t),
                     sizeof(trigger_t), &probe)) {
      return;
    }
  }
  for (uint8_t i = 0U;
       i < count && s_count < TRIGGER_ALL_RECORDS; i++) {
    memcpy(&s_triggers[s_count],
           p + rec_off + (uint32_t)i * sizeof(trigger_t),
           sizeof(trigger_t));
    s_count++;
  }
}

void Trigger_ReloadFlex(void)
{
  /* append_flex сам сбрасывает очередь запусков; защёлки «смены DATA»
   * хвоста относились к прежним записям — чистим их диапазон.
   * s_pending/s_cache/s_src_state хвост не используют — пользовательская
   * часть списка тут не трогается вообще. */
  append_flex();
  memset(&s_rx_state[s_user_count], 0,
         sizeof(fire_state_t) * (TRIGGER_ALL_RECORDS - s_user_count));
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
  s_user_count = 0U;
  s_staged_max = 0U;
  s_stage_tick = 0U;
  s_store_base = 0U;
  s_store_pages = 0U;
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
    s_store_pages = (TRIGGER_HEADER_SIZE + (uint32_t)count * stride
                     + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE;
    s_generation = gen;
    Storage_NoteTrig(base, s_store_pages);
    /* Легаси-регион при живом новом хранилище — мусорный резерв
     * (миграция оборвалась после записи области, но до стирания
     * легаси): записи уже подняты из TRGH, источник дублирует их —
     * стираем и снимаем резерв у storage.c. */
    if (*(const uint32_t *)TRIGGER_LEGACY_ADDR == TRIGGER_MAGIC) {
      uint32_t legacy_end = TRIGGER_LEGACY_ADDR
          + ((TRIGGER_LEGACY_COUNT * TRIGGER_RECORD_SIZE_V2
              + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE)
            * TRIGGER_FLASH_PAGE;
      (void)erase_pages(TRIGGER_LEGACY_ADDR, legacy_end);
      Storage_NoteLegacyTrigDone();
    }
    /* Хвост списка — скомпилированные записи ГЛ из блоба FLXH. */
    s_user_count = s_count;
    Trigger_ReloadFlex();
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

  /* Миграция финализируется сразу: поднятые записи пишем в зазор
   * «посередине» (см. storage.c), легаси-регион стираем после успеха —
   * при обрыве питания посреди записи следующий старт снова найдёт
   * источник и повторит переезд. Без финализации легаси
   * реимпортировалось при КАЖДОМ старте: удалённый, но ни разу не
   * сохранённый триггер воскресал «фантомом» после каждого ребута. */
  if (s_count != 0U) {
    const trigger_t *src[TRIGGER_MAX_RECORDS];
    for (uint8_t j = 0U; j < s_count; j++) {
      src[j] = &s_triggers[j];
    }
    uint32_t size = TRIGGER_HEADER_SIZE + (uint32_t)s_count * sizeof(trigger_t);
    uint32_t pages = (size + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE;
    uint32_t gap_lo = Storage_TrigGapBase();
    uint32_t gap_hi = Storage_TrigGapEnd();
    uint32_t base = 0U;
    if (gap_lo + pages * TRIGGER_FLASH_PAGE <= gap_hi) {
      /* Каноничное место — вплотную под журналом: область заканчивается
       * на центре зазора («триггеры снизу вверх от центра», отчёт
       * мастера). Обязателен RangeFree: легаси-пул журнала внутри
       * зазора резервирован до миграции записей в EVLH. */
      const uint32_t cands[3] = {
        gap_hi - pages * TRIGGER_FLASH_PAGE,
        (gap_lo + (gap_hi - gap_lo - pages * TRIGGER_FLASH_PAGE) / 2U)
            & ~(TRIGGER_FLASH_PAGE - 1U),
        gap_lo,
      };
      for (uint8_t c = 0U; c < 3U; c++) {
        if (cands[c] >= gap_lo
            && cands[c] + pages * TRIGGER_FLASH_PAGE <= gap_hi
            && Storage_RangeFree(cands[c],
                                 cands[c] + pages * TRIGGER_FLASH_PAGE)) {
          base = cands[c];
          break;
        }
      }
    }
    if (base != 0U
        && erase_pages(base, base + pages * TRIGGER_FLASH_PAGE)
        && program_store(base, 1U, s_count, src)) {
      s_store_base = base;
      s_store_pages = pages;
      s_generation = 1U;
      Storage_NoteTrig(base, pages);
      /* Легаси-страницы стираем только ПОСЛЕ валидного нового
       * хранилища; резерв storage снимаем при успехе. */
      uint32_t legacy_end = TRIGGER_LEGACY_ADDR
          + ((TRIGGER_LEGACY_COUNT * TRIGGER_RECORD_SIZE_V2
              + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE)
            * TRIGGER_FLASH_PAGE;
      (void)erase_pages(TRIGGER_LEGACY_ADDR, legacy_end);
      Storage_NoteLegacyTrigDone();
    }
    /* Сбой записи не фатален: список уже в RAM и работает, переезд
     * повторится при следующем старте. */
  }
  /* Хвост списка — скомпилированные записи ГЛ из блоба FLXH. */
  s_user_count = s_count;
  Trigger_ReloadFlex();
}

uint8_t Trigger_Get(uint8_t index, trigger_t *out)
{
  /* Хост видит только пользовательскую часть списка — записи ГЛ
   * в хвосте ей не показываются (не должны появляться «фантомными»
   * триггерами в вычитке). */
  if (index >= s_user_count) {
    return 0U;
  }
  *out = s_triggers[index];
  return 1U;
}

uint8_t Trigger_Count(void)
{
  return s_user_count;
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

/* Стирает все области триггеров (активная TRGH + зомби-регионы после
 * обрывов + легаси v2). Пул как диапазон больше не существует —
 * чужие области (VARH/EVLH/FLXH) не трогаем. */
static uint8_t erase_trig_regions(void)
{
  for (uint32_t page = Storage_CodeEnd(); page < TRIGGER_FLASH_END;
       page += TRIGGER_FLASH_PAGE) {
    const trigger_header_t *h = (const trigger_header_t *)page;
    if (h->magic != TRIGGER_HEADER_MAGIC) {
      continue;
    }
    /* Размер области считается только из ВАЛИДНОГО заголовка (version/
     * count/crc8 — те же проверки, что у find_store): мусорный count
     * из повреждённого заголовка иначе стирал бы страницы соседних
     * областей (VARH/EVLH) — маркер TRGH сам по себе не граница. */
    uint8_t stride;
    if (h->version == TRIGGER_STORE_VERSION) {
      stride = TRIGGER_RECORD_SIZE;
    } else if (h->version == TRIGGER_STORE_VERSION_V3) {
      stride = TRIGGER_RECORD_SIZE_V2;
    } else {
      stride = 0U;
    }
    uint32_t pages;
    if (stride != 0U && h->count <= TRIGGER_MAX_RECORDS
        && crc8((const uint8_t *)h, offsetof(trigger_header_t, crc8))
               == h->crc8) {
      uint32_t bytes = TRIGGER_HEADER_SIZE + (uint32_t)h->count * stride;
      pages = (bytes + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE;
      if (page + pages * TRIGGER_FLASH_PAGE > TRIGGER_FLASH_END) {
        pages = (TRIGGER_FLASH_END - page) / TRIGGER_FLASH_PAGE;
      }
    } else {
      pages = 1U; /* дохлый маркер — вытираем только свою страницу */
    }
    if (!erase_pages(page, page + pages * TRIGGER_FLASH_PAGE)) {
      return 0U;
    }
  }
  /* Легаси-регион v2 вытираем тоже — иначе его записи снова
   * смигрируют при следующем старте после «Заводских настроек». */
  uint32_t legacy_end = TRIGGER_LEGACY_ADDR
      + ((TRIGGER_LEGACY_COUNT * TRIGGER_RECORD_SIZE_V2
          + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE)
        * TRIGGER_FLASH_PAGE;
  if (!erase_pages(TRIGGER_LEGACY_ADDR, legacy_end)) {
    return 0U;
  }
  Storage_NoteLegacyTrigDone();
  return 1U;
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
  erase_trig_regions();
  s_user_count = 0U;
  /* Записи ГЛ живут в блобе FLXH — «очистка триггеров» их не трогает:
   * хвост списка пересобирается из мастер-копии. */
  append_flex();
  s_staged_max = 0U;
  s_stage_tick = 0U;
  s_store_base = 0U;
  s_store_pages = 0U;
  s_flash_valid_count = 0U;
  Storage_NoteTrig(0U, 0U);
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
      || (trig->rx_flags & ~(TRIGGER_F_MUTE_ECHO | TRIGGER_F_FIRE_ON_BOOT
                             | TRIGGER_F_COND_MASK
                             | TRIGGER_F_TX_EVENT_DATA)) != 0U
      || (trig->src_flags & ~(TRIGGER_F_MUTE_ECHO
                              | TRIGGER_F_CACHE_ONLY)) != 0U) {
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
    total = s_user_count > s_staged_max ? s_user_count : s_staged_max;
  }
  if (total > TRIGGER_MAX_RECORDS) {
    return 0U;
  }
  if (s_staged_max == 0U && total == s_user_count) {
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
    } else if (j < s_user_count) {
      src[j] = &s_triggers[j];
    } else {
      src[j] = &empty_rec;
    }
  }

  /* Область триггеров плавает «посередине» свободного пространства
   * (между EVLH/VARH сверху и FLXH снизу — границы даёт storage.c).
   * 0 записей — 0 страниц: хранилище отсутствует, память свободна. */
  uint32_t size = (total != 0U)
      ? TRIGGER_HEADER_SIZE + (uint32_t)total * sizeof(trigger_t)
      : 0U;
  uint32_t pages = (size + TRIGGER_FLASH_PAGE - 1U) / TRIGGER_FLASH_PAGE;
  uint32_t gap_lo = Storage_TrigGapBase();
  uint32_t gap_hi = Storage_TrigGapEnd();
  uint32_t base_mid = 0U;
  if (total != 0U && gap_lo + pages * TRIGGER_FLASH_PAGE <= gap_hi) {
    /* Канонично область заканчивается на центре зазора — вплотную под
     * журналом («триггеры снизу вверх от центра», отчёт мастера). */
    base_mid = gap_hi - pages * TRIGGER_FLASH_PAGE;
  } else if (total != 0U) {
    return 0U; /* зазор не вмещает список — переменные/ГЛ съели место */
  }

  /* Атомарная замена: новая область пишется в страницы, НЕ занятые
   * действующим хранилищем (кандидаты — середина и края зазора), и
   * становится видимой только когда дописан заголовок (program_store
   * пишет его последним). Сброс посреди коммита — IWDG, обрыв USB,
   * питание — оставляет ПРЕЖНИЙ список целым. Если все кандидаты
   * пересекаются со старым — стираем старую область и пишем на её
   * место: обрыв тогда даёт пустое хранилище, а не мусор. */
  uint32_t base = 0xFFFFFFFFU;
  uint32_t old_base = s_store_base;
  uint32_t old_end = 0U;
  if (old_base != 0U) {
    old_end = old_base + s_store_pages * TRIGGER_FLASH_PAGE;
    if (old_end > TRIGGER_FLASH_END) {
      old_end = TRIGGER_FLASH_END;
    }
  }
  if (total != 0U && old_base != 0U) {
    const uint32_t anchors[3] = { base_mid, gap_hi - pages * TRIGGER_FLASH_PAGE,
                                  gap_lo };
    for (uint8_t c = 0U; c < 3U; c++) {
      uint32_t cand = anchors[c];
      uint32_t cand_end = cand + pages * TRIGGER_FLASH_PAGE;
      if (cand < gap_lo || cand_end > gap_hi) {
        continue;
      }
      if (cand >= old_end || cand_end <= old_base) {
        /* Не пересекается со старым хранилищем — но должно быть
         * свободно по storage (резервы легаси-регионов и прочие
         * области зазора тоже считаются). */
        if (Storage_RangeFree(cand, cand_end)) {
          base = cand;
          break;
        }
      }
    }
  } else if (total != 0U) {
    /* Старого хранилища нет — в середину зазора, если она свободна. */
    if (Storage_RangeFree(base_mid, base_mid + pages * TRIGGER_FLASH_PAGE)) {
      base = base_mid;
    } else {
      const uint32_t cands[2] = { gap_hi - pages * TRIGGER_FLASH_PAGE,
                                  gap_lo };
      for (uint8_t c = 0U; c < 2U; c++) {
        if (cands[c] >= gap_lo
            && cands[c] + pages * TRIGGER_FLASH_PAGE <= gap_hi
            && Storage_RangeFree(cands[c],
                                 cands[c] + pages * TRIGGER_FLASH_PAGE)) {
          base = cands[c];
          break;
        }
      }
    }
  }

  if (total == 0U) {
    if (!erase_trig_regions()) {
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
    s_store_pages = pages;
    Storage_NoteTrig(base, pages);
  } else {
    /* Все свободные кандидаты заняты/пересекаются со старым — стираем
     * старую область и пишем на её место: обрыв здесь даёт пустое
     * хранилище, а не мусор (RAM-список обновляется только при успехе).
     * Старой области нет (old_base == 0) — in-place писать некуда:
     * erase_pages(0, ...) стёр бы страницы от адреса 0 (бутлоадер) —
     * честный отказ вместо кирпича. */
    if (old_base == 0U) {
      return 0U;
    }
    uint32_t new_end = old_base + pages * TRIGGER_FLASH_PAGE;
    if (new_end > old_end && !Storage_RangeFree(old_end, new_end)) {
      return 0U; /* рост списка упрётся в чужую область — отказ */
    }
    if (!erase_pages(old_base,
                     new_end > old_end ? new_end : old_end)) {
      return 0U;
    }
    if (!program_store(old_base, s_generation + 1U, total, src)) {
      return 0U; /* RAM-список не трогаем: на Flash старое/мусор */
    }
    base = old_base;
    s_store_pages = pages;
    Storage_NoteTrig(base, pages);
  }

  for (uint8_t j = 0U; j < total; j++) {
    s_triggers[j] = *src[j];
  }
  /* Хвост записей ГЛ пересобирается за новой пользовательской частью —
   * записи из FLXH коммитом триггеров не задеваются. */
  s_user_count = total;
  append_flex();
  s_store_base = (total != 0U) ? base : 0U;
  if (total == 0U) {
    s_store_pages = 0U;
    Storage_NoteTrig(0U, 0U);
  }
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
  uint8_t total = s_user_count;
  if (index >= total) {
    total = (uint8_t)(index + 1U);
  }
  return Trigger_Commit(total);
}

/* Пересадка области на новый адрес — вызывается из storage.c, когда
 * растущая область переменных/ГЛ наезжает на триггеры. Содержимое
 * берётся из RAM-копии списка (область — только проекция). Новое
 * место стирается, пишется целиком, старые страницы вычищаются. */
uint8_t Trigger_Relocate(uint32_t new_base)
{
  if (s_store_base == 0U || s_user_count == 0U) {
    return 1U; /* переносить нечего */
  }
  if (new_base < Storage_CodeEnd()
      || new_base + s_store_pages * TRIGGER_FLASH_PAGE > TRIGGER_FLASH_END) {
    return 0U;
  }
  if (new_base == s_store_base) {
    return 1U;
  }
  /* Переносится только область TRGH — пользовательские записи;
   * записи ГЛ в неё не входят (они из блоба FLXH). */
  const trigger_t *src[TRIGGER_MAX_RECORDS];
  for (uint8_t j = 0U; j < s_user_count; j++) {
    src[j] = &s_triggers[j];
  }
  /* Целевой диапазон стирается целиком — он может частично пересекаться
   * со старой областью, но список в RAM, так что это не страшно. */
  if (!erase_pages(new_base, new_base + s_store_pages * TRIGGER_FLASH_PAGE)) {
    return 0U;
  }
  uint32_t old_base = s_store_base;
  uint32_t old_end = old_base + s_store_pages * TRIGGER_FLASH_PAGE;
  if (!program_store(new_base, s_generation + 1U, s_user_count, src)) {
    return 0U;
  }
  s_generation++;
  s_store_base = new_base;
  Storage_NoteTrig(new_base, s_store_pages);
  /* Страницы старой области вне новой — зомби, вытираем. */
  if (old_base < new_base) {
    (void)erase_pages(old_base, new_base < old_end ? new_base : old_end);
  }
  if (old_end > new_base + s_store_pages * TRIGGER_FLASH_PAGE) {
    (void)erase_pages(new_base + s_store_pages * TRIGGER_FLASH_PAGE, old_end);
  }
  return 1U;
}

uint8_t Trigger_SetEnabled(uint8_t index, uint8_t enabled)
{
  if (index >= s_user_count) {
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
     * смысла и пропускается. Байт с маской 0x00 — wildcard «X».
     * Байты за пределами DLC считаются нулевыми, как в host-логике
     * (_tokens_match / _cache_src_matches): маска, выставленная на
     * позицию, которой нет в коротком кадре, на ПК давала «не совпало»
     * (0 != ожидаемое), а на МК молча пропускалась — программы ГЛ
     * с длинным шаблоном DATA срабатывали на коротких кадрах. */
    for (uint8_t i = 0; i < 8U; i++) {
      if (t->rx_data_mask[i] == 0U) {
        continue;
      }
      uint8_t b = (i < frame->dlc) ? frame->data[i] : 0U;
      if ((t->rx_data[i] & t->rx_data_mask[i]) != (b & t->rx_data_mask[i])) {
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
 * триггер снова исполняет до rx/src_fire_limit сработок. Для
 * пользовательских триггеров вызывается только когда лимит != 0; для
 * записей ГЛ (хвост списка) — всегда (трекинг смены DATA для лимита
 * сработок).
 * full_data=1 (записи ГЛ): «смена DATA» — сравнение НОРМАЛИЗОВАННЫХ
 * восьми байт кадра, как _pad_8 на ПК — байты за пределами DLC
 * считаются нулевыми, и сама DLC сменой DATA не является
 * (11 / 11 00 — одинаковые). full_data=0 — байты, значимые в маске
 * фильтра, плюс DLC (семантика пользовательских триггеров не
 * меняется — ревью CM-R2). */
static void fire_track(fire_state_t *st, const can_frame_t *frame,
                       const trigger_t *t, uint8_t is_src,
                       uint8_t full_data)
{
  uint8_t dlc = frame->dlc > 8U ? 8U : frame->dlc;
  uint8_t changed = (uint8_t)(st->have == 0U);
  if (changed == 0U && full_data != 0U) {
    for (uint8_t i = 0; i < 8U; i++) {
      uint8_t cur = (i < dlc) ? frame->data[i] : 0U;
      if (cur != st->last[i]) {
        changed = 1U;
        break;
      }
    }
  }
  if (changed == 0U && full_data == 0U) {
    changed = (uint8_t)(st->last_dlc != dlc);
    for (uint8_t i = 0; changed == 0U && i < 8U; i++) {
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
 * цепочку заново и пинг-понг триггеров не ограничивался.
 * event_data — pad-8 снимок DATA кадра-события для X-подстановки
 * (записи ГЛ берут его из элемента очереди — у каждого запуска свой
 * кадр); NULL — читать s_cache[index] (пользовательские триггеры). */
static uint8_t send_response(const trigger_t *t, uint8_t index,
                             uint8_t echo, const uint8_t *event_data)
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
    /* ГЛ: «X»-байты ответа подставляются из кадра события. */
    if (t->rx_flags & TRIGGER_F_TX_EVENT_DATA) {
      const uint8_t *subst = event_data;
      if (subst == NULL && index < TRIGGER_MAX_RECORDS
          && s_cache_valid[index]) {
        subst = s_cache[index].data;
      }
      for (uint8_t j = 0; j < 8U; j++) {
        if (t->src_from[j] == 0xFFU) {
          resp.data[j] = (subst != NULL) ? subst[j] : 0U;
        }
      }
    }
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

/* Постановка серии записи ГЛ в очередь: серия сохраняет свой срок,
 * число повторов, интервал и снимок DATA события для X-подстановки —
 * на ПК то же расписание дают независимые QTimer (_send_frame,
 * delay + n*between). Переполнение → 0: серия отбрасывается с учётом
 * в s_dropped_count — потеря диагностируется, молчаливого дропа нет
 * (ревью CM-R1). */
static uint8_t flex_enqueue(uint8_t index, const trigger_t *t,
                            const uint8_t *event_data, uint8_t echo,
                            uint32_t fire_at)
{
  for (uint8_t q = 0U; q < FLEX_QUEUE_DEPTH; q++) {
    flex_firing_t *e = &s_flex_queue[q];
    if (e->used != 0U) {
      continue;
    }
    e->used = 1U;
    e->trigger_index = index;
    e->fire_at_tick = fire_at;
    e->remaining = t->tx_count ? t->tx_count : 1U;
    e->interval_ms = t->tx_interval_ms;
    e->echo = echo;
    e->retries = 0U;
    if (event_data != NULL) {
      memcpy(e->data, event_data, 8U);
    } else {
      memset(e->data, 0, sizeof(e->data));
    }
    return 1U;
  }
  return 0U;
}

/* Взвод запуска записи ГЛ: мгновенную отправку (delay=0, count=1)
 * пробуем сразу — отказ ящиков ставит серию в очередь с нулевым
 * сроком (ретраи в Trigger_Poll), остальные серии ждут свой тик. */
static void flex_fire(uint8_t index, const trigger_t *t,
                      const can_frame_t *frame)
{
  uint8_t sends = t->tx_count ? t->tx_count : 1U;
  uint8_t ev_data[8];
  const uint8_t *evp = NULL;
  if (t->rx_flags & TRIGGER_F_TX_EVENT_DATA) {
    memset(ev_data, 0, sizeof(ev_data));
    memcpy(ev_data, frame->data,
           frame->dlc < 8U ? frame->dlc : 8U);
    evp = ev_data;
  }
  if (t->delay_ms == 0U && sends == 1U) {
    if (send_response(t, index, frame->echo, evp)) {
      s_fired_count++;
      return;
    }
  }
  if (!flex_enqueue(index, t, evp, frame->echo,
                    HAL_GetTick() + t->delay_ms)) {
    /* Очередь переполнена — серия потеряна, но не молча: считаем в
     * dropped (Trigger_GetStats → CMD_TRIGGER_STATS на ПК). */
    s_dropped_count += sends;
  }
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
        fire_track(&s_src_state[i], frame, t, 1U, 0U);
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
    /* CACHE_ONLY — запись только наполняет кэш, ответ не вооружается:
     * связка «кэш + обычный ответ» одного UI-триггера раскладывается в
     * филлеры (cache_enabled+CACHE_ONLY) и ответные записи группы. */
    if (t->src_flags & TRIGGER_F_CACHE_ONLY) {
      continue;
    }
    /* rx_flags&MUTE_ECHO — триггер не реагирует на кадры, отправленные
     * самим МК (TX-эхо); 0 — слушает и внешние, и свои (как в v2).
     * FIRE_ON_BOOT — вооружается один раз в Trigger_FireOnBoot(), кадры
     * шины его не возбуждают (поля приёма в UI при галке заблокированы). */
    if ((t->rx_flags & TRIGGER_F_FIRE_ON_BOOT) != 0U
        || (frame->echo != 0U && (t->rx_flags & TRIGGER_F_MUTE_ECHO) != 0U)
        || !rx_header_matches(t, frame)) {
      continue;
    }
    if (i >= s_user_count) {
      /* Запись, скомпилированная из программы ГЛ (хвост списка —
       * записей пользовательских триггеров не касается). Порядок
       * повторяет _event_fired на ПК:
       * 1) «смена DATA» считается по pad_8 — всем восьми нормализованным
       *    байтам кадра (CM-R2);
       * 2) запоминается только последний ПОЛНОСТЬЮ совпавший кадр
       *    (ПК обновляет last после _frame_event_matches) — кадр с
       *    тем же ID, но не подходящей DATA лимит не сбрасывает;
       * 3) подавления «программа занята» для скомпилированных
       *    программ НЕТ: на ПК _rule_busy растёт только от
       *    exec_delay-отсрочки, а такие действия компилятор на МК
       *    не отдаёт — каждый совпавший кадр ставит свою серию
       *    отправок со своим расписанием (ревью CM-R1). */
      if (!rx_data_matches(t, frame)) {
        continue;
      }
      fire_track(&s_rx_state[i], frame, t, 0U, 1U);
      if (s_rx_state[i].suppress != 0U) {
        continue;
      }
    } else {
      if (t->rx_fire_limit != 0U) {
        fire_track(&s_rx_state[i], frame, t, 0U, 0U);
      }
      if (s_rx_state[i].suppress != 0U || !rx_data_matches(t, frame)) {
        continue;
      }
    }
    if (i >= s_user_count) {
      flex_fire(i, t, frame);
    } else {
      if (t->rx_flags & TRIGGER_F_TX_EVENT_DATA) {
        /* Запись с подстановкой «X из события»: кадр, взведший
         * программу, складываем в слот кэша — send_response берёт
         * из него позиции src_from[j]==0xFF. Байты за DLC кадра
         * HAL не заполняет — там мусор стека; обнуляем (_pad_8). */
        s_cache[i] = *frame;
        if (frame->dlc < 8U) {
          memset(&s_cache[i].data[frame->dlc], 0, 8U - frame->dlc);
        }
        s_cache_valid[i] = 1U;
      }
      uint8_t sends = t->tx_count ? t->tx_count : 1U;
      if (t->delay_ms == 0U && sends == 1U) {
        /* Zero delay, single shot: send immediately, no need to go
         * through the pending list — keeps the "instant echo" case as
         * low-latency as possible (still bounded by
         * CanBridge_Transmit()'s own mailbox wait). */
        if (send_response(t, i, frame->echo, NULL)) {
          s_fired_count++;
        } else {
          /* Ящики заняты — не дропаем молча: переводим отправку на
           * путь с ретраями в Trigger_Poll(). */
          arm_response(i, t, frame->echo);
        }
      } else {
        arm_response(i, t, frame->echo);
      }
    }
    if (t->rx_fire_limit != 0U
        && ++s_rx_state[i].count >= t->rx_fire_limit) {
      s_rx_state[i].suppress = 1U;
    }
  }
}

void Trigger_FireOnBoot(void)
{
  /* «Сработка после старта устройства»: включённые записи с
   * rx_flags&FIRE_ON_BOOT вооружаются один раз здесь — условие приёма у
   * них заменено событием запуска (Trigger_OnFrame их пропускает).
   * Путь отправки общий: мгновенная — напрямую, с задержкой/повторами —
   * через s_pending в Trigger_Poll; занятые ящики добираются ретраями. */
  for (uint8_t i = 0; i < s_count; i++) {
    const trigger_t *t = &s_triggers[i];
    if (!t->enabled || (t->rx_flags & TRIGGER_F_FIRE_ON_BOOT) == 0U) {
      continue;
    }
    uint8_t sends = t->tx_count ? t->tx_count : 1U;
    /* boot_delay_ms — пауза от старта МК до сработки (протокол 7):
     * складывается с delay_ms записи — вооружаем с суммарным тиком.
     * Записи ГЛ (хвост) идут в s_flex_queue — как запуски по кадру:
     * кадра события нет, снимок DATA нулевой (как у ПК — пустой
     * фрейм события «Включение устройства»). */
    if (i >= s_user_count) {
      (void)flex_enqueue(i, t, NULL, 0U,
                         HAL_GetTick() + t->delay_ms
                         + t->boot_delay_ms);
      continue;
    }
    if (t->boot_delay_ms == 0U && t->delay_ms == 0U && sends == 1U) {
      if (send_response(t, i, 0U, NULL)) {
        s_fired_count++;
      } else {
        arm_response(i, t, 0U);
      }
    } else {
      arm_response(i, t, 0U);
      s_pending[i].fire_at_tick += t->boot_delay_ms;
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
    /* Плановый срок (fire_at_tick) и ближайшая попытка хранятся
     * раздельно: срок попытки = fire_at + retries*RETRY_DELAY. Отказ
     * ящиков отодвигает попытку на ~1 мс, но НЕ сдвигает расписание
     * серии — следующие плановые отправки идут от исходных сроков
     * (ревью CM-R8). */
    uint32_t due = s_pending[i].fire_at_tick
                   + (uint32_t)s_pending[i].retries
                     * TRIGGER_TX_RETRY_DELAY_MS;
    if (s_pending[i].armed && (int32_t)(now - due) >= 0) {
      uint32_t lateness = now - s_pending[i].fire_at_tick;
      if (lateness > s_max_lateness_ms) {
        s_max_lateness_ms = lateness;
      }
      uint8_t index = s_pending[i].trigger_index;
      const trigger_t *t = &s_triggers[index];
      if (!send_response(t, index, s_pending[i].echo, NULL)) {
        /* Ящики заняты: попытку не сжигаем, переарм на ~1 мс
         * (retries+1 сам отодвигает due — fire_at не трогаем).
         * Раньше провал тут молча съедал отправку — ответ триггера
         * пропадал на нагруженной шине без следа ни в одном
         * счётчике. */
        if (s_pending[i].retries < TRIGGER_TX_RETRY_MAX) {
          s_pending[i].retries++;
          continue;
        }
        /* Ретраи исчерпаны — серия отбрасывается целиком (как в
         * очереди ГЛ): на мёртвой шине оставшиеся повторы горели бы
         * те же 20 мс, потеря серии считается в dropped разом. */
        s_dropped_count += s_pending[i].remaining;
        s_pending[i].armed = 0U;
        s_pending[i].retries = 0U;
        continue;
      } else {
        s_fired_count++;
      }
      s_pending[i].retries = 0U;
      /* Повторные отправки («Кол-во отправок» > 1): следующая —
       * в ПЛАНОВЫЙ срок fire_at += interval, а не «через interval от
       * текущей попытки»: краткий отказ (1050→1051) не сдвигает серию
       * 1070/1090 (ревью CM-R8). Просроченный плановый срок просто
       * исполняется ближайшим Poll — одна отправка за проход.
       * В кэш-режиме каждый повтор шлёт уже свежие данные кэша. */
      if (s_pending[i].remaining > 1U) {
        s_pending[i].remaining--;
        s_pending[i].fire_at_tick += s_pending[i].interval_ms;
      } else {
        s_pending[i].armed = 0U;
      }
    }
  }
  /* Серии записей ГЛ: у каждой свой плановый срок — расписание серии
   * сохраняется полностью (ревью CM-R1). Повторы идут от исходного
   * fire_at += interval, как у абсолютных QTimer на ПК. Отказ ящиков
   * отодвигает только ближайшую попытку (due = fire_at + retries*1
   * мс), плановые сроки не сдвигаются (ревью CM-R8); за один проход
   * Poll из серии отправляется один кадр — просроченные плановые
   * сроки догоняются по одному за тик опроса. */
  for (uint8_t q = 0U; q < FLEX_QUEUE_DEPTH; q++) {
    flex_firing_t *e = &s_flex_queue[q];
    uint32_t due = e->fire_at_tick
                   + (uint32_t)e->retries * TRIGGER_TX_RETRY_DELAY_MS;
    if (e->used == 0U || (int32_t)(now - due) < 0) {
      continue;
    }
    uint32_t lateness = now - e->fire_at_tick;
    if (lateness > s_max_lateness_ms) {
      s_max_lateness_ms = lateness;
    }
    uint8_t index = e->trigger_index;
    if (index >= s_count) {
      e->used = 0U; /* запись убрали (перезапись блоба) */
      continue;
    }
    const trigger_t *t = &s_triggers[index];
    const uint8_t *evp =
        (t->rx_flags & TRIGGER_F_TX_EVENT_DATA) != 0U ? e->data : NULL;
    if (!send_response(t, index, e->echo, evp)) {
      /* Ящики заняты: попытку не сжигаем, переарм на ~1 мс — тот же
       * ограниченный механизм ретраев, что и у пользовательских
       * записей; исчерпание считается в dropped (ревью CM-R1).
       * Ограниченная политика при длительном отказе: после
       * TRIGGER_TX_RETRY_MAX попыток серия отбрасывается целиком —
       * бесконечного цикла и неконтролируемого залпа нет, потеря
       * диагностируется счётчиком (ревью CM-R8). */
      if (e->retries < TRIGGER_TX_RETRY_MAX) {
        e->retries++;
        continue;
      }
      s_dropped_count += e->remaining;
      e->used = 0U;
      continue;
    }
    s_fired_count++;
    e->retries = 0U;
    if (e->remaining > 1U) {
      e->remaining--;
      e->fire_at_tick += e->interval_ms;
    } else {
      e->used = 0U;
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
