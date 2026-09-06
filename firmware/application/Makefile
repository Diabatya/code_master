# Makefile for STM32F105RCT6 main application firmware (CAN gateway + triggers).
# Mirrors firmware/bootloader/Makefile; builds with arm-none-eabi-gcc and the
# vendored STM32CubeF1 HAL/USB libraries.
#
# NOTE: arm-none-eabi-gcc is not available in the sandbox that produced this
# firmware skeleton, so this Makefile has NOT been compile-tested here. Build
# and verify on your own machine (see firmware/bootloader/README.md for the
# toolchain path used previously: /opt/homebrew/bin/arm-none-eabi-gcc on macOS).

PROJECT = codemaster_app
BUILD_DIR = build

CUBE = ../libs/STM32CubeF1

CC = arm-none-eabi-gcc
AS = arm-none-eabi-gcc -x assembler-with-cpp
CP = arm-none-eabi-objcopy
SZ = arm-none-eabi-size
AR = arm-none-eabi-ar
LD = arm-none-eabi-gcc

MCU = -mcpu=cortex-m3 -mthumb
DEFS = -DSTM32F105xC -DUSE_HAL_DRIVER

INCLUDES = \
  -IInc \
  -I$(CUBE)/Drivers/CMSIS/Include \
  -I$(CUBE)/Drivers/CMSIS/Device/ST/STM32F1xx/Include \
  -I$(CUBE)/Drivers/STM32F1xx_HAL_Driver/Inc \
  -I$(CUBE)/Middlewares/ST/STM32_USB_Device_Library/Core/Inc \
  -I$(CUBE)/Middlewares/ST/STM32_USB_Device_Library/Class/CDC/Inc

CFLAGS = $(MCU) $(DEFS) $(INCLUDES) -O2 -g -Wall -ffunction-sections -fdata-sections
ASFLAGS = $(MCU) $(DEFS) $(INCLUDES) -Wall -fdata-sections -ffunction-sections
LDFLAGS = $(MCU) -specs=nano.specs -specs=nosys.specs -TSTM32F105RCTx_APP.ld \
          -Wl,-Map=$(BUILD_DIR)/$(PROJECT).map,--gc-sections -ffunction-sections -fdata-sections

C_SOURCES = \
  Src/main.c \
  Src/protocol.c \
  Src/can_bridge.c \
  Src/trigger.c \
  Src/device_config.c \
  Src/usbd_cdc_if.c \
  Src/usbd_conf.c \
  Src/usbd_desc.c \
  Src/stm32f1xx_it.c \
  $(CUBE)/Drivers/CMSIS/Device/ST/STM32F1xx/Source/Templates/system_stm32f1xx.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_cortex.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_flash.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_flash_ex.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_gpio.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_gpio_ex.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_pcd.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_pcd_ex.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_pwr.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_rcc.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_rcc_ex.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_can.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_dma.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_iwdg.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_tim.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_hal_tim_ex.c \
  $(CUBE)/Drivers/STM32F1xx_HAL_Driver/Src/stm32f1xx_ll_usb.c \
  $(CUBE)/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_core.c \
  $(CUBE)/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ioreq.c \
  $(CUBE)/Middlewares/ST/STM32_USB_Device_Library/Core/Src/usbd_ctlreq.c \
  $(CUBE)/Middlewares/ST/STM32_USB_Device_Library/Class/CDC/Src/usbd_cdc.c

ASM_SOURCES = \
  $(CUBE)/Drivers/CMSIS/Device/ST/STM32F1xx/Source/Templates/gcc/startup_stm32f105xc.s

OBJECTS = $(addprefix $(BUILD_DIR)/,$(notdir $(C_SOURCES:.c=.o)))
vpath %.c $(sort $(dir $(C_SOURCES)))

OBJECTS += $(addprefix $(BUILD_DIR)/,$(notdir $(ASM_SOURCES:.s=.o)))
vpath %.s $(sort $(dir $(ASM_SOURCES)))

.PHONY: all clean

all: $(BUILD_DIR)/$(PROJECT).elf $(BUILD_DIR)/$(PROJECT).bin $(BUILD_DIR)/$(PROJECT).hex
	$(SZ) $(BUILD_DIR)/$(PROJECT).elf

$(BUILD_DIR)/%.o: %.c Makefile | $(BUILD_DIR)
	$(CC) -c $(CFLAGS) -Wa,-a,-ad,-alms=$(BUILD_DIR)/$(notdir $(<:.c=.lst)) $< -o $@

$(BUILD_DIR)/%.o: %.s Makefile | $(BUILD_DIR)
	$(AS) -c $(ASFLAGS) $< -o $@

$(BUILD_DIR)/$(PROJECT).elf: $(OBJECTS) Makefile
	$(LD) $(OBJECTS) $(LDFLAGS) -o $@

$(BUILD_DIR)/$(PROJECT).raw.hex: $(BUILD_DIR)/$(PROJECT).elf
	$(CP) -O ihex $< $@

$(BUILD_DIR)/$(PROJECT).hex: $(BUILD_DIR)/$(PROJECT).raw.hex ../tools/add_app_metadata.py
	python3 ../tools/add_app_metadata.py $< $@ $(BUILD_DIR)/$(PROJECT).bin

$(BUILD_DIR)/$(PROJECT).bin: $(BUILD_DIR)/$(PROJECT).hex
	@test -f $@

$(BUILD_DIR):
	mkdir -p $@

clean:
	rm -rf $(BUILD_DIR)
