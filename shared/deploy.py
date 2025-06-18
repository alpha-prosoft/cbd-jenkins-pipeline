#!/usr/bin/env python3

import argparse
import boto3
import yaml
import time

# --- YAML Customization for CloudFormation Tags ---
def general_tag_handler(loader, tag_suffix, node):
    """
    Handles YAML tags starting with '!' (like !Ref, !Sub, !GetAtt)
    by constructing the underlying data structure (scalar, sequence, or mapping)
    without interpreting the tag's specific meaning. This allows parsing
    CloudFormation templates to extract sections like 'Parameters'.
    """
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    elif isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    else:
        # This case should ideally not be reached with valid YAML.
        return None 

# Add this handler to SafeLoader for all tags starting with '!'
yaml.SafeLoader.add_multi_constructor('!', general_tag_handler)
# --- End YAML Customization ---

def get_vpc_data(aws_region, environment_name):
    """
    Placeholder function to retrieve VPC data.
    This will be implemented later to interact with AWS.
    """
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
            # A VPC can have multiple CIDR blocks associated with it.
            # We'll take the primary one or the first one listed.
            if 'CidrBlockAssociationSet' in vpcs[0] and vpcs[0]['CidrBlockAssociationSet']:
                for assoc in vpcs[0]['CidrBlockAssociationSet']:
                    if assoc.get('CidrBlockState', {}).get('State') == 'associated':
                        vpc_cidr = assoc['CidrBlock']
                        break # Take the first associated CIDR
            if not vpc_cidr and 'CidrBlock' in vpcs[0]: # Fallback for older response formats or single CIDR
                 vpc_cidr = vpcs[0]['CidrBlock']

        else: # Exactly one VPC
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
    """
    Retrieves public and private hosted zone names and IDs based on a suffix.
    Uses boto3 to query AWS Route53.
    """
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
                    # Remove trailing dot from zone name if present
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
    """
    Retrieves subnet IDs and maps them to their 'Name' tags.
    Uses boto3 to query AWS EC2.
    """
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
    """
    Deploys a CloudFormation stack (creates or updates).
    """
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
            
            # Now create the stack
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
            # Stack exists and is not in ROLLBACK_COMPLETE, so update it
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
                    return True # Considered successful, no waiting needed
                else:
                    print(f"Error updating stack {stack_name}: {e}")
                    raise
    
    except cf_client.exceptions.ClientError as e:
        if "does not exist" in str(e):
            # Stack does not exist, so create it
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
        else: # Other errors from describe_stacks
            print(f"Error during initial describe_stacks for {stack_name}: {e}")
            raise

    if action_taken and waiter_type:
        print(f"Waiting for stack {stack_name} operation ({waiter_type}) to complete...")
    waiter = cf_client.get_waiter(waiter_type)
    try:
        waiter.wait(StackName=stack_name, WaiterConfig={'Delay': 30, 'MaxAttempts': 120}) # Wait up to 1 hour
        print(f"Stack {stack_name} operation completed successfully.")
        return True
    except Exception as wait_error:
        print(f"Error waiting for stack {stack_name} operation: {wait_error}")
        # Optionally, describe stack events here for more detailed error output
        try:
            events_response = cf_client.describe_stack_events(StackName=stack_name)
            print("Recent stack events:")
            for event in events_response['StackEvents'][:10]: # Print last 10 events
                ts = event.get('Timestamp').strftime('%Y-%m-%d %H:%M:%S')
                resource_status = event.get('ResourceStatus', '')
                reason = event.get('ResourceStatusReason', '')
                print(f"  {ts} - {event.get('ResourceType')} - {event.get('LogicalResourceId')} - {resource_status} - {reason}")
        except Exception as event_error:
            print(f"Could not retrieve stack events: {event_error}")
        raise # Re-raise the original wait_error

def get_stack_outputs(aws_region, project_name, environment_name, base_stack_name):
    """
    Retrieves outputs from a CloudFormation stack (identified by project_name, environment_name, and base_stack_name)
    and returns them as a dictionary.
    """
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
            raise # Re-raise other client errors
    except Exception as e:
        print(f"An unexpected error occurred while retrieving outputs for stack {actual_stack_name}: {e}")
        raise
    
    return retrieved_outputs

def deploy(aws_account_id, aws_region, aws_cloudformation_file, project_name, deployment_name, deployment_type, environment_name, hosted_zone_suffix, parent_stacks_csv=None):
    """
    Placeholder function to deploy AWS CloudFormation stacks.
    """
    print("Deploying CloudFormation stack...")
    print(f"AWS Account ID: {aws_account_id}")
    print(f"AWS Region: {aws_region}")
    print(f"CloudFormation File: {aws_cloudformation_file}")
    print(f"Project Name: {project_name}")
    print(f"Deployment Name: {deployment_name}")
    print(f"Deployment Type: {deployment_type}")
    print(f"Environment Name: {environment_name}")
    print(f"Hosted Zone Suffix: {hosted_zone_suffix}")
    print("Deployment logic to be implemented.")

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

    # Get outputs from a specific global/core stack
    # Note: "us-east-1" is hardcoded here as per original logic for this specific stack.
    # 'environment_name' from the main CLI args is used to prefix this global/core stack.
    core_global_base_stack_name = "CORE-global" # This is the base name after PROJECT-ENV prefix
    core_global_outputs = get_stack_outputs("us-east-1", project_name, environment_name, core_global_base_stack_name)
    # Construct the full name for logging consistent with how get_stack_outputs does it
    full_core_stack_name = f"{project_name.upper()}-{environment_name.upper()}-{core_global_base_stack_name}".replace('_', '-')
    print(f"Outputs from {full_core_stack_name} stack: {core_global_outputs}")
    params.update(core_global_outputs)
    # The "Initial parameters gathered" log will now reflect params after this merge.

    if parent_stacks_csv:
        parent_stack_base_names = [name.strip() for name in parent_stacks_csv.split(',') if name.strip()]
        if parent_stack_base_names:
            print(f"Processing parent stacks for additional parameters: {parent_stack_base_names}")
            for parent_stack_base_name in parent_stack_base_names:
                # Construct the full name for logging consistent with how get_stack_outputs does it
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


    print(f"Initial parameters gathered: {params}")

    # Read and parse the CloudFormation template
    try:
        with open(aws_cloudformation_file, 'r') as f:
            template_body = f.read()
        
        cf_template = yaml.safe_load(template_body)
        template_parameters = cf_template.get('Parameters', {})
        print(f"Parameters defined in CloudFormation template '{aws_cloudformation_file}': {list(template_parameters.keys())}")

    except FileNotFoundError:
        print(f"Error: CloudFormation template file not found at {aws_cloudformation_file}")
        raise
    except yaml.YAMLError as e:
        print(f"Error: Could not parse CloudFormation template file {aws_cloudformation_file}: {e}")
        raise
    except Exception as e:
        print(f"An unexpected error occurred while reading/parsing {aws_cloudformation_file}: {e}")
        raise

    # Resolve parameters for CloudFormation
    cf_deploy_params = []
    for param_key, param_details in template_parameters.items():
        if param_key in params:
            cf_deploy_params.append({
                'ParameterKey': param_key,
                'ParameterValue': str(params[param_key]) # Ensure value is string
            })
        else:
            # Check if there's a Default value in the template
            if 'Default' not in param_details:
                print(f"Warning: Parameter '{param_key}' from template is not found in gathered params and has no default value. Deployment might fail.")
            else:
                print(f"Info: Parameter '{param_key}' not in gathered params, will use default from template: '{param_details['Default']}'")
                # CloudFormation will use its default, no need to pass it unless overriding.
                # If you want to explicitly pass defaults, uncomment below:
                # cf_deploy_params.append({
                #     'ParameterKey': param_key,
                #     'ParameterValue': str(param_details['Default'])
                # })


    # Construct the stack name: ENVIRONMENTNAMEUPPER-deploymenttype-projectname-deploymentname
    # All parts are lowercased and hyphenated, except environment_name which is uppercased.
    stack_name_parts = [
        project_name.upper(),
        environment_name.upper(),
        deployment_type,
        deployment_name
    ]
    stack_name = "-".join(stack_name_parts).replace('_', '-')


    print(f"Resolved parameters for CloudFormation deployment of stack '{stack_name}': {cf_deploy_params}")

    deploy_cloudformation(aws_region, stack_name, template_body, cf_deploy_params)
    print("CloudFormation deployment process completed successfully or no updates were needed.")

    # Get and merge stack outputs from the deployed stack into params
    # Construct the base name for the deployed stack (everything after the PROJECTUPPER-ENVUPPER- prefix)
    deployed_base_stack_name_parts = [
        deployment_type,
        # project_name is now part of the prefix in get_stack_outputs
        deployment_name
    ]
    deployed_base_stack_name = "-".join(deployed_base_stack_name_parts).replace('_', '-') # This will be TYPE-DEPLOYMENTNAME
    
    # The 'stack_name' variable (e.g., PROJECTUPPER-ENVUPPER-TYPE-DEPLOYMENTNAME) is the one we are getting outputs from.
    # get_stack_outputs will reconstruct this name using project_name, environment_name, and the new deployed_base_stack_name.
    deployed_stack_outputs = get_stack_outputs(aws_region, project_name, environment_name, deployed_base_stack_name)
    print(f"Outputs from deployed stack '{stack_name}': {deployed_stack_outputs}") # stack_name should be correct here
    params.update(deployed_stack_outputs)
    print(f"Final parameters after merging stack outputs: {params}")
    

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Deploy AWS CloudFormation stacks.")
    
    parser.add_argument("--aws-account-id", 
                        required=True, 
                        help="Your AWS Account ID.")
    parser.add_argument("--aws-region", 
                        required=True, 
                        help="The AWS region for deployment (e.g., us-east-1).")
    parser.add_argument("--aws-cloudformation-file", 
                        required=True, 
                        help="Path to the CloudFormation template file.")
    parser.add_argument("--project-name",
                        required=True,
                        help="The name of the project.")
    parser.add_argument("--deployment-name",
                        required=True,
                        help="The name of the deployment.")
    parser.add_argument("--deployment-type",
                        required=True,
                        help="The type of the deployment (e.g., service, job).")
    parser.add_argument("--environment-name",
                        required=True,
                        help="The name of the environment (e.g., dev, staging, prod).")
    parser.add_argument("--hosted-zone",
                        required=True,
                        help="The suffix of the hosted zone to search for (e.g., mycompany.com).")
    parser.add_argument("--parent-stacks",
                        required=False,
                        help="Comma-separated list of parent stack base names to fetch outputs from (e.g., 'stack1-base,stack2-base').")
    
    args = parser.parse_args()
    
    deploy(args.aws_account_id, 
           args.aws_region, 
           args.aws_cloudformation_file, 
           args.project_name, 
           args.deployment_name, 
           args.deployment_type, 
           args.environment_name, 
           args.hosted_zone,
           args.parent_stacks)
