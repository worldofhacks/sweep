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
    this.batteryRequests = [];
  }
  sendCustom(sid, command, payload) {
    const request = { sid, command, payload: Buffer.from(payload), sentNs: process.hrtime.bigint() };
    this.requests.push(request);
    this.emit('sent', request);
  }
  sendBatteryQuery() {
    this.batteryRequests.push({ sentNs: process.hrtime.bigint() });
    this.emit('battery_sent');
  }
  batteryReply(type) {
    this.emit('core_response', { type: type || 'battery' });
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

async function withSampler(options, test) {
  if (typeof options === 'function') {
    test = options;
    options = {};
  }
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'sweep-paired-encoder-'));
  const socketPath = path.join(directory, 'encoder.sock');
  const serial = new FakeSerial();
  let clock = 1000;
  const sampler = new PairedEncoderSampler(serial, socketPath, {
    monotonicNs: () => String(clock++),
    pollIntervalMs: options.pollIntervalMs || 100,
    replyTimeoutMs: options.replyTimeoutMs || 20,
  });
  sampler.start();
  if (options.activate !== false) {
    sampler.beginInitialization();
    sampler.activate();
  }
  await wait(5);
  try {
    await test({ sampler, serial, socketPath });
  } finally {
    await new Promise((resolve) => sampler.stop(resolve));
    fs.rmSync(directory, { recursive: true, force: true });
  }
}


async function testBatteryReplyBeforeActivationBlocksTheFirstEncoderPoll() {
  await withSampler({ activate: false, pollIntervalMs: 1 }, async ({ sampler, serial }) => {
    serial.sendBatteryQuery();
    sampler.activate();
    await wait(8);
    assert.strictEqual(serial.batteryRequests.length, 1);
    assert.deepStrictEqual(serial.requests, []);

    serial.batteryReply('battery_new');
    await wait(5);
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [58]);
  });
}

async function testBatteryRequestDuringAnEncoderPairDrainsBeforeTheNextPair() {
  await withSampler({ pollIntervalMs: 1 }, async ({ serial }) => {
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [58]);
    serial.sendBatteryQuery();
    assert.strictEqual(serial.batteryRequests.length, 0);

    serial.reply(0, 10);
    serial.reply(1, 20);
    await wait(2);
    assert.strictEqual(serial.batteryRequests.length, 1);
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [58, 58]);

    serial.batteryReply();
    await wait(5);
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [58, 58, 58]);
  });
}

async function testWormNeckReadBlocksTheNextEncoderPollUntilItsResponse() {
  await withSampler({ activate: false, pollIntervalMs: 1 }, async ({ sampler, serial }) => {
    serial.sendCustom(4, 4, Buffer.from([0x6b, 1]));
    sampler.activate();
    await wait(8);
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [0x6b]);

    serial.reply(4, 0, 0x6b);
    await wait(5);
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [0x6b, 58]);
  });
}

async function testMissedBatteryReplyLeavesThePairedStreamStale() {
  await withSampler({ pollIntervalMs: 1 }, async ({ serial, socketPath }) => {
    const client = await connect(socketPath);
    try {
      const firstPair = nextJson(client);
      serial.reply(0, 10);
      serial.reply(1, 20);
      serial.sendBatteryQuery();
      assert.strictEqual((await firstPair).type, 'sweep_encoder_pair');

      await wait(20);
      assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [58, 58]);
    } finally {
      client.destroy();
    }
  });
}

async function testNativeInitializationDefersPollingButKeepsTheEncoderGate() {
  await withSampler({ activate: false }, async ({ sampler, serial, socketPath }) => {
    const client = await connect(socketPath);
    try {
      serial.sendCustom(0, 4, Buffer.from([58, 2]));
      sampler.beginInitialization();
      await wait(25);
      assert.deepStrictEqual(serial.requests, []);

      const firstPair = nextJson(client);
      sampler.activate();
      await wait(5);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0]);
      serial.reply(0, 10);
      serial.reply(1, 20);
      assert.strictEqual((await firstPair).type, 'sweep_encoder_pair');

      const invalidated = nextJson(client);
      sampler.beginInitialization();
      assert.deepStrictEqual(await invalidated, {
        v: 1,
        type: 'sweep_encoder_unavailable',
        poll_id: null,
        reason: 'serial_reinitializing',
      });
      await wait(25);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0, 1]);

      sampler.activate();
      await wait(5);
      assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0, 1, 0]);
    } finally {
      client.destroy();
    }
  });
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

async function testDeferredVendorReadsKeepTheirSpacingBeforeAnotherEncoderPoll() {
  await withSampler({ pollIntervalMs: 1 }, async ({ sampler, serial }) => {
    assert.deepStrictEqual(serial.requests.map((request) => request.sid), [0]);
    let injected = false;
    serial.on('sent', (request) => {
      if (request.payload[0] !== 59) return;
      setTimeout(() => serial.reply(request.sid, 1, 59), 1);
      if (!injected && request.sid === 0) {
        injected = true;
        setTimeout(() => serial.sendCustom(1, 4, Buffer.from([59, 4])), 1);
      }
    });
    serial.sendCustom(1, 4, Buffer.from([59, 4]));
    await wait(5);
    serial.sendCustom(0, 4, Buffer.from([59, 4]));
    serial.reply(0, 10);
    serial.reply(1, 20);
    await wait(24);
    const vendor = serial.requests.filter((request) => request.payload[0] === 59);
    assert.deepStrictEqual(vendor.map((request) => request.sid), [1, 0, 1]);
    assert(vendor[1].sentNs - vendor[0].sentNs >= 4000000n);
    assert(vendor[2].sentNs - vendor[1].sentNs >= 4000000n);
    const lastVendor = serial.requests.lastIndexOf(vendor[2]);
    const nextEncoder = serial.requests.findIndex((request, index) => index > lastVendor && request.payload[0] === 58);
    assert(nextEncoder > lastVendor);
    assert.strictEqual(sampler.isActive(), true);
  });
}


async function testNativeEncoderReplyBlocksTheNextPollUntilEverySideReplies() {
  await withSampler({ pollIntervalMs: 1 }, async ({ serial }) => {
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [58]);
    serial.reply(0, 10);
    serial.reply(1, 20);

    serial.sendCustom(0, 4, Buffer.from([59, 4]));
    serial.reply(0, 1, 59);
    serial.sendCustom(1, 4, Buffer.from([59, 4]));
    await wait(8);
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [58, 58, 59, 59]);

    serial.reply(1, 1, 59);
    await wait(5);
    assert.deepStrictEqual(serial.requests.map((request) => request.payload[0]), [58, 58, 59, 59, 58]);
  });
}

async function testInitializationAndStopDrainDeferredVendorReadsWithoutBursting() {
  await withSampler({ activate: false }, async ({ sampler, serial }) => {
    sampler.activate();
    await wait(5);
    serial.sendCustom(1, 4, Buffer.from([59, 4]));
    await wait(5);
    serial.sendCustom(0, 4, Buffer.from([59, 4]));
    sampler.beginInitialization();
    await wait(12);
    const initializationVendor = serial.requests.filter((request) => request.payload[0] === 59);
    assert.deepStrictEqual(initializationVendor.map((request) => request.sid), [1, 0]);
    assert(initializationVendor[1].sentNs - initializationVendor[0].sentNs >= 4000000n);

    sampler.activate();
    await wait(5);
    serial.sendCustom(1, 4, Buffer.from([59, 4]));
    await wait(5);
    serial.sendCustom(0, 4, Buffer.from([59, 4]));
    await new Promise((resolve) => sampler.stop(resolve));
    const vendor = serial.requests.filter((request) => request.payload[0] === 59);
    assert.deepStrictEqual(vendor.map((request) => request.sid), [1, 0, 1, 0]);
    assert(vendor[3].sentNs - vendor[2].sentNs >= 4000000n);
  });
}

async function testReinitializationPublishesInvalidationWithoutALatePair() {
  await withSampler(async ({ sampler, serial, socketPath }) => {
    const client = await connect(socketPath);
    let buffer = '';
    const messages = [];
    client.on('data', (chunk) => {
      buffer += chunk.toString('utf8');
      let newline = buffer.indexOf('\n');
      while (newline >= 0) {
        messages.push(JSON.parse(buffer.slice(0, newline)));
        buffer = buffer.slice(newline + 1);
        newline = buffer.indexOf('\n');
      }
    });
    try {
      serial.sendCustom(1, 4, Buffer.from([59, 4]));
      await wait(5);
      serial.sendCustom(0, 4, Buffer.from([59, 4]));
      serial.reply(0, 10);
      serial.reply(1, 20);
      await wait(1);
      sampler.beginInitialization();
      await wait(18);
      assert.deepStrictEqual(messages.map((message) => message.type), [
        'sweep_encoder_pair',
        'sweep_encoder_unavailable',
      ]);
      assert.strictEqual(messages[1].reason, 'serial_reinitializing');
      assert.strictEqual(sampler._qualified, false);
      assert.strictEqual(sampler._timer, null);
    } finally {
      client.destroy();
    }
  });
}

async function testFaultDrainKeepsDeferredVendorReadsSpacedAndNeverReusesLateReply() {
  await withSampler(async ({ sampler, serial, socketPath }) => {
    const client = await connect(socketPath);
    try {
      serial.sendCustom(1, 4, Buffer.from([59, 4]));
      await wait(5);
      serial.sendCustom(0, 4, Buffer.from([59, 4]));
      const unavailable = await nextJson(client);
      assert.deepStrictEqual(unavailable, {
        v: 1,
        type: 'sweep_encoder_unavailable',
        poll_id: 1,
        reason: 'missing_encoder_reply',
      });
      await wait(8);
      const vendor = serial.requests.filter((request) => request.payload[0] === 59);
      assert.deepStrictEqual(vendor.map((request) => request.sid), [1, 0]);
      assert(vendor[1].sentNs - vendor[0].sentNs >= 4000000n);
      serial.reply(0, 88);
      await wait(25);
      assert.strictEqual(sampler.isActive(), false);
      assert.deepStrictEqual(serial.requests.filter((request) => request.payload[0] === 58).map((request) => request.sid), [0]);
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

async function testReaderConnectingAfterBootFailureReceivesTheLatchedFault() {
  await withSampler(async ({ serial, socketPath }) => {
    await wait(30);
    const client = await connect(socketPath);
    try {
      assert.deepStrictEqual(await nextJson(client), {
        v: 1,
        type: 'sweep_encoder_unavailable',
        poll_id: 1,
        reason: 'missing_encoder_reply',
      });
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
  await testBatteryReplyBeforeActivationBlocksTheFirstEncoderPoll();
  await testBatteryRequestDuringAnEncoderPairDrainsBeforeTheNextPair();
  await testWormNeckReadBlocksTheNextEncoderPollUntilItsResponse();
  await testMissedBatteryReplyLeavesThePairedStreamStale();
  await testNativeInitializationDefersPollingButKeepsTheEncoderGate();
  await testDelayedPairFansOut();
  await testOutOfOrderReplyCannotAdvanceThePoll();
  await testDeferredVendorReadsKeepTheirSpacingBeforeAnotherEncoderPoll();
  await testNativeEncoderReplyBlocksTheNextPollUntilEverySideReplies();
  await testInitializationAndStopDrainDeferredVendorReadsWithoutBursting();
  await testReinitializationPublishesInvalidationWithoutALatePair();
  await testFaultDrainKeepsDeferredVendorReadsSpacedAndNeverReusesLateReply();
  await testIncompletePollLatchesUnavailableAndNeverReusesLateReply();
  await testReaderConnectingAfterBootFailureReceivesTheLatchedFault();
  await testExternalDriveEncoderReadsAreSuppressedForSamplerLifetime();
  await testFanoutBoundsClientsAndDropsSlowReaders();
  process.stdout.write('paired encoder sampler tests passed\n');
})().catch((error) => {
  process.stderr.write(error.stack + '\n');
  process.exitCode = 1;
});
