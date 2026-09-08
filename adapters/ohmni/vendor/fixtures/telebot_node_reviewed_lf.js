//
// Main telebot control class
//
// Jared
//

const fs = require('fs');
const DeviceScanner = require('./device_scanner');
const exec = require('child_process').exec;
const async = require('async');
const Serial = require('./serial');
const SysControl = require('./sys_control');
const ControlModel = require('./control_model');
const BotShellServer = require('./bot_shell_server');
const LocalApi = require('./local_api');
const BlueControllerPlugin = require('./bluetooth_plugins/bluetooth_plugin');
const SnapshotMgr = require('./snapshot_mgr');
const HardwareInfoCollector = require('./hw_info_collector')
const crypto = require("crypto");
const LidarNode = require('./lidar_node');

// ======== Main class ========

function TelebotNode() {

  const self = this;

  // This is used for reading the three config files
  // and where to create node.sock and node.pid for android app communication
  // For now we auto take this from the installed path, which is
  // /data/data/com.appdomain/files/assets/node-files, and we go
  // two levels up.
  const config_path = __dirname.split("/").slice(0, -2).join("/");
  console.log("Config path:", config_path);

  // Start firmware query and fill it up here
  this._fw_version = {
    left_drive: null,
    right_drive: null,
    core: null,
    done: false
  };

  // hardware info data collection
  this._hwInfoCollector = new HardwareInfoCollector();
  this._hwInfoCollector.on("cpu_info", function (msg) {
    self._api.sendJson({
      type: "cpu_info",
      data: msg
    })
  });

  //Incall state
  this._isIncall = false;

  // Serial control
  this._serial = new Serial();
  this._serial.on("open", () => {
    this.handleSerialConnected();
  });
  this._serial.on("servo_response", function (msg) {
    self.handleServoResponse(msg);
  });
  this._serial.on("core_response", function (msg) {
    self.handleCoreResponse(msg);
  });

  this._serial.on("close", function (error) {
    self.handleSerialDisconnected(error);
  });
  // Helper to disable serial queries
  // if we need specific reports
  this._disable_serial_queries = false;

  // Control action dispatch
  this._dispatch = {};
  this.setupCtrlDispatch();

  // Odometry report cache, prepared for sending
  this._odo = { type: "odo", l: 0, r: 0 };

  // Control model
  this._model = new ControlModel({
    calibpath: config_path + "/telebot_calib.json"
  }, this._serial, this);

  // Autodock events already have a type and status, use
  // these to drive the UI.  Dock is detected marker info
  this._model.on("dock", function (e) {
    self._api.sendJson({
      type: "dock",
      mx: e.markerX,
      my: e.markerY
    });
  });

  // Vision/frame data that we want to push up to the
  // appropriate level. fdata is array of objs to render
  this._model.on("FrameData", function (fdata) {
    self._api.sendJson({
      type: "frame",
      objs: fdata,
    });
  });

  this._model.on("event", function (e) {
    self._api.sendJson(e);

    // Turn back on the status tick
    if ((self._tick_interval == null) && (!e.running)) {
      self._tick_interval = setInterval(function () {
        self.tick();
      }, self._tick_delay_ms);
    }
  });

  this._model.on("calib_event", function (e) {
    self._api.sendJson(e);
  });

  // Botshell which allows user to control us - set up server
  // and bind our primary console botshell for legacy compat
  this._botshell_server = new BotShellServer(this);
  this._botshell = this._botshell_server._console_botshell;

  // Set up the Local API which allow the app to control us
  this._api = new LocalApi(this, config_path);

  // New local api hooks for control
  this._api.on("lapi-ctrl", function (msg) {
    // console.log("LOGBOT TelebotNode lapi-ctrl", msg);
    self.handleCtrlMsg(msg);
  });

  //Set up Local Api get status change
  this._api.on("lapi-onstatus", function () {
    //console.log("LOGBOT TelebotNode lapi-onstatus");
    self._isIncall = true;
    self._model.wake_head();

    // Ensure wheel torques are on in case they
    // accidentally get turned off
    self._model.wake_wheels();

    // For good measure - unmute and adjust
    // all the volumes accordingly to prevent
    // user error
    console.log("LOGBOT lapi-onstatus set speaker");

    // There is a alsa level bug here where when you
    // switch the state from the JABRA itself, it
    // doesn't update.  So then if the mic is muted
    // but the system thinks it's on, it doesn't send
    // the switch command.  So toggle it like this.
    self._api.cmd_setSpeakerEnabled({ value: 0 });
    self._api.cmd_setSpeakerEnabled({ value: 1 });
    self._api.cmd_setMicEnabled({ value: 0 });
    self._api.cmd_setMicEnabled({ value: 1 });

    // Same applies to these - create a change so
    // that it will apply no matter the cached state
    self._api.cmd_setSpeakerVolume({ value: "9" });
    self._api.cmd_setSpeakerVolume({ value: "10" });
    self._api.cmd_setMicLevel({ value: "6" });
    self._api.cmd_setMicLevel({ value: "7" });

  });

  //
  this._api.on("lapi-dis", function () {
    // Clean up autodock first just so we don't have autodock runaway
    self._model.autodock_tick_cleanup();

    //console.log("LOGBOT TelebotNODE lapi-dis");
    self._model.rest_head();

    // NOTE: Jared removed below 2021-02-23.  Our firmware has automatic
    // idling so we don't need to turn off torque.  Otherwise this has some
    // strange side effects, for example the SDRive reverse/forwards in
    // Factory test will not work because they assume torque is on.
    // -------------------------------------------------
    // Do not release the wheels when Bot is docked,
    // This DOCKED check is to avoid cleaner staffs accidentally undock the bot
    // if (self.CACHED_DOCKED === 0) self._model.rest_wheels();

    // Stop lidar scanning
    if (self._lidarDevice) self._lidarDevice.stop_scan();

    //set incall
    self._isIncall = false;
  });

  this._api.on("restart_cpu_info_collector", function (cpu_collect_interval) {
    console.log("restart CPU Info collector with new interval: ", cpu_collect_interval);
    self._hwInfoCollector.restartCpuInfoCollector(cpu_collect_interval);
  });

  this._api.on("stop_cpu_info_collector", function () {
    console.log("stop CPU Info collector");
    self._hwInfoCollector.stopCpuInfoCollector();
  });

  this.blueControllerPlugin = new BlueControllerPlugin(this);

  // Audio engine to control speaker volume
  this._tinyalsa = require('tinyalsa');

  // Launching stuff
  this._joining = false;

  // Some status ticks here
  this.set_tick_delay(500);

  // Obstacle update tick here
  this._obs_update_delay_ms = 100; // we currently want to update at 10Hz
  this._obs_update_interval = setInterval(() => {
    if (!this._lidarNode.isCollisionDetectionActive()) return;
    const state = [];
    const { collisionDetectionNode } = this._lidarNode;
    const { obstacleMap, isInFront } = collisionDetectionNode;
    state[0] = isInFront ? obstacleMap[0] : Math.max(obstacleMap[0], obstacleMap[1]);
    state[1] = isInFront ? obstacleMap[1] : obstacleMap[2];
    this._api.sendJson({
      type: "obs",
      state: state
    });

  }, this._obs_update_delay_ms);

  // Normalize overall system volume. This affects the
  // text to speech but NOT the in-call volume based on
  // my testing on Feb 2, 2017. Set it a bit low so it
  // isn't so shouty.
  SysControl.setVolume(3, 10);

  // Set permissions accordingly
  this._v4l2grab_path = "/data/data/com.ohmnilabs.telebot_rtc/files/assets/utils/v4l2grab";
  try {
    fs.chmodSync(this._v4l2grab_path, 0o755);
  } catch (e) {
    console.log("Chmod v4l2grab failed:", e);
  }

  // Create our snapshot manager
  this._snapshot_mgr = new SnapshotMgr();

  this._lidarNode = new LidarNode();

  this._lidarNode.on('stop_collision_detection', () => {
    this._api.sendJson({
      type: "obs",
      state: [0, 0]
    });
  });

  // For maximum video encoding performance,
  // switch to performance governor
  try {
    fs.writeFileSync("/sys/devices/system/cpu/cpufreq/policy0/scaling_governor", "performance\n");
    fs.writeFileSync("/sys/devices/system/cpu/cpufreq/policy1/scaling_governor", "performance\n");
    fs.writeFileSync("/sys/devices/system/cpu/cpufreq/policy2/scaling_governor", "performance\n");
    fs.writeFileSync("/sys/devices/system/cpu/cpufreq/policy3/scaling_governor", "performance\n");
  } catch (e) {
    console.log("Unable to set CPU governor to performance:", e);
  }

  // Load and instantiate plugins here
  // Developer edition
  this._plugins = [];
  try {
    var plugpath = "/data/data/com.ohmnilabs.telebot_rtc/files/plugins";
    fs.readdirSync(plugpath).forEach((f) => {
      if (!f.endsWith(".js")) return;
      console.log("Loading OhmniJS plugin:", f);
      try {
        var PluginClass = require(plugpath + "/" + f);
        this._plugins.push(new PluginClass(this));
      } catch (e) {
        console.log("*** FAILED TO LOAD PLUGIN", f, "Error:", e);
      }
    });
  } catch (e) {
    console.log("OhmniJS plugins dir not found:", e);
  }
}
// ============= Handle lidar device =============
TelebotNode.prototype.scanLidarDevice = function () {
  console.log("Start scan lidar device");
  // release the lidar serial port first
  this.releaseLidar();
  try {
    this._lidarDevice = this._lidarNode.scanLidarDevice();
  } catch (error) {
    console.log("Do not find lidar device", error);
  }
}

TelebotNode.prototype.releaseLidar = function () {
  if (!this._lidarDevice) return;
  const self = this;
  try {
    self._lidarDevice.set_motor_pwm(0);
    self._lidarDevice.close();
    self._lidarDevice = null;
    console.log("closed lidar device");
  } catch (error) {
    console.log("Could not close lidar device", error);
  }
  // self._lidarDevice = null;
}

TelebotNode.prototype.startCollisionDetection = function (config) {
  if (!this._lidarDevice) {
    console.log("There is no lidar device.");
    return;
  }
  // Create our collision detection node for the A2M8 lidar
  if (!this._lidarNode.isCollisionDetectionAvailable()) {
    const options = {
      lidarDevice: this._lidarDevice,
      controlModel: this._model,
      mainSerial: this._serial,
      config
    }
    this._lidarNode.createCollisionDetectionNode(options);
    this._lidarNode.startCollisionDetection();
  }
}

TelebotNode.prototype.stopCollisionDetection = function () {
  if (!this._lidarDevice) {
    console.log("There is no lidar device.");
    return;
  }
  this._lidarNode.stopCollisionDetection();
}

TelebotNode.prototype.cmd_configCollisionDetection = function (config) {
  if (!this._lidarDevice) {
    console.log("There is no lidar device.");
    return;
  }

  this._lidarNode.configCollisionDetection(config);
}


// ======== Handle messages from serial ========

// Just log them for now, we'll break it out later
TelebotNode.prototype.handleServoResponse = function (msg) {
  const self = this;
  switch (msg.addr) {
    case 53:
      console.log("Servo", msg.sid, "LED =", msg.data.readUInt8(0));
      break;
    case 58:
      console.log("Servo", msg.sid, "calpos =", msg.data.readUInt16LE(0) & 0x3ff);
      break;
    case 60:
      console.log("Servo", msg.sid, "apos =", msg.data.readUInt16LE(0));
      break;
    case 62:
      console.log("Servo", msg.sid, "diffpos =", msg.data.readInt16LE(0));
      break;
    case 59:
      if (msg.sid == 1) {
        self._odo.r = msg.data.readInt32LE(0);
      } else if (msg.sid == 0) {
        self._odo.l = msg.data.readInt32LE(0);
      }
      break;
    case 0x6b:
      // No need to print out anything on this poll
      break;
    default:
      console.log("Servo", msg.sid, "data", msg.addr, " = ", msg.data);
      break;
  }
};

TelebotNode.prototype.handleSerialDisconnected = function (error) {
  console.log("handleSerialDisconnected", error);
  this.get_internal_bot_info();
}

TelebotNode.prototype.handleSerialConnected = function (error) {
  const self = this;
  console.log("handleSerialConnected", error);
  // Query the versions first, async, then init
  this.get_internal_bot_info().then(() => {
    // Re-init our control model and servos
    self._model.initialize(self._fw_version);
  });
}

// Core responses we parse to JSON for now
TelebotNode.prototype.handleCoreResponse = function (json) {

  switch (json.type) {
    case "battery":
      // Convert voltages to cell voltages
      var v = json.v;
      var c = [
        v[0],
        v[1] - v[0],
        v[2] - v[1]
      ];

      // For now simply forward to server, and save a local
      var msg = { type: json.type, v: c, d: json.d };

      // We are now sending this to our android app instead of straight to server
      this._api.sendJson(msg);
      this.CACHED_BAT = c;

      // Some local notifications for now
      if (this.CACHED_DOCKED != json.d) {
        console.log("DOCKED STATUS:", this.CACHED_DOCKED, "->", json.d);
        this.CACHED_DOCKED = json.d;
      }

      break;

    case "battery_new":

      // Forward to server here
      this._api.sendJson(json);
      this.CACHED_BAT = json.v;
      this.CACHED_BATINFO = json;

      // Find the min cell
      var mincell = null;
      json.v.forEach((v) => {
        if (mincell == null || v < mincell) {
          mincell = v;
        }
      });

      // Cache it here
      if (this.CACHED_MINCELL != mincell) {
        this.CACHED_MINCELL = mincell;
        var bstat = 0;
        if (mincell < 3150) bstat = 20;
        else if (mincell < 3250) bstat = 50;
        else if (mincell < 3380) bstat = 80;
        else bstat = 100;

        if (this.CACHED_BSTAT != bstat) {
          this.CACHED_BSTAT = bstat;
          exec("dumpsys battery set level " + bstat, (error, stdout, stderr) => { });
        }
      }

      // Some local notifications for now
      if (this.CACHED_DOCKED != json.d) {
        console.log("DOCKED STATUS:", this.CACHED_DOCKED, "->", json.d);
        this.CACHED_DOCKED = json.d;

        if (json.d) {
          exec("dumpsys battery set ac 1", (error, stdout, stderr) => { });
        } else {
          exec("dumpsys battery unplug", (error, stdout, stderr) => { });
        }
      }

      //for detect low battery and sleep when not in-call, servo is not active used, and not docked
      // if (
      //   mincell < 3150 &&
      //   !this._isIncall &&
      //   this._model._headState !== "SLEEPING" &&
      //   this._model._servo_is_idle &&
      //   !json.d
      // ) {
      //   console.log("Detect low battery cell, min cell: ", mincell);
      //   console.log(" >please go to dock charge");
      //   this._model.sleep_head();
      // }

      break;
  }
};

// ======== Sending custom json message to the client ========
TelebotNode.prototype.sendCustomJsonMsg = function (json) {
  const json_msg = { type: "capi", jsonmsg: json };
  this._api.sendJson(json_msg);
}

// ======== Handle messages from hive ========

// New dispatch stuff.  We already JSONify the msg so we don't
// want to reformat and pass through our old tlvstream here.  We may
// want to evolve this later on
TelebotNode.prototype.handleCtrlMsg = function (msg) {
  // Wrap this in exception logic for safety.
  // For example, sometimes we get throws from
  // ioctl or other stuff
  try {

    // Get message type
    let code = msg.code;

    // Legacy stuff here
    if (msg.type == 'cmd') code = 0xffff;

    // Check in dispatch table
    //console.log("Dispatching", code, "with", msg.data);
    const handler = this._dispatch[code];
    if (handler != null) {
      handler.call(this, msg.data);
    }

  } catch (err) {
    console.log("Handling message", msg, "caused exception", err.stack);
  }

};

TelebotNode.prototype.setupCtrlDispatch = function () {
  // Get here
  const self = this;

  // Servo stuff
  this._dispatch[0xffff] = function (json) {
    // Look up dynamic dispatch.  A bit ugly all this JSON
    // nesting - we can clean up now that we have a direct peer conn
    console.log("Dispatch json msg", json);
    var dispatchfn = self["handleJsonMessage_" + json.type];
    if (dispatchfn != null) dispatchfn.call(self, json);
  };

  // Command to recenter head
  this._dispatch[20] = function (data) {
    this._model.recenter_head();
  };

  // THIS IS CURRENTLY USED FOR MOUSE LOOK
  this._dispatch[62] = function (data) {

    // Parse and send to model
    const dyaw = data[0];
    const dpitch = data[1];

    // Hack here for now, figure out cleaner integration later -
    // disable yaw rotation while docked
    //if (self.CACHED_DOCKED == 1) {
    //  dyaw = 0.0;
    //}

    // Now pass this on
    self._model.updateLookAngles(dyaw, dpitch);
  };

  /* Center lock not used for now since no head yaw joint
  this._dispatch[63] = function(buf) {

    // Set yaw lock flag based on value
    target_head.yaw.center_lock = origval > 0.5;

  };*/

  // THIS IS CURRENTLY USED for WASD motion
  this._dispatch[70] = function (data) {
    // New motion command
    const vfwd = data[0];
    const vleft = data[1];

    // Hack here for now, figure out cleaner integration later -
    // disable forward motion once docked
    //if (self.CACHED_DOCKED == 1) {
    //  if (vfwd > 0.0) vfwd *= 0.05;
    //}

    self._model.updateTargetVelocity(vleft, vfwd);
  };

};


// JSON message handler for now, handle it directly here
TelebotNode.prototype.handleJsonMessage_disconnect = function (msg) {
  // Client disconnected, rest head
  this._model.rest_head();
};

// Set color message from the server
TelebotNode.prototype.handleJsonMessage_setLightColor = function (msg) {

  // Pass on to inner message
  console.log("Server message: setLightColor");
  this.handleJsonCmd_setLightColor({
    h: msg.h,
    s: msg.s,
    v: msg.v,
  });

};

// Set move message from the server
TelebotNode.prototype.handleJsonCmd_move = function (msg) {
  // Log it here
  console.log("Got move:", msg);

  // Send it up here
  // TODO: more validation?
  this._api.cmd_move(msg);

};

// Set neck torque message from the server
TelebotNode.prototype.handleJsonCmd_setNeckTorqueEnabled = function (msg) {

  // Log it here
  console.log("Set neck torque enabled:", msg);

  // Send it up here
  // TODO: more validation?
  this._api.cmd_setNeckTorqueEnabled(msg);

};

// Set neck position message from the server
TelebotNode.prototype.handleJsonCmd_setNeckPosition = function (msg) {
  // Log it here
  console.log("Set neck position:", msg);

  // Send it up here
  // TODO: more validation?
  this._api.cmd_setNeckPosition(msg);

};


// Load a page command from the server
TelebotNode.prototype.handleJsonMessage_loadpage = function (msg) {
  // Log it here
  console.log("Got loadpage:", msg);

  // Send it up here
  // TODO: more validation?
  this._api.sendJson(msg);

};


// ======== Analysis ========

// BE CAREFUL.  Only use static strings or well sanitized ones
// here as we're running these routines and getting the output.
TelebotNode.prototype.runAndReport = function (qname, str, optcb) {
  // Run it here async and report results, only one instance
  // can be running at a time.
  const key = "__query_" + qname;
  if (this[key] != null) return;

  // Run it here
  this[key] = exec(str, (error, stdout, stderr) => {
    // Make message
    const msg = {
      type: "log",
      area: "top",
      msg: stdout,
      err: stderr
    };
    // Do optional callback
    if (optcb != null) optcb(msg);

    // Report this data here
    this._api.sendJson(msg);

    // Clear it here
    this[key] = null;
  });
};


// Firmware fetch updated to use async
TelebotNode.prototype.get_firmware_version = function () {

  const tagmap = { 0: "left", 1: "right", 4: "new_neck", 20: "core" };
  const tags = { type: "fwver" };

  // Bind handler to handle responses, don't need to check address
  // since this is the only eep query
  const handler = (msg) => {
    const tag = tagmap[msg.sid];
    if (tag != null) tags[tag] = msg.data.toString('ascii');
    else if (msg.sid == 3) { // neck servo report is not a string
      if (msg.data[0] == 1 && msg.data[1] == 1)
        tags["neck"] = "DRS-0101";
      else if (msg.data[0] == 2 && msg.data[1] == 1)
        tags["neck"] = "DRS-0201";
      else
        tags["neck"] = "Unknown servo model";
    }
  }

  this._serial.on("servo_eep_response", handler);

  // Start sending out queries accordingly
  const version_tag = Buffer.from([0xf1, 0x00]);
  const neck_model = Buffer.from([0x00, 0x02]);
  return new Promise(resolve => {
    async.eachSeries([0, 1, 20, 4, 4, 3, 3, 3, 3, 3, 4, 4], (item, cb) => {
      this._serial.sendCustom(item, 2, (item == 3) ? neck_model : version_tag);
      setTimeout(cb, 10);
    }, (err) => {
      this._serial.removeListener("servo_eep_response", handler);
      // Check cached file
      var jsconf = {};
      try {
        jsconf = JSON.parse(fs.readFileSync("/etc/ohmni_devconfig.json", "utf8").toString());
        //console.log("**** Loaded devconfig:", jsconf);
      } catch (e) {
      }

      // Handle the case either neck servo is still not found,
      if (tags.neck == null && tags.new_neck == null) {

        // Scan failed, neither servo was detected.  Use
        // cached value or just default to old neck
        if (jsconf.neck_type == "worm") {
          tags["new_neck"] = "cached";
        } else if (jsconf.neck_type != null) {
          tags["neck"] = jsconf.neck_type;
        } else {
          tags["neck"] = "DRS-0101";
        }


      } else if (tags.new_neck != null) {

        // New neck was detected, make sure file matches
        if (jsconf.neck_type != "worm") {
          jsconf.neck_type = "worm";
          fs.writeFileSync("/etc/ohmni_devconfig.json", JSON.stringify(jsconf));
        }

      } else if (tags.neck != null) {

        // Old neck was detected, make sure file matches
        if (jsconf.neck_type != tags.neck) {
          jsconf.neck_type = tags.neck;
          fs.writeFileSync("/etc/ohmni_devconfig.json", JSON.stringify(jsconf));
        }

      }

      this._fw_version = tags;
      console.log(
        'Left tag:', tags.left,
        '| Right tag:', tags.right,
        '| Core:', tags.core,
        '| Neck:', tags.neck,
        '| New Neck:', tags.new_neck);

      // All done now
      return resolve(tags);
    });
  });
};

TelebotNode.prototype.set_tick_delay = function (tick_delay) {
  const self = this;
  this._tick_delay_ms = tick_delay;
  if (this._tick_interval) clearInterval(this._tick_interval);
  this._tick_interval = setInterval(function () {
    self.tick();
  }, this._tick_delay_ms);
}

TelebotNode.prototype.stop_tick_ = function () {
  const self = this;
  if (this._tick_interval) clearInterval(this._tick_interval);
  this._tick_interval = null;
  return new Promise(resolve => {
    setTimeout(() => {
      resolve();
    }, this._tick_delay_ms)
  })
}

TelebotNode.prototype.get_internal_bot_info = async function () {
  const deviceScanner = new DeviceScanner()
  const hardware_info = deviceScanner.scanUsbDevices();
  const serial_info = { is_connected: this._serial.opened, port: this._serial._portstr };
  // Stop get odom info from wheels through serial to get the firmware version first
  await this.stop_tick_();
  const fwver = await this.get_firmware_version();
  this.set_tick_delay(this._tick_delay_ms);
  const returnedData = { type: "internal_bot_info", hardware_info, fwver, serial_info }
  // Defer for a bit then just push this up
  // so that we can report the data
  setTimeout(() => { this._api.sendJson(returnedData); }, 1000);
  return Promise.resolve({ returnedData });
}

// Server tells us to run top and report back data
// Comes in through local_api.js and TelebotService onMessage
TelebotNode.prototype.handleJsonMessage_top = function (msg, optcb) {
  this.runAndReport("top", "top -n 1 -m 5", optcb);
};

TelebotNode.prototype.handleJsonMessage_camcheck = function (msg, optcb) {
  const devname = this._model.get_video_devname();
  const estr = this._v4l2grab_path + " -W 1280 -H 1024 -o /dev/null -t 30 -d " + devname;
  this.runAndReport("cam", estr, optcb);
};

TelebotNode.prototype.handleJsonMessage_uptime = function (msg, optcb) {
  this.runAndReport("uptime", "uptime", optcb);
};

TelebotNode.prototype.handleJsonMessage_lsdevusb = function (msg, optcb) {
  this.runAndReport("lsdevusb", "ls -al /dev/usb", optcb);
};


// Second level message encapsulation for stuff straight
// from the client, so needs more validation,e tc.
TelebotNode.prototype.handleJsonMessage_jsoncmd = function (msg) {
  var self = this;
  try {
    // Get and parse safely
    // TODO: cleanup extra encap moving forwards
    const str = msg.jsonstr.toString();
    const json = JSON.parse(str);

    // Check that we have a cmd
    const cmd = json.cmd;

    // Look up dynamic dispatch
    const dispatchfn = self["handleJsonCmd_" + cmd];
    if (dispatchfn != null) dispatchfn.call(self, json);
    else console.log("No such command:", dispatchfn);

  } catch (err) {
    console.log("Error parsing inner jsoncmd:", err);
  }

};


// JSON message handler for various settings
TelebotNode.prototype.handleJsonCmd_setBrightness = function (msg) {
  SysControl.setBrightness(msg.value);
};

TelebotNode.prototype.handleJsonCmd_setRotation = function (msg) {
  SysControl.setRotation(msg.value);
};

TelebotNode.prototype.handleJsonCmd_setVolume = function (msg) {
  SysControl.setVolume(msg.stream, msg.value);
};

TelebotNode.prototype.handleJsonCmd_servoReboot = function (msg) {
  // Do a new init here
  console.log("Manual servo reboot!");
  this._model.init_servos(function () {
    console.log("Servo reboot complete!")
  });
};

TelebotNode.prototype.handleJsonCmd_accelparams = function (msg) {
  // Set into the model
  var v = parseInt(msg.value);
  console.log("setting acceleration to", v);
  this._model.setAcceleration(v);
};

TelebotNode.prototype.handleJsonCmd_ping = function (msg) {
  // Send a pong back now via jsoncmd channel
  var reply = { type: "jsoncmd", cmd: "pong", id: msg.id, seqnum: msg.seqnum, time: msg.time };
  this._link.sendJson(1, reply);
};

TelebotNode.prototype.handleJsonCmd_setLightColor = function (msg) {
  // Parse and bound these now
  if (msg.h < 0) msg.h = 0;
  if (msg.h > 255) msg.h = 255;
  if (msg.s < 0) msg.s = 0;
  if (msg.s > 255) msg.s = 255;
  if (msg.v < 0) msg.v = 0;
  if (msg.v > 255) msg.v = 255;

  // Send the light setting command
  console.log("Updating light color");
  this._serial.sendCustom(20, 0x03, new Buffer([0xc1, 3, msg.h, msg.s, msg.v]));

};

TelebotNode.prototype.handleJsonCmd_autodock = function (msg) {
  console.log("Autodock triggered!");
  // Clear the status tick interval as we're going to poll the
  // dock contact status in control model at a faster rate
  var self = this;
  if (this._tick_interval != null) {
    clearInterval(self._tick_interval);
    this._tick_interval = null;
  }
  this._model.autodock();
};

TelebotNode.prototype.handleJsonCmd_autodockCalibrate = function (msg) {
  console.log("Autodock calibration triggered!");
  if (msg.visionBasedCalibrationEnabled) {
    this._botshell.cmd_vb_autodock_calibrate_ll([]);
    return;
  }
  this._botshell.cmd_autodock_calibrate_ll([]);
};

TelebotNode.prototype.handleJsonCmd_updateAuxCamCalibrate = function (msg) {
  console.log("aux cam calibration triggered!", msg);
  const downCamCenter = msg.downCamCPoint;
  console.log("downCamCenter", downCamCenter);
  this._botshell.cmd_update_aux_cam_center_point([downCamCenter.centerX, downCamCenter.centerY]);
};

TelebotNode.prototype.handleJsonCmd_auxCamCalibrate = function (msg) {
  console.log("aux cam calibration triggered!");
  this._botshell.cmd_aux_cam_calibrate_ll([]);
};


TelebotNode.prototype.handleJsonCmd_startCvEngine = function (msg) {
  console.log("CV Engine start:", msg.tag);
  this._model.start_cv_engine(msg.tag);
};

TelebotNode.prototype.handleJsonCmd_stopCvEngine = function (msg) {
  console.log("CV Engine stop");
  this._model.stop_cv_engine();
};

TelebotNode.prototype.handleJsonCmd_say = function (msg) {
  console.log("say:", msg.cmdstring);
  this._botshell.cmd_say([msg.cmdstring]);
};

TelebotNode.prototype.handleJsonCmd_resetAutoexposure = function (msg) {
  console.log("Reset autoexposure!");
  this._model.reset_autoexposure(msg);
};

TelebotNode.prototype.handleJsonCmd_setAutoExposure = function (msg) {
  console.log("Set auto exposure!");
  this._model.set_autoexposure(msg);
};

TelebotNode.prototype.handleJsonCmd_setManualExposure = function (msg) {
  console.log("Set manual exposure!");
  this._model.set_manualexposure(msg);
};

TelebotNode.prototype.handleJsonCmd_setSpeakerVolume = function (msg) {
  console.log("Set speaker volume:", msg);
  this._api.cmd_setSpeakerVolume(msg);
};

TelebotNode.prototype.handleJsonCmd_setSpeakerEnabled = function (msg) {
  console.log("Set speaker enabled:", msg);
  this._api.cmd_setSpeakerEnabled(msg);
};

TelebotNode.prototype.handleJsonCmd_setMicLevel = function (msg) {
  console.log("Set mic level:", msg);
  this._api.cmd_setMicLevel(msg);
};

TelebotNode.prototype.handleJsonCmd_setMicEnabled = function (msg) {
  console.log("Set mic enabled:", msg);
  this._api.cmd_setMicEnabled(msg);
};

TelebotNode.prototype.handleJsonCmd_clickLook = function (msg) {
  this._model.look(msg.data.x, msg.data.y, msg.data.speed);
};

TelebotNode.prototype.handleJsonCmd_snapshot = function (msg) {

  // Trigger driver snapshot
  console.log("Triggering snapshot...");

  // For now just snap to the same temp file, we could do
  // random generated filenames if needed
  // TODO: more dynamic sizing from web request?
  const id = crypto.randomBytes(6).toString('hex');
  const spath = "/dev/libcamera/snap-" + id + ".jpg";

  // Use maximum default size for snapshot on all platforms


  const snapshotOptions = {
    width: 0,
    height: 0,
    fps: 0,
    ouputFile: spath,
    cameraType: 0 // main cam
  }

  this._snapshot_mgr.snapshot(snapshotOptions, () => {
    console.log("Snapshot done! Notifying app.");
    // Chmod it to system/system 0660 so that our
    // app can read it but not other stuff
    fs.chmodSync(spath, '0660');
    fs.chownSync(spath, 1000, 1000);

    // Now trigger the send stream
    this._api.sendJson({
      type: "snapdone",
      path: spath,
    });
  });

};

TelebotNode.prototype.handleJsonCmd_collisionStop = function (msg) {
  if (!this._lidarNode.isCollisionDetectionAvailable()) return;
  this._lidarNode.toggleCollisionDetection(msg.v);
  this._api.sendJson({
    type: 'CollisionStop',
    isEnable: msg.v
  });
}

TelebotNode.prototype.handleJsonCmd_zoom = function (msg) {
  // Fixed formats and calculations for supercam
  if (!this._model._ardockMgr.is_using_supercam()) return;

  // Sanitize zoom value
  var zlevel = parseInt(msg.value);
  if (zlevel < 0) zlevel = 0;
  if (zlevel > 256) zlevel = 256;

  // Handle disabling/turning off zoom
  if (zlevel == 0) {
    this._snapshot_mgr.format(1280, 960, 30);
    this._snapshot_mgr.zoom(0);
  } else {
    this._snapshot_mgr.format(3840, 2160, 30);
    this._snapshot_mgr.zoom(zlevel);

    // Also adjust driver behavior as well, fix at maximum
    // res and drop framerate instead
  }
};

// Arm control commands over ohmni API
TelebotNode.prototype.handleJsonCmd_torque = function (msg) {
  if (msg.sid == null || msg.v == null) return;
  this._botshell.cmd_torque([msg.sid, msg.v]);
};

// Arm control commands over ohmni API
TelebotNode.prototype.handleJsonCmd_vzero = function (msg) {
  this._botshell.cmd_vzero(["ok"]);
};

// Arm control commands over ohmni API
TelebotNode.prototype.handleJsonCmd_vpos_set = function (msg) {
  this._botshell.cmd_vpos_set([msg.sid, msg.v]);
};


// Arm control commands over ohmni API
TelebotNode.prototype.handleJsonCmd_vtarg_set = function (msg) {
  if (msg.sid == null || msg.v == null) return;
  this._botshell.cmd_vtarg_set([msg.sid, msg.v]);
};

// Arm control commands over ohmni API
TelebotNode.prototype.handleJsonCmd_vstate = function (msg) {
  if (msg.arr == null) return;
  this._botshell.cmd_vstate(msg.arr);
};

// Arm control commands over ohmni API
TelebotNode.prototype.handleJsonCmd_vstate_multi = function (msg) {
  if (msg.arr == null) return;
  this._botshell.cmd_vstate_multi(msg.arr);
};

// Arm control commands over ohmni API
// TODO: add semantic arguments OR array style
TelebotNode.prototype.handleJsonCmd_vparams = function (msg) {
  if (msg.arr == null) return;
  this._botshell.cmd_vparams(msg.arr);
};

// Arm control commands over ohmni API
// TODO: add semantic arguments OR array style
// TODO: lock this down for maximum safety
TelebotNode.prototype.handleJsonCmd_set_phase = function (msg) {
  if (msg.arr == null) return;
  // Restrict device range for now to accessories
  if (msg.arr[0] == null || msg.arr[0] <= 10 || msg.arr[0] >= 20) return;
  this._botshell.cmd_set_phase(msg.arr);
};

// Remote firmware update here
// Remember, only you can stop forest fire and callback hell!
TelebotNode.prototype.handleJsonCmd_fwUpdate = function (msg) {
  // Helpers here
  const self = this;
  const path = msg; // path to the firmware.bin for remote update
  console.log('Start remote firmware update.');

  // Exit if we don't get the correct path
  if (!fs.existsSync(path)) {
    console.log('Firmware file not found, abort.');
    return
  }
  console.log('Found firmware file for update.');

  // Stop our periodical base query first
  if (self._tick_interval != null) {
    clearInterval(self._tick_interval);
    this._tick_interval = null;
  }

  // Stop periodic signals sent from other classes as well
  self._model.stop();

  // Turn off torque on our drives individually in case of compatibility
  self._botshell.cmd_torque([0, 'off']);
  self._botshell.cmd_torque([1, 'off']);

  // Applying firmware function to be called later, planting a forest here
  const apply_fw = function () {
    // Get and clean up the version tag to something we can read
    const left_check = left_tag.split('-')[0];
    const right_check = right_tag.split('-')[0];
    console.log('Left firmware tag check is', left_check, '| Right firmware tag check is', right_check);

    // Check for version to see if we can update or not, only continue
    // when both drives are upgradable
    if (((parseInt(left_check) > 2016) || (left_check === 'stable'))
      && ((parseInt(right_check) > 2016) || (right_check === 'stable'))) {
      // Chaining the update, then resume normal operation once we are done
      console.log('Drive compatible, proceed to firmware update.');
      self._botshell.cmd_fw_update([0, path], function () {
        console.log('Finished updating left drive, proceed to right drive.');
        self._botshell.cmd_fw_update([1, path], function () {
          console.log('Finished updating right drive, proceed to resume normal operation.');
          resume();
        });
      });
    }

    // Resume anyway if we can't upgrade
    else {
      console.log('Firmware tag conflict, got', left_tag, 'for left drive and', right_tag, 'for right drive.');
      resume();
    }
  }

  // Turn on torque and resume the base query
  var resume = function () {
    console.log('Resume normal operation.');
    self._botshell.cmd_torque([0, 'on']);
    self._botshell.cmd_torque([1, 'on']);
    self._tick_interval = setInterval(function () {
      self.tick();
    }, self._tick_delay_ms);
    self._model.start();
  }

  // Tag query, wait a bit before sending the second message
  // request or we will recieve a conflict in serial bus.
  const left_tag = self._fw_version.left_drive;
  const right_tag = self._fw_version.right_drive;
  console.log('Left tag:', left_tag, '| Right tag:', right_tag);
  setTimeout(function () {
    if ((right_tag != null) && (left_tag != null)) apply_fw()
    else {
      console.log('No version tag info, abort.')
      resume();
    }
  }, 200);

}

// ======== Status ticks ========

TelebotNode.prototype.tick = function () {


  // Suppression is only used for testing when we
  // want to make sure we aren't sending latent queries
  // on the bus.  Be careful with this.
  if (this._disable_serial_queries) return;

  // Called from timer scope so protect against errors
  try {

    // Run async
    async.series([

      (cb) => {

        // Update battery info
        this._serial.sendBatteryQuery();
        setTimeout(cb, this._tick_delay_ms / 2);

      },
      (cb) => {

        // Query right drive odometry
        this._serial.sendCustom(1, 4, new Buffer([59, 4]));
        setTimeout(cb, 5);

      },
      (cb) => {

        // Query left drive odometry
        this._serial.sendCustom(0, 4, new Buffer([59, 4]));
        setTimeout(cb, 5);

      },
      (cb) => {

        // Query neck state. if not new neck, no-op
        this._model.neck_odometry_poll((state) => {

          // Old neck has no state
          if (state == null) return;

          // New neck - load into odo update to stream to UI side
          this._odo.n = state.pos;
          this._odo.nf = state.flags;

        });
        setTimeout(cb, 5);

      },
      (cb) => {

        // Now report odometry after poll cycle is complete
        this._api.sendJson(this._odo);
        setTimeout(cb, 0);

      }
    ]);

  } catch (e) {
    console.log("Error in tick update:", e.stack);
  }
};

// ======== Exports ========

module.exports = TelebotNode;

