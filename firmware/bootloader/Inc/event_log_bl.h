/* Append-only запись в кольцо журнала приложения из бутлоадера. */

#ifndef __EVENT_LOG_BL_H
#define __EVENT_LOG_BL_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

/* Записать «вход в загрузчик» (EVLOG_BOOTLOADER, тип 11).
 * code: 1 — остались по флагу хоста, 2 — приложение невалидно.
 * Никогда не стирает Flash; пропускает запись, если слот занят. */
void BlEventLog_NoteBoot(uint8_t code);

#ifdef __cplusplus
}
#endif

#endif /* __EVENT_LOG_BL_H */
