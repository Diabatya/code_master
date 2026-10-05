/* Маркерное хранилище конца Flash (отчёт мастера): жёстких адресов нет —
 * только у кода. Каждая область начинается с заголовка store_header_t,
 * находится сканированием страниц и может переноситься при росте соседей.
 *
 * Карта (по возрастанию адресов):
 *   0x08009000  код приложения (единственный фиксированный регион)
 *   code_end    CFGH — настройки приложения (скорости/режимы/терминаторы
 *               CAN): 1 страница, владелец device_config.c, область
 *               резервируется всегда — «настройки сразу после кода»
 *               FLXH — программы гибкой логики, цепочка поколений
 *               «сверху вниз» от CFGH: новый блоб пишется ВЫШЕ
 *               прежних, старые поколения не затираются (история +
 *               power-loss safety — прежняя область жива до коммита)
 *   ... свободный зазор ...
 *               TRGH — триггеры, «снизу вверх от центра»: область
 *               ЗАКАНЧИВАЕТСЯ на середине зазора и растёт в сторону
 *               FLXH (меньшие адреса)
 *   center      EVLH — журнал событий, «сверху вниз от центра»:
 *               НАЧИНАЕТСЯ на середине зазора и лежит к VARH —
 *               4 страницы [center, center+4p)
 *   ... свободный зазор ...
 *               VARH — конфигурация переменных, цепочка поколений
 *               «снизу вверх» от конца Flash: новый блоб пишется НИЖЕ
 *               прежних; при исчерпании места область уплотняется —
 *               все поколения стираются и цепочка начинается с якоря
 *   0x08040000  FLASH_END
 *
 * VARH/FLXH — непрозрачные для прошивки блобы: содержимое сериализует и
 * разбирает ПК (JSON). CFGH — непрозрачный для storage.c блоб настроек,
 * пишет device_config.c. Прошивка обеспечивает размещение, атомарный
 * коммит (заголовок пишется последним), выбор новейшего поколения по
 * generation при скане и перенос TRGH/EVLH при росте.
 */

#ifndef __STORAGE_H
#define __STORAGE_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

#define STORE_MAGIC_FLEX  0x464C5848UL /* "FLXH" */
#define STORE_MAGIC_VAR   0x56415248UL /* "VARH" */
#define STORE_MAGIC_EVLOG 0x45564C48UL /* "EVLH" */
#define STORE_MAGIC_TRIG  0x54524748UL /* "TRGH" (формат trigger.c) */
#define STORE_MAGIC_CFG   0x43464748UL /* "CFGH" — настройки приложения
                                        * сразу за кодом (отчёт мастера) */

#define STORE_REGION_FLEX 0U
#define STORE_REGION_VAR  1U
#define STORE_REGION_CFG  2U

#define STORE_VERSION        1U
#define STORE_HEADER_SIZE    20U
#define STORE_FLASH_PAGE     2048U
#define STORE_FLASH_END      0x08040000UL
#define STORE_APP_CODE_START 0x08009000UL
#define STORE_EVLOG_PAGES    4U
#define STORE_CFG_PAGES      1U /* настройки приложения — 1 страница */
/* Блоб переменных/ГЛ не может занять весь камень — санити-предел. */
#define STORE_MAX_PAYLOAD    (48U * 1024U)

typedef struct __attribute__((packed)) {
  uint32_t magic;        /* VARH/FLXH/EVLH */
  uint32_t generation;   /* монотонный счётчик — выбор новейшей области */
  uint32_t payload_len;  /* байт полезной нагрузки за заголовком */
  uint32_t payload_crc;  /* CRC32 полезной нагрузки */
  uint16_t version;      /* STORE_VERSION */
  uint8_t  region;       /* STORE_REGION_* */
  uint8_t  crc8;         /* crc8 байт 0..18 — точка валидности */
} store_header_t; /* 20 байт */

/* Сканирует свободное пространство Flash и регистрирует области
 * VARH/FLXH/EVLH/TRGH. Вызывать один раз при старте до EventLog_Init/
 * Trigger_Init — они берут у модуля свои базы. Записей во Flash не
 * делает (только чтение), безопасен до инициализации IWDG-логики. */
void Storage_Init(void);

/* Конец кода приложения, округлённый вверх до страницы. За ним
 * резервируется страница CFGH (настройки приложения) — якорь FLXH. */
uint32_t Storage_CodeEnd(void);
uint32_t Storage_CfgBase(void);   /* = code_end — каноничная страница CFGH */
uint32_t Storage_CfgEnd(void);    /* = code_end + 1 страница */

/* Текущие позиции областей (0 = область не найдена). */
uint32_t Storage_VarBase(void);   /* база области переменных */
uint32_t Storage_VarEnd(void);    /* = STORE_FLASH_END, если область есть */
uint32_t Storage_FlexBase(void);  /* = cfg_end */
uint32_t Storage_FlexEnd(void);   /* cfg_end + размер области (или cfg_end) */
uint32_t Storage_EvlogBase(void); /* база кольца журнала (канонично центр зазора) */
uint32_t Storage_EvlogEnd(void);  /* evlog_base + 4 страницы */
uint8_t  Storage_EvlogFound(void); /* 1 — область EVLH найдена на Flash */
/* Каноничная позиция EVLH — центр зазора [flex_end, var_base) —
 * независимо от того, где область реально найдена сейчас. */
uint32_t Storage_EvlogCanonicalBase(void);
uint32_t Storage_TrigBase(void);  /* найденная область триггеров (0 = нет) */
uint32_t Storage_TrigEnd(void);

/* event_log.c сообщает модулю фактическую базу кольца журнала
 * (создание/перенос) — нужно для расчёта зазора триггеров. */
void Storage_NoteEvlogBase(uint32_t base);

/* trigger.c сообщает позицию области триггеров после инициализации/
 * коммита/переноса и снимает резерв легаси-региона после миграции. */
void Storage_NoteTrig(uint32_t base, uint32_t pages);
void Storage_NoteLegacyTrigDone(void);

/* Полностью стирает ВСЕ поколения области (заводские настройки):
 * скан по маркерам — заодно вычищает и осиротевшие области. */
uint8_t Storage_Clear(uint8_t region);

/* Первая свободная полка под pages страниц — первичное размещение
 * журнала, если каноничная позиция занята. */
uint32_t Storage_FindFreeRange(uint32_t pages);

/* 1, если диапазон не пересекается ни с одной известной областью. */
uint8_t Storage_RangeFree(uint32_t from, uint32_t to);

/* Окно размещения триггеров «посередине»: [TrigGapBase, TrigGapEnd). */
uint32_t Storage_TrigGapBase(void); /* = flex_end */
uint32_t Storage_TrigGapEnd(void);  /* = evlog_base (канонично var-4стр) */

/* event_log.c сообщает, что легаси-пул журнала (0x0803C000, записи без
 * заголовка EVLH) смигрирован/вытёрт — резерв снимается. */
void Storage_NoteLegacyEvlogDone(void);
/* Вынос легаси-пула журнала: стирает его страницы (если на них не села
 * другая область) и снимает резерв. Вызывать после миграции записей в
 * область EVLH. */
uint8_t Storage_LegacyEvlogCleanup(void);

/* Чтение полезной нагрузки новейшего блоба области (VAR/FLEX): копирует
 * до len байт из offset в out. 0 при невалидной области/выходе за
 * границы payload_len новейшего поколения. */
uint8_t Storage_Read(uint8_t region, uint32_t offset, uint8_t *out,
                     uint32_t len);
/* base — страница заголовка новейшего поколения, len — payload_len. */
uint8_t Storage_RegionInfo(uint8_t region, uint32_t *base_out,
                           uint32_t *len_out, uint32_t *gen_out);

/* Сессия записи блоба: Begin выбирает/освобождает страницы (при надобности
 * пересаживая TRGH/EVLH) и стирает целевой диапазон; WriteChunk пишет
 * полезную нагрузку кусками; Commit дописывает заголовок — область
 * становится видимой. Обрыв до Commit оставляет прежнее состояние или
 * пустоту, но не половинчатую область. */
uint8_t Storage_BeginWrite(uint8_t region, uint32_t payload_len);
/* База активной сессии (0 — сессии нет) — для ответа CMD_STORAGE_BEGIN. */
uint32_t Storage_WriteBase(void);
uint8_t Storage_WriteChunk(uint32_t offset, const uint8_t *data,
                           uint32_t len);
uint8_t Storage_Commit(uint32_t payload_crc);
/* Сессия без коммита устаревает через 15 с — висячий сеанс не должен
 * блокировать область навсегда (USB-обрыв посреди записи). */
void Storage_Poll(void);

/* Настройки приложения (CFGH — владелец device_config.c): чтение
 * полезной нагрузки новейшего найденного поколения и запись нового
 * поколения на каноничную страницу за кодом. Write сама стирает
 * страницу, пишет payload, затем заголовок (gen+1) и убирает
 * вытесненную копию, найденную в другом месте (переезд при росте
 * кода). */
uint8_t  Storage_CfgRead(uint8_t *out, uint32_t len);
uint8_t  Storage_CfgWrite(const uint8_t *payload, uint32_t len);
uint32_t Storage_CfgFoundBase(void); /* база найденной области (0 — нет) */

#ifdef __cplusplus
}
#endif

#endif /* __STORAGE_H */
