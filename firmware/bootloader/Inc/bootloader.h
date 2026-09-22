/* AN3155-compatible bootloader over USB CDC. */

#ifndef __BOOTLOADER_H
#define __BOOTLOADER_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>
#include <stdbool.h>

void Bootloader_Init(void);
void Bootloader_ProcessRx(const uint8_t* data, uint16_t len);
void Bootloader_Task(void);

void Bootloader_RequestStay(void);

/* 0 — прыжок в приложение; 1 — остаться: флаг хоста (BKP->DR1);
 * 2 — остаться: приложение невалидно/отсутствует. Код уходит в журнал
 * событий (EVLOG_BOOTLOADER, см. firmware/application/Inc/event_log.h). */
uint8_t Bootloader_ShouldStay(void);

void Bootloader_JumpToApplication(uint32_t address);

#ifdef __cplusplus
}
#endif

#endif /* __BOOTLOADER_H */
