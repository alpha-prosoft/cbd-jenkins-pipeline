def getJiraIssue() {
    echo "Getting jira issue from commit message"
    
    // Check if GERRIT_CHANGE_SUBJECT is set
    if (!env.GERRIT_CHANGE_SUBJECT) {
        echo "INFO: GERRIT_CHANGE_SUBJECT not set, skipping JIRA extraction"
        return null
    }
    
    try {
        def response = sh(label: "Extract jira issue", returnStdout: true, script: """#!/bin/bash 
            JIRA_PATTERN="^\\[[a-zA-Z0-9,\\.\\_\\-]+-[0-9]+\\]"
            JIRA_ISSUE="\$(echo "${env.GERRIT_CHANGE_SUBJECT}" | grep -o -E "\${JIRA_PATTERN}" | sed 's/^\\[\\(.*\\)\\]\$/\\1/')"
            if [ -z "\${JIRA_ISSUE}" ] ; then
                        echo "ERROR: Pattern does not match. Please use [JIRA-123] syntax in commit messages"
              exit 1
            fi
            echo "\${JIRA_ISSUE}"
      """)
        return response.trim();
    } catch (Exception e) {
        error "Error extracing JIRA: ${e.message}"
    }
}

/**
 * Finds the appropriate URL from a formatted string based on a ticket prefix.
 *
 * @param urls A comma-separated string of URLs, each prefixed with an identifier and a colon.
 * Example: "AP:https://bla1.com,AL:https://bla2.com,QA:http://test.env"
 * @param ticket A ticket string containing a prefix followed by a hyphen.
 * Example: "AP-21", "AL-2133", "QA-100"
 * @return The URL corresponding to the ticket's prefix, or null if not found or inputs are invalid.
 */
def findUrlForTicket(String urls, String ticket) {
    // --- Input Validation ---
    if (urls == null || urls.trim().isEmpty() || ticket == null || ticket.trim().isEmpty()) {
        println "Error: URLs string and ticket string cannot be null or empty."
        return null // Return null for invalid input
    }

    // --- Extract Ticket Prefix ---
    def ticketParts = ticket.split('-', 2) // Split only on the first hyphen
    if (ticketParts.length < 2 || ticketParts[0].trim().isEmpty()) {
        println "Error: Ticket format is invalid. Expected format like 'PREFIX-NUMBER' (e.g., 'AP-123'). Ticket received: ${ticket}"
        return null // Return null if ticket format is wrong
    }
    def ticketPrefix = ticketParts[0].trim() // Get the prefix (e.g., "AP")

    // --- Parse URLs into a Map ---
    def urlMap = [:] // Create an empty map to store Prefix -> URL
    try {
        urls.split(',').each { pair ->
            def parts = pair.split(':', 2) // Split only on the first colon
            if (parts.length == 2) {
                def prefix = parts[0].trim() // Get the prefix (e.g., "AP")
                def urlValue = parts[1].trim() // Get the URL (e.g., "https://bla1.com")
                if (!prefix.isEmpty() && !urlValue.isEmpty()) {
                    urlMap[prefix] = urlValue // Add to the map
                } else {
                    println "Warning: Skipping invalid pair in URLs string: '${pair}'"
                }
            } else {
                println "Warning: Skipping malformed pair in URLs string: '${pair}'"
            }
        }
    } catch (Exception e) {
        error "Error parsing URLs string: ${urls}, with erro: ${e.message}"
    }


    if (urlMap.containsKey(ticketPrefix)) {
        return urlMap[ticketPrefix] // Return the URL found in the map
    } else {
        throw new Exception("URL was not found for ${ticketPrefix}")
    }
}

def checkJira() {
    withCredentials([usernamePassword(credentialsId: 'jira-http', passwordVariable: 'JIRA_PW', usernameVariable: 'JIRA_USER'),
                     sshUserPrivateKey(credentialsId: 'gerrit-ssh', keyFileVariable: 'SSHFILEPATH', passphraseVariable: 'SSHPASSPHRASE', usernameVariable: 'SSHUSERNAME')]) {
        echo "INFO: Check jira ticket status"
        if (!env.GERRIT_CHANGE_SUBJECT) {
            echo "INFO: skipping JIRA check - not a Gerrit build"
            return true;
        }

        String gerritMessage = sh(label: 'Check gerrit commit message', returnStdout: true, script: """#!/bin/bash
           output=\$(mktemp)
           target_url="https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/${GERRIT_PATCHSET_REVISION}/commit" 
           curl -b ~/.gitcookie --fail -v \$target_url -o \$output       
           tail -n +2 \$output | jq -r ".message"
           """)
        echo "Commit message ${gerritMessage}"

        def jiraIssue = getJiraIssue()
        echo "Checking JIRA: ${jiraIssue}"
        def jiraUrl = findUrlForTicket("${env.GLOBAL_JIRA_URL}", "${jiraIssue}")
        echo "Jira url: ${jiraUrl}"

        try {
            def jiraStatus = sh(label: 'Check related Jira issue', returnStdout: true, script: """#!/bin/bash
                set -eux pipefail
    
                update_issue() {
                  JIRA_STATUS="\${1}"
                  ISSUE_SUM="\$(echo "\${JIRA_STATUS}" | jq -r ".fields.summary")"
                  ISSUE_DESC="\$(echo "\${JIRA_STATUS}" | jq -r ".fields.description")"
                  ISSUE_TYPE="\$(echo "\${JIRA_STATUS}" | jq -r ".fields.issuetype.name")"
                  GERRIT_MESSAGE="${gerritMessage}"
    
                  GERRIT_SUM_STEP1="\$(echo "\${GERRIT_MESSAGE/"[${jiraIssue}] "/}" | head -n 1)"
                  GERRIT_SUM_STEP2="\${GERRIT_SUM_STEP1//\\'/\\\\u0027}"
                  GERRIT_SUM="\${GERRIT_SUM_STEP2//\\"/\\\\\\"}"
                  GERRIT_DESC_STEP1="\$(echo "\${GERRIT_MESSAGE}" | tail -n +2)"
                  GERRIT_DESC_STEP2="\$(printf '%q' "\$GERRIT_DESC_STEP1")"
                  GERRIT_DESC_STEP3="\${GERRIT_DESC_STEP2//\\"/\\\\\\"}"
                  GERRIT_DESC_STEP4="\${GERRIT_DESC_STEP3#\\\$\\\'}"
                  GERRIT_DESC_STEP5="\${GERRIT_DESC_STEP4%?}"
                  GERRIT_DESC="\${GERRIT_DESC_STEP5//\\\\\\'/\\\\u0027}"
                  CONT_TYPE="Content-Type:application/json"
    
                  if [ "\${ISSUE_TYPE^^}" == "BUG" ] || [ "\${ISSUE_TYPE^^}" ==  "STORY" ] ; then
                    echo "INFO: Issue is a story or bug, skipping"
                    exit 1
                  elif [ "\${ISSUE_SUM}" !=  "\${GERRIT_SUM_STEP1}" ] && [ "\${ISSUE_DESC}" !=  "\${GERRIT_DESC_STEP1}" ] ; then
                    echo "INFO: updating JIRA issue summary and description based on commit message."
                    API_DATA='{ "fields": {"summary": "'"\${GERRIT_SUM}"'", "description": "'"\${GERRIT_DESC}"'" } }'
                  elif [ "\${ISSUE_SUM}" !=  "\${GERRIT_SUM_STEP1}" ] ; then
                    API_DATA='{ "fields": {"summary": "'"\${GERRIT_SUM}"'" } }'
                    echo "INFO: updating JIRA issue summary based on commit message."
                  elif [ "\${ISSUE_DESC}" !=  "\${GERRIT_DESC_STEP1}" ] ; then
                    API_DATA='{ "fields": {"description": "'"\${GERRIT_DESC}"'" } }'
                    echo "INFO: updating JIRA issue description based on commit message"
                  else
                    echo "INFO: Issue and commit message are the same, nothing to update"
                    exit 0
                  fi
                  echo "\${API_DATA}"
                  url="${jiraUrl}/rest/api/2/issue/${jiraIssue}"
                  echo "Updating on URL: \${url}"
                  curl -k -f -D- -u  "${JIRA_USER}:${JIRA_PW}" -X PUT --data "\${API_DATA}" -H "\${CONT_TYPE}" \${url}
                }
    
                CHECK_GERRIT_BUILD="${env.GERRIT_CHANGE_SUBJECT}"
                if [[ "\${CHECK_GERRIT_BUILD}" == null ]] ; then
                  echo "INFO: skipping JIRA test"
                  exit 0
                fi
                
                url="${jiraUrl}/rest/api/2/issue/${jiraIssue}"
                echo "Getting on URL: \${url}"

                JIRA_STATUS="\$(curl -k -s -u ${JIRA_USER}:${JIRA_PW} \${url})"
    
                if get_status="\$(echo "\${JIRA_STATUS}" | jq -er ".fields.status.name")"; then
                    if [ "\${get_status}" != "In Progress" ] ; then
                      echo "ERROR: Related Jira Issue (\${JIRA_STATUS}) has to be In Progress, current status is \${get_status}, \${url}"
                      exit 0
                    else
                      echo "INFO: Related Jira Issue (\${JIRA_STATUS}) status: \${get_status}"url
                      update_issue "\${JIRA_STATUS}"
                      exit 0
                    fi
                elif get_error="\$(echo "\${JIRA_STATUS}" | jq -er ".errorMessages[]")"; then
                  echo "ERROR: Commit message error related to \${JIRA_STATUS}: \$get_error, \${url}"
                  exit 0
                else
                  echo "ERROR: Unknown error during Jira issue , \${url}"
                echo "Jira check is done!"
                exit 0
                fi
                """).trim()

            if (jiraStatus.contains("ERROR: Pattern")) {
                sh(label: 'Jira ticket pattern does not match [JIRA-123]', script: "echo -e '\\e[31m${jiraStatus}\\e[0m' && exit 1")
            } else if (jiraStatus.contains("ERROR: Related")) {
                sh script: "echo -e '\\e[31m${jiraStatus}\\e[0m' && exit 1", label: 'Jira ticket not In-Progress state'
            } else if (jiraStatus.contains("INFO: Related Jira Issue")) {
                sh(label: 'Jira ticket OK', script: "echo '${jiraStatus}' && exit 0")
            } else {
                sh(label: 'Unknown jira error', script: "echo -e '\\e[31m${jiraStatus}\\e[0m' && exit 1")
            }
        } catch (err) {
            println "Jira check failed!"
            println "Error details: ${err.getMessage()}"
            error("Jira Check script failed. See console for details.")
        }
    }

}

def close() {
    if (!env.GERRIT_CHANGE_SUBJECT) {
        echo "INFO: Skipping JIRA close - not a Gerrit build"
        return
    }
    
    withCredentials([usernamePassword(credentialsId: 'jira-http', passwordVariable: 'JIRA_PW', usernameVariable: 'JIRA_USER')]) {
        def jiraIssue = getJiraIssue()
        echo "Checking JIRA: ${jiraIssue}"

        def jiraUrl = findUrlForTicket("${env.GLOBAL_JIRA_URL}", "${jiraIssue}")
        echo "Jira url: ${jiraUrl}"

        sh(label: 'Close related Jira issue', script: """#!/bin/bash
            set -xeu pipefail
            CHECK_GERRIT_BUILD="${env.GERRIT_CHANGE_SUBJECT}"
            if [[ "\${CHECK_GERRIT_BUILD}" == null ]] ; then
              echo "INFO: skipping JIRA closing"
              exit 0
            fi
            DONE_WORKFLOW_ID="\$(curl -k -s -u ${JIRA_USER}:${JIRA_PW} ${jiraUrl}/rest/api/2/issue/${jiraIssue}/transitions?transitionId | \
            jq -re '.transitions[] | select(.name=="Done") | .id')"
            curl -k -D- -u ${JIRA_USER}:${JIRA_PW} \
                 -X POST \
                 --data '{ "transition": { "id": '"\${DONE_WORKFLOW_ID}"' } }' \
                  -H "Content-Type: application/json" \
                  ${jiraUrl}/rest/api/2/issue/${jiraIssue}/transitions?transitionId
          """)
    }
}

return this

