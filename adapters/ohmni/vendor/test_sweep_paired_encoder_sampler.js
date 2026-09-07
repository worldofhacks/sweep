'use strict';

const assert = require('assert');
const EventEmitter = require('events');
const fs = require('fs');
const net = require('net');
const os = require('os');
const path = require('path');
const PairedEncoderSampler = require('./sweep_paired_encoder_sampler');

class FakeSerial extends EventEmitter {
  constructor() {
    super();
    this.opened = true;
    this.requests = [];
  }
  sendCustom(sid, command, payload) {
    this.requests.push({ sid, command, payload: Buffer.from(payload) });
  }
  reply(sid, value, address) {
    const data = Buffer.alloc(2);
    data.writeUInt16LE(value, 0);
    this.emit('servo_response', { sid, addr: address === undefined ? 58 : address, data });
  }
}

function wait(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function connect(socketPath) {
  return new Promise((resolve, reject) => {
    const client = net.createConnection(socketPath);
    client.once('connect', () => resolve(client));
    client.once('error', reject);
  });
}

function nextJson(client) {
  return new Promise((resolve, reject) => {
    let text = '';
    const timeout = setTimeout(() => reject(new Error('timed out waiting for sampler output')), 250);
    client.on('data', function onData(chunk) {
      text += chunk.toString('utf8');
      const newline = text.indexOf('\n');
      if (newline < 0) return;
      clearTimeout(timeout);
      client.removeListener('data', onData);
      resolve(JSON.parse(text.slice(0, newline)));
    });
  });
}

async function withSampler(test) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sweep-paired-encoder-'));
  const socketPath = path.join(directory, 'encoder.sock');
  const serial = new FakeSerial();
  let clock = 1000;
  const sampler = new PairedEncoderSampler(serial, socketPath, {
    monotonicNs: () => String(clock++),
    pollIntervalMs: 100,
    replyTimeoutMs: 20,
  });
  sampler.start();
  await wait(5);
  try {
    await test({ sampler, serial, socketPath });
  } finally {
    await new Promise((resolve) => sampler.stop(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  }
}

async function testDelayedPairFansOut() {
  await withSampler(async ({ serial, socketPath }) => {
    const first = await connect(socketPath);
    const second = await connect(socketPath);
    try {
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0]);
      const one = nextJson(first);
      const two = nextJson(second);
      await wait(8);
      serial.sendCustom(1, 4, Buffer.from([59, 4]));
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0]);
      serial.reply(0, 12);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0, 1]);
      await wait(8);
      serial.reply(1, 34);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0, 1, 1]);
      assert.deepStrictEqual(serial.requests[2].payload, Buffer.from([59, 4]));
      const [firstEvent, secondEvent] = await Promise.all([one, two]);
      assert.deepStrictEqual(firstEvent, secondEvent);
      assert.deepStrictEqual(firstEvent, {
        v: 1,
        type: 'sweep_encoder_pair',
        poll_id: 1,
        left: 12,
        right: 34,
        left_receipt_ns: '1001',
        right_receipt_ns: '1002',
      });
    } finally {
      first.destroy();
      second.destroy();
    }
  });
}

async function testOutOfOrderReplyCannotAdvanceThePoll() {
  await withSampler(async ({ serial, socketPath }) => {
    const client = await connect(socketPath);
    try {
      const received = nextJson(client);
      serial.reply(1, 99);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0]);
      serial.reply(0, 10);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0, 1]);
      serial.reply(1, 20);
      assert.deepStrictEqual(await received, {
        v: 1,
        type: 'sweep_encoder_pair',
        poll_id: 1,
        left: 10,
        right: 20,
        left_receipt_ns: '1001',
        right_receipt_ns: '1002',
      });
    } finally {
      client.destroy();
    }
  });
}

async function testIncompletePollLatchesUnavailableAndNeverReusesLateReply() {
  await withSampler(async ({ serial, socketPath }) => {
    const client = await connect(socketPath);
    try {
      const unavailable = nextJson(client);
      await wait(30);
      assert.deepStrictEqual(await unavailable, {
        v: 1,
        type: 'sweep_encoder_unavailable',
        poll_id: 1,
        reason: 'missing_encoder_reply',
      });
      serial.reply(0, 88);
      await wait(30);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0]);
    } finally {
      client.destroy();
    }
  });
}

async function testExternalDriveEncoderReadsAreSuppressedForSamplerLifetime() {
  await withSampler(async ({ serial, socketPath }) => {
    const client = await connect(socketPath);
    try {
      const received = nextJson(client);
      serial.sendCustom(0, 4, Buffer.from([58, 2]));
      serial.sendCustom(1, 4, Buffer.from([58, 4]));
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0]);
      serial.reply(0, 10);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0, 1]);
      serial.sendCustom(1, 4, Buffer.from([58, 2]));
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0, 1]);
      serial.reply(1, 20);
      assert.strictEqual((await received).type, 'sweep_encoder_pair');
    } finally {
      client.destroy();
    }
  });
}

async function testFanoutBoundsClientsAndDropsSlowReaders() {
  await withSampler(async ({ sampler, socketPath }) => {
    const clients = await Promise.all(Array.from({ length: 5 }, () => connect(socketPath)));
    try {
      await wait(5);
      assert.strictEqual(sampler._clients.length, 4);
      const slow = {
        destroyed: false,
        writableLength: 65536,
        destroy() { this.destroyed = true; },
        write() { throw new Error('slow reader was written'); },
      };
      sampler._clients = [slow];
      sampler._publish({ v: 1, type: 'sweep_encoder_pair' });
      assert.strictEqual(slow.destroyed, true);
    } finally {
      clients.forEach((client) => client.destroy());
    }
  });
}

(async () => {
  await testDelayedPairFansOut();
  await testOutOfOrderReplyCannotAdvanceThePoll();
  await testIncompletePollLatchesUnavailableAndNeverReusesLateReply();
  await testExternalDriveEncoderReadsAreSuppressedForSamplerLifetime();
  await testFanoutBoundsClientsAndDropsSlowReaders();
  process.stdout.write('paired encoder sampler tests passed\n');
})().catch((error) => {
  process.stderr.write(error.stack + '\n');
  process.exitCode = 1;
});
