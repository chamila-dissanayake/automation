#!/usr/bin/env python3

import argparse
import logging
import sys
import time
import json
from datetime import datetime, timezone
import boto3
from botocore.exceptions import ClientError, WaiterError
import urllib3

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Slack webhook URL
SLACK_WEBHOOK_URL = ''

def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Restore Aurora MySQL DB cluster using point-in-time recovery'
    )
    parser.add_argument(
        '--restore-time',
        default='',
        help='DB restore time in ISO 8601 format (YYYY-MM-DDTHH:MM:SS)'
    )
    parser.add_argument(
        '--env-name',
        default='fieldresearch-dev',
        help='Environment name (e.g., dev, qa, prod)'
    )
    """
    parser.add_argument(
        '--triggered-by',
        default='manual',
        help='Who triggered the restore (default: automation)'
    )
    """
    parser.add_argument(
        '--route53-zone-name',
        default='pearsonaurora.com',
        help='Route53 hosted zone name (e.g., example.com)'
    )
    parser.add_argument(
        '--cname-ro-prefix',
        default='',
        help='Database CNAME prefix for reader to update the read only record (e.g., db.ro.example.com)'
    )
    parser.add_argument(
        '--cname-rw-prefix',
        default='',
        help='Database CNAME prefix for writer to update the write record (e.g., db.rw.example.com)'
    )
    parser.add_argument(
        '--region',
        default='ca-central-1',
        help='AWS region (default: ca-central-1)'
    )
    parser.add_argument(
        '--ssm-parameter-name',
        default='/fieldresearch/dev/rds/db-cluster-identifier',
        help='SSM parameter name to store new cluster ID'
    )
    parser.add_argument(
        '--subnet-group',
        help='DB subnet group name (optional, inherits from source if not specified)'
    )
    parser.add_argument(
        '--security-groups',
        nargs='+',
        help='Security group IDs (optional, inherits from source if not specified)'
    )

    return parser.parse_args()

def get_restore_time(hour=9, minute=0, second=0):
    """
    Return the today's date and time calculated using time in argument in ISO 8601 format (YYYY-MM-DDTHH:MM:SS).

    Args:
        hour (int): Hour of the day (default: 9)
        minute (int): Minute of the hour (default: 0)
        second (int): Second of the minute (default: 0)

    Returns:
        str: Date and time string at specified time UTC today in ISO 8601 format
    """

    return datetime.now(timezone.utc).replace(hour=hour, minute=minute, second=second).strftime('%Y-%m-%dT%H:%M:%S')

def db_cluster_last_restorable_time(rds_client, cluster_id):
    """
    Retrieve the latest restorable time for the given DB cluster.

    Args:
        rds_client: Boto3 RDS client
        cluster_id: DB cluster identifier
    Returns:
        datetime: The latest restorable time in UTC
    """
    try:
        response = rds_client.describe_db_clusters(
            DBClusterIdentifier=cluster_id
        )
        if not response['DBClusters']:
            raise ValueError(f"Cluster {cluster_id} not found")

        last_restorable_time = response['DBClusters'][0]['LatestRestorableTime']

        return last_restorable_time

    except ClientError as e:
        logger.error(f"Failed to retrieve DB cluster info: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error retrieving DB cluster info: {str(e)}")
        raise

def get_current_db_cluster_id(ssm_client, parameter_name):
    """
    Retrieve the latest DB cluster identifier from SSM Parameter Store.

    Args:
        ssm_client: Boto3 SSM client
        parameter_name: SSM parameter name

    Returns:
        str: The latest DB cluster identifier
    """
    try:
        response = ssm_client.get_parameter(Name=parameter_name)
        return response['Parameter']['Value']
    except ClientError as e:
        logger.error(f"Failed to retrieve parameter {parameter_name} from SSM: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error retrieving parameter {parameter_name} from SSM: {str(e)}")
        raise

def get_source_cluster_config(rds_client, source_cluster_id):
    """
    Retrieve configuration details from the source DB cluster.

    Args:
        rds_client: Boto3 RDS client
        source_cluster_id: Source DB cluster identifier

    Returns:
        dict: Dictionary containing:
            - subnet_group: DB subnet group name
            - security_groups: List of VPC security group IDs
            - db_cluster_parameter_group: DB cluster parameter group name
            - db_instance_parameter_group: DB instance parameter group name (from first instance)
            - instance_class: DB instance class (from first instance)
    """
    try:
        logger.info(f"Retrieving configuration from source cluster: {source_cluster_id}")

        # Get cluster details
        cluster_response = rds_client.describe_db_clusters(
            DBClusterIdentifier=source_cluster_id
        )

        if not cluster_response['DBClusters']:
            raise ValueError(f"Source cluster {source_cluster_id} not found")

        cluster = cluster_response['DBClusters'][0]

        config = {
            'subnet_group': cluster.get('DBSubnetGroup'),
            'security_groups': [sg['VpcSecurityGroupId'] for sg in cluster.get('VpcSecurityGroups', [])],
            'db_cluster_parameter_group': cluster.get('DBClusterParameterGroup'),
            'db_instance_parameter_group': None,
            'instance_class': None
        }

        # Get instance details from the first instance in the cluster
        if cluster.get('DBClusterMembers'):
            first_instance_id = cluster['DBClusterMembers'][0]['DBInstanceIdentifier']

            instance_response = rds_client.describe_db_instances(
                DBInstanceIdentifier=first_instance_id
            )

            if instance_response['DBInstances']:
                instance = instance_response['DBInstances'][0]
                config['db_instance_parameter_group'] = instance['DBParameterGroups'][0]['DBParameterGroupName'] if instance.get('DBParameterGroups') else None
                config['instance_class'] = instance.get('DBInstanceClass', 'db.t3.medium')

        #logger.info(f"Source cluster configuration retrieved:")
        #logger.info(f"  Subnet Group: {config['subnet_group']}")
        #logger.info(f"  Security Groups: {config['security_groups']}")
        #logger.info(f"  Cluster Parameter Group: {config['db_cluster_parameter_group']}")
        #logger.info(f"  Instance Parameter Group: {config['db_instance_parameter_group']}")
        #logger.info(f"  Instance Class: {config['instance_class']}")

        return config

    except ClientError as e:
        logger.error(f"Failed to retrieve source cluster config: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error retrieving source cluster config: {str(e)}")
        raise


def restore_db_cluster(rds_client, source_cluster_id, restore_time, new_cluster_id, 
                       subnet_group=None, security_groups=None, db_cluster_parameter_group=None):
    """
    Restore DB cluster using point-in-time recovery.

    Args:
        rds_client: Boto3 RDS client
        source_cluster_id: Source DB cluster identifier
        restore_time: Point in time to restore to (datetime object)
        new_cluster_id: New DB cluster identifier
        subnet_group: Optional DB subnet group name
        security_groups: Optional list of security group IDs
        db_cluster_parameter_group: Optional DB cluster parameter group name

    Returns:
        dict: Response from restore_db_cluster_to_point_in_time API call
    """
    try:
        logger.info(f"Starting point-in-time restore for cluster: {source_cluster_id}")
        logger.info(f"Restore time: {restore_time}")
        logger.info(f"New cluster ID: {new_cluster_id}")

        # Build restore parameters
        restore_params = {
            'DBClusterIdentifier': new_cluster_id,
            'SourceDBClusterIdentifier': source_cluster_id,
            'RestoreType': 'full-copy',
            'UseLatestRestorableTime': False,
            'RestoreToTime': restore_time
        }

        # Add optional parameters if provided
        if subnet_group:
            restore_params['DBSubnetGroupName'] = subnet_group
            logger.info(f"Using subnet group: {subnet_group}")

        if security_groups:
            restore_params['VpcSecurityGroupIds'] = security_groups
            logger.info(f"Using security groups: {security_groups}")

        if db_cluster_parameter_group:
            restore_params['DBClusterParameterGroupName'] = db_cluster_parameter_group
            logger.info(f"Using DB cluster parameter group: {db_cluster_parameter_group}")

        # Perform the restore
        response = rds_client.restore_db_cluster_to_point_in_time(**restore_params)

        logger.info(f"Successfully initiated restore for cluster: {new_cluster_id}")
        logger.debug(f"Restore response: {response}")

        return response

    except ClientError as e:
        logger.error(f"Failed to restore DB cluster: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during DB restore: {str(e)}")
        raise


def wait_for_cluster_available(rds_client, cluster_id, max_wait_time=3600, check_interval=30):
    """
    Wait for DB cluster to become available.

    Args:
        rds_client: Boto3 RDS client
        cluster_id: DB cluster identifier
        max_wait_time: Maximum time to wait in seconds (default: 1 hour)
        check_interval: Time between status checks in seconds (default: 30s)

    Returns:
        bool: True if cluster is available, raises exception otherwise
    """
    try:
        logger.info(f"Waiting for cluster {cluster_id} to become available...")
        logger.info(f"This may take up to {max_wait_time/60} minutes")

        waiter = rds_client.get_waiter('db_cluster_available')
        waiter.wait(
            DBClusterIdentifier=cluster_id,
            WaiterConfig={
                'Delay': check_interval,
                'MaxAttempts': max_wait_time // check_interval
            }
        )

        logger.info(f"Cluster {cluster_id} is now available")
        return True

    except WaiterError as e:
        logger.error(f"Timeout waiting for cluster to become available: {str(e)}")
        raise
    except ClientError as e:
        logger.error(f"Error while waiting for cluster: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while waiting for cluster: {str(e)}")
        raise


def create_db_instance(rds_client, cluster_id, instance_identifier, instance_class=None,
                       db_parameter_group=None):
    """
    Create a DB instance in the restored cluster.

    Args:
        rds_client: Boto3 RDS client
        cluster_id: DB cluster identifier
        instance_identifier: DB instance identifier
        instance_class: DB instance class (default: db.t4g.medium)
        db_parameter_group: DB parameter group name (optional)

    Returns:
        dict: Response from create_db_instance API call
    """
    try:
        logger.info(f"Creating DB instance: {instance_identifier}")
        logger.info(f"In cluster: {cluster_id}")

        instance_params = {
            'DBInstanceIdentifier': instance_identifier,
            'DBInstanceClass': instance_class or 'db.t4g.medium',
            'Engine': 'aurora-mysql',
            'DBClusterIdentifier': cluster_id
        }

        if db_parameter_group:
            instance_params['DBParameterGroupName'] = db_parameter_group
            logger.info(f"Using DB parameter group: {db_parameter_group}")

        response = rds_client.create_db_instance(**instance_params)

        logger.info(f"Successfully initiated DB instance creation: {instance_identifier}")
        logger.debug(f"Create instance response: {response}")

        return response

    except ClientError as e:
        logger.error(f"Failed to create DB instance: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error during DB instance creation: {str(e)}")
        raise


def wait_for_instance_available(rds_client, instance_identifier, max_wait_time=1800, check_interval=30):
    """
    Wait for DB instance to become available.

    Args:
        rds_client: Boto3 RDS client
        instance_identifier: DB instance identifier
        max_wait_time: Maximum time to wait in seconds (default: 30 minutes)
        check_interval: Time between status checks in seconds (default: 30s)

    Returns:
        bool: True if instance is available, raises exception otherwise
    """
    try:
        logger.info(f"Waiting for instance {instance_identifier} to become available...")
        logger.info(f"This may take up to {max_wait_time/60} minutes")

        waiter = rds_client.get_waiter('db_instance_available')
        waiter.wait(
            DBInstanceIdentifier=instance_identifier,
            WaiterConfig={
                'Delay': check_interval,
                'MaxAttempts': max_wait_time // check_interval
            }
        )

        logger.info(f"Instance {instance_identifier} is now available")
        return True

    except WaiterError as e:
        logger.error(f"Timeout waiting for instance to become available: {str(e)}")
        raise
    except ClientError as e:
        logger.error(f"Error while waiting for instance: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error while waiting for instance: {str(e)}")
        raise


def get_cluster_endpoints(rds_client, cluster_id):
    """
    Get the endpoint (URI) of the DB cluster.

    Args:
        rds_client: Boto3 RDS client
        cluster_id: DB cluster identifier

    Returns:
        dict: Dictionary with 'Endpoint' and 'ReaderEndpoint' keys
    """
    try:
        logger.info(f"Retrieving endpoint for cluster: {cluster_id}")

        response = rds_client.describe_db_clusters(
            DBClusterIdentifier=cluster_id
        )

        if not response['DBClusters']:
            raise ValueError(f"Cluster {cluster_id} not found")

        endpoints = {
            'Endpoint': response['DBClusters'][0]['Endpoint'],
            'ReaderEndpoint': response['DBClusters'][0]['ReaderEndpoint']
        }

        return endpoints

    except ClientError as e:
        logger.error(f"Failed to get cluster endpoint: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting cluster endpoint: {str(e)}")
        raise

def get_zone_id(route53_client, zone_name):
    """
    Get Route53 hosted zone ID by zone name.

    Args:
        route53_client: Boto3 Route53 client
        zone_name: Hosted zone name (e.g., example.com)

    Returns:
        str: Hosted zone ID
    """
    try:
        logger.info(f"Retrieving hosted zone ID for zone: {zone_name}")

        response = route53_client.list_hosted_zones_by_name(DNSName=zone_name)
        zones = response['HostedZones']

        for zone in zones:
            if zone['Name'].rstrip('.') == zone_name.rstrip('.'):
                zone_id = zone['Id'].split('/')[-1]
                logger.info(f"Found hosted zone ID: {zone_id}")
                return zone_id

        raise ValueError(f"Hosted zone {zone_name} not found")

    except ClientError as e:
        logger.error(f"Failed to get hosted zone ID: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error getting hosted zone ID: {str(e)}")
        raise

def update_route53_cname(route53_client, zone_id, cname_record, target_endpoint):
    """
    Update Route53 CNAME record to point to new database endpoint.

    Args:
        route53_client: Boto3 Route53 client
        zone_id: Route53 hosted zone ID
        cname_record: CNAME record name (e.g., db.example.com)
        target_endpoint: Target endpoint address

    Returns:
        dict: Response from change_resource_record_sets API call
    """
    try:
        logger.info(f"Updating Route53 CNAME record: {cname_record}")
        logger.info(f"New target: {target_endpoint}")

        # Ensure CNAME record ends with a dot
        #if not cname_record.endswith('.'):
        #    cname_record = cname_record + '.'

        # Ensure target endpoint ends with a dot
        #if not target_endpoint.endswith('.'):
        #    target_endpoint = target_endpoint + '.'

        response = route53_client.change_resource_record_sets(
            HostedZoneId=zone_id,
            ChangeBatch={
                'Comment': f'Updated by db_restore.py on {datetime.now().isoformat()}',
                'Changes': [
                    {
                        'Action': 'UPSERT',
                        'ResourceRecordSet': {
                            'Name': cname_record,
                            'Type': 'CNAME',
                            'TTL': 300,
                            'ResourceRecords': [
                                {
                                    'Value': target_endpoint
                                }
                            ]
                        }
                    }
                ]
            }
        )

        change_id = response['ChangeInfo']['Id']
        logger.info(f"Route53 change initiated. Change ID: {change_id}")

        # Wait for change to be propagated
        waiter = route53_client.get_waiter('resource_record_sets_changed')
        waiter.wait(Id=change_id)

        logger.info(f"Route53 CNAME record updated successfully")

        return response

    except ClientError as e:
        logger.error(f"Failed to update Route53 record: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error updating Route53 record: {str(e)}")
        raise


def save_cluster_id_to_ssm(ssm_client, parameter_name, cluster_id):
    """
    Save new cluster ID to SSM Parameter Store.

    Args:
        ssm_client: Boto3 SSM client
        parameter_name: SSM parameter name
        cluster_id: DB cluster identifier to save

    Returns:
        dict: Response from put_parameter API call
    """
    try:
        logger.info(f"Saving cluster ID to SSM parameter: {parameter_name}")

        response = ssm_client.put_parameter(
            Name=parameter_name,
            Value=cluster_id,
            Type='String',
            Overwrite=True,
            Description=f'New DB cluster identifier created on {datetime.now().isoformat()}'
        )

        logger.info(f"Cluster ID saved to SSM successfully. Version: {response['Version']}")

        return response

    except ClientError as e:
        logger.error(f"Failed to save cluster ID to SSM: {e.response['Error']['Message']}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error saving to SSM: {str(e)}")
        raise


def generate_new_cluster_id(env_name):
    """
    Generate new cluster ID with timestamp.

    Args:
        env_name: Environment name

    Returns:
        str: New cluster identifier with datestamp
    """
    datestamp = datetime.now().strftime('%Y%m%d')
    new_cluster_id = f"{env_name}-{datestamp}"    # Strips last 6 characters of the course_cluster_id to remove the existing datestamp
    logger.info(f"Generated new cluster ID: {new_cluster_id}")
    return new_cluster_id

def send_slack_message(message, level='info'):
    """
    Send a message to Slack webhook.

    Args:
        message: Message text to send
        level: Message level ('info', 'success', 'warning', 'error')
    """
    try:
        # Add emoji based on level
        emoji_map = {
            'info': ':information_source:',
            'success': ':white_check_mark:',
            'warning': ':warning:',
            'error': ':x:'
        }
        emoji = emoji_map.get(level, ':information_source:')

        slack_message = {
            'text': f"{emoji} *DB Restore Process* - {message}"
        }

        http = urllib3.PoolManager()
        encoded_message = json.dumps(slack_message).encode('utf-8')

        response = http.request(
            'POST',
            SLACK_WEBHOOK_URL,
            body=encoded_message,
            headers={'Content-Type': 'application/json'}
        )

        if response.status != 200:
            logger.warning(f"Slack notification failed with status {response.status}")
    except Exception as e:
        logger.warning(f"Failed to send Slack notification: {str(e)}")


def main():
    """Main execution function."""
    try:
        # Parse command line arguments
        args = parse_arguments()

        logger.info("=" * 80)
        logger.info("Starting Aurora MySQL Point-in-Time Restore Process in %s", args.env_name)
        logger.info("=" * 80)
        send_slack_message(
            f"Starting Aurora MySQL Point-in-Time Restore Process in {args.env_name}\n",
            level='info'
        )

        # Initialize AWS clients
        logger.info(f"Initializing AWS clients for region: {args.region}")
        rds_client = boto3.client('rds', region_name=args.region)
        route53_client = boto3.client('route53', region_name=args.region)
        ssm_client = boto3.client('ssm', region_name=args.region)

        # Construct/get restore time
        restore_time = ""

        if args.restore_time:
            try:
                # If restore time provided, parse it
                restore_time = datetime.fromisoformat(args.restore_time.replace('Z', '+00:00'))
                logger.info(f"Using provided restore time: {restore_time}")
            except ValueError as e:
                logger.error(f"Invalid restore time format: {args.restore_time}")
                logger.error("Expected format: YYYY-MM-DDTHH:MM:SS")
                sys.exit(1)
        else:
             # Default to last restorable time of the DB cluster
            restore_time = db_cluster_last_restorable_time(
                    rds_client=rds_client,
                    cluster_id=get_current_db_cluster_id(ssm_client, args.ssm_parameter_name)
                )
            logger.info(f"Using last restorable time: {restore_time}")

        """
        if args.triggered_by == 'schedule':
            # If triggered by schedule, set restore time to yesterday 9:00 AM UTC
            restore_time = get_restore_time(hour=9, minute=0, second=0)
            logger.info(f"Restore time set to 9:00 AM UTC: {restore_time}")
        else:
            if args.restore_time:
                try:
                    # If restore time provided, parse it
                    restore_time = datetime.fromisoformat(args.restore_time.replace('Z', '+00:00'))
                    logger.info(f"Using provided restore time: {restore_time}")
                except ValueError as e:
                    logger.error(f"Invalid restore time format: {args.restore_time}")
                    logger.error("Expected format: YYYY-MM-DDTHH:MM:SS")
                    sys.exit(1)
            else:
                # Default to last restorable time of the DB cluster
                restore_time = db_cluster_last_restorable_time(
                        rds_client=rds_client,
                        cluster_id=get_current_db_cluster_id(ssm_client, args.ssm_parameter_name)
                    )
                logger.info(f"Using last restorable time: {restore_time}")
        """

        # Step 1: Get Source cluster configs
        # Generate new cluster ID
        source_cluster_id = get_current_db_cluster_id(ssm_client, args.ssm_parameter_name)
        new_cluster_id = generate_new_cluster_id(args.env_name)

        # Get source cluster configuration
        logger.info("\n" + "=" * 80)
        logger.info("Step 1/6: Retrieving source cluster configuration")
        logger.info("=" * 80)

        send_slack_message(
            f"Step 1/6: Retrieving source cluster configuration\n",
            level='info'
        )

        source_config = get_source_cluster_config(rds_client, source_cluster_id)

        # Use command-line arguments if provided, otherwise use source cluster config
        subnet_group = source_config['subnet_group']
        security_groups = source_config['security_groups']
        db_cluster_parameter_group = source_config['db_cluster_parameter_group']
        db_instance_parameter_group = source_config['db_instance_parameter_group']
        instance_class = source_config['instance_class']

        logger.info("Using the following configuration for restore:")
        logger.info("Source Cluster ID: %s", source_cluster_id)
        logger.info("New Cluster ID: %s", new_cluster_id)
        logger.info("Subnet Group: %s", subnet_group)
        logger.info("Security Groups: %s", security_groups)
        logger.info("DB Cluster Parameter Group: %s", db_cluster_parameter_group)
        logger.info("DB Instance Parameter Group: %s", db_instance_parameter_group)
        logger.info("Instance Class: %s", instance_class)

        send_slack_message(
            f"Retrieved source cluster configuration: -\n"
            f"Source Cluster ID: {source_cluster_id}\n" +
            f"Subnet Group: {subnet_group}\n" +
            f"Security Groups: {security_groups}\n" +
            f"DB Cluster Parameter Group: {db_cluster_parameter_group}\n" +
            f"DB Instance Parameter Group: {db_instance_parameter_group}\n" +
            f"Instance Class: {instance_class}",
            level='info'
        )

        # Step 2: Restore DB cluster
        logger.info("\n" + "=" * 80)
        logger.info("Step 2/6: Initiating point-in-time restore")
        logger.info("Source Cluster ID: %s", source_cluster_id)
        logger.info("New Cluster ID: %s", new_cluster_id)
        logger.info("Restore Time: %s", restore_time)
        logger.info("=" * 80)
        send_slack_message(
            f"Step 2/6: Initiating point-in-time restore\n"
            f"Source Cluster ID: {source_cluster_id}\n" +
            f"New Cluster ID: {new_cluster_id}\n" +
            f"Restore Time: {restore_time}",
            level='info'
        )
        restore_response = restore_db_cluster(
            rds_client=rds_client,
            source_cluster_id=source_cluster_id,
            restore_time=restore_time,
            new_cluster_id=new_cluster_id,
            subnet_group=subnet_group,
            security_groups=security_groups,
            db_cluster_parameter_group=db_cluster_parameter_group
        )
        send_slack_message(
            f"Point-in-time restore initiated successfully",
            level='success'
        )
        logger.info("Waiting for cluster to become available...")
        logger.info("=" * 80)
        send_slack_message(
            f"Waiting for cluster to become available\n"
            f"This may take up to 30 minutes...",
            level='info'
        )

        wait_for_cluster_available(rds_client, new_cluster_id)

        send_slack_message(
            f"Cluster {new_cluster_id} is now available",
            level='success'
        )

        # Step 3: Create DB instance in the restored cluster
        instance_identifier = f"{new_cluster_id}-0"

        logger.info("\n" + "=" * 80)
        logger.info(f"Step 3/6: Creating DB instance {instance_identifier} in restored cluster {new_cluster_id}")
        logger.info("=" * 80)

        send_slack_message(
            f"Step 3/6: Creating DB instance\n"
            f"Instance ID: {instance_identifier}\n",
            level='info'
        )
        create_db_instance(
            rds_client=rds_client,
            cluster_id=new_cluster_id,
            instance_identifier=instance_identifier,
            instance_class=instance_class,
            db_parameter_group=db_instance_parameter_group
        )
        send_slack_message(
            f"DB instance creation initiated\n",
            level='success'
        )

        # Wait for instance to be available
        send_slack_message(
            f"Waiting for DB instance {instance_identifier} to become available\n"
            f"This may take up to 30 minutes...",
            level='info'
        )
        wait_for_instance_available(rds_client, instance_identifier)
        send_slack_message(
            f"DB instance {instance_identifier} is now available",
            level='success'
        )

        # Step 4: Get cluster endpoints
        logger.info("\n" + "=" * 80)
        logger.info("Step 4/6: Retrieving cluster endpoints")
        logger.info("=" * 80)
        send_slack_message(
            f"Step 4/6: Retrieving cluster endpoints",
            level='info'
        )
        cluster_endpoints = get_cluster_endpoints(rds_client, new_cluster_id)
        logger.info(f"New DB cluster endpoints -\nWriter: {cluster_endpoints['Endpoint']}\nReader: {cluster_endpoints['ReaderEndpoint']}")
        send_slack_message(
            f"Cluster endpoints retrieved\n",
            level='success'
        )

        # Step 5: Update Route53 CNAME records
        cname_rw = ""
        cname_ro = ""

        if args.cname_ro_prefix:
            cname_ro = f"{args.cname_ro_prefix}.{args.route53_zone_name}"
        else:
            cname_ro = f"{args.env_name}-readonly.{args.route53_zone_name}"

        if args.cname_rw_prefix:
            cname_rw = f"{args.cname_rw_prefix}.{args.route53_zone_name}"
        else:
            cname_rw = f"{args.env_name}-writer.{args.route53_zone_name}"

        logger.info("\n" + "=" * 80)
        logger.info("Step 5/6: Updating Route53 CNAME records")
        logger.info("=" * 80)
        send_slack_message(
            f"Step 5/6: Updating Route53 CNAME records for reader and writer\n"
            f"Writer Record: {cname_rw}\n"
            f"Target: {cluster_endpoints['Endpoint']}\n"
            f"Reader Record: {cname_ro}\n"
            f"Target: {cluster_endpoints['ReaderEndpoint']}",
            level='info'
        )

        # Get hosted zone ID from zone name
        zone_id = get_zone_id(route53_client, args.route53_zone_name)

        # Update writer CNAME
        update_route53_cname(
            route53_client=route53_client,
            zone_id=zone_id,
            cname_record=cname_rw,
            target_endpoint=cluster_endpoints['Endpoint']
        )
        logger.info(f"Writer CNAME {cname_rw} updated to point to {cluster_endpoints['Endpoint']}")
        send_slack_message(
            f"Writer CNAME {cname_rw} updated successfully",
            level='success'
        )

        # Update reader CNAME
        update_route53_cname(
            route53_client=route53_client,
            zone_id=zone_id,
            cname_record=cname_ro,
            target_endpoint=cluster_endpoints['ReaderEndpoint']
        )
        logger.info(f"Reader CNAME {cname_ro} updated to point to {cluster_endpoints['ReaderEndpoint']}")
        send_slack_message(
            f"Reader CNAME {cname_ro} updated successfully",
            level='success'
        )

        send_slack_message(
            f"Route53 CNAME records updated",
            level='success'
        )

        # Step 6: Save cluster ID to SSM
        logger.info("\n" + "=" * 80)
        logger.info("Step 6/6: Saving cluster ID to SSM Parameter Store")
        logger.info("=" * 80)
        send_slack_message(
            f"Step 6/6: Saving new cluster ID to SSM parameter {args.ssm_parameter_name} for later reference\n"
            f"Parameter: {args.ssm_parameter_name}",
            level='info'
        )
        save_cluster_id_to_ssm(
            ssm_client=ssm_client,
            parameter_name=args.ssm_parameter_name,
            cluster_id=new_cluster_id
        )
        send_slack_message(
            f"Cluster ID saved to SSM parameter successfully",
            level='success'
        )

        # Success summary
        logger.info("\n" + "=" * 80)
        logger.info("RESTORE COMPLETED SUCCESSFULLY")
        logger.info("=" * 80)
        logger.info(f"Source Cluster: {source_cluster_id}")
        logger.info(f"New Cluster: {new_cluster_id}")
        logger.info(f"New DB Instance: {instance_identifier}")
        logger.info(f"Restore Time: {restore_time}")
        logger.info(f"Writer Endpoint: {cluster_endpoints['Endpoint']}")
        logger.info(f"Reader Endpoint: {cluster_endpoints['ReaderEndpoint']}")
        logger.info(f"Writer CNAME: {cname_rw}")
        logger.info(f"Reader CNAME: {cname_ro}")
        logger.info(f"SSM Parameter: {args.ssm_parameter_name}")
        logger.info("=" * 80)

        send_slack_message(
            f"✅ *RESTORE COMPLETED SUCCESSFULLY*\n\n"
            f"*Source Cluster:* {source_cluster_id}\n"
            f"*New DB Cluster:* {new_cluster_id}\n"
            f"*New DB Instance:* {instance_identifier}\n"
            f"*Restore Time:* {restore_time}\n"
            f"*Writer Endpoint:* {cluster_endpoints['Endpoint']}\n"
            f"*Reader Endpoint:* {cluster_endpoints['ReaderEndpoint']}\n"
            f"*Writer CNAME:* {cname_rw}\n"
            f"*Reader CNAME:* {cname_ro}\n"
            f"*SSM Parameter:* {args.ssm_parameter_name}",
            level='success'
        )

        sys.exit(0)

    except KeyboardInterrupt:
        logger.warning("Process interrupted by user!!!")
        send_slack_message(
            f"⚠️ *RESTORE INTERRUPTED!!!*",
            level='warning'
        )
        sys.exit(130)
    except Exception as e:
        logger.error(f"Fatal error: {str(e)}", exc_info=True)
        send_slack_message(
            f"❌ *RESTORE FAILED!!!*\n\nError: {str(e)}",
            level='error'
        )
        sys.exit(1)


if __name__ == '__main__':
    main()