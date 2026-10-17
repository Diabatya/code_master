/* Идентичность устройства для загрузчика (CM-F04).
 *
 * Загрузчик показывает те же iProduct/iSerial, что и приложение —
 * «Тип устройства» и серийный номер из записи CFG0 страницы
 * идентичности (формат device_config_t приложения, см.
 * firmware/application/Inc/device_config.h и PROTOCOL.md ч.3).
 * Страница только читается — записать её может лишь DFU/ST-Link
 * (CM-F03). Платы, прошитые до переноса страницы, держат запись по
 * легаси-адресу — он проверяется вторым. */

#ifndef __DEVICE_IDENTITY_H
#define __DEVICE_IDENTITY_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define DEVICE_INFO_ADDR         0x08008000U
#define DEVICE_INFO_ADDR_LEGACY  0x0803D800U
#define DEVICE_CONFIG_MAGIC      0x43464730U /* "CFG0" */
#define DEVICE_IDENT_NAME_MAX    9U
#define DEVICE_IDENT_SERIAL_MAX  10U

typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint8_t  name_len;
  char     name[DEVICE_IDENT_NAME_MAX];
  uint8_t  serial_len;
  char     serial[DEVICE_IDENT_SERIAL_MAX];
  uint16_t vid;
  uint16_t pid;
  uint8_t  reserved[2];
  uint8_t  crc8;
} device_identity_t; /* 32 байта, упаковано как device_config_t */

/* Читает запись идентичности из Flash: актуальная страница, затем
 * легаси-адрес. Возвращает 1 и заполняет *out при магии и CRC8
 * верными, иначе 0. */
uint8_t DeviceIdentity_Read(device_identity_t *out);

#ifdef __cplusplus
}
#endif

#endif /* __DEVICE_IDENTITY_H */
