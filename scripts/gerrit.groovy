@groovy.transform.Field String gerritPostFn = '''
gerrit_post() {
    local url="$1" body="$2" output http_status
    output=$(mktemp)
    echo "INFO: POST as ${GERRIT_USER} to: ${url}" >&2
    http_status=$(curl -u "${GERRIT_USER}:${GERRIT_PW}" -sS -w "%{http_code}" -H "Content-Type: application/json" --data "${body}" "${url}" -o "${output}")
    if [ "${http_status}" -ge 400 ]; then
        echo "ERROR: Gerrit returned HTTP ${http_status} for ${url}" >&2
        echo "ERROR: Response: $(head -c 500 "${output}")" >&2
        rm -f "${output}"
        return 22
    fi
    echo "INFO: Gerrit returned HTTP ${http_status}" >&2
    cat "${output}"
    rm -f "${output}"
}
'''

def doCheckout() {
  checkout([$class: 'GitSCM',
   branches: [[name: 'master']],
   doGenerateSubmoduleConfigurations: false,
   extensions: [
    [
    $class: 'BuildChooserSetting',
       buildChooser: [$class: 'GerritTriggerBuildChooser']
    ],
    [
        $class: 'SubmoduleOption',
        disableSubmodules: false,
        parentCredentials: true,
        recursiveSubmodules: true,
        reference: '',
        trackingSubmodules: false
      ]],
   submoduleCfg: [],
   userRemoteConfigs: [[credentialsId: 'gerrit-http',
     refspec: '$GERRIT_REFSPEC',
     url: "${GIT_URL}"]]]
  )
}

def checkSubmitStatus(deployTarget) {
  if (deployTarget == "PROD" && env.GERRIT_CHANGE_ID) {
    withCredentials([usernamePassword(credentialsId: 'gerrit-http',
                                      passwordVariable: 'GERRIT_PW',
                                      usernameVariable: 'GERRIT_USER')]) {
      String submitStatus = sh(label: 'Check submit status', returnStdout: true, script:
            """#!/bin/bash
            set -euo pipefail
            ${gerritPostFn}
            CHECK_GERRIT_BUILD="${env.GERRIT_CHANGE_SUBJECT}"
            echo "INFO: Checking submit status for change ${GERRIT_CHANGE_ID}, patchset ${GERRIT_PATCHSET_REVISION}"
            echo "INFO: GERRIT_CHANGE_SUBJECT=\${CHECK_GERRIT_BUILD}"
            if [[ "\${CHECK_GERRIT_BUILD}" == null ]] ; then
                echo "INFO: skipping Gerrit submit test (GERRIT_CHANGE_SUBJECT is null)"
                exit 0
            fi

            ACTIONS_URL="https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/${GERRIT_PATCHSET_REVISION}/actions"
            echo "INFO: Fetching actions from: \${ACTIONS_URL}"

            RAW_RESPONSE=\$(curl -u "\${GERRIT_USER}:\${GERRIT_PW}" --fail -sS "\${ACTIONS_URL}") || {
                echo "ERROR: curl request to Gerrit actions API as \${GERRIT_USER} failed with exit code \$?"
                exit 0
            }

            echo "INFO: Raw API response (first 500 chars): \${RAW_RESPONSE:0:500}"

            JSON_RESPONSE=\$(echo "\${RAW_RESPONSE}" | tail -n +2) || {
                echo "ERROR: Failed to strip Gerrit magic prefix from response"
                exit 0
            }

            echo "INFO: JSON response (first 500 chars): \${JSON_RESPONSE:0:500}"

            SUBMITTABLE=\$(echo "\${JSON_RESPONSE}" | jq -r '.submit.label // "null"') || {
                echo "ERROR: jq parsing failed with exit code \$?"
                exit 0
            }

            echo "INFO: submit.label value = '\${SUBMITTABLE}'"

            if [ "\${SUBMITTABLE}" != "Submit" ] ; then
                echo "INFO: Checking why change is not submittable..."
                SUBMIT_ACTION=\$(echo "\${JSON_RESPONSE}" | jq '.submit // "not present"') || true
                echo "INFO: Full submit action object: \${SUBMIT_ACTION}"
                echo "ERROR: Change is not ready to submit! (submit.label='\${SUBMITTABLE}')"
                exit 0
            else
                echo "INFO: Ready to submit, adding Patch-Set-Lock"
                LOCK_URL="https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/current/review"
                echo "INFO: Posting Patch-Set-Lock to: \${LOCK_URL}"
                gerrit_post "\${LOCK_URL}" '{"message": "Ready for production","labels":{"Patch-Set-Lock": 1}}' > /dev/null
                echo "INFO: Patch-Set-Lock applied successfully"
            fi
            """).trim()

      echo "Full submit status output:"
      echo "${submitStatus}"
      // Extract just the last meaningful line for decision-making
      String lastLine = submitStatus.split('\n').last().trim()
      echo "Decision based on last line: ${lastLine}"
      if (lastLine.startsWith("INFO: Ready") || lastLine.startsWith("INFO: Patch-Set-Lock applied") || lastLine.startsWith("INFO: skipping")) {
        sh(label: 'Ready to submit', script: "echo '${lastLine}' && exit 0")
      } else {
        echo "All status lines:"
        submitStatus.split('\n').eachWithIndex { line, idx ->
          echo "  [${idx}] ${line}"
        }
        sh(label: 'Change is not ready to submit!', script: "echo -e '\\e[31m${lastLine}\\e[0m' && exit 1")
      }
    }
  }
  else {
    print "DEV deployment"
  }
}

def submitChange() {
  withCredentials([usernamePassword(credentialsId: 'gerrit-http', passwordVariable: 'GERRIT_PW', usernameVariable: 'GERRIT_USER')]) {
    if (env.GERRIT_CHANGE_ID) {
      sh(label: "Submit change", script: """#!/bin/bash
        set -eo pipefail
        ${gerritPostFn}
        CHECK_GERRIT_BUILD="${env.GERRIT_CHANGE_SUBJECT}"
        if [[ "\${CHECK_GERRIT_BUILD}" == null ]] ; then
          echo "INFO: skipping Gerrit submit"
          exit 0
        fi
        gerrit_post "https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/current/review" \
             '{"message": "Looking good","labels":{"Verified": 1}}'
        gerrit_post "https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/submit" '{}'
      """)
    }
  }
}

def unlockPatchSet () {
  withCredentials([usernamePassword(credentialsId: 'gerrit-http', passwordVariable: 'GERRIT_PW', usernameVariable: 'GERRIT_USER')]) {
    sh(label: "Unlock patchset", script: """#!/bin/bash
      set -eo pipefail
      ${gerritPostFn}
      CHECK_GERRIT_BUILD="${env.GERRIT_CHANGE_SUBJECT}"
      if [[ "\${CHECK_GERRIT_BUILD}" == null ]] ; then
        echo "INFO: skipping Gerrit unlockPatchSet"
        exit 0
      fi
      gerrit_post "https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/current/review" \
             '{"message": "Unlocking","labels":{"Patch-Set-Lock": 0}}'

    """)
  }
}


def maybePushToPublic() {
  if (env.PUBLIC_PUSH && env.PUBLIC_PUSH == "true") {
   withCredentials([usernamePassword(credentialsId: "github-http", usernameVariable: 'GIT_USERNAME', passwordVariable: 'GIT_PASSWORD')]) {
     sh(label: "Push to public", script: """#!/bin/bash
         set -e
         echo "Pushing to public \$last_commit_id"
         last_commit_id=\$(git log --format="%H" -n 1)
         git config credential.helper '!f() { sleep 1; echo "username=${GIT_USERNAME}"; echo "password=${GIT_PASSWORD}"; }; f'
         git push --force ${env.PUBLIC_URL} \$last_commit_id:refs/heads/master
     """)
     }
  }
}

def init() {
  sh 'id'
  sh 'ls -la'
  sh 'mkdir -p ansible'
  sh 'mkdir -p test'
  sh 'mkdir -p resources'
  sh 'mkdir -p schema'
  sh 'mkdir -p api'
  sh 'mkdir -p lib'
  sh 'mkdir -p ext'
  sh 'mkdir -p www'
  sh 'mkdir -p cert'
}

return this
