/* CAN1/CAN2 bxCAN bridge: init, RX ring buffers (>=512 frames/channel per
 * ТЗ 12.2), and packing/unpacking compatible with core/can_protocol.py
 * (see firmware/PROTOCOL.md Part 1.1). RX is filled from IRQ context
 * (HAL_CAN_RxFifo0MsgPendingCallback) and drained from the main loop, so
 * reception is never blocked by USB/trigger/Flash work — only bounded by
 * how fast the ring buffer fills relative to the main loop's drain rate.
 */

#ifndef __CAN_BRIDGE_H
#define __CAN_BRIDGE_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "stm32f1xx_hal.h"

/* ТЗ 12.2: RAM ring buffers per CAN channel, >= 512 packets deep. Rounded
 * up to a power of two (1024) so head/tail wraparound is a cheap mask
 * instead of a modulo, doubling the documented minimum for extra headroom
 * against the Flash-write stall discussed in firmware/PROTOCOL.md Part 3. */
#define CAN_RING_DEPTH   1024U

typedef struct {
  uint8_t  channel;      /* 0 = CAN1, 1 = CAN2 */
  uint8_t  extended;      /* 0 = standard 11-bit ID, 1 = extended 29-bit ID */
  uint32_t id;
  uint8_t  dlc;           /* 0..8, CAN 2.0 only (no CAN FD, see PROTOCOL.md) */
  uint8_t  data[8];
} can_frame_t;

/* Initializes CAN1 (master) + CAN2 (slave) peripherals, GPIO, filters
 * (pass-all on both, filtering is done in software by triggers/UI), and
 * enables RX FIFO0 pending interrupts. baud_kbps applies to both channels
 * (bxCAN2 shares CAN1's filter bank config but has its own bit timing). */
uint8_t CanBridge_Init(uint32_t baud_kbps);

/* Sets the TJA1050 Normal/Silent select + termination-enable GPIOs for one
 * channel (0=CAN1, 1=CAN2). silent=1 puts the transceiver in listen-only
 * (Silent) mode. Applies the CAN_TRANSCEIVER_SWITCH_DELAY_US settle delay
 * documented in main.h after a Silent->Normal transition, before the first
 * TX is allowed. */
void CanBridge_SetTransceiverMode(uint8_t channel, uint8_t silent, uint8_t term_enable);

/* Pops the oldest buffered RX frame for the given channel into *out.
 * Returns 1 if a frame was available, 0 if the ring was empty. */
uint8_t CanBridge_PopRx(uint8_t channel, can_frame_t *out);

/* Transmits a frame immediately on the given channel (blocking on a free
 * mailbox for a bounded number of polls; used both for trigger responses
 * and for PC->device forwarded frames). Returns 1 on success. */
uint8_t CanBridge_Transmit(const can_frame_t *frame);

/* Returns 1 if channel's RX ring has overflowed at least once since the
 * last call (clears the flag on read) — surfaced to protocol.c so it can
 * be reported to the PC (ТЗ 12.2: drop-oldest + error flag on overflow). */
uint8_t CanBridge_TookOverflow(uint8_t channel);

#ifdef __cplusplus
}
#endif

#endif /* __CAN_BRIDGE_H */
