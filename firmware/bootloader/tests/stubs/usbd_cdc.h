/* Minimal stub of usbd_cdc.h for the bootloader host harness:
 * usbd_cdc_if.h only needs the USBD_CDC_ItfTypeDef typedef for the
 * extern fops declaration — the object itself is never referenced by
 * bootloader.c, so no definition is required. */
#ifndef __USBD_CDC_H_STUB
#define __USBD_CDC_H_STUB

#include <stdint.h>

typedef struct {
  uint8_t dummy;
} USBD_CDC_ItfTypeDef;

#endif /* __USBD_CDC_H_STUB */
