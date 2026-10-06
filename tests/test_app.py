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
        self.assertIn(b"Simulation", page.data)
        run = self.client.post("/api/run", headers=self.headers, json={"direction": "forward", "speed": 35})
        self.assertEqual(run.status_code, 200)
        token = run.json["token"]
        beat = self.client.post("/api/heartbeat", headers=self.headers, json={"token": token})
        self.assertEqual(beat.status_code, 200)
        self.assertNotIn("token", self.client.get("/api/status").json)
        stop = self.client.post("/api/stop", headers=self.headers, json={})
        self.assertEqual(stop.json["direction"], "stopped")
        self.assertEqual(self.client.post("/api/heartbeat", headers=self.headers, json={"token": token}).status_code, 409)

    def test_cross_site_and_form_commands_rejected(self):
        payload = {"direction": "forward", "speed": 50}
        self.assertEqual(self.client.post("/api/run", json=payload).status_code, 403)
        self.assertEqual(self.client.post("/api/run", headers=self.headers, data=payload).status_code, 403)
        foreign = {**self.headers, "Origin": "https://other.example"}
        self.assertEqual(self.client.post("/api/run", headers=foreign, json=payload).status_code, 403)
        self.assertEqual(self.client.get("/api/run").status_code, 405)
        self.assertEqual(self.motor.status()["direction"], "stopped")

    def test_invalid_json_and_values_rejected(self):
        for data in ([], None, {"direction": "forward", "speed": "100"}, {"direction": "reverse", "speed": 101}):
            response = self.client.post("/api/run", headers=self.headers, data=__import__("json").dumps(data), content_type="application/json")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(self.motor.status()["direction"], "stopped")


if __name__ == "__main__":
    unittest.main()
