/* USB Device descriptor header. */

#ifndef __USBD_DESC_H
#define __USBD_DESC_H

#ifdef __cplusplus
extern "C" {
#endif

#include "usbd_def.h"

#define USBD_VID                      0x0483U
#define USBD_PID_FS                   0x5741U
#define USBD_LANGID_STRING            0x409U
#define USBD_MANUFACTURER_STRING      "KOD MASTER"
#define USBD_PRODUCT_STRING_FS        "CodeMaster Bootloader"
#define USBD_SERIALNUMBER_STRING_FS   "00000000001A"
#define USBD_CONFIGURATION_STRING_FS  "CDC Bootloader Config"
#define USBD_INTERFACE_STRING_FS      "CDC Bootloader Interface"

extern USBD_DescriptorsTypeDef FS_Desc;

#ifdef __cplusplus
}
#endif

#endif /* __USBD_DESC_H */
