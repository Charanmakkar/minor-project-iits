"""One L298N motor channel, with serialized commands and a software watchdog."""

import logging
import math
import secrets
import threading
import time


class CommandConflict(ValueError):
    """A command cannot safely be applied in the current state."""


class GPIOHardware:
    """BCM17 -> IN1, BCM27 -> IN2, BCM18 -> ENA (remove ENA jumper)."""

    simulated = False

    def __init__(self, *, pin_factory=None):
        from gpiozero import DigitalOutputDevice, PWMOutputDevice

        if pin_factory is None:
            from gpiozero.pins.lgpio import LGPIOFactory

            pin_factory = LGPIOFactory()

        self._devices = []
        self._factory = pin_factory
        try:
            # Disable the bridge before configuring direction pins.
            self.enable = PWMOutputDevice(
                18, initial_value=0, frequency=1000, pin_factory=self._factory
            )
            self._devices.append(self.enable)
            self.in1 = DigitalOutputDevice(17, initial_value=False, pin_factory=self._factory)
            self._devices.append(self.in1)
            self.in2 = DigitalOutputDevice(27, initial_value=False, pin_factory=self._factory)
            self._devices.append(self.in2)
        except BaseException:
            self.close()
            raise

    def coast(self):
        self.enable.off()
        self.in1.off()
        self.in2.off()

    def drive(self, direction, speed):
        # PWM must be zero before changing either direction input.
        self.coast()
        (self.in1 if direction == "forward" else self.in2).on()
        self.enable.value = speed / 100.0

    def close(self):
        for device in self._devices:
            try:
                device.off()
            except Exception:
                logging.exception("Could not switch GPIO off during cleanup")
            finally:
                try:
                    device.close()
                except Exception:
                    logging.exception("Could not release GPIO during cleanup")
        self._factory.close()


class SimulatedHardware:
    """No GPIO imports or physical outputs; use for a laptop demonstration."""

    simulated = True

    def coast(self):
        pass

    def drive(self, direction, speed):
        pass

    def close(self):
        pass


class MotorController:
    def __init__(self, hardware, timeout=2.0, *, clock=time.monotonic, watchdog=True):
        self.hardware = hardware
        self.timeout = timeout
        self._clock = clock
        self._lock = threading.RLock()
        self._shutdown = threading.Event()
        self._thread = None
        self._direction = "stopped"
        self._speed = 0
        self._token = None
        self._deadline = 0
        self._reason = "Ready; motor drive is off."
        self._must_stop = False
        self._closed = False
        self._fault = False
        self.hardware.coast()
        if watchdog:
            self._thread = threading.Thread(target=self._watch, name="motor-watchdog", daemon=True)
            self._thread.start()

    def _state(self):
        # Never expose the current owner's heartbeat token in public status.
        return {
            "direction": self._direction,
            "speed": self._speed,
            "reason": self._reason,
            "simulated": self.hardware.simulated,
            "watchdog_seconds": self.timeout,
            "requires_stop": self._must_stop,
            "fault": self._fault,
        }

    def _coast(self, reason):
        self._token = None
        self._direction = "stopped"
        self._speed = 0
        self._reason = reason
        try:
            self.hardware.coast()
        except Exception:
            self._fault = True
            self._reason = "GPIO fault: disconnect motor power and restart the application."
            raise

    def _expire(self):
        if self._token is not None and self._clock() >= self._deadline:
            self._coast("Connection timeout; motor drive is off. Press Stop before restarting.")
            self._must_stop = True

    def status(self):
        with self._lock:
            self._expire()
            return self._state()

    def run(self, direction, speed, token=None):
        if direction not in ("forward", "reverse"):
            raise ValueError("Direction must be forward or reverse.")
        if isinstance(speed, bool) or not isinstance(speed, (int, float)):
            raise ValueError("Speed must be a number from 0 to 100.")
        if not 0 <= speed <= 100 or not math.isfinite(speed):
            raise ValueError("Speed must be a finite number from 0 to 100.")
        with self._lock:
            self._expire()
            if self._closed or self._fault:
                raise CommandConflict("Controller unavailable; disconnect motor power and restart.")
            if speed == 0:
                return self.stop()
            if self._must_stop:
                raise CommandConflict("Press Stop, wait for the shaft to stop, then run again.")
            if self._token is not None and token != self._token:
                raise CommandConflict("Another page owns this run. Press Stop before taking control.")
            if self._direction not in ("stopped", direction):
                self._coast("Reversal rejected; press Stop and wait for the shaft to stop.")
                self._must_stop = True
                raise CommandConflict(self._reason)
            # A stale request cannot restart a stopped motor after a Stop command.
            if self._token is None and token is not None:
                raise CommandConflict("This run has ended. Press a direction button to start again.")
            try:
                self.hardware.drive(direction, speed)
            except Exception:
                self._fault = True
                self._coast("GPIO fault: disconnect motor power and restart the application.")
                raise
            self._direction = direction
            self._speed = speed
            self._token = self._token or secrets.token_urlsafe(24)
            self._deadline = self._clock() + self.timeout
            self._reason = "Running; keep this page visible."
            return {**self._state(), "token": self._token}

    def heartbeat(self, token):
        with self._lock:
            self._expire()
            if self._token is None or token != self._token:
                raise CommandConflict("Run ended or belongs to another page. Press Stop to reset.")
            self._deadline = self._clock() + self.timeout
            return self._state()

    def stop(self):
        with self._lock:
            if not self._closed:
                self._coast("Motor drive is off; wait for the shaft to stop before reversing.")
                self._must_stop = False
            return self._state()

    def _watch(self):
        while not self._shutdown.wait(0.05):
            try:
                self.status()
            except Exception:
                logging.exception("Motor watchdog could not disable GPIO; disconnect motor power.")

    def close(self):
        self._shutdown.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        with self._lock:
            if not self._closed:
                try:
                    self._coast("Controller stopped.")
                finally:
                    self._closed = True
                    self.hardware.close()
