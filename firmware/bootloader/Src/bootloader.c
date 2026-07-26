/* AN3155-compatible bootloader over USB CDC.
 *
 * Implements the same protocol used by core/bootloader.py:
 *   0x7F  sync
 *   0x00  Get
 *   0x01  Get Version
 *   0x02  Get ID
 *   0x11  Read Memory
 *   0x21  Go
 *   0x31  Write Memory
 *   0x43  Erase
 *   0x44  Extended Erase
 */

#include "main.h"
#include "bootloader.h"
#include "usbd_cdc_if.h"
#include <string.h>

#define ACK_BYTE   0x79U
#define NACK_BYTE  0x1FU

#define BOOTLOADER_VERSION 0x31U
#define STM32F105_PID      0x0418U

#define FLASH_START        0x08000000U
#define APP_START          0x08008000U
#define APP_PAGES_START    16U
#define APP_PAGES_TOTAL    112U

static const uint8_t ack = ACK_BYTE;
static const uint8_t nack = NACK_BYTE;

static void bl_send_ack(void)
{
  CDC_Transmit_FS((uint8_t *)&ack, 1);
}

static void bl_send_nack(void)
{
  CDC_Transmit_FS((uint8_t *)&nack, 1);
}

static bool bl_read_byte(uint8_t *out, uint32_t timeout_ms)
{
  uint32_t start = HAL_GetTick();
  while (CDC_GetRxAvailable() == 0) {
    if ((HAL_GetTick() - start) > timeout_ms) {
      return false;
    }
  }
  *out = CDC_ReadRxByte();
  return true;
}

static bool bl_read_bytes(uint8_t *buf, uint16_t len, uint32_t timeout_ms)
{
  for (uint16_t i = 0; i < len; i++) {
    if (!bl_read_byte(&buf[i], timeout_ms)) {
      return false;
    }
  }
  return true;
}

static bool bl_address_in_app(uint32_t address)
{
  if (address < APP_START) {
    return false;
  }
  if (address >= (FLASH_START + 256U * 1024U)) {
    return false;
  }
  return true;
}

static bool bl_app_is_valid(void)
{
  uint32_t sp = *(__IO uint32_t *)APP_START;
  uint32_t rv = *(__IO uint32_t *)(APP_START + 4U);

  /* Stack pointer should be in RAM (0x20000000 - 0x20010000 for 64 KB) */
  if ((sp < 0x20000000U) || (sp > 0x20010000U)) {
    return false;
  }
  /* Reset vector must be odd (Thumb) and inside flash */
  if ((rv & 1U) == 0U) {
    return false;
  }
  rv &= ~1U;
  if ((rv < APP_START) || (rv >= (FLASH_START + 256U * 1024U))) {
    return false;
  }
  return true;
}

void Bootloader_RequestStay(void)
{
  *(__IO uint32_t *)BOOTLOADER_FLAG_ADDRESS = BOOTLOADER_FLAG_VALUE;
}

bool Bootloader_ShouldStay(void)
{
  bool software_reset = (RCC->CSR & RCC_CSR_SFTRSTF) != 0U;
  bool stay = false;

  if (software_reset &&
      (*(__IO uint32_t *)BOOTLOADER_FLAG_ADDRESS == BOOTLOADER_FLAG_VALUE)) {
    stay = true;
  }

  /* Clear flag and reset flags */
  *(__IO uint32_t *)BOOTLOADER_FLAG_ADDRESS = 0U;
  RCC->CSR |= RCC_CSR_RMVF;

  if (!stay && !bl_app_is_valid()) {
    stay = true;
  }
  return stay;
}

void Bootloader_JumpToApplication(uint32_t address)
{
  if (!bl_app_is_valid()) {
    return;
  }

  uint32_t sp = *(__IO uint32_t *)address;
  uint32_t rv = *(__IO uint32_t *)(address + 4U);

  __disable_irq();

  /* Reset peripheral clocks used by bootloader */
  __HAL_RCC_USB_OTG_FS_CLK_DISABLE();
  __HAL_RCC_GPIOA_CLK_DISABLE();
  __HAL_RCC_GPIOC_CLK_DISABLE();
  __HAL_RCC_AFIO_CLK_DISABLE();

  SCB->VTOR = address;
  __set_MSP(sp);

  void (*app_reset)(void) = (void (*)(void))rv;
  app_reset();

  while (1) { }
}

static bool bl_flash_program(uint32_t address, const uint8_t *data, uint16_t len)
{
  HAL_FLASH_Unlock();

  for (uint16_t i = 1; i <= len; i += 2) {
    uint16_t half = data[i - 1];
    if (i < len) {
      half |= (uint16_t)(data[i] << 8);
    } else {
      half |= 0xFF00U;
    }

    if (HAL_FLASH_Program(FLASH_TYPEPROGRAM_HALFWORD, address + i - 1, half) != HAL_OK) {
      HAL_FLASH_Lock();
      return false;
    }
  }

  HAL_FLASH_Lock();
  return true;
}

static bool bl_flash_erase_app(void)
{
  FLASH_EraseInitTypeDef erase = {0};
  uint32_t error = 0;

  erase.TypeErase = FLASH_TYPEERASE_PAGES;
  erase.PageAddress = APP_START;
  erase.NbPages = APP_PAGES_TOTAL;

  HAL_FLASH_Unlock();
  bool ok = (HAL_FLASHEx_Erase(&erase, &error) == HAL_OK);
  HAL_FLASH_Lock();
  return ok;
}

static bool bl_address_check(uint32_t address, uint16_t len)
{
  if (!bl_address_in_app(address)) {
    return false;
  }
  if ((address + len) > (FLASH_START + 256U * 1024U)) {
    return false;
  }
  return true;
}

/* -------------------------------------------------------------------------- */
/* Command handlers                                                           */
/* -------------------------------------------------------------------------- */

static void bl_cmd_get(void)
{
  uint8_t resp[12];
  uint8_t cmds[] = {0x00U, 0x01U, 0x02U, 0x11U, 0x21U, 0x31U, 0x43U, 0x44U};
  uint8_t cs = 0;

  resp[0] = BOOTLOADER_VERSION;
  cs ^= resp[0];
  resp[1] = (uint8_t)(sizeof(cmds) - 1U);
  cs ^= resp[1];
  for (uint8_t i = 0; i < sizeof(cmds); i++) {
    resp[2 + i] = cmds[i];
    cs ^= cmds[i];
  }
  resp[2 + sizeof(cmds)] = cs;

  CDC_Transmit_FS(resp, sizeof(resp));
}

static void bl_cmd_get_version(void)
{
  /* Python reads the first byte as version, then skips everything until ACK.
   * Send version, a few dummy bytes, and ACK. */
  uint8_t resp[] = {
    BOOTLOADER_VERSION,
    0x00U, 0x01U, 0x02U, 0x11U, 0x21U, 0x31U, 0x43U, 0x44U,
    ACK_BYTE
  };
  CDC_Transmit_FS(resp, sizeof(resp));
}

static void bl_cmd_get_id(void)
{
  uint8_t resp[3];
  resp[0] = 0x01U;                /* N = 1 (2 bytes follow) */
  resp[1] = (uint8_t)(STM32F105_PID >> 8);
  resp[2] = (uint8_t)(STM32F105_PID);
  CDC_Transmit_FS(resp, sizeof(resp));
}

static void bl_cmd_read_memory(void)
{
  uint8_t addr[4];
  uint8_t ck;
  uint8_t n;

  if (!bl_read_bytes(addr, 4, 200)) { bl_send_nack(); return; }
  if (!bl_read_byte(&ck, 200)) { bl_send_nack(); return; }

  uint8_t sum = addr[0] ^ addr[1] ^ addr[2] ^ addr[3];
  if (ck != sum) { bl_send_nack(); return; }

  bl_send_ack();

  if (!bl_read_byte(&n, 200)) { bl_send_nack(); return; }

  uint16_t length = (uint16_t)n + 1U;
  bl_send_ack();

  uint32_t address = ((uint32_t)addr[0] << 24) | ((uint32_t)addr[1] << 16) |
                     ((uint32_t)addr[2] << 8) | (uint32_t)addr[3];

  if (!bl_address_check(address, length)) {
    bl_send_nack();
    return;
  }

  /* Send data in chunks to fit the TX buffer. */
  uint16_t sent = 0;
  while (sent < length) {
    uint16_t chunk = length - sent;
    if (chunk > 64U) { chunk = 64U; }
    CDC_Transmit_FS((uint8_t *)(address + sent), chunk);
    sent += chunk;
  }
}

static void bl_cmd_write_memory(void)
{
  uint8_t addr[4];
  uint8_t ck;
  uint8_t n;

  if (!bl_read_bytes(addr, 4, 200)) { bl_send_nack(); return; }
  if (!bl_read_byte(&ck, 200)) { bl_send_nack(); return; }

  uint8_t sum = addr[0] ^ addr[1] ^ addr[2] ^ addr[3];
  if (ck != sum) { bl_send_nack(); return; }

  uint32_t address = ((uint32_t)addr[0] << 24) | ((uint32_t)addr[1] << 16) |
                     ((uint32_t)addr[2] << 8) | (uint32_t)addr[3];

  if (!bl_address_in_app(address)) { bl_send_nack(); return; }

  bl_send_ack();

  if (!bl_read_byte(&n, 200)) { bl_send_nack(); return; }

  uint16_t length = (uint16_t)n + 1U;
  uint8_t data[256];

  if (length > sizeof(data)) { bl_send_nack(); return; }

  if (!bl_read_bytes(data, length, 1000)) { bl_send_nack(); return; }
  if (!bl_read_byte(&ck, 200)) { bl_send_nack(); return; }

  sum = n;
  for (uint16_t i = 0; i < length; i++) {
    sum ^= data[i];
  }
  if (ck != sum) { bl_send_nack(); return; }

  if (!bl_address_check(address, length)) { bl_send_nack(); return; }

  if (!bl_flash_program(address, data, length)) {
    bl_send_nack();
    return;
  }

  bl_send_ack();
}

static void bl_cmd_go(void)
{
  uint8_t addr[4];
  uint8_t ck;

  if (!bl_read_bytes(addr, 4, 200)) { bl_send_nack(); return; }
  if (!bl_read_byte(&ck, 200)) { bl_send_nack(); return; }

  uint8_t sum = addr[0] ^ addr[1] ^ addr[2] ^ addr[3];
  if (ck != sum) { bl_send_nack(); return; }

  uint32_t address = ((uint32_t)addr[0] << 24) | ((uint32_t)addr[1] << 16) |
                     ((uint32_t)addr[2] << 8) | (uint32_t)addr[3];

  if (address != APP_START) { bl_send_nack(); return; }

  bl_send_ack();
  Bootloader_JumpToApplication(address);
}

static void bl_cmd_erase(bool extended)
{
  uint8_t buf[3];

  if (extended) {
    if (!bl_read_bytes(buf, 2, 200)) { bl_send_nack(); return; }
    if (CDC_GetRxAvailable() > 0) {
      uint8_t next = CDC_ReadRxByte();
      uint8_t cs = buf[0] ^ buf[1];
      if (next != cs) {
        (void)next;
      }
    }

    uint16_t pages = ((uint16_t)buf[0] << 8) | buf[1];
    if (pages == 0xFFFFU) {
      if (bl_flash_erase_app()) { bl_send_ack(); } else { bl_send_nack(); }
    } else {
      bl_send_nack();
    }
  } else {
    if (!bl_read_byte(&buf[0], 200)) { bl_send_nack(); return; }
    if (CDC_GetRxAvailable() > 0) {
      uint8_t next = CDC_ReadRxByte();
      if (next != (buf[0] ^ 0xFFU)) {
        (void)next;
      }
    }

    if (buf[0] == 0xFFU) {
      if (bl_flash_erase_app()) { bl_send_ack(); } else { bl_send_nack(); }
    } else {
      bl_send_nack();
    }
  }
}

/* -------------------------------------------------------------------------- */
/* Public API                                                                 */
/* -------------------------------------------------------------------------- */

void Bootloader_Init(void)
{
}

void Bootloader_ProcessRx(const uint8_t *data, uint16_t len)
{
  (void)data;
  (void)len;
}

void Bootloader_Task(void)
{
  while (CDC_GetRxAvailable() > 0) {
    uint8_t first;
    if (!bl_read_byte(&first, 0)) { break; }

    if (first == 0x7FU) {
      bl_send_ack();
      continue;
    }

    uint8_t ck;
    if (!bl_read_byte(&ck, 200)) {
      bl_send_nack();
      return;
    }

    if (ck != (first ^ 0xFFU)) {
      bl_send_nack();
      continue;
    }

    bl_send_ack();

    switch (first) {
      case 0x00U: bl_cmd_get(); break;
      case 0x01U: bl_cmd_get_version(); break;
      case 0x02U: bl_cmd_get_id(); break;
      case 0x11U: bl_cmd_read_memory(); break;
      case 0x21U: bl_cmd_go(); break;
      case 0x31U: bl_cmd_write_memory(); break;
      case 0x43U: bl_cmd_erase(false); break;
      case 0x44U: bl_cmd_erase(true); break;
      default: bl_send_nack(); break;
    }
  }
}
