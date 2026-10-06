import threading
import unittest

from motor_controller import CommandConflict, GPIOHardware, MotorController, SimulatedHardware


class FakeHardware(SimulatedHardware):
    def __init__(self):
        self.calls = []

    def coast(self):
        self.calls.append(("coast",))

    def drive(self, direction, speed):
        self.calls.append((direction, speed))

    def close(self):
        self.calls.append(("close",))


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.hardware = FakeHardware()
        self.motor = MotorController(self.hardware, clock=lambda: self.now, watchdog=False)
        self.addCleanup(self.motor.close)

    def test_startup_is_off_and_status_never_leaks_token(self):
        self.assertEqual(self.hardware.calls, [("coast",)])
        self.assertEqual(self.motor.status()["direction"], "stopped")
        self.motor.run("forward", 40)
        self.assertNotIn("token", self.motor.status())

    def test_invalid_commands_never_drive(self):
        for speed in (-1, 101, 10**400, float("nan"), float("inf"), True, "40", None):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                self.motor.run("forward", speed)
        with self.assertRaises(ValueError):
            self.motor.run("left", 50)
        self.assertEqual(self.hardware.calls, [("coast",)])

    def test_heartbeat_expiry_coasts_and_late_heartbeat_cannot_restart(self):
        token = self.motor.run("forward", 50)["token"]
        self.now += 1.5
        self.motor.heartbeat(token)
        self.now += 1.5
        self.assertEqual(self.motor.status()["direction"], "forward")
        self.now += 0.6
        with self.assertRaises(CommandConflict):
            self.motor.heartbeat(token)
        self.assertEqual(self.hardware.calls[-1], ("coast",))
        with self.assertRaises(CommandConflict):
            self.motor.run("forward", 50)
        self.motor.stop()
        self.assertEqual(self.motor.run("forward", 50)["direction"], "forward")

    def test_stop_and_zero_speed_clear_lease(self):
        for stop in (self.motor.stop, lambda: self.motor.run("forward", 0)):
            token = self.motor.run("forward", 25)["token"]
            self.assertEqual(stop()["speed"], 0)
            with self.assertRaises(CommandConflict):
                self.motor.heartbeat(token)
            with self.assertRaises(CommandConflict):
                self.motor.run("forward", 70, token)

    def test_reversal_coasts_and_requires_explicit_stop(self):
        token = self.motor.run("forward", 50)["token"]
        with self.assertRaises(CommandConflict):
            self.motor.run("reverse", 50, token)
        self.assertEqual(self.hardware.calls[-1], ("coast",))
        with self.assertRaises(CommandConflict):
            self.motor.run("reverse", 50)
        self.motor.stop()
        self.motor.run("reverse", 30)
        self.assertEqual(self.hardware.calls[-1], ("reverse", 30))

    def test_another_tab_cannot_take_over_or_keep_alive(self):
        token = self.motor.run("forward", 40)["token"]
        for other in (None, "wrong"):
            with self.assertRaises(CommandConflict):
                self.motor.run("forward", 70, other)
            with self.assertRaises(CommandConflict):
                self.motor.heartbeat(other)
        self.assertEqual(self.motor.run("forward", 70, token)["speed"], 70)

    def test_close_coasts_and_prevents_new_run(self):
        self.motor.run("forward", 20)
        self.motor.close()
        self.assertEqual(self.hardware.calls[-2:], [("coast",), ("close",)])
        with self.assertRaises(CommandConflict):
            self.motor.run("forward", 20)

    def test_gpio_failure_latches_fault_and_blocks_future_runs(self):
        def broken_drive(direction, speed):
            raise OSError("GPIO unavailable")

        self.hardware.drive = broken_drive
        with self.assertRaises(OSError):
            self.motor.run("forward", 20)
        self.assertEqual(self.hardware.calls[-1], ("coast",))
        self.assertTrue(self.motor.status()["fault"])
        self.motor.stop()
        with self.assertRaises(CommandConflict):
            self.motor.run("forward", 20)

    def test_watchdog_coasts_without_any_incoming_request(self):
        coasted = threading.Event()

        class ObservedHardware(FakeHardware):
            def coast(self):
                super().coast()
                coasted.set()

        hardware = ObservedHardware()
        motor = MotorController(hardware, timeout=0.05)
        self.addCleanup(motor.close)
        coasted.clear()
        motor.run("forward", 45)
        self.assertTrue(coasted.wait(timeout=1), "watchdog did not coast independently")
        self.assertEqual(hardware.calls[-1], ("coast",))


class GPIOTests(unittest.TestCase):
    def test_real_adapter_uses_correct_pins_pwm_and_coast(self):
        from gpiozero.pins.mock import MockFactory, MockPWMPin

        factory = MockFactory(pin_class=MockPWMPin)
        hardware = GPIOHardware(pin_factory=factory)
        self.addCleanup(hardware.close)
        in1, in2, enable = (factory.pin(pin) for pin in (17, 27, 18))
        self.assertEqual((in1.state, in2.state, enable.state), (0, 0, 0))
        hardware.drive("forward", 35)
        self.assertEqual((in1.state, in2.state, enable.state), (1, 0, 0.35))
        hardware.coast()
        self.assertEqual((in1.state, in2.state, enable.state), (0, 0, 0))
        hardware.drive("reverse", 80)
        self.assertEqual((in1.state, in2.state, enable.state), (0, 1, 0.8))


if __name__ == "__main__":
    unittest.main()
