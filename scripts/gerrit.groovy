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
   userRemoteConfigs: [[credentialsId: 'gerrit-ssh',
     refspec: '$GERRIT_REFSPEC',
     url: "${GIT_URL}"]]]
  )
}

def checkSubmitStatus(deployTarget) {
  if (deployTarget == "PROD" && env.GERRIT_CHANGE_ID) {
    withCredentials([sshUserPrivateKey(credentialsId: 'gerrit-ssh',
                                       keyFileVariable: 'SSHFILEPATH',
                                       passphraseVariable: 'SSHPASSPHRASE',
                                       usernameVariable: 'SSHUSERNAME')]) {
      String submitStatus = sh(label: 'Check submit status', returnStdout: true, script:
            """#!/bin/bash
            set -euo pipefail
            CHECK_GERRIT_BUILD="${env.GERRIT_CHANGE_SUBJECT}"
            echo "INFO: Checking submit status for change ${GERRIT_CHANGE_ID}, patchset ${GERRIT_PATCHSET_REVISION}"
            echo "INFO: GERRIT_CHANGE_SUBJECT=\${CHECK_GERRIT_BUILD}"
            if [[ "\${CHECK_GERRIT_BUILD}" == null ]] ; then
                echo "INFO: skipping Gerrit submit test (GERRIT_CHANGE_SUBJECT is null)"
                exit 0
            fi

            ACTIONS_URL="https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/${GERRIT_PATCHSET_REVISION}/actions"
            echo "INFO: Fetching actions from: \${ACTIONS_URL}"

            RAW_RESPONSE=\$(curl -b ~/.gitcookie --fail -s "\${ACTIONS_URL}") || {
                echo "ERROR: curl request to Gerrit actions API failed with exit code \$?"
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
                curl -b ~/.gitcookie --fail -s "\${LOCK_URL}" \
                         --data '{"message": "Ready for production","labels":{"Patch-Set-Lock": 1}}' > /dev/null
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
  withCredentials([sshUserPrivateKey(credentialsId: 'gerrit-ssh', keyFileVariable: 'SSHFILEPATH', passphraseVariable: 'SSHPASSPHRASE', usernameVariable: 'SSHUSERNAME')]) {
    if (env.GERRIT_CHANGE_ID) {
      sh(label: "Submit change", script: """#!/bin/bash
        CHECK_GERRIT_BUILD="${env.GERRIT_CHANGE_SUBJECT}"
        if [[ "\${CHECK_GERRIT_BUILD}" == null ]] ; then
          echo "INFO: skipping Gerrit submit"
          exit 0
        fi
        curl -b ~/.gitcookie --fail https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/current/review \
             --data '{"message": "Looking good","labels":{"Verified": 1}}'
             
        curl -b ~/.gitcookie --fail https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/submit \
             --data '{}'
      """)
    }
  }
}

def unlockPatchSet () {
  withCredentials([sshUserPrivateKey(credentialsId: 'gerrit-ssh', keyFileVariable: 'SSHFILEPATH', passphraseVariable: 'SSHPASSPHRASE', usernameVariable: 'SSHUSERNAME')]) {
    sh(label: "Unlock patchset", script: """#!/bin/bash
      CHECK_GERRIT_BUILD="${env.GERRIT_CHANGE_SUBJECT}"
      if [[ "\${CHECK_GERRIT_BUILD}" == null ]] ; then
        echo "INFO: skipping Gerrit unlockPatchSet"
        exit 0
      fi
      curl -b ~/.gitcookie --fail https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/current/review \
             --data '{"message": "Unlocking","labels":{"Patch-Set-Lock": 0}}'

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
