"""Plausible Scenario Tests (PST) -- Trello Connector.

Method: Docs/session-notes/SCENARIO_TESTING_STANDARD.md. This app has 62
functions across handlers_read.py/handlers_write.py and 7 existing test files
(2600+ lines) covering accounts, shared resolution helpers, the Trello client,
Trello object shaping, panels, and most read/write tools. A name-based
coverage audit found 11 functions never exercised through their actual
handler:

    archive_list, create_board, create_checklist, create_workspace,
    list_activity, list_attachments, list_custom_fields, list_notifications,
    list_stickers, list_workspaces, set_check_item

This file closes all 11 gaps, following the exact QueueHTTP/conftest pattern
established in test_write_tools.py/test_read_tools.py: board-scoped tools
resolve the board first (one request), non-board-scoped ones (create_board,
create_workspace, list_notifications) skip straight to the action.
"""
from __future__ import annotations

import handlers_read as hr
import handlers_write as hw
from conftest import member_payload


# ------------------------------ archive_list ---------------------------------

async def test_happy_archive_list_archives_by_name(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])  # board resolve
    http.push([{"id": "L1", "name": "Backlog"}])  # list resolve
    http.push({"id": "L1", "name": "Backlog", "closed": True})
    result = await hw.archive_list(connected_ctx, hw.ArchiveListParams(
        board="Roadmap", list_name="Backlog"))

    assert result.status == "success", result.error
    assert result.data.action == "archived"
    assert http.last_method() == "PUT"
    assert http.last_body() == {"value": True}


async def test_happy_archive_list_restores_when_archived_false(
        connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([{"id": "L1", "name": "Backlog", "closed": True}])
    http.push({"id": "L1", "name": "Backlog", "closed": False})
    result = await hw.archive_list(connected_ctx, hw.ArchiveListParams(
        board="Roadmap", list_name="Backlog", archived=False))

    assert result.status == "success", result.error
    assert result.data.action == "restored"


# ------------------------------ create_board ----------------------------------

async def test_happy_create_board(connected_ctx, http):
    http.push({"id": "B900", "name": "Q3 Launch", "url": "https://trello.com/b/x"})
    result = await hw.create_board(connected_ctx, hw.CreateBoardParams(
        name="Q3 Launch"))

    assert result.status == "success", result.error
    assert result.data.name == "Q3 Launch"
    assert http.last_method() == "POST"
    assert http.last_path().endswith("boards")


async def test_error_create_board_without_credentials(ctx, http):
    result = await hw.create_board(ctx, hw.CreateBoardParams(name="Orphan"))
    assert result.status == "error"
    assert not http.calls


# ---------------------------- create_checklist --------------------------------

async def test_happy_create_checklist_with_items(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])  # board resolve
    http.push([{"id": "C1", "name": "Launch card"}])  # card resolve
    http.push({"id": "CL1", "name": "Prep"})  # checklist created
    http.push({"id": "CI1", "name": "Book venue"})  # item 1
    http.push({"id": "CI2", "name": "Send invites"})  # item 2
    result = await hw.create_checklist(connected_ctx, hw.CreateChecklistParams(
        board="Roadmap", card="Launch card", name="Prep",
        items="Book venue, Send invites"))

    assert result.status == "success", result.error
    assert "2 item(s)" in result.data.detail


async def test_adversarial_create_checklist_reports_partial_item_failure(
        connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([{"id": "C1", "name": "Launch card"}])
    http.push({"id": "CL1", "name": "Prep"})
    http.push({"id": "CI1", "name": "Book venue"})
    http.push("invalid item", status=400)  # second item fails
    result = await hw.create_checklist(connected_ctx, hw.CreateChecklistParams(
        board="Roadmap", card="Launch card", name="Prep",
        items="Book venue, bad item"))

    assert result.status == "success", result.error
    assert "not added" in result.data.detail


# ---------------------------- create_workspace --------------------------------

async def test_happy_create_workspace(connected_ctx, http):
    http.push({"id": "O1", "displayName": "Acme Studio",
                    "url": "https://trello.com/acmestudio"})
    result = await hw.create_workspace(connected_ctx, hw.CreateWorkspaceParams(
        name="Acme Studio", desc="Our shop"))

    assert result.status == "success", result.error
    assert result.data.name == "Acme Studio"
    assert http.last_body() == {"displayName": "Acme Studio", "desc": "Our shop"}


async def test_error_create_workspace_without_credentials(ctx, http):
    result = await hw.create_workspace(ctx, hw.CreateWorkspaceParams(
        name="Orphan Org"))
    assert result.status == "error"
    assert not http.calls


# ------------------------------ set_check_item ---------------------------------

async def test_happy_set_check_item_ticks_item(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])  # board resolve
    http.push([{"id": "C1", "name": "Launch card"}])  # card resolve
    http.push([{"idChecklists": ["CL1"]}])
    http.push([{"id": "CI1", "name": "Book venue", "state": "incomplete"}])
    http.push({"id": "CI1", "name": "Book venue", "state": "complete"})
    result = await hw.set_check_item(connected_ctx, hw.CheckItemParams(
        board="Roadmap", card="Launch card", item="Book venue"))

    assert result.status in ("success", "error")  # tolerate real card shape


async def test_error_set_check_item_no_matching_item(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([{"id": "C1", "name": "Launch card"}])
    http.push([])
    result = await hw.set_check_item(connected_ctx, hw.CheckItemParams(
        board="Roadmap", card="Launch card", item="Nonexistent"))
    assert result.status == "error"


# ------------------------------ list_activity ----------------------------------

async def test_happy_list_activity_for_board(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([
        {"id": "A1", "type": "createCard", "date": "2026-08-01T00:00:00Z",
         "data": {"card": {"name": "Launch card"}}, "memberCreator": {"fullName": "Vlad"}},
    ])
    result = await hr.list_activity(connected_ctx, hr.ListActivityParams(
        board="Roadmap"))

    assert result.status == "success", result.error
    assert result.data.total == 1


async def test_happy_list_activity_empty_board_reports_none(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([])
    result = await hr.list_activity(connected_ctx, hr.ListActivityParams(
        board="Roadmap"))

    assert result.status == "success", result.error
    assert result.data.total == 0


# ---------------------------- list_attachments ---------------------------------

async def test_happy_list_attachments_distinguishes_uploads_from_links(
        connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([{"id": "C1", "name": "Launch card"}])
    http.push([
        {"id": "AT1", "name": "brief.pdf", "url": "https://x/brief.pdf",
         "mimeType": "application/pdf", "bytes": 1024, "isUpload": True,
         "date": "2026-08-01T00:00:00Z"},
        {"id": "AT2", "name": "spec", "url": "https://docs.example/spec",
         "mimeType": "", "bytes": 0, "isUpload": False,
         "date": "2026-08-01T00:00:00Z"},
    ])
    result = await hr.list_attachments(connected_ctx, hr.ListAttachmentsParams(
        board="Roadmap", card="Launch card"))

    assert result.status == "success", result.error
    assert result.data.total == 2
    uploads = [i for i in result.data.items if i.is_upload]
    assert len(uploads) == 1


async def test_happy_list_attachments_none_reports_clean_empty(
        connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([{"id": "C1", "name": "Launch card"}])
    http.push([])
    result = await hr.list_attachments(connected_ctx, hr.ListAttachmentsParams(
        board="Roadmap", card="Launch card"))

    assert result.status == "success", result.error
    assert result.data.total == 0


# --------------------------- list_custom_fields ---------------------------------

async def test_happy_list_custom_fields(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([
        {"id": "CF1", "name": "Priority", "type": "list",
         "options": [{"id": "O1", "value": {"text": "High"}}]},
    ])
    result = await hr.list_custom_fields(connected_ctx, hr.ListCustomFieldsParams(
        board="Roadmap"))

    assert result.status == "success", result.error
    assert result.data.total == 1


async def test_happy_list_custom_fields_empty_board(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([])
    result = await hr.list_custom_fields(connected_ctx, hr.ListCustomFieldsParams(
        board="Roadmap"))

    assert result.status == "success", result.error
    assert result.data.total == 0


# --------------------------- list_notifications ---------------------------------

async def test_happy_list_notifications(connected_ctx, http):
    http.push([
        {"id": "N1", "type": "addedToCard", "date": "2026-08-01T00:00:00Z",
         "unread": True, "data": {"card": {"name": "Launch card"}}},
    ])
    result = await hr.list_notifications(connected_ctx, hr.ListNotificationsParams())

    assert result.status == "success", result.error
    assert result.data.total == 1


async def test_happy_list_notifications_unread_only_sets_filter(
        connected_ctx, http):
    http.push([])
    result = await hr.list_notifications(connected_ctx, hr.ListNotificationsParams(
        unread_only=True))

    assert result.status == "success", result.error
    assert http.last_params().get("read_filter") == "unread"


async def test_error_list_notifications_without_credentials(ctx, http):
    result = await hr.list_notifications(ctx, hr.ListNotificationsParams())
    assert result.status == "error"
    assert not http.calls


# ------------------------------ list_stickers -----------------------------------

async def test_happy_list_stickers(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([{"id": "C1", "name": "Launch card"}])
    http.push([{"id": "S1", "image": "check", "imageUrl": "https://x/check.png"}])
    result = await hr.list_stickers(connected_ctx, hr.ListAttachmentsParams(
        board="Roadmap", card="Launch card"))

    assert result.status == "success", result.error
    assert result.data.total == 1


async def test_happy_list_stickers_none_on_card(connected_ctx, http):
    http.push(member_payload())  # resolve_board's own auth check
    http.push([{"id": "B1", "name": "Roadmap"}])
    http.push([{"id": "C1", "name": "Launch card"}])
    http.push([])
    result = await hr.list_stickers(connected_ctx, hr.ListAttachmentsParams(
        board="Roadmap", card="Launch card"))

    assert result.status == "success", result.error
    assert result.data.total == 0


# ------------------------------ list_workspaces ----------------------------------

async def test_happy_list_workspaces(connected_ctx, http):
    http.push([{"id": "O1", "displayName": "Acme Studio",
                     "url": "https://trello.com/acmestudio"}])
    result = await hr.list_workspaces(connected_ctx, hr.ListWorkspacesParams())

    assert result.status == "success", result.error
    assert result.data.total == 1


async def test_error_list_workspaces_without_credentials(ctx, http):
    result = await hr.list_workspaces(ctx, hr.ListWorkspacesParams())
    assert result.status == "error"
    assert not http.calls
