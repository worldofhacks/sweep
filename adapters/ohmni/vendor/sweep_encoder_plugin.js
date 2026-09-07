'use strict';

const path = require('path');
const PairedEncoderSampler = require('./sweep_encoder_plugin/sampler');
const EncoderTrace = require('./sweep_encoder_plugin/trace');

function SweepEncoderPlugin(owner) {
  if (!owner || !owner._serial || !owner._model) {
    throw new Error('Sweep encoder plugin requires the Telebot serial owner and control model');
  }
  if (owner._sweepEncoderPlugin) {
    throw new Error('Sweep encoder plugin is already installed for this Telebot owner');
  }

  owner._sweepEncoderPlugin = this;
  this._trace = new EncoderTrace(
    owner._serial,
    owner._sweepEncoderTracePath || path.resolve(__dirname, '..', 'sweep_encoder_trace.json')
  );
  this._trace.installWireObserver();
  this._sampler = new PairedEncoderSampler(
    owner._serial,
    owner._sweepEncoderSocketPath || path.resolve(__dirname, '..', 'sweep_encoder.sock')
  );
  this._sampler.start();
  this._trace.installCallObserver();

  const modelStart = owner._model.start.bind(owner._model);
  owner._model.start = (...args) => {
    const result = modelStart(...args);
    this._sampler.activate();
    return result;
  };

  const modelInitialize = owner._model.initialize.bind(owner._model);
  owner._model.initialize = (...args) => {
    this._sampler.beginInitialization();
    return modelInitialize(...args);
  };
}

module.exports = SweepEncoderPlugin;
