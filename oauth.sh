#!/bin/bash
#
# References
#  https://tesla-api.timdorr.com/api-basics/authentication
#  https://github.com/timdorr/tesla-api/discussions/281
#
# Notes
#  - the code verifier is only used when we're obtaining our initial tokens
#  - access tokens last 28800 seconds (ie, 8 hours)
#  - refresh tokens don't expire (but can only be used once)
#  - use the refresh token to obtain new access and refresh tokens
#  - once a (parked) car is awoken, it goes back to sleep after 15 mins

STATE=`openssl rand -hex 6`
CODE_VERIFIER=`./code_verifier.py`
CODE_CHALLENGE=`./code_challenge.py "$CODE_VERIFIER"`

# generate the authorize request URL.

AUTH_URL="https://auth.tesla.com/oauth2/v3/authorize"
PARAMS="response_type=code"
PARAMS="${PARAMS}&client_id=ownerapi"
PARAMS="${PARAMS}&code_challenge=$CODE_CHALLENGE"
PARAMS="${PARAMS}&code_challenge_method=S256"
PARAMS="${PARAMS}&scope=openid+email+offline_access"
PARAMS="${PARAMS}&state=$STATE"
PARAMS="${PARAMS}&redirect_uri=https://auth.tesla.com/void/callback"

if [ ! -f "token.access" ] || [ ! -f "token.refresh" ] ; then

  # print the URL, have the user put this into the browser and login.

  echo -e "[ Link to open in browser ]\n"
  echo "${AUTH_URL}?${PARAMS}"
  echo ""

  # Expected URL in user's browser (line breaks for readability),
  #
  #   https://auth.tesla.com/void/callback?
  #     code=3c8ccd...57559&
  #     state=a10007043b2b&
  #     issuer=https%3A%2F%2Fauth.tesla.com%2Foauth2%2Fv3

  echo -n "Enter code from URL: "
  read CODE

  # request for the bearer token and refresh token

  JSON="{
        \"grant_type\": \"authorization_code\",
        \"client_id\": \"ownerapi\",
        \"code\": \"$CODE\",
        \"code_verifier\": \"$CODE_VERIFIER\",
        \"redirect_uri\":
        \"https://auth.tesla.com/void/callback\"
        }"

  curl -s -v -X POST -d "$JSON" \
    -H "Content-Type: application/json" \
    https://auth.tesla.com/oauth2/v3/token \
    >auth.step3 2>&1

  # Expected JSON response ...
  #  {
  #    "access_token":"eyJhb-9txiV....",
  #    "refresh_token":"eyJhbGciOiJSUz...",
  #    "id_token":"eyJhbGciOiJSUzI1N...",
  #    "expires_in":28800,
  #    "state":"2ff6ebfbe4fc",
  #    "token_type":"Bearer"
  #  }

  ACCESS_T=`cat auth.step3 | tail -1 | jq .access_token | sed -e 's/"//g'`
  REFRESH_T=`cat auth.step3 | tail -1 | jq .refresh_token | sed -e 's/"//g'`

  if [ -z "$ACCESS_T" ] ; then
    echo "FATAL! Could not identify access token."
    exit 1
  fi
  if [ -z "$REFRESH_T" ] ; then
    echo "FATAL! Could not identify refresh token."
    exit 1
  fi

  echo "$ACCESS_T" >token.access"
  echo "$REFRESH_T" >token.refresh"
fi

# Identify my vehicle. Note that if $ACCESS_T has expired, we'll get a
# response - {"error":"invalid bearer token"}

RETRIES=2
while [ $RETRIES -gt 0 ] ; do

  ACCESS_T="`cat token.access`"
  REFRESH_T="`cat token.refresh`"

  if [ -z "$ACCESS_T" ] ; then
    echo "FATAL! No access token"
    exit 1
  fi
  if [ -z "$REFRESH_T" ] ; then
    echo "FATAL! No refresh token"
    exit 1
  fi

  echo "NOTICE: attempting to obtain vehicle list."

  curl -s \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $ACCESS_T" \
    https://owner-api.teslamotors.com/api/1/vehicles \
    >vehicle.list.out 2>&1

  grep 'invalid bearer token' vehicle.list.out >/dev/null
  if [ $? -ne 0 ] ; then
    mv vehicle.list.out vehicle.list
    break # yay, we managed to get our vehicle list
  else
    echo "NOTICE: obtaining new access/refresh tokens."

    # looks like we need to refresh our access token

    JSON="{
          \"grant_type\": \"refresh_token\",
          \"client_id\": \"ownerapi\",
          \"refresh_token\": \"$REFRESH_T\",
          \"scope\": \"openid email offline_access\"
          }"
    curl -s -v -X POST -d "$JSON" \
      -H "Content-Type: application/json" \
      https://auth.tesla.com/oauth2/v3/token \
      >auth.refresh 2>&1

    ACCESS_T=`tail -1 auth.refresh | jq .access_token | sed -e 's/"//g'`
    REFRESH_T=`tail -1 auth.refresh | jq .refresh_token | sed -e 's/"//g'`

    if [ -z "$ACCESS_T" ] ; then
      echo "FATAL! No access token"
      exit 1
    fi
    if [ -z "$REFRESH_T" ] ; then
      echo "FATAL! No refresh token"
      exit 1
    fi

    echo "$ACCESS_T" >token.access
    echo "$REFRESH_T" >token.refresh
  fi
  RETRIES=$(($RETRIES - 1))
done

# We need to get our vehicle ID in order to proceed.

ID=`cat vehicle.list | tail -1 | jq '.response[0].id'`
if [ -z "$ID" ] ; then
  echo "FATAL! No vehicle id."
  exit 1
fi

# We may need to wake the vehicle. This might take several seconds.

STATE=`tail -1 vehicle.list | jq .response[0].state | sed -e 's/"//g'`
echo "NOTICE: identified vehicle ID $ID, currently ${STATE}."

if [ "$STATE" = "offline" ] || [ "$STATE" = "asleep" ] ; then
  echo "NOTICE: Waking vehicle."

  curl -s -v -X POST \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $ACCESS_T" \
    https://owner-api.teslamotors.com/api/1/vehicles/${ID}/wake_up \
    >vehicle.wakeup 2>&1

  exit 0
fi

# Get vehicle data. Note that if the vehicle is offline/asleep, the response
# we receive is,
#   {
#     "response":null,
#     "error":"vehicle unavailable: {:error=>\"vehicle unavailable:\"}",
#     "error_description":""
#   }

echo "NOTICE: Obtaining vehicle data."

curl -s \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $ACCESS_T" \
  https://owner-api.teslamotors.com/api/1/vehicles/${ID}/vehicle_data \
  >vehicle.data 2>&1

