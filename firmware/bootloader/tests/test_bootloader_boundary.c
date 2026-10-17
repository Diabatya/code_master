/* Host boundary harness for the PRODUCTION AN3155 dispatcher in
 * firmware/bootloader/Src/bootloader.c (CM-FR02): the file under test
 * is included textually, CDC byte streams are fed through the same
 * public entry point (Bootloader_Task) the USB stack calls.
 *
 * Covered contract:
 *  - WRITE MEMORY: identity page 0x08008000-0x080087FF is read-only —
 *     any range touching it (start inside, middle VER1 slot, crossing
 *     the upper boundary) gets NACK and ZERO flash-program calls;
 *     metadata 0x08008800 and code 0x08009000+ still program; ranges
 *     above WRITABLE_END keep being refused;
 *  - ERASE/EXTENDED ERASE: page 16 (identity) is rejected; a page list
 *     containing a forbidden page anywhere — including the LAST
 *     position — is rejected before the first erase (zero erase ops);
 *     mass erase 0xFFFF and legacy erase 0x43/0xFF cover exactly
 *     pages 17..119;
 *  - READ MEMORY keeps serving the identity page (read-only != hidden);
 *  - GET VERSION reports the bumped bootloader revision (>=0x32);
 *  - GO refuses anything but APP_START.
 *
 * The STM32 flash array is modelled by a byte array: HAL_FLASH_Program /
 * HAL_FLASHEx_Erase / CDC_Transmit_FS translate the absolute target
 * addresses into it. Additionally the harness tries to place a real
 * mapping at 0x08000000 (mmap MAP_FIXED); when it succeeds, the
 * production code paths that dereference absolute flash pointers
 * (READ payload source, bl_app_is_valid on GO) run against it too —
 * on hosts where a fixed low mapping is impossible those checks are
 * reported as SKIP rather than silently dropped.
 *
 * Build & run (from the repository root):
 *   cc -I firmware/bootloader/tests/stubs -I firmware/bootloader/Inc \
 *      -I firmware/bootloader/Src \
 *      firmware/bootloader/tests/test_bootloader_boundary.c \
 *      -o /tmp/test_bootloader_boundary
 *   /tmp/test_bootloader_boundary
 */

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <stddef.h>
#include <stdlib.h>
#include <sys/mman.h>

#include "main.h"
#include "bootloader.h"
#include "usbd_cdc_if.h"

/* ---- Modelled flash ------------------------------------------------ */
#define FLASH_BASE_HOST 0x08000000UL
#define FLASH_SIZE_HOST (256U * 1024U)
/* Настоящее отображение по реальному базовому адресу (если удалось) —
 * по нему читают прямые разыменования production-кода. */
static volatile uint8_t *s_flash_live;
/* Логическая модель — источник/приёмник операций с адресной
 * трансляцией; при успешном mmap ссылается на то же отображение. */
static uint8_t s_flash_model[FLASH_SIZE_HOST];

static void flash_model_erase_all(void)
{
  memset((void *)s_flash_model, 0xFF, FLASH_SIZE_HOST);
  if (s_flash_live != NULL) {
    memset((void *)s_flash_live, 0xFF, FLASH_SIZE_HOST);
  }
}

static uint8_t *flash_cell(uint32_t addr)
{
  if (addr < FLASH_BASE_HOST
      || addr >= FLASH_BASE_HOST + FLASH_SIZE_HOST) {
    return NULL;
  }
  return s_flash_model + (addr - FLASH_BASE_HOST);
}

/* ---- Controllable HAL_GetTick -------------------------------------- */
static uint32_t s_tick;
uint32_t HAL_GetTick(void)
{
  /* Каждый запрос времени двигает часы — таймауты bl_read_byte на
   * пустом RX истекают сразу, харнесс не висит на ожидании. */
  return s_tick++;
}
void HAL_Delay(uint32_t Delay) { s_tick += Delay; }

/* ---- Peripheral globals referenced by the stub header -------------- */
PWR_TypeDef StubPWR;
BKP_TypeDef StubBKP;
RCC_TypeDef StubRCC;
SysTick_TypeDef StubSysTick;
NVIC_Stub_TypeDef StubNVIC;
SCB_Stub_TypeDef StubSCB;
static uint32_t s_control, s_msp;
void Stub_set_CONTROL(uint32_t v) { s_control = v; }
void Stub_set_MSP(uint32_t v) { s_msp = v; }
void USB_DevDisconnect(void *instance) { (void)instance; }
void App_KickWatchdog(void) {}
void Error_Handler(void) {}

/* ---- HAL Flash adapters: program/erase act on the model ------------ */
static uint32_t s_program_calls;
static uint32_t s_erase_calls;
static uint32_t s_erased_pages[128];
static uint32_t s_erased_count;

void HAL_FLASH_Unlock(void) {}
void HAL_FLASH_Lock(void) {}

HAL_StatusTypeDef HAL_FLASH_Program(uint32_t TypeProgram,
                                    uint32_t Address, uint64_t Data)
{
  (void)TypeProgram;
  s_program_calls++;
  uint8_t *cell = flash_cell(Address);
  if (cell == NULL || flash_cell(Address + 1U) == NULL) {
    return HAL_ERROR;
  }
  /* Flash пишет только стирание битов 1→0 — как кремний. */
  cell[0] &= (uint8_t)(Data & 0xFFU);
  cell[1] &= (uint8_t)((Data >> 8) & 0xFFU);
  if (s_flash_live != NULL) {
    volatile uint8_t *live = s_flash_live + (Address - FLASH_BASE_HOST);
    live[0] &= (uint8_t)(Data & 0xFFU);
    live[1] &= (uint8_t)((Data >> 8) & 0xFFU);
  }
  return HAL_OK;
}

HAL_StatusTypeDef HAL_FLASHEx_Erase(FLASH_EraseInitTypeDef *pEraseInit,
                                    uint32_t *PageError)
{
  (void)PageError;
  for (uint32_t i = 0U; i < pEraseInit->NbPages; i++) {
    uint32_t addr = pEraseInit->PageAddress + i * 2048U;
    s_erase_calls++;
    if (s_erased_count < 128U) {
      s_erased_pages[s_erased_count++] = addr;
    }
    uint8_t *cell = flash_cell(addr);
    if (cell != NULL && flash_cell(addr + 2047U) != NULL) {
      memset(cell, 0xFF, 2048U);
      if (s_flash_live != NULL) {
        memset((void *)(s_flash_live + (addr - FLASH_BASE_HOST)),
               0xFF, 2048U);
      }
    }
  }
  return HAL_OK;
}

/* ---- CDC transport model -------------------------------------------- */
static uint8_t s_rx[8192];
static uint32_t s_rx_len, s_rx_pos;
static uint8_t s_tx[65536];
static uint32_t s_tx_len;

uint8_t CDC_Transmit_FS(uint8_t *Buf, uint16_t Len)
{
  /* READ MEMORY шлёт прямым указателем на адрес Flash: транслируем
   * адрес в модель (или читаем живое отображение — байты те же). */
  uintptr_t a = (uintptr_t)Buf;
  const uint8_t *src = Buf;
  if (a >= FLASH_BASE_HOST && a + Len <= FLASH_BASE_HOST + FLASH_SIZE_HOST) {
    src = s_flash_model + (a - FLASH_BASE_HOST);
  }
  if (s_tx_len + Len <= sizeof(s_tx)) {
    memcpy(s_tx + s_tx_len, src, Len);
    s_tx_len += Len;
  }
  return 0U;
}
uint16_t CDC_GetRxAvailable(void)
{
  return (uint16_t)(s_rx_len - s_rx_pos);
}
uint8_t CDC_ReadRxByte(void)
{
  return s_rx[s_rx_pos++];
}

static void rx_reset(void)
{
  s_rx_len = 0U;
  s_rx_pos = 0U;
}
static void tx_reset(void) { s_tx_len = 0U; }

static void rx_push(uint8_t b) { s_rx[s_rx_len++] = b; }
static void rx_push_bytes(const uint8_t *p, uint32_t n)
{
  memcpy(s_rx + s_rx_len, p, n);
  s_rx_len += n;
}

/* AN3155 команда: [cmd, cmd^0xFF] → первый ACK, затем тело → ACK/NACK. */
static void rx_cmd(uint8_t cmd)
{
  rx_push(cmd);
  rx_push((uint8_t)(cmd ^ 0xFFU));
}
static void rx_addr(uint32_t addr)
{
  uint8_t a[4] = {
    (uint8_t)(addr >> 24), (uint8_t)(addr >> 16),
    (uint8_t)(addr >> 8), (uint8_t)addr,
  };
  rx_push_bytes(a, 4U);
  rx_push((uint8_t)(a[0] ^ a[1] ^ a[2] ^ a[3]));
}
static void rx_write_payload(const uint8_t *data, uint16_t len)
{
  uint8_t n = (uint8_t)(len - 1U);
  uint8_t cs = n;
  rx_push(n);
  for (uint16_t i = 0U; i < len; i++) {
    rx_push(data[i]);
    cs ^= data[i];
  }
  rx_push(cs);
}
static uint8_t tx_last(void) { return s_tx_len ? s_tx[s_tx_len - 1U] : 0U; }

static int fails = 0;
#define CHECK(cond, msg) do { \
    if (!(cond)) { printf("FAIL: %s\n", msg); fails++; } \
    else { printf("PASS: %s\n", msg); } \
  } while (0)

#define ACK 0x79U
#define NACK 0x1FU

#define DEV_INFO   0x08008000U
#define META_ADDR  0x08008800U
#define APP_ADDR   0x08009000U
#define WRITE_END  0x0803C000U

static void write_cmd(uint32_t addr, const uint8_t *data, uint16_t len)
{
  rx_cmd(0x31U);
  rx_addr(addr);
  rx_write_payload(data, len);
  Bootloader_Task();
}

static void ext_erase_cmd(const uint16_t *pages, uint16_t count)
{
  uint8_t hdr[2] = {
    (uint8_t)((count - 1U) >> 8), (uint8_t)(count - 1U),
  };
  uint8_t cs = (uint8_t)(hdr[0] ^ hdr[1]);
  rx_cmd(0x44U);
  rx_push_bytes(hdr, 2U);
  for (uint16_t i = 0U; i < count; i++) {
    uint8_t p[2] = {
      (uint8_t)(pages[i] >> 8), (uint8_t)pages[i],
    };
    rx_push_bytes(p, 2U);
    cs = (uint8_t)(cs ^ p[0] ^ p[1]);
  }
  rx_push(cs);
  Bootloader_Task();
}

static void mass_erase_cmd(void)
{
  /* Extended erase 0xFFFF + checksum. */
  rx_cmd(0x44U);
  rx_push(0xFFU);
  rx_push(0xFFU);
  rx_push(0x00U); /* 0xFF ^ 0xFF */
  Bootloader_Task();
}

static void legacy_mass_erase_cmd(void)
{
  rx_cmd(0x43U);
  rx_push(0xFFU);
  rx_push(0x00U); /* 0xFF ^ 0xFF */
  Bootloader_Task();
}

/* Продакшн-диспетчер — текстуально, как в других харнессах проекта. */
#include "bootloader.c"

int main(void)
{
  /* Настоящее отображение по адресу Flash: на Linux (CI) MAP_FIXED в
   * низких адресах доступен, на macOS/arm64 — нет (4-GB __PAGEZERO).
   * Без него прямо-разыменовывающие проверки отмечаются SKIP. */
  void *m = mmap((void *)FLASH_BASE_HOST, FLASH_SIZE_HOST,
                 PROT_READ | PROT_WRITE,
                 MAP_PRIVATE | MAP_ANONYMOUS | MAP_FIXED, -1, 0);
  s_flash_live = (m == (void *)FLASH_BASE_HOST)
                   ? (volatile uint8_t *)m : NULL;
  printf("flash live map: %s\n",
         s_flash_live != NULL ? "at 0x08000000" : "unavailable (SKIP for dereference paths)");
  flash_model_erase_all();

  /* ---- GET VERSION: ревизия загрузчика >= 0x32 -------------------- */
  tx_reset(); rx_reset();
  rx_cmd(0x01U);
  Bootloader_Task();
  CHECK(s_tx_len >= 2U && s_tx[0] == ACK && s_tx[1] >= 0x32U,
        "GET VERSION reports >= 0x32 (identity-protected bootloader)");

  /* ---- WRITE MEMORY на страницу идентичности → NACK, 0 flash-op --- */
  uint8_t payload[32];
  memset(payload, 0x5A, sizeof(payload));
  uint32_t p0 = s_program_calls;

  tx_reset(); rx_reset();
  write_cmd(DEV_INFO, payload, 16U);
  CHECK(tx_last() == NACK, "WRITE @0x08008000 (identity start) NACK");
  CHECK(s_program_calls == p0, "identity write: zero flash ops");

  tx_reset(); rx_reset();
  write_cmd(DEV_INFO + 0x400U, payload, 32U); /* середина стр. (VER1) */
  CHECK(tx_last() == NACK, "WRITE @0x08008400 (VER1 slot) NACK");
  CHECK(s_program_calls == p0, "VER1-slot write: zero flash ops");

  /* Пересечение границы: старт в идентичности, конец — в метаданных. */
  tx_reset(); rx_reset();
  write_cmd(DEV_INFO + 0x7F0U, payload, 32U);
  CHECK(tx_last() == NACK, "WRITE crossing 0x08008800 upward NACK");
  CHECK(s_program_calls == p0, "boundary-crossing write: zero flash ops");

  /* Ниже страницы идентичности — область самого загрузчика. */
  tx_reset(); rx_reset();
  write_cmd(0x08007F00U, payload, 16U);
  CHECK(tx_last() == NACK, "WRITE @0x08007F00 (bootloader area) NACK");
  CHECK(s_program_calls == p0, "bootloader-area write: zero flash ops");

  /* ---- Разрешённые области продолжают писаться --------------------- */
  tx_reset(); rx_reset();
  write_cmd(META_ADDR, payload, 16U);
  CHECK(tx_last() == ACK, "WRITE @0x08008800 (metadata page) ACK");
  CHECK(memcmp(flash_cell(META_ADDR), payload, 16U) == 0,
        "metadata bytes landed in flash model");

  tx_reset(); rx_reset();
  write_cmd(APP_ADDR, payload, 8U);
  CHECK(tx_last() == ACK, "WRITE @0x08009000 (app code) ACK");

  /* Граница верха: последний байт разрешённого диапазона. */
  tx_reset(); rx_reset();
  write_cmd(WRITE_END - 16U, payload, 16U);
  CHECK(tx_last() == ACK, "WRITE ending at 0x0803C000 ACK");
  uint32_t p1 = s_program_calls;
  tx_reset(); rx_reset();
  write_cmd(WRITE_END - 15U, payload, 16U);
  CHECK(tx_last() == NACK, "WRITE spilling past 0x0803C000 NACK");
  CHECK(s_program_calls == p1, "upper-boundary spill: zero flash ops");

  /* ---- Extended erase: страница идентичности запрещена ------------- */
  uint16_t list16[] = {16U};
  tx_reset(); rx_reset();
  uint32_t e0 = s_erase_calls;
  ext_erase_cmd(list16, 1U);
  CHECK(tx_last() == NACK, "ext erase page 16 (identity) NACK");
  CHECK(s_erase_calls == e0, "page-16 erase: zero erase ops");

  /* Запрещённая страница В КОНЦЕ списка — до неё не должно дойти. */
  uint16_t list_tail[] = {17U, 18U, 16U};
  tx_reset(); rx_reset();
  ext_erase_cmd(list_tail, 3U);
  CHECK(tx_last() == NACK, "ext erase list [17,18,16] NACK");
  CHECK(s_erase_calls == e0,
        "forbidden page at list end: zero erase ops (pre-validated)");

  /* Разрешённые страницы стираются. */
  uint16_t list_ok[] = {17U, 18U, 119U};
  memset(flash_cell(META_ADDR), 0xA5, 2048U);
  tx_reset(); rx_reset();
  ext_erase_cmd(list_ok, 3U);
  CHECK(tx_last() == ACK, "ext erase [17,18,119] ACK");
  CHECK(flash_cell(META_ADDR)[0] == 0xFFU
        && flash_cell(META_ADDR)[2047] == 0xFFU,
        "page 17 erased in flash model");

  uint16_t list120[] = {120U};
  tx_reset(); rx_reset();
  ext_erase_cmd(list120, 1U);
  CHECK(tx_last() == NACK, "ext erase page 120 (event log) NACK");
  CHECK(s_erase_calls == e0 + 3U, "page-120 erase: zero erase ops");

  /* ---- Mass erase: ровно страницы 17..119 --------------------------- */
  flash_model_erase_all();
  memset(flash_cell(DEV_INFO), 0x11, 2048U);
  memset(flash_cell(WRITE_END), 0x22, 2048U);
  tx_reset(); rx_reset(); s_erased_count = 0U;
  mass_erase_cmd();
  CHECK(tx_last() == ACK, "mass erase ACK");
  CHECK(s_erased_count == 103U,
        "mass erase covers exactly pages 17..119 (103 pages)");
  CHECK(s_erased_count == 0U
        || (s_erased_pages[0] == META_ADDR
            && s_erased_pages[s_erased_count - 1U]
               == FLASH_BASE_HOST + 119U * 2048U),
        "mass erase span: first=0x08008800, last=page 119");
  CHECK(flash_cell(DEV_INFO)[0] == 0x11U,
        "identity page survives mass erase");
  CHECK(flash_cell(WRITE_END)[0] == 0x22U,
        "upper storage survives mass erase");

  /* Легаси Erase 0x43 mass — тот же диапазон 17..119. */
  memset(flash_cell(DEV_INFO), 0x33, 2048U);
  tx_reset(); rx_reset(); s_erased_count = 0U;
  legacy_mass_erase_cmd();
  CHECK(tx_last() == ACK, "legacy mass erase ACK");
  CHECK(flash_cell(DEV_INFO)[0] == 0x33U,
        "identity page survives legacy mass erase");
  CHECK(s_erased_count == 103U,
        "legacy mass erase covers pages 17..119");

  /* ---- READ MEMORY: идентичность читается (read-only) -------------- */
  memset(flash_cell(DEV_INFO), 0x77, 16U);
  tx_reset(); rx_reset();
  rx_cmd(0x11U);
  rx_addr(DEV_INFO);
  rx_push(15U); /* N=15 → 16 байт */
  Bootloader_Task();
  CHECK(s_tx_len >= 2U && s_tx[0] == ACK && s_tx[1] == ACK,
        "READ @0x08008000 (identity) ACK — read stays allowed");
  CHECK(s_tx_len >= 18U
        && s_tx[s_tx_len - 1U] == 0x77U
        && s_tx[s_tx_len - 16U] == 0x77U,
        "identity page bytes returned");

  /* Чтение области самого загрузчика — по-прежнему запрещено. */
  tx_reset(); rx_reset();
  rx_cmd(0x11U);
  rx_addr(0x08000000U);
  rx_push(15U);
  Bootloader_Task();
  CHECK(tx_last() == NACK, "READ @0x08000000 (bootloader) NACK");

  /* ---- GO: только APP_START --------------------------------------- */
  tx_reset(); rx_reset();
  rx_cmd(0x21U);
  rx_addr(DEV_INFO);
  Bootloader_Task();
  CHECK(tx_last() == NACK, "GO to identity page NACK");

  if (s_flash_live != NULL) {
    /* GO на APP_START: приложение стёрто — ACK команде, прыжка нет
     * (bl_app_is_valid по настоящему отображению отказывает). */
    tx_reset(); rx_reset();
    rx_cmd(0x21U);
    rx_addr(APP_ADDR);
    Bootloader_Task();
    CHECK(tx_last() == ACK, "GO to APP_START ACKed");
    CHECK(s_msp == 0U && s_control == 0U, "no jump on invalid app");
  } else {
    printf("SKIP: GO-to-APP_START (no live flash map on this host)\n");
  }

  /* ---- Мусор/плохая контрольная сумма → NACK ----------------------- */
  tx_reset(); rx_reset();
  rx_push(0x31U);
  rx_push(0x00U); /* должно быть 0x31^0xFF=0xCE */
  Bootloader_Task();
  CHECK(tx_last() == NACK, "bad command checksum NACK");

  if (fails == 0) {
    printf("ALL PASS\n");
    return 0;
  }
  printf("%d FAILURES\n", fails);
  return 1;
}
