/* Trigger engine: up to 10 independently configurable triggers (ТЗ 10.2 —
 * 10 tabs / unified selector 1-10), each with a receive condition
 * (ID/mask/data/data-mask on a given channel) and a response (CAN frame +
 * delay). Storage is Flash-backed (see firmware/PROTOCOL.md Part 3, trigger
 * page 0x0803E000). Field set is a superset inferred from the existing
 * PC-side ui/can_trigger_tab.py logic, since ТЗ section 8 (exact trigger
 * fields) was not provided — see firmware/PROTOCOL.md 2.4 note.
 *
 * Latency: Trigger_OnFrame() must be called from the main loop immediately
 * after popping a frame from the CAN ring buffer (see main.c), never from
 * IRQ context, so it can never delay CAN reception (ТЗ 12.3). Delayed
 * responses (delay_ms > 0) are deferred using HAL_GetTick() (see trigger.c)
 * so a requested delay is honored without busy-waiting, keeping the main
 * loop free to keep draining CAN ring buffers while a response is pending.
 * NOTE: HAL_GetTick() only has 1ms (SysTick) resolution, which is borderline
 * against the ТЗ 12.3 "±1ms" latency target — see the caveat in README.md;
 * a hardware timer (TIM) would be needed for a tighter guarantee.
 */

#ifndef __TRIGGER_H
#define __TRIGGER_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include "can_bridge.h"

#define TRIGGER_COUNT        10U
#define TRIGGER_PAGE_ADDR    0x0803E000U
#define TRIGGER_PAGE_SIZE    2048U /* one page holds all 10 slots, see layout below */
#define TRIGGER_MAGIC         0x54524731U /* "TRG1" */
#define TRIGGER_FORMAT_VERSION 1U
#define TRIGGER_RECORD_SIZE    54U

typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint8_t  enabled;
  uint8_t  rx_channel;      /* 0=CAN1, 1=CAN2 */
  uint8_t  rx_extended;
  uint32_t rx_id;
  uint32_t rx_id_mask;      /* bits set = must match; bits clear = don't-care */
  uint8_t  rx_dlc;
  uint8_t  rx_data[8];
  uint8_t  rx_data_mask[8]; /* per-byte don't-care mask, 0x00 = ignore byte */
  uint8_t  tx_channel;
  uint8_t  tx_extended;
  uint32_t tx_id;
  uint8_t  tx_dlc;
  uint8_t  tx_data[8];
  uint16_t delay_ms;        /* response delay, 0..~65s */
  uint8_t  reserved[2];     /* [0]=FORMAT_VERSION, [1]=RECORD_SIZE */
  uint8_t  tx_rtr;          /* 0=data response, 1=Remote Transmission Request */
  uint8_t  reserved_pad;    /* keeps sizeof(trigger_t) even */
  uint8_t  crc8;
} trigger_t; /* 54 bytes, fits 10x in the 2KB page with room to spare */

/* Loads all 10 triggers from Flash into RAM (call once at boot). Any slot
 * with a bad magic/CRC is treated as "disabled, all zero". */
void Trigger_Init(void);

/* Must be called once per main-loop iteration to service pending delayed
 * responses (uses HAL_GetTick(), millisecond resolution — see the ±1ms
 * latency caveat in README.md regarding SysTick-based timing vs. a
 * dedicated hardware timer). */
void Trigger_Poll(void);

/* Returns cumulative trigger timing counters since boot. */
void Trigger_GetStats(uint32_t *fired_count, uint32_t *max_lateness_ms);

/* Evaluates all enabled triggers against a freshly received frame and
 * arms any matching response (respecting its configured delay_ms). Must
 * be called from the main loop only, never from IRQ context. */
void Trigger_OnFrame(const can_frame_t *frame);

/* Read one trigger slot (index 0..9) into *out. Returns 1 if index valid. */
uint8_t Trigger_Get(uint8_t index, trigger_t *out);

/* Validates and writes one trigger slot to Flash + RAM. Returns 1 on
 * success. NOTE: like device_config, this erases and reprograms the whole
 * 2KB trigger page (all 10 slots), since STM32F1 Flash cannot erase less
 * than a full page — callers should batch multiple trigger edits together
 * where possible to minimize erase/reprogram cycles. */
uint8_t Trigger_Set(uint8_t index, const trigger_t *trig);

/* Stages one trigger in RAM and commits all staged records in one Flash erase. */
uint8_t Trigger_Stage(uint8_t index, const trigger_t *trig);
uint8_t Trigger_Commit(void);

/* Enables/disables a trigger without touching its other fields (still
 * requires a full page rewrite per the note above). */
uint8_t Trigger_SetEnabled(uint8_t index, uint8_t enabled);

#ifdef __cplusplus
}
#endif

#endif /* __TRIGGER_H */
