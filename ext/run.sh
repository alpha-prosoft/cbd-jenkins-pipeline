#!/bin/bash

set -euo pipefail

echo "Going to deploy me some stuff"

export RESOURCE_NAME=$1
export ENV_NAME_UPPER=$2

if [[ -z "${BUILD_ID:-}" ]]; then
  echo "ERROR: BUILD_ID is not set. Source build.sh first or export BUILD_ID of an existing image."
  return 1 2>/dev/null || exit 1
fi

pipeline_url="https://raw.githubusercontent.com/alpha-prosoft/cbd-jenkins-pipeline/master"

target_dir=${PWD}/target
mkdir -p ${target_dir}/ext ${target_dir}/shared

for file in ext/deploy.sh shared/params.py; do
  curl -fsS -H 'Cache-Control: no-cache' ${pipeline_url}/${file} > ${target_dir}/${file}
done
chmod 755 ${target_dir}/ext/deploy.sh

export TARGET_ACCOUNT_ID="$(aws sts get-caller-identity | jq -r '.Account')"

# When not on EC2 the deploy container has no instance metadata to authenticate
# with, so pass the current credentials and region into it. On EC2 these are
# empty and deploy.sh falls back to instance metadata.
cred_args=()
imds_token=$(curl -s -m 2 -X PUT "http://169.254.169.254/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60" || true)
if [[ -z "${imds_token}" ]]; then
  # Off EC2: local SSO credentials can be very short-lived, so assume a role for a
  # longer-lived (up to 1h) session and pass that into the deploy container.
  assume_role_name="${DEPLOY_ASSUME_ROLE:-DeliveryRole}"
  echo "Not on EC2 - assuming ${assume_role_name} for the deploy container session"
  assumed=$(aws sts assume-role \
    --role-arn "arn:aws:iam::${TARGET_ACCOUNT_ID}:role/${assume_role_name}" \
    --role-session-name "local-deploy-${RESOURCE_NAME}-${BUILD_ID}" \
    --duration-seconds 3600 \
    --query 'Credentials' --output json)
  cred_args=(
    -e AWS_ACCESS_KEY_ID="$(jq -r '.AccessKeyId' <<<"${assumed}")"
    -e AWS_SECRET_ACCESS_KEY="$(jq -r '.SecretAccessKey' <<<"${assumed}")"
    -e AWS_SESSION_TOKEN="$(jq -r '.SessionToken' <<<"${assumed}")"
    -e AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$(aws configure get region)}"
  )
fi

docker run -v /var/run/docker.sock:/var/run/docker.sock \
	  -e TargetAccountId="${TARGET_ACCOUNT_ID}" \
	  -e EnvironmentNameUpper="${ENV_NAME_UPPER}" \
	  -e ResourceName="${RESOURCE_NAME}" \
	  -e BUILD_ID="${BUILD_ID}" \
	  "${cred_args[@]}" \
	  -v ${target_dir}/ext:/dist/ext \
	  -v ${target_dir}/shared:/dist/shared \
	  ${RESOURCE_NAME}:b${BUILD_ID} /dist/ext/deploy.sh
