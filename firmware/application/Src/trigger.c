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
 * make sure all 10 slots still fit in the trigger Flash page. */
_Static_assert((sizeof(trigger_t) % 2U) == 0U, "trigger_t size must be halfword-aligned");
_Static_assert((TRIGGER_COUNT * sizeof(trigger_t)) <= TRIGGER_PAGE_SIZE, "triggers must fit in the trigger Flash page");

static trigger_t s_triggers[TRIGGER_COUNT];

/* Pending deferred responses (delay_ms > 0). A small fixed-size list is
 * enough since there are at most TRIGGER_COUNT=10 triggers and each can
 * have at most one response in flight at a time (a new match on the same
 * trigger while one is already pending simply re-arms it). */
typedef struct {
  uint8_t  armed;
  uint8_t  trigger_index;
  uint32_t fire_at_tick;
} pending_response_t;

static pending_response_t s_pending[TRIGGER_COUNT];
static uint32_t s_fired_count;
static uint32_t s_max_lateness_ms;

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
                  && flash_t->reserved[1] == TRIGGER_RECORD_SIZE))) {
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
    .NbPages     = 1U,
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

uint8_t Trigger_Set(uint8_t index, const trigger_t *trig)
{
  if (index >= TRIGGER_COUNT) {
    return 0U;
  }

  trigger_t new_t = *trig;
  new_t.magic = TRIGGER_MAGIC;
  new_t.reserved[0] = TRIGGER_FORMAT_VERSION;
  new_t.reserved[1] = TRIGGER_RECORD_SIZE;
  new_t.crc8 = crc8((const uint8_t *)&new_t, offsetof(trigger_t, crc8));

  if (memcmp(&new_t, &s_triggers[index], sizeof(new_t)) == 0) {
    return 1U;
  }

  trigger_t backup = s_triggers[index];
  s_triggers[index] = new_t;

  if (!flash_write_all_triggers()) {
    s_triggers[index] = backup; /* roll back RAM mirror on Flash failure */
    return 0U;
  }
  return 1U;
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
  if (t->rx_channel != frame->channel) {
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

static void arm_response(uint8_t index, const trigger_t *t)
{
  s_pending[index].armed = 1U;
  s_pending[index].trigger_index = index;
  s_pending[index].fire_at_tick = HAL_GetTick() + t->delay_ms;
}

void Trigger_OnFrame(const can_frame_t *frame)
{
  for (uint8_t i = 0; i < TRIGGER_COUNT; i++) {
    if (frame_matches(&s_triggers[i], frame)) {
      if (s_triggers[i].delay_ms == 0U) {
        /* Zero delay: send immediately, no need to go through the pending
         * list — keeps the "instant echo" case as low-latency as possible
         * (still bounded by CanBridge_Transmit()'s own mailbox wait). */
        can_frame_t resp = {
          .channel = s_triggers[i].tx_channel,
          .extended = s_triggers[i].tx_extended,
          .id = s_triggers[i].tx_id,
          .dlc = s_triggers[i].tx_dlc,
        };
        memcpy(resp.data, s_triggers[i].tx_data, 8);
        if (CanBridge_Transmit(&resp)) {
          s_fired_count++;
        }
      } else {
        arm_response(i, &s_triggers[i]);
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
      s_pending[i].armed = 0U;
      const trigger_t *t = &s_triggers[s_pending[i].trigger_index];
      can_frame_t resp = {
        .channel = t->tx_channel,
        .extended = t->tx_extended,
        .id = t->tx_id,
        .dlc = t->tx_dlc,
      };
      memcpy(resp.data, t->tx_data, 8);
      if (CanBridge_Transmit(&resp)) {
        s_fired_count++;
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
