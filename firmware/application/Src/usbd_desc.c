/**
  * usbd_desc.c — application variant.
  *
  * Difference from firmware/bootloader/Src/usbd_desc.c: the product string and
  * USB serial-number string are now sourced from the Flash-stored device
  * configuration (see device_config.c / firmware/PROTOCOL.md Part 3 — Device
  * Name <=9 chars, Serial Number <=10 chars per ТЗ 11.2) instead of a fixed
  * compile-time string / the raw factory unique-ID. This lets the PC
  * configurator's flashing workflow (ТЗ 11.3) make the device re-enumerate
  * under the operator-chosen name and serial after CMD_CFG_WRITE.
  *
  * NOTE: this deliberately reuses the factory-unique-ID-derived serial as a
  * *fallback* only (when no valid config page is present yet), so a brand
  * new, never-configured board still enumerates with a distinct serial
  * instead of an empty string.
  */

#include "usbd_core.h"
#include "usbd_desc.h"
#include "usbd_conf.h"
#include "device_config.h"

/* STM32F105 unique device ID registers (fallback serial only) */
#define DEVICE_ID1          0x1FFFF7E8U
#define DEVICE_ID2          0x1FFFF7ECU
#define DEVICE_ID3          0x1FFFF7F0U
#define USB_SIZ_STRING_SERIAL 0x1AU

static uint8_t *USBD_FS_DeviceDescriptor(USBD_SpeedTypeDef speed, uint16_t * length);
static uint8_t *USBD_FS_LangIDStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length);
static uint8_t *USBD_FS_ManufacturerStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length);
static uint8_t *USBD_FS_ProductStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length);
static uint8_t *USBD_FS_SerialStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length);
static uint8_t *USBD_FS_ConfigStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length);
static uint8_t *USBD_FS_InterfaceStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length);

USBD_DescriptorsTypeDef FS_Desc = {
  USBD_FS_DeviceDescriptor,
  USBD_FS_LangIDStrDescriptor,
  USBD_FS_ManufacturerStrDescriptor,
  USBD_FS_ProductStrDescriptor,
  USBD_FS_SerialStrDescriptor,
  USBD_FS_ConfigStrDescriptor,
  USBD_FS_InterfaceStrDescriptor,
};

#if defined ( __ICCARM__ )
  #pragma data_alignment=4
#endif
__ALIGN_BEGIN uint8_t USBD_DeviceDesc[USB_LEN_DEV_DESC] __ALIGN_END = {
  0x12,
  USB_DESC_TYPE_DEVICE,
  0x00,
  0x02,
  0x02,
  0x02,
  0x00,
  USB_MAX_EP0_SIZE,
  LOBYTE(USBD_VID),
  HIBYTE(USBD_VID),
  LOBYTE(USBD_PID_FS),
  HIBYTE(USBD_PID_FS),
  0x00,
  0x02,
  USBD_IDX_MFC_STR,
  USBD_IDX_PRODUCT_STR,
  USBD_IDX_SERIAL_STR,
  USBD_MAX_NUM_CONFIGURATION
};

#if defined ( __ICCARM__ )
  #pragma data_alignment=4
#endif
__ALIGN_BEGIN uint8_t USBD_LangIDDesc[USB_LEN_LANGID_STR_DESC] __ALIGN_END = {
  USB_LEN_LANGID_STR_DESC,
  USB_DESC_TYPE_STRING,
  LOBYTE(USBD_LANGID_STRING),
  HIBYTE(USBD_LANGID_STRING),
};

#if defined ( __ICCARM__ )
  #pragma data_alignment=4
#endif
__ALIGN_BEGIN uint8_t USBD_StringSerial[USB_SIZ_STRING_SERIAL] __ALIGN_END =
{
  USB_SIZ_STRING_SERIAL,
  USB_DESC_TYPE_STRING,
};

#if defined ( __ICCARM__ )
  #pragma data_alignment=4
#endif
__ALIGN_BEGIN uint8_t USBD_StrDesc[USBD_MAX_STR_DESC_SIZ] __ALIGN_END;

static void IntToUnicode(uint32_t value, uint8_t * pbuf, uint8_t len);
static void Get_SerialNum(void);

static uint8_t *USBD_FS_DeviceDescriptor(USBD_SpeedTypeDef speed, uint16_t * length)
{
  *length = sizeof(USBD_DeviceDesc);
  return (uint8_t *) USBD_DeviceDesc;
}

static uint8_t *USBD_FS_LangIDStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length)
{
  *length = sizeof(USBD_LangIDDesc);
  return (uint8_t *) USBD_LangIDDesc;
}

static uint8_t *USBD_FS_ProductStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length)
{
  const device_config_t *cfg = DeviceConfig_Get();

  if (cfg->device_name_len > 0U) {
    /* Flash-configured name (up to 9 ASCII chars, ТЗ 11.2). Use it as-is;
     * USBD_GetString() converts to UTF-16LE for us. */
    static char name_buf[DEVICE_CONFIG_NAME_MAX + 1U];
    memcpy(name_buf, cfg->device_name, cfg->device_name_len);
    name_buf[cfg->device_name_len] = '\0';
    USBD_GetString((uint8_t *) name_buf, USBD_StrDesc, length);
  } else {
    USBD_GetString((uint8_t *) USBD_PRODUCT_STRING_FS, USBD_StrDesc, length);
  }
  return USBD_StrDesc;
}

static uint8_t *USBD_FS_ManufacturerStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length)
{
  USBD_GetString((uint8_t *) USBD_MANUFACTURER_STRING, USBD_StrDesc, length);
  return USBD_StrDesc;
}

static uint8_t *USBD_FS_SerialStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length)
{
  const device_config_t *cfg = DeviceConfig_Get();

  if (cfg->serial_len > 0U) {
    /* Flash-configured serial (up to 10 ASCII chars, ТЗ 11.2). Build a
     * USB string descriptor from it directly. */
    static char serial_buf[DEVICE_CONFIG_SERIAL_MAX + 1U];
    memcpy(serial_buf, cfg->serial, cfg->serial_len);
    serial_buf[cfg->serial_len] = '\0';
    USBD_GetString((uint8_t *) serial_buf, USBD_StrDesc, length);
    return USBD_StrDesc;
  }

  /* Fallback: derive from the 96-bit factory unique ID, as the bootloader
   * does — only used before the device has ever been configured. */
  *length = USB_SIZ_STRING_SERIAL;
  Get_SerialNum();
  return USBD_StringSerial;
}

static uint8_t *USBD_FS_ConfigStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length)
{
  USBD_GetString((uint8_t *) USBD_CONFIGURATION_STRING_FS, USBD_StrDesc, length);
  return USBD_StrDesc;
}

static uint8_t *USBD_FS_InterfaceStrDescriptor(USBD_SpeedTypeDef speed, uint16_t * length)
{
  USBD_GetString((uint8_t *) USBD_INTERFACE_STRING_FS, USBD_StrDesc, length);
  return USBD_StrDesc;
}

static void Get_SerialNum(void)
{
  uint32_t deviceserial0, deviceserial1, deviceserial2;

  deviceserial0 = *(uint32_t *) DEVICE_ID1;
  deviceserial1 = *(uint32_t *) DEVICE_ID2;
  deviceserial2 = *(uint32_t *) DEVICE_ID3;

  deviceserial0 += deviceserial2;

  if (deviceserial0 != 0)
  {
    IntToUnicode(deviceserial0, &USBD_StringSerial[2], 8);
    IntToUnicode(deviceserial1, &USBD_StringSerial[18], 4);
  }
}

static void IntToUnicode(uint32_t value, uint8_t * pbuf, uint8_t len)
{
  uint8_t idx = 0;

  for (idx = 0; idx < len; idx++)
  {
    if (((value >> 28)) < 0xA)
    {
      pbuf[2 * idx] = (value >> 28) + '0';
    }
    else
    {
      pbuf[2 * idx] = (value >> 28) + 'A' - 10;
    }

    value = value << 4;

    pbuf[2 * idx + 1] = 0;
  }
}
