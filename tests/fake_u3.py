"""A stand-in for LabJackPython's u3.U3, enough to run devices.labjack_thread
without hardware: a MAX31856 behind SPI, the IKR 270 on FIO2, and a gate
output whose every write is recorded."""
import math
import threading
import time


class FakeU3:
    def __init__(self, divider, temp_c=30.0, vac_mbar=1.5e-7):
        self.divider = divider
        self.temp_c = temp_c
        self.fault = 0
        self.vac_mbar = vac_mbar
        self.reg = {0x00: 0x00, 0x01: 0x00}
        self.writes = []                 # (time, state) for every FIO0 write
        self.fail_next_on_write = False
        self.lock = threading.Lock()

    # -- device set-up -----------------------------------------------------
    def getCalibrationData(self):
        pass

    def configIO(self, **kw):
        self.config = kw

    def watchdog(self, **kw):
        self.watchdog_cfg = kw

    def close(self):
        pass

    # -- analogue in: IKR 270, p = 10 ** (1.25 U - 12.75) --------------------
    def getAIN(self, channel, *args):
        u_gauge = (math.log10(self.vac_mbar) + 12.75) / 1.25
        return u_gauge / self.divider

    # -- digital out: heater gate --------------------------------------------
    def setDOState(self, fio, state):
        assert fio == 0
        if state and self.fail_next_on_write:
            self.fail_next_on_write = False
            raise IOError("simulated USB error")
        with self.lock:
            self.writes.append((time.time(), state))

    # -- SPI: MAX31856 -------------------------------------------------------
    def spi(self, SPIBytes, **kw):
        addr = SPIBytes[0]
        if addr & 0x80:                                  # write
            self.reg[addr & 0x7F] = SPIBytes[1]
            return {"SPIBytes": [0] * len(SPIBytes)}
        n = len(SPIBytes) - 1
        if addr == 0x0C:                                 # linearised temperature
            raw = int(round(self.temp_c / 0.0078125)) << 5
            data = [(raw >> 16) & 0xFF, (raw >> 8) & 0xFF, raw & 0xFF]
        elif addr == 0x0F:
            data = [self.fault]
        else:
            data = [self.reg.get(addr + i, 0) for i in range(n)]
        return {"SPIBytes": [0] + data[:n]}

    # -- helpers for tests ---------------------------------------------------
    def gate_now(self):
        with self.lock:
            return self.writes[-1][1] if self.writes else None

    def on_fraction(self, t0, t1):
        """Fraction of [t0, t1] the gate was commanded ON."""
        with self.lock:
            w = list(self.writes)
        on, state, last = 0.0, 0, t0
        for t, s in w:
            if t <= t0:
                state = s
                continue
            if t >= t1:
                break
            if state:
                on += t - last
            state, last = s, t
        if state:
            on += t1 - last
        return on / (t1 - t0)
