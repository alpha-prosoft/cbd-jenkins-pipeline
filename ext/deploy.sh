#!/bin/bash

set -euxo pipefail

echo "Started deploy.sh"

# Determine script directory and repository root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
echo "Script directory: ${SCRIPT_DIR}"
echo "Repository root: ${REPO_ROOT}"

while getopts “:r:t:” opt; do
  case $opt in
  r) ROLE=$OPTARG ;;
  t) TAGS=$OPTARG ;;
  *) echo "Usage: cmd [-r] [-t]"a && exit 1 ;;
  esac
done

echo "Role ${ROLE:-}"
echo "Tags ${TAGS:-}"

# Safeguard: fail if legacy variables are set
if [[ ! -z "${SERVICE_NAME:-}" ]]; then
  echo "ERROR: SERVICE_NAME is set but no longer supported. Use RESOURCE_NAME instead."
  exit 1
fi

if [[ ! -z "${PROJECT_NAME:-}" ]]; then
  echo "ERROR: PROJECT_NAME is set but no longer supported. Use RESOURCE_NAME instead."
  exit 1
fi

echo "Current environment (secrets masked)"
env | grep -vE '(SECRET|TOKEN|PASSWORD|KEY)' | sort

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

if [[ ! -z "${RESOURCE_NAME:-}" ]]; then
  export ResourceName="${RESOURCE_NAME}"
fi

# Validate required variables
if [[ -z "${ResourceName:-}" ]]; then
  echo "ERROR: ResourceName is not set. Please set RESOURCE_NAME environment variable."
  exit 1
fi

if [[ -z "${BUILD_ID:-}" ]]; then
  echo "ERROR: BUILD_ID is not set"
  exit 1
fi

if [[ -z "${TargetAccountId:-}" ]]; then
  echo "ERROR: TargetAccountId is not set"
  exit 1
fi

if [[ -z "${EnvironmentNameUpper:-}" ]]; then
  echo "ERROR: EnvironmentNameUpper is not set"
  exit 1
fi

echo "Building ${ResourceName}"

echo "We are running inside ${work_dir}"

echo "Setting up ansible directories"
mkdir -p $work_dir/group_vars

SESSION_TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 21600")

if [[ -z "${SESSION_TOKEN}" ]]; then
  echo "ERROR: Failed to retrieve EC2 metadata session token"
  exit 1
fi

export AWS_DEFAULT_REGION=$(curl -s -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" \
  http://169.254.169.254/latest/dynamic/instance-identity/document |
  jq -r .region)

if [[ -z "${AWS_DEFAULT_REGION}" || "${AWS_DEFAULT_REGION}" == "null" ]]; then
  echo "ERROR: Failed to retrieve AWS_DEFAULT_REGION from instance metadata"
  exit 1
fi

echo "AWS Region: ${AWS_DEFAULT_REGION}"

echo "Assuming role in target account"
SESSION=$(aws sts assume-role \
  --role-arn arn:aws:iam::${TargetAccountId}:role/DeliveryRole \
  --role-session-name "${ResourceName}-deployment-${BUILD_ID}" \
  --endpoint https://sts.${AWS_DEFAULT_REGION}.amazonaws.com \
  --region ${AWS_DEFAULT_REGION})

CURRENT_ROLE=$(curl -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" http://169.254.169.254/latest/meta-data/iam/security-credentials/)
curl -o security-credentials.json -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/${CURRENT_ROLE}/

export PIPELINE_AWS_ACCESS_KEY_ID=$(cat security-credentials.json | jq -r '.AccessKeyId')
export PIPELINE_AWS_SECRET_ACCESS_KEY=$(cat security-credentials.json | jq -r '.SecretAccessKey')
export PIPELINE_AWS_SESSION_TOKEN=$(cat security-credentials.json | jq -r '.Token')
export PIPELINE_ACCOUNT_ID=$(curl -s http://169.254.169.254/latest/dynamic/instance-identity/document | jq -r '.accountId')

# Validate pipeline credentials
if [[ -z "${PIPELINE_AWS_ACCESS_KEY_ID}" || "${PIPELINE_AWS_ACCESS_KEY_ID}" == "null" ]]; then
  echo "ERROR: Failed to retrieve PIPELINE_AWS_ACCESS_KEY_ID"
  exit 1
fi

if [[ -z "${PIPELINE_AWS_SECRET_ACCESS_KEY}" || "${PIPELINE_AWS_SECRET_ACCESS_KEY}" == "null" ]]; then
  echo "ERROR: Failed to retrieve PIPELINE_AWS_SECRET_ACCESS_KEY"
  exit 1
fi

if [[ -z "${PIPELINE_AWS_SESSION_TOKEN}" || "${PIPELINE_AWS_SESSION_TOKEN}" == "null" ]]; then
  echo "ERROR: Failed to retrieve PIPELINE_AWS_SESSION_TOKEN"
  exit 1
fi

echo "Pipeline credentials successfully retrieved and validated"

export AWS_ACCESS_KEY_ID=$(echo $SESSION | jq -r '.Credentials.AccessKeyId')
export AWS_SECRET_ACCESS_KEY=$(echo $SESSION | jq -r '.Credentials.SecretAccessKey')
export AWS_SESSION_TOKEN=$(echo $SESSION | jq -r '.Credentials.SessionToken')

# Validate AWS credentials
if [[ -z "${AWS_ACCESS_KEY_ID}" || "${AWS_ACCESS_KEY_ID}" == "null" ]]; then
  echo "ERROR: Failed to retrieve AWS_ACCESS_KEY_ID from assumed role"
  exit 1
fi

if [[ -z "${AWS_SECRET_ACCESS_KEY}" || "${AWS_SECRET_ACCESS_KEY}" == "null" ]]; then
  echo "ERROR: Failed to retrieve AWS_SECRET_ACCESS_KEY from assumed role"
  exit 1
fi

if [[ -z "${AWS_SESSION_TOKEN}" || "${AWS_SESSION_TOKEN}" == "null" ]]; then
  echo "ERROR: Failed to retrieve AWS_SESSION_TOKEN from assumed role"
  exit 1
fi

echo "AWS credentials successfully retrieved and validated"

# Resolve deployment parameters using params.py
echo "Resolving deployment parameters using params.py..."

# Determine hosted zone suffix (can be overridden via HOSTED_ZONE_SUFFIX env var)
# Default: ${EnvironmentNameLower}.alpha-prosoft.com (e.g., dev.alpha-prosoft.com)
HOSTED_ZONE_SUFFIX="${HOSTED_ZONE_SUFFIX:-${EnvironmentNameUpper,,}.alpha-prosoft.com}"
echo "Using hosted zone suffix: ${HOSTED_ZONE_SUFFIX}"

# Build params.py arguments
PARAMS_ARGS=(
  --aws-account-id "${TargetAccountId}"
  --aws-region "${AWS_DEFAULT_REGION}"
  --environment-name "${EnvironmentNameUpper}"
  --hosted-zone "${HOSTED_ZONE_SUFFIX}"
  --output json
  --quiet
)

# Add resource name if specified
if [[ ! -z "${ResourceName:-}" ]]; then
  PARAMS_ARGS+=(--resource-name "${ResourceName}")
fi

# Add parent stacks if specified
if [[ ! -z "${PARENT_STACKS:-}" ]]; then
  PARAMS_ARGS+=(--parent-stacks "${PARENT_STACKS}")
fi

# Add custom parameters
PARAMS_ARGS+=(
  --param "BuildId=${BUILD_ID}"
  --param "Version=${BUILD_ID}"
  --param "ResourceName=${ResourceName}"
)

# Call params.py to resolve all infrastructure parameters
echo "Calling params.py with arguments: ${PARAMS_ARGS[@]}"
PARAMS_PY_PATH="${REPO_ROOT}/shared/params.py"
echo "Using params.py at: ${PARAMS_PY_PATH}"

if [ ! -f "${PARAMS_PY_PATH}" ]; then
  echo "ERROR: params.py not found at ${PARAMS_PY_PATH}"
  echo "Available files in ${REPO_ROOT}/shared/:"
  ls -la "${REPO_ROOT}/shared/" 2>/dev/null || echo "Directory not found"
  exit 1
fi

RESOLVED_PARAMS=$(python3 "${PARAMS_PY_PATH}" "${PARAMS_ARGS[@]}")

if [ $? -ne 0 ]; then
  echo "ERROR: Failed to resolve parameters using params.py"
  exit 1
fi

echo "Parameters resolved successfully"

# Read artifacts.json or create empty object
if [ ! -f "/dist/artifacts.json" ]; then
  echo "{}" >/dist/artifacts.json
fi

# Merge resolved params with artifacts and AWS credentials
params=$(echo "${RESOLVED_PARAMS}" | jq '. + {
  "AWS_ACCESS_KEY_ID": "'$AWS_ACCESS_KEY_ID'",
  "AWS_SESSION_TOKEN": "'$AWS_SESSION_TOKEN'",
  "AWS_SECRET_ACCESS_KEY": "'$AWS_SECRET_ACCESS_KEY'",
  "AWS_DEFAULT_REGION": "'${AWS_DEFAULT_REGION}'"
}')

# Add artifacts.json content to params
artifacts_content=$(cat /dist/artifacts.json)
params=$(echo "${params}" | jq '. + '"${artifacts_content}"'')

# Build pipeline access credentials
pipeline_access=$(jq -n \
  --arg accessKeyId "${PIPELINE_AWS_ACCESS_KEY_ID}" \
  --arg sessionToken "${PIPELINE_AWS_SESSION_TOKEN}" \
  --arg secretAccessKey "${PIPELINE_AWS_SECRET_ACCESS_KEY}" \
  --arg region "${AWS_DEFAULT_REGION}" \
  --arg accountId "${PIPELINE_ACCOUNT_ID}" \
  --arg resourceName "${ResourceName}" \
  '{
    "AWS_ACCESS_KEY_ID": $accessKeyId,
    "AWS_SESSION_TOKEN": $sessionToken,
    "AWS_SECRET_ACCESS_KEY": $secretAccessKey,
    "AWS_DEFAULT_REGION": $region,
    "AccountId": $accountId,
    "ResourceName": $resourceName
  }')

# Build resource tags
resource_tags=$(jq -n \
  --arg resourceName "${ResourceName}" \
  --arg envNameLower "${EnvironmentNameUpper,,}" \
  --arg buildId "${BUILD_ID}" \
  --arg version "${BUILD_ID}" \
  '{
    "ResourceName": $resourceName,
    "EnvironmentNameLower": $envNameLower,
    "BuildId": $buildId,
    "Version": $version
  }')

# Create final group_vars/all.json
jq -n \
  --argjson params "${params}" \
  --argjson pipelineParams "${pipeline_access}" \
  --argjson resourceTags "${resource_tags}" \
  --arg repoDir "${HOME}" \
  '{
    "params": $params,
    "pipeline_params": $pipelineParams,
    "resource_tags": $resourceTags,
    "repo_dir": $repoDir
  }' >$work_dir/group_vars/all.json

echo "#### Final params in group_vars/all.json (secrets masked) #############"
jq '.params.AWS_ACCESS_KEY_ID = "***MASKED***" | 
    .params.AWS_SECRET_ACCESS_KEY = "***MASKED***" | 
    .params.AWS_SESSION_TOKEN = "***MASKED***" | 
    .pipeline_params.AWS_ACCESS_KEY_ID = "***MASKED***" | 
    .pipeline_params.AWS_SECRET_ACCESS_KEY = "***MASKED***" | 
    .pipeline_params.AWS_SESSION_TOKEN = "***MASKED***"' $work_dir/group_vars/all.json
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
