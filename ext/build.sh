#!/bin/bash

set -eou pipefail

export RESOURCE_NAME=$1
export ENV_NAME_UPPER=$2

SESSION_TOKEN=$(curl -s -m 2 \
	          -X PUT "http://169.254.169.254/latest/api/token" \
		  -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" || true)

if [[ -n "${SESSION_TOKEN}" ]]; then
  echo "Running on EC2 - using instance metadata credentials"
  ROLE_NAME=$(curl -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" \
	           http://169.254.169.254/latest/meta-data/iam/security-credentials/)

  CREDENTIALS=$(curl -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" \
	       http://169.254.169.254/latest/meta-data/iam/security-credentials/$ROLE_NAME)

  export AWS_DEFAULT_REGION=$(curl -s -H "X-aws-ec2-metadata-token: $SESSION_TOKEN" \
             http://169.254.169.254/latest/dynamic/instance-identity/document \
	         | jq -r .region)

  export AWS_ACCESS_KEY_ID=$(echo $CREDENTIALS | jq -r '.AccessKeyId')
  export AWS_SECRET_ACCESS_KEY=$(echo $CREDENTIALS | jq -r '.SecretAccessKey')
  export AWS_SESSION_TOKEN=$(echo $CREDENTIALS | jq -r '.Token')
else
  echo "Not on EC2 - using ambient AWS credentials (aws login)"
  export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-$(aws configure get region)}"
fi

export TARGET_ACCOUNT_ID="$(aws sts get-caller-identity | jq -r '.Account')"

export DOCKER_BUILDKIT=1

export LATEST_IMAGE="$(aws ec2 describe-images \
                          --owners self --no-paginate  \
			  | jq -r '.Images[].Name' \
			  | grep build-${RESOURCE_NAME}  \
			  | sort | tail -1)"

echo "Last image found: $LATEST_IMAGE"

if [[ "$LATEST_IMAGE" == "" ]]; then
  export BUILD_ID="1000"
else
  export BUILD_ID="${LATEST_IMAGE##*.b}"
fi
export BUILD_ID=$((BUILD_ID+1))
echo "New build id: $BUILD_ID"

mkdir -p cert
touch cert/empty
if [[ -d /etc/pki/ca-trust/source/anchors ]]; then
  find /etc/pki/ca-trust/source/anchors -maxdepth 1 -type f -exec cp {} cert/ \;
fi

# When not on EC2 the in-container build has no instance metadata to authenticate
# with, so hand it the current credentials through the build context. On EC2 this
# file is absent and the container uses instance metadata.
creds_file="ansible/build/aws-creds.json"
rm -f "${creds_file}"
# Always remove the exported credentials, even if the build fails or is interrupted.
trap 'rm -f "${creds_file}"' EXIT
if [[ -z "${SESSION_TOKEN}" ]]; then
  # Off EC2 the in-container build has no instance metadata to authenticate with.
  # Local SSO credentials can be very short-lived, so assume a role to obtain a
  # full-length (up to 1h via role chaining) session for the build, and hand
  # those to the container. On EC2 this whole block is skipped (metadata is used).
  assume_role_name="${BUILD_ASSUME_ROLE:-DeliveryRole}"
  echo "Not on EC2 - assuming ${assume_role_name} for a longer-lived build session"
  aws sts assume-role \
    --role-arn "arn:aws:iam::${TARGET_ACCOUNT_ID}:role/${assume_role_name}" \
    --role-session-name "local-build-${RESOURCE_NAME}-${BUILD_ID}" \
    --duration-seconds 3600 \
    --query 'Credentials' --output json \
    | jq '{AWS_ACCESS_KEY_ID: .AccessKeyId,
           AWS_SECRET_ACCESS_KEY: .SecretAccessKey,
           AWS_SESSION_TOKEN: .SessionToken,
           AWS_DEFAULT_REGION: "'"${AWS_DEFAULT_REGION}"'"}' \
    > "${creds_file}"
fi

arg_http_proxy="--build-arg http_proxy=${http_proxy:-}"
arg_https_proxy="--build-arg https_proxy=${https_proxy:-}"
arg_no_proxy="--build-arg no_proxy=${no_proxy:-}"
arg_HTTP_PROXY="--build-arg HTTP_PROXY=${HTTP_PROXY:-}"
arg_HTTPS_PROXY="--build-arg HTTPS_PROXY=${HTTPS_PROXY:-}"
arg_NO_PROXY="--build-arg NO_PROXY=${NO_PROXY:-}"

docker build --progress=plain \
             --network=host \
             --no-cache \
             --pull \
	     --build-arg BUILD_ID="${BUILD_ID}" \
	     --build-arg BuildId="${BUILD_ID}" \
	     --build-arg AWS_REGION="${AWS_DEFAULT_REGION}" \
      	     --build-arg AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION}" \
	     --build-arg DOCKER_REGISTRY_URL="${DOCKER_REGISTRY_URL:-docker.io}" \
             --build-arg RESOURCE_NAME="${RESOURCE_NAME}" \
             ${arg_http_proxy} \
	     ${arg_https_proxy} \
	     ${arg_no_proxy} \
	     ${arg_HTTP_PROXY} \
      	     ${arg_HTTPS_PROXY} \
	     ${arg_NO_PROXY} \
	     -t ${RESOURCE_NAME}:b${BUILD_ID} \
	     -f Dockerfile .

