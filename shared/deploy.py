import argparse
import boto3
import json
import yaml
import time
import subprocess
import os
from jinja2 import Environment, FileSystemLoader, select_autoescape

def general_tag_handler(loader, tag_suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    else:
        return None 

yaml.SafeLoader.add_multi_constructor('!', general_tag_handler)

def get_vpc_data(aws_region, environment_name):
    print(f"Fetching VPC data for region {aws_region} and environment {environment_name}...")
    ec2_client = boto3.client('ec2', region_name=aws_region)
    vpc_id = None
    vpc_cidr = None

    try:
        response = ec2_client.describe_vpcs()
        vpcs = response.get('Vpcs', [])
        
        if not vpcs:
            print(f"Warning: No VPCs found in region {aws_region}.")
        elif len(vpcs) > 1:
            print(f"Warning: Multiple VPCs found in region {aws_region}. Using the first one: {vpcs[0]['VpcId']}.")
            vpc_id = vpcs[0]['VpcId']
            if 'CidrBlockAssociationSet' in vpcs[0] and vpcs[0]['CidrBlockAssociationSet']:
                for assoc in vpcs[0]['CidrBlockAssociationSet']:
                    if assoc.get('CidrBlockState', {}).get('State') == 'associated':
                        vpc_cidr = assoc['CidrBlock']
                        break
            if not vpc_cidr and 'CidrBlock' in vpcs[0]:
                 vpc_cidr = vpcs[0]['CidrBlock']

        else:
            vpc_id = vpcs[0]['VpcId']
            if 'CidrBlockAssociationSet' in vpcs[0] and vpcs[0]['CidrBlockAssociationSet']:
                for assoc in vpcs[0]['CidrBlockAssociationSet']:
                    if assoc.get('CidrBlockState', {}).get('State') == 'associated':
                        vpc_cidr = assoc['CidrBlock']
                        break
            if not vpc_cidr and 'CidrBlock' in vpcs[0]:
                 vpc_cidr = vpcs[0]['CidrBlock']

    except Exception as e:
        print(f"Error fetching VPC data: {e}")
        raise

    if vpc_id and vpc_cidr:
        print(f"Retrieved VPCId: {vpc_id}, VPCCidr: {vpc_cidr}")
    else:
        print("Warning: Could not retrieve valid VPCId and VPCCidr.")
        
    return {"VPCId": vpc_id, "VPCCidr": vpc_cidr}

def get_hosted_zone_data(aws_region, hosted_zone_suffix):
    print(f"Fetching hosted zone data for region {aws_region} with suffix '{hosted_zone_suffix}'...")
    client = boto3.client('route53', region_name=aws_region)
    
    hosted_zone_info = {
        "PublicHostedZoneName": None,
        "PublicHostedZoneId": None,
        "PrivateHostedZoneName": None,
        "PrivateHostedZoneId": None,
    }
    
    if not hosted_zone_suffix.endswith('.'):
        search_suffix = hosted_zone_suffix + '.'
    else:
        search_suffix = hosted_zone_suffix

    try:
        paginator = client.get_paginator('list_hosted_zones')
        for page in paginator.paginate():
            for zone in page['HostedZones']:
                zone_name = zone['Name']
                zone_id = zone['Id'].replace('/hostedzone/', '')
                is_private = zone['Config']['PrivateZone']

                if zone_name.endswith(search_suffix):
                    processed_zone_name = zone_name.rstrip('.')
                    if is_private:
                        if not hosted_zone_info["PrivateHostedZoneName"]:
                            hosted_zone_info["PrivateHostedZoneName"] = processed_zone_name
                            hosted_zone_info["PrivateHostedZoneId"] = zone_id
                    else: 
                        if not hosted_zone_info["PublicHostedZoneName"]:
                            hosted_zone_info["PublicHostedZoneName"] = processed_zone_name
                            hosted_zone_info["PublicHostedZoneId"] = zone_id
                
                if hosted_zone_info["PublicHostedZoneName"] and hosted_zone_info["PrivateHostedZoneName"]:
                    break
            if hosted_zone_info["PublicHostedZoneName"] and hosted_zone_info["PrivateHostedZoneName"]:
                break
                
    except Exception as e:
        print(f"Error fetching hosted zones: {e}")
        raise

    if not hosted_zone_info["PublicHostedZoneName"]:
        print(f"Warning: Public hosted zone ending with '{search_suffix}' not found.")
    if not hosted_zone_info["PrivateHostedZoneName"]:
        print(f"Warning: Private hosted zone ending with '{search_suffix}' not found.")

    print(f"Retrieved Hosted Zone Info: {hosted_zone_info}")
    return hosted_zone_info

def get_subnet_data(aws_region, vpc_id):
    print(f"Fetching subnet data for VPC {vpc_id} in region {aws_region}...")
    ec2_client = boto3.client('ec2', region_name=aws_region)
    subnet_params = {}

    if not vpc_id:
        print("Warning: VPCId not provided, cannot fetch subnet data.")
        return subnet_params

    try:
        paginator = ec2_client.get_paginator('describe_subnets')
        for page in paginator.paginate(Filters=[{'Name': 'vpc-id', 'Values': [vpc_id]}]):
            for subnet in page['Subnets']:
                subnet_id = subnet['SubnetId']
                subnet_name_tag = None
                if 'Tags' in subnet:
                    for tag in subnet['Tags']:
                        if tag['Key'] == 'Name':
                            subnet_name_tag = tag['Value']
                            break
                if subnet_name_tag:
                    subnet_params[subnet_name_tag] = subnet_id
                else:
                    print(f"Warning: Subnet {subnet_id} does not have a 'Name' tag. It will not be added to params by its name.")
    except Exception as e:
        print(f"Error fetching subnet data: {e}")
        raise

    print(f"Retrieved Subnet Info: {subnet_params}")
    return subnet_params

def deploy_cloudformation(aws_region, stack_name, template_body, cf_parameters):
    print(f"Starting CloudFormation deployment for stack: {stack_name} in region {aws_region}...")
    cf_client = boto3.client('cloudformation', region_name=aws_region)
    action_taken = False 
    waiter_type = None

    try:
        stack_description_response = cf_client.describe_stacks(StackName=stack_name)
        stack_status = stack_description_response['Stacks'][0]['StackStatus']
        print(f"Stack {stack_name} exists with status: {stack_status}")

        if stack_status == 'ROLLBACK_COMPLETE':
            print(f"Stack {stack_name} is in ROLLBACK_COMPLETE state. Deleting before recreate...")
            cf_client.delete_stack(StackName=stack_name)
            delete_waiter = cf_client.get_waiter('stack_delete_complete')
            print(f"Waiting for stack {stack_name} deletion to complete...")
            delete_waiter.wait(StackName=stack_name, WaiterConfig={'Delay': 15, 'MaxAttempts': 40})
            print(f"Stack {stack_name} deleted successfully. Proceeding to create.")
            
            print(f"Attempting to create stack {stack_name} after deletion...")
            response = cf_client.create_stack(
                StackName=stack_name,
                TemplateBody=template_body,
                Parameters=cf_parameters,
                Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND']
            )
            print(f"Create initiated for stack {stack_name}. Stack ID: {response.get('StackId')}")
            waiter_type = 'stack_create_complete'
            action_taken = True
        else:
            print(f"Attempting to update stack {stack_name}...")
            try:
                response = cf_client.update_stack(
                    StackName=stack_name,
                    TemplateBody=template_body,
                    Parameters=cf_parameters,
                    Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND']
                )
                print(f"Update initiated for stack {stack_name}. Stack ID: {response.get('StackId')}")
                waiter_type = 'stack_update_complete'
                action_taken = True
            except cf_client.exceptions.ClientError as e:
                if "No updates are to be performed" in str(e):
                    print(f"No updates to be performed on stack {stack_name}.")
                    return True
                else:
                    print(f"Error updating stack {stack_name}: {e}")
                    raise
    
    except cf_client.exceptions.ClientError as e:
        if "does not exist" in str(e):
            print(f"Stack {stack_name} does not exist, attempting to create...")
            try:
                response = cf_client.create_stack(
                    StackName=stack_name,
                    TemplateBody=template_body,
                    Parameters=cf_parameters,
                    Capabilities=['CAPABILITY_IAM', 'CAPABILITY_NAMED_IAM', 'CAPABILITY_AUTO_EXPAND']
                )
                print(f"Create initiated for stack {stack_name}. Stack ID: {response.get('StackId')}")
                waiter_type = 'stack_create_complete'
                action_taken = True
            except Exception as create_error:
                print(f"Error creating stack {stack_name}: {create_error}")
                raise
        else:
            print(f"Error during initial describe_stacks for {stack_name}: {e}")
            raise

    if action_taken and waiter_type:
        print(f"Waiting for stack {stack_name} operation ({waiter_type}) to complete...")
    waiter = cf_client.get_waiter(waiter_type)
    try:
        waiter.wait(StackName=stack_name, WaiterConfig={'Delay': 30, 'MaxAttempts': 120})
        print(f"Stack {stack_name} operation completed successfully.")
        return True
    except Exception as wait_error:
        print(f"Error waiting for stack {stack_name} operation: {wait_error}")
        print(f"Attempting to retrieve all stack events for {stack_name} due to error...")
        all_events = []
        try:
            paginator = cf_client.get_paginator('describe_stack_events')
            for page in paginator.paginate(StackName=stack_name):
                all_events.extend(page['StackEvents'])
            
            if all_events:
                all_events.reverse()
                print("All stack events (chronological order):")
                for event in all_events:
                    ts = event.get('Timestamp').strftime('%Y-%m-%d %H:%M:%S')
                    resource_type = event.get('ResourceType', '')
                    logical_id = event.get('LogicalResourceId', '')
                    resource_status = event.get('ResourceStatus', '')
                    reason = event.get('ResourceStatusReason', '')
                    reason_str = str(reason).replace('\n', ' ') if reason else ''
                    print(f"  {ts} - {resource_type} - {logical_id} - {resource_status} - {reason_str}")
            else:
                print(f"No stack events found for {stack_name}.")
        except Exception as event_error:
            print(f"Could not retrieve all stack events for {stack_name}: {event_error}")
        raise

def get_stack_outputs(aws_region, project_name, environment_name, base_stack_name):
    actual_stack_name = f"{project_name.upper()}-{environment_name.upper()}-{base_stack_name}".replace('_', '-')
    
    print(f"Attempting to retrieve outputs for stack: {actual_stack_name} in region {aws_region}...")
    cf_client = boto3.client('cloudformation', region_name=aws_region)
    retrieved_outputs = {}

    try:
        stack_description_response = cf_client.describe_stacks(StackName=actual_stack_name)
        
        if not stack_description_response or not stack_description_response.get('Stacks'):
            print(f"Warning: Stack {actual_stack_name} not found or description is empty.")
            return retrieved_outputs

        stack_info = stack_description_response['Stacks'][0]
        outputs = stack_info.get('Outputs')

        if outputs:
            print(f"Found outputs for stack {actual_stack_name}:")
            for output in outputs:
                output_key = output.get('OutputKey')
                output_value = output.get('OutputValue')
                if output_key:
                    print(f"  Retrieved output: {output_key} = {output_value}")
                    retrieved_outputs[output_key] = output_value
            print("Stack outputs retrieved.")
        else:
            print(f"No outputs found for stack {actual_stack_name}.")

    except cf_client.exceptions.ClientError as e:
        if "does not exist" in str(e):
            print(f"Warning: Stack {actual_stack_name} does not exist. Cannot retrieve outputs.")
        else:
            print(f"Error describing stack {actual_stack_name} to get outputs: {e}")
            raise
    except Exception as e:
        print(f"An unexpected error occurred while retrieving outputs for stack {actual_stack_name}: {e}")
        raise
    
    return retrieved_outputs

def deploy(aws_account_id, aws_region, aws_cloudformation_file, project_name, deployment_name, deployment_type, environment_name, hosted_zone_suffix, parent_stacks_csv=None, cli_params_list=None):
    print("Starting CloudFormation deployment process...")
    print(f"Using AWS Account ID: {aws_account_id}")
    print(f"Target AWS Region: {aws_region}")
    print(f"CloudFormation File: {aws_cloudformation_file}")
    print(f"Project Name: {project_name}")
    print(f"Deployment Name: {deployment_name}")
    print(f"Deployment Type: {deployment_type}")
    print(f"Environment Name: {environment_name}")
    print(f"Hosted Zone Suffix: {hosted_zone_suffix}")

    print("Gathering initial parameters...")
    params = {
        "AccountId": aws_account_id,
        "Region": aws_region,
        "ProjectName": project_name,
        "DeploymentName": deployment_name,
        "EnvironmentNameLower": environment_name.lower(),
        "EnvironmentNameUpper": environment_name.upper()
    }

    vpc_data = get_vpc_data(aws_region, environment_name)
    params.update(vpc_data)

    hosted_zone_data = get_hosted_zone_data(aws_region, hosted_zone_suffix)
    params.update(hosted_zone_data)
    
    vpc_id_for_subnets = params.get("VPCId")
    if vpc_id_for_subnets:
        subnet_data = get_subnet_data(aws_region, vpc_id_for_subnets)
        params.update(subnet_data)
    else:
        print("Warning: VPCId not found in params, skipping subnet data retrieval.")

    core_global_base_stack_name = "CORE-global"
    print(f"Retrieving outputs from global/core stack '{project_name.upper()}-{environment_name.upper()}-{core_global_base_stack_name}' in us-east-1...")
    core_global_outputs = get_stack_outputs("us-east-1", project_name, environment_name, core_global_base_stack_name)
    full_core_stack_name = f"{project_name.upper()}-{environment_name.upper()}-{core_global_base_stack_name}".replace('_', '-')
    print(f"Outputs from {full_core_stack_name} stack: {core_global_outputs}")
    params.update(core_global_outputs)

    if parent_stacks_csv:
        parent_stack_base_names = [name.strip() for name in parent_stacks_csv.split(',') if name.strip()]
        if parent_stack_base_names:
            print(f"Processing parent stacks for additional parameters: {parent_stack_base_names}")
            for parent_stack_base_name in parent_stack_base_names:
                full_parent_stack_name = f"{project_name.upper()}-{environment_name.upper()}-{parent_stack_base_name}".replace('_', '-')
                print(f"Retrieving outputs from parent stack: {full_parent_stack_name} in region {aws_region}...")
                parent_outputs = get_stack_outputs(aws_region, project_name, environment_name, parent_stack_base_name)
                if parent_outputs:
                    print(f"Adding outputs from parent stack {full_parent_stack_name}: {parent_outputs}")
                    params.update(parent_outputs)
                else:
                    print(f"No outputs found or retrieved for parent stack {full_parent_stack_name}.")
        else:
            print("No valid parent stack names found in --parent-stacks input.")

    if "BuildId" not in params:
        try:
            git_hash = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode('utf-8').strip()
            params["BuildId"] = git_hash
            print(f"Added BuildId from git: {git_hash}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Could not determine git revision for BuildId: {e}. BuildId will not be set automatically.")
        except FileNotFoundError:
            print("Warning: git command not found. BuildId will not be set automatically.")



    ssm_client = boto3.client('ssm', region_name=aws_region)
    param_store_key = f"/deploy/{params['EnvironmentNameLower']}/params.json"
    print(f"Checking for parameters in SSM Parameter Store at key: {param_store_key}")
    try:
        response = ssm_client.get_parameter(Name=param_store_key, WithDecryption=True)
        param_value = response['Parameter']['Value']
        print("Found parameters in SSM Parameter Store. Merging them.")
        ssm_params = json.loads(param_value)
        params.update(ssm_params)
        print(f"Merged parameters from SSM: {ssm_params}")
    except ssm_client.exceptions.ParameterNotFound:
        print(f"No parameters found in SSM Parameter Store at {param_store_key}. Skipping.")
    except Exception as e:
        print(f"Error fetching or parsing parameters from SSM Parameter Store: {e}")

    cli_param_dict_parsed = {}
    if cli_params_list:
        print(f"Processing CLI parameters from --param to update gathered params: {cli_params_list}")
        for p_str in cli_params_list:
            if '=' in p_str:
                key, value = p_str.split('=', 1)
                if key in params:
                    print(f"Overriding gathered parameter '{key}' with value from --param: '{value}' (was: '{params.get(key)}')")
                else:
                    print(f"Adding new parameter from --param: '{key}' = '{value}'")
                params[key] = value
                cli_param_dict_parsed[key] = value
            else:
                print(f"Warning: CLI parameter '{p_str}' from --param is not in KEY=VALUE format and will be ignored.")

    print(f"Reading and parsing CloudFormation template: {aws_cloudformation_file}...")
    try:
        with open(aws_cloudformation_file, 'r') as f:
            template_body = f.read()
        
        cf_template = yaml.safe_load(template_body)
    except FileNotFoundError:
        print(f"Error: CloudFormation template file not found at {aws_cloudformation_file}")
        raise
    except yaml.YAMLError as e:
        print(f"Error: Could not parse CloudFormation template file {aws_cloudformation_file}: {e}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred while reading/parsing {aws_cloudformation_file}: {e}")
        raise

    print(f"Rendering CloudFormation template '{aws_cloudformation_file}' using Jinja2...")
    try:
        template_dir = os.path.dirname(os.path.abspath(aws_cloudformation_file))
        jinja_env = Environment(
            loader=FileSystemLoader(template_dir),
            autoescape=select_autoescape(['html', 'xml', 'yaml', 'json']),
        )

        jinja_template_object = jinja_env.from_string(template_body)
        rendered_template_body = jinja_template_object.render({'params': params})
        
        template_body = rendered_template_body
        print("Jinja2 rendering complete.")

        print("Re-parsing template after Jinja2 rendering to update parameter definitions...")
        cf_template = yaml.safe_load(template_body)
    except Exception as e:
        print(f"Error during Jinja2 rendering or re-parsing of template {aws_cloudformation_file}: {e}")
        raise

    print("Resolving parameters for CloudFormation deployment...")
    template_parameters = cf_template.get('Parameters', {})
    cf_deploy_params = []
    for param_key, param_details in template_parameters.items():
        if param_key in params:
            param_value = str(params[param_key])
            cf_deploy_params.append({
                'ParameterKey': param_key,
                'ParameterValue': param_value
            })

            if param_details.get('NoEcho'):
                print(f"    {param_key}: ****")
            else:
                print(f"    {param_key}: {param_value}")
        else:
            if 'Default' not in param_details:
                param_value = str(params[param_key])
                print(f"    {param_key}: <<< MISSING")
            else:
                print(f"    {param_key}: {param_details['Default']} (default)")

    print("Constructing CloudFormation stack name...")
    stack_name_parts = [
        project_name.upper(),
        environment_name.upper(),
        deployment_type,
        deployment_name
    ]
    stack_name = "-".join(stack_name_parts).replace('_', '-')
    print(f"CloudFormation stack name determined: {stack_name}")


    print(f"Final resolved parameters for CloudFormation deployment of stack '{stack_name}': {cf_deploy_params}")

    deploy_cloudformation(aws_region, stack_name, template_body, cf_deploy_params)
    print(f"CloudFormation deployment for stack '{stack_name}' completed (or no updates were needed).")

    print(f"Retrieving outputs from deployed stack '{stack_name}'...")
    deployed_base_stack_name_parts = [
        deployment_type,
        deployment_name
    ]
    deployed_base_stack_name = "-".join(deployed_base_stack_name_parts).replace('_', '-')
    
    deployed_stack_outputs = get_stack_outputs(aws_region, project_name, environment_name, deployed_base_stack_name)
    print(f"Outputs from deployed stack '{stack_name}': {deployed_stack_outputs}")
    params.update(deployed_stack_outputs)
    print(f"Final parameters after merging outputs from deployed stack '{stack_name}': {params}")
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy AWS CloudFormation stacks.")
    
    parser.add_argument("--aws-account-id", required=True, help="Your AWS Account ID.")
    parser.add_argument("--aws-region", required=True, help="The AWS region for deployment (e.g., us-east-1).")
    parser.add_argument("--aws-cloudformation-file", required=True, help="Path to the CloudFormation template file.")
    parser.add_argument("--project-name", required=True, help="The name of the project.")
    parser.add_argument("--deployment-name", required=True, help="The name of the deployment.")
    parser.add_argument("--deployment-type", required=True, help="The type of the deployment (e.g., service, job).")
    parser.add_argument("--environment-name", required=True, help="The name of the environment (e.g., dev, staging, prod).")
    parser.add_argument("--hosted-zone", required=True, help="The suffix of the hosted zone to search for (e.g., mycompany.com).")
    parser.add_argument("--parent-stacks", required=False, help="Comma-separated list of parent stack base names to fetch outputs from (e.g., 'stack1-base,stack2-base').")
    parser.add_argument("--param", action='append', default=[], help="Additional parameters to pass directly to CloudFormation in 'KEY=VALUE' format. Can be specified multiple times. These override other gathered parameters if keys conflict.")
    
    args = parser.parse_args()
    
    deploy(args.aws_account_id, 
           args.aws_region, 
           args.aws_cloudformation_file, 
           args.project_name, 
           args.deployment_name, 
           args.deployment_type, 
           args.environment_name, 
           args.hosted_zone,
           args.parent_stacks,
           args.param)
