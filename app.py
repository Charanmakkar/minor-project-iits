"""Run with `python app.py`; add --simulate to use no GPIO hardware."""

import argparse
import logging
import signal

from flask import Flask, jsonify, render_template, request
from waitress import serve

from motor_controller import CommandConflict, GPIOHardware, MotorController, SimulatedHardware


def create_app(controller):
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 1024

    @app.before_request
    def check_command_request():
        if request.method == "POST":
            # Cross-site forms cannot send JSON + this custom header. No CORS is enabled.
            if request.headers.get("X-Motor-Control") != "1" or not request.is_json:
                return jsonify(error="Commands require JSON and X-Motor-Control: 1."), 403
            origin = request.headers.get("Origin")
            if origin and origin != request.host_url.rstrip("/"):
                return jsonify(error="Cross-origin commands are not allowed."), 403

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def body():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise ValueError("Send a JSON object.")
        return payload

    @app.errorhandler(CommandConflict)
    def conflict(error):
        return jsonify(error=str(error)), 409

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error=str(error)), 400

    @app.errorhandler(Exception)
    def unexpected(error):
        from werkzeug.exceptions import HTTPException

        if isinstance(error, HTTPException):
            return jsonify(error=error.description), error.code
        app.logger.exception("Motor request failed")
        try:
            controller.stop()
        except Exception:
            app.logger.exception("Could not disable GPIO; disconnect motor power")
        return jsonify(error="Controller error. Disconnect motor power and check the server."), 503

    @app.get("/")
    def index():
        return render_template("index.html", simulated=controller.hardware.simulated)

    @app.get("/api/status")
    def status():
        return jsonify(controller.status())

    @app.post("/api/run")
    def run():
        data = body()
        return jsonify(controller.run(data.get("direction"), data.get("speed"), data.get("token")))

    @app.post("/api/heartbeat")
    def heartbeat():
        return jsonify(controller.heartbeat(body().get("token")))

    @app.post("/api/stop")
    def stop():
        body()
        return jsonify(controller.stop())

    return app


def main():
    parser = argparse.ArgumentParser(description="LAN controller for one L298N DC motor")
    parser.add_argument("--simulate", action="store_true", help="no GPIO; test the web page on any computer")
    parser.add_argument("--host", default="0.0.0.0", help="listen address (default: all interfaces)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, terminate)
    hardware = SimulatedHardware() if args.simulate else GPIOHardware()
    controller = None
    try:
        controller = MotorController(hardware)
        mode = "SIMULATION: no physical outputs" if args.simulate else "GPIO: real motor outputs"
        print(f"{mode}. Open http://<Pi-IP>:{args.port} on your trusted LAN.", flush=True)
        serve(create_app(controller), host=args.host, port=args.port, threads=4)
    except KeyboardInterrupt:
        pass
    finally:
        if controller is not None:
            controller.close()
        else:
            hardware.close()


if __name__ == "__main__":
    main()
