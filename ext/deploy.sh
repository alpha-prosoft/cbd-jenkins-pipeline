#!/bin/bash

set -euxo pipefail

echo "Started deploy.sh"

while getopts “:r:t:” opt; do
  case $opt in
    r) ROLE=$OPTARG ;;
    t) TAGS=$OPTARG ;;
    *) echo "Usage: cmd [-r] [-t]"a && exit 1
  esac
done



echo "Role ${ROLE:-}"
echo "Tags ${TAGS:-}"

echo "Current environment"
env

echo "######################"

ls -la /dist/ansible/deploy/roles

cd /dist

echo "Checking current user"
id

echo "Docker socket permissions"
ls -la /var/run/docker.sock

echo "Prepare request directory"
export work_dir="/dist/${BUILD_ID}"
mkdir -p $work_dir

if [[ ! -z "${SERVICE_NAME:-}" ]]; then 
  export ServiceName="${SERVICE_NAME}"
fi

if [[ ! -z "${PROJECT_NAME:-}" ]]; then 
  export ProjectName="${PROJECT_NAME}"
fi

echo "Building ${ProjectName}/${ServiceName}"

echo "We are running inside ${work_dir}"

echo "Setting up ansible directories"
mkdir -p $work_dir/group_vars

SESSION_TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

export AWS_DEFAULT_REGION=$(curl -s -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" \
                               http://169.254.169.254/latest/dynamic/instance-identity/document \
	                             | jq -r .region)

echo "Assuming role in target account"
SESSION=$(aws sts assume-role \
            --role-arn arn:aws:iam::${TargetAccountId}:role/DeliveryRole \
            --role-session-name "${ServiceName}-deployment-${BUILD_ID}" \
            --endpoint https://sts.${AWS_DEFAULT_REGION}.amazonaws.com \
            --region ${AWS_DEFAULT_REGION})

CURRENT_ROLE=$(curl -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/)
curl -o security-credentials.json -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" \
          http://169.254.169.254/latest/meta-data/iam/security-credentials/${CURRENT_ROLE}/


export PIPELINE_AWS_ACCESS_KEY_ID=$(cat security-credentials.json | jq -r '.AccessKeyId')
export PIPELINE_AWS_SECRET_ACCESS_KEY=$(cat security-credentials.json | jq -r '.SecretAccessKey')
export PIPELINE_AWS_SESSION_TOKEN=$(cat security-credentials.json | jq -r '.Token')
export PIPELINE_ACCOUNT_ID=$(curl -s http://169.254.169.254/latest/dynamic/instance-identity/document | jq -r '.accountId')

export AWS_ACCESS_KEY_ID=$(echo $SESSION | jq -r '.Credentials.AccessKeyId')
export AWS_SECRET_ACCESS_KEY=$(echo $SESSION | jq -r '.Credentials.SecretAccessKey')
export AWS_SESSION_TOKEN=$(echo $SESSION | jq -r '.Credentials.SessionToken')

aws ssm get-parameter \
       --name "/install/config" \
       --with-decryption \
       --query 'Parameter.Value' \
       --output text > /tmp/config.json || echo "{}" > /tmp/config.json

echo "#### Deployment config: "
cat /tmp/config.json
echo "###################"


vpc_filter="aws-controltower-"
if [[ ! -z "$(cat /tmp/config.json | jq -r '.deployer."vpc-filter" // empty')" ]]; then 
   vpc_filter="$(cat /tmp/config.json | jq -r '.deployer."vpc-filter"')"
fi
echo "Fetching VPC data using filter: ${vpc_filter}"
VPC_ID=$(aws ec2 describe-vpcs \
           --query "Vpcs[*].VpcId" \
           --filter "Name=tag:Name,Values=${vpc_filter}*" \
           --output text)

VPC_CIDR=$(aws ec2 describe-vpcs \
             --query "Vpcs[*].CidrBlock" \
             --filter "Name=tag:Name,Values=${vpc_filter}*"  \
             --output text)


echo "Fetching subnets"
SUBNET_IDS=$(aws ec2 describe-subnets \
    --query "Subnets[*].{Id:SubnetId,Name:Tags[?Key=='Name']|[0].Value}" \
    --output text | awk '{printf $1":"$2"\n"}')

echo "Fetching subnet CIRDs"
SUBNET_CIDRS=$(aws ec2 describe-subnets \
    --query "Subnets[*].{Name:Tags[?Key=='Name']|[0].Value,Cidr:CidrBlock}" \
    --output text | awk '{printf $1":"$2"\n"}')
    
RESULT_SUBNETS=$(for i in $SUBNET_IDS; do echo "\"${i##*-}\" : \"${i%%:*}\","; done)
RESULT_SUBNET_CIDRS=$(for i in $SUBNET_CIDRS; do echo "\"${i##*-}Cidr\" : \"${i%%:*}\","; done)

echo "List hosted zones since this usally beaks the build"
aws route53 list-hosted-zones \
        --query 'HostedZones[*].[Name,Config.PrivateZone,Name]' \
        --output text

hosted_zone_filter="alpha-prosoft"
echo "Hosted zone filter: ${hosted_zone_filter}"
if [[ ! -z "$(cat /tmp/config.json | jq -r '.deployer.hostedZoneFilter // empty')" ]]; then 
   hosted_zone_filter="$(cat /tmp/config.json | jq -r '.deployer.hostedZoneFilter')"
fi

PRIVATE_HOSTED_ZONE_ID=$(aws route53 list-hosted-zones \
            --query "HostedZones[*].[Id,Config.PrivateZone,Name]" \
            --output text | grep "True" | grep "${hosted_zone_filter}" \
            | awk '{printf $1}' || echo "NoZone")

PRIVATE_HOSTED_ZONE_NAME=$(aws route53 list-hosted-zones \
            --query "HostedZones[*].[Name,Config.PrivateZone,Name]" \
            --output text | grep "True" | grep "${hosted_zone_filter}" \
	    | awk '{printf $1}' || echo "NoZone")

PUBLIC_HOSTED_ZONE_ID=$(aws route53 list-hosted-zones \
            --query "HostedZones[*].[Id,Config.PrivateZone,Name]" \
     --output text | grep "False" | grep "${hosted_zone_filter}" | awk '{printf $1}')

PUBLIC_HOSTED_ZONE_NAME=$(aws route53 list-hosted-zones \
            --query "HostedZones[*].[Name,Config.PrivateZone,Name]" \
     --output text | grep "False" | grep "${hosted_zone_filter}" | awk '{printf $1}')


aws ec2 describe-route-tables --query 'RouteTables[].{Name:Tags[?Key=='\''Name'\'']|[0].Value, Id:RouteTableId}' --output text
aws ec2 describe-route-tables --query 'RouteTables[].{Name:Tags[?Key=='\''Name'\'']|[0].Value, Id:RouteTableId}' --output text | grep -vi None
aws ec2 describe-route-tables --query 'RouteTables[].{Name:Tags[?Key=='\''Name'\'']|[0].Value, Id:RouteTableId}' --output text | grep -vi None | awk '{printf $1":"$2"\n"}'

ROUTE_TABLES=$(aws ec2 describe-route-tables \
    --query "RouteTables[].{Name:Tags[?Key=='Name']|[0].Value, Id:RouteTableId}" \
    --output text | grep -vi "None" | awk '{printf $1":"$2"\n"}')
    
RESULT_ROUTE_TABLES=$(for i in $ROUTE_TABLES; do echo "\"${i##*-}\" : \"${i%%:*}\","; done)


priority="$(aws ssm get-parameter \
         --name "/priority/${ServiceName}" \
         --query 'Parameter.Value' \
         --output text 2>/dev/null || echo '')"

max_priority="$(aws ssm get-parameter \
             --name "/priority/current"  \
             --query 'Parameter.Value' \
             --output text 2>/dev/null || echo '')"

echo "Current priority ${priority}"
echo "Max priority ${max_priority}"

if [[ -z "${max_priority}" ]]; then
  max_priority=1
  aws ssm put-parameter \
    --name "/priority/current" \
    --type "String" \
    --value ${max_priority} \
    --overwrite
fi

if [[ -z "${priority}" ]]; then
  priority=$((max_priority + ( RANDOM % 10 ) + 1))
  aws ssm put-parameter \
    --name "/priority/current" \
    --type "String" \
    --value ${priority} \
    --overwrite

  aws ssm put-parameter \
    --name "/priority/${ServiceName}" \
    --type "String" \
    --value ${priority} \
    --overwrite
fi

echo "Final priority ${priority}"

if [ ! -f "/dist/artifacts.json" ]; then
 echo "{}" > /dist/artifacts.json
fi

target_access=$(cat /dist/artifacts.json | \
 jq '. + {"AWS_ACCESS_KEY_ID" :  "'$AWS_ACCESS_KEY_ID'",
          "AWS_SESSION_TOKEN" : "'$AWS_SESSION_TOKEN'",
          "AWS_SECRET_ACCESS_KEY" : "'$AWS_SECRET_ACCESS_KEY'",
          "AWS_DEFAULT_REGION" : "'${AWS_DEFAULT_REGION}'",
          }')


pipeline_access=$(cat /dist/artifacts.json | \
 jq '. + {"AWS_ACCESS_KEY_ID" :  "'${PIPELINE_AWS_ACCESS_KEY_ID}'",
          "AWS_SESSION_TOKEN" : "'${PIPELINE_AWS_SESSION_TOKEN}'",
          "AWS_SECRET_ACCESS_KEY" : "'${PIPELINE_AWS_SECRET_ACCESS_KEY}'",
          "AWS_DEFAULT_REGION" : "'${AWS_DEFAULT_REGION}'",
          "AccountId" : "'${PIPELINE_ACCOUNT_ID}'",
          "ServiceName" : "'${ServiceName}'"
          }')


load_balancer_scheme="internet-facing"
echo "Hosted zone filter: ${hosted_zone_filter}"
if [[ ! -z "$(cat /tmp/config.json | jq -r '.deployer.scheme // empty')" ]]; then 
   load_balancer_scheme="$(cat /tmp/config.json | jq -r '.deployer.scheme')"
fi

params=$(echo "${target_access}" | \
 jq '. + {"BuildId" : "'${BUILD_ID}'",
          "Version" : "'${BUILD_ID}'",
          "Region" : "'${AWS_DEFAULT_REGION}'",
          "VPCId" : "'${VPC_ID}'",
          "VPCCidr" : "'${VPC_CIDR}'",
          '"${RESULT_SUBNETS}"'
          '"${RESULT_SUBNET_CIDRS}"'
          '"${RESULT_ROUTE_TABLES}"'
          "AccountId" : "'${TargetAccountId}'",
	  "LoadBalancerScheme": "'${load_balancer_scheme}'",
          "Priority" : "'${priority}'",
          "PrivateHostedZoneName" : "'${PRIVATE_HOSTED_ZONE_NAME%.*}'",
          "PrivateHostedZoneId" : "'${PRIVATE_HOSTED_ZONE_ID##*/}'",
          "PublicHostedZoneName" : "'${PUBLIC_HOSTED_ZONE_NAME%.*}'",
          "PublicHostedZoneId" : "'${PUBLIC_HOSTED_ZONE_ID##*/}'",
          "EnvironmentNameUpper" : "'${EnvironmentNameUpper}'",
          "EnvironmentNameLower" : "'${EnvironmentNameUpper,,}'",
          "DeploymentS3BucketName" : "'${TargetAccountId}-${EnvironmentNameUpper,,}-deployment'",
          "RuntimeImage" : "'${ServiceName}-runtime-image:${BUILD_ID}'",
          "HostedZoneName" : "'${PUBLIC_HOSTED_ZONE_NAME}'",
          "ProjectName" : "'${ProjectName:-alpha}'",
          "ServiceName" : "'${ServiceName}'"
           }')




echo '{ "params" : '${params}', 
        "pipeline_params" : '${pipeline_access}', 
        "resource_tags" : {} }' > $work_dir/group_vars/all.json

correction_script=$(mktemp)
cat > ${correction_script} <<EOF
import sys 
import json

config_file=sys.argv[1]
out_file=sys.argv[2]


with open(out_file) as out:
  group_vars = json.load(out)
  data = group_vars["params"]
  

with open(config_file) as config: 
  config = json.load(config)

print("Checking for config")
with open(out_file, "w") as out:
  if "deployer" in config and "paramsSubstitutions" in config["deployer"]:
    print("Found deployment config")
    if "paramsSubstitutions" in config["deployer"]:
      print("Found substitutions")
      substitutions = config["deployer"]["paramsSubstitutions"]
      new_data = {}
      for key, value in data.items():
        if key in substitutions:
          new_key = substitutions[key]
          print(f"Substituting: {key} for {new_key}")
          new_data[new_key] = value
        else:
          new_data[key] = value  
      group_vars["params"] = new_data

  if "deployer" in config and "oidc" in config["deployer"]:
    print("Found OIDC config")
    oidc = config["deployer"]["oidc"]
    attribute_mapping = oidc.get("attributeMapping", {})
    provider_details = oidc.get("providerDetails", {})
    params = group_vars["params"]

    params["OidcProviderName"] = oidc.get("providerName")

    params["OidcAttributeMappingEmail"] = attribute_mapping.get("email")
    params["OidcAttributeMappingName"] = attribute_mapping.get("name")
    params["OidcAttributeMappingProfile"] = attribute_mapping.get("profile")
    params["OidcAttributeMappingUsername"] = attribute_mapping.get("username")

    params["OidcProviderDetailsClientId"] = provider_details.get("clientId")
    params["OidcProviderDetailsClientSecret"] = provider_details.get("clientSecret")
    params["OidcProviderDetailsAuthorizedScopes"] = provider_details.get("authorizedScopes", "openid email profile")
    params["OidcProviderDetailsOidcIssuer"] = provider_details.get("oidcIssuer")
    params["OidcProviderDetailsAttributeRequestMethod"] = provider_details.get("attributeRequestMethod", "GET")

  json.dump(group_vars, out)
EOF

python3 ${correction_script} /tmp/config.json $work_dir/group_vars/all.json 

echo "#### Final params in group_vars/all.json #############" 
jq '.' $work_dir/group_vars/all.json
echo "######################################################"

echo "Executing ansible deployment"

export ANSIBLE_FORCE_COLOR=true

echo "localhost" > "$work_dir/inventory"
cp /etc/ansible/ansible.cfg ./ansible.cfg

echo "callbacks_enabled = profile_tasks" >> ./ansible.cfg
ANSIBLE_CONFIG="$(pwd)/ansible.cfg"
export ANSIBLE_CONFIG

echo "### <ansible-config> ###"""
cat $ANSIBLE_CONFIG
echo "### </ansible-config> ###"""

ansible-playbook \
         -i $work_dir/inventory \
         --connection=local \
         --extra-vars "BuildId=${BUILD_ID}" \
         --tags "${TAGS:-untagged}" \
         ${ANSIBLE_LOG_LEVEL:--vv} \
         $HOME/ansible/deploy/deploy.yml

