#!/usr/bin/env python

# import normal packages
import platform
import logging
import sys
import os
import sys
if sys.version_info.major == 2:
    import gobject
else:
    from gi.repository import GLib as gobject
import sys
import json
import time
import configparser # for config/ini file
import paho.mqtt.client as mqtt
import requests # for http GET

try:
  import thread   # for daemon = True  / Python 2.x
except:
  import _thread as thread   # for daemon = True  / Python 3.x
import dbus

from threading import Thread

# our own packages from victron
sys.path.insert(1, os.path.join(os.path.dirname(__file__), '/opt/victronenergy/dbus-systemcalc-py/ext/velib_python'))
from vedbus import VeDbusService
from settingsdevice import SettingsDevice
from dbusmonitor import DbusMonitor


#formatting
_kwh = lambda p, v: (str(round(v, 2)) + 'KWh')
_a = lambda p, v: (str(round(v, 1)) + 'A')
_w = lambda p, v: (str(round(v, 1)) + 'W')
_v = lambda p, v: (str(round(v, 1)) + 'V')
_hz = lambda p, v: (str(round(v, 1)) + 'Hz')
_pct = lambda p, v: (str(round(v, 1)) + '%')
_c = lambda p, v: (str(round(v, 1)) + '°C')


class SystemBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SYSTEM)


class SessionBus(dbus.bus.BusConnection):
    def __new__(cls):
        return dbus.bus.BusConnection.__new__(cls, dbus.bus.BusConnection.TYPE_SESSION)


def dbusconnection():
    return SessionBus() if 'DBUS_SESSION_BUS_ADDRESS' in os.environ else SystemBus()


def new_service(base, type, physical, logical, id, instance):
    if instance == 0:
      self =  VeDbusService("{}.{}".format(base, type), dbusconnection())
    else:
      self =  VeDbusService("{}.{}.{}_id{:02d}".format(base, type, physical,  id), dbusconnection())
    # physical is the physical connection
    # logical is the logical connection to align with the numbering of the console display
    # Create the management objects, as specified in the ccgx dbus-api document
    self.add_path('/Mgmt/ProcessName', __file__)
    self.add_path('/Mgmt/ProcessVersion', 'Unkown version, and running on Python ' + platform.python_version())
    self.add_path('/Mgmt/Connection', logical)

    # Create the mandatory objects, note these may need to be customised after object creation
    self.add_path('/DeviceInstance', instance)
    self.add_path('/ProductId', 0)
    self.add_path('/ProductName', '')
    self.add_path('/FirmwareVersion', 0)
    self.add_path('/HardwareVersion', 0)
    self.add_path('/Connected', 0)  # Mark devices as disconnected until they are confirmed
    self.add_path('/Serial', 0)

    return self


def getConfig():
  config = configparser.ConfigParser()
  config.read("%s/config.ini" % (os.path.dirname(os.path.realpath(__file__))))
  return config;


################################################################################
#                                                                              #
#   Inverter                                                                   #
#                                                                              #
################################################################################

class DbusHmInverterService:
  def __init__(self, deviceinstance, dbusmonitor):

    self.settings = None
    self._inverterLoopCounter = 0
    self._deviceinstance = deviceinstance
    self._active = False
    self._inverterData = {}
    
    # Ahoy
    self._inverterData[0] = {}
    self._inverterData[0]['ch0/P_AC'] = 0
    self._inverterData[0]['ch0/U_AC'] = 0
    self._inverterData[0]['ch0/I_AC'] = 0
    self._inverterData[0]['ch0/P_DC'] = 0
    self._inverterData[0]['ch0/Freq'] = 0
    self._inverterData[0]['ch0/YieldTotal'] = 0
    self._inverterData[0]['ch1/U_DC'] = 0
    self._inverterData[0]['ch0/Efficiency'] = 0
    self._inverterData[0]['ch0/Temp'] = 0

    for i in range(1, 5):
      self._inverterData[0][f'ch{i}/I_DC'] = 0

    # OpenDTU
    self._inverterData[1] = {}
    self._inverterData[1]['0/power'] = 0
    self._inverterData[1]['0/voltage'] = 0
    self._inverterData[1]['0/current'] = 0
    self._inverterData[1]['0/powerdc'] = 0
    self._inverterData[1]['0/frequency'] = 0
    self._inverterData[1]['0/yieldtotal'] = 0
    self._inverterData[1]['1/voltage'] = 0
    self._inverterData[1]['0/efficiency'] = 0
    self._inverterData[1]['0/temperature'] = 0

    for i in range(1, 5):
      self._inverterData[1][f'{i}/current'] = 0

    self._dbus = dbusconnection()

    self._dbusmonitor = dbusmonitor
    
    self._init_device_settings(self._deviceinstance)

    self._MQTTName = "{}-{}".format(self._dbusmonitor.get_value('com.victronenergy.system','/Serial'),self._deviceinstance) 
    self._inverterPath = self.settings['/InverterPath']
    
    self._MQTTconnected = 0
    self._init_MQTT()

    base = 'com.victronenergy'

    # Create VE.Bus inverter
    if self.settings['/DTU'] == 0:
      dtu = "Ahoy"
    else:
      dtu = "OpenDTU"

    self._dbusservice = new_service(base, 'vebus', 'DTU', dtu, self._deviceinstance, self._deviceinstance)

    # Init the inverter
    self._initInverter()

    # add _inverterLoop function 'timer'
    gobject.timeout_add(500, self._inverterLoop) 
  

  ###############################
  # Private                     #
  ###############################


  def _initInverter(self):
    maxPower = self.settings['/MaxPower']

    paths = {
      '/Ac/ActiveIn/L1/V':                  {'initial': 0, 'textformat': _v},
      '/Ac/ActiveIn/L2/V':                  {'initial': 0, 'textformat': _v},
      '/Ac/ActiveIn/L3/V':                  {'initial': 0, 'textformat': _v},
      '/Ac/ActiveIn/L1/I':                  {'initial': 0, 'textformat': _a},
      '/Ac/ActiveIn/L2/I':                  {'initial': 0, 'textformat': _a},
      '/Ac/ActiveIn/L3/I':                  {'initial': 0, 'textformat': _a},
      '/Ac/ActiveIn/L1/F':                  {'initial': 0, 'textformat': _hz},
      '/Ac/ActiveIn/L2/F':                  {'initial': 0, 'textformat': _hz},
      '/Ac/ActiveIn/L3/F':                  {'initial': 0, 'textformat': _hz},
      '/Ac/ActiveIn/L1/P':                  {'initial': 0, 'textformat': _w},
      '/Ac/ActiveIn/L2/P':                  {'initial': 0, 'textformat': _w},
      '/Ac/ActiveIn/L3/P':                  {'initial': 0, 'textformat': _w},
      '/Ac/Inverter/L1/P':                  {'initial': 0, 'textformat': _w},
      '/Ac/Inverter/L2/P':                  {'initial': 0, 'textformat': _w},
      '/Ac/Inverter/L3/P':                  {'initial': 0, 'textformat': _w},
      '/Ac/Inverter/L1/I':                  {'initial': 0, 'textformat': _a},
      '/Ac/Inverter/L2/I':                  {'initial': 0, 'textformat': _a},
      '/Ac/Inverter/L3/I':                  {'initial': 0, 'textformat': _a},
      '/Ac/ActiveIn/P':                     {'initial': 0, 'textformat': _w},
      '/Ac/ActiveIn/Connected':             {'initial': 0, 'textformat': None},
      '/Ac/Out/L1/V':                       {'initial': 0, 'textformat': _v},
      '/Ac/Out/L2/V':                       {'initial': 0, 'textformat': _v},
      '/Ac/Out/L3/V':                       {'initial': 0, 'textformat': _v},
      '/Ac/Out/L1/I':                       {'initial': 0, 'textformat': _a},
      '/Ac/Out/L2/I':                       {'initial': 0, 'textformat': _a},
      '/Ac/Out/L3/I':                       {'initial': 0, 'textformat': _a},
      '/Ac/Out/L1/F':                       {'initial': 0, 'textformat': _hz},
      '/Ac/Out/L2/F':                       {'initial': 0, 'textformat': _hz},
      '/Ac/Out/L3/F':                       {'initial': 0, 'textformat': _hz},
      '/Ac/Out/L1/P':                       {'initial': 0, 'textformat': _w},
      '/Ac/Out/L2/P':                       {'initial': 0, 'textformat': _w},
      '/Ac/Out/L3/P':                       {'initial': 0, 'textformat': _w},
      '/Ac/ActiveIn/ActiveInput':           {'initial': 0, 'textformat': None},
      '/Ac/In/1/CurrentLimit':              {'initial': 0, 'textformat': None},
      '/Ac/In/1/CurrentLimitIsAdjustable':  {'initial': 0, 'textformat': None},
      '/Ac/In/2/CurrentLimit':              {'initial': 0, 'textformat': None},
      '/Ac/In/2/CurrentLimitIsAdjustable':  {'initial': 0, 'textformat': None},
      '/Settings/SystemSetup/AcInput1':     {'initial': 1, 'textformat': None},
      '/Settings/SystemSetup/AcInput2':     {'initial': 0, 'textformat': None},
      '/Ac/PowerMeasurementType':           {'initial': 4, 'textformat': None},
      '/Ac/State/IgnoreAcIn1':              {'initial': 0, 'textformat': None},
      '/Ac/State/IgnoreAcIn2':              {'initial': 0, 'textformat': None},

      '/Ac/Power':                          {'initial': 0, 'textformat': _w},
      '/Ac/Efficiency':                     {'initial': 0, 'textformat': _pct},
      '/Ac/PowerLimit':                     {'initial': maxPower, 'textformat': _w},
      '/Ac/MaxPower':                       {'initial': maxPower, 'textformat': _w},
      '/Ac/Energy/Forward':                 {'initial': None,     'textformat': _kwh},

      '/Ac/NumberOfPhases':                 {'initial': 3, 'textformat': None},
      '/Ac/NumberOfAcInputs':               {'initial': 1, 'textformat': None},

      '/Alarms/HighDcCurrent':              {'initial': 0, 'textformat': None},
      '/Alarms/HighDcVoltage':              {'initial': 0, 'textformat': None},
      '/Alarms/LowBattery':                 {'initial': 0, 'textformat': None},
      '/Alarms/PhaseRotation':              {'initial': 0, 'textformat': None},
      '/Alarms/Ripple':                     {'initial': 0, 'textformat': None},
      '/Alarms/TemperatureSensor':          {'initial': 0, 'textformat': None},
      '/Alarms/L1/HighTemperature':         {'initial': 0, 'textformat': None},
      '/Alarms/L1/LowBattery':              {'initial': 0, 'textformat': None},
      '/Alarms/L1/Overload':                {'initial': 0, 'textformat': None},
      '/Alarms/L1/Ripple':                  {'initial': 0, 'textformat': None},
      '/Alarms/L2/HighTemperature':         {'initial': 0, 'textformat': None},
      '/Alarms/L2/LowBattery':              {'initial': 0, 'textformat': None},
      '/Alarms/L2/Overload':                {'initial': 0, 'textformat': None},
      '/Alarms/L2/Ripple':                  {'initial': 0, 'textformat': None},
      '/Alarms/L3/HighTemperature':         {'initial': 0, 'textformat': None},
      '/Alarms/L3/LowBattery':              {'initial': 0, 'textformat': None},
      '/Alarms/L3/Overload':                {'initial': 0, 'textformat': None},
      '/Alarms/L3/Ripple':                  {'initial': 0, 'textformat': None},

      '/Dc/0/Power':                        {'initial': 0, 'textformat': _w},
      '/Dc/0/Current':                      {'initial': 0, 'textformat': _a},
      '/Dc/1/Power':                        {'initial': 0, 'textformat': _w},
      '/Dc/1/Current':                      {'initial': 0, 'textformat': _a},
      '/Dc/0/Voltage':                      {'initial': 0, 'textformat': _v},
      '/Dc/0/Temperature':                  {'initial': 0, 'textformat': None},

      '/Mode':                              {'initial': 0, 'textformat': None},
      '/ModeIsAdjustable':                  {'initial': 1, 'textformat': None},

      '/VebusChargeState':                  {'initial': 0, 'textformat': None},
      '/VebusSetChargeState':               {'initial': 0, 'textformat': None},

      '/Leds/Inverter':                     {'initial': 1, 'textformat': None},

      '/Bms/AllowToCharge':                 {'initial': 0, 'textformat': None},
      '/Bms/AllowToDischarge':              {'initial': 0, 'textformat': None},
      '/Bms/BmsExpected':                   {'initial': 0, 'textformat': None},
      '/Bms/Error':                         {'initial': 0, 'textformat': None},

      '/Soc':                               {'initial': 0, 'textformat': None},
      '/State':                             {'initial': 0, 'textformat': None},
      '/RunState':                          {'initial': 0, 'textformat': None},
      '/VebusError':                        {'initial': 0, 'textformat': None},
      '/Temperature':                       {'initial': 0, 'textformat': _c},

      '/Hub4/L1/AcPowerSetpoint':           {'initial': 0, 'textformat': None},
      '/Hub4/DisableCharge':                {'initial': 0, 'textformat': None},
      '/Hub4/DisableFeedIn':                {'initial': 0, 'textformat': None},
      '/Hub4/L2/AcPowerSetpoint':           {'initial': 0, 'textformat': None},
      '/Hub4/L3/AcPowerSetpoint':           {'initial': 0, 'textformat': None},
      '/Hub4/DoNotFeedInOvervoltage':       {'initial': 0, 'textformat': None},
      '/Hub4/L1/MaxFeedInPower':            {'initial': 0, 'textformat': None},
      '/Hub4/L2/MaxFeedInPower':            {'initial': 0, 'textformat': None},
      '/Hub4/L3/MaxFeedInPower':            {'initial': 0, 'textformat': None},
      '/Hub4/TargetPowerIsMaxFeedIn':       {'initial': 0, 'textformat': None},
      '/Hub4/FixSolarOffsetTo100mV':        {'initial': 0, 'textformat': None},
      '/Hub4/AssistantId':                  {'initial': 5, 'textformat': None},

      '/PvInverter/Disable':                {'initial': 0, 'textformat': None},
      '/SystemReset':                       {'initial': 0, 'textformat': None},
      '/Enabled':                           {'initial': 0, 'textformat': None},
    }

    # add path values to dbus
    self._dbusservice.add_path('/CustomName', self.settings['/Customname'], writeable=True, onchangecallback=self.customnameChanged)
    self._dbusservice.add_path('/Master', 0, writeable=False)
    for path, settings in paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handlechangedvalue)

    self._dbusservice['/ProductId'] = 0xFFF1
    self._dbusservice['/FirmwareVersion'] = 0x482
    self._dbusservice['/ProductName'] = 'Hoymiles'
    self._dbusservice['/Connected'] = 1


  def _handlechangedvalue(self, path, value):
    if path == '/Ac/PowerLimit':
      if self._dbusservice['/RunState'] >= 2:
        self._inverterSetPower(value)

    if path == '/Enabled':
      logging.debug("dbus_value_changed: %s %s" % (path, value,))
      if self._isMaster() == 1:
        return False
      else:
        if value == 1:
          self.settings['/Enabled'] = 1
          self._dbusservice['/Enabled'] = 1
        else:
          self.settings['/Enabled'] = 0
          self._dbusservice['/Enabled'] = 0
      self._checkInverterState()

    return True # accept the change


  def _dbusValueChanged(self,dbusServiceName, dbusPath, options, changes, deviceInstance):
    if dbusPath == '/VebusService':
      vesrevice = self._dbusmonitor.get_value('com.victronenergy.system','/VebusService')

      if vesrevice.endswith('_id{}'.format(self._deviceinstance)):
        self._dbusservice['/Master'] = 1
        self._dbusservice['/Enabled'] = 3
      else:
        self._dbusservice['/Master'] = 0
        self._dbusservice['/Enabled'] = self.settings['/Enabled']

    return


  def _init_device_settings(self, deviceinstance):
    if self.settings:
        return

    path = '/Settings/DTU/{}'.format(deviceinstance)

    SETTINGS = {
        '/Customname':                    [path + '/CustomName', 'HM-600', 0, 0],
        '/MaxPower':                      [path + '/MaxPower', 600, 0, 0],
        '/Phase':                         [path + '/Phase', 1, 1, 3],
        '/MqttUrl':                       [path + '/MqttUrl', '127.0.0.1', 0, 0],
        '/InverterPath':                  [path + '/InverterPath', 'inverter/HM-600', 0, 0],
        '/DTU':                           [path + '/DTU', 0, 0, 1],
        '/InverterID':                    [path + '/InverterID', 0, 0, 9],
        '/Enabled':                       [path + '/Enabled', 1, 0, 1],
    }

    self.settings = SettingsDevice(self._dbus, SETTINGS, self._setting_changed)


  def _setting_changed(self, setting, oldvalue, newvalue):
    logging.info("setting changed, setting: %s, old: %s, new: %s" % (setting, oldvalue, newvalue))

    if setting == '/Customname':
      self._dbusservice['/CustomName'] = newvalue

    elif setting == '/MaxPower':
      self._dbusservice['/Ac/MaxPower'] = newvalue
      
    elif setting == '/InverterPath':
      self._inverterPath = newvalue
      try:
        self._MQTTclient.connect(self.settings['/MqttUrl'])
      except Exception as e:
        logging.exception("Fehler beim connecten mit Broker")
        self._MQTTconnected = 0
    
    elif setting == '/MqttUrl':
      try:
        self._MQTTclient.connect(newvalue)
      except Exception as e:
        logging.exception("Fehler beim connecten mit Broker")
        self._MQTTconnected = 0

    elif setting == '/DTU':
      if self.settings['/DTU'] == 0:
        self._dbusservice['/Mgmt/Connection'] = "Ahoy"
      else:
        self._dbusservice['/Mgmt/Connection'] = "OpenDTU"
      try:
        self._MQTTclient.connect(self.settings['/MqttUrl'])
      except Exception as e:
        logging.exception("Fehler beim connecten mit Broker")
        self._MQTTconnected = 0


  def _checkInverterState(self):
    if self._dbusservice['/RunState'] == 0: # Inverter is switched off
      # Switch on inverter if activated
      if self._active == True and self.Enabled == True:
        self._inverterSetLimit(self._dbusservice['/Ac/PowerLimit'], True)
        self._dbusservice['/RunState'] = 1
        self._dbusservice['/State'] = 9
        self._inverterOn()
        return

      # Switch off inverter again if it is still running
      if self._dbusservice['/Ac/Power'] > 0:
        self._inverterOff()

    else: # Inverter is switched on
      if  self._dbusservice['/RunState'] == 1: # Start inverter
        if self._dbusservice['/Ac/Power'] == 0:
          self._inverterOn()
        else:
          self._dbusservice['/RunState'] = 2
        return

      # Switch off inverter if not activated
      if self._active == False or self.Enabled == False:
        self._inverterOff()
        self._dbusservice['/RunState'] = 0
        return


  def _inverterOn(self):
    logging.info("Inverter %s on" % (self._deviceinstance))
    self._MQTTclient.publish(self._inverterControlPath('power'), 1)
    

  def _inverterOff(self):
    logging.info("Inverter %s off" % (self._deviceinstance))
    self._MQTTclient.publish(self._inverterControlPath('power'), 0)
    self._dbusservice['/State'] = 0


  def _inverterSetLimit(self, newLimit, force=False):
    if self._dbusservice['/RunState'] >= 1 or force:
      self._inverterSetPower(newLimit, force)
    self._dbusservice['/Ac/PowerLimit'] = newLimit


  def _inverterSetPower(self, power, force=False):
    newPower      = int(power)
    currentPower  = int(self._dbusservice['/Ac/PowerLimit'] )

    if newPower != currentPower or force == True:
      self._MQTTclient.publish(self._inverterControlPath('limit_nonpersistent_absolute'), newPower)
      

  def _inverterLoop(self):
    try:
      # 0.5s interval
      self._inverterLoopCounter +=1
      self._inverterUpdate()
      
      # 20s interval
      if self._inverterLoopCounter % 40 == 0:
        self._checkInverterState()

      # 5min interval
      if self._inverterLoopCounter % 600 == 0:
        self._inverterLoopCounter = 0

        if self._dbusservice['/RunState'] > 1:
          self._inverterSetPower(self._dbusservice['/Ac/PowerLimit'], True)

    except Exception as e:
      logging.critical('Error at %s', '_inverterLoop', exc_info=e)

    return True


  def _inverterUpdate(self):
    try:

      pvinverter_phase = 'L' + str(self.settings['/Phase'])        

      if self.settings['/DTU'] == 0:
        # Ahoy
        powerAC     = self._inverterData[0]['ch0/P_AC']
        voltageAC   = self._inverterData[0]['ch0/U_AC']
        currentAC   = self._inverterData[0]['ch0/I_AC']
        frequency   = self._inverterData[0]['ch0/Freq']
        yieldTotal  = self._inverterData[0]['ch0/YieldTotal']
        efficiency  = self._inverterData[0]['ch0/Efficiency']
        volatageDC  = self._inverterData[0]['ch1/U_DC']
        powerDC     = self._inverterData[0]['ch0/P_DC']
        temperature = self._inverterData[0]['ch0/Temp']
        currentDC = 0
        for i in range(1, 5):
          currentDC -= self._inverterData[0][f'ch{i}/I_DC']
      else:
        # OpenDTU
        powerAC     = self._inverterData[1]['0/power']
        voltageAC   = self._inverterData[1]['0/voltage']
        currentAC   = self._inverterData[1]['0/current']
        frequency   = self._inverterData[1]['0/frequency']
        yieldTotal  = self._inverterData[1]['0/yieldtotal']
        efficiency  = self._inverterData[1]['0/efficiency']
        volatageDC  = self._inverterData[1]['1/voltage']
        powerDC     = self._inverterData[1]['0/powerdc']
        temperature = self._inverterData[1]['0/temperature']
        currentDC = 0
        for i in range(1, 5):
          currentDC -= self._inverterData[1][f'{i}/current']

      #send data to DBus
      for phase in ['L1', 'L2', 'L3']:
        pre1 = '/Ac/ActiveIn/' + phase
        pre2 = '/Ac/Inverter/' + phase

        if phase == pvinverter_phase:
          self._dbusservice[pre1 + '/V'] = voltageAC
          self._dbusservice[pre2 + '/I'] = currentAC
          self._dbusservice[pre2 + '/P'] = powerAC
          self._dbusservice[pre1 + '/F'] = frequency

        else:
          self._dbusservice[pre1 + '/V'] = 0
          self._dbusservice[pre2 + '/I'] = 0
          self._dbusservice[pre2 + '/P'] = 0
          self._dbusservice[pre1 + '/F'] = 0

      self._dbusservice['/Ac/Power'] = powerAC
      self._dbusservice['/Ac/Energy/Forward'] = yieldTotal
      self._dbusservice['/Ac/Efficiency'] = efficiency

      self._dbusservice['/Dc/1/Current'] = currentDC
      self._dbusservice['/Dc/0/Voltage'] = volatageDC
      self._dbusservice['/Dc/1/Power'] = powerDC

      self._dbusservice['/Temperature'] = temperature

    except Exception as e:
      logging.critical('Error at %s', '_update', exc_info=e)

    return True


  def _init_MQTT(self):
    self._MQTTclient = mqtt.Client(self._MQTTName) # create new instance
    self._MQTTclient.on_disconnect = self._on_MQTT_disconnect
    self._MQTTclient.on_connect = self._on_MQTT_connect
    self._MQTTclient.on_message = self._on_MQTT_message
    try:
      self._MQTTclient.connect(self.settings['/MqttUrl'])  # connect to broker
      self._MQTTclient.loop_start()
    except Exception as e:
      logging.exception("Fehler beim connecten mit Broker")
      self._MQTTconnected = 0


  def _on_MQTT_disconnect(self, client, userdata, rc):
    print("Client Got Disconnected")
    if rc != 0:
        print('Unexpected MQTT disconnection. Will auto-reconnect')

    else:
        print('rc value:' + str(rc))

    try:
        print("Trying to Reconnect")
        client.connect(self.settings['/MqttUrl'])
        self._MQTTconnected = 1
    except Exception as e:
        logging.exception("Fehler beim reconnecten mit Broker")
        print("Error in Retrying to Connect with Broker")
        self._MQTTconnected = 0
        print(e)


  def _on_MQTT_connect(self, client, userdata, flags, rc):
    if rc == 0:
        self._MQTTconnected = 1

        for k,v in self._inverterData[self.settings['/DTU']].items():
          client.subscribe(f'{self._inverterPath}/{k}')

    else:
        print("Failed to connect, return code %d\n", rc)


  def _on_MQTT_message(self, client, userdata, msg):
      try:       
        for k,v in self._inverterData[self.settings['/DTU']].items():
          if msg.topic == f'{self._inverterPath}/{k}':
            self._inverterData[self.settings['/DTU']][k] = float(msg.payload)
            return

      except Exception as e:
          logging.critical('Error at %s', '_update', exc_info=e)


  def _inverterControlPath(self, setting):
    if self.settings['/DTU'] == 0:
      # Ahoy
      ID = self.settings['/InverterID']
      path = '/'.join(self._inverterPath.split('/')[:-1])
      return path + f'/ctrl/{setting}/{ID}'
    else:
      # OpenDTU
      return self._inverterPath + f'/cmd/{setting}'


  def _getMaxPower(self):
    if self.Enabled == False:
       return 0
    else:
      return self._dbusservice['/Ac/MaxPower']


  def _getMinPower(self):
    if self.Enabled == False:
       return 0
    else:
      return self._dbusservice['/Ac/MaxPower'] * 0.05


  def _getPowerLimit(self):
    if self.Enabled == False:
       return 0
    else:
      return self._dbusservice['/Ac/PowerLimit']
   
  
  def _setActive(self, active):
    self._active = active
    self._checkInverterState()
    return True


  def _getActive(self):
    return self._active


  def _isMaster(self):
    return self._dbusservice['/Master'] 


  def _getEnabled(self):
    if self._dbusservice['/Enabled'] == 1 or self._dbusservice['/Enabled'] == 3:
      return True
    else:
      return False


  ###############################
  # Public                      #
  ###############################


  MaxPower = property(fget=_getMaxPower)
  MinPower = property(fget=_getMinPower)
  PowerLimit = property(fget=_getPowerLimit)
  Active = property(fget=_getActive, fset=_setActive)
  IsMaster = property(fget=_isMaster)
  Enabled = property(fget=_getEnabled)


  def getDbusservice(self,path):
    return self._dbusservice[path]


  def setDbusservice(self,path,value):
    self._dbusservice[path] = value
    return True


  def setPowerLimit(self,newLimit):
    newLimit = int(min(newLimit, self._dbusservice['/Ac/MaxPower']))
    newLimit = int(max(newLimit, self._dbusservice['/Ac/MaxPower'] * 0.05))
    logging.debug("Inverter %s limit: %s" % (self._deviceinstance,newLimit))
    self._inverterSetLimit(newLimit)
    
    return self._dbusservice['/Ac/PowerLimit']


  def customnameChanged(self, path, val):
    self.settings['/Customname'] = val
    return True


################################################################################
#                                                                              #
#   Inverter Control                                                           #
#                                                                              #
################################################################################

class hmControl:
  def __init__(self):
    self.settings = None
    self._controlLoopCounter = 0
    self._pvPowerAvg =  [0] * 20 * 15
    self._gridPower = 0
    self._gridPowerAvg =  [0] * 6
    self._loadPower = 0
    self._loadPowerHistory =  [600] * 30
    self._loadPowerMin = [600]*40
    self._powerLimitCounter = 10
    self._dbus = dbusconnection()
    self._powerMeterService = None

    self._devices = []
    self._initDbusMonitor()
    self._initDeviceSettings()

    self._dbusservice = new_service('com.victronenergy', 'hm', 'hmControl', 'hmControl', 0, 0)
    self._initDbusservice()

    self._refreshAcloads()

    self._checkState()

    # add _controlLoop function 'timer'
    gobject.timeout_add(500, self._controlLoop)


  ###############################
  # Private                     #
  ###############################


  def _initDbusservice(self):
    paths = {
      '/AvailableAcLoads':      {'initial': '', 'textformat': None},
      '/StartLimit':            {'initial': 0, 'textformat': None},
      '/State':                 {'initial': 0, 'textformat': None},
      '/PvAvgPower':            {'initial': 0, 'textformat': _w},
      '/Ac/Power':              {'initial': 0, 'textformat': _a},
      #'/Debug0':                {'initial': 0, 'textformat': None},
      #'/Debug1':                {'initial': 0, 'textformat': None},
      #'/Debug2':                {'initial': 50, 'textformat': None},
      #'/Debug3':                {'initial': 50, 'textformat': None},
    }

    # add path values to dbus
    for path, settings in paths.items():
      self._dbusservice.add_path(
        path, settings['initial'], gettextcallback=settings['textformat'], writeable=True, onchangecallback=self._handleChangedValue)

    self._dbusservice['/ProductId'] = 0xFFF1
    self._dbusservice['/FirmwareVersion'] = 1
    self._dbusservice['/ProductName'] = 'hmControl'
    self._dbusservice['/Connected'] = 1


  def _handleChangedValue(self, path, value):
    logging.info("dbus_value_changed: %s %s" % (path, value,))
    return True


  def _controlLoop(self):
    try:
      # 0.5s interval
      self._controlLoopCounter +=1
      self._powerLimitCounter +=1
      
      self._updateVebusTotal()
      self._getSystemPower()
      self._calcLimit()

      # 5min interval
      if self._controlLoopCounter % 600 == 0:
        self._controlLoopCounter = 0
        self._checkState()
        

    except Exception as e:
      logging.critical('Error at %s', '_inverterLoop', exc_info=e)

    return True


  def _initDbusMonitor(self):
    dummy = {'code': None, 'whenToLog': 'configChange', 'accessLevel': None}
    dbus_tree = {
      'com.victronenergy.settings': { # Not our settings
        '/Settings/CGwacs/BatteryLife/State': dummy,
      },
      'com.victronenergy.system': {
        '/Dc/Battery/Soc': dummy,
        '/Dc/Pv/Power': dummy,
        '/Ac/Consumption/L1/Power': dummy,
        '/Ac/Consumption/L2/Power': dummy,
        '/Ac/Consumption/L3/Power': dummy,
        '/Ac/Grid/L1/Power': dummy,
        '/Ac/Grid/L2/Power': dummy,
        '/Ac/Grid/L3/Power': dummy,
        '/VebusService': dummy,
      },
      'com.victronenergy.hub4': {
        '/PvPowerLimiterActive': dummy,
      },
      'com.victronenergy.acload': {
        '/Ac/Power': dummy,
        '/Ac/L1/Power': dummy,
        '/Ac/L2/Power': dummy,
        '/Ac/L3/Power': dummy,
        '/CustomName': dummy,
        '/ProductName': dummy,
        '/DeviceInstance': dummy,
        '/Connected': dummy,
      },
    }
    self._dbusmonitor = DbusMonitor(dbus_tree, valueChangedCallback=self._dbusValueChanged, deviceAddedCallback= self._dbusDeviceAdded, deviceRemovedCallback=self._dbusDeviceRemoved)


  def _dbusValueChanged(self,dbusServiceName, dbusPath, options, changes, deviceInstance):
    for device in self._devices:
      device._dbusValueChanged(dbusServiceName, dbusPath, options, changes, deviceInstance)

    if dbusPath in {'/Dc/Battery/Soc','/Settings/CGwacs/BatteryLife/State','/Hub','/PvPowerLimiterActive'}:
      logging.info("dbus_value_changed: %s %s %s" % (dbusServiceName, dbusPath, changes['Value']))

    if dbusPath == '/Settings/CGwacs/BatteryLife/State' or dbusPath == '/Dc/Battery/Soc':
      self._checkState()        

    elif dbusPath == '/Connected':
      self._refreshAcloads()

    elif dbusPath == '/VebusService':
      self._devices.sort(reverse=True, key=lambda x: x.IsMaster)

      for device in self._devices:
        logging.debug("Device: %s  Master: %s" % (device.getDbusservice('/DeviceInstance'), device.IsMaster))

    return

      
  def _dbusDeviceAdded(self,dbusservicename, instance):
    logging.info("dbus device added: %s %s " % (dbusservicename, instance))
    self._refreshAcloads()
    return


  def _dbusDeviceRemoved(self,dbusservicename, instance):
    logging.info("dbus device removed: %s %s " % (dbusservicename, instance))
    self._refreshAcloads()
    return


  def _initDeviceSettings(self):
    if self.settings:
        return

    path = '/Settings/DTU/Control'

    SETTINGS = {
        '/StartLimit':                    [path + '/StartLimit', 0, 0, 1],
        '/StartLimitMin':                 [path + '/StartLimitMin', 50, 50, 500],
        '/StartLimitMax':                 [path + '/StartLimitMax', 500, 100, 2000],
        '/LimitMode':                     [path + '/LimitMode', 0, 0, 2],
        '/PowerMeterInstance':            [path + '/PowerMeterInstance', 0, 0, 0],
        '/GridTargetDevMin':              [path + '/GridTargetDevMin', 25, 5, 100],
        '/GridTargetDevMax':              [path + '/GridTargetDevMax', 25, 5, 100],
        '/GridTargetPower':               [path + '/GridTargetPower', 25, -100, 200],
        '/GridTargetInterval':            [path + '/GridTargetInterval', 15, 3, 60],
        '/BaseLoadPeriod':                [path + '/BaseLoadPeriod', 0.5, 0.5, 10],
        '/Settings/SystemSetup/AcInput1': ['/Settings/SystemSetup/AcInput1', 1, 0, 1],
        '/Settings/SystemSetup/AcInput2': ['/Settings/SystemSetup/AcInput2', 0, 0, 1],
    }

    self.settings = SettingsDevice(self._dbus, SETTINGS, self._settingChanged)


  def _settingChanged(self, setting, oldvalue, newvalue):
    logging.info("setting changed, setting: %s, old: %s, new: %s" % (setting, oldvalue, newvalue))

    if setting == '/PowerMeterInstance':
      self._refreshAcloads()

    elif setting == '/StartLimit' or setting == '/StartLimitMax':
      self._checkStartLimit()


  def _updateVebusTotal(self):
    inverterTotalPower = [0] * 3
    inverterTotalCurrent = [0] * 3
    inverterTotalPowerDC = 0
    inverterTotalCurrentDC = 0

    if self._powerMeterService != None:
      self._dbusservice['/Ac/Power'] =  self._dbusmonitor.get_value(self._powerMeterService,'/Ac/Power') or 0
      for i in range(0,3):
        inverterTotalPower[i] = self._dbusmonitor.get_value(self._powerMeterService,f'/Ac/L{i+1}/Power') or 0
        inverterTotalCurrent[i] = self._dbusmonitor.get_value(self._powerMeterService,f'/Ac/L{i+1}/Current') or 0
    else:
      for device in self._devices:
        for i in range(0,3):
          inverterTotalPower[i] += device.getDbusservice(f'/Ac/Inverter/L{i+1}/P')
          inverterTotalCurrent[i] += device.getDbusservice(f'/Ac/Inverter/L{i+1}/I')
      self._dbusservice['/Ac/Power'] = sum(inverterTotalPower)

    for device in self._devices:
      inverterTotalPowerDC += device.getDbusservice('/Dc/1/Power')
      inverterTotalCurrentDC += device.getDbusservice('/Dc/1/Current')

    for i in range(0,3):
      self._devices[0].setDbusservice(f'/Ac/ActiveIn/L{i+1}/P', 0 - inverterTotalPower[i])
      self._devices[0].setDbusservice(f'/Ac/ActiveIn/L{i+1}/I', 0 - inverterTotalCurrent[i])
    self._devices[0].setDbusservice('/Ac/ActiveIn/P', 0 - self._dbusservice['/Ac/Power'])
    self._devices[0].setDbusservice('/Dc/0/Power', inverterTotalPowerDC)
    self._devices[0].setDbusservice('/Dc/0/Current', inverterTotalCurrentDC)


  def _getSystemPower(self):

    self._gridPower = self._dbusmonitor.get_value('com.victronenergy.system','/Ac/Grid/L1/Power') + \
                      self._dbusmonitor.get_value('com.victronenergy.system','/Ac/Grid/L2/Power') + \
                      self._dbusmonitor.get_value('com.victronenergy.system','/Ac/Grid/L3/Power')
    self._loadPower = self._dbusmonitor.get_value('com.victronenergy.system','/Ac/Consumption/L1/Power') + \
                      self._dbusmonitor.get_value('com.victronenergy.system','/Ac/Consumption/L2/Power') + \
                      self._dbusmonitor.get_value('com.victronenergy.system','/Ac/Consumption/L3/Power')

    self._gridPowerAvg.pop(0)
    self._gridPowerAvg.append(self._gridPower)

    self._loadPowerHistory.pop(len(self._loadPowerHistory)-1)
    self._loadPowerHistory.insert(0,self._loadPower)

    #5s interval
    if self._controlLoopCounter % 10 == 0:
      self._pvPowerAvg.pop(0)
      self._pvPowerAvg.append(self._dbusmonitor.get_value('com.victronenergy.system','/Dc/Pv/Power') or 0)

    # 15s interval
    if self._controlLoopCounter % 30 == 0:
      self._loadPowerMin.pop(len(self._loadPowerMin)-1)
      self._loadPowerMin.insert(0,min(self._loadPowerHistory))

    # 60s interval
    if self._controlLoopCounter % 120 == 0:
      self._dbusservice['/PvAvgPower'] = int(sum(self._pvPowerAvg) / len(self._pvPowerAvg))
      #self._dbusservice['/PvAvgPower'] = self._dbusservice['/Debug3']


  def _calcLimit(self):
    
    if self._dbusservice['/State'] != 0:
      
      # 1min interval
      if self._controlLoopCounter % 120 == 0:
        self._checkStartLimit()

      # Maximum power
      if self.settings['/LimitMode'] == 0:
        # 30s interval
        if self._controlLoopCounter % 60 == 0:
          newTarget = 0 
          for device in self._devices:
            newTarget += device.MaxPower
          self._setLimit(newTarget)

      # Grid target limit mode
      if self.settings['/LimitMode'] == 1 and self._powerLimitCounter >= self.settings['/GridTargetInterval'] * 2:
        if self._gridPower < self.settings['/GridTargetPower'] - self.settings['/GridTargetDevMin'] \
        or self._gridPower > self.settings['/GridTargetPower'] + self.settings['/GridTargetDevMax']:

          if self._gridPower < self.settings['/GridTargetPower'] - 2 * self.settings['/GridTargetDevMin']:
            gridPowerTarget = self._gridPower
          else:
            gridPowerTarget = sum(self._gridPowerAvg) / len(self._gridPowerAvg)

          #self._dbusservice['/Debug0']  =  self._gridPower
          #self._dbusservice['/Debug1']  =  gridPowerTarget

          newTarget = self._dbusservice['/Ac/Power'] + gridPowerTarget - self.settings['/GridTargetPower']
          self._setLimit(newTarget)

      # Base load limit mode
      if self.settings['/LimitMode'] == 2:
        #self._dbusservice['/Debug0']  =  self._gridPower
        if self._gridPower < 0 and self._powerLimitCounter >= 8:
          newTarget = self._actualLimit() + self._gridPower - 10
          logging.debug("set limit1: %s" % (newTarget))
          self._setLimit(newTarget)

        # 15s interval
        if self._controlLoopCounter % 30 == 0:
          newTarget = min(self._loadPowerMin[0:int(self.settings['/BaseLoadPeriod'] * 4)]) - 10
          if newTarget > self._actualLimit():
            logging.debug("set limit2: %s" % (newTarget))
            self._setLimit(newTarget)


  def _checkState(self):
    if self._batteryLifeIsSelfConsumption() == True and self._dbusservice['/State'] == 0:
      if self.settings['/StartLimit'] == 1 and self._dbusservice['/PvAvgPower'] > 10:
        self._dbusservice['/StartLimit'] = self.settings['/StartLimitMin']
        self._checkStartLimit()
      else:
        for device in self._devices:
          device.setPowerLimit(1)
          device.Active = True
      self._dbusservice['/State'] = 1

    elif self._batteryLifeIsSelfConsumption() == False and self._dbusservice['/State'] != 0:
      for device in self._devices:
        device.Active = False
      self._dbusservice['/StartLimit'] = 0
      self._dbusservice['/State'] = 0

    elif self._batteryLifeIsSelfConsumption() == False:
      for device in self._devices:
        device.Active = False
      

  def _checkStartLimit(self):    
    if self._dbusservice['/StartLimit'] == 0:
      return False
    
    newLimit = max(self.settings['/StartLimitMin'], int(self._dbusservice['/PvAvgPower'] * 0.95))
    
    # Check end of StartLimit mode
    if newLimit >= self.settings['/StartLimitMax'] or self.settings['/StartLimit'] == 0 \
      or self._dbusmonitor.get_value('com.victronenergy.settings','/Settings/CGwacs/BatteryLife/State') == 9:
        # Activate all inverter
        for device in self._devices:
          if device.Active == False:
            device.setPowerLimit(1)
            device.Active = True
        self._dbusservice['/StartLimit'] = 0
        logging.info("Start limit off.")
        return False

    # Check available inverter power 
    if self._availablePower() < newLimit:
      for device in self._devices:
        if device.Active == False:
          # Activate next inverter
          device.setPowerLimit(1)
          device.Active = True 
          if self._availablePower() >= newLimit:
            break
    else:
      for device in self._devices[::-1]:
        if device.Active == True:
          if self._availablePower() - device.MaxPower > newLimit:
            # Deactivate last inverter
            device.Active = False
          else:
            break 

    if self._availablePower() < newLimit:
      #Start limit is higher than available inverter power, exit start limit mode
      self._dbusservice['/StartLimit'] = 0
      logging.info("Start limit off.")
      return False

    self._dbusservice['/StartLimit'] = newLimit
    
    return True


  def _setLimit(self, newLimit):
    primaryMaxPower = self._devices[0].MaxPower
    primaryMinPower = self._devices[0].MinPower
    primaryPowerLimit = self._devices[0].PowerLimit
    secondaryMinPower = 0
    secondaryMaxPower = 0
    secondaryPowerLimit = 0
    limitSet = 0

    if self._dbusservice['/StartLimit'] > 0:
      newLimit = min(newLimit, self._dbusservice['/StartLimit'])

    for i in range(1, len(self._devices)):
      if self._devices[i].Active == True:
        secondaryMaxPower += self._devices[i].MaxPower
        secondaryMinPower += self._devices[i].MinPower
        secondaryPowerLimit += self._devices[i].PowerLimit

    if newLimit > primaryMaxPower + secondaryMaxPower and primaryMaxPower + secondaryMaxPower == primaryPowerLimit + secondaryPowerLimit \
      or newLimit ==  primaryPowerLimit + secondaryPowerLimit:
        return 

    self._powerLimitCounter = 0

    if newLimit >= primaryMaxPower + secondaryMaxPower:
      for device in self._devices:
        if device.Active == True:
          limitSet += device.setPowerLimit(device.MaxPower)
    
    elif newLimit <= primaryMinPower + secondaryMinPower:
      for device in self._devices:
        if device.Active == True:
          limitSet += device.setPowerLimit(device.MinPower)

    else:
      if primaryMaxPower >= newLimit - secondaryPowerLimit and primaryMinPower <= newLimit - secondaryPowerLimit:
        limitSet += self._devices[0].setPowerLimit(newLimit - secondaryPowerLimit)
        limitSet += secondaryPowerLimit
      
      elif newLimit <= (primaryMaxPower/2 + secondaryMinPower):
        for i in range(1, len(self._devices)):
          if self._devices[i].Active == True:
            limitSet += self._devices[i].setPowerLimit(self._devices[i].MinPower)
        limitSet += self._devices[0].setPowerLimit(newLimit - limitSet)
      
      elif newLimit >= (primaryMaxPower/2 + secondaryMaxPower):
        for i in range(1, len(self._devices)):
          if self._devices[i].Active == True:
            limitSet += self._devices[i].setPowerLimit(self._devices[i].MaxPower)
        limitSet += self._devices[0].setPowerLimit(newLimit - limitSet)
      
      else:
        for i in range(1, len(self._devices)):
          if self._devices[i].Active == True:
            p = int((newLimit - primaryMaxPower/2) * self._devices[i].MaxPower / secondaryMaxPower)
            limitSet += self._devices[i].setPowerLimit(p)
        limitSet += self._devices[0].setPowerLimit(newLimit - limitSet)
    
    #self._dbusservice['/Debug2'] = limitSet


  def _actualLimit(self):
    actualLimit =0
    for device in self._devices:
      if device.Active == True:
        actualLimit += device.PowerLimit
    
    return actualLimit


  def _availablePower(self):
    availablePower = 0
    for device in self._devices:
      if device.Active == True:
        availablePower += device.MaxPower
    
    return availablePower


  def _batteryLifeIsSelfConsumption(self):
    # Optimized mode with BatteryLife:
    # 1: Value set by the GUI when BatteryLife is enabled. Hub4Control uses it to find the right BatteryLife #   state (values 2-7) based on system state
    # 2: Self consumption
    # 3: Self consumption, SoC exceeds 85%
    # 4: Self consumption, SoC at 100%
    # 5: SoC below BatteryLife dynamic SoC limit
    # 6: SoC has been below SoC limit for more than 24 hours. Charging with battery with 5amps
    # 7: Multi/Quattro is in sustain
    # 8: Recharge, SOC dropped 5% or more below MinSOC.
    if self._dbusmonitor.get_value('com.victronenergy.settings','/Settings/CGwacs/BatteryLife/State') in (2, 3, 4):
      return True

    # Keep batteries charged mode:
    # 9: 'Keep batteries charged' mode enabled
    if self._dbusmonitor.get_value('com.victronenergy.settings','/Settings/CGwacs/BatteryLife/State') == 9 \
    and self._dbusmonitor.get_value('com.victronenergy.system','/Dc/Battery/Soc') > 95 and self._dbusservice['/State'] != 0:
      return True

    if self._dbusmonitor.get_value('com.victronenergy.settings','/Settings/CGwacs/BatteryLife/State') == 9 \
    and self._dbusmonitor.get_value('com.victronenergy.system','/Dc/Battery/Soc') == 100:
      return True

    # Optimized mode without BatteryLife:
    # 10: Self consumption, SoC at or above minimum SoC
    # 11: Self consumption, SoC is below minimum SoC
    # 12: Recharge, SOC dropped 5% or more below minimum SoC
    if self._dbusmonitor.get_value('com.victronenergy.settings','/Settings/CGwacs/BatteryLife/State') == 10:
      return True

    return False


  def _refreshAcloads(self):
    availableAcLoads = []
    powerMeterService = None
    deviceName = ''

    for service in self._dbusmonitor.get_service_list('com.victronenergy.acload'):
      logging.debug("acload: %s %s %s" % (service, self._dbusmonitor.get_value(service,'/CustomName'), self._dbusmonitor.get_value(service,'/DeviceInstance')))
      if self._dbusmonitor.get_value(service,'/CustomName') == None:
        deviceName = self._dbusmonitor.get_value(service,'/ProductName')
      else:
        deviceName = self._dbusmonitor.get_value(service,'/CustomName')

      availableAcLoads.append(deviceName+':'+str(self._dbusmonitor.get_value(service,'/DeviceInstance')))
      if self._dbusmonitor.get_value(service,'/DeviceInstance') == self.settings['/PowerMeterInstance'] and self._dbusmonitor.get_value(service,'/Connected') == 1:
         powerMeterService = service
      
    self._powerMeterService = powerMeterService
    self._dbusservice['/AvailableAcLoads'] = availableAcLoads


  ###############################
  # Public                      #
  ###############################


  def addDevice(self,deviceinstance):
    newDevice = DbusHmInverterService(deviceinstance, self._dbusmonitor)
    
    if self._dbusservice['/State'] != 0:
      newDevice.setPowerLimit(1)
      newDevice.Active = True
    else:
      newDevice.Active = False

    self._devices.append(newDevice)

        
################################################################################
#                                                                              #
#   Main                                                                       #
#                                                                              #
################################################################################

def main():
  #configure logging
  logging.basicConfig(      format='%(asctime)s,%(msecs)d %(name)s %(levelname)s %(message)s',
                            datefmt='%Y-%m-%d %H:%M:%S',
                            level=logging.INFO,
                            handlers=[
                                logging.FileHandler("%s/current.log" % (os.path.dirname(os.path.realpath(__file__)))),
                                logging.StreamHandler()
                            ])
  thread.daemon = True # allow the program to quit

  try:
      logging.info("Start")

      from dbus.mainloop.glib import DBusGMainLoop
      # Have a mainloop, so we can send/receive asynchronous calls to and from dbus
      DBusGMainLoop(set_as_default=True)
      
      logging.info('Connected to dbus, and switching over to gobject.MainLoop() (= event based)')
      mainloop = gobject.MainLoop()
      #start our main-service

      config = getConfig()

      vebus = hmControl()

      for section in config.sections()[::-1]:
        if config.has_option(section, 'Deviceinstance') == True:
          vebus.addDevice(int(config[section]['Deviceinstance']))

      #logging.getLogger().setLevel(logging.DEBUG)      
      mainloop.run()

  except Exception as e:
    logging.critical('Error at %s', 'main', exc_info=e)

if __name__ == "__main__":
  main()
