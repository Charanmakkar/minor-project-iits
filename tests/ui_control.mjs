import assert from "node:assert/strict";
import {pathToFileURL} from "node:url";
import {DriveSession, mixDrive, virtualSpeed, steeringLabel} from "../static/control.mjs";

const stopped = () => ({direction: "stopped", speed: 0, steering: 0, left_pwm: 0, right_pwm: 0,
  reason: "Drive off", simulated: true, watchdog_seconds: 2, requires_stop: false, fault: false});
const running = (speed, steering = 0, token = "owner") => ({...stopped(), direction: "forward",
  speed, steering, ...mixDrive(speed, steering), token});
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

function harness() {
  const calls = [], timers = new Map();
  let timerId = 0, now = 0, hidden = false;
  const session = new DriveSession((path, data, options) => new Promise((resolve, reject) => {
    calls.push({path, data, options, resolve, reject});
  }), {
    setTimer: (callback, delay) => { const id = ++timerId; timers.set(id, {callback, at: now + delay}); return id; },
    clearTimer: id => timers.delete(id), now: () => now, hidden: () => hidden,
  });
  session.accept(stopped());
  return {session, calls, timers,
    hide: value => { hidden = value; },
    tick: async () => {
      const entry = [...timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (entry) { timers.delete(entry[0]); now = entry[1].at; entry[1].callback(); }
      await settle();
    },
    reply: async (index, value) => { calls[index].resolve(value); await settle(); },
    fail: async (index, message = "Network lost") => { calls[index].reject(new Error(message)); await settle(); },
    begin: async function (speed = 30, steering = 0) {
      session.desiredChanged(speed, steering); session.start(); await this.tick();
      await this.reply(calls.length - 1, running(speed, steering));
    },
  };
}

export async function runUiTests() {
  const passed = [];
  async function test(name, body) { await body(); passed.push(name); }

  await test("forward mixer slows only the inside wheel; display scale is explicitly 1:1", () => {
    assert.deepEqual(mixDrive(60, 0), {left_pwm: 60, right_pwm: 60});
    assert.deepEqual(mixDrive(60, -50), {left_pwm: 30, right_pwm: 60});
    assert.deepEqual(mixDrive(60, 100), {left_pwm: 60, right_pwm: 0});
    assert.deepEqual(mixDrive(100, -100), {left_pwm: 0, right_pwm: 100});
    assert.deepEqual(mixDrive(0, -100), {left_pwm: 0, right_pwm: 0});
    assert.equal(virtualSpeed(0), 0); assert.equal(virtualSpeed(47), 47); assert.equal(virtualSpeed(100), 100);
    assert.equal(steeringLabel(-30), "Left 30%"); assert.equal(steeringLabel(30), "Right 30%");
  });

  await test("sliders preview while stopped; applied state waits for the server", async () => {
    const h = harness();
    assert.equal(h.session.canStart(), false);
    h.session.desiredChanged(40, -20); await h.tick();
    assert.equal(h.calls.length, 0);
    assert.equal(h.session.state.left_pwm, 0);
    h.session.start(); await h.tick();
    assert.deepEqual(h.calls[0].data, {speed: 40, steering: -20, token: null});
    assert.equal(h.session.state.left_pwm, 0);
    await h.reply(0, running(40, -20));
    assert.equal(h.session.state.left_pwm, 32);
  });

  await test("rapid input is coalesced and the latest value flushes without overlapping runs", async () => {
    const h = harness(); h.session.desiredChanged(20, 0); h.session.start(); await h.tick();
    h.session.desiredChanged(40, -25); h.session.desiredChanged(80, 75); await h.tick();
    assert.equal(h.calls.length, 1);
    await h.reply(0, running(20)); await h.tick();
    assert.equal(h.calls.length, 2);
    assert.deepEqual(h.calls[1].data, {speed: 80, steering: 75, token: "owner"});
    await h.reply(1, running(80, 75));
    assert.equal(h.session.state.right_pwm, 20);
    assert.equal(h.session.pending, null);
  });

  await test("Stop invalidates queued updates and a late initial run is stopped again", async () => {
    const h = harness(); h.session.desiredChanged(20, 0); h.session.start(); await h.tick();
    h.session.desiredChanged(70, -40);
    const stopping = h.session.stop();
    assert.equal(h.calls[1].path, "/api/stop");
    await h.reply(1, stopped());
    assert.equal(h.session.canStart(), false); // Initial run has not settled yet.
    await h.reply(0, running(20));
    assert.equal(h.calls[2].path, "/api/stop");
    assert.equal(h.session.token, null);
    await h.reply(2, stopped()); await stopping; await h.tick();
    assert.equal(h.calls.filter(call => call.path === "/api/run").length, 1);
    assert.equal(h.session.state.direction, "stopped");
    assert.equal(h.session.canStart(), true);
  });

  await test("new Start is blocked until every pending Stop settles", async () => {
    const h = harness(); h.session.desiredChanged(30, 0);
    const first = h.session.stop(), second = h.session.stop();
    await h.reply(1, stopped());
    assert.equal(h.session.canStart(), false);
    h.session.start(); await h.tick(); assert.equal(h.calls.length, 2);
    await h.reply(0, {...stopped(), reason: "obsolete response"});
    await Promise.all([first, second]);
    assert.equal(h.session.state.reason, "Drive off");
    assert.equal(h.session.canStart(), true);
  });

  await test("zero speed stops immediately; raising it again does not restart", async () => {
    const h = harness(); await h.begin();
    h.session.desiredChanged(70, 20); // Queued, not yet sent.
    h.session.desiredChanged(0, 20);
    h.session.desiredChanged(50, 20);
    assert.equal(h.calls[1].path, "/api/stop");
    await h.reply(1, stopped()); await h.tick();
    assert.equal(h.calls.length, 2);
    assert.equal(h.session.active, false); assert.equal(h.session.token, null);
  });

  await test("a stale status response cannot clear a new run's token", async () => {
    const h = harness(); const reading = h.session.readStatus();
    h.session.desiredChanged(30, 0); h.session.start(); await h.tick();
    await h.reply(1, running(30)); await h.reply(0, stopped()); await reading;
    assert.equal(h.session.token, "owner"); assert.equal(h.session.state.speed, 30);
  });

  await test("an old heartbeat snapshot cannot overwrite a newer applied speed", async () => {
    const h = harness(); await h.begin(30);
    const heartbeat = h.session.heartbeat();
    h.session.desiredChanged(80, -50); await h.tick();
    await h.reply(2, running(80, -50));
    await h.reply(1, running(30)); await heartbeat;
    assert.equal(h.session.state.speed, 80); assert.equal(h.session.state.steering, -50);
  });

  await test("heartbeat loss invalidates queued slider updates and does not restart on reconnect", async () => {
    const h = harness(); await h.begin();
    const heartbeat = h.session.heartbeat();
    h.session.desiredChanged(70, 25);
    await h.fail(1); await heartbeat; await h.tick();
    assert.equal(h.calls.length, 2); assert.equal(h.session.state, null);
    h.session.desiredChanged(90, 50);
    const reading = h.session.readStatus(); await h.reply(2, stopped()); await reading; await h.tick();
    assert.equal(h.calls.length, 3); assert.equal(h.session.active, false);
  });

  await test("hiding the page cancels an unsent Start and sends a keepalive Stop", async () => {
    const h = harness(); h.session.desiredChanged(30, 0); h.session.start();
    h.hide(true); h.session.leave(); await h.tick();
    assert.equal(h.calls.length, 1); assert.equal(h.calls[0].path, "/api/stop");
    assert.deepEqual(h.calls[0].options, {keepalive: true});
    await h.reply(0, stopped()); h.hide(false); await h.tick();
    assert.equal(h.calls.length, 1); assert.equal(h.session.active, false);
  });

  await test("an ambiguous run failure requests Stop and discards all pending motion", async () => {
    const h = harness(); h.session.desiredChanged(30, 0); h.session.start(); await h.tick();
    h.session.desiredChanged(90, -20);
    await h.fail(0);
    assert.equal(h.calls[1].path, "/api/stop");
    await h.reply(1, stopped()); await h.tick();
    assert.equal(h.calls.length, 2); assert.equal(h.session.active, false);
    assert.equal(h.session.token, null); assert.equal(h.session.pending, null);
  });

  return {passed: passed.length, tests: passed};
}

// Run directly with `node tests/ui_control.mjs`, or import runUiTests in a harness.
if (typeof process !== "undefined" && process.argv?.[1] &&
    import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    const result = await runUiTests();
    for (const name of result.tests) console.log(`PASS: ${name}`);
    console.log(`\n${result.passed} UI tests passed.`);
  } catch (error) {
    console.error("UI tests failed:", error.message);
    throw error;
  }
}
