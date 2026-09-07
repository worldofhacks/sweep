'use strict';

const fs = require('fs');
const net = require('net');

const ADDRESS = 58;
const BYTES = 2;
const READ_COMMAND = 4;
const POLL_INTERVAL_MS = 100;
const REPLY_TIMEOUT_MS = 100;
const NORMAL_READ_TIMEOUT_MS = 350;
const MAX_NORMAL_READ_KEYS = 7;
const DEFERRED_REQUEST_GAP_MS = 5;
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

function isReadRequest(command, payload) {
  return command === READ_COMMAND && payload && payload.length >= 1;
}

function readKey(sid, address) {
  return sid + ':' + address;
}

function PairedEncoderSampler(serial, socketPath, options) {
  options = options || {};
  this._serial = serial;
  this._directSendCustom = serial.sendCustom.bind(serial);
  this._directSendBatteryQuery = serial.sendBatteryQuery.bind(serial);
  this._deferredRequests = [];
  this._draining = false;
  this._deferredDrainTimer = null;
  this._deferredDrainDone = [];
  this._scheduleAfterDrainMs = null;
  this._scheduleAfterReadsMs = null;
  this._normalReads = {};
  this._expiredNormalReads = {};
  this._normalReadTimeoutMs = options.normalReadTimeoutMs || NORMAL_READ_TIMEOUT_MS;
  this._onDiagnostic = options.onDiagnostic || function () {};
  this._socketPath = socketPath;
  this._clock = options.monotonicNs || monotonicNs;
  this._setTimeout = options.setTimeout || setTimeout;
  this._clearTimeout = options.clearTimeout || clearTimeout;
  this._pollIntervalMs = options.pollIntervalMs || POLL_INTERVAL_MS;
  this._replyTimeoutMs = options.replyTimeoutMs || REPLY_TIMEOUT_MS;
  this._pollId = 0;
  this._active = null;
  this._failed = false;
  this._unavailable = null;
  this._timer = null;
  this._server = null;
  this._clients = [];
  this._stopped = true;
  this._qualified = false;
  this._onServoResponse = this._handleServoResponse.bind(this);
  this._onCoreResponse = this._handleCoreResponse.bind(this);
  this._onSerialClose = this._fail.bind(this, 'serial_disconnected');
  this._gatedSendCustom = this._queueOrSend.bind(this);
  this._gatedSendBatteryQuery = this._queueOrSendBatteryQuery.bind(this);
}

PairedEncoderSampler.prototype.isActive = function () {
  return this._active !== null || this._draining;
};

PairedEncoderSampler.prototype.start = function () {
  if (!this._stopped) return;
  this._stopped = false;
  this._serial.sendCustom = this._gatedSendCustom;
  this._serial.sendBatteryQuery = this._gatedSendBatteryQuery;
  this._createServer();
  this._serial.on('servo_response', this._onServoResponse);
  this._serial.on('core_response', this._onCoreResponse);
  this._serial.on('close', this._onSerialClose);
};

PairedEncoderSampler.prototype.stop = function (done) {
  if (this._stopped) {
    if (done) done();
    return;
  }
  this._stopped = true;
  this._scheduleAfterDrainMs = null;
  this._scheduleAfterReadsMs = null;
  this._clearNormalReads();
  this._abortActive();
  if (this._timer) {
    this._clearTimeout(this._timer);
    this._timer = null;
  }
  this._releaseBus(() => {
    this._serial.sendCustom = this._directSendCustom;
    this._serial.sendBatteryQuery = this._directSendBatteryQuery;
    this._serial.removeListener('servo_response', this._onServoResponse);
    this._serial.removeListener('core_response', this._onCoreResponse);
    this._serial.removeListener('close', this._onSerialClose);
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
    if (this._unavailable) client.write(JSON.stringify(this._unavailable) + '\n');
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
  if (this._stopped || this._failed || this._timer || this._active || this._draining) return;
  this._timer = this._setTimeout(() => {
    this._timer = null;
    this._beginPoll();
  }, delayMs);
};

PairedEncoderSampler.prototype.beginInitialization = function () {
  if (this._stopped) return;
  const poll = this._active;
  const wasQualified = this._qualified;
  this._qualified = false;
  this._scheduleAfterDrainMs = null;
  this._scheduleAfterReadsMs = null;
  this._clearNormalReads();
  this._abortActive();
  if (this._timer) {
    this._clearTimeout(this._timer);
    this._timer = null;
  }
  if (wasQualified) {
    this._publish({
      v: 1,
      type: 'sweep_encoder_unavailable',
      poll_id: poll ? poll.id : null,
      reason: 'serial_reinitializing',
    });
  }
  this._releaseBus();
};

PairedEncoderSampler.prototype.activate = function () {
  if (this._stopped || this._failed || this._qualified || !this._serial.opened) return;
  this._qualified = true;
  this._unavailable = null;
  this._scheduleWhenDrainCompletes(0);
};

PairedEncoderSampler.prototype._beginPoll = function () {
  if (this._stopped || this._failed || this._active || this._draining) return;
  if (this._hasOutstandingReads()) {
    this._scheduleWhenReadsComplete(0);
    return;
  }
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
  if (this._active || this._draining) {
    this._deferredRequests.push({ kind: 'custom', sid: sid, command: command, payload: Buffer.from(payload) });
    return;
  }
  this._sendVendorRequest(sid, command, payload);
};

PairedEncoderSampler.prototype._queueOrSendBatteryQuery = function () {
  if (this._active || this._draining) {
    this._deferredRequests.push({ kind: 'battery' });
    return;
  }
  this._sendBatteryQuery();
};

PairedEncoderSampler.prototype._sendVendorRequest = function (sid, command, payload) {
  if (!this._failed && !this._stopped && isReadRequest(command, payload) && !isDriveEncoderRequest(sid, command, payload)) {
    this._trackNormalRead(this._normalReadKey(readKey(sid, payload[0])), { sid: sid, address: payload[0] });
  }
  this._directSendCustom(sid, command, payload);
};

PairedEncoderSampler.prototype._sendBatteryQuery = function () {
  if (!this._failed && !this._stopped) this._trackNormalRead('battery', { kind: 'battery' });
  this._directSendBatteryQuery();
};

PairedEncoderSampler.prototype._releaseBus = function (done) {
  if (done) this._deferredDrainDone.push(done);
  if (this._draining) return;
  if (!this._deferredRequests.length) {
    this._finishDeferredDrain();
    return;
  }
  this._draining = true;
  const drain = () => {
    this._deferredDrainTimer = null;
    const request = this._deferredRequests.shift();
    if (request && request.kind === 'battery') this._sendBatteryQuery();
    else if (request) this._sendVendorRequest(request.sid, request.command, request.payload);
    if (request || this._deferredRequests.length) {
      this._deferredDrainTimer = this._setTimeout(drain, DEFERRED_REQUEST_GAP_MS);
      return;
    }
    this._draining = false;
    this._finishDeferredDrain();
  };
  drain();
};

PairedEncoderSampler.prototype._scheduleWhenDrainCompletes = function (delayMs) {
  if (!this._draining) {
    this._scheduleWhenReadsComplete(delayMs);
    return;
  }
  if (this._scheduleAfterDrainMs === null || delayMs < this._scheduleAfterDrainMs) {
    this._scheduleAfterDrainMs = delayMs;
  }
};

PairedEncoderSampler.prototype._finishDeferredDrain = function () {
  const completions = this._deferredDrainDone;
  this._deferredDrainDone = [];
  completions.forEach((complete) => complete());
  const delayMs = this._scheduleAfterDrainMs;
  this._scheduleAfterDrainMs = null;
  if (delayMs !== null && this._qualified && !this._failed && !this._stopped) {
    this._scheduleWhenReadsComplete(delayMs);
  }
};

PairedEncoderSampler.prototype._hasOutstandingReads = function () {
  return Object.keys(this._normalReads).length !== 0 || Object.keys(this._expiredNormalReads).length !== 0;
};

PairedEncoderSampler.prototype._scheduleWhenReadsComplete = function (delayMs) {
  if (!this._hasOutstandingReads()) {
    this._schedule(delayMs);
    return;
  }
  if (this._scheduleAfterReadsMs === null || delayMs < this._scheduleAfterReadsMs) {
    this._scheduleAfterReadsMs = delayMs;
  }
};

PairedEncoderSampler.prototype._requestSide = function (poll, side) {
  if (this._active !== poll || this._stopped) return;
  poll.side = side;
  poll.timeout = this._setTimeout(() => this._expire(poll), this._replyTimeoutMs);
  this._directSendCustom(side, READ_COMMAND, Buffer.from([ADDRESS, BYTES]));
};

PairedEncoderSampler.prototype._handleServoResponse = function (message) {
  const key = readKey(message.sid, message.addr);
  if (this._normalReads[key]) {
    this._completeNormalRead(key);
    return;
  }
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

PairedEncoderSampler.prototype._handleCoreResponse = function (message) {
  if (!message || (message.type !== 'battery' && message.type !== 'battery_new')) return;
  this._completeNormalRead('battery');
};

PairedEncoderSampler.prototype._trackNormalRead = function (key, details) {
  const existing = this._normalReads[key];
  if (existing && existing.timeout) this._clearTimeout(existing.timeout);
  const expired = this._expiredNormalReads[key];
  if (expired) this._clearTimeout(expired.timeout);
  const read = {
    details: details,
    quarantined: Boolean(expired),
    timeout: this._setTimeout(() => this._expireNormalRead(key, read), this._normalReadTimeoutMs),
  };
  this._normalReads[key] = read;
  if (expired) this._markExpiredNormalRead(key);
};

PairedEncoderSampler.prototype._normalReadKey = function (key) {
  if (this._normalReads[key] || this._expiredNormalReads[key] ||
      Object.keys(this._normalReads).length + Object.keys(this._expiredNormalReads).length < MAX_NORMAL_READ_KEYS) {
    return key;
  }
  this._onDiagnostic({
    type: 'normal_read_key_limit',
    key: key,
    outstanding: this._normalReadSnapshot(),
  });
  return 'other';
};

PairedEncoderSampler.prototype._completeNormalRead = function (key) {
  if (this._expiredNormalReads[key]) return;
  const read = this._normalReads[key];
  if (!read) return;
  this._clearTimeout(read.timeout);
  delete this._normalReads[key];
  this._resumeWhenReadsComplete();
};

PairedEncoderSampler.prototype._expireNormalRead = function (key, read) {
  if (this._normalReads[key] !== read) return;
  delete this._normalReads[key];
  if (!read.quarantined) this._markExpiredNormalRead(key);
  this._onDiagnostic({
    type: 'normal_read_expired',
    key: key,
    outstanding: this._normalReadSnapshot(),
  });
  this._publish({
    v: 1,
    type: 'sweep_encoder_unavailable',
    poll_id: null,
    reason: 'normal_read_timeout',
  });
  this._resumeWhenReadsComplete();
};

PairedEncoderSampler.prototype._normalReadSnapshot = function () {
  return {
    pending: Object.keys(this._normalReads).sort(),
    expired: Object.keys(this._expiredNormalReads).sort(),
  };
};

PairedEncoderSampler.prototype._clearNormalReads = function () {
  Object.keys(this._normalReads).forEach((key) => this._clearTimeout(this._normalReads[key].timeout));
  Object.keys(this._expiredNormalReads).forEach((key) => this._clearTimeout(this._expiredNormalReads[key].timeout));
  this._normalReads = {};
  this._expiredNormalReads = {};
};

PairedEncoderSampler.prototype._markExpiredNormalRead = function (key) {
  const expired = {
    timeout: this._setTimeout(() => {
      if (this._expiredNormalReads[key] !== expired) return;
      delete this._expiredNormalReads[key];
      this._resumeWhenReadsComplete();
    }, this._normalReadTimeoutMs),
  };
  this._expiredNormalReads[key] = expired;
};

PairedEncoderSampler.prototype._resumeWhenReadsComplete = function () {
  if (this._hasOutstandingReads()) return;
  const delayMs = this._scheduleAfterReadsMs;
  this._scheduleAfterReadsMs = null;
  if (delayMs !== null && this._qualified && !this._failed && !this._stopped) this._schedule(delayMs);
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
  this._qualified = false;
  this._scheduleAfterDrainMs = null;
  this._scheduleAfterReadsMs = null;
  this._clearNormalReads();
  this._failed = true;
  this._releaseBus();
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
  if (payload.type === 'sweep_encoder_unavailable') this._unavailable = payload;
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
  this._unavailable = null;
  this._publish({
    v: 1,
    type: 'sweep_encoder_pair',
    poll_id: poll.id,
    left: poll.left,
    right: poll.right,
    left_receipt_ns: poll.leftReceiptNs,
    right_receipt_ns: poll.rightReceiptNs,
  });
  this._releaseBus();
  this._scheduleWhenDrainCompletes(this._pollIntervalMs);
};

module.exports = PairedEncoderSampler;
