#!/bin/bash

set -e 

echo "Going to deploy me some stuff"

export RESOURCE_NAME=$1
export ENV_NAME_UPPER=$2

target_dir=${PWD}/target
mkdir -p ${target_dir}
curl -H 'Cache-Control: no-cache' \
	https://raw.githubusercontent.com/raiffeisenbankinternational/cbd-jenkins-pipeline/master/ext/deploy.sh > $target_dir/deploy.sh

chmod +x $target_dir/deploy.sh
chmod 755 $target_dir/deploy.sh

export TARGET_ACCOUNT_ID="$(aws sts get-caller-identity | jq -r '.Account')"

docker run -v /var/run/docker.sock:/var/run/docker.sock \
	  -e TargetAccountId="${TARGET_ACCOUNT_ID}" \
	  -e EnvironmentNameUpper="${ENV_NAME_UPPER}" \
	  -e ResourceName="${RESOURCE_NAME}" \
	  -e BUILD_ID="${BUILD_ID}" \
	  -v $target_dir/deploy.sh:/dist/deploy.sh \
	  ${RESOURCE_NAME}:b${BUILD_ID} /dist/deploy.sh
