/* CDC-stream protocol handler: parses the interleaved byte stream from
 * CDC_GetRxAvailable()/CDC_ReadRxByte() (see usbd_cdc_if.c) into CAN frames
 * to forward and commands to answer, per firmware/PROTOCOL.md. Also
 * forwards buffered CAN RX frames out to the PC. Must be driven from the
 * main loop (Protocol_Poll()), never from IRQ context.
 */

#ifndef __PROTOCOL_H
#define __PROTOCOL_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

/* Call once at boot, after CanBridge_Init()/Trigger_Init()/DeviceConfig_Init(). */
void Protocol_Init(uint8_t device_type, uint8_t device_version);

/* Drains whatever is available in the CDC RX FIFO (commands + PC->device
 * CAN frames per firmware/PROTOCOL.md Part 1.1), and pushes any buffered
 * CAN RX frames (from can_bridge ring buffers) out over CDC. Call once per
 * main-loop iteration, after Trigger_Poll(). */
void Protocol_Poll(void);

#ifdef __cplusplus
}
#endif

#endif /* __PROTOCOL_H */
