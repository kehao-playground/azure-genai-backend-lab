from behave import given, then, when
from tests.bdd.steps.rag_steps import _override_rag_service


def _headers(
    tenant: str, groups: tuple[str, ...] = (), user_id: str = "u1"
) -> dict[str, str]:
    headers = {"X-Tenant-Id": tenant, "X-User-Id": user_id}
    if groups:
        headers["X-Group-Ids"] = ",".join(groups)
    return headers


def _ask(context, tenant: str, groups: tuple[str, ...] = ()) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/rag",
        json={"question": context.question},
        headers=_headers(tenant, groups),
    )


@given('tenant "{tenant}" has an indexed document about "{topic}"')
def step_tenant_has_indexed_document(context, tenant: str, topic: str) -> None:  # type: ignore[no-untyped-def]
    context.question = f"What is our {topic}?"
    documents = [
        {
            "chunk_id": "chunk-1",
            "parent_id": "doc-1",
            "title": topic.title(),
            "heading_path": topic.title(),
            "content": f"Our {topic} requires manager approval for reimbursement.",
            "tenant_id": tenant,
            "allowed_groups": [],
        }
    ]
    _override_rag_service(documents, context)


@when('tenant "{tenant}" asks the same question')
def step_other_tenant_asks_same_question(context, tenant: str) -> None:  # type: ignore[no-untyped-def]
    _ask(context, tenant)


@given('tenant "{tenant}" has a document restricted to group "{group}"')
def step_tenant_has_group_restricted_document(context, tenant: str, group: str) -> None:  # type: ignore[no-untyped-def]
    context.question = "What is the oncall escalation procedure?"
    documents = [
        {
            "chunk_id": "chunk-1",
            "parent_id": "doc-1",
            "title": "Oncall Escalation",
            "heading_path": "Oncall Escalation",
            "content": "The oncall escalation procedure pages the on-duty engineer.",
            "tenant_id": tenant,
            "allowed_groups": [group],
        }
    ]
    context.doc_tenant = tenant
    _override_rag_service(documents, context)


@given('tenant "{tenant}" has a document with an empty ACL')
def step_tenant_has_empty_acl_document(context, tenant: str) -> None:  # type: ignore[no-untyped-def]
    context.question = "What is the vacation policy?"
    documents = [
        {
            "chunk_id": "chunk-1",
            "parent_id": "doc-1",
            "title": "Vacation Policy",
            "heading_path": "Vacation Policy",
            "content": "The vacation policy allows unlimited paid time off.",
            "tenant_id": tenant,
            "allowed_groups": [],
        }
    ]
    context.doc_tenant = tenant
    _override_rag_service(documents, context)


@when('a "{tenant}" user without groups asks about it')
def step_user_without_groups_asks(context, tenant: str) -> None:  # type: ignore[no-untyped-def]
    _ask(context, tenant)


@when('a "{tenant}" user in group "{group}" asks about it')
def step_user_in_group_asks(context, tenant: str, group: str) -> None:  # type: ignore[no-untyped-def]
    _ask(context, tenant, (group,))


@then('the rag response status is "{status}"')
def step_rag_response_status(context, status: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response.status_code == 200
    assert context.response.json()["status"] == status


@given('tenant "{tenant}" has a conversation with one exchange')
def step_tenant_has_conversation(context, tenant: str) -> None:  # type: ignore[no-untyped-def]
    context.conversation_tenant = tenant
    response = context.client.post(
        "/api/v1/chat", json={"message": "Hello"}, headers=_headers(tenant)
    )
    assert response.status_code == 200
    context.conversation_id = response.json()["conversation_id"]


@when('tenant "{tenant}" requests that conversation id')
def step_other_tenant_requests_conversation(context, tenant: str) -> None:  # type: ignore[no-untyped-def]
    context.response = context.client.post(
        "/api/v1/chat",
        json={"message": "intruder", "conversation_id": context.conversation_id},
        headers=_headers(tenant),
    )


@then('the response is 404 with error code "{code}"')
def step_response_is_404_with_code(context, code: str) -> None:  # type: ignore[no-untyped-def]
    assert context.response.status_code == 404
    assert context.response.json()["error"]["code"] == code


@then("tenant \"{tenant}\" can continue the conversation successfully")
def step_tenant_continues_conversation(context, tenant: str) -> None:  # type: ignore[no-untyped-def]
    response = context.client.post(
        "/api/v1/chat",
        json={"message": "still me", "conversation_id": context.conversation_id},
        headers=_headers(tenant),
    )
    assert response.status_code == 200
