/* Linker script for STM32F105RCT6 main application.
 *
 * Flash layout (see firmware/PROTOCOL.md, Part 3):
 *   0x08000000 - 0x08007FFF (32 KB)  bootloader (not part of this project)
 *   0x08008000 - 0x080087FF (2 KB)   device info page: имя/s/n/версия ПО
 *                                    (идентификация — в начале Flash)
 *   0x08008800 - 0x08008FFF (2 KB)   application integrity metadata (APP1)
 *   0x08009000 - 0x0803BFFF (204 KB) application code  <-- this .ld's FLASH region
 *   code_end   - 0x0803FFFF          маркерное хранилище (storage.c):
 *                                    FLXH (ГЛ) за кодом · TRGH «посередине»
 *                                    · EVLH (журнал) · VARH (переменные) у
 *                                    конца Flash — области по маркерам,
 *                                    фиксированных адресов нет
 *
 * The metadata page is intentionally excluded from the linker FLASH region.
 * It is generated after linking by firmware/tools/add_app_metadata.py and
 * contains the application size and CRC32 used by the bootloader.
 *
 * The metadata and storage regions are intentionally excluded from the
 * linker FLASH region so that .text/.data can never be placed there by
 * accident; storage.c/trigger.c/event_log.c find their areas by scanning
 * marker headers (see Inc/storage.h). The application uses well under
 * half of the region — the rest is free space for the storage chain.
 */

MEMORY
{
  RAM    (xrw) : ORIGIN = 0x20000000, LENGTH = 64K
  FLASH  (rx)  : ORIGIN = 0x08009000, LENGTH = 204K
}

/* Highest address of the stack. End of 64 KB RAM. */
_estack = ORIGIN(RAM) + LENGTH(RAM);

/* Minimal stack/heap sizes. */
_Min_Heap_Size  = 0x800;
_Min_Stack_Size = 0x800;

/* Fixed addresses живут в заголовках модулей (device_config.h,
 * trigger.h — TRIGGER_LEGACY_ADDR, event_log.h — EVENT_LOG_LEGACY_BASE):
 * у маркерного хранилища фиксированных адресов нет — только маркеры. */

SECTIONS
{
  .isr_vector :
  {
    . = ALIGN(4);
    KEEP(*(.isr_vector))
    . = ALIGN(4);
  } >FLASH

  .text :
  {
    . = ALIGN(4);
    *(.text)
    *(.text*)
    *(.glue_7)
    *(.glue_7t)
    *(.eh_frame)

    KEEP (*(.init))
    KEEP (*(.fini))

    . = ALIGN(4);
    _etext = .;
  } >FLASH

  .rodata :
  {
    . = ALIGN(4);
    *(.rodata)
    *(.rodata*)
    . = ALIGN(4);
  } >FLASH

  .ARM.extab :
  {
    *(.ARM.extab* .gnu.linkonce.armextab.*)
  } >FLASH

  .ARM :
  {
    __exidx_start = .;
    *(.ARM.exidx*)
    __exidx_end = .;
  } >FLASH

  .preinit_array :
  {
    PROVIDE_HIDDEN (__preinit_array_start = .);
    KEEP (*(.preinit_array*))
    PROVIDE_HIDDEN (__preinit_array_end = .);
  } >FLASH

  .init_array :
  {
    PROVIDE_HIDDEN (__init_array_start = .);
    KEEP (*(SORT(.init_array.*)))
    KEEP (*(.init_array*))
    PROVIDE_HIDDEN (__init_array_end = .);
  } >FLASH

  .fini_array :
  {
    PROVIDE_HIDDEN (__fini_array_start = .);
    KEEP (*(SORT(.fini_array.*)))
    KEEP (*(.fini_array*))
    PROVIDE_HIDDEN (__fini_array_end = .);
  } >FLASH

  _sidata = LOADADDR(.data);

  /* Конец области кода/данных во Flash — начало маркерного хранилища
   * (storage.c): FLXH/EVLH/TRGH/VARH размещаются выше этого адреса. */
  _app_code_end = _sidata + SIZEOF(.data);

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)

    . = ALIGN(4);
    _edata = .;
  } >RAM AT> FLASH

  /* Строго между .data и .bss: нельзя ставить ПОСЛЕ _ebss — область
   * _ebss..SP красится стековой канарейкой (paint_stack_canary) и дамп
   * краха затёрся бы до чтения. Startup зануляет только _sbss.._ebss,
   * поэтому .noinit переживает системный/IWDG-ресет. */
  . = ALIGN(4);
  .noinit (NOLOAD) :
  {
    *(.noinit)
    *(.noinit*)
    . = ALIGN(4);
  } >RAM

  . = ALIGN(4);
  .bss :
  {
    _sbss = .;
    __bss_start__ = _sbss;
    *(.bss)
    *(.bss*)
    *(COMMON)

    . = ALIGN(4);
    _ebss = .;
    __bss_end__ = _ebss;
  } >RAM

  ._user_heap_stack :
  {
    . = ALIGN(8);
    PROVIDE ( end = . );
    PROVIDE ( _end = . );
    . = . + _Min_Heap_Size;
    . = . + _Min_Stack_Size;
    . = ALIGN(8);
  } >RAM

  /DISCARD/ :
  {
    libc.a ( * )
    libm.a ( * )
    libgcc.a ( * )
  }

  .ARM.attributes 0 : { *(.ARM.attributes) }
}
