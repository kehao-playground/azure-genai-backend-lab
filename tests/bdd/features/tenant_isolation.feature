Feature: Tenant isolation
  Scenario: A tenant cannot retrieve another tenant's document
    Given tenant "tenant-a" has an indexed document about "expense policy"
    When tenant "tenant-b" asks the same question
    Then the rag response status is "no_answer"

  Scenario: Group ACL restricts documents within a tenant
    Given tenant "tenant-a" has a document restricted to group "oncall"
    When a "tenant-a" user without groups asks about it
    Then the rag response status is "no_answer"
    When a "tenant-a" user in group "billing" asks about it
    Then the rag response status is "no_answer"
    When a "tenant-a" user in group "oncall" asks about it
    Then the rag response status is "answered"

  Scenario: Unrestricted documents are tenant-wide readable
    Given tenant "tenant-a" has a document with an empty ACL
    When a "tenant-a" user without groups asks about it
    Then the rag response status is "answered"

  Scenario: A tenant cannot continue another tenant's conversation
    Given tenant "tenant-a" has a conversation with one exchange
    When tenant "tenant-b" requests that conversation id
    Then the response is 404 with error code "conversation_not_found"
    And tenant "tenant-a" can continue the conversation successfully
