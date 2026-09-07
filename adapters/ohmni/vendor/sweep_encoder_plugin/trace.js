'use strict';

const fs = require('fs');

const FIRST_LIMIT = 32;
const LATEST_LIMIT = 64;
const DATA_LIMIT = 32;
const ENCODER_ADDRESSES = new Set([58, 59]);

function monotonicNs() {
  const now = process.hrtime();
  return String(now[0] * 1000000000 + now[1]);
}

function recordData(data, record) {
  const length = data && Number.isSafeInteger(data.length) && data.length >= 0 ? data.length : 0;
  const value = Array.prototype.slice.call(data || [], 0, Math.min(length, DATA_LIMIT));
  record.bytes = value;
  record.data_length = length;
  if (length === 2) record.uint16_le = value[0] | (value[1] << 8);
  if (length === 4) {
    record.int32_le = (value[0] | (value[1] << 8) | (value[2] << 16) | (value[3] << 24));
  }
}

function EncoderTrace(serial, snapshotPath) {
  this._serial = serial;
  this._snapshotPath = snapshotPath;
  this._first = [];
  this._latest = [];
  this._fault = null;
  this._flushPending = false;
  this._writePending = false;
  this._wireSend = serial.sendCustom.bind(serial);
  this._onResponse = this._recordResponse.bind(this);
}

EncoderTrace.prototype.installWireObserver = function () {
  this._serial.sendCustom = (sid, command, payload) => {
    this._recordSend('wire_send_custom', sid, command, payload);
    return this._wireSend(sid, command, payload);
  };
  this._serial.on('servo_response', this._onResponse);
};

EncoderTrace.prototype.installCallObserver = function () {
  const sendCustom = this._serial.sendCustom.bind(this._serial);
  this._serial.sendCustom = (sid, command, payload) => {
    this._recordSend('send_custom_call', sid, command, payload);
    return sendCustom(sid, command, payload);
  };
};

EncoderTrace.prototype.snapshot = function () {
  return {
    v: 1,
    type: 'sweep_encoder_trace',
    first: this._first.slice(),
    latest: this._latest.slice(),
    fault: this._fault === null ? null : {
      reason: this._fault.reason,
      poll_id: this._fault.poll_id,
      pending_side: this._fault.pending_side,
      monotonic_ns: this._fault.monotonic_ns,
      context: this._fault.context.slice(),
      after_fault: this._fault.after_fault.slice(),
    },
  };
};

EncoderTrace.prototype.recordFault = function (reason, pollId, pendingSide) {
  if (this._fault !== null) return;
  this._fault = {
    reason: reason,
    poll_id: pollId,
    pending_side: pendingSide,
    monotonic_ns: monotonicNs(),
    context: this._latest.slice(),
    after_fault: [],
  };
  this._scheduleFlush();
};

EncoderTrace.prototype._recordSend = function (type, sid, command, payload) {
  if (command !== 4 || !payload || !ENCODER_ADDRESSES.has(payload[0])) return;
  const record = { type: type, monotonic_ns: monotonicNs(), sid: sid, command: command };
  recordData(payload, record);
  if (record.bytes.length) record.address = record.bytes[0];
  if (record.bytes.length > 1) record.requested_data_length = record.bytes[1];
  this._record(record);
};

EncoderTrace.prototype._recordResponse = function (message) {
  if (!message || !ENCODER_ADDRESSES.has(message.addr)) return;
  const record = { type: 'servo_response', monotonic_ns: monotonicNs(), sid: message.sid, address: message.addr };
  recordData(message.data, record);
  this._record(record);
};

EncoderTrace.prototype._record = function (record) {
  if (this._first.length < FIRST_LIMIT) this._first.push(record);
  this._latest.push(record);
  if (this._latest.length > LATEST_LIMIT) this._latest.shift();
  if (this._fault !== null) {
    this._fault.after_fault.push(record);
    if (this._fault.after_fault.length > LATEST_LIMIT) this._fault.after_fault.shift();
  }
  this._scheduleFlush();
};

EncoderTrace.prototype._scheduleFlush = function () {
  if (this._flushPending) return;
  this._flushPending = true;
  setTimeout(() => {
    this._flushPending = false;
    this._flush();
  }, 25);
};

EncoderTrace.prototype._flush = function () {
  if (this._writePending) {
    this._scheduleFlush();
    return;
  }
  this._writePending = true;
  const temporary = this._snapshotPath + '.tmp';
  fs.writeFile(temporary, JSON.stringify(this.snapshot()) + '\n', { mode: 0o600 }, (error) => {
    if (error) {
      this._writePending = false;
      return;
    }
    fs.rename(temporary, this._snapshotPath, () => {
      this._writePending = false;
      if (this._flushPending) return;
    });
  });
};

module.exports = EncoderTrace;
