'use strict';

const fs = require('fs');

const FIRST_LIMIT = 32;
const LATEST_LIMIT = 64;
const DATA_LIMIT = 32;
const TRACE_ADDRESSES = new Set([58, 59, 107]);

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
  this._lastNormalReadDiagnostic = null;
  this._flushPending = false;
  this._writePending = false;
  this._wireSend = serial.sendCustom.bind(serial);
  this._wireSendBatteryQuery = serial.sendBatteryQuery.bind(serial);
  this._onResponse = this._recordResponse.bind(this);
  this._onCoreResponse = this._recordCoreResponse.bind(this);
}

EncoderTrace.prototype.installWireObserver = function () {
  this._serial.sendCustom = (sid, command, payload) => {
    this._recordSend('wire_send_custom', sid, command, payload);
    return this._wireSend(sid, command, payload);
  };
  this._serial.sendBatteryQuery = () => {
    this._record({ type: 'wire_send_battery_query', monotonic_ns: monotonicNs() });
    return this._wireSendBatteryQuery();
  };
  this._serial.on('servo_response', this._onResponse);
  this._serial.on('core_response', this._onCoreResponse);
};

EncoderTrace.prototype.installCallObserver = function () {
  const sendCustom = this._serial.sendCustom.bind(this._serial);
  const sendBatteryQuery = this._serial.sendBatteryQuery.bind(this._serial);
  this._serial.sendCustom = (sid, command, payload) => {
    this._recordSend('send_custom_call', sid, command, payload);
    return sendCustom(sid, command, payload);
  };
  this._serial.sendBatteryQuery = () => {
    this._record({ type: 'send_battery_query_call', monotonic_ns: monotonicNs() });
    return sendBatteryQuery();
  };
};

EncoderTrace.prototype.snapshot = function () {
  return {
    v: 1,
    type: 'sweep_encoder_trace',
    first: this._first.slice(),
    latest: this._latest.slice(),
    last_normal_read_diagnostic: this._lastNormalReadDiagnostic === null ? null : {
      type: this._lastNormalReadDiagnostic.type,
      monotonic_ns: this._lastNormalReadDiagnostic.monotonic_ns,
      key: this._lastNormalReadDiagnostic.key,
      outstanding: this._lastNormalReadDiagnostic.outstanding,
    },
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

EncoderTrace.prototype.recordDiagnostic = function (diagnostic) {
  const record = {
    type: diagnostic.type,
    monotonic_ns: monotonicNs(),
    key: diagnostic.key,
    outstanding: diagnostic.outstanding,
  };
  if (diagnostic.type === 'normal_read_expired' || diagnostic.type === 'normal_read_key_limit') {
    this._lastNormalReadDiagnostic = {
      type: record.type,
      monotonic_ns: record.monotonic_ns,
      key: record.key,
      outstanding: record.outstanding,
    };
  }
  this._record(record);
};

EncoderTrace.prototype._recordSend = function (type, sid, command, payload) {
  if (command !== 4 || !payload || !TRACE_ADDRESSES.has(payload[0])) return;
  const record = { type: type, monotonic_ns: monotonicNs(), sid: sid, command: command };
  recordData(payload, record);
  if (record.bytes.length) record.address = record.bytes[0];
  if (record.bytes.length > 1) record.requested_data_length = record.bytes[1];
  this._record(record);
};

EncoderTrace.prototype._recordResponse = function (message) {
  if (!message || !TRACE_ADDRESSES.has(message.addr)) return;
  const record = { type: 'servo_response', monotonic_ns: monotonicNs(), sid: message.sid, address: message.addr };
  recordData(message.data, record);
  this._record(record);
};

EncoderTrace.prototype._recordCoreResponse = function (message) {
  if (!message || (message.type !== 'battery' && message.type !== 'battery_new')) return;
  this._record({ type: 'core_response', monotonic_ns: monotonicNs(), response_type: message.type });
};

EncoderTrace.prototype._record = function (record) {
  if (this._first.length < FIRST_LIMIT) this._first.push(record);
  this._latest.push(record);
  if (this._latest.length > LATEST_LIMIT) this._latest.shift();
  if (this._fault !== null) {
    if (this._fault.after_fault.length < LATEST_LIMIT) this._fault.after_fault.push(record);
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
