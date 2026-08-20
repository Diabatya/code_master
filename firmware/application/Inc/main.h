/* Main header for the STM32F105 CAN-gateway application firmware.
 *
 * Pinout used below is a PLACEHOLDER — no schematic was available when this
 * firmware was written (confirmed with the project owner: "Есть только текст
 * ТЗ, схемы нет"). Every non-obvious pin choice is called out with a comment
 * and is collected again in firmware/application/README.md. Re-check against
 * the real board before flashing hardware.
 */

#ifndef __MAIN_H
#define __MAIN_H

#ifdef __cplusplus
extern "C" {
#endif

#include "stm32f1xx_hal.h"

/* Shared with the bootloader project — must stay bit-for-bit identical so the
 * existing bootloader's soft-reset detection keeps working (see
 * firmware/bootloader/Inc/main.h and firmware/PROTOCOL.md 1.5). */
#define BOOTLOADER_FLAG_ADDRESS 0x20004FF0U
#define BOOTLOADER_FLAG_VALUE   0xDEADBEEFU

/* ---- Placeholder pinout (PLACEHOLDER — confirm against real schematic) --- */

/* CAN1: default (non-remapped) bxCAN1 pins on the F105 */
#define CAN1_RX_PORT   GPIOB
#define CAN1_RX_PIN    GPIO_PIN_8
#define CAN1_TX_PORT   GPIOB
#define CAN1_TX_PIN    GPIO_PIN_9

/* CAN2: default (non-remapped) bxCAN2 pins on the F105 */
#define CAN2_RX_PORT   GPIOB
#define CAN2_RX_PIN    GPIO_PIN_12
#define CAN2_TX_PORT   GPIOB
#define CAN2_TX_PIN    GPIO_PIN_13

/* TJA1050 Normal(0)/Silent(1) select pin per channel — PLACEHOLDER */
#define CAN1_TXRX_S_PORT  GPIOB
#define CAN1_TXRX_S_PIN   GPIO_PIN_0
#define CAN2_TXRX_S_PORT  GPIOB
#define CAN2_TXRX_S_PIN   GPIO_PIN_1

/* 120R bus-termination enable per channel — PLACEHOLDER */
#define CAN1_TERM_PORT GPIOC
#define CAN1_TERM_PIN  GPIO_PIN_0
#define CAN2_TERM_PORT GPIOC
#define CAN2_TERM_PIN  GPIO_PIN_1

/* Silent->Normal switch settle time before first TX (ТЗ note: needs
 * oscilloscope calibration against real TJA1050 + board; a few microseconds
 * is typical per the datasheet). PLACEHOLDER, conservative. */
#define CAN_TRANSCEIVER_SWITCH_DELAY_US 5U

/* General-purpose outputs OUT1..OUT4 per ТЗ. OUT4 = PA15, which is normally
 * JTDI; JTAG is disabled (SWD-only debug, ТЗ 13) to free this pin. */
#define OUT1_PORT GPIOC
#define OUT1_PIN  GPIO_PIN_2
#define OUT2_PORT GPIOC
#define OUT2_PIN  GPIO_PIN_3
#define OUT3_PORT GPIOC
#define OUT3_PIN  GPIO_PIN_4
#define OUT4_PORT GPIOA
#define OUT4_PIN  GPIO_PIN_15

/* VBUS sense: HIGH = USB powered, LOW = standalone mode (ТЗ 12.4). Same pin
 * already used as the USB PCD VBUS-sense input in usbd_conf.c; read directly
 * via GPIO here for the standalone-mode decision in the main loop. */
#define VBUS_SENSE_PORT GPIOA
#define VBUS_SENSE_PIN  GPIO_PIN_9

/* Status LED, reused from the bootloader project. */
#define LED_PORT GPIOC
#define LED_PIN  GPIO_PIN_13

void Error_Handler(void);

#ifdef __cplusplus
}
#endif

#endif /* __MAIN_H */
