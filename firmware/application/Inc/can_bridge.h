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

/* Packed: кадры живут в двух кольцах по 1024 шт. + эхо-кольце — выравнивание
 * раздувало бы каждый слот до 20 байт (RAM F105 = 64 КБ, хвост под
 * кучу/стек уже впритык). Cortex-M3 допускает невыровненные доступы —
 * компилятор сам соберёт поле id побайтно. */
typedef struct __attribute__((packed)) {
  uint8_t  channel;      /* 0 = CAN1, 1 = CAN2 */
  uint8_t  extended;      /* 0 = standard 11-bit ID, 1 = extended 29-bit ID */
  uint8_t  rtr;           /* 0 = data frame, 1 = Remote Transmission Request */
  uint32_t id;
  uint8_t  dlc;           /* 0..8, CAN 2.0 only (no CAN FD, see PROTOCOL.md) */
  uint8_t  echo;          /* 0 = кадр с шины/с ПК; >0 = глубина TX-эха
                           * (собственная передача, возвращённая в PopRx
                           * для цепочек триггеров; ограничена
                           * CAN_TX_ECHO_MAX против пинг-понга) */
  uint8_t  data[8];
} can_frame_t;

typedef struct {
  uint32_t rx_count;
  uint32_t tx_count;
  uint32_t lost_count;
  uint32_t error_count;
  uint32_t busoff_count;
  uint32_t recovery_count;
  uint32_t tx_fail_count;  /* кадры, не отправленные: TX-ящики заняты */
  uint32_t fifo_poll_count; /* кадры, вычитанные backstop-опросом FIFO
                             * из главного цикла (PollHealth): >0 значит,
                             * что RX0-прерывание часть кадров пропустило
                             * — на F105 CAN1_RX0 делит вектор с USB_LP */
} can_stats_t;

/* Initializes CAN1 (master) + CAN2 (slave) peripherals, GPIO, filters
 * (pass-all on both, filtering is done in software by triggers/UI), and
 * enables RX FIFO0 pending interrupts. Each channel gets its own bit rate:
 * CAN1 = can1_baud_kbps, CAN2 = can2_baud_kbps (bxCAN2 shares CAN1's
 * filter bank config but has its own bit timing). */
uint8_t CanBridge_Init(uint32_t can1_baud_kbps, uint32_t can2_baud_kbps);

/* Reconfigures one channel's bit rate at runtime (0 = CAN1, 1 = CAN2):
 * the peripheral briefly enters init mode and comes back on the bus with
 * the new timing. Transceiver Normal/Silent and termination states are
 * GPIO-driven and survive the re-init. Returns 1 on success. */
uint8_t CanBridge_SetBaud(uint8_t channel, uint32_t baud_kbps);

/* Returns the currently applied bit rate of channel 0/1 in kbit/s. */
uint32_t CanBridge_GetBaud(uint8_t channel);

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

/* Returns 1 if CAN1/CAN2 have been successfully initialized and are ready. */
uint8_t CanBridge_IsReady(void);

/* Reads cumulative RX/TX/lost counters for one channel. */
void CanBridge_GetStats(uint8_t channel, can_stats_t *out);
void CanBridge_PollHealth(void);

/* Returns 1 if channel's RX ring has overflowed at least once since the
 * last call (clears the flag on read) — surfaced to protocol.c so it can
 * be reported to the PC (ТЗ 12.2: drop-oldest + error flag on overflow). */
uint8_t CanBridge_TookOverflow(uint8_t channel);

/* Returns 1 if channel's bxCAN peripheral raised at least one error
 * interrupt (stuff/form/ACK/bit/CRC error, error-warning, error-passive or
 * bus-off — see HAL_CAN_ERROR_* in stm32f1xx_hal_can.h) since the last
 * call, clearing the flag on read (same drop-and-report semantics as
 * CanBridge_TookOverflow()). If last_error_code is non-NULL, it receives
 * the raw HAL error-code bitmask captured at the time of the most recent
 * error, for diagnostics on the PC side (see PROTOCOL.md CMD_CAN_ERROR_STATUS). */
uint8_t CanBridge_TookError(uint8_t channel, uint32_t *last_error_code);

/* Returns 1 if channel's bxCAN peripheral entered Bus-Off state at least
 * once since the last call, clearing the flag on read. AutoBusOff (see
 * CanBridge_Init()) makes the peripheral recover automatically once the
 * bus is quiet again, but the PC still needs to know it happened (e.g. to
 * warn the user about a bad/missing termination or a disconnected bus). */
uint8_t CanBridge_TookBusOff(uint8_t channel);

#ifdef __cplusplus
}
#endif

#endif /* __CAN_BRIDGE_H */
