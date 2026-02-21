def getJiraIssue() {
    echo "Getting jira issue from commit message"

    // Check if GERRIT_CHANGE_SUBJECT is set
    if (!env.GERRIT_CHANGE_SUBJECT) {
        echo "INFO: GERRIT_CHANGE_SUBJECT not set, skipping JIRA extraction"
        return null
    }

    def matcher = (env.GERRIT_CHANGE_SUBJECT =~ /^\[([a-zA-Z0-9,._-]+-[0-9]+)\]/)
    if (matcher.find()) {
        def issue = matcher.group(1)
        echo "INFO: Extracted JIRA issue: ${issue}"
        return issue
    } else {
        error "ERROR: Pattern does not match. Please use [JIRA-123] syntax in commit messages"
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

        // Fetch commit message from Gerrit
        String gerritMessage = sh(label: 'Fetch Gerrit commit message', returnStdout: true, script: """#!/bin/bash
           set -eu
           output=\$(mktemp)
           target_url="https://${GERRIT_URL}/a/changes/${GERRIT_CHANGE_ID}/revisions/${GERRIT_PATCHSET_REVISION}/commit"
           curl -b ~/.gitcookie --fail -s \$target_url -o \$output
           tail -n +2 \$output | jq -r ".message"
           rm -f \$output
           """).trim()
        echo "INFO: Commit message: ${gerritMessage}"

        def jiraIssue = getJiraIssue()
        echo "INFO: Checking JIRA: ${jiraIssue}"
        def jiraUrl = findUrlForTicket("${env.GLOBAL_JIRA_URL}", "${jiraIssue}")
        echo "INFO: Jira URL: ${jiraUrl}"

        // Write commit message to a temp file to avoid shell escaping issues
        writeFile file: '.gerrit_commit_msg.tmp', text: gerritMessage

        try {
            sh(label: 'Check and sync JIRA issue', script: """#!/bin/bash
                set -eu
                python3 shared/jira_sync.py check \
                    --jira-url '${jiraUrl}' \
                    --jira-user '${JIRA_USER}' \
                    --jira-password '${JIRA_PW}' \
                    --jira-issue '${jiraIssue}' \
                    --gerrit-message "\$(cat .gerrit_commit_msg.tmp)"
                rm -f .gerrit_commit_msg.tmp
                """)
        } catch (err) {
            sh(script: "rm -f .gerrit_commit_msg.tmp", returnStatus: true)
            println "Jira check failed!"
            println "Error details: ${err.getMessage()}"
            error("Jira Check failed. See console for details.")
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
        echo "INFO: Closing JIRA: ${jiraIssue}"

        def jiraUrl = findUrlForTicket("${env.GLOBAL_JIRA_URL}", "${jiraIssue}")
        echo "INFO: Jira URL: ${jiraUrl}"

        sh(label: 'Close JIRA issue', script: """#!/bin/bash
            set -eu
            python3 shared/jira_sync.py close \
                --jira-url '${jiraUrl}' \
                --jira-user '${JIRA_USER}' \
                --jira-password '${JIRA_PW}' \
                --jira-issue '${jiraIssue}'
          """)
    }
}

return this
