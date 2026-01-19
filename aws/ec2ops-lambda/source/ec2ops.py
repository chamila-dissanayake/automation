import json
import os
from urllib.request import urlopen
from urllib.error import URLError, HTTPError
from socket import timeout
import boto3
from botocore.config import Config
import urllib3

env = os.environ["env_name"]
region = os.environ["region"]
instance_id = os.environ["instance_id"]
slack_webhook = os.environ["slack_webhook"]
debug_logs = os.environ["debug_logs"]
operation = os.environ["operation"]

my_config = Config(
    region_name = region,
    signature_version = 'v4',
    retries = {
        'max_attempts': 10,
        'mode': 'standard'
        }
    )

url_timeout = 10
http = urllib3.PoolManager()

def logger(str_input, log_type='STD'):
    if log_type == "ERR":
        print ("ERROR || " + str(str_input))
    elif log_type == "DBG" and debug_logs:
        print("DEBUG || " + str(str_input))
    elif log_type == "INF":
        print ("INFO || " + str(str_input))
    else:
        print ("INFO || " + str(str_input))

def performEC2Operation(str_task, str_instance_id):
    logger("Called function performEC2Operation to " + str_task + " ECS instance ID: " + str_instance_id, "DBG")
    client = boto3.client('ec2', config=my_config)

    if str_task == "start":
        response = client.start_instances(
            InstanceIds=[
                str_instance_id,
            ],
        )
        logger (response, "DBG")
        code_number = response['StartingInstances'][0]['CurrentState']['Code']
        code_name = response['StartingInstances'][0]['CurrentState']['Name']

        if int(code_number) == 0 or int(code_number) == 16:
            logger ("Requested to start EC2 instance " + str_instance_id + " and currently in " + code_name + " state!", "STD")
            return True
        else:
            logger ("Failed to start EC2 instance " + str_instance_id + " and currently in " + code_name + " state!", "ERR")
            return False
    elif str_task == "stop":
        response = client.stop_instances(
            InstanceIds=[
                str_instance_id,
            ],
        )
        logger (response, "DBG")
        code_number = response['StoppingInstances'][0]['CurrentState']['Code']
        code_name = response['StoppingInstances'][0]['CurrentState']['Name']

        if int(code_number) == 32 or int(code_number) == 64 or int(code_number) == 80:
            logger ("Requested to stop EC2 instance " + str_instance_id + " and currently in " + code_name + " state!", "STD")
            return True
        else:
            logger ("Failed to stop EC2 instance " + str_instance_id + " and currently in " + code_name + " state!", "ERR")
            return False
    else:
        logger ("Unrecognized type of EC2 operation detected!", "ERR")
        return False

def sendToSlack(message=""):
    json_message = { "text": message }
    encoded_msg = json.dumps(json_message).encode('utf-8')
    resp = http.request('POST', slack_webhook, body=encoded_msg)

    if resp.status != 200:
        logger("message : " + message + " status_code: " + str(resp.status) + " response: " + resp.data, "ERR")
        print(
            {
                "message": message,
                "status_code": resp.status,
                "response": resp.data
            }
        )

def lambda_handler(event, context):
    if operation == "start":
        logger ("Running EC2 start operation for " + instance_id + "!!!")
        sendToSlack ("Running EC2 start operation for " + instance_id + "!!!")
        if performEC2Operation(operation, instance_id):
            sendToSlack ("EC2 instance ID %s started!" % instance_id)
            return True
        else:
            sendToSlack ("Failed to start EC2 instance %s!" % instance_id)
            return False
    elif operation == "stop":
        logger ("Running EC2 stop operation for " + instance_id + "!!!")
        sendToSlack ("Running EC2 stop operation for " + instance_id + "!!!")
        if performEC2Operation(operation, instance_id):
            sendToSlack ("EC2 instance ID %s is stopped!" % instance_id)
            return True
        else:
            sendToSlack ("Failed to stop EC2 instance %s!" % instance_id)
            return False
    else:
        logger ("Unrecognized type of EC2 operation detected!", "ERR")
        return False