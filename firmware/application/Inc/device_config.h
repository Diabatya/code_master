/* Device configuration storage ("страница А" per ТЗ 11.2 — Device Name,
 * Serial Number, VID/PID) persisted in the dedicated configuration page
 * before the trigger pages. See firmware/PROTOCOL.md Part 3 for the exact
 * byte layout and the memory map (0x0803D800..0x0803DFFF).
 *
 * This is a *different* "page A" concept than the bootloader/application
 * Flash split (0x08000000/0x08008000) — do not confuse the two; see the
 * note carried over from firmware/FIRMWARE_INPUT_REQUEST.md.
 */

#ifndef __DEVICE_CONFIG_H
#define __DEVICE_CONFIG_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define DEVICE_CONFIG_NAME_MAX     9U   /* ТЗ 11.2: Device Name <= 9 chars */
#define DEVICE_CONFIG_SERIAL_MAX   10U  /* ТЗ 11.2: Serial Number <= 10 chars */

#define DEVICE_CONFIG_PAGE_ADDR    0x0803D800U
#define DEVICE_CONFIG_PAGE_SIZE    2048U
#define DEVICE_CONFIG_MAGIC        0x43464730U /* "CFG0" */
#define DEVICE_CONFIG_FORMAT_VERSION 1U
#define DEVICE_CONFIG_RECORD_SIZE  32U

#define DEVICE_CONFIG_DEFAULT_VID  0x0483U
#define DEVICE_CONFIG_DEFAULT_PID  0x5740U

/* In-RAM mirror of the persisted config, kept packed 1:1 with the on-Flash
 * layout documented in firmware/PROTOCOL.md Part 3 (32 bytes total). */
typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint8_t  device_name_len;
  char     device_name[DEVICE_CONFIG_NAME_MAX];
  uint8_t  serial_len;
  char     serial[DEVICE_CONFIG_SERIAL_MAX];
  uint16_t vid;
  uint16_t pid;
  uint8_t  reserved[2];
  uint8_t  crc8;
} device_config_t;

/* Extended settings record, stored at DEVICE_CONFIG_PAGE_ADDR + 32 inside
 * the same 2 KB page (the main 32-byte record stays byte-compatible with
 * older host firmware). Holds operator CAN bit rates: they must persist on
 * the device because triggers/gateways run autonomously without the PC
 * (ТЗ 12.4) and must come up at the configured speed after power cycles. */
#define DEVICE_EXT_CONFIG_OFFSET  32U
#define DEVICE_EXT_CONFIG_MAGIC   0x43455830U /* "CEX0" */
#define DEVICE_EXT_CONFIG_VERSION 1U
#define DEVICE_CONFIG_DEFAULT_BAUD_KBPS 500U

typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint16_t can1_baud_kbps;
  uint16_t can2_baud_kbps;
  uint8_t  version;
  uint8_t  reserved[6];
  uint8_t  crc8;
} device_ext_config_t; /* 16 bytes */

/* Loads the config from Flash into the RAM mirror (call once at boot, before
 * MX_USB_DEVICE_Init() so the USB descriptors already see the right
 * name/serial on first enumeration). Falls back to defaults if the page is
 * blank/corrupt (magic or CRC mismatch). */
void DeviceConfig_Init(void);

/* Returns a pointer to the current in-RAM config (read-only for callers). */
const device_config_t *DeviceConfig_Get(void);

/* Returns 1 when the persisted page passed magic/CRC/format validation. */
uint8_t DeviceConfig_IsValid(void);

/* Validates and writes a new config to Flash (erase + program the whole
 * 2 KB page — see the timing caveat in firmware/PROTOCOL.md Part 3).
 * Returns 1 on success, 0 on invalid parameters or Flash-program failure.
 * Updates the in-RAM mirror on success. */
uint8_t DeviceConfig_Write(const uint8_t *device_name, uint8_t device_name_len,
                            const uint8_t *serial, uint8_t serial_len,
                            uint16_t vid, uint16_t pid);

/* Erases the config page and reloads defaults (CMD_CFG_FACTORY_RESET,
 * ТЗ 10.3 "Заводские настройки"). Returns 1 on success. */
uint8_t DeviceConfig_FactoryReset(void);

/* CAN bit rate (kbit/s) configured for channel 0/1 — from the extended
 * record; DEVICE_CONFIG_DEFAULT_BAUD_KBPS when absent/corrupt. */
uint32_t DeviceConfig_GetCanBaud(uint8_t channel);

/* Persists a new CAN bit rate for channel 0/1: erases+reprograms the whole
 * config page (main + extended records), keeping name/serial/VID/PID
 * untouched. Returns 1 on success. */
uint8_t DeviceConfig_SetCanBaud(uint8_t channel, uint32_t baud_kbps);

#ifdef __cplusplus
}
#endif

#endif /* __DEVICE_CONFIG_H */
