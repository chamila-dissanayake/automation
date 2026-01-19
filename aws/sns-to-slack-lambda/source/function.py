#!/usr/bin/python3.6
import urllib3
import json
import os

http = urllib3.PoolManager()
def lambda_handler(event, context):
    url = os.environ["slack_webhook"]
    msg = {
        "channel": "#field-research-aurora",
        "username": "fieldresearch-aurora",
        "text": event['Records'][0]['Sns']['Subject'],
        "icon_emoji": ""
    }

    encoded_msg = json.dumps(msg).encode('utf-8')
    resp = http.request('POST',url, body=encoded_msg)
    print({
        "message": event['Records'][0]['Sns']['Subject'],
        "status_code": resp.status, 
        "response": resp.data
    })