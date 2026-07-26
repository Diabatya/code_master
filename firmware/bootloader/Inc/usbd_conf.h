/* USB Device configuration header. */

#ifndef __USBD_CONF_H
#define __USBD_CONF_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "stm32f1xx_hal.h"

#define USBD_MAX_NUM_INTERFACES     1U
#define USBD_MAX_NUM_CONFIGURATION  1U
#define USBD_MAX_STR_DESC_SIZ       64U
#define USBD_SUPPORT_USER_STRING    0U
#define USBD_SELF_POWERED           1U
#define USBD_DEBUG_LEVEL            0U

/* For CDC class */
#define CDC_IN_EP                   0x81U
#define CDC_OUT_EP                  0x01U
#define CDC_CMD_EP                  0x82U
#define CDC_DATA_HS_MAX_PACKET_SIZE 512U
#define CDC_DATA_FS_MAX_PACKET_SIZE 64U
#define CDC_CMD_PACKET_SIZE         8U
#define CDC_DATA_MAX_PACKET_SIZE    64U

void *usbd_pool_malloc(size_t size);
void usbd_pool_free(void *p);

#define USBD_malloc                 usbd_pool_malloc
#define USBD_free                   usbd_pool_free
#define USBD_memset                 memset
#define USBD_memcpy                 memcpy
#define USBD_Delay                  HAL_Delay

#if (USBD_DEBUG_LEVEL > 0U)
  #define USBD_UsrLog(...)   do { printf(__VA_ARGS__); printf("\n"); } while(0)
#else
  #define USBD_UsrLog(...)
#endif

#if (USBD_DEBUG_LEVEL > 1U)
  #define USBD_ErrLog(...)   do { printf("ERROR: "); printf(__VA_ARGS__); printf("\n"); } while(0)
#else
  #define USBD_ErrLog(...)
#endif

#if (USBD_DEBUG_LEVEL > 2U)
  #define USBD_DbgLog(...)   do { printf("DEBUG: "); printf(__VA_ARGS__); printf("\n"); } while(0)
#else
  #define USBD_DbgLog(...)
#endif

#ifdef __cplusplus
}
#endif

#endif /* __USBD_CONF_H */
