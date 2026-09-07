'use strict';

const assert = require('assert');
const EventEmitter = require('events');
const fs = require('fs');
const os = require('os');
const path = require('path');
const SweepEncoderPlugin = require('./sweep_encoder_plugin');
const EncoderTrace = require('./sweep_encoder_plugin/trace');

class FakeSerial extends EventEmitter {
  constructor() {
    super();
    this.opened = true;
    this.requests = [];
  }

  sendCustom(sid, command, payload) {
    this.requests.push({ sid, command, payload: Buffer.from(payload) });
  }

  sendBatteryQuery() {}
}

async function wait(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function testTraceKeepsFirstEncoderFailureAfterStartupTraffic() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sweep-encoder-trace-'));
  const serial = new FakeSerial();
    const trace = new EncoderTrace(serial, path.join(directory, 'trace.json'));
    trace.installWireObserver();
    try {
      for (let value = 0; value < 100; value += 1) {
        serial.sendCustom(value % 2, 2, Buffer.from([0xf1, 0]));
        serial.emit('servo_response', { sid: value % 2, addr: 20, data: Buffer.alloc(256) });
      }
      serial.sendCustom(0, 4, Buffer.from([58, 2]));
      serial.emit('servo_response', { sid: 0, addr: 58, data: Buffer.from([0x00, 0x40]) });
      for (let value = 0; value < 100; value += 1) {
        const data = Buffer.alloc(4);
        data.writeInt32LE(value, 0);
        serial.emit('servo_response', { sid: value % 2, addr: 59, data });
      }
      const snapshot = trace.snapshot();
      assert.strictEqual(snapshot.first.length, 32);
      assert.strictEqual(snapshot.latest.length, 64);
      assert.strictEqual(snapshot.first[0].type, 'wire_send_custom');
      assert.strictEqual(snapshot.first[0].address, 58);
      assert.strictEqual(snapshot.first[1].uint16_le, 16384);
      assert.strictEqual(snapshot.latest[0].int32_le, 36);
      assert.strictEqual(snapshot.latest[63].int32_le, 99);
    await wait(40);
    assert.deepStrictEqual(JSON.parse(fs.readFileSync(path.join(directory, 'trace.json'), 'utf8')), snapshot);
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function testPluginUsesExistingOwnerAndWaitsForModelStart() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sweep-encoder-plugin-'));
  const serial = new FakeSerial();
  const calls = [];
  const owner = {
    _serial: serial,
    _sweepEncoderSocketPath: path.join(directory, 'encoder.sock'),
    _sweepEncoderTracePath: path.join(directory, 'trace.json'),
    _model: {
      initialize(version) { calls.push(['initialize', version]); return 'initialized'; },
      start() { calls.push(['start']); return 'started'; },
    },
  };

  try {
    const plugin = new SweepEncoderPlugin(owner);
    assert.strictEqual(plugin._sampler._serial, serial);
    assert.deepStrictEqual(serial.requests, []);
    assert.strictEqual(owner._model.initialize('firmware'), 'initialized');
    await wait(10);
    assert.deepStrictEqual(serial.requests, []);
    assert.strictEqual(owner._model.start(), 'started');
    await wait(10);
    assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0]);
    serial.sendCustom(1, 4, Buffer.from([59, 4]));
    serial.emit('servo_response', { sid: 1, addr: 59, data: Buffer.from([0xd6, 0xff, 0xff, 0xff]) });
    serial.emit('servo_response', { sid: 0, addr: 58, data: Buffer.from([0x00, 0x40]) });
    serial.emit('servo_response', { sid: 0, addr: 58, data: Buffer.from([0x0a, 0x00]) });
    await wait(5);
    serial.emit('servo_response', { sid: 1, addr: 58, data: Buffer.from([0x14, 0x00]) });
    await wait(5);
    assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0, 1, 1]);
    assert.deepStrictEqual(calls, [['initialize', 'firmware'], ['start']]);

    const trace = plugin._trace.snapshot();
    assert.strictEqual(trace.type, 'sweep_encoder_trace');
    assert(trace.first.some((entry) => entry.type === 'wire_send_custom' && entry.sid === 0 && entry.address === 58 && entry.bytes[0] === 58 && entry.bytes[1] === 2));
    assert(trace.first.some((entry) => entry.type === 'send_custom_call' && entry.sid === 1 && entry.bytes[0] === 59 && entry.bytes[1] === 4));
    assert(trace.first.some((entry) => entry.type === 'servo_response' && entry.sid === 1 && entry.address === 59 && entry.int32_le === -42));
    assert(trace.first.some((entry) => entry.type === 'servo_response' && entry.sid === 0 && entry.address === 58 && entry.uint16_le === 16384));
    await wait(40);
    assert.deepStrictEqual(JSON.parse(fs.readFileSync(owner._sweepEncoderTracePath, 'utf8')), trace);

    const unavailable = [];
    plugin._sampler._publish = (record) => unavailable.push(record);
    owner._model.initialize('reinitialize');
    assert.deepStrictEqual(unavailable, [{
      v: 1,
      type: 'sweep_encoder_unavailable',
      poll_id: null,
      reason: 'serial_reinitializing',
    }]);
    assert.throws(() => new SweepEncoderPlugin(owner), /already installed/);
    await new Promise((resolve) => plugin._sampler.stop(resolve));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function testPluginFreezesEncoderTraceWhenSamplerFails() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sweep-encoder-fault-trace-'));
  const serial = new FakeSerial();
  const owner = {
    _serial: serial,
    _sweepEncoderSocketPath: path.join(directory, 'encoder.sock'),
    _sweepEncoderTracePath: path.join(directory, 'trace.json'),
    _model: { initialize() {}, start() {} },
  };

  try {
    const plugin = new SweepEncoderPlugin(owner);
    for (let poll = 1; poll <= 34; poll += 1) {
      for (const sid of [0, 1]) {
        plugin._sampler._directSendCustom(sid, 4, Buffer.from([58, 2]));
        serial.emit('servo_response', { sid, addr: 58, data: Buffer.from([poll, 0]) });
      }
    }
    plugin._sampler._active = { id: 35, side: 1, timeout: null };
    plugin._sampler._fail('missing_encoder_reply');
    serial.emit('servo_response', { sid: 1, addr: 58, data: Buffer.from([0x34, 0x12]) });
    for (let value = 0; value < 100; value += 1) {
      serial.sendCustom(value % 2, 4, Buffer.from([59, 4]));
    }

    const fault = plugin._trace.snapshot().fault;
    assert.strictEqual(fault.reason, 'missing_encoder_reply');
    assert.strictEqual(fault.poll_id, 35);
    assert.strictEqual(fault.pending_side, 1);
    assert.strictEqual(fault.context.length, 64);
    assert(fault.context.some((entry) => entry.type === 'wire_send_custom' && entry.address === 58));
    assert.strictEqual(fault.after_fault.length, 64);
    assert(fault.after_fault.some((entry) => entry.type === 'servo_response' && entry.address === 58 && entry.uint16_le === 0x1234));
    await new Promise((resolve) => plugin._sampler.stop(resolve));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function testPluginRecordsActualSamplerTimeout() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sweep-encoder-timeout-trace-'));
  const serial = new FakeSerial();
  const owner = {
    _serial: serial,
    _sweepEncoderSocketPath: path.join(directory, 'encoder.sock'),
    _sweepEncoderTracePath: path.join(directory, 'trace.json'),
    _model: { initialize() {}, start() {} },
  };

  try {
    const plugin = new SweepEncoderPlugin(owner);
    owner._model.start();
    await wait(130);
    const fault = plugin._trace.snapshot().fault;
    assert.strictEqual(fault.reason, 'missing_encoder_reply');
    assert.strictEqual(fault.poll_id, 1);
    assert.strictEqual(fault.pending_side, 0);
    assert(fault.context.some((entry) => entry.type === 'wire_send_custom' && entry.sid === 0 && entry.address === 58));
    await new Promise((resolve) => plugin._sampler.stop(resolve));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

Promise.resolve()
  .then(testTraceKeepsFirstEncoderFailureAfterStartupTraffic)
  .then(testPluginUsesExistingOwnerAndWaitsForModelStart)
  .then(testPluginFreezesEncoderTraceWhenSamplerFails)
  .then(testPluginRecordsActualSamplerTimeout)
  .then(() => process.stdout.write('sweep encoder plugin tests passed\n'))
  .catch((error) => {
    process.stderr.write(error.stack + '\n');
    process.exitCode = 1;
  });
