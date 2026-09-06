/* CAN1/CAN2 bxCAN bridge implementation. See can_bridge.h and
 * firmware/PROTOCOL.md Part 1.1 for the wire format this feeds/reads. */

#include <string.h>
#include "main.h"
#include "can_bridge.h"

CAN_HandleTypeDef hcan1;
CAN_HandleTypeDef hcan2;

typedef struct {
  can_frame_t   buf[CAN_RING_DEPTH];
  volatile uint16_t head;
  volatile uint16_t tail;
  volatile uint8_t  overflow;
  volatile uint32_t rx_count;
  volatile uint32_t lost_count;
} can_ring_t;

static can_ring_t s_ring[2];
static volatile uint32_t s_tx_count[2];

/* Sticky per-channel error/bus-off flags + last raw HAL error code, set
 * from HAL_CAN_ErrorCallback() (IRQ context) and consumed/cleared by
 * CanBridge_TookError()/CanBridge_TookBusOff() (main loop / protocol.c),
 * mirroring the existing overflow-flag pattern above. */
static volatile uint8_t  s_error_pending[2];
static volatile uint8_t  s_busoff_pending[2];
static volatile uint32_t s_last_error_code[2];

static void ring_push(uint8_t channel, const can_frame_t *frame)
{
  can_ring_t *ring = &s_ring[channel];
  uint16_t next = (uint16_t)((ring->head + 1U) & (CAN_RING_DEPTH - 1U));
  if (next == ring->tail) {
    /* Ring full: drop the oldest frame (advance tail) per ТЗ 12.2
     * "drop-oldest + error flag on overflow" instead of dropping the new
     * frame, so the PC sees the most recent bus activity rather than a
     * stale window. */
    ring->tail = (uint16_t)((ring->tail + 1U) & (CAN_RING_DEPTH - 1U));
    ring->overflow = 1U;
    ring->lost_count++;
  }
  ring->rx_count++;
  ring->buf[ring->head] = *frame;
  ring->head = next;
}

uint8_t CanBridge_PopRx(uint8_t channel, can_frame_t *out)
{
  if (channel > 1U) {
    return 0U;
  }
  can_ring_t *ring = &s_ring[channel];
  if (ring->head == ring->tail) {
    return 0U;
  }
  *out = ring->buf[ring->tail];
  ring->tail = (uint16_t)((ring->tail + 1U) & (CAN_RING_DEPTH - 1U));
  return 1U;
}

void CanBridge_GetStats(uint8_t channel, can_stats_t *out)
{
  if (out == NULL) {
    return;
  }
  if (channel > 1U) {
    memset(out, 0, sizeof(*out));
    return;
  }
  out->rx_count = s_ring[channel].rx_count;
  out->tx_count = s_tx_count[channel];
  out->lost_count = s_ring[channel].lost_count;
}

uint8_t CanBridge_TookOverflow(uint8_t channel)
{
  if (channel > 1U) {
    return 0U;
  }
  uint8_t was = s_ring[channel].overflow;
  s_ring[channel].overflow = 0U;
  return was;
}

uint8_t CanBridge_TookError(uint8_t channel, uint32_t *last_error_code)
{
  if (channel > 1U) {
    return 0U;
  }
  uint8_t was = s_error_pending[channel];
  s_error_pending[channel] = 0U;
  if (last_error_code != NULL) {
    *last_error_code = s_last_error_code[channel];
  }
  return was;
}

uint8_t CanBridge_TookBusOff(uint8_t channel)
{
  if (channel > 1U) {
    return 0U;
  }
  uint8_t was = s_busoff_pending[channel];
  s_busoff_pending[channel] = 0U;
  return was;
}

/* IRQ-context callback invoked by HAL_CAN_IRQHandler on any enabled error
 * condition (see CAN_IT_ERROR/CAN_IT_BUSOFF/CAN_IT_LAST_ERROR_CODE
 * activation in CanBridge_Init()). Kept minimal like
 * HAL_CAN_RxFifo0MsgPendingCallback() below: just latch the flags/code for
 * the main loop to pick up via CanBridge_TookError()/CanBridge_TookBusOff(),
 * never block or do protocol work here. */
void HAL_CAN_ErrorCallback(CAN_HandleTypeDef *hcan)
{
  uint8_t channel = (hcan->Instance == CAN1) ? 0U : 1U;
  uint32_t code = hcan->ErrorCode;

  s_last_error_code[channel] = code;
  s_error_pending[channel] = 1U;
  if ((code & HAL_CAN_ERROR_BOF) != 0U) {
    s_busoff_pending[channel] = 1U;
  }

  /* HAL_CAN_IRQHandler() ORs newly detected error bits into hcan->ErrorCode
   * (it never clears bits we've already consumed), so without resetting it
   * here every future unrelated error would keep re-reporting old bits
   * (e.g. a one-off bus-off would "stick" in every later error report). */
  hcan->ErrorCode = HAL_CAN_ERROR_NONE;
}

static void gpio_init_can_pins(void)
{
  GPIO_InitTypeDef gpio = {0};

  __HAL_RCC_GPIOB_CLK_ENABLE();

  /* CAN1 RX (input floating/pull-up per AF input) + TX (AF push-pull) */
  gpio.Pin = CAN1_RX_PIN;
  gpio.Mode = GPIO_MODE_INPUT;
  gpio.Pull = GPIO_PULLUP;
  HAL_GPIO_Init(CAN1_RX_PORT, &gpio);

  gpio.Pin = CAN1_TX_PIN;
  gpio.Mode = GPIO_MODE_AF_PP;
  gpio.Speed = GPIO_SPEED_FREQ_HIGH;
  HAL_GPIO_Init(CAN1_TX_PORT, &gpio);

  /* CAN2 RX/TX */
  gpio.Pin = CAN2_RX_PIN;
  gpio.Mode = GPIO_MODE_INPUT;
  gpio.Pull = GPIO_PULLUP;
  HAL_GPIO_Init(CAN2_RX_PORT, &gpio);

  gpio.Pin = CAN2_TX_PIN;
  gpio.Mode = GPIO_MODE_AF_PP;
  gpio.Speed = GPIO_SPEED_FREQ_HIGH;
  HAL_GPIO_Init(CAN2_TX_PORT, &gpio);

  /* TJA1050 Normal/Silent select + 120R termination-enable, both channels.
   * Output push-pull, default = Normal mode, termination disabled — the
   * actual desired state is set explicitly by CanBridge_SetTransceiverMode()
   * during init, this is just a safe power-on default. */
  __HAL_RCC_GPIOC_CLK_ENABLE();
  GPIO_InitTypeDef out = {0};
  out.Mode = GPIO_MODE_OUTPUT_PP;
  out.Pull = GPIO_NOPULL;
  out.Speed = GPIO_SPEED_FREQ_LOW;

  out.Pin = CAN1_TXRX_S_PIN;
  HAL_GPIO_Init(CAN1_TXRX_S_PORT, &out);
  HAL_GPIO_WritePin(CAN1_TXRX_S_PORT, CAN1_TXRX_S_PIN, GPIO_PIN_RESET);

  out.Pin = CAN2_TXRX_S_PIN;
  HAL_GPIO_Init(CAN2_TXRX_S_PORT, &out);
  HAL_GPIO_WritePin(CAN2_TXRX_S_PORT, CAN2_TXRX_S_PIN, GPIO_PIN_RESET);

  out.Pin = CAN1_TERM_PIN;
  HAL_GPIO_Init(CAN1_TERM_PORT, &out);
  HAL_GPIO_WritePin(CAN1_TERM_PORT, CAN1_TERM_PIN, GPIO_PIN_RESET);

  out.Pin = CAN2_TERM_PIN;
  HAL_GPIO_Init(CAN2_TERM_PORT, &out);
  HAL_GPIO_WritePin(CAN2_TERM_PORT, CAN2_TERM_PIN, GPIO_PIN_RESET);
}

void CanBridge_SetTransceiverMode(uint8_t channel, uint8_t silent, uint8_t term_enable)
{
  GPIO_TypeDef *s_port = (channel == 0U) ? CAN1_TXRX_S_PORT : CAN2_TXRX_S_PORT;
  uint16_t      s_pin  = (channel == 0U) ? CAN1_TXRX_S_PIN  : CAN2_TXRX_S_PIN;
  GPIO_TypeDef *t_port = (channel == 0U) ? CAN1_TERM_PORT   : CAN2_TERM_PORT;
  uint16_t      t_pin  = (channel == 0U) ? CAN1_TERM_PIN    : CAN2_TERM_PIN;

  uint8_t was_silent = (HAL_GPIO_ReadPin(s_port, s_pin) == GPIO_PIN_SET) ? 1U : 0U;

  HAL_GPIO_WritePin(s_port, s_pin, silent ? GPIO_PIN_SET : GPIO_PIN_RESET);
  HAL_GPIO_WritePin(t_port, t_pin, term_enable ? GPIO_PIN_SET : GPIO_PIN_RESET);

  if (was_silent && !silent) {
    /* Silent->Normal transition: honor the settle delay before the caller
     * is allowed to transmit (ТЗ note on TJA1050 switch timing — value is
     * a conservative placeholder pending oscilloscope calibration, see
     * main.h::CAN_TRANSCEIVER_SWITCH_DELAY_US). */
    for (volatile uint32_t i = 0; i < (CAN_TRANSCEIVER_SWITCH_DELAY_US * 72U); i++) {
      __NOP();
    }
  }
}

static uint8_t configure_bit_timing(CAN_HandleTypeDef *hcan, uint32_t baud_kbps)
{
  /* APB1 = 36 MHz (see SystemClock_Config in main.c). Prescaler/BS1/BS2
   * chosen for a fixed 16 time-quanta bit (BS1=13tq, BS2=2tq, SJW=1tq,
   * sample point ~87.5%), scaling only the prescaler with baud rate —
   * standard, well-tested combination for CAN 2.0 at 36 MHz APB clock. */
  uint32_t prescaler;
  switch (baud_kbps) {
    case 1000: prescaler = 2;  break;
    case 500:  prescaler = 4;  break;
    case 250:  prescaler = 8;  break;
    case 125:  prescaler = 16; break;
    case 100:  prescaler = 20; break;
    case 50:   prescaler = 40; break;
    case 20:   prescaler = 100; break;
    case 10:   prescaler = 200; break;
    default:   prescaler = 4;  break; /* fall back to 500 kbit/s */
  }

  hcan->Init.Prescaler = prescaler;
  hcan->Init.Mode = CAN_MODE_NORMAL;
  hcan->Init.SyncJumpWidth = CAN_SJW_1TQ;
  hcan->Init.TimeSeg1 = CAN_BS1_13TQ;
  hcan->Init.TimeSeg2 = CAN_BS2_2TQ;
  hcan->Init.TimeTriggeredMode = DISABLE;
  hcan->Init.AutoBusOff = ENABLE;
  hcan->Init.AutoWakeUp = DISABLE;
  hcan->Init.AutoRetransmission = ENABLE;
  hcan->Init.ReceiveFifoLocked = DISABLE;
  hcan->Init.TransmitFifoPriority = DISABLE;

  return (HAL_CAN_Init(hcan) == HAL_OK) ? 1U : 0U;
}

uint8_t CanBridge_Init(uint32_t baud_kbps)
{
  memset(s_ring, 0, sizeof(s_ring));
  memset((void *)s_error_pending, 0, sizeof(s_error_pending));
  memset((void *)s_busoff_pending, 0, sizeof(s_busoff_pending));
  memset((void *)s_last_error_code, 0, sizeof(s_last_error_code));

  gpio_init_can_pins();

  __HAL_RCC_CAN1_CLK_ENABLE();
  /* CAN2 clock is gated by the CAN1 clock enable on connectivity-line
   * parts (shared filter banks/master-slave relationship), but HAL still
   * expects CAN2's own instance to be usable once CAN1's clock is on. */

  hcan1.Instance = CAN1;
  hcan2.Instance = CAN2;

  if (!configure_bit_timing(&hcan1, baud_kbps)) {
    return 0U;
  }
  if (!configure_bit_timing(&hcan2, baud_kbps)) {
    return 0U;
  }

  /* Pass-all filters: 0..13 assigned to CAN1, 14..27 to CAN2 (bxCAN shared
   * filter bank split on this family). Software-side filtering (triggers,
   * PC-side ID filter box in ui/can_monitor_tab.py) happens above this
   * layer, so the firmware itself does not drop frames by ID. */
  CAN_FilterTypeDef filter = {0};
  filter.FilterIdHigh = 0x0000;
  filter.FilterIdLow = 0x0000;
  filter.FilterMaskIdHigh = 0x0000;
  filter.FilterMaskIdLow = 0x0000;
  filter.FilterFIFOAssignment = CAN_FILTER_FIFO0;
  filter.FilterBank = 0;
  filter.FilterMode = CAN_FILTERMODE_IDMASK;
  filter.FilterScale = CAN_FILTERSCALE_32BIT;
  filter.FilterActivation = ENABLE;
  filter.SlaveStartFilterBank = 14;
  if (HAL_CAN_ConfigFilter(&hcan1, &filter) != HAL_OK) {
    return 0U;
  }

  filter.FilterBank = 14;
  if (HAL_CAN_ConfigFilter(&hcan2, &filter) != HAL_OK) {
    return 0U;
  }

  if (HAL_CAN_Start(&hcan1) != HAL_OK) {
    return 0U;
  }
  if (HAL_CAN_Start(&hcan2) != HAL_OK) {
    return 0U;
  }

  if (HAL_CAN_ActivateNotification(&hcan1, CAN_IT_RX_FIFO0_MSG_PENDING) != HAL_OK) {
    return 0U;
  }
  if (HAL_CAN_ActivateNotification(&hcan2, CAN_IT_RX_FIFO0_MSG_PENDING) != HAL_OK) {
    return 0U;
  }

  /* Error/bus-off reporting (CURSOR_FIX_PROMPT.md 4.4): without these,
   * AutoBusOff silently recovers the peripheral on its own, but neither the
   * firmware nor the PC ever learn that a bus fault (e.g. bad/missing
   * termination, disconnected bus) happened at all. See
   * HAL_CAN_ErrorCallback() below and CMD_CAN_ERROR_STATUS in protocol.c /
   * PROTOCOL.md. */
  uint32_t error_its = CAN_IT_ERROR | CAN_IT_BUSOFF | CAN_IT_LAST_ERROR_CODE;
  if (HAL_CAN_ActivateNotification(&hcan1, error_its) != HAL_OK) {
    return 0U;
  }
  if (HAL_CAN_ActivateNotification(&hcan2, error_its) != HAL_OK) {
    return 0U;
  }

  /* Default both channels to Normal mode, termination off — actual
   * per-channel state (Normal/Silent, termination on/off) is a hardware
   * jumper/config concern per board and should be revisited once the real
   * schematic is available (see README.md "Placeholder pinout"). */
  CanBridge_SetTransceiverMode(0, 0, 0);
  CanBridge_SetTransceiverMode(1, 0, 0);

  return 1U;
}

uint8_t CanBridge_Transmit(const can_frame_t *frame)
{
  CAN_HandleTypeDef *hcan = (frame->channel == 0U) ? &hcan1 : &hcan2;

  CAN_TxHeaderTypeDef header;
  header.StdId = frame->extended ? 0U : (frame->id & 0x7FFU);
  header.ExtId = frame->extended ? (frame->id & 0x1FFFFFFFU) : 0U;
  header.IDE = frame->extended ? CAN_ID_EXT : CAN_ID_STD;
  header.RTR = CAN_RTR_DATA;
  header.DLC = frame->dlc;
  header.TransmitGlobalTime = DISABLE;

  /* Bounded poll for a free TX mailbox: reception must never be blocked by
   * this (ТЗ 12.3), so we do not spin forever — a saturated TX side
   * (bus-off, arbitration loss under heavy load) yields after a few
   * hundred iterations, and the frame is simply not sent rather than
   * stalling the main loop / CAN RX IRQ handling. */
  uint32_t mailbox;
  for (uint32_t attempt = 0; attempt < 500U; attempt++) {
    if (HAL_CAN_GetTxMailboxesFreeLevel(hcan) > 0U) {
      if (HAL_CAN_AddTxMessage(hcan, &header, (uint8_t *)frame->data, &mailbox) != HAL_OK) {
        return 0U;
      }
      s_tx_count[frame->channel]++;
      return 1U;
    }
  }
  return 0U;
}

/* IRQ-context callback invoked by HAL_CAN_IRQHandler when a frame lands in
 * RX FIFO0. Kept minimal (copy into ring buffer only) to respect the
 * ТЗ 12.3 <= ±1ms / non-blocking-reception requirements. */
void HAL_CAN_RxFifo0MsgPendingCallback(CAN_HandleTypeDef *hcan)
{
  CAN_RxHeaderTypeDef header;
  can_frame_t frame;

  if (HAL_CAN_GetRxMessage(hcan, CAN_RX_FIFO0, &header, frame.data) != HAL_OK) {
    return;
  }

  frame.channel = (hcan->Instance == CAN1) ? 0U : 1U;
  frame.extended = (header.IDE == CAN_ID_EXT) ? 1U : 0U;
  frame.id = frame.extended ? header.ExtId : header.StdId;
  frame.dlc = (uint8_t)header.DLC;

  ring_push(frame.channel, &frame);

  /* Trigger evaluation is deliberately NOT called from here: per ТЗ 12.3,
   * trigger processing must never block reception, and doing more work
   * inside this ISR would extend IRQ latency for the *next* incoming
   * frame. Trigger_OnFrame() is instead invoked from the main loop
   * immediately after CanBridge_PopRx(), see main.c. */
}
