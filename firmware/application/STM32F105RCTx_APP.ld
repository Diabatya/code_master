/* Linker script for STM32F105RCT6 main application.
 *
 * Flash layout (see firmware/PROTOCOL.md, Part 3):
 *   0x08000000 - 0x08007FFF (32 KB)  bootloader (not part of this project)
 *   0x08008000 - 0x0803AFFF (204 KB) application code  <-- this .ld's FLASH region
 *   0x0803B000 - 0x0803CFFF (8 KB)   event log ring (see Inc/event_log.h)
 *   0x0803D000 - 0x0803D7FF (2 KB)   application integrity metadata
 *   0x0803D800 - 0x0803DFFF (2 KB)   device config page ("page A" per ТЗ 11.2)
 *   0x0803E000 - 0x0803FFFF (8 KB)   trigger storage (10 slots)
 *
 * The metadata page is intentionally excluded from the linker FLASH region.
 * It is generated after linking by firmware/tools/add_app_metadata.py and
 * contains the application size and CRC32 used by the bootloader.
 *
 * The metadata, event log, config and trigger pages are intentionally
 * excluded from the linker FLASH region so that .text/.data can never be
 * placed there by accident; device_config.c / trigger.c / event_log.c
 * address their own fixed pages directly (see Inc/device_config.h,
 * Inc/trigger.h, Inc/event_log.h). Shrinking the app region from the
 * previous 212 KB frees the 8 KB now reserved for the event log — the
 * application uses well under half of the smaller region.
 */

MEMORY
{
  RAM    (xrw) : ORIGIN = 0x20000000, LENGTH = 64K
  FLASH  (rx)  : ORIGIN = 0x08008000, LENGTH = 204K
}

/* Highest address of the stack. End of 64 KB RAM. */
_estack = ORIGIN(RAM) + LENGTH(RAM);

/* Minimal stack/heap sizes. */
_Min_Heap_Size  = 0x800;
_Min_Stack_Size = 0x800;

/* Fixed addresses for the event log/config/trigger Flash pages, used by
 * event_log.c / device_config.c / trigger.c. Kept here (rather than only in
 * a header) so the memory map is visible in one place next to the FLASH
 * region definition above. */
_eventlog_pool_addr = 0x0803B000;
_config_page_addr  = 0x0803D800;
_trigger_page_addr = 0x0803E000;

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

  .data :
  {
    . = ALIGN(4);
    _sdata = .;
    *(.data)
    *(.data*)

    . = ALIGN(4);
    _edata = .;
  } >RAM AT> FLASH

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
