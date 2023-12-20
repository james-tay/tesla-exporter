## Tesla Metrics Exporter for Prometheus

### Initial Setup

These scripts obtain a vehicle's metrics and expose them in a manner that
prometheus can scrape. In order to obtain a vehicle's metrics, it uses an
access token to authenticate itself against Tesla's API servers. This access
token expires every couple of hours and has to be refreshed using a refresh
token, which also gets refreshed on each use. 

The `oauth.sh` script is used to obtain a refresh token and an access token.
To do this, it generates a URL which the user must copy/paste into a browser.
This URL brings the user to Tesla's login page and after the user successfully
enters their credentials, is redirected to an "error" page. What is important
is the access code in this redirected URL, which the user then copy/pastes
back to the `oauth.sh`'s prompt. Once this is done, `oauth.sh` should be able
to access Tesla's API, and it then writes the files `token.refresh` and
`token.access`. **The user is responsible for keeping these files private**.

If `oauth.sh` discovers the vehicle is asleep, it will send the wake command.
After a few seconds (or up to a minute), if the `oauth.sh` script is run again, 
the vehicle should now be awake, and metrics are written into the file
`vehicle.data`.

### Running The Exporter

Once `oauth.sh` has correctly generated `vehicle.data`, we can now run the
`tesla_exporter.py` process. This script assumes that you already have the
`token.access` and `token.refresh` files. `tesla_exporter.py` uses these
to periodically pull metrics from the vehicle. If the vehicle goes to sleep,
`tesla_exporter.py` will no longer be able to present metrics to prometheus.
The script has a user configurable variable `cfg_sleep_allowed`, which
determines how long the vehicle is allowed to remain asleep before it is
woken up, after which metrics will be available for prometheus to scrape
again. Regardless, `tesla_exporter.py` will always try to obtain vehicle
metrics every `cfg_check_interval` seconds.

`tesla_exporter.py` will automatically attempt to refresh `token.access`
when it expires and will update both `token.access` and `token.refresh` in
the process.

More than 200x metrics are typically exposed by the exporter. This may change
as Tesla pushes newer versions of their software into the car. The raw data
can be inspected via,

```
$ jq . < vehicle.data
{
  "response": {
    "id": 1234567890,
    "user_id": 1234567890,
    ...
    "charge_state": {
      "battery_heater_on": false,
      "battery_level": 70,
      "battery_range": 247.19,
      ...
    }
    ...
  }
}
```

### Tesla Powerwall

To obtain metrics from a Powerwall, using the same access token, we need to
look up our "energy_site_id". This can be done via,

```
curl -s \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $ACCESS_T" \
    https://owner-api.teslamotors.com/api/1/products
```

The output from the above curl should include,

```
{
  "response": [
    {
      "energy_site_id": 1234567890,
      ...
    }
    ...
  ],
  ...
}
```

Write the Powerwall ID number in the file specified in `cfg_powerwall_id_file`.
This file is read once when the `tesla_exporter.py` script starts up. If the
file is not present, the script will not try to obtain Powerwall metrics.

