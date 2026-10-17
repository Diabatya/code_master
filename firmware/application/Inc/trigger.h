/* Trigger engine: up to TRIGGER_COUNT independently configurable triggers
 * (ТЗ 10.2; количество ограничено только местом в странице Flash), each
 * with a receive condition
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

/* Хранилище триггеров — маркерное (storage.c): область начинается
 * заголовком "TRGH", находится сканированием страниц за концом кода и
 * «плавает» посередине свободного пространства (между цепочкой FLXH
 * снизу и EVLH/VARH сверху). storage.c пересаживает её
 * (Trigger_Relocate), когда растущие области переменных/ГЛ наезжают
 * на её страницы — фиксированного адреса нет.
 *
 * Пустое устройство не занимает ни страницы; область растёт с числом
 * записей (1 страница на каждые ~22 записи + заголовок). Новая область
 * пишется в свободные страницы зазора и становится видимой только когда
 * дописан заголовок — обрыв коммита оставляет прежний список целым.
 * Записи v2 из старой фиксированной области 0x0803E000 импортируются
 * при первой загрузке и переписываются в новый формат при ближайшем
 * COMMIT. Формат записи v3 — 90 Б: к 82 байтам v2 добавились
 * rx_fire_limit/rx_flags и src_fire_limit/src_flags (функции «кол-во
 * сработок до смены DATA» и «слушать отправляемое»). Хранилище v3
 * (записи 82 Б) читается и поднимается в RAM; следующий COMMIT пишет
 * хранилище v4 с записями 90 Б. */
#define TRIGGER_POOL_BASE     0x0803E000U /* только для host-теста эмуляции */
#define TRIGGER_FLASH_END     0x08040000U /* конец Flash F105RCT6 */
#define TRIGGER_FLASH_PAGE    2048U
#define TRIGGER_POOL_PAGES    ((TRIGGER_FLASH_END - TRIGGER_POOL_BASE) / TRIGGER_FLASH_PAGE)
#define TRIGGER_HEADER_MAGIC  0x54524748U /* "TRGH" */
#define TRIGGER_HEADER_SIZE   16U
#define TRIGGER_STORE_VERSION 4U
#define TRIGGER_STORE_VERSION_V3 3U /* записи 82 Б — читаются, мигрируют */
#define TRIGGER_MAGIC         0x54524732U /* "TRG2" */
#define TRIGGER_FORMAT_VERSION 3U
#define TRIGGER_FORMAT_VERSION_V2 2U
#define TRIGGER_RECORD_SIZE    90U
#define TRIGGER_RECORD_SIZE_V2 82U
/* Флаги rx_flags/src_flags: 1 — игнорировать кадры, отправленные самим
 * МК (TX-эхо); 0 — слушать и внешние, и свои (поведение v2). Бит
 * инвертирован относительно галочки UI «Слушать отправляемое», чтобы
 * старые записи (flags=0) сохраняли прежнюю реакцию на эхо. */
#define TRIGGER_F_MUTE_ECHO   0x01U
/* rx_flags бит 1 — «сработка после старта устройства»: условие приёма
 * заменяется событием запуска МК — триггер вооружается однократно в
 * Trigger_FireOnBoot() при загрузке, на кадры шины не реагирует.
 * Поля rx_* в записи при этом не используются (UI их блокирует).
 * Требует protocol>=6: старая прошивка бита не знает — хост такие
 * записи на неё не пишет (исполняет приложение). */
#define TRIGGER_F_FIRE_ON_BOOT 0x02U
/* src_flags бит 1 — «только наполнение кэша»: запись пополняет
 * s_cache по своему src-фильтру, но ответ не вооружается. Так
 * выражается связка «автозапись DATA в кэш + обычный ответ» в одном
 * блоке UI: строки кэша — филлеры (cache_enabled=1, CACHE_ONLY),
 * фреймы ответа — обычные записи группы (cache_enabled=0). Без бита
 * кэш-запись отвечала бы закэшированным кадром и плодила лишнюю
 * отправку рядом с настроенным ответом. Протокол 8. */
#define TRIGGER_F_CACHE_ONLY  0x02U
/* rx_flags биты 2..4 — индекс условия приёма при мульти-условиях ИЛИ
 * (несколько строк «Приём» в UI одного триггера). Прошивка бит не
 * интерпретирует — каждая запись самостоятельна; он нужен только
 * вычитке хоста для сборки условий обратно в один блок. Протокол 7. */
#define TRIGGER_F_COND_SHIFT  2U
#define TRIGGER_F_COND_MASK   0x1CU
/* rx_flags бит 5 — «X-байты ответа из кадра события» (скомпилированная
 * ГЛ): позиции отмечены src_from[i]==0xFF (кэш-фильтр src_* у таких
 * записей не используется), при матче байты кадра-события складываются
 * в элемент очереди запусков (s_flex_queue, pad-8 снимок) и
 * подставляются в ответ — в т.ч. в повторах серии (семантика ГЛ
 * «X = байт из пришедшего кадра»). Протокол 12. */
#define TRIGGER_F_TX_EVENT_DATA 0x20U
/* rx_flags бит 6 — событие перехода доп. канала (FLXB v2, протокол 13):
 * rx_channel = канал 1..7, rx_dlc = 1 («канал включился» —
 * OFF→ON/PWM/PULSE) или 2 («выключился» — активный→OFF, включая
 * авто-выключение по сроку). Остальные rx-поля нулевые; кадры шины
 * такую запись не возбуждают. Протокол 13. */
#define TRIGGER_F_RX_AUX_EVENT 0x40U
/* src_flags бит 2 — действие = команда доп. каналу (FLXB v2):
 * tx_channel = канал 1..7, tx_dlc = режим AUX_MODE_*, параметры в
 * tx_data: PWM — freq u16 [0..1], duty [2], duration u32 [3..6];
 * PULSE — on u16 [0..1], off u16 [2..3], count u16 [4..5];
 * ON — duration u32 [0..3]; OFF — без параметров. tx_count/interval
 * для aux-команд = 1/0. Протокол 13. */
#define TRIGGER_F_TX_AUX       0x04U
/* src_flags бит 3 — условие по доп. каналу (FLXB v2, только при
 * cache_enabled==0): src_channel = канал 1..7, src_dlc = 1 (условие
 * «работает») или 2 («не работает»). Проверяется в момент события
 * по логическому состоянию AuxOut_IsActive — низкая фаза ШИМ и ШИМ
 * 0% остаются «работает». Протокол 13. */
#define TRIGGER_F_COND_AUX     0x08U
/* rx_flags бит 7 — событие по переменной (FLXB v3, протокол 14).
 * Два подвида по rx_channel:
 *  - 0..2 (CAN-канал источника): «динамическая переменная» — кадр
 *    проходит обычный фильтр rx_extended/rx_id/rx_id_mask/rx_dlc,
 *    затем из его DATA извлекается беззнаковое поле и сравнивается.
 *    Раскладка rx_data_mask: [0] = offset байта, [1] = длина 1..8,
 *    [2] = порядок (0 — старший байт в первой позиции списка,
 *    big-endian окна; 1 — little-endian), [3] = сравнение
 *    (0 ==, 1 >, 2 <, 3 — каждый подошедший кадр, «импульсная»),
 *    [4..7] = 0. Сравниваемое значение — u64 LE в rx_data[0..7].
 *    Срабатывание — фронт истинности условия (false→true), как
 *    _dyn_flags на ПК; между кадрами источника значение не меняется,
 *    поэтому оценка только на подошедших кадрах эквивалентна.
 *  - 0xFF: слушатель фронта статического флага — rx_dlc = фронт
 *    (0 — любой, 1 — «стал 1», 2 — «стал 0»), rx_data_mask[0] =
 *    индекс флага 0..15; кадры шины запись не возбуждают. Флаги
 *    поддерживаются записями-поднимателями статических переменных
 *    и действиями «Переменная» — см. TRIGGER_F_TX_SETFLAG. */
#define TRIGGER_F_RX_VAR       0x80U
#define TRIGGER_VAR_CHAN_FLAG  0xFFU
#define FLEX_MAX_FLAGS         16U
/* src_flags бит 4 — псевдо-действие «установить флаг» (FLXB v3):
 * tx_data[0] = индекс флага 0..15, tx_data[1] = значение 0/1,
 * tx_data[2..5] = u32 LE «авто-выкл через N мс» (0 — нет;
 * реализует действие «включить на N мс»). CAN не отправляется;
 * delay_ms/count/interval работают как у обычной серии. Переход
 * флага ставится в очередь и раздаётся слушателям RX_VAR/0xFF
 * с бюджетом на вызов Poll — как aux-события. Протокол 14. */
#define TRIGGER_F_TX_SETFLAG   0x10U
/* src_flags бит 5 — условие «флаг в состоянии» (FLXB v3):
 * src_id = индекс флага 0..15, src_dlc = 1 («стал/есть 1») или
 * 2 («есть 0»). Проверяется в момент события — как COND_AUX по
 * логическому состоянию. Протокол 14. */
#define TRIGGER_F_COND_FLAG    0x20U
/* Предел по RAM, а не по пулу: триггеры, staging-буфер и кэши живут в
 * ОЗУ — 70 записей ≈ 3 страницы пула из 4 (четвёртая остаётся запасом
 * на рост записи/пула). При росте RAM-бюджета можно поднять до 99. */
#define TRIGGER_MAX_RECORDS   70U
/* Скомпилированные записи ГЛ (контейнер FLXB в блобе FLXH): живут в
 * хвосте списка за пользовательскими триггерами, исполняются тем же
 * движком — матч кадра, задержки, повторы, кэш, FIRE_ON_BOOT.
 * ПК не вычитывает их как триггеры (Trigger_Count/Get ограничены
 * пользовательской частью). */
#define FLEX_MAX_RECORDS      16U
#define TRIGGER_ALL_RECORDS   (TRIGGER_MAX_RECORDS + FLEX_MAX_RECORDS)
/* Edge-группа алиасов именованного состояния dyn_cache (CM-FR12):
 * RX_VAR-записи одного логического события «var == имя» со всеми его
 * сырыми значениями делят общую защёлку фронта — переход между
 * алиасами внутри состояния не пересрабатывает. Идентификатор группы
 * кладётся в rx_data_mask[5] (0 — у записи своя защёлка), разрешён
 * только у dyn_cache-сравнений «==». Битмаска защёлок 32-битная:
 * id группы 1..FLEX_EDGE_GROUP_MAX. */
#define FLEX_EDGE_GROUP_MAX   31U
/* Глубина очереди отложенных запусков записей ГЛ: каждое совпадение
 * события ставит свою серию (как QTimer на ПК), записи ДЕЛЯТ общий пул.
 * Ограничена RAM; переполнение считается в dropped-статистике, а не
 * теряет команду молча (ревью CM-R1). */
#define FLEX_QUEUE_DEPTH      16U
/* Бюджет разбора событий доп. каналов за один Trigger_Poll — защита
 * от кольцевых программ ГЛ («вкл A → выкл B; выкл B → вкл A»): каскад
 * обрезается до бюджета за проход, остальное ждёт следующего Poll.
 * Протокол 13. */
#define FLEX_AUX_EVENTS_PER_POLL 8U
/* Очередь переходов статических флагов (FLXB v3, протокол 14) и
 * бюджет её разбора за один Poll — та же защита от кольцевых
 * программ, что и у aux-событий: «флаг A → флаг B → флаг A…»
 * обрезается до бюджета за проход, переполнение считается
 * в dropped-статистике. */
#define FLEX_FLAG_EVENTS_DEPTH      8U
#define FLEX_FLAG_EVENTS_PER_POLL   8U
/* Легаси-регион v2: 49 слотов × 82 Б с 0x0803E000 (читается один раз
 * при миграции, затем страницы возвращаются в пул). */
#define TRIGGER_LEGACY_ADDR   0x0803E000U
#define TRIGGER_LEGACY_COUNT  49U

typedef struct __attribute__((packed)) {
  uint32_t magic;       /* "TRGH" */
  uint8_t  version;     /* TRIGGER_STORE_VERSION */
  uint8_t  count;       /* число записей, идущих следом */
  uint8_t  flags;       /* зарезервировано, 0 */
  uint32_t generation;  /* монотонный счётчик — выбор новейшей области */
  uint32_t reserved;
  uint8_t  crc8;        /* crc8 байт 0..14 */
} trigger_header_t; /* 16 B */

typedef struct __attribute__((packed)) {
  uint32_t magic;
  uint8_t  enabled;
  uint8_t  rx_channel;      /* «Приём»: 0=CAN1, 1=CAN2, 2=любой из двух */
  uint8_t  rx_extended;
  uint32_t rx_id;
  uint32_t rx_id_mask;      /* bits set = must match; bits clear = don't-care */
  uint8_t  rx_dlc;
  uint8_t  rx_data[8];
  uint8_t  rx_data_mask[8]; /* per-byte don't-care mask, 0x00 = ignore byte */
  uint8_t  tx_channel;      /* «Куда отправляем»: 0=CAN1, 1=CAN2, 2=оба */
  uint8_t  tx_extended;
  uint32_t tx_id;
  uint8_t  tx_dlc;
  uint8_t  tx_data[8];
  uint16_t delay_ms;        /* пауза перед отправкой, 0..~65s */
  uint8_t  reserved[2];     /* [0]=FORMAT_VERSION, [1]=RECORD_SIZE */
  uint8_t  tx_rtr;          /* 0=data response, 1=Remote Transmission Request */
  uint8_t  cache_enabled;   /* режим «автоматическая запись DATA в кэш» */
  uint8_t  src_channel;     /* «Откуда читаем»: 0=CAN1, 1=CAN2, 2=любой */
  uint8_t  src_extended;
  uint32_t src_id;          /* кадр кэшируется при точном совпадении ID */
  uint8_t  src_dlc;
  uint8_t  src_from[8];     /* побайтовая нижняя граница Data (src_dlc байт) */
  uint8_t  src_to[8];       /* побайтовая верхняя граница; from>to = wildcard «X» (байт игнорируется, в кэше = 0x00) */
  uint16_t tx_interval_ms;  /* пауза между повторными отправками */
  uint8_t  tx_count;        /* кол-во отправок (0 трактуется как 1) */
  uint8_t  rx_rtr;          /* приём: 0=любой кадр, 1=только RTR, 2=только data */
  uint8_t  group_seq;       /* связка записей одного UI-триггера (хост) */
  /* --- поля формата v3 (82..89) --- */
  uint16_t rx_fire_limit;   /* «кол-во сработок до смены DATA» приёма: N
                             * срабатываний на неизменной Data, затем
                             * игнор до смены содержимого. 0 = выкл. */
  uint8_t  rx_flags;        /* TRIGGER_F_* — бит 0: MUTE_ECHO */
  uint16_t src_fire_limit;  /* то же для захвата кэша (по src-фильтру) */
  uint8_t  src_flags;       /* TRIGGER_F_* — бит 0: MUTE_ECHO */
  uint16_t boot_delay_ms;   /* «сработка после старта»: пауза от запуска
                             * МК до вооружения ответа, 0..9999 мс —
                             * добавляется к delay_ms. Имеет смысл только
                             * с FIRE_ON_BOOT. Протокол 7. */
  uint8_t  crc8;
} trigger_t; /* 90 bytes */

/* Loads all triggers from Flash into RAM (call once at boot). Any slot
 * with a bad magic/CRC is treated as "disabled, all zero". */
void Trigger_Init(void);

/* Must be called once per main-loop iteration to service pending delayed
 * responses (uses HAL_GetTick(), millisecond resolution — see the ±1ms
 * latency caveat in README.md regarding SysTick-based timing vs. a
 * dedicated hardware timer). */
void Trigger_Poll(void);

/* Returns cumulative trigger timing counters since boot. dropped_count —
 * отправки ответа, исчерпавшие ретраи на занятых TX-ящиках (можно NULL). */
void Trigger_GetStats(uint32_t *fired_count, uint32_t *max_lateness_ms,
                      uint32_t *dropped_count);

/* Сколько включённых триггеров прошло валидацию Flash при старте —
 * диагностика «триггеры пропали после выключения питания». */
uint8_t Trigger_FlashValidCount(void);

/* Evaluates all enabled triggers against a freshly received frame and
 * arms any matching response (respecting its configured delay_ms). Must
 * be called from the main loop only, never from IRQ context. */
void Trigger_OnFrame(const can_frame_t *frame);

/* Событие перехода доп. канала (FLXB v2, протокол 13): матчит записи
 * хвоста ГЛ с TRIGGER_F_RX_AUX_EVENT — channel 1..7, is_on = новое
 * логическое состояние. Вызывается только из Trigger_Poll — бюджет
 * каскада FLEX_AUX_EVENTS_PER_POLL ограничивает кольцевые программы. */
void Trigger_OnAuxEvent(uint8_t channel, uint8_t is_on);

/* Однократно вооружает все включённые триггеры с rx_flags&
 * TRIGGER_F_FIRE_ON_BOOT — «сработка после старта устройства».
 * Вызывать один раз при загрузке, когда CAN уже поднят (в main() после
 * CanBridge_StartInterrupts): отправка уходит через Trigger_Poll() с
 * учётом delay_ms/tx_count/tx_interval_ms, как обычный ответ. */
void Trigger_FireOnBoot(void);

/* Read one trigger slot (index 0..Trigger_Count()-1) into *out. Returns 1
 * if index valid. */
uint8_t Trigger_Get(uint8_t index, trigger_t *out);

/* Число записей в активном списке триггеров (0 на пустом устройстве). */
uint8_t Trigger_Count(void);

/* Validates and writes one trigger slot to Flash + RAM. Returns 1 on
 * success. NOTE: STM32F1 Flash стирается только постранично — запись
 * переписывает весь активный список, но только реально занятые страницы
 * (пустые страницы пула не стираются). */
uint8_t Trigger_Set(uint8_t index, const trigger_t *trig);

/* Запись с провода (82 Б v2 или 90 Б v3) → runtime trigger_t. Хвост
 * короткой записи нулями — новые поля v3 выключены. */
uint8_t Trigger_FromWire(const uint8_t *src, uint32_t len, trigger_t *out);

/* Stages one trigger in RAM and commits the list in one Flash pass.
 * COMMIT принимает итоговую длину списка: позиции без staged-записи
 * берутся из текущего списка, хвост за total отбрасывается — так
 * выражается и изменение, и удаление, и добавление. */
uint8_t Trigger_Stage(uint8_t index, const trigger_t *trig);
uint8_t Trigger_Commit(uint8_t total);

/* Полностью стирает хранилище триггеров (все области TRGH + легаси
 * v2-регион) и очищает RAM-список — для «Заводских настроек». */
void Trigger_ClearAll(void);

/* Позиция области триггеров для раскладки маркерного хранилища
 * (storage.c): база и занятые страницы, 0/0 — области нет. */
uint32_t Trigger_StoreBase(void);
uint32_t Trigger_StorePages(void);

/* Пересадка области триггеров на new_base — storage.c вызывает её,
 * когда растущая область переменных/гибкой логики наезжает на
 * триггеры. Содержимое берётся из RAM-копии списка. */
uint8_t Trigger_Relocate(uint32_t new_base);

/* Enables/disables a trigger without touching its other fields. */
uint8_t Trigger_SetEnabled(uint8_t index, uint8_t enabled);

/* Перечитывает контейнер FLXB из блоба FLXH и пересобирает хвост
 * списка записей (скомпилированные программы ГЛ). Вызывается после
 * COMMIT области FLEX — программы применяются без перезагрузки. */
void Trigger_ReloadFlex(void);

#ifdef __cplusplus
}
#endif

#endif /* __TRIGGER_H */
