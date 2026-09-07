'use strict';

const assert = require('assert');
const EventEmitter = require('events');
const fs = require('fs');
const os = require('os');
const path = require('path');
const SweepEncoderPlugin = require('./sweep_encoder_plugin');

class FakeSerial extends EventEmitter {
  constructor() {
    super();
    this.opened = true;
    this.requests = [];
  }

  sendCustom(sid, command, payload) {
    this.requests.push({ sid, command, payload: Buffer.from(payload) });
  }
}

async function wait(ms) {
  await new Promise((resolve) => setTimeout(resolve, ms));
}

async function testPluginUsesExistingOwnerAndWaitsForModelStart() {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sweep-encoder-plugin-'));
  const serial = new FakeSerial();
  const calls = [];
  const owner = {
    _serial: serial,
    _sweepEncoderSocketPath: path.join(directory, 'encoder.sock'),
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
    assert.deepStrictEqual(calls, [['initialize', 'firmware'], ['start']]);

    const unavailable = [];
    plugin._sampler._publish = (record) => unavailable.push(record);
    owner._model.initialize('reinitialize');
    assert.deepStrictEqual(unavailable, [{
      v: 1,
      type: 'sweep_encoder_unavailable',
      poll_id: 1,
      reason: 'serial_reinitializing',
    }]);
    assert.throws(() => new SweepEncoderPlugin(owner), /already installed/);
    await new Promise((resolve) => plugin._sampler.stop(resolve));
  } finally {
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

testPluginUsesExistingOwnerAndWaitsForModelStart()
  .then(() => process.stdout.write('sweep encoder plugin tests passed\n'))
  .catch((error) => {
    process.stderr.write(error.stack + '\n');
    process.exitCode = 1;
  });
