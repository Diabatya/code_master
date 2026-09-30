/* Дополнительные каналы выхода OUT1..OUT4 (ТЗ 2.4): PC10/PC11/PC12/PA15,
 * распиновка — main.h / firmware/PINOUT.md.
 *
 * Управление — командой CMD_AUX_SET (PROTOCOL.md): режимы Вкл/Выкл,
 * серия импульсов и программный ШИМ. Команда исполняется автономно:
 * ПК отправляет один пакет, дальнейшее переключение GPIO делает
 * AuxOut_Poll() из главного цикла — связь с ПК для этого не нужна.
 *
 * ШИМ программный (HAL_GetTick, шаг 1 мс): практический потолок
 * ~500 Гц при 50% заполнении, для больших частот нужен аппаратный
 * TIM — отражено ограничением в PROTOCOL.md и в UI. */

#ifndef AUX_OUT_H
#define AUX_OUT_H

#include <stdint.h>

#define AUX_CH_COUNT   4U
#define AUX_MODE_OFF   0U
#define AUX_MODE_ON    1U
#define AUX_MODE_PULSE 2U
#define AUX_MODE_PWM   3U

void    AuxOut_Init(void);
/* Одна команда на канал: mode + параметры импульсов/ШИМ.
 * Возвращает 1 при валидных аргументах, 0 — если канал/режим неверны. */
uint8_t AuxOut_Set(uint8_t channel, uint8_t mode,
                   uint16_t pulse_on_ms, uint16_t pulse_off_ms,
                   uint16_t pulse_count, uint16_t pwm_freq_hz,
                   uint8_t pwm_duty_pct, uint32_t pwm_time_ms);
void    AuxOut_Poll(void);
/* Текущий логический уровень канала (0/1) — для диагностики. */
uint8_t AuxOut_Get(uint8_t channel);

#endif /* AUX_OUT_H */
