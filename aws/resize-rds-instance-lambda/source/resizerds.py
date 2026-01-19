import boto3
import os
from botocore.exceptions import ClientError
import time
import json
import urllib3

rds         = boto3.client('rds', region_name=os.environ["region"])
http        = urllib3.PoolManager()

def modify_rds(inst_name,inst_size,EnablePerformanceInsightsFlag):
    try:
        print('Changing ' + inst_name + ' to ' + inst_size )
        response=rds.modify_db_instance(
            DBInstanceIdentifier=inst_name,
            DBInstanceClass=inst_size,
            EnablePerformanceInsights=EnablePerformanceInsightsFlag,
            ApplyImmediately=True
            )
        time.sleep(60)
        waiter = rds.get_waiter('db_instance_available')
        print( "Waiting for " + inst_name + " to complete..." )
        waiter.wait(
            DBInstanceIdentifier=inst_name
            )
        print_slack_msg("DB instance " + inst_name + " changed to " + inst_size + " successfully!")
    except ClientError as e:
        print(e)
        print_slack_msg("Failed to change DB instance " + inst_name + " to " + inst_size + "!")


def describe_db_clusters(env_name):
    cluster_list=rds.describe_db_clusters()
    for dbcluster in cluster_list["DBClusters"]:
        if env_name in dbcluster["DBClusterIdentifier"]:
            for dbinstancemembers in dbcluster["DBClusterMembers"]:
                if dbinstancemembers["IsClusterWriter"] == False:
                    reader_identifier = ""
                    reader_identifier=dbinstancemembers["DBInstanceIdentifier"]

                    return(reader_identifier)

def print_slack_msg(message):
    slack_webhook = os.environ["slack_webhook"]
    print("Messsage")

    slack_msg = { "text": message }

    encoded_msg = json.dumps(slack_msg).encode('utf-8')
    resp = http.request('POST',slack_webhook, body=encoded_msg)
    print(
        {
            "message" : slack_msg,
            "status_code": resp.status,
            "response": resp.data
        }
    )

def change_db_parameter_group(inst_name, param_group):
    try:
        response=rds.modify_db_instance(
            DBInstanceIdentifier=inst_name,
            DBParameterGroupName=param_group,
            ApplyImmediately=True
            )
        time.sleep(60)
        waiter = rds.get_waiter('db_instance_available')
        print( "Waiting for " + inst_name + " to complete..." )
        waiter.wait(
            DBInstanceIdentifier=inst_name
            )
        print_slack_msg(inst_name + " DB instance's parameter group changed to " + param_group + " successfully!")
    except ClientError as e:
        print (e)
        print_slack_msg("Failed to change the DB parameter group of " + inst_name + " to " + param_group + "!")

def reboot_rds_inst(inst_name):
    try:
        response=rds.reboot_db_instance(
            DBInstanceIdentifier=inst_name,
            ForceFailover=False
        )
        print("Rebooting DB instance" + inst_name)
        time.sleep(60)
        waiter = rds.get_waiter('db_instance_available')
        print( "Waiting for " + inst_name + " to complete its reboot..." )
        waiter.wait(
            DBInstanceIdentifier=inst_name
            )
        print_slack_msg(" DB instance " + inst_name + " rebooted successfully!")
    except ClientError as e:
        print(e)
        print_slack_msg("Failed to reboot DB instance " + inst_name + "!")

def lambda_handler(event, context):

    env_name=os.environ["env_name"]
    param_group=os.environ["db_param_group_name"]
    dbinstance_type=os.environ["dbinstance_type"]
    ep_flag=bool(os.environ["EnablePerformanceInsightsFlag"])

    print('Environment Name : ' + env_name)
    print('DB instance Type : ' + dbinstance_type)
    print('Performance Insights Flag : ',  ep_flag)

    reader_id=""

    reader_id=describe_db_clusters(env_name)
    if reader_id is None:
        print('Unable to find Reader instance for Environment ' + env_name)
    else:
        print('Reader Identifier : ' + reader_id)
        print('going to upgrade ' + reader_id + ' to ' + dbinstance_type )
        modify_rds(reader_id,dbinstance_type,ep_flag)
        change_db_parameter_group(reader_id, param_group)
        reboot_rds_inst(reader_id)