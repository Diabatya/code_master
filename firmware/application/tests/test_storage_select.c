/* Host harness for the PRODUCTION firmware/application/Src/storage.c —
 * the source is included textually (not an algorithm copy). Covers
 * review finding CM-N16: Storage_Init must select region copies by
 * payload integrity (CRC32), not by header CRC8 + generation only —
 * a corrupt higher-generation copy that survived outside the erased
 * span must not win over an intact lower-generation one.
 *
 * Flash emulation: a real-address anonymous mmap covering
 * 0x08000000..0x0803FFFF — storage.c dereferences literal Flash
 * addresses, so the buffer is mapped exactly there. The linker symbol
 * _app_code_end is pinned to an absolute address (0x0800B000 = app
 * base 0x08009000 + 4 pages) — all scenarios keep their regions above
 * that code end.
 *
 * Build & run on Linux/CI (from the repository root):
 *   cc -I firmware/application/tests/stubs -I firmware/application/Inc \
 *      -I firmware/application/Src \
 *      firmware/application/tests/test_storage_select.c -o /tmp/test_storage_select
 *   /tmp/test_storage_select
 *   /tmp/test_storage_select --dump flash.bin   # 256 KiB dump @0x08000000
 *      prints "SEL <kind> base=0x.. gen=.. read=.." lines for pytest
 *
 * macOS (arm64 reserves the low 4 GiB as pagezero — build x86_64 and
 * run under Rosetta 2):
 *   cc -arch x86_64 -Wl,-pagezero_size,0x4000 <same includes> \
 *      firmware/application/tests/test_storage_select.c -o /tmp/test_storage_select
 *   arch -x86_64 /tmp/test_storage_select
 */

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stddef.h>
#include <sys/mman.h>

/* _app_code_end as an ABSOLUTE linker symbol: storage.c only takes its
 * address (end of the application code in Flash). Pinned to
 * 0x0800B000 — code occupies 4 pages after 0x08009000. */
#if defined(__APPLE__)
__asm__(".globl __app_code_end\n\t__app_code_end = 0x0800B000");
#else
__asm__(".globl _app_code_end\n\t_app_code_end = 0x0800B000");
#endif

#define FLASH_BASE 0x08000000UL
#define FLASH_SIZE 0x00040000UL          /* 256 KiB F105RCT6 */
#define APP_BASE   0x08009000UL          /* STORE_APP_CODE_START */
#define CODE_END   0x0800B000UL          /* _app_code_end (4 pages) */
#define PAGE       2048U

/* ---- Emulated NOR Flash ------------------------------------------- */
static uint8_t *s_flash;

static uint8_t *fp(uint32_t addr)
{
  if (addr < FLASH_BASE || addr >= FLASH_BASE + FLASH_SIZE) {
    fprintf(stderr, "flash access out of range: 0x%08X\n", addr);
    exit(2);
  }
  return s_flash + (addr - FLASH_BASE);
}

static void flash_erase_all(void)
{
  memset(s_flash, 0xFF, FLASH_SIZE);
}

/* Заголовки проекта — до собственных реализаций HAL (нужны типы
 * HAL_StatusTypeDef / FLASH_EraseInitTypeDef из стаб-заголовка). */
#include "main.h"
#include "storage.h"
#include "trigger.h"
#include "event_log.h"

/* ---- HAL stubs ------------------------------------------------------ */
static uint32_t s_tick;
static uint32_t s_kick_count;
static uint32_t s_program_calls;
static uint32_t s_erase_calls;

uint32_t HAL_GetTick(void) { return s_tick; }
void HAL_Delay(uint32_t d) { s_tick += d; }
void HAL_FLASH_Unlock(void) {}
void HAL_FLASH_Lock(void) {}
void App_KickWatchdog(void) { s_kick_count++; }

HAL_StatusTypeDef HAL_FLASH_Program(uint32_t type, uint32_t addr,
                                    uint64_t data)
{
  (void)type;
  s_program_calls++;
  /* NOR semantics: programming only clears bits 1→0. A write over
   * unerased data yields garbage exactly like real Flash — catches
   * write-without-erase bugs. */
  uint16_t *p = (uint16_t *)fp(addr);
  *p = (uint16_t)(*p & (uint16_t)data);
  return HAL_OK;
}

HAL_StatusTypeDef HAL_FLASHEx_Erase(FLASH_EraseInitTypeDef *e,
                                    uint32_t *page_error)
{
  s_erase_calls++;
  for (uint32_t i = 0; i < e->NbPages; i++) {
    memset(fp(e->PageAddress + i * PAGE), 0xFF, PAGE);
  }
  if (page_error) {
    *page_error = 0xFFFFFFFFUL;
  }
  return HAL_OK;
}

/* Relocation callbacks — selection tests never reach them, return
 * success so compact/prepare paths don't deadlock if exercised. */
uint8_t Trigger_Relocate(uint32_t new_base) { (void)new_base; return 1U; }
uint8_t EventLog_Relocate(uint32_t new_base) { (void)new_base; return 1U; }

/* ---- Production source under test ----------------------------------- */
#include "storage.c"

/* ---- Blob planting helpers ------------------------------------------ */
static void plant_blob(uint32_t addr, uint32_t magic, uint32_t gen,
                       const uint8_t *payload, uint32_t len, int corrupt)
{
  store_header_t h;
  uint32_t span = blob_pages(len) * STORE_FLASH_PAGE;

  memset(fp(addr), 0xFF, span);
  memcpy(fp(addr + STORE_HEADER_SIZE), payload, len);
  memset(&h, 0, sizeof(h));
  h.magic = magic;
  h.generation = gen;
  h.payload_len = len;
  h.payload_crc = crc32_of(payload, len);
  h.version = STORE_VERSION;
  h.region = 0U;
  h.crc8 = crc8((const uint8_t *)&h, offsetof(store_header_t, crc8));
  memcpy(fp(addr), &h, sizeof(h));
  if (corrupt) {
    /* Обрыв посреди записи: заголовок/CRC8 валидны, payload не сходится
     * с CRC32 — в точности карта из ревью CM-N16. */
    s_flash[addr - FLASH_BASE + STORE_HEADER_SIZE + 1] ^= 0x5A;
  }
}

/* Сброс статиков модуля между «загрузками» МК (тот же TU — доступ к
 * internal state легален и нужен для сценария повторного запуска). */
static void reset_storage(void)
{
  memset(&s_var, 0, sizeof(s_var));
  memset(&s_flex, 0, sizeof(s_flex));
  memset(&s_cfg, 0, sizeof(s_cfg));
  memset(&s_trig, 0, sizeof(s_trig));
  s_evlog = 0U;
  s_legacy_trig_end = 0U;
  s_legacy_evlog_end = 0U;
  s_write_active = 0U;
  s_write_region = 0U;
  s_write_base = 0U;
  s_write_len = 0U;
  s_write_tick = 0U;
  s_code_end = 0U;
}

static int s_fail;

#define CHECK(cond, msg)                                              \
  do {                                                                \
    if (!(cond)) {                                                    \
      printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, msg);            \
      s_fail++;                                                       \
    }                                                                 \
  } while (0)

/* ------------------------------------------------------------------ */
static void test_cfg_corrupt_higher_gen_loses(void)
{
  /* Карта ревью: целая CFGH gen=2 на каноничной странице, битая
   * gen=3 выше — вне зоны записи обновления. МК обязан выбрать
   * читаемую копию, а не старшую по поколению. */
  uint8_t pl[80];
  memset(pl, 0x42, sizeof(pl));

  flash_erase_all();
  reset_storage();
  plant_blob(CODE_END, STORE_MAGIC_CFG, 2U, pl, sizeof(pl), 0);
  plant_blob(CODE_END + 4 * PAGE, STORE_MAGIC_CFG, 3U, pl, sizeof(pl), 1);
  Storage_Init();
  CHECK(Storage_CfgFoundBase() == CODE_END,
        "intact gen2 CFGH wins over corrupt gen3");
  uint8_t out[80];
  CHECK(Storage_CfgRead(out, sizeof(out)) == 1U,
        "CfgRead reads the selected intact copy");
  CHECK(memcmp(out, pl, sizeof(pl)) == 0, "CfgRead payload intact");
}

static void test_cfg_corrupt_at_lower_addr_loses(void)
{
  /* Битая старшая копия НИЖЕ целой младшей — порядок адресов не
   * должен спасать повреждённый блок. */
  uint8_t pl[64];
  memset(pl, 0x77, sizeof(pl));

  flash_erase_all();
  reset_storage();
  plant_blob(CODE_END, STORE_MAGIC_CFG, 3U, pl, sizeof(pl), 1);
  plant_blob(CODE_END + 4 * PAGE, STORE_MAGIC_CFG, 2U, pl, sizeof(pl), 0);
  Storage_Init();
  CHECK(Storage_CfgFoundBase() == CODE_END + 4 * PAGE,
        "intact gen2 CFGH at higher addr wins over corrupt gen3 below");
  uint8_t out[64];
  CHECK(Storage_CfgRead(out, sizeof(out)) == 1U, "CfgRead ok");
}

static void test_cfg_valid_higher_gen_wins(void)
{
  /* Валидная старшая копия по-прежнему выигрывает у валидной младшей
   * — контракт поколений сохранён для целых данных. */
  uint8_t pl[48];
  memset(pl, 0x33, sizeof(pl));

  flash_erase_all();
  reset_storage();
  plant_blob(CODE_END, STORE_MAGIC_CFG, 2U, pl, sizeof(pl), 0);
  plant_blob(CODE_END + 4 * PAGE, STORE_MAGIC_CFG, 3U, pl, sizeof(pl), 0);
  Storage_Init();
  CHECK(Storage_CfgFoundBase() == CODE_END + 4 * PAGE,
        "valid gen3 wins over valid gen2");
  uint32_t base = 0, len = 0, gen = 0;
  (void)base; (void)len; (void)gen;
}

static void test_cfg_single_corrupt_is_absent(void)
{
  /* Единственная копия бита — область честно отсутствует:
   * CfgFoundBase=0/CfgRead=0. Никакого «успешного восстановления»
   * по умолчанию не заявляется. */
  uint8_t pl[48];
  memset(pl, 0x55, sizeof(pl));

  flash_erase_all();
  reset_storage();
  plant_blob(CODE_END, STORE_MAGIC_CFG, 5U, pl, sizeof(pl), 1);
  Storage_Init();
  CHECK(Storage_CfgFoundBase() == 0U, "single corrupt CFGH not selected");
  uint8_t out[48];
  CHECK(Storage_CfgRead(out, sizeof(out)) == 0U, "CfgRead refuses");
}

static void test_var_corrupt_higher_gen_loses(void)
{
  /* VARH: цепочка у верха Flash — битая gen=3 ниже целой gen=2. */
  uint8_t pl[96];
  memset(pl, 0xAB, sizeof(pl));
  uint32_t top = STORE_FLASH_END - PAGE;      /* целая gen2 сверху */
  uint32_t low = top - 2 * PAGE;              /* битая gen3 ниже   */

  flash_erase_all();
  reset_storage();
  plant_blob(top, STORE_MAGIC_VAR, 2U, pl, sizeof(pl), 0);
  plant_blob(low, STORE_MAGIC_VAR, 3U, pl, sizeof(pl), 1);
  Storage_Init();
  CHECK(Storage_VarBase() == top,
        "VARH: intact gen2 wins over corrupt gen3");
  uint8_t out[96];
  CHECK(Storage_Read(STORE_REGION_VAR, 0, out, sizeof(out)) == 1U,
        "Storage_Read(VAR) ok");
  CHECK(memcmp(out, pl, sizeof(pl)) == 0, "VAR payload intact");
}

static void test_flex_corrupt_higher_gen_loses(void)
{
  /* FLXH: битая gen=4 выше целой gen=2 — исполняемая ГЛ обязана быть
   * выбранной копией, а не просто «присутствовать в дампе». */
  uint8_t pl[120];
  memset(pl, 0x66, sizeof(pl));
  uint32_t lo = CODE_END + PAGE;              /* за страницей CFGH */
  uint32_t hi = lo + 3 * PAGE;

  flash_erase_all();
  reset_storage();
  plant_blob(lo, STORE_MAGIC_FLEX, 2U, pl, sizeof(pl), 0);
  plant_blob(hi, STORE_MAGIC_FLEX, 4U, pl, sizeof(pl), 1);
  Storage_Init();
  uint32_t base = 0, len = 0, gen = 0;
  CHECK(Storage_RegionInfo(STORE_REGION_FLEX, &base, &len, &gen) == 1U,
        "FLXH region found");
  CHECK(base == lo && gen == 2U, "FLXH: intact gen2 wins");
  const uint8_t *got = Storage_FlexPayload(&len);
  CHECK(got != NULL && len == sizeof(pl)
        && memcmp(got, pl, sizeof(pl)) == 0, "FlexPayload intact");
}

static void test_equal_generations_last_wins(void)
{
  /* Две целые копии одного поколения — как прежде выигрывает
   * встреченная последней (выше по адресу): семантика скана не
   * изменилась, добавлен только фильтр целостности. */
  uint8_t pl[40];
  memset(pl, 0x99, sizeof(pl));

  flash_erase_all();
  reset_storage();
  plant_blob(CODE_END, STORE_MAGIC_CFG, 7U, pl, sizeof(pl), 0);
  plant_blob(CODE_END + 4 * PAGE, STORE_MAGIC_CFG, 7U, pl, sizeof(pl), 0);
  Storage_Init();
  CHECK(Storage_CfgFoundBase() == CODE_END + 4 * PAGE,
        "equal gen: higher address wins (as before)");
}

static void test_cold_boot_repeat_same_choice(void)
{
  /* Повторный «холодный» запуск на том же дампе — тот же выбранный
   * блок (выбор детерминирован, не зависит от порядка вызова Init). */
  uint8_t pl[64];
  memset(pl, 0x5C, sizeof(pl));

  flash_erase_all();
  reset_storage();
  plant_blob(CODE_END, STORE_MAGIC_CFG, 2U, pl, sizeof(pl), 0);
  plant_blob(CODE_END + 4 * PAGE, STORE_MAGIC_CFG, 3U, pl, sizeof(pl), 1);
  Storage_Init();
  uint32_t first = Storage_CfgFoundBase();
  reset_storage();           /* имитация повторного холодного старта */
  Storage_Init();
  CHECK(Storage_CfgFoundBase() == first && first == CODE_END,
        "repeat cold boot picks the same intact copy");
}

static void test_large_blob_crc_feeds_watchdog(void)
{
  /* Большой блоб (~32 КБ): CRC-проход обязан кормить IWDG — без
   * покормов Storage_Init на большом хранилище сбрасывал бы МК. */
  static uint8_t pl[32768];
  memset(pl, 0x11, sizeof(pl));
  uint32_t top = STORE_FLASH_END - blob_pages(sizeof(pl)) * PAGE;

  flash_erase_all();
  reset_storage();
  s_kick_count = 0;
  plant_blob(top, STORE_MAGIC_VAR, 1U, pl, sizeof(pl), 0);
  Storage_Init();
  CHECK(Storage_VarBase() == top, "large VARH selected");
  CHECK(s_kick_count > 0, "crc32_of feeds IWDG on long payloads");
}

/* ------------------------------------------------------------------ */
/* --dump <file>: загрузить 256-КиБ образ Flash (@0x08000000), проиници-
 * ализировать настоящий Storage_Init и напечатать выбранные области —
 * этот режим подключает штатный Bootloader.flash_firmware (pytest) к
 * реальному выбору МК: «сохранённое ПК» == «выбранное прошивкой». */
static int dump_mode(const char *path)
{
  FILE *f = fopen(path, "rb");
  if (!f) {
    fprintf(stderr, "cannot open %s\n", path);
    return 2;
  }
  flash_erase_all();
  if (fread(s_flash, 1, FLASH_SIZE, f) != FLASH_SIZE) {
    fprintf(stderr, "dump shorter than %lu bytes\n",
            (unsigned long)FLASH_SIZE);
    fclose(f);
    return 2;
  }
  fclose(f);
  reset_storage();
  Storage_Init();

  uint32_t base = 0, len = 0, gen = 0;
  uint8_t tmp[2048];
  printf("SEL cfg base=0x%08X gen=%u read=%u\n", Storage_CfgFoundBase(),
         s_cfg.generation,
         Storage_CfgFoundBase() ? Storage_CfgRead(tmp, sizeof(tmp)) : 0U);
  if (Storage_RegionInfo(STORE_REGION_VAR, &base, &len, &gen)) {
    printf("SEL var base=0x%08X gen=%u len=%u\n", base, gen, len);
  } else {
    printf("SEL var base=none\n");
  }
  if (Storage_RegionInfo(STORE_REGION_FLEX, &base, &len, &gen)) {
    printf("SEL flex base=0x%08X gen=%u len=%u\n", base, gen, len);
  } else {
    printf("SEL flex base=none\n");
  }
  printf("SEL trig base=0x%08X\n", Storage_TrigBase());
  printf("SEL evlog base=0x%08X\n", Storage_EvlogFound()
                                     ? Storage_EvlogBase() : 0U);
  return 0;
}

int main(int argc, char **argv)
{
  s_flash = mmap((void *)FLASH_BASE, FLASH_SIZE,
                 PROT_READ | PROT_WRITE,
                 MAP_FIXED | MAP_PRIVATE | MAP_ANON, -1, 0);
  if (s_flash == MAP_FAILED || s_flash != (uint8_t *)FLASH_BASE) {
    fprintf(stderr,
            "mmap at 0x%08lX failed — on macOS arm64 build x86_64 "
            "(-arch x86_64 -Wl,-pagezero_size,0x4000) and run via "
            "Rosetta\n", (unsigned long)FLASH_BASE);
    return 2;
  }

  if (argc == 3 && strcmp(argv[1], "--dump") == 0) {
    return dump_mode(argv[2]);
  }

  test_cfg_corrupt_higher_gen_loses();
  test_cfg_corrupt_at_lower_addr_loses();
  test_cfg_valid_higher_gen_wins();
  test_cfg_single_corrupt_is_absent();
  test_var_corrupt_higher_gen_loses();
  test_flex_corrupt_higher_gen_loses();
  test_equal_generations_last_wins();
  test_cold_boot_repeat_same_choice();
  test_large_blob_crc_feeds_watchdog();

  if (s_fail) {
    printf("FAIL %d scenario(s)\n", s_fail);
    return 1;
  }
  printf("PASS (all scenarios)\n");
  return 0;
}
