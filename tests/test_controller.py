import threading
import unittest
from unittest.mock import patch

from motor_controller import CommandConflict, GPIOHardware, MotorController, SimulatedHardware, mix_motor_outputs


class FakeHardware(SimulatedHardware):
    def __init__(self):
        self.calls = []

    def coast(self):
        self.calls.append(("coast",))

    def drive(self, left_pwm, right_pwm):
        self.calls.append((left_pwm, right_pwm))

    def close(self):
        self.calls.append(("close",))


class MixerTests(unittest.TestCase):
    def test_straight_half_turn_and_full_turn(self):
        for speed, steering, expected in (
            (80, 0, (80, 80)),
            (80, -50, (40, 80)),
            (80, 50, (80, 40)),
            (100, -100, (0, 100)),
            (100, 100, (100, 0)),
            (0, -100, (0, 0)),
            (0, 100, (0, 0)),
            (12.5, 20, (12.5, 10)),
        ):
            with self.subTest(speed=speed, steering=steering):
                self.assertEqual(mix_motor_outputs(speed, steering), expected)

    def test_steering_is_symmetric_and_never_exceeds_requested_speed(self):
        for speed in (0, 0.5, 30, 100):
            for steering in (-100, -90, -50, 0, 10, 50, 100):
                left, right = mix_motor_outputs(speed, steering)
                reverse_left, reverse_right = mix_motor_outputs(speed, -steering)
                self.assertEqual((left, right), (reverse_right, reverse_left))
                self.assertTrue(0 <= left <= speed and 0 <= right <= speed)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.hardware = FakeHardware()
        self.motor = MotorController(self.hardware, clock=lambda: self.now, watchdog=False)
        self.addCleanup(self.motor.close)

    def test_startup_is_off_and_status_never_leaks_token(self):
        self.assertEqual(self.hardware.calls, [("coast",)])
        self.assertEqual(self.motor.status()["direction"], "stopped")
        self.assertEqual((self.motor.status()["left_pwm"], self.motor.status()["right_pwm"]), (0, 0))
        self.motor.run(40, 0)
        self.assertNotIn("token", self.motor.status())

    def test_invalid_commands_never_drive(self):
        for speed in (-1, 101, 10**400, float("nan"), float("inf"), True, "40", None):
            with self.subTest(speed=speed), self.assertRaises(ValueError):
                self.motor.run(speed, 0)
        for steering in (-101, 101, 10**400, float("nan"), float("inf"), True, "40", None):
            with self.subTest(steering=steering), self.assertRaises(ValueError):
                self.motor.run(50, steering)
        self.assertEqual(self.hardware.calls, [("coast",)])

    def test_heartbeat_expiry_coasts_and_late_heartbeat_cannot_restart(self):
        token = self.motor.run(50, 40)["token"]
        self.now += 1.5
        self.motor.heartbeat(token)
        self.now += 1.5
        self.assertEqual(self.motor.status()["direction"], "forward")
        self.now += 0.6
        with self.assertRaises(CommandConflict):
            self.motor.heartbeat(token)
        self.assertEqual(self.hardware.calls[-1], ("coast",))
        self.assertEqual((self.motor.status()["left_pwm"], self.motor.status()["right_pwm"]), (0, 0))
        with self.assertRaises(CommandConflict):
            self.motor.run(50, 0)
        self.motor.stop()
        self.assertEqual(self.motor.run(50, 0)["direction"], "forward")

    def test_stop_and_zero_speed_clear_lease(self):
        for stop in (self.motor.stop, lambda: self.motor.run(0, -100), lambda: self.motor.run(0, 100)):
            token = self.motor.run(25, -50)["token"]
            state = stop()
            self.assertEqual((state["speed"], state["steering"], state["left_pwm"], state["right_pwm"]), (0, 0, 0, 0))
            with self.assertRaises(CommandConflict):
                self.motor.heartbeat(token)
            with self.assertRaises(CommandConflict):
                self.motor.run(70, 0, token)

    def test_steering_and_speed_update_in_same_run(self):
        token = self.motor.run(50, -50)["token"]
        self.assertEqual(self.hardware.calls[-1], (25, 50))
        result = self.motor.run(80, 75, token)
        self.assertEqual(result["token"], token)
        self.assertEqual((result["speed"], result["steering"], result["left_pwm"], result["right_pwm"]), (80, 75, 80, 20))
        self.assertEqual(self.hardware.calls[-1], (80, 20))

    def test_another_tab_cannot_take_over_or_keep_alive(self):
        token = self.motor.run(40, 0)["token"]
        for other in (None, "wrong"):
            with self.assertRaises(CommandConflict):
                self.motor.run(70, 50, other)
            with self.assertRaises(CommandConflict):
                self.motor.heartbeat(other)
        self.assertEqual(self.motor.run(70, 50, token)["speed"], 70)

    def test_close_coasts_and_prevents_new_run(self):
        self.motor.run(20, 0)
        self.motor.close()
        self.assertEqual(self.hardware.calls[-2:], [("coast",), ("close",)])
        with self.assertRaises(CommandConflict):
            self.motor.run(20, 0)

    def test_gpio_failure_latches_fault_and_blocks_future_runs(self):
        def broken_drive(left_pwm, right_pwm):
            raise OSError("GPIO unavailable")

        self.hardware.drive = broken_drive
        with self.assertRaises(OSError):
            self.motor.run(20, 0)
        self.assertEqual(self.hardware.calls[-1], ("coast",))
        self.assertTrue(self.motor.status()["fault"])
        self.motor.stop()
        with self.assertRaises(CommandConflict):
            self.motor.run(20, 0)

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
        motor.run(45, -50)
        self.assertTrue(coasted.wait(timeout=1), "watchdog did not coast independently")
        self.assertEqual(hardware.calls[-1], ("coast",))


class GPIOTests(unittest.TestCase):
    def setUp(self):
        from gpiozero.pins.mock import MockFactory, MockPWMPin

        self.factory = MockFactory(pin_class=MockPWMPin)
        self.hardware = GPIOHardware(pin_factory=self.factory)
        self.addCleanup(self.hardware.close)
        self.pins = [self.factory.pin(pin) for pin in (17, 27, 18, 23, 24, 13)]

    def test_both_channels_use_correct_pins_pwm_and_coast(self):
        self.assertEqual([pin.state for pin in self.pins], [0] * 6)
        self.hardware.drive(35, 80)
        self.assertEqual([pin.state for pin in self.pins], [1, 0, 0.35, 1, 0, 0.8])
        self.hardware.coast()
        self.assertEqual([pin.state for pin in self.pins], [0] * 6)
        self.hardware.drive(0, 80)
        self.assertEqual([pin.state for pin in self.pins], [0, 0, 0, 1, 0, 0.8])
        self.hardware.drive(40, 0)
        self.assertEqual([pin.state for pin in self.pins], [1, 0, 0.4, 0, 0, 0])

    def test_both_enables_off_before_direction_inputs_change(self):
        self.hardware.drive(50, 50)

        def check_enabled_states():
            self.assertEqual((self.pins[2].state, self.pins[5].state), (0, 0))

        original_off = self.hardware.in1.off

        def checked_off():
            check_enabled_states()
            original_off()

        with patch.object(self.hardware.in1, "off", side_effect=checked_off) as off:
            self.hardware.drive(0, 60)
            off.assert_called_once()

    def test_pwm_only_update_preserves_direction_without_coasting(self):
        self.hardware.drive(50, 50)
        with patch.object(self.hardware, "coast", wraps=self.hardware.coast) as coast:
            self.hardware.drive(20, 60)
            coast.assert_not_called()
        self.assertEqual([pin.state for pin in self.pins], [1, 0, 0.2, 1, 0, 0.6])

    def test_coast_attempts_right_disable_even_when_left_disable_fails(self):
        self.hardware.drive(50, 50)
        with patch.object(self.hardware.enable_left, "off", side_effect=OSError("left GPIO failed")):
            with self.assertRaises(OSError):
                self.hardware.coast()
        self.assertEqual(self.pins[5].state, 0)


if __name__ == "__main__":
    unittest.main()
