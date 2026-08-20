/* USB Device descriptor header — application variant. */

#ifndef __USBD_DESC_H
#define __USBD_DESC_H

#ifdef __cplusplus
extern "C" {
#endif

#include "usbd_def.h"

#define USBD_VID                      0x0483U
/* PID=0x5740 for the application, PID=0x5741 reserved for the bootloader
 * (firmware/bootloader/Inc/usbd_desc.h) — see firmware/FIRMWARE_INPUT_REQUEST.md
 * "Рекомендуемый PID для основного приложения". */
#define USBD_PID_FS                   0x5740U
#define USBD_LANGID_STRING            0x409U
#define USBD_MANUFACTURER_STRING      "KOD MASTER"
/* Fallback product string used only until device_config has been read once
 * (e.g. very first boot before Get_SerialNum()/USBD_FS_ProductStrDescriptor
 * has pulled the Flash-configured Device Name — see usbd_desc.c). */
#define USBD_PRODUCT_STRING_FS        "CodeMaster CAN"
#define USBD_CONFIGURATION_STRING_FS  "CodeMaster CAN Config"
#define USBD_INTERFACE_STRING_FS      "CodeMaster CAN Interface"

extern USBD_DescriptorsTypeDef FS_Desc;

#ifdef __cplusplus
}
#endif

#endif /* __USBD_DESC_H */
