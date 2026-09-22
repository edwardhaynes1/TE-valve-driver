"""MAX31856 thermocouple converter on the LabJack's bit-banged SPI (FIO4-7).

    try_init(lj, announce_failure) -> bool   configure; log the outcome
    read(lj) -> Reading                      one transfer: temperature, fault
                                             bits (FAULT_BITS), and whether the
                                             chip still holds our settings
    lost_hint(cr0, cr1) -> str               why the settings are gone
    FAULT_BITS                               fault register bit meanings

A bit-banged SPI read never fails on its own: an unplugged MAX31856 reads
all ones, and a re-powered one reads its factory settings — which don't
convert, so the temperature reads 0.000 °C with no fault. So every read
also reads back CR0/CR1, and the LabJack thread sets the chip up again
whenever they are not ours.
"""

import time
from collections import namedtuple

from .config import (
    TC_FIO_CS, TC_FIO_SCK, TC_FIO_SDI, TC_FIO_SDO, TC_RETRY_S,
)
from .shared import log_event


# MAX31856 register map
REG_CR0     = 0x00
REG_CR1     = 0x01
REG_LTCBH   = 0x0C   # linearized TC temp, high byte
REG_FAULTSR = 0x0F   # fault status register

FAULT_BITS = {
    0x01: "open circuit",
    0x02: "over/under-voltage",
    0x04: "TC low threshold",
    0x08: "TC high threshold",
    0x10: "CJ low threshold",
    0x20: "CJ high threshold",
    0x40: "CJ out of range",
    0x80: "TC out of range",
}


# ═══════════════════════════════════════════════════════════════════════════════
# MAX31856 THERMOCOUPLE  — SPI helpers (LabJack U3 bit-banged SPI)
# ═══════════════════════════════════════════════════════════════════════════════
#
# The U3's SPI command configures pins and transfers in a single low-level
# operation: every transfer must pass the full pin assignment and mode. Passing
# an empty byte list triggers an internal LabJackPython indexing bug, so we
# only ever call _tc_transfer with at least one byte.

def _tc_transfer(lj, spi_bytes):
    """One SPI transfer with the MAX31856 pin configuration."""
    result = lj.spi(
        SPIBytes=spi_bytes,
        AutoCS=True,
        DisableDirConfig=False,
        SPIMode='A',            # CPOL0, CPHA0 (standard for MAX31856)
        SPIClockFactor=0,
        CSPINNum=TC_FIO_CS,
        CLKPinNum=TC_FIO_SCK,
        MISOPinNum=TC_FIO_SDO,
        MOSIPinNum=TC_FIO_SDI,
    )
    return result['SPIBytes']


def _tc_read_reg(lj, reg):
    return _tc_transfer(lj, [reg & 0x7F, 0x00])[1]


def _tc_read_regs(lj, reg, n):
    return _tc_transfer(lj, [reg & 0x7F] + [0x00] * n)[1:]


def _tc_write_reg(lj, reg, value):
    _tc_transfer(lj, [(reg & 0x7F) | 0x80, value & 0xFF])


def _tc_decode_temp(bytes3):
    """Decode the 19-bit signed linearized TC temperature (0.0078125 °C/LSB)."""
    raw = (bytes3[0] << 16) | (bytes3[1] << 8) | bytes3[2]
    raw >>= 5
    if raw & 0x40000:
        raw -= 0x80000
    return raw * 0.0078125


# CR0 bit map (MAX31856 datasheet):
#   bit7 CMODE      1 = automatic conversion
#   bit5:4 OCFAULT  01 = open-circuit detection enabled, TC resistance < 5 kR
#   bit0 50/60 Hz   1 = 50 Hz mains notch filter  (Bern — the old 0x80 value
#                       left this at 60 Hz, which is wrong for Europe)
CR0_VALUE = 0x80 | 0x10 | 0x01      # = 0x91
CR1_VALUE = 0x03                     # AVGSEL = 1 sample, TC type = K


CR0_POWER_ON = 0x00                 # factory settings: conversions off
CR1_POWER_ON = 0x03


def lost_hint(cr0, cr1):
    """Turn a CR0/CR1 readback that isn't ours into a likely cause."""
    if cr0 == CR0_POWER_ON and cr1 == CR1_POWER_ON:
        return ("reads its power-on settings: the MAX31856 lost power "
                "(reconnected, or a supply glitch)")
    if cr0 == 0x00 and cr1 == 0x00:
        return "reads all-zero: MAX31856 unpowered, or SDO (FIO4) not connected / shorted to GND"
    if cr0 == 0xFF and cr1 == 0xFF:
        return "reads all-ones: SDO (FIO4) floating or pulled high — chip absent or unpowered"
    return "reads garbage: check SCK/SDI/CS (FIO5-7) wiring, connector seating and ground"


def _tc_init(lj):
    """Configure MAX31856 for automatic conversion, Type-K.

    Returns (ok, cr0, cr1): ok is True if the register readback confirms SPI
    communication is working; cr0/cr1 are what was read back.

    Open-circuit fault detection is deliberately ENABLED here. Without it a
    detached thermocouple returns a plausible-looking number rather than a
    fault — which, now that the heater is under software control, would let
    the interlock be satisfied by a sensor that is no longer attached."""
    _tc_write_reg(lj, REG_CR0, CR0_VALUE)
    _tc_write_reg(lj, REG_CR1, CR1_VALUE)
    cr0 = _tc_read_reg(lj, REG_CR0)
    cr1 = _tc_read_reg(lj, REG_CR1)
    ok = cr0 == CR0_VALUE and cr1 == CR1_VALUE
    if ok:
        # First conversion + open-circuit check. Only when the chip answered:
        # retries run on the device thread, and one that finds nothing must
        # not hold it up.
        time.sleep(0.3)
    return ok, cr0, cr1


def try_init(lj, announce_failure):
    """Initialise the MAX31856, logging the outcome. Returns True on success.
    Failures are logged only if announce_failure, so retries don't spam."""
    try:
        ok, cr0, cr1 = _tc_init(lj)
    except Exception as e:
        if announce_failure:
            log_event(f"MAX31856 init error: {e} — retrying every {TC_RETRY_S:g} s")
        return False
    if ok:
        log_event("MAX31856 thermocouple init OK")
        return True
    if announce_failure:
        log_event(f"MAX31856 not responding: CR0=0x{cr0:02X} CR1=0x{cr1:02X} "
                  f"(expected 0x{CR0_VALUE:02X}/0x{CR1_VALUE:02X}) — "
                  f"{lost_hint(cr0, cr1)}. Retrying every {TC_RETRY_S:g} s")
    return False


Reading = namedtuple("Reading", "temp_c fault configured cr0 cr1")


def read(lj):
    """One transfer of all 16 registers (the address auto-increments):
    Reading(temp_c, fault, configured, cr0, cr1). configured is False if
    CR0 or CR1 aren't ours — then temp_c and fault mean nothing, and
    lost_hint(cr0, cr1) says why."""
    regs = _tc_read_regs(lj, REG_CR0, 16)
    cr0, cr1 = regs[REG_CR0], regs[REG_CR1]
    return Reading(_tc_decode_temp(regs[REG_LTCBH:REG_LTCBH + 3]), regs[REG_FAULTSR],
                   cr0 == CR0_VALUE and cr1 == CR1_VALUE, cr0, cr1)
