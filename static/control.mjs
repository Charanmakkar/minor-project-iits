// Shared pure functions and request coordinator are exported for deterministic tests.
export function mixDrive(speed, steering) {
  const turn = steering / 100;
  return {left_pwm: speed * (1 + Math.min(turn, 0)), right_pwm: speed * (1 - Math.max(turn, 0))};
}

export function virtualSpeed(speed) {
  // This is a user-selected display scale, not a physical speed calculation.
  return speed;
}

export function steeringLabel(steering) {
  return steering === 0 ? "Straight" : `${steering < 0 ? "Left" : "Right"} ${Math.abs(steering)}%`;
}

export class DriveSession {
  constructor(request, {onChange = () => {}, hidden = () => false,
    setTimer = setTimeout, clearTimer = clearTimeout, now = () => Date.now(), throttle = 80} = {}) {
    Object.assign(this, {request, onChange, hidden, setTimer, clearTimer, now, throttle});
    this.state = null;
    this.desired = {speed: 0, steering: 0};
    this.token = null;
    this.active = false;
    this.inFlight = false;
    this.pendingStops = 0;
    this.pending = null;
    this.timer = null;
    this.epoch = 0;
    this.revision = 0;
    this.lastSent = -Infinity;
    this.heartbeatBusy = false;
    this.statusBusy = false;
    this.error = "";
  }

  notify() { this.onChange(this); }

  canStart() {
    return !this.hidden() && !this.active && !this.inFlight && !this.pendingStops &&
      this.desired.speed > 0 && this.state?.direction === "stopped" &&
      !this.state.fault && !this.state.requires_stop;
  }

  clearQueue() {
    this.pending = null;
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
  }

  desiredChanged(speed, steering) {
    this.desired = {speed, steering};
    if (this.active) {
      if (speed === 0) { void this.stop(); return; }
      this.pending = {...this.desired};
      this.schedule();
    }
    this.notify();
  }

  start() {
    if (!this.canStart()) return;
    this.epoch++;
    this.active = true;
    this.error = "";
    this.pending = {...this.desired};
    this.schedule(true);
    this.notify();
  }

  schedule(immediate = false) {
    if (!this.active || !this.pending || this.inFlight || this.pendingStops || this.hidden() || this.timer !== null) return;
    const delay = immediate ? 0 : Math.max(0, this.throttle - (this.now() - this.lastSent));
    this.timer = this.setTimer(() => { this.timer = null; void this.sendLatest(); }, delay);
  }

  accept(next) {
    this.state = next;
    if (next.direction === "stopped" || next.fault) {
      this.token = null;
      this.active = false;
      this.clearQueue();
    }
    this.notify();
  }

  lose(error) {
    this.epoch++;
    this.active = false;
    this.token = null;
    this.state = null;
    this.clearQueue();
    this.error = error.message || "Connection lost.";
    this.notify();
  }

  async sendLatest() {
    if (!this.active || !this.pending || this.inFlight || this.pendingStops || this.hidden()) return;
    const command = this.pending, epoch = this.epoch;
    this.pending = null;
    this.inFlight = true;
    this.revision++;
    this.lastSent = this.now();
    this.notify();
    try {
      const next = await this.request("/api/run", {...command, token: this.token});
      if (epoch !== this.epoch || this.hidden()) {
        // Stop may have reached the server before this run; stop again after it.
        await this.request("/api/stop", {}, {keepalive: this.hidden()});
        return;
      }
      this.token = next.token || null;
      this.error = "";
      this.accept(next);
    } catch (error) {
      if (epoch === this.epoch) this.lose(error);
      // A lost run response does not prove the command failed to reach the GPIO.
      try { await this.request("/api/stop", {}, {keepalive: this.hidden()}); } catch (_) {}
    } finally {
      this.inFlight = false;
      this.schedule();
      this.notify();
    }
  }

  async stop({keepalive = false} = {}) {
    const epoch = ++this.epoch;
    this.revision++;
    this.active = false;
    this.token = null;
    this.clearQueue();
    this.pendingStops++;
    this.notify();
    try {
      const next = await this.request("/api/stop", {}, {keepalive});
      if (epoch === this.epoch) { this.error = ""; this.accept(next); }
    } catch (error) {
      if (epoch === this.epoch) this.lose(error);
    } finally { this.pendingStops--; this.notify(); }
  }

  async heartbeat() {
    if (!this.token || !this.active || this.hidden() || this.heartbeatBusy || this.inFlight || this.pendingStops) return;
    this.heartbeatBusy = true;
    const token = this.token, epoch = this.epoch, revision = this.revision;
    try {
      const next = await this.request("/api/heartbeat", {token});
      if (token === this.token && epoch === this.epoch && revision === this.revision) this.accept(next);
    } catch (error) {
      if (token === this.token && epoch === this.epoch && revision === this.revision) this.lose(error);
    } finally { this.heartbeatBusy = false; }
  }

  async readStatus() {
    if (this.active || this.inFlight || this.pendingStops || this.hidden() || this.statusBusy) return;
    this.statusBusy = true;
    const epoch = this.epoch, revision = this.revision;
    try {
      const next = await this.request("/api/status");
      if (epoch === this.epoch && revision === this.revision && !this.active && !this.inFlight && !this.pendingStops) this.accept(next);
    } catch (error) {
      if (epoch === this.epoch && revision === this.revision && !this.active && !this.inFlight && !this.pendingStops) this.lose(error);
    } finally { this.statusBusy = false; }
  }

  leave() {
    if (this.active || this.inFlight) void this.stop({keepalive: true});
    else { this.clearQueue(); this.notify(); }
  }
}

export async function browserRequest(path, data, {keepalive = false} = {}) {
  const abort = new AbortController();
  const timer = setTimeout(() => abort.abort(), 1200);
  try {
    const options = {cache: "no-store", signal: abort.signal, keepalive};
    if (data !== undefined) Object.assign(options, {method: "POST",
      headers: {"Content-Type": "application/json", "X-Motor-Control": "1"}, body: JSON.stringify(data)});
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Request failed.");
    return result;
  } finally { clearTimeout(timer); }
}

export function mountControls(document) {
  const $ = id => document.getElementById(id);
  const percent = value => `${Number(value.toFixed(1))}%`;
  function render(session) {
    const {desired, state, active, pendingStops, inFlight} = session;
    const preview = mixDrive(desired.speed, desired.steering);
    $("speed-label").textContent = percent(desired.speed);
    $("virtual-speed").textContent = virtualSpeed(desired.speed);
    $("steering-label").textContent = steeringLabel(desired.steering);
    $("preview-pwm").textContent = `Preview: left ${percent(preview.left_pwm)} · right ${percent(preview.right_pwm)} PWM`;
    $("preview-mode").textContent = pendingStops ? "Stopping. Further slider changes are preview only." : active ?
      (inFlight || session.pending ? "Applying the latest slider command…" : "Live control. Slider changes apply while this page stays visible.") :
      "Preview only. Choose a speed and press Start to apply it.";
    $("start").disabled = !session.canStart();
    $("start").textContent = active ? "Drive active" : "Start drive";
    $("error").textContent = session.error;
    const known = state && !state.fault;
    $("state").textContent = pendingStops ? "Requesting stop…" : !state ?
      (session.error ? "Control disconnected" : "Connecting…") : state.fault ? "GPIO fault — disconnect motor power" :
      state.direction === "stopped" ? "Drive off" : active ? "Forward drive" : "Drive running — press Stop to take control";
    $("reason").textContent = state?.reason || (session.error ?
      "Output unknown. Heartbeats have stopped; use the power cut-off if drive continues." : "Reading controller status.");
    for (const side of ["left", "right"]) {
      const value = known ? state[`${side}_pwm`] : 0;
      $(`${side}-value`).textContent = known ? `${percent(value)} PWM` : "Unknown";
      $(`${side}-bar`).value = value;
      $(`${side}-bar`).setAttribute("aria-valuetext", known ? `${percent(value)} PWM` : "Unknown");
      $(`${side}-wheel`).setAttribute("fill", known && value > 0 ? "#245dcc" : "#8799aa");
    }
    const turning = known && state.direction !== "stopped";
    $("turn-path").setAttribute("visibility", turning ? "visible" : "hidden");
    if (turning) $("turn-path").setAttribute("d", `M240 143 Q240 93 ${240 + state.steering * 1.35} 42`);
    $("applied-turn").textContent = !known ? "Unknown" : turning ? steeringLabel(state.steering) : "Drive off";
  }
  const session = new DriveSession(browserRequest, {onChange: render, hidden: () => document.hidden});
  const update = () => session.desiredChanged(Number($("speed").value), Number($("steering").value));
  $("speed").addEventListener("input", update);
  $("steering").addEventListener("input", update);
  $("start").addEventListener("click", () => session.start());
  $("stop").addEventListener("click", () => { void session.stop(); });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) session.leave();
    else { session.notify(); void session.readStatus(); }
  });
  globalThis.addEventListener("pagehide", () => session.leave());
  setInterval(() => { void session.heartbeat(); }, 300);
  setInterval(() => { void session.readStatus(); }, 1000);
  render(session);
  void session.readStatus();
  return session;
}

if (typeof document !== "undefined") mountControls(document);
