#!/usr/bin/env python3
"""
CloudFormation Parameter Resolution Module

This module provides standalone parameter resolution for CloudFormation deployments.
It can be used both as a CLI tool and as an importable module.

Parameter Resolution Order (later sources override earlier ones):
1. Base CLI arguments (AccountId, Region, ProjectName, etc.)
2. AWS infrastructure discovery (VPC, subnets, hosted zones)
3. Auto-generated values (BuildId from git)
4. Core global stack outputs (us-east-1)
5. Parent stack outputs (if specified)
6. CLI parameter overrides (--param KEY=VALUE) - Highest priority

Output Formats:
- pretty: Structured display with categories and missing parameter detection (default)
- json: JSON format for programmatic use
- text: KEY=VALUE pairs for shell scripts

Options:
- --quiet: Suppress progress messages, only show final output
"""

import argparse
import boto3
import yaml
import subprocess
import json
import sys

def general_tag_handler(loader, tag_suffix, node):
    """
    YAML tag handler for CloudFormation intrinsic functions.
    Allows safe_load to handle !Ref, !Sub, etc. without errors.
    """
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
    """
    Fetches VPC data for the specified region and environment.
    
    Searches for VPCs in the region. If multiple VPCs are found, uses the first one.
    Returns VPCId and VPCCidr.
    
    Args:
        aws_region: AWS region to search in
        environment_name: Environment name (for logging purposes)
        
    Returns:
        dict: {"VPCId": vpc_id, "VPCCidr": vpc_cidr}
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
    """
    Fetches hosted zone data for zones ending with the specified suffix.
    
    Searches Route53 for both public and private hosted zones matching the suffix.
    Returns the first matching zone of each type.
    
    Args:
        aws_region: AWS region (used for client initialization)
        hosted_zone_suffix: Domain suffix to search for (e.g., "example.com")
        
    Returns:
        dict: {
            "PublicHostedZoneName": name,
            "PublicHostedZoneId": id,
            "PrivateHostedZoneName": name,
            "PrivateHostedZoneId": id
        }
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
    Fetches subnet data for the specified VPC.
    
    Retrieves all subnets in the VPC and creates a mapping from subnet Name tags
    to subnet IDs. Subnets without Name tags are skipped.
    
    Args:
        aws_region: AWS region
        vpc_id: VPC ID to fetch subnets for
        
    Returns:
        dict: {subnet_name: subnet_id, ...}
        Example: {"public-subnet-1a": "subnet-abc123", "private-subnet-1a": "subnet-def456"}
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


def get_stack_outputs(stack_region, project_name, environment_name, base_stack_name):
    """
    Retrieves outputs from a CloudFormation stack.
    
    Constructs the full stack name as: {PROJECT}-{ENV}-{BASE_STACK_NAME} (if project_name provided)
    or {ENV}-{BASE_STACK_NAME} (if project_name is None)
    and fetches all outputs from that stack.
    
    Args:
        stack_region: AWS region where the stack exists (per-stack)
        project_name: Project name (converted to uppercase), optional - if None, omitted from stack name
        environment_name: Environment name (converted to uppercase)
        base_stack_name: Base stack name (e.g., "CORE-global", "vpc-setup")
        
    Returns:
        dict: {output_key: output_value, ...}
    """
    if project_name:
        actual_stack_name = f"{project_name.upper()}-{environment_name.upper()}-{base_stack_name}".replace('_', '-')
    else:
        actual_stack_name = f"{environment_name.upper()}-{base_stack_name}".replace('_', '-')
    
    print(f"Attempting to retrieve outputs for stack: {actual_stack_name} in region {stack_region}...")
    cf_client = boto3.client('cloudformation', region_name=stack_region)
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
            print(f"ERROR: Stack {actual_stack_name} does not exist in region {stack_region}.")
            print(f"  Stack name searched: {actual_stack_name}")
            print(f"  Region searched: {stack_region}")
            print(f"  AWS error: {e}")
            return retrieved_outputs  # Return empty dict to trigger failure in caller
        else:
            print(f"ERROR: Failed to describe stack {actual_stack_name} to get outputs: {e}")
            raise
    except Exception as e:
        print(f"ERROR: Unexpected error occurred while retrieving outputs for stack {actual_stack_name}: {e}")
        raise
    
    return retrieved_outputs


def format_params_pretty(params):
    """
    Formats parameters in a structured, easy-to-read format.
    Groups parameters by category and highlights missing values.
    
    Args:
        params: Dictionary of resolved parameters
        
    Returns:
        str: Formatted parameter output
    """
    output_lines = []
    output_lines.append("\n" + "=" * 80)
    output_lines.append("RESOLVED PARAMETERS")
    output_lines.append("=" * 80)
    
    # Get metadata if available, otherwise use empty dict
    metadata = params.get('_metadata', {})
    
    # Define categories with display names
    categories = {
        'base': 'Base Parameters',
        'vpc': 'VPC & Network',
        'hosted_zones': 'Route53 Hosted Zones',
        'build': 'Build Information',
        'subnets': 'Subnets',
        'core_stack': 'Core Stack Outputs',
        'parent_stacks': 'Parent Stack Outputs',
        'overrides': 'CLI Overrides'
    }
    
    # Track which params have been displayed
    displayed_params = set(['_metadata'])
    
    # Display categorized parameters
    for category_key, category_name in categories.items():
        category_params = metadata.get(category_key, [])
        
        if category_key == 'subnets':
            # Special handling for subnets (any param containing 'subnet')
            category_params = [k for k in params.keys() if 'subnet' in k.lower() and k != '_metadata']
        elif category_key in ['core_stack', 'parent_stacks', 'overrides']:
            # These are tracked separately, skip for now
            continue
        
        if category_params:
            output_lines.append(f"\n{category_name}:")
            output_lines.append("-" * 40)
            
            for param_key in category_params:
                displayed_params.add(param_key)
                value = params.get(param_key)
                
                if value is None or value == '':
                    output_lines.append(f"  {param_key:35} = <MISSING>")
                else:
                    output_lines.append(f"  {param_key:35} = {value}")
    
    # Display subnet parameters if any
    subnet_params = {k: v for k, v in params.items() if 'subnet' in k.lower() and k not in displayed_params}
    if subnet_params:
        output_lines.append(f"\nSubnets:")
        output_lines.append("-" * 40)
        for param_key in sorted(subnet_params.keys()):
            displayed_params.add(param_key)
            value = subnet_params[param_key]
            if value is None or value == '':
                output_lines.append(f"  {param_key:35} = <MISSING>")
            else:
                output_lines.append(f"  {param_key:35} = {value}")
    
    # Display any remaining parameters (from stacks or overrides)
    remaining_params = {k: v for k, v in params.items() if k not in displayed_params}
    if remaining_params:
        output_lines.append(f"\nAdditional Parameters (Stack Outputs & Overrides):")
        output_lines.append("-" * 40)
        for param_key in sorted(remaining_params.keys()):
            value = remaining_params[param_key]
            if value is None or value == '':
                output_lines.append(f"  {param_key:35} = <MISSING>")
            else:
                output_lines.append(f"  {param_key:35} = {value}")
    
    # Summary of missing parameters
    missing_params = [k for k, v in params.items() if (v is None or v == '') and k != '_metadata']
    if missing_params:
        output_lines.append(f"\n" + "!" * 80)
        output_lines.append(f"WARNING: {len(missing_params)} parameter(s) missing:")
        output_lines.append("!" * 80)
        for param in missing_params:
            output_lines.append(f"  - {param}")
    
    output_lines.append("\n" + "=" * 80)
    output_lines.append(f"Total Parameters: {len(params) - 1}")  # -1 for _metadata
    output_lines.append(f"Missing Parameters: {len(missing_params)}")
    output_lines.append("=" * 80 + "\n")
    
    return "\n".join(output_lines)


def resolve_baseline_params(
    aws_account_id,
    aws_region,
    environment_name,
    hosted_zone_suffix,
    project_name=None,
    parent_stacks_csv=None,
    cli_params_list=None
):
    """
    Resolves baseline parameters for CloudFormation deployment.
    
    This function gathers parameters from multiple sources in the following order,
    with later sources overriding earlier ones:
    
    1. Base CLI arguments
    2. AWS infrastructure discovery (VPC, subnets, hosted zones)
    3. Auto-generated values (BuildId from git)
    4. Core global stack outputs (us-east-1)
    5. Parent stack outputs (if specified)
    6. CLI parameter overrides (--param KEY=VALUE) - Highest priority
    
    Args:
        aws_account_id: AWS account ID
        aws_region: AWS region for deployment
        environment_name: Environment name (e.g., dev, prod)
        hosted_zone_suffix: Hosted zone suffix to search for (e.g., "example.com")
        project_name: Project name (optional) - if not provided, stack names will be {ENV}-{STACK} instead of {PROJECT}-{ENV}-{STACK}
        parent_stacks_csv: Comma-separated parent stack base names (optional)
        cli_params_list: List of 'KEY=VALUE' strings for overrides (optional)
        
    Returns:
        dict: Flat dictionary of resolved parameters
        
    Example:
        params = resolve_baseline_params(
            aws_account_id="123456789012",
            aws_region="us-east-1",
            environment_name="dev",
            hosted_zone_suffix="example.com",
            project_name="myproject",
            parent_stacks_csv="CORE-vpc,CORE-network",
            cli_params_list=["BuildId=custom-123", "CustomParam=value"]
        )
    """
    print("Starting parameter resolution process...")
    print(f"AWS Account ID: {aws_account_id}")
    print(f"AWS Region: {aws_region}")
    print(f"Project Name: {project_name if project_name else '(not specified)'}")
    print(f"Environment Name: {environment_name}")
    print(f"Hosted Zone Suffix: {hosted_zone_suffix}")

    # 1. Initialize base parameters from CLI arguments
    print("\n=== Phase 1: Base Parameters from CLI Arguments ===")
    params = {
        "AccountId": aws_account_id,
        "Region": aws_region,
        "EnvironmentNameLower": environment_name.lower(),
        "EnvironmentNameUpper": environment_name.upper()
    }
    if project_name:
        params["ProjectName"] = project_name
    print(f"Base parameters: {params}")

    # 2. AWS infrastructure discovery
    print("\n=== Phase 2: AWS Infrastructure Discovery ===")
    
    # VPC data
    vpc_data = get_vpc_data(aws_region, environment_name)
    params.update(vpc_data)

    # Hosted zone data
    hosted_zone_data = get_hosted_zone_data(aws_region, hosted_zone_suffix)
    params.update(hosted_zone_data)
    
    # Subnet data
    vpc_id_for_subnets = params.get("VPCId")
    if vpc_id_for_subnets:
        subnet_data = get_subnet_data(aws_region, vpc_id_for_subnets)
        params.update(subnet_data)
    else:
        print("Warning: VPCId not found in params, skipping subnet data retrieval.")

    # 3. Auto-generated values (BuildId from git)
    print("\n=== Phase 3: Auto-generated Values ===")
    if "BuildId" not in params:
        try:
            git_hash = subprocess.check_output(['git', 'rev-parse', '--short', 'HEAD']).decode('utf-8').strip()
            params["BuildId"] = git_hash
            print(f"Added BuildId from git: {git_hash}")
        except subprocess.CalledProcessError as e:
            print(f"Warning: Could not determine git revision for BuildId: {e}. BuildId will not be set automatically.")
        except FileNotFoundError:
            print("Warning: git command not found. BuildId will not be set automatically.")

    # 4. Parent stack outputs
    print("\n=== Phase 4: Parent Stack Outputs ===")
    if parent_stacks_csv:
        parent_stack_entries = [entry.strip() for entry in parent_stacks_csv.split(',') if entry.strip()]
        if parent_stack_entries:
            print(f"Processing parent stacks for additional parameters: {parent_stack_entries}")
            for parent_entry in parent_stack_entries:
                # Parse {parent}@{region} format
                if '@' in parent_entry:
                    parent_stack_base_name, stack_region = parent_entry.split('@', 1)
                    parent_stack_base_name = parent_stack_base_name.strip()
                    stack_region = stack_region.strip()
                else:
                    parent_stack_base_name = parent_entry
                    stack_region = aws_region  # Default to deployment region
                
                if project_name:
                    full_parent_stack_name = f"{project_name.upper()}-{environment_name.upper()}-{parent_stack_base_name}".replace('_', '-')
                else:
                    full_parent_stack_name = f"{environment_name.upper()}-{parent_stack_base_name}".replace('_', '-')
                
                print(f"Retrieving outputs from parent stack: {full_parent_stack_name} in region {stack_region}...")
                print(f"  Parent entry: {parent_entry}")
                print(f"  Base stack name: {parent_stack_base_name}")
                print(f"  Target region: {stack_region}")
                
                parent_outputs = get_stack_outputs(stack_region, project_name, environment_name, parent_stack_base_name)
                
                if parent_outputs:
                    print(f"Successfully retrieved {len(parent_outputs)} output(s) from parent stack {full_parent_stack_name}")
                    print(f"Adding outputs from parent stack {full_parent_stack_name}: {parent_outputs}")
                    params.update(parent_outputs)
                else:
                    error_msg = f"CRITICAL ERROR: Failed to retrieve outputs from required parent stack '{full_parent_stack_name}' in region '{stack_region}'. This stack is required for deployment and must exist with valid outputs."
                    print(f"\n{'!' * 80}")
                    print(error_msg)
                    print(f"{'!' * 80}\n")
                    raise RuntimeError(error_msg)
        else:
            print("No valid parent stack names found in --parent-stacks input.")
    else:
        print("No parent stacks specified.")

    # 5. CLI parameter overrides
    print("\n=== Phase 5: CLI Parameter Overrides ===")
    if cli_params_list:
        print(f"Processing CLI parameters from --param to update gathered params: {cli_params_list}")
        for p_str in cli_params_list:
            if '=' in p_str:
                key, value = p_str.split('=', 1)
                if key in params:
                    print(f"Overriding parameter '{key}' with value from --param: '{value}' (was: '{params.get(key)}')")
                else:
                    print(f"Adding new parameter from --param: '{key}' = '{value}'")
                params[key] = value
            else:
                print(f"Warning: CLI parameter '{p_str}' from --param is not in KEY=VALUE format and will be ignored.")
    else:
        print("No CLI parameter overrides provided.")

    print("\n=== Parameter Resolution Complete ===")
    print(f"Total parameters resolved: {len(params)}")
    
    # Track parameter categories for better organization
    param_metadata = {
        'base': ['AccountId', 'Region', 'ProjectName', 'EnvironmentNameLower', 'EnvironmentNameUpper'],
        'vpc': ['VPCId', 'VPCCidr'],
        'hosted_zones': ['PublicHostedZoneName', 'PublicHostedZoneId', 'PrivateHostedZoneName', 'PrivateHostedZoneId'],
        'build': ['BuildId']
    }
    
    # Store metadata in params for display purposes
    params['_metadata'] = param_metadata
    
    return params


def main():
    """
    CLI interface for parameter resolution.
    
    Provides JSON and text output formats for resolved parameters.
    """
    parser = argparse.ArgumentParser(
        prog='params.py',
        description='Resolve CloudFormation deployment parameters from AWS infrastructure and stack outputs',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Pretty output with categories (default)
  python scripts/params.py \\
    --aws-account-id 123456789012 \\
    --aws-region us-east-1 \\
    --project-name myproject \\
    --environment-name dev \\
    --hosted-zone example.com

  # JSON output for programmatic use
  python scripts/params.py \\
    --aws-account-id 123456789012 \\
    --aws-region us-east-1 \\
    --project-name myproject \\
    --environment-name dev \\
    --hosted-zone example.com \\
    --output json

  # Text output for shell scripts
  python scripts/params.py \\
    --aws-account-id 123456789012 \\
    --aws-region us-east-1 \\
    --project-name myproject \\
    --environment-name dev \\
    --hosted-zone example.com \\
    --output text

  # With parent stacks and overrides
  python scripts/params.py \\
    --aws-account-id 123456789012 \\
    --aws-region us-east-1 \\
    --project-name myproject \\
    --environment-name dev \\
    --hosted-zone example.com \\
    --parent-stacks CORE-global@us-east-1,CORE-vpc,CORE-network \\
    --param BuildId=custom-123 \\
    --param CustomParam=value

  # Without project name (stack names will be {ENV}-{STACK})
  python scripts/params.py \\
    --aws-account-id 123456789012 \\
    --aws-region us-east-1 \\
    --environment-name dev \\
    --hosted-zone example.com \\
    --parent-stacks CORE-global@us-east-1

  # Quiet mode (suppress progress messages)
  python scripts/params.py \\
    --aws-account-id 123456789012 \\
    --aws-region us-east-1 \\
    --project-name myproject \\
    --environment-name dev \\
    --hosted-zone example.com \\
    --output json \\
    --quiet
        """
    )
    
    parser.add_argument("--aws-account-id", 
                        required=True, 
                        metavar='ACCOUNT_ID',
                        help="AWS Account ID")
    parser.add_argument("--aws-region", 
                        required=True,
                        metavar='REGION',
                        help="AWS region (e.g., us-east-1)")
    parser.add_argument("--project-name", 
                        required=False,
                        metavar='PROJECT',
                        help="Project name (optional). If not provided, stack names will be {ENV}-{STACK} instead of {PROJECT}-{ENV}-{STACK}")
    parser.add_argument("--environment-name", 
                        required=True,
                        metavar='ENVIRONMENT',
                        help="Environment name (e.g., dev, staging, prod)")
    parser.add_argument("--hosted-zone", 
                        required=True,
                        metavar='ZONE',
                        help="Hosted zone suffix (e.g., example.com)")
    parser.add_argument("--parent-stacks",
                        metavar='STACKS',
                        help="Comma-separated parent stack names with optional region (e.g., 'CORE-global@us-east-1,CORE-vpc,CORE-network@eu-west-1'). Region defaults to --region if not specified.")
    parser.add_argument("--param", 
                        action='append', 
                        default=[],
                        metavar='KEY=VALUE',
                        help="Additional parameters (can be specified multiple times)")
    parser.add_argument("--output", 
                        choices=["json", "text", "pretty"], 
                        default="pretty",
                        metavar='FORMAT',
                        help="Output format: json, text, or pretty (default: pretty)")
    parser.add_argument("--quiet", 
                        action='store_true',
                        help="Suppress progress messages (auto-enabled for JSON output)")
    
    args = parser.parse_args()
    
    # Auto-enable quiet mode for JSON output
    if args.output == 'json':
        args.quiet = True
    
    # Redirect print statements if quiet mode
    original_print = print
    if args.quiet:
        import io
        import contextlib
        print_buffer = io.StringIO()
        def quiet_print(*args_inner, **kwargs):
            # Only suppress stdout, keep stderr
            if kwargs.get('file') == sys.stderr:
                original_print(*args_inner, **kwargs)
            else:
                original_print(*args_inner, **kwargs, file=print_buffer)
        # Temporarily replace print
        import builtins
        builtins.print = quiet_print
    
    try:
        params = resolve_baseline_params(
            aws_account_id=args.aws_account_id,
            aws_region=args.aws_region,
            environment_name=args.environment_name,
            hosted_zone_suffix=args.hosted_zone,
            project_name=args.project_name,
            parent_stacks_csv=args.parent_stacks,
            cli_params_list=args.param if args.param else None
        )
        
        # Restore original print if quiet mode
        if args.quiet:
            import builtins
            builtins.print = original_print
        
        # Output results
        if args.output == "json":
            # JSON output: silent, no headers, just pure JSON
            output_params = {k: v for k, v in params.items() if k != '_metadata'}
            print(json.dumps(output_params, indent=2))
        elif args.output == "text":
            # Text output: no header for easy parsing
            for key, value in sorted(params.items()):
                if key != '_metadata':
                    print(f"{key}={value}")
        else:  # pretty
            # Pretty output: with header
            print("\n=== RESOLVED PARAMETERS ===")
            print(format_params_pretty(params))
        
        return 0
        
    except Exception as e:
        # Restore original print if quiet mode
        if args.quiet:
            import builtins
            builtins.print = original_print
        print(f"\nError: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
