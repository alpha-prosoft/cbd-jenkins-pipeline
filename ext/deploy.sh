#!/bin/bash

set -euxo pipefail

echo "Started deploy.sh"

while getopts “:r:t:” opt; do
  case $opt in
  r) ROLE=$OPTARG ;;
  t) TAGS=$OPTARG ;;
  *) echo "Usage: cmd [-r] [-t]"a && exit 1 ;;
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
  http://169.254.169.254/latest/dynamic/instance-identity/document |
  jq -r .region)

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

if [ ! -f "/dist/artifacts.json" ]; then
  echo "{}" >/dist/artifacts.json
fi

target_access=$(cat /dist/artifacts.json |
  jq '. + {"AWS_ACCESS_KEY_ID" :  "'$AWS_ACCESS_KEY_ID'",
          "AWS_SESSION_TOKEN" : "'$AWS_SESSION_TOKEN'",
          "AWS_SECRET_ACCESS_KEY" : "'$AWS_SECRET_ACCESS_KEY'",
          "AWS_DEFAULT_REGION" : "'${AWS_DEFAULT_REGION}'",
          }')

pipeline_access=$(cat /dist/artifacts.json |
  jq '. + {"AWS_ACCESS_KEY_ID" :  "'${PIPELINE_AWS_ACCESS_KEY_ID}'",
          "AWS_SESSION_TOKEN" : "'${PIPELINE_AWS_SESSION_TOKEN}'",
          "AWS_SECRET_ACCESS_KEY" : "'${PIPELINE_AWS_SECRET_ACCESS_KEY}'",
          "AWS_DEFAULT_REGION" : "'${AWS_DEFAULT_REGION}'",
          "AccountId" : "'${PIPELINE_ACCOUNT_ID}'",
          "ServiceName" : "'${ServiceName}'"
          }')

params=$(echo "${target_access}" |
  jq '. + {"BuildId" : "'${BUILD_ID}'",
          "Version" : "'${BUILD_ID}'",
          "Region" : "'${AWS_DEFAULT_REGION}'",
          "AccountId" : "'${TargetAccountId}'",
          "EnvironmentNameUpper" : "'${EnvironmentNameUpper}'",
          "EnvironmentNameLower" : "'${EnvironmentNameUpper,,}'",
          "ProjectName" : "'${ProjectName:-alpha}'",
          "ServiceName" : "'${ServiceName}'"
           }')

echo '{ "params" : '${params}', 
        "pipeline_params" : '${pipeline_access}', 
        "resource_tags" : {} }' >$work_dir/group_vars/all.json

echo "#### Final params in group_vars/all.json #############"
jq '.' $work_dir/group_vars/all.json
echo "######################################################"

echo "Executing ansible deployment"

export ANSIBLE_FORCE_COLOR=true

echo "localhost" >"$work_dir/inventory"
cp /etc/ansible/ansible.cfg ./ansible.cfg

echo "callbacks_enabled = profile_tasks" >>./ansible.cfg
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
