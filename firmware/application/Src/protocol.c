/* Protocol handler implementation. See protocol.h and firmware/PROTOCOL.md
 * for the exact wire format (Part 1 = existing/frozen, Part 2 = new
 * commands added for ТЗ 11/8 functionality).
 */

#include <string.h>
#include "main.h"
#include "protocol.h"
#include "usbd_cdc_if.h"
#include "can_bridge.h"
#include "device_config.h"
#include "trigger.h"

/* Markers, see firmware/PROTOCOL.md 1.1 (must match core/can_protocol.py) */
#define MARKER_RX_STD   0xAAU /* device -> PC, standard ID */
#define MARKER_RX_EXT   0xABU /* device -> PC, extended ID */
#define MARKER_TX_STD   0xBBU /* PC -> device, standard ID */
#define MARKER_TX_EXT   0xBCU /* PC -> device, extended ID */

#define CMD_DEVICE_ID        0x90U
#define CMD_DEVICE_ID_RESP   0x91U
#define CMD_DEVICE_INFO      0x92U
#define CMD_DEVICE_INFO_RESP 0x93U
#define CMD_AUTO_SPEED       0xA0U
#define CMD_AUTO_SPEED_RESP  0xA1U

#define CMD_CFG_READ           0xC0U
#define CMD_CFG_WRITE          0xC1U
#define CMD_CFG_FACTORY_RESET  0xC2U
#define CMD_TRIGGER_READ       0xC3U
#define CMD_TRIGGER_WRITE      0xC4U
#define CMD_TRIGGER_ENABLE     0xC5U
#define CMD_CAN_ERROR_STATUS   0xC6U
#define CMD_CAN_STATS           0xC7U
#define CMD_TRIGGER_STATS       0xC8U
#define CMD_SYSTEM_INFO         0xC9U
#define APP_METADATA_ADDR       0x0803D000U
#define APP_METADATA_MAGIC      0x41505031U
#define CMD_RESP_OFFSET        0x10U /* response marker = request | 0x10, see PROTOCOL.md Part 2 */

#define REBOOT_MAGIC_LEN 22U
static const uint8_t REBOOT_MAGIC[REBOOT_MAGIC_LEN] =
  { 0x00, 'R','E','B','O','O','T','_','T','O','_','B','O','O','T','L','O','A','D','E','R','\n' };

static uint8_t s_device_type;
static uint8_t s_device_version;

void Protocol_Init(uint8_t device_type, uint8_t device_version)
{
  s_device_type = device_type;
  s_device_version = device_version;
}

static void reboot_to_bootloader(void)
{
  uint32_t *flag = (uint32_t *)BOOTLOADER_FLAG_ADDRESS;
  *flag = BOOTLOADER_FLAG_VALUE;
  NVIC_SystemReset();
}

static uint8_t xor_checksum(const uint8_t *buf, uint32_t len)
{
  uint8_t x = 0;
  for (uint32_t i = 0; i < len; i++) {
    x ^= buf[i];
  }
  return x;
}

/* Sends one CAN frame to the PC in the format from PROTOCOL.md 1.1. */
static void send_can_frame(const can_frame_t *frame)
{
  /* Worst case: marker(1) + channel(1) + 4-byte ext id(4) + len(1) +
   * 8 data + checksum(1) = 16 bytes (matches PROTOCOL.md 1.1: "8 + len"
   * for the extended format, len<=8). Sized to 16 exactly, not 13 as in
   * an earlier draft of this function (that undersized buffer allowed a
   * 3-4 byte stack overflow for ext frames with dlc>=6 — fixed). */
  uint8_t buf[16];
  uint32_t n = 0;

  buf[n++] = frame->extended ? MARKER_RX_EXT : MARKER_RX_STD;
  buf[n++] = frame->channel;
  if (frame->extended) {
    buf[n++] = (uint8_t)(frame->id & 0xFFU);
    buf[n++] = (uint8_t)((frame->id >> 8) & 0xFFU);
    buf[n++] = (uint8_t)((frame->id >> 16) & 0xFFU);
    buf[n++] = (uint8_t)((frame->id >> 24) & 0xFFU);
  } else {
    buf[n++] = (uint8_t)(frame->id & 0xFFU);
    buf[n++] = (uint8_t)((frame->id >> 8) & 0xFFU);
  }
  buf[n++] = frame->dlc;
  for (uint8_t i = 0; i < frame->dlc; i++) {
    buf[n++] = frame->data[i];
  }
  /* checksum covers the WHOLE frame INCLUDING the marker, per
   * core/can_protocol.py::pack_can_frame ("frame += bytes([xor_checksum(frame)])"
   * where `frame` already starts with the marker byte). An earlier draft of
   * this file excluded the marker, which would never match the PC's
   * checksum in either direction — fixed. See PROTOCOL.md 1.1 (corrected). */
  buf[n] = xor_checksum(&buf[0], n);
  n++;

  CDC_Transmit_FS(buf, (uint16_t)n);
}

static void send_new_cmd_response(uint8_t cmd, uint8_t status, const uint8_t *payload, uint8_t len)
{
  uint8_t buf[3 + 255];
  buf[0] = (uint8_t)(cmd + CMD_RESP_OFFSET);
  buf[1] = status;
  buf[2] = len;
  if (len > 0U && payload != NULL) {
    memcpy(&buf[3], payload, len);
  }
  CDC_Transmit_FS(buf, (uint16_t)(3U + len));
}

/* Handles one fully-received new-protocol command (0xC0-0xC5), consuming
 * `total_len` bytes already known to be present in the FIFO starting at
 * offset 0 ([cmd][payload_len][payload...]). Returns nothing; always
 * sends a response. */
static void handle_new_command(uint8_t cmd, const uint8_t *payload, uint8_t payload_len)
{
  switch (cmd) {
    case CMD_CFG_READ: {
      const device_config_t *cfg = DeviceConfig_Get();
      uint8_t out[1 + DEVICE_CONFIG_NAME_MAX + 1 + DEVICE_CONFIG_SERIAL_MAX + 4];
      uint32_t n = 0;
      out[n++] = cfg->device_name_len;
      memcpy(&out[n], cfg->device_name, cfg->device_name_len); n += cfg->device_name_len;
      out[n++] = cfg->serial_len;
      memcpy(&out[n], cfg->serial, cfg->serial_len); n += cfg->serial_len;
      out[n++] = (uint8_t)(cfg->vid & 0xFFU);
      out[n++] = (uint8_t)((cfg->vid >> 8) & 0xFFU);
      out[n++] = (uint8_t)(cfg->pid & 0xFFU);
      out[n++] = (uint8_t)((cfg->pid >> 8) & 0xFFU);
      send_new_cmd_response(cmd, 0x00U, out, (uint8_t)n);
      break;
    }

    case CMD_CFG_WRITE: {
      if (payload_len < 2U) {
        send_new_cmd_response(cmd, 0x01U, NULL, 0U);
        break;
      }
      uint32_t p = 0;
      uint8_t name_len = payload[p++];
      if (name_len > DEVICE_CONFIG_NAME_MAX || (p + name_len) > payload_len) {
        send_new_cmd_response(cmd, 0x01U, NULL, 0U);
        break;
      }
      const uint8_t *name = &payload[p]; p += name_len;
      if (p >= payload_len) { send_new_cmd_response(cmd, 0x01U, NULL, 0U); break; }
      uint8_t serial_len = payload[p++];
      if (serial_len > DEVICE_CONFIG_SERIAL_MAX || (p + serial_len + 4U) > payload_len) {
        send_new_cmd_response(cmd, 0x01U, NULL, 0U);
        break;
      }
      const uint8_t *serial = &payload[p]; p += serial_len;
      uint16_t vid = (uint16_t)(payload[p] | (payload[p + 1] << 8)); p += 2U;
      uint16_t pid = (uint16_t)(payload[p] | (payload[p + 1] << 8)); p += 2U;

      uint8_t ok = DeviceConfig_Write(name, name_len, serial, serial_len, vid, pid);
      send_new_cmd_response(cmd, ok ? 0x00U : 0x02U, NULL, 0U);
      if (ok) {
        /* Re-enumerate with the new name/serial (ТЗ 11.3). A full
         * USB stack re-init is simplest/most robust here; a soft reset
         * via the same bootloader-flag mechanism would also work but
         * would unnecessarily route through the bootloader. */
        HAL_Delay(50);
        NVIC_SystemReset();
      }
      break;
    }

    case CMD_CFG_FACTORY_RESET: {
      uint8_t ok = DeviceConfig_FactoryReset();
      send_new_cmd_response(cmd, ok ? 0x00U : 0x02U, NULL, 0U);
      if (ok) {
        HAL_Delay(50);
        NVIC_SystemReset();
      }
      break;
    }

    case CMD_TRIGGER_READ: {
      if (payload_len < 1U || payload[0] >= TRIGGER_COUNT) {
        send_new_cmd_response(cmd, 0x01U, NULL, 0U);
        break;
      }
      trigger_t t;
      Trigger_Get(payload[0], &t);
      send_new_cmd_response(cmd, 0x00U, (const uint8_t *)&t, (uint8_t)sizeof(t));
      break;
    }

    case CMD_TRIGGER_WRITE: {
      if (payload_len < 1U + sizeof(trigger_t) || payload[0] >= TRIGGER_COUNT) {
        send_new_cmd_response(cmd, 0x01U, NULL, 0U);
        break;
      }
      trigger_t t;
      memcpy(&t, &payload[1], sizeof(trigger_t));
      uint8_t ok = Trigger_Set(payload[0], &t);
      send_new_cmd_response(cmd, ok ? 0x00U : 0x02U, NULL, 0U);
      break;
    }

    case CMD_TRIGGER_ENABLE: {
      if (payload_len < 2U || payload[0] >= TRIGGER_COUNT) {
        send_new_cmd_response(cmd, 0x01U, NULL, 0U);
        break;
      }
      uint8_t ok = Trigger_SetEnabled(payload[0], payload[1]);
      send_new_cmd_response(cmd, ok ? 0x00U : 0x02U, NULL, 0U);
      break;
    }

    case CMD_CAN_ERROR_STATUS: {
      /* Request payload: [channel]. Response payload: [had_error]
       * [had_busoff][last_error_code (4 bytes, little-endian)]. Reading
       * this clears the sticky had_error/had_busoff flags (same
       * drop-and-report semantics as the RX-overflow flag), so the PC
       * should poll it periodically to not miss transient faults. See
       * CanBridge_TookError()/CanBridge_TookBusOff() in can_bridge.c and
       * firmware/PROTOCOL.md. */
      if (payload_len < 1U || payload[0] > 1U) {
        send_new_cmd_response(cmd, 0x01U, NULL, 0U);
        break;
      }
      uint32_t last_error_code = 0U;
      uint8_t had_error = CanBridge_TookError(payload[0], &last_error_code);
      uint8_t had_busoff = CanBridge_TookBusOff(payload[0]);
      uint8_t out[6];
      out[0] = had_error;
      out[1] = had_busoff;
      out[2] = (uint8_t)(last_error_code & 0xFFU);
      out[3] = (uint8_t)((last_error_code >> 8) & 0xFFU);
      out[4] = (uint8_t)((last_error_code >> 16) & 0xFFU);
      out[5] = (uint8_t)((last_error_code >> 24) & 0xFFU);
      send_new_cmd_response(cmd, 0x00U, out, (uint8_t)sizeof(out));
      break;
    }

    case CMD_CAN_STATS: {
      if (payload_len < 1U || payload[0] > 1U) {
        send_new_cmd_response(cmd, 0x01U, NULL, 0U);
        break;
      }
      can_stats_t stats;
      CanBridge_GetStats(payload[0], &stats);
      uint8_t out[12];
      memcpy(&out[0], &stats.rx_count, 4U);
      memcpy(&out[4], &stats.tx_count, 4U);
      memcpy(&out[8], &stats.lost_count, 4U);
      send_new_cmd_response(cmd, 0x00U, out, (uint8_t)sizeof(out));
      break;
    }

    case CMD_TRIGGER_STATS: {
      uint32_t fired_count = 0U;
      uint32_t max_lateness_ms = 0U;
      Trigger_GetStats(&fired_count, &max_lateness_ms);
      uint8_t out[8];
      memcpy(&out[0], &fired_count, 4U);
      memcpy(&out[4], &max_lateness_ms, 4U);
      send_new_cmd_response(cmd, 0x00U, out, (uint8_t)sizeof(out));
      break;
    }

    case CMD_SYSTEM_INFO: {
      const device_config_t *cfg = DeviceConfig_Get();
      const uint8_t *metadata = (const uint8_t *)APP_METADATA_ADDR;
      uint8_t out[16] = {
        s_device_version,
        1U,
        0U,
        1U,
        cfg->reserved[0],
        cfg->reserved[1],
        10U,
        0U,
      };
      out[2] = 256U & 0xFFU;
      out[3] = (256U >> 8) & 0xFFU;
      if (*(const uint32_t *)&metadata[0] == APP_METADATA_MAGIC) {
        memcpy(&out[8], &metadata[8], 8U);
      }
      send_new_cmd_response(cmd, 0x00U, out, (uint8_t)sizeof(out));
      break;
    }

    default:
      send_new_cmd_response(cmd, 0x03U, NULL, 0U);
      break;
  }
}

/* Attempts to parse and consume exactly one structure starting at FIFO
 * offset 0. Returns the number of bytes to discard from the FIFO for this
 * attempt: >0 if a structure (valid or invalid-but-resynchronized) was
 * consumed, 0 if more bytes are needed before a decision can be made. */
static uint16_t try_parse_one(void)
{
  uint16_t avail = CDC_GetRxAvailable();
  if (avail == 0U) {
    return 0U;
  }

  uint8_t marker;
  CDC_PeekRxByte(0, &marker);

  /* --- Reboot-to-bootloader magic string can start at this offset ---
   * Checked against however many bytes are actually available so far (not
   * just when the full REBOOT_MAGIC_LEN has arrived): an earlier version of
   * this check only fired once `avail >= REBOOT_MAGIC_LEN`, so if the magic
   * string happened to be split across two USB CDC polls (quite possible —
   * it can appear anywhere in the stream per PROTOCOL.md 1.5), the leading
   * partial match fell through to the "unknown byte, drop and resync" path
   * below and dropped the very byte the magic needed, so the reboot could
   * never be recognized. Now: if the bytes seen so far still match the
   * magic's prefix, wait for more instead of resyncing. */
  if (marker == REBOOT_MAGIC[0]) {
    uint32_t check_len = (avail < REBOOT_MAGIC_LEN) ? avail : REBOOT_MAGIC_LEN;
    uint8_t match_so_far = 1U;
    for (uint32_t i = 0; i < check_len; i++) {
      uint8_t b;
      CDC_PeekRxByte((uint16_t)i, &b);
      if (b != REBOOT_MAGIC[i]) { match_so_far = 0U; break; }
    }
    if (match_so_far) {
      if (avail >= REBOOT_MAGIC_LEN) {
        reboot_to_bootloader(); /* never returns */
      }
      return 0U; /* still a valid prefix, wait for the rest */
    }
  }

  /* --- CAN frame from PC (0xBB/0xBC) --- */
  if (marker == MARKER_TX_STD || marker == MARKER_TX_EXT) {
    uint8_t extended = (marker == MARKER_TX_EXT) ? 1U : 0U;
    /* Bytes needed to safely read the dlc field: marker+channel+id+dlc,
     * i.e. up to and including the dlc byte itself (offset 6 ext / 4 std,
     * so 7/5 bytes). An earlier version used 8/6 here (one too many),
     * which then propagated into total_len below and made every parsed
     * frame's total length (and therefore its buffer/checksum window) one
     * byte too long — fixed. */
    uint32_t header_len = extended ? 7U : 5U;
    if (avail < header_len) {
      return 0U; /* wait for more bytes */
    }
    uint8_t channel;
    CDC_PeekRxByte(1, &channel);
    uint32_t id = 0;
    uint8_t b;
    if (extended) {
      for (uint8_t i = 0; i < 4U; i++) { CDC_PeekRxByte(2U + i, &b); id |= ((uint32_t)b) << (8U * i); }
    } else {
      for (uint8_t i = 0; i < 2U; i++) { CDC_PeekRxByte(2U + i, &b); id |= ((uint32_t)b) << (8U * i); }
    }
    uint8_t len_off = extended ? 6U : 4U;
    uint8_t dlc;
    CDC_PeekRxByte(len_off, &dlc);
    if (dlc > 8U) {
      return 1U; /* corrupt length: resync by dropping just the marker */
    }
    /* total_len (including checksum) = header_len + dlc + 1, i.e. 8+dlc
     * (ext, max 16) / 6+dlc (std, max 14) — matches PROTOCOL.md 1.1 and
     * core/can_protocol.py's `total_length = 4 + id_length + length`. */
    uint32_t total_len = header_len + dlc + 1U;
    if (avail < total_len) {
      return 0U; /* wait for the rest */
    }

    /* Worst case total_len is 16 (ext, dlc=8); sized to 16 exactly, not 13
     * as in an earlier draft (which both undersized the buffer AND relied
     * on the wrong total_len formula above — either bug alone could
     * overflow this buffer by several bytes, fixed together). */
    uint8_t frame_bytes[16];
    for (uint32_t i = 0; i < total_len; i++) {
      CDC_PeekRxByte((uint16_t)i, &frame_bytes[i]);
    }
    /* checksum covers the WHOLE frame INCLUDING the marker (frame_bytes[0]),
     * per core/can_protocol.py::pack_can_frame/unpack_can_frame — an earlier
     * version excluded the marker here, which would never match a real PC
     * frame's checksum. Fixed together with send_can_frame() above. */
    uint8_t checksum = xor_checksum(&frame_bytes[0], (uint32_t)(total_len - 1U));
    if (checksum != frame_bytes[total_len - 1U]) {
      return 1U; /* bad checksum: drop just the marker byte and resync */
    }

    can_frame_t frame;
    frame.channel = channel;
    frame.extended = extended;
    frame.id = id;
    frame.dlc = dlc;
    memcpy(frame.data, &frame_bytes[len_off + 1U], dlc);
    CanBridge_Transmit(&frame);
    return (uint16_t)total_len;
  }

  /* --- Legacy single-byte commands (0x90/0x92/0xA0), see PROTOCOL.md 1.2-1.4 --- */
  if (marker == CMD_DEVICE_ID) {
    uint8_t resp[3] = { CMD_DEVICE_ID_RESP, s_device_type, s_device_version };
    CDC_Transmit_FS(resp, 3U);
    return 1U;
  }
  if (marker == CMD_DEVICE_INFO) {
    const device_config_t *cfg = DeviceConfig_Get();
    uint8_t resp[3 + DEVICE_CONFIG_SERIAL_MAX + 2];
    uint32_t n = 0;
    resp[n++] = CMD_DEVICE_INFO_RESP;
    resp[n++] = cfg->serial_len;
    memcpy(&resp[n], cfg->serial, cfg->serial_len); n += cfg->serial_len;
    resp[n++] = s_device_type;
    /* memory_kb saturates at 255 for this 256KB part — known protocol
     * limitation, see PROTOCOL.md 1.3. */
    resp[n++] = 255U;
    CDC_Transmit_FS(resp, (uint16_t)n);
    return 1U;
  }
  if (marker == CMD_AUTO_SPEED) {
    /* Minimal placeholder auto-baud: without a schematic-confirmed bus
     * loopback/listen setup we cannot safely bit-bang a real detection
     * sweep here yet (see firmware/FIRMWARE_INPUT_REQUEST.md "CAN
     * auto-baud detection parameters"). Report the currently configured
     * rate instead of 0, so the PC's auto_detect_can_speed() gets a usable
     * answer rather than a hard failure; replace with a real sweep once
     * hardware is available to validate transceiver switching timing. */
    extern uint32_t g_can_baud_kbps; /* defined in main.c */
    uint8_t resp[3] = { CMD_AUTO_SPEED_RESP, (uint8_t)(g_can_baud_kbps >> 8), (uint8_t)(g_can_baud_kbps & 0xFFU) };
    CDC_Transmit_FS(resp, 3U);
    return 1U;
  }

  /* --- New commands (0xC0-0xC5), see PROTOCOL.md Part 2 --- */
  if (marker >= 0xC0U && marker <= 0xC9U) {
    if (avail < 2U) {
      return 0U;
    }
    uint8_t payload_len;
    CDC_PeekRxByte(1, &payload_len);
    uint32_t total_len = 2U + payload_len;
    if (avail < total_len) {
      return 0U;
    }
    uint8_t payload[255];
    for (uint32_t i = 0; i < payload_len; i++) {
      CDC_PeekRxByte((uint16_t)(2U + i), &payload[i]);
    }
    handle_new_command(marker, payload, payload_len);
    return (uint16_t)total_len;
  }

  /* Unknown byte: drop and resync, matching can_protocol.py's
   * _find_marker() behavior of scanning forward one byte at a time. */
  return 1U;
}

void Protocol_Poll(void)
{
  /* Drain the CDC RX FIFO. Bounded per call so a very long pending queue
   * cannot starve CanBridge_PopRx()/Trigger_Poll() in the same main-loop
   * iteration; the remainder is picked up on the next iteration. */
  for (uint16_t guard = 0; guard < 32U; guard++) {
    uint16_t consumed = try_parse_one();
    if (consumed == 0U) {
      break;
    }
    for (uint16_t i = 0; i < consumed; i++) {
      (void)CDC_ReadRxByte();
    }
  }

  /* Forward buffered CAN RX frames to the PC. Bounded per call for the
   * same reason as above; at 14000 pkt/s aggregate this loop will
   * typically drain a handful of frames per main-loop pass rather than
   * the whole backlog at once, which is fine since the ring buffers
   * (CAN_RING_DEPTH=1024/channel) absorb the difference. */
  can_frame_t frame;
  for (uint8_t channel = 0; channel < 2U; channel++) {
    for (uint16_t guard = 0; guard < 64U; guard++) {
      if (!CanBridge_PopRx(channel, &frame)) {
        break;
      }
      Trigger_OnFrame(&frame);
      send_can_frame(&frame);
    }
  }
}
