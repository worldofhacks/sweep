'use strict';

const fs = require('fs');
const net = require('net');

const ADDRESS = 58;
const BYTES = 2;
const READ_COMMAND = 4;
const POLL_INTERVAL_MS = 100;
const REPLY_TIMEOUT_MS = 100;
const MAX_CLIENTS = 4;
const MAX_CLIENT_BUFFER_BYTES = 64 * 1024;

function monotonicNs() {
  const now = process.hrtime();
  return String(now[0] * 1000000000 + now[1]);
}

function isPosition(value) {
  return typeof value === 'number' && value >= 0 && value < 16384 && Math.floor(value) === value;
}

function isDriveEncoderRequest(sid, command, payload) {
  return (sid === 0 || sid === 1) && command === READ_COMMAND && payload &&
    payload.length >= 1 && payload[0] === ADDRESS;
}

function PairedEncoderSampler(serial, socketPath, options) {
  options = options || {};
  this._serial = serial;
  this._directSendCustom = serial.sendCustom.bind(serial);
  this._deferredRequests = [];
  this._socketPath = socketPath;
  this._clock = options.monotonicNs || monotonicNs;
  this._setTimeout = options.setTimeout || setTimeout;
  this._clearTimeout = options.clearTimeout || clearTimeout;
  this._pollIntervalMs = options.pollIntervalMs || POLL_INTERVAL_MS;
  this._replyTimeoutMs = options.replyTimeoutMs || REPLY_TIMEOUT_MS;
  this._pollId = 0;
  this._active = null;
  this._failed = false;
  this._timer = null;
  this._server = null;
  this._clients = [];
  this._stopped = true;
  this._onServoResponse = this._handleServoResponse.bind(this);
  this._onSerialClose = this._fail.bind(this, 'serial_disconnected');
  this._onSerialOpen = this._resumeAfterOpen.bind(this);
  this._gatedSendCustom = this._queueOrSend.bind(this);
}

PairedEncoderSampler.prototype.isActive = function () {
  return this._active !== null;
};

PairedEncoderSampler.prototype.start = function () {
  if (!this._stopped) return;
  this._stopped = false;
  this._serial.sendCustom = this._gatedSendCustom;
  this._createServer();
  this._serial.on('servo_response', this._onServoResponse);
  this._serial.on('close', this._onSerialClose);
  this._serial.on('open', this._onSerialOpen);
  this._schedule(0);
};

PairedEncoderSampler.prototype.stop = function (done) {
  if (this._stopped) {
    if (done) done();
    return;
  }
  this._stopped = true;
  this._abortActive();
  if (this._timer) {
    this._clearTimeout(this._timer);
    this._timer = null;
  }
  this._releaseBus();
  this._serial.sendCustom = this._directSendCustom;
  this._serial.removeListener('servo_response', this._onServoResponse);
  this._serial.removeListener('close', this._onSerialClose);
  this._serial.removeListener('open', this._onSerialOpen);
  const server = this._server;
  this._server = null;
  this._clients.forEach(function (client) { client.destroy(); });
  this._clients = [];
  if (!server) {
    this._unlinkSocket();
    if (done) done();
    return;
  }
  server.close(() => {
    this._unlinkSocket();
    if (done) done();
  });
};

PairedEncoderSampler.prototype._createServer = function () {
  if (fs.existsSync(this._socketPath)) {
    const stat = fs.lstatSync(this._socketPath);
    if (!stat.isSocket()) throw new Error('paired encoder path is not a socket');
    fs.unlinkSync(this._socketPath);
  }
  this._server = net.createServer((client) => {
    if (this._clients.length >= MAX_CLIENTS) {
      client.destroy();
      return;
    }
    this._clients.push(client);
    client.on('error', () => this._removeClient(client));
    client.on('close', () => this._removeClient(client));
  });
  this._server.listen(this._socketPath, () => fs.chmodSync(this._socketPath, 0o600));
};

PairedEncoderSampler.prototype._unlinkSocket = function () {
  try {
    if (fs.existsSync(this._socketPath) && fs.lstatSync(this._socketPath).isSocket()) {
      fs.unlinkSync(this._socketPath);
    }
  } catch (_error) {
    // A stopped sampler must never remove a non-socket path.
  }
};

PairedEncoderSampler.prototype._removeClient = function (client) {
  const index = this._clients.indexOf(client);
  if (index >= 0) this._clients.splice(index, 1);
};

PairedEncoderSampler.prototype._schedule = function (delayMs) {
  if (this._stopped || this._failed || this._timer || this._active) return;
  this._timer = this._setTimeout(() => {
    this._timer = null;
    this._beginPoll();
  }, delayMs);
};

PairedEncoderSampler.prototype._resumeAfterOpen = function () {
  if (!this._failed) this._schedule(0);
};

PairedEncoderSampler.prototype._beginPoll = function () {
  if (this._stopped || this._failed || this._active) return;
  if (!this._serial.opened) {
    this._schedule(this._pollIntervalMs);
    return;
  }
  const poll = {
    id: ++this._pollId,
    startedNs: this._clock(),
    side: 0,
    left: null,
    right: null,
    leftReceiptNs: null,
    rightReceiptNs: null,
    timeout: null,
  };
  this._active = poll;
  this._requestSide(poll, 0);
};

PairedEncoderSampler.prototype._queueOrSend = function (sid, command, payload) {
  if (isDriveEncoderRequest(sid, command, payload)) return;
  if (this._active) {
    this._deferredRequests.push({ sid: sid, command: command, payload: Buffer.from(payload) });
    return;
  }
  this._directSendCustom(sid, command, payload);
};

PairedEncoderSampler.prototype._releaseBus = function () {
  const deferred = this._deferredRequests;
  this._deferredRequests = [];
  deferred.forEach((request) => this._directSendCustom(request.sid, request.command, request.payload));
};

PairedEncoderSampler.prototype._requestSide = function (poll, side) {
  if (this._active !== poll || this._stopped) return;
  poll.side = side;
  poll.timeout = this._setTimeout(() => this._expire(poll), this._replyTimeoutMs);
  this._directSendCustom(side, READ_COMMAND, Buffer.from([ADDRESS, BYTES]));
};

PairedEncoderSampler.prototype._handleServoResponse = function (message) {
  const poll = this._active;
  if (!poll || message.sid !== poll.side || message.addr !== ADDRESS || !message.data || message.data.length < 2) {
    return;
  }
  const value = message.data.readUInt16LE(0);
  if (!isPosition(value)) return;
  this._clearTimeout(poll.timeout);
  poll.timeout = null;
  const receipt = this._clock();
  if (poll.side === 0) {
    poll.left = value;
    poll.leftReceiptNs = receipt;
    this._requestSide(poll, 1);
    return;
  }
  poll.right = value;
  poll.rightReceiptNs = receipt;
  this._complete(poll);
};

PairedEncoderSampler.prototype._expire = function (poll) {
  if (this._active !== poll) return;
  this._fail('missing_encoder_reply');
};

PairedEncoderSampler.prototype._fail = function (reason) {
  if (this._failed || this._stopped) return;
  const poll = this._active;
  if (poll && poll.timeout) this._clearTimeout(poll.timeout);
  this._active = null;
  this._releaseBus();
  this._failed = true;
  this._publish({
    v: 1,
    type: 'sweep_encoder_unavailable',
    poll_id: poll ? poll.id : null,
    reason: reason,
  });
};

PairedEncoderSampler.prototype._abortActive = function () {
  const poll = this._active;
  if (!poll) return;
  if (poll.timeout) this._clearTimeout(poll.timeout);
  this._active = null;
};

PairedEncoderSampler.prototype._publish = function (payload) {
  const message = JSON.stringify(payload) + '\n';
  this._clients.slice().forEach((client) => {
    if (client.destroyed) return;
    if (client.writableLength + Buffer.byteLength(message) > MAX_CLIENT_BUFFER_BYTES) {
      client.destroy();
      return;
    }
    client.write(message);
  });
};

PairedEncoderSampler.prototype._complete = function (poll) {
  if (this._active !== poll || poll.left === null || poll.right === null) return;
  this._active = null;
  this._releaseBus();
  this._publish({
    v: 1,
    type: 'sweep_encoder_pair',
    poll_id: poll.id,
    left: poll.left,
    right: poll.right,
    left_receipt_ns: poll.leftReceiptNs,
    right_receipt_ns: poll.rightReceiptNs,
  });
  this._schedule(this._pollIntervalMs);
};

module.exports = PairedEncoderSampler;
