"""A stand-in for LabJackPython's u3.U3, enough to run the LabJack thread
without hardware: a MAX31856 behind SPI, the IKR 270 on FIO2, optional
heater voltage / current sense inputs, and a gate output whose every write
is recorded. Faults can be switched on to exercise the error paths."""
import math
import threading
import time

V_SENSE_FIO = 1          # where the tests wire the optional sense inputs
I_SENSE_FIO = 3


class FakeU3:
    def __init__(self, divider, temp_c=30.0, vac_mbar=1.5e-7):
        self.divider = divider
        self.temp_c = temp_c
        self.fault = 0
        self.vac_mbar = vac_mbar
        self.gauge_volts = None          # set to force a gauge-side voltage
        self.reg = {0x00: 0x00, 0x01: 0x00}
        self.writes = []                 # (time, state) for every FIO0 write
        self.lock = threading.Lock()
        # sense inputs: what the heater circuit does
        self.rail_v = 24.0               # supply voltage seen by the V divider
        self.v_scale = 11.0              # supply volts per LabJack volt
        self.i_scale = 0.5               # amps per LabJack volt
        self.element_ohm = 88.0          # None = open element / SW171 off
        self.stray_a = 0.0               # current flowing with the gate OFF
        self.sense_delay_s = 0.0         # slow sense reads stretch every tick
        # faults
        self.fail_next_on_write = False
        self.fail_all_writes = False
        self.fail_vacuum = False
        self.fail_spi = False
        self.tc_absent = False           # MAX31856 reads back all zeros

    # -- device set-up -----------------------------------------------------
    def getCalibrationData(self):
        pass

    def configIO(self, **kw):
        self.config = kw

    def watchdog(self, **kw):
        self.watchdog_cfg = kw

    def close(self):
        pass

    # -- analogue in ---------------------------------------------------------
    def getAIN(self, channel, *args):
        if channel == 2:                                  # IKR 270 on FIO2
            if self.fail_vacuum:
                raise IOError("simulated AIN timeout")
            u_gauge = (self.gauge_volts if self.gauge_volts is not None
                       else (math.log10(self.vac_mbar) + 12.75) / 1.25)
            return u_gauge / self.divider
        if channel == V_SENSE_FIO:
            time.sleep(self.sense_delay_s)
            return self.rail_v / self.v_scale
        if channel == I_SENSE_FIO:
            if self.gate_now():
                amps = 0.0 if self.element_ohm is None else self.rail_v / self.element_ohm
            else:
                amps = self.stray_a
            return amps / self.i_scale
        raise ValueError(f"nothing wired to AIN{channel}")

    # -- digital out: heater gate --------------------------------------------
    def setDOState(self, fio, state):
        assert fio == 0
        if self.fail_all_writes or (state and self.fail_next_on_write):
            self.fail_next_on_write = False
            raise IOError("simulated USB error")
        with self.lock:
            self.writes.append((time.time(), state))

    # -- SPI: MAX31856 -------------------------------------------------------
    def spi(self, SPIBytes, **kw):
        if self.fail_spi:
            raise IOError("simulated SPI error")
        addr = SPIBytes[0]
        n = len(SPIBytes) - 1
        if self.tc_absent:
            return {"SPIBytes": [0] * len(SPIBytes)}
        if addr & 0x80:                                  # write
            self.reg[addr & 0x7F] = SPIBytes[1]
            return {"SPIBytes": [0] * len(SPIBytes)}
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
