#!/usr/bin/env python3
#
# This script reads in a JSON file containing vehicle data. Our job is to
# expose this data to prometheus for scraping. Since the JSON data is in
# a tree'ish structure, we iterate through the fields to build up the full
# metric name. For example,
#
#  {
#    "response": {
#      ...
#      "charge_state": {
#        "battery_level": 67,
#        "battery_range": 238.89,
#        ...
#      }
#    }
#  }
#
# In the above example, the metrics exposed are formatted,
#
#   charge_state_battery_level 67
#   charge_state_battery_range 238.89
#
# The various data types we encounter in the various JSON fields need to be
# modified such that all metrics expose either integer or floats. For JSON
# fields that are neither integer nor float, consider the following examples,
#
#   "battery_heater_on": false
#   "fast_charger_brand": "<invalid>"
#   "not_enough_power_to_heat": null
#   "car_version": "2023.6.9 8b27e21d9137"
#
#  The above fields will be exposed as,
#
#   battery_heater_on 0
#   fast_charger_brand{value="invalid"} 1
#   not_enough_power_to_heat 0
#   car_version{value="2023.6.9 8b27e21d9137"} 1
#
# When configuring prometheus to scrape these metrics, we typically want the
# "instance" label to reflect the name of the vehicle, and not the host
# where this script is running on. Thus, consider the following prometheus
# config block which uses "metric_relabel_configs" to replace the "instance"
# label with the name of the vehicle (ie, "slowpoke") when scraping the host
# "192.168.20.20",
#
#  - job_name: 'car'
#    static_configs:
#      - targets:
#        - 192.168.20.20:9100
#    metric_relabel_configs:
#      - target_label: instance
#        replacement: slowpoke
#
# This web service responds to the "/metrics" endpoint (prometheus's scraping
# default), as well as "/healthz" to indicate normal operation (ie, HTTP 200)
# or a fault condition (ie, HTTP 500).
#
# References
#
#   - https://docs.python.org/3/library/urllib.request.html

import os
import sys
import json
import time
import threading
import http.server
import urllib.request

import pprint

cfg_port = 9100         # the port our webserver listens on
cfg_stale_thres = 90    # treat metrics as stale if older than this (secs)
cfg_check_interval = 50 # how often we'll try check for new metrics
cfg_drive_interval = 25 # how often we poll when the car is in "drive"
cfg_api_retries = 3     # how many times we call tesla APIs before giving up
cfg_retry_sleep = 5     # pause this number of seconds between retries

cfg_tesla_owner_url = "https://owner-api.teslamotors.com"
cfg_tesla_auth_url = "https://auth.tesla.com"

# All metrics to have this prefix added to them.
cfg_metrics_prefix="tesla"

# If the car goes asleep (or offline), how long (secs) before we wake it.
cfg_sleep_allowed = 60 * 60

# The file which contains the access token (periodically refreshed)
cfg_access_token_file = "/data/token.access"

# The file which contains the refresh token (never refreshed)
cfg_refresh_token_file = "/data/token.refresh"

# The JSON file where we store vehicle raw data
cfg_vehicle_data_file = "/data/vehicle.data"

G_metrics_cur = None    # metrics we expose on our web server
G_metrics_new = None    # metrics we accumulate while iterating through JSON
G_last_load = 0         # epoch time that we last loaded fresh JSON data
G_last_loop = 0         # epoch time of last main loop (to detect thread death)
G_last_online = 0       # timestamp of when the car was last online

# -----------------------------------------------------------------------------

# This function is supplied "filename", which should be a JSON file. Our
# job is to open the file and return a JSON object.

def f_load_json(filename):
  fd = None
  obj = None
  try:
    fd = open(filename)
    obj = json.load(fd)
  except:
    e = sys.exc_info()
    print("WARNING: Cannot load '%s' - %s" % (filename, e[1]))

  if (fd is not None):
    fd.close()
  return(obj)

# This function is given a JSON object. Our job is to identify all fields
# which are metrics, format their values and add it to "G_metrics_new",
# prepending the metric with "prefix". While iterating through "obj", if
# we find a nested dict/list, then we call ourself again, with a modified
# "prefix".

def f_iterate(obj, prefix):
  fields = obj.keys()
  for f in fields:
    if (prefix == ""):
      field_name = f
    else:
      field_name = "%s_%s" % (prefix, f)

    if (type(obj[f]) is dict):
      f_iterate(obj[f], field_name)
    else:

      # based on the data type, figure out how "value" should be represented.

      value = None # always format this into a string
      label = None # an optional {label="foo"}

      if (type(obj[f]) is float):                       # float
        value = "%f" % obj[f]
      if (type(obj[f]) is int):                         # int
        value = "%d" % obj[f]
      if (type(obj[f]) is bool):                        # bool
        value = 0
        if (obj[f]):
          value = 1
      if (type(obj[f]) is str):                         # str

        # if the string is empty (or just white space, ignore it

        s = obj[f].lstrip(" ").rstrip(" ")
        if (len(s) > 0):
          value = "1"
          s = s.replace("<","").replace(">","")
          s = s.replace(",", "_").replace(" ", "_")
          label = "value=\"%s\"" % s

      if (value is not None):
        m = field_name
        if (label is not None):
          m = "%s{%s}" % (field_name, label)
        G_metrics_new[m] = value

# This function returns the (epoch) mtime of the specified file, if something
# goes wrong, it returns -1.

def f_get_file_age(filename):
  try:
    sbuf = os.stat(filename)
  except:
    e = sys.exc_info()
    print("WARNING: Cannot stat() %s - %s" % (filename, e[1]))
    return(-1)
  return(sbuf.st_mtime)

# This function is supplied a filename, which we assume contains an oauth
# token. The token is return on success, or None if something goes wrong.

def f_get_token(filename):
  try:
    fd = open(filename)
  except:
    e = sys.exc_info()
    print("WARNING: Cannot open %s - %s" % (filename, e[1]))
    return(None)
  token = fd.read().rstrip("\n")
  fd.close()
  return(token)

# This function is given a filename, its job is to write "token" into the
# file.

def f_save_data(filename, data):
  print("NOTICE: updating file '%s'" % filename)
  try:
    fd = open("%s.new" % filename, "w")
  except:
    e = sys.exc_info()
    print("FATAL: Cannot open %s.new for writing - %s" % (filename, e[1]))
    os._exit(1)

  fd.write("%s\n" % data)
  fd.close()
  os.rename("%s.new" % filename, filename)

# This function is called if our API call failed while using our access token.
# Presumably it needs to be updated. This function attempts to do that using
# the refresh token and writes the new access token into a file. Recall that
# the expect response is the following JSON,
#
#  {
#    "access_token": "...",
#    "refresh_token": "...",
#    ...
#  }

def f_update_access_token():

  x = {}
  x["grant_type"] = "refresh_token"
  x["client_id"] = "ownerapi"
  x["refresh_token"] = f_get_token(cfg_refresh_token_file)
  x["scope"] = "openid email offline_access"
  data = json.dumps(x).encode("ascii")
  hdrs = {}
  hdrs["Content-Type"] = "application/json"
  url = "%s/oauth2/v3/token" % cfg_tesla_auth_url

  print("NOTICE: Refreshing access token - %s" % url)
  req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
  try:
    resp = urllib.request.urlopen(req)
  except:
    e = sys.exc_info()
    print("NOTICE: %s failed - %s" % (url, e[1]))
    resp = None

  if (resp is not None):                # new access token should be here
    payload = resp.read()
    obj = None
    try:
      obj = json.loads(payload)
    except:
      e = sys.exc_info()
      print("WARNING: No JSON response from %s - %s" % (url, e[1]))
    if ((obj is not None) and
        ("access_token" in obj) and
        ("refresh_token" in obj)):
      f_save_data(cfg_access_token_file, obj["access_token"])
      f_save_data(cfg_refresh_token_file, obj["refresh_token"])

# This function calls the tesla API's "/vehicles" endpoint, which is expected
# to return JSON like,
#
#   {'count': 1,
#    'response': [{'access_type': 'OWNER',
#                  'api_version': 54,
#                   ...
#                   'id': 3744405482650726,
#                   'state': 'offline',
#                   ...
#
# On success, it returns a hash which provides basic vehicle information. If
# something didn't work out, it returns None.

def f_get_vehicle_id():
  retries = cfg_api_retries
  while (retries > 0):

    access_token = f_get_token(cfg_access_token_file)
    if (access_token is None):
      return(None)

    retries = retries - 1
    hdrs = {}
    hdrs["Content-Type"] = "application/json"
    hdrs["Authorization"] = "Bearer %s" % access_token
    url = "%s/api/1/vehicles" % cfg_tesla_owner_url

    print("NOTICE: Listing vehicles - %s" % url)
    req = urllib.request.Request(url, data=None, headers=hdrs)
    resp = None
    try:
      resp = urllib.request.urlopen(req)
    except:
      e = sys.exc_info()
      print("NOTICE: %s failed - %s" % (url, e[1]))

    if (resp is None):                  # try to refresh our access token
      f_update_access_token()
    else:                               # hopefully we got a JSON response
      payload = resp.read()
      obj = None
      try:
        obj = json.loads(payload)
      except:
        e = sys.exc_info()
        print("WARNING: No JSON response from %s - %s" % (url, e[1]))
      if ((obj is not None) and
          ("count" in obj) and
          (obj["count"] == 1) and
          ("response" in obj) and
          (obj["response"] is not None) and
          (len(obj["response"]) == 1) and
          ("id" in obj["response"][0]) and
          ("state" in obj["response"][0]) and
          (obj["response"][0]["state"] is not None)):
        x = {}
        x["id"] = obj["response"][0]["id"]
        x["state"] = obj["response"][0]["state"]
        print("NOTICE: found vehicle id %d (%s)" % (x["id"], x["state"]))
        return(x)
    time.sleep(cfg_retry_sleep)
  return(None)

# This function is supplied a vehicle ID. Our job is to send the wake request.
# We return immediately. The response we get is typically,
#
#  {
#    "response": {
#      ...
#      "display_name": "SlowPoke",
#      "state": "asleep"
#      ...
#    }
#  }

def f_wake_vehicle(id):
  retries = cfg_api_retries
  while (retries > 0):

    retries = retries - 1
    hdrs = {}
    hdrs["Content-Type"] = "application/json"
    hdrs["Authorization"] = "Bearer %s" % f_get_token(cfg_access_token_file)
    url = "%s/api/1/vehicles/%d/wake_up" % (cfg_tesla_owner_url, id)

    print("NOTICE: Waking vehicle %d" % id)
    req = urllib.request.Request(url, data=None, headers=hdrs, method="POST")
    resp = None
    try:
      resp = urllib.request.urlopen(req)
    except:
      e = sys.exc_info()
      print("NOTICE: %s failed - %s" % (url, e[1]))

    if (resp is None):                  # try to refresh our access token
      f_update_access_token()
    else:
      payload = resp.read()
      obj = None
      try:
        obj = json.loads(payload)
      except:
        e = sys.exc_info()
        print("WARNING: No JSON response from %s - %s" % (url, e[1]))
      if ((obj is not None) and
          ("response" in obj) and
          (obj["response"] is not None) and
          ("state" in obj["response"]) and
          ("display_name" in obj["response"])):
        print("NOTICE: wakeup sent to %s(%s)" % \
              (obj["response"]["display_name"],
               obj["response"]["state"]))
      else:
        print("WARNING: unexpected response - %s" % payload)
      return
    time.sleep(cfg_retry_sleep)

# This function is supplied a vehicle ID. Our job is to attempt to pull down
# vehicle data. This can only be done if the vehicle is online, otherwise
# we'll get a HTTP 408 "Request Timeout" response. Recall that the response
# we typically get looks like,
#
#  {
#    "response": {
#      "id": 123456,
#      "display_name": "SlowPoke",
#      "state": "online",
#      ...
#      "charge_state": {
#        "battery_level": 69,
#        ...
#      }
#      ...
#    }
#  }

def f_get_vehicle_data(id):
  retries = cfg_api_retries
  while (retries > 0):

    retries = retries - 1
    hdrs = {}
    hdrs["Content-Type"] = "application/json"
    hdrs["Authorization"] = "Bearer %s" % f_get_token(cfg_access_token_file)
    url = "%s/api/1/vehicles/%d/vehicle_data" % (cfg_tesla_owner_url, id)

    print("NOTICE: Getting vehicle data - %s" % url)
    req = urllib.request.Request(url, data=None, headers=hdrs)
    resp = None
    try:
      resp = urllib.request.urlopen(req)
    except:
      e = sys.exc_info()
      print("NOTICE: %s failed - %s" % (url, e[1]))

    if (resp is None):                  # try to refresh our access token
      f_update_access_token()
    else:                               # hopefully we got a JSON response
      payload = resp.read()
      obj = None
      try:
        obj = json.loads(payload)
      except:
        e = sys.exc_info()
        print("WARNING: No JSON response from %s - %s" % (url, e[1]))
      if ((obj is not None) and
          ("response" in obj)):
        f_save_data(cfg_vehicle_data_file, str(payload, "UTF-8"))
      return
    time.sleep(cfg_retry_sleep)

# -----------------------------------------------------------------------------

class c_webserver(http.server.BaseHTTPRequestHandler):

  # Suppress log messages by overriding this function with empty code.

  def log_message(self, format, *args):
    return

  # This function is called whenever a client performs an HTTP GET.

  def do_GET(self):
    print("NOTICE: do_GET() path:%s" % self.path)

    # if we're called with "/healthz", check if main thread is alive.

    if (self.path == "/healthz"):
      last_loop = time.time() - G_last_loop
      if (last_loop > cfg_check_interval * 2):  # something's wrong
        self.send_response(500)
        self.end_headers()
        msg = "ERR last_loop:%d secs ago\n" % last_loop
      else:
        self.send_response(200)
        self.end_headers()
        msg = "OK last_loop:%d secs ago\n" % last_loop
      self.wfile.write(str.encode(msg))
      return

    # if we're not called with "/metrics", just return a 404.

    if (self.path != "/metrics"):
      self.send_response(404)
      self.end_headers()
      return

    self.send_response(200)
    self.send_header("Content-type", "text/plain")
    self.end_headers()

    # print out all metrics in G_metrics_cur

    buf = ""
    if (G_metrics_cur is not None):
      for m in G_metrics_cur.keys():
        buf += "%s %s\n" % (m, G_metrics_cur[m])
    self.wfile.write(str.encode(buf))
    sys.stdout.flush()

def f_webserver():
  try:
    ws = http.server.HTTPServer(("0.0.0.0", cfg_port), c_webserver)
    ws.serve_forever()
  except:
    e = sys.exc_info()
    print("FATAL! Cannot setup webservice - %s" % e[1])
    os._exit(1)

# -----------------------------------------------------------------------------

# start the webserver thread.

web_t = threading.Thread(target=f_webserver)
web_t.start()

# note down mtime of the "cfg_vehicle_data_file", we'll load it the next time
# it gets updated.

G_last_load = f_get_file_age (cfg_vehicle_data_file)

# a counter which records the start time of each main loop

cycle_start = time.time()

# The current polling frequency

poll_freq = cfg_check_interval

# program main loop

while 1:

  now = time.time()
  G_last_loop = now

  # Get vehicle ID, because this call tells you if the car is online

  vehicle = f_get_vehicle_id()

  # If the car is online, try grab vehicle data

  if (vehicle is not None) and (vehicle["state"] == "online"):
    G_last_online = now
    f_get_vehicle_data(vehicle["id"])
  else:

    # if the vehicle is not online, and it's been quite a while, wake it.

    offline_duration = now - G_last_online
    if (vehicle is not None) and ("state" in vehicle) and ("id" in vehicle):
      print("NOTICE: vehicle %s for %d/%d secs" % \
            (vehicle["state"], offline_duration, cfg_sleep_allowed))
      if (offline_duration > cfg_sleep_allowed):
        f_wake_vehicle(vehicle["id"])

  # Check if vehicle JSON file is new

  age = f_get_file_age (cfg_vehicle_data_file)
  if (age > G_last_load):

    # parse and confirm that "obj" and its data are good before we continue.

    obj = f_load_json(cfg_vehicle_data_file)
    if ((obj is not None) and
        ("response" in obj) and
        (obj["response"] is not None) and
        ("state" in obj["response"]) and
        (obj["response"]["state"] is not None) and
        (obj["response"]["state"] == "online")):

      G_metrics_new = {}
      f_iterate(obj["response"], cfg_metrics_prefix)
      if (len(G_metrics_new.keys()) > 1):
        print("NOTICE: metrics loaded with age %.3fsecs." % \
              (time.time() - age))
        G_metrics_cur = G_metrics_new
        G_last_load = age

        # if vehicle's "shift_state" is "D", we'll want to poll vehicle
        # metrics more frequently.

        poll_freq = cfg_check_interval
        if ("drive_state" in obj["response"]) and \
           ("shift_state" in obj["response"]["drive_state"]) and \
           (obj["response"]["drive_state"]["shift_state"] is not None) and \
           (obj["response"]["drive_state"]["shift_state"] == "D"):
          poll_freq = cfg_drive_interval

  # if vehicle JSON file is stale, then "G_metrics_cur" is stale too

  if (G_metrics_cur is not None) and (time.time() - age > cfg_stale_thres):
    print("NOTICE: metrics are now stale, age %.3fsec" % age)
    G_metrics_cur = None

  # calculate how long we'll sleep

  tv_end = time.time()
  cycle_start = cycle_start + poll_freq
  duration = cycle_start - tv_end
  if (duration > 0):
    print("NOTICE: cycle ended at %.3f, sleeping for %.3f secs." %
          (tv_end, duration))
    sys.stdout.flush()
    time.sleep(duration)
  else:
    sys.stdout.flush()

