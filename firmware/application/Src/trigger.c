/* Trigger engine implementation. See trigger.h and firmware/PROTOCOL.md
 * Part 3 for the on-Flash layout/rationale. */

#include <string.h>
#include <stddef.h>
#include "main.h"
#include "trigger.h"

/* sizeof(trigger_t) must be a multiple of 2 (half-word) so that
 * flash_write_all_triggers()'s halfword-by-halfword HAL_FLASH_Program()
 * loop below never truncates the last byte, and so that the write step
 * (sizeof(trigger_t)) matches the read step used by Trigger_Init(). Also
 * make sure all TRIGGER_COUNT slots still fit in the trigger Flash
 * region. */
_Static_assert((sizeof(trigger_t) % 2U) == 0U, "trigger_t size must be halfword-aligned");
_Static_assert(sizeof(trigger_t) == TRIGGER_RECORD_SIZE, "trigger_t must match wire record size");
_Static_assert((TRIGGER_COUNT * sizeof(trigger_t)) <= TRIGGER_PAGE_SIZE, "triggers must fit in the trigger Flash region");

static trigger_t s_triggers[TRIGGER_COUNT];
static trigger_t s_staged[TRIGGER_COUNT];
static uint8_t s_stage_flags[TRIGGER_COUNT]; /* per-slot staged mark —
 * битовой маски uint16_t хватало на 16 слотов, а TRIGGER_COUNT=49 */
/* Rollback-копия для Commit — статическая: 49×82=4018 Б на стеке при
 * гарантированных 2 КБ (_Min_Stack_Size=0x800) привели бы к переполнению. */
static trigger_t s_commit_backup[TRIGGER_COUNT];

/* Кэш режима «автоматическая запись DATA»: последний кадр, попавший в
 * фильтр «Откуда читаем». Хранится в RAM, не во Flash — перезапись
 * страницы на каждый кадр исчерпала бы ресурс стираний (~10k циклов). */
static can_frame_t s_cache[TRIGGER_COUNT];
static uint8_t s_cache_valid[TRIGGER_COUNT];

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
} pending_response_t;

static pending_response_t s_pending[TRIGGER_COUNT];
static uint32_t s_fired_count;
static uint32_t s_max_lateness_ms;

static uint8_t trigger_fields_valid(const trigger_t *trig);

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

void Trigger_Init(void)
{
  memset(s_pending, 0, sizeof(s_pending));
  memset(s_staged, 0, sizeof(s_staged));
  memset(s_stage_flags, 0, sizeof(s_stage_flags));
  memset(s_cache, 0, sizeof(s_cache));
  memset(s_cache_valid, 0, sizeof(s_cache_valid));
  s_fired_count = 0U;
  s_max_lateness_ms = 0U;

  const uint8_t *page = (const uint8_t *)TRIGGER_PAGE_ADDR;
  for (uint8_t i = 0; i < TRIGGER_COUNT; i++) {
    const trigger_t *flash_t = (const trigger_t *)(page + (uint32_t)i * sizeof(trigger_t));
    if (flash_t->magic == TRIGGER_MAGIC) {
      uint8_t computed = crc8((const uint8_t *)flash_t, offsetof(trigger_t, crc8));
      if (computed == flash_t->crc8
          && ((flash_t->reserved[0] == 0U && flash_t->reserved[1] == 0U)
              || (flash_t->reserved[0] == TRIGGER_FORMAT_VERSION
                  && flash_t->reserved[1] == TRIGGER_RECORD_SIZE))
          && trigger_fields_valid(flash_t)) {
        memcpy(&s_triggers[i], flash_t, sizeof(trigger_t));
        continue;
      }
    }
    load_default(&s_triggers[i]);
  }
}

uint8_t Trigger_Get(uint8_t index, trigger_t *out)
{
  if (index >= TRIGGER_COUNT) {
    return 0U;
  }
  *out = s_triggers[index];
  return 1U;
}

static uint8_t flash_write_all_triggers(void)
{
  HAL_FLASH_Unlock();

  FLASH_EraseInitTypeDef erase_init = {
    .TypeErase   = FLASH_TYPEERASE_PAGES,
    .PageAddress = TRIGGER_PAGE_ADDR,
    .NbPages     = TRIGGER_PAGE_SIZE / 2048U,
  };
  uint32_t page_error = 0U;
  if (HAL_FLASHEx_Erase(&erase_init, &page_error) != HAL_OK) {
    HAL_FLASH_Lock();
    return 0U;
  }

  uint32_t addr = TRIGGER_PAGE_ADDR;
  for (uint8_t i = 0; i < TRIGGER_COUNT; i++) {
    const uint16_t *src = (const uint16_t *)&s_triggers[i];
    for (uint32_t w = 0; w < (sizeof(trigger_t) / 2U); w++) {
      if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, addr, src[w]) != HAL_OK) {
        HAL_FLASH_Lock();
        return 0U;
      }
      addr += 2U;
    }
  }

  HAL_FLASH_Lock();
  return 1U;
}

static uint8_t trigger_fields_valid(const trigger_t *trig)
{
  /* Каналы 0-based как в UI: 0=CAN1, 1=CAN2, 2=оба канала — на приём
   * матчит любой канал, на ответ шлёт в оба. src_* — матчер «Откуда
   * читаем» кэш-режима. */
  if (trig == NULL || trig->rx_channel > 2U || trig->tx_channel > 2U
      || trig->rx_extended > 1U || trig->tx_extended > 1U
      || trig->rx_dlc > 8U || trig->tx_dlc > 8U
      || trig->tx_rtr > 1U || trig->cache_enabled > 1U
      || trig->src_channel > 2U || trig->src_extended > 1U
      || trig->src_dlc > 8U) {
    return 0U;
  }
  uint32_t rx_max = trig->rx_extended ? 0x1FFFFFFFU : 0x7FFU;
  uint32_t tx_max = trig->tx_extended ? 0x1FFFFFFFU : 0x7FFU;
  uint32_t src_max = trig->src_extended ? 0x1FFFFFFFU : 0x7FFU;
  return (trig->rx_id <= rx_max && trig->rx_id_mask <= rx_max
          && trig->tx_id <= tx_max && trig->src_id <= src_max) ? 1U : 0U;
}

uint8_t Trigger_Stage(uint8_t index, const trigger_t *trig)
{
  if (index >= TRIGGER_COUNT || !trigger_fields_valid(trig)) {
    return 0U;
  }
  trigger_t staged = *trig;
  staged.magic = TRIGGER_MAGIC;
  staged.reserved[0] = TRIGGER_FORMAT_VERSION;
  staged.reserved[1] = TRIGGER_RECORD_SIZE;
  staged.crc8 = crc8((const uint8_t *)&staged, offsetof(trigger_t, crc8));
  s_staged[index] = staged;
  s_stage_flags[index] = 1U;
  return 1U;
}

uint8_t Trigger_Commit(void)
{
  uint8_t any_staged = 0U;
  for (uint8_t i = 0U; i < TRIGGER_COUNT; i++) {
    if (s_stage_flags[i] != 0U) {
      any_staged = 1U;
      break;
    }
  }
  if (any_staged == 0U) {
    return 1U;
  }
  memcpy(s_commit_backup, s_triggers, sizeof(s_commit_backup));
  for (uint8_t i = 0U; i < TRIGGER_COUNT; i++) {
    if (s_stage_flags[i] != 0U) {
      s_triggers[i] = s_staged[i];
    }
  }
  if (!flash_write_all_triggers()) {
    memcpy(s_triggers, s_commit_backup, sizeof(s_triggers));
    return 0U;
  }
  memset(s_stage_flags, 0, sizeof(s_stage_flags));
  /* Перезаписанные слоты не должны использовать кэш, собранный по
   * старым критериям «Откуда читаем». */
  memset(s_cache_valid, 0, sizeof(s_cache_valid));
  return 1U;
}

uint8_t Trigger_Set(uint8_t index, const trigger_t *trig)
{
  if (!Trigger_Stage(index, trig)) {
    return 0U;
  }
  return Trigger_Commit();
}

uint8_t Trigger_SetEnabled(uint8_t index, uint8_t enabled)
{
  if (index >= TRIGGER_COUNT) {
    return 0U;
  }
  trigger_t updated = s_triggers[index];
  updated.enabled = enabled ? 1U : 0U;
  return Trigger_Set(index, &updated);
}

static uint8_t frame_matches(const trigger_t *t, const can_frame_t *frame)
{
  if (!t->enabled) {
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
  for (uint8_t i = 0; i < frame->dlc && i < 8U; i++) {
    if ((t->rx_data[i] & t->rx_data_mask[i]) != (frame->data[i] & t->rx_data_mask[i])) {
      return 0U;
    }
  }
  return 1U;
}

/* Матчер «Откуда читаем» кэш-режима: кадр кэшируется, если канал/битность/
 * ID совпали, а Data (big-endian, src_dlc байт) попадает в [from, to].
 * src_dlc == 0 — данные не проверяются (матч только по ID). */
static uint8_t src_matches(const trigger_t *t, const can_frame_t *frame)
{
  if (t->src_channel != 2U && t->src_channel != frame->channel) {
    return 0U;
  }
  if (t->src_extended != frame->extended) {
    return 0U;
  }
  if (t->src_id != frame->id) {
    return 0U;
  }
  if (t->src_dlc == 0U) {
    return 1U;
  }
  uint64_t from = 0U, to = 0U, value = 0U;
  for (uint8_t i = 0; i < t->src_dlc && i < 8U; i++) {
    from = (from << 8) | t->src_from[i];
    to = (to << 8) | t->src_to[i];
    /* Кадр короче src_dlc — недостающие байты считаются нулевыми, как в
     * host-логике _data_in_range() (ljust нулями). */
    value = (value << 8) | (i < frame->dlc ? frame->data[i] : 0U);
  }
  return (from <= value && value <= to) ? 1U : 0U;
}

static void arm_response(uint8_t index, const trigger_t *t)
{
  s_pending[index].armed = 1U;
  s_pending[index].trigger_index = index;
  s_pending[index].fire_at_tick = HAL_GetTick() + t->delay_ms;
  s_pending[index].remaining = t->tx_count ? t->tx_count : 1U;
  s_pending[index].interval_ms = t->tx_interval_ms;
}

/* Одна отправка ответа триггера. В кэш-режиме шлётся последний кадр из
 * s_cache[index] (ID/DLC/Data — как приняты с шины), в обычном — поля
 * tx_* записи. tx_channel хранится 0-based (индекс комбобокса UI), а
 * CanBridge_Transmit ждёт wire-нумерацию 1/2. */
static uint8_t send_response(const trigger_t *t, uint8_t index)
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
  for (uint8_t i = 0; i < TRIGGER_COUNT; i++) {
    const trigger_t *t = &s_triggers[i];
    /* Кэш пополняется независимо от условия «Приём» — триггер постоянно
     * переписывает Data последнего подходящего кадра. Обновление идёт
     * до проверки rx-матча: кадр, попавший в оба фильтра, обновит кэш
     * до отправки. */
    if (t->enabled && t->cache_enabled && src_matches(t, frame)) {
      s_cache[i] = *frame;
      s_cache_valid[i] = 1U;
    }
    if (frame_matches(t, frame)) {
      uint8_t sends = t->tx_count ? t->tx_count : 1U;
      if (t->delay_ms == 0U && sends == 1U) {
        /* Zero delay, single shot: send immediately, no need to go
         * through the pending list — keeps the "instant echo" case as
         * low-latency as possible (still bounded by
         * CanBridge_Transmit()'s own mailbox wait). */
        if (send_response(t, i)) {
          s_fired_count++;
        }
      } else {
        arm_response(i, t);
      }
    }
  }
}

void Trigger_Poll(void)
{
  uint32_t now = HAL_GetTick();
  for (uint8_t i = 0; i < TRIGGER_COUNT; i++) {
    if (s_pending[i].armed && (int32_t)(now - s_pending[i].fire_at_tick) >= 0) {
      uint32_t lateness = now - s_pending[i].fire_at_tick;
      if (lateness > s_max_lateness_ms) {
        s_max_lateness_ms = lateness;
      }
      uint8_t index = s_pending[i].trigger_index;
      const trigger_t *t = &s_triggers[index];
      if (send_response(t, index)) {
        s_fired_count++;
      }
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

void Trigger_GetStats(uint32_t *fired_count, uint32_t *max_lateness_ms)
{
  if (fired_count != NULL) {
    *fired_count = s_fired_count;
  }
  if (max_lateness_ms != NULL) {
    *max_lateness_ms = s_max_lateness_ms;
  }
}
