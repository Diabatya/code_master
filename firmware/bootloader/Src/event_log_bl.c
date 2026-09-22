/* Минимальная append-only запись в кольцо журнала событий приложения.
 *
 * Пул и формат записи идентичны firmware/application/Src/event_log.c:
 * 4 страницы по 2048 Б с 0x0803B000, записи 16 Б, CRC8 poly 0x07 init 0x00.
 *
 * Жёсткое правило: бутлоадер НИКОГДА не стирает страницы журнала — если
 * следующий слот не чист, запись просто пропускается. Стиранием кольца
 * управляет только приложение; потерять одно событие безопаснее, чем
 * дать бутлоадеру erase-путь по чужой области.
 *
 * Зачем это нужно: промежуток «устройство в загрузчике» раньше был слепым —
 * мастер прошивает железку, а журнал приложения молчит (приложение в этот
 * момент не работает). Теперь вход в загрузчик виден в логе, включая случай
 * «приложение невалидно/отсутствует» — самое раннее событие во всей системе. */

#include "stm32f1xx_hal.h"
#include "event_log_bl.h"

#define EVLOG_POOL_BASE    0x0803B000UL
#define EVLOG_PAGE_SIZE    2048U
#define EVLOG_PAGES        4U
#define EVLOG_RECORD_SIZE  16U
#define EVLOG_SLOTS        (EVLOG_PAGES * EVLOG_PAGE_SIZE / EVLOG_RECORD_SIZE) /* 512 */
#define EVLOG_MAGIC        0x4C45U   /* 'EL' little-endian */
#define EVLOG_TYPE_BL      11U       /* EVLOG_BOOTLOADER */

typedef struct __attribute__((packed)) {
  uint16_t magic;          /* 'EL' */
  uint32_t seq;            /* глобальный монотонный счётчик, 1..0xFFFFFFFF */
  uint32_t timestamp_ms;   /* HAL_GetTick() момента записи */
  uint8_t  type;
  uint8_t  channel;        /* 0xFF */
  uint8_t  code;           /* причина входа в загрузчик */
  uint8_t  crc8;
  uint8_t  pad[2];
} evlog_rec_t;

_Static_assert(sizeof(evlog_rec_t) == EVLOG_RECORD_SIZE, "record layout");

static uint8_t evlog_crc8(const uint8_t *data, uint32_t len)
{
  uint8_t crc = 0U;
  for (uint32_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint32_t b = 0; b < 8U; b++) {
      crc = (crc & 0x80U) ? (uint8_t)((crc << 1) ^ 0x07U) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

/* Записывает одну запись «вход в загрузчик» в следующий слот кольца.
 * code: 1 — остались по флагу хоста, 2 — приложение невалидно/отсутствует.
 * Тихий выход, если слот занят (стирать из бутлоадера нельзя) или Flash
 * не запрограммировалась — бутлоадер должен остаться рабочим при любом
 * состоянии журнала. */
void BlEventLog_NoteBoot(uint8_t code)
{
  /* Находим запись с максимальным seq — следующий слот идёт за ней по кольцу.
   * Скан 512 слотов — чистое чтение Flash, ~мкс на 72 МГц. */
  uint32_t best_seq = 0U;
  int32_t  best_slot = -1;
  for (uint32_t slot = 0; slot < EVLOG_SLOTS; slot++) {
    const evlog_rec_t *r = (const evlog_rec_t *)(EVLOG_POOL_BASE + slot * EVLOG_RECORD_SIZE);
    if ((r->magic == EVLOG_MAGIC) && (r->seq != 0U) && (r->seq != 0xFFFFFFFFUL) &&
        (r->crc8 == evlog_crc8((const uint8_t *)r, 15U)) && (r->seq >= best_seq)) {
      best_seq = r->seq;
      best_slot = (int32_t)slot;
    }
  }

  uint32_t next = (best_slot >= 0) ? ((uint32_t)best_slot + 1U) % EVLOG_SLOTS : 0U;
  const uint32_t *slot_words = (const uint32_t *)(EVLOG_POOL_BASE + next * EVLOG_RECORD_SIZE);
  for (uint32_t i = 0; i < EVLOG_RECORD_SIZE / 4U; i++) {
    if (slot_words[i] != 0xFFFFFFFFUL) {
      return; /* слот занят — без erase-полномочий писать некуда */
    }
  }

  evlog_rec_t rec;
  rec.magic = EVLOG_MAGIC;
  rec.seq = best_seq + 1U;
  rec.timestamp_ms = HAL_GetTick();
  rec.type = EVLOG_TYPE_BL;
  rec.channel = 0xFFU;
  rec.code = code;
  rec.pad[0] = 0U;
  rec.pad[1] = 0U;
  rec.crc8 = evlog_crc8((const uint8_t *)&rec, 15U);

  HAL_FLASH_Unlock();
  const uint16_t *src = (const uint16_t *)&rec;
  uint32_t dst = EVLOG_POOL_BASE + next * EVLOG_RECORD_SIZE;
  for (uint32_t i = 0; i < EVLOG_RECORD_SIZE / 2U; i++, dst += 2U) {
    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, dst, src[i]) != HAL_OK) {
      break; /* частичная запись отбрасывается CRC/магией при следующем скане */
    }
  }
  HAL_FLASH_Lock();
}
