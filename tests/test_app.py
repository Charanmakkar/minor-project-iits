import json
import unittest

from app import create_app
from motor_controller import MotorController, SimulatedHardware


class AppTests(unittest.TestCase):
    def setUp(self):
        self.motor = MotorController(SimulatedHardware(), watchdog=False)
        self.addCleanup(self.motor.close)
        self.client = create_app(self.motor).test_client()
        self.headers = {"X-Motor-Control": "1"}

    def test_page_and_run_stop_flow(self):
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"SIMULATION", page.data.upper())
        run = self.client.post("/api/run", headers=self.headers, json={"speed": 40, "steering": -50})
        self.assertEqual(run.status_code, 200)
        self.assertEqual((run.json["left_pwm"], run.json["right_pwm"]), (20, 40))
        token = run.json["token"]
        update = self.client.post("/api/run", headers=self.headers, json={"speed": 60, "steering": 100, "token": token})
        self.assertEqual((update.json["left_pwm"], update.json["right_pwm"]), (60, 0))
        beat = self.client.post("/api/heartbeat", headers=self.headers, json={"token": token})
        self.assertEqual(beat.status_code, 200)
        self.assertNotIn("token", self.client.get("/api/status").json)
        stop = self.client.post("/api/stop", headers=self.headers, json={})
        self.assertEqual(stop.json["direction"], "stopped")
        self.assertEqual((stop.json["left_pwm"], stop.json["right_pwm"]), (0, 0))
        self.assertEqual(self.client.post("/api/heartbeat", headers=self.headers, json={"token": token}).status_code, 409)

    def test_cross_site_and_form_commands_rejected(self):
        payload = {"speed": 50, "steering": 0}
        self.assertEqual(self.client.post("/api/run", json=payload).status_code, 403)
        self.assertEqual(self.client.post("/api/run", headers=self.headers, data=payload).status_code, 403)
        foreign = {**self.headers, "Origin": "https://other.example"}
        self.assertEqual(self.client.post("/api/run", headers=foreign, json=payload).status_code, 403)
        self.assertEqual(self.client.get("/api/run").status_code, 405)
        self.assertEqual(self.motor.status()["direction"], "stopped")

    def test_invalid_json_and_values_rejected(self):
        for data in (
            [], None, {}, {"speed": "100", "steering": 0}, {"speed": 101, "steering": 0},
            {"speed": -1, "steering": 0}, {"speed": 50, "steering": 101},
            {"speed": 50, "steering": -101}, {"speed": 50, "steering": True},
            {"speed": 50, "steering": float("nan")}, {"speed": 50},
        ):
            response = self.client.post("/api/run", headers=self.headers, data=json.dumps(data), content_type="application/json")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.motor.status()["direction"], "stopped")

    def test_another_page_cannot_overwrite_run_but_can_stop(self):
        self.client.post("/api/run", headers=self.headers, json={"speed": 50, "steering": 0})
        intruder = self.client.post("/api/run", headers=self.headers, json={"speed": 90, "steering": -100})
        self.assertEqual(intruder.status_code, 409)
        self.assertEqual(self.motor.status()["speed"], 50)
        stop = self.client.post("/api/run", headers=self.headers, json={"speed": 0, "steering": 100})
        self.assertEqual((stop.json["left_pwm"], stop.json["right_pwm"]), (0, 0))

    def test_server_expiry_rejects_late_heartbeat_until_reset(self):
        now = [0]
        self.motor._clock = lambda: now[0]
        run = self.client.post("/api/run", headers=self.headers, json={"speed": 80, "steering": -50})
        now[0] = 2.1
        heartbeat = self.client.post("/api/heartbeat", headers=self.headers, json={"token": run.json["token"]})
        self.assertEqual(heartbeat.status_code, 409)
        status = self.client.get("/api/status").json
        self.assertTrue(status["requires_stop"])
        self.assertEqual((status["left_pwm"], status["right_pwm"]), (0, 0))
        self.assertEqual(self.client.post("/api/run", headers=self.headers, json={"speed": 80, "steering": 0}).status_code, 409)
        self.client.post("/api/stop", headers=self.headers, json={})
        self.assertEqual(self.client.post("/api/run", headers=self.headers, json={"speed": 80, "steering": 0}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
