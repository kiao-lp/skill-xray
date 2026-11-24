#!/usr/bin/env python3
"""Command line for Xray Cloud in a Kiao worker turn."""

import argparse
import base64
import mimetypes
import re
import sys
from pathlib import Path

# The worker verifies the installed package tree between turns.
sys.dont_write_bytecode = True

import xray_issues as issues
import xray_reports as reports
from xray_client import InputError, XrayClient, XrayError, json_text, read_file, read_json, read_text


RUN_DETAILS = (
    "comment startedOn finishedOn assigneeId executedById defects "
    "evidence{id filename size} "
    "steps{id status{name} action data result actualResult comment defects evidence{id filename}} "
    "examples{id status{name}} gherkin unstructured"
)
PROJECT_SETTINGS = (
    "projectId testEnvironments testTypeSettings{defaultTestTypeId testTypes{id name kind}} "
    "testCoverageSettings{coverableIssueTypeIds issueLinkTypeId issueLinkTypeDirection}"
)
FOLDER = "name path issuesCount testsCount preconditionsCount"


def project_settings(client, project, selection):
    data = client.field("getProjectSettings", {"projectIdOrKey": ("String!", project)}, selection)
    if not isinstance(data, dict) or not data.get("projectId"):
        raise XrayError(f"Xray project {project} was not found")
    return data


def project_id(client, project):
    if issues.ISSUE_ID.fullmatch(project):
        return project
    return project_settings(client, project, "projectId")["projectId"]


def project(client, args):
    return project_settings(client, args.project, PROJECT_SETTINGS)


def statuses(client, args):
    field = "getStepStatuses" if args.steps else "getStatuses"
    project = project_id(client, args.project) if args.project else None
    selection = "name testStatus{name}" if args.steps else "name final coverageStatus"
    data = client.field(field, {"projectId": ("String", project)}, selection)
    if not isinstance(data, list):
        raise XrayError("Xray did not return the statuses")
    return {"items": data}


def runs(client, args):
    groups = {kind: getattr(args, kind) for kind in ("test", "execution", "plan") if getattr(args, kind)}
    if not groups:
        raise InputError("Filter runs by --test, --execution or --plan")
    ids = issues.resolve_ids(client, groups)
    value = client.field(
        "getTestRuns",
        {
            "testIssueIds": ("[String]", ids.get("test")),
            "testExecIssueIds": ("[String]", ids.get("execution")),
            "testPlanIssueIds": ("[String]", ids.get("plan")),
            "statuses": ("[String]", args.status),
            **issues.page_args(args.limit, args.start),
        },
        issues.list_selection("run", issues.RUN),
    )
    return issues.page(value)


def run(client, args):
    selection = f"{issues.RUN} {RUN_DETAILS}" if args.details else issues.RUN
    data = client.field("getTestRunById", {"id": ("String!", args.run_id)}, selection)
    if not isinstance(data, dict):
        raise XrayError(f"Xray run {args.run_id} was not found")
    return data


def record(client, args):
    target = {"id": ("String!", args.run_id)}
    if args.status:
        return issues.mutate(client, "updateTestRunStatus", {**target, "status": ("String!", args.status)})
    comment = read_text(args.comment_file) if args.comment_file else args.comment
    return issues.mutate(client, "updateTestRunComment", {**target, "comment": ("String!", comment)})


def record_step(client, args):
    comment = read_text(args.comment_file) if args.comment_file else args.comment
    values = {"comment": comment, "status": args.status, "actualResult": args.actual_result}
    update = {key: value for key, value in values.items() if value is not None}
    if not update:
        raise InputError("Supply --status, --comment, --comment-file or --actual-result")
    result = issues.mutate(
        client, "updateTestRunStep",
        {
            "testRunId": ("String!", args.run_id), "stepId": ("String!", args.step_id),
            "iterationRank": ("String", args.iteration), "updateData": ("UpdateTestRunStepInput!", update),
        },
        "warnings",
    )
    return {"runId": args.run_id, "stepId": args.step_id, **result}


def defects(client, args):
    add = args.action == "add"
    return issues.mutate(
        client, "addDefectsToTestRun" if add else "removeDefectsFromTestRun",
        {"id": ("String!", args.run_id), "issues": ("[String]!", list(dict.fromkeys(args.issues)))},
        "addedDefects warnings" if add else "",
    )


def evidence(client, args):
    data = base64.b64encode(read_file(args.file)).decode("ascii")
    mime_type = mimetypes.guess_type(args.file.name)[0] or "application/octet-stream"
    file = {"data": data, "filename": args.file.name, "mimeType": mime_type}
    return issues.mutate(
        client, "addEvidenceToTestRun",
        {"id": ("String!", args.run_id), "evidence": ("[AttachmentDataInput]!", [file])},
        "addedEvidence warnings",
    )


def folder(client, args):
    payload = {
        "projectId": ("String!", project_id(client, args.project)), "path": ("String!", args.path),
    }
    if args.action == "tests":
        selection = issues.list_selection("test", issues.issue_selection("test"))
        folder_filter = {"path": args.path, "includeDescendants": args.descendants}
        return issues.page(client.field("getTests", {
            "projectId": payload["projectId"], "folder": ("FolderSearchInput", folder_filter),
            **issues.page_args(args.limit, args.start),
        }, selection))
    if args.action == "get":
        result = client.field("getFolder", payload, f"{FOLDER} folders")
        if not isinstance(result, dict):
            raise XrayError(f"Xray folder {args.path} was not found")
        return result
    if args.action == "rename":
        payload["newName"] = ("String!", args.name)
    if args.action == "move":
        payload["destinationPath"] = ("String!", args.destination)
    return issues.mutate(client, args.action + "Folder", payload, f"folder{{{FOLDER}}} warnings")


def graphql(client, args):
    variables = read_json(args.variables) if args.variables else {}
    if not isinstance(variables, dict):
        raise InputError("GraphQL variables must be a JSON object")
    return client.graphql(nonempty(read_text(args.file)), variables)


def nonempty(value):
    if not value.strip():
        raise InputError("Expected a nonempty value")
    return value


def project_key(value):
    key = value.upper()
    if not issues.PROJECT_KEY.fullmatch(key):
        raise InputError("Expected a Jira project key")
    return key


def project_ref(value):
    return value if issues.ISSUE_ID.fullmatch(value) else project_key(value)


def issue_key(value):
    key = issues.parse_ref(value)
    if not issues.ISSUE_KEY.fullmatch(key):
        raise InputError("Expected a Jira issue key")
    return key


def issue_id(value):
    if not issues.ISSUE_ID.fullmatch(value):
        raise InputError("Expected a numeric Jira issue ID")
    return value


def job_id(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise InputError("Expected an Xray bulk import job ID")
    return value


def folder_path(value):
    if not value.startswith("/"):
        raise InputError("Use an Xray folder path beginning with /")
    return value


def offset(value):
    start = int(value)
    if not 0 <= start < 2**31:
        raise InputError("--start must be a nonnegative GraphQL Int")
    return start


def limit(value):
    size = int(value)
    if not 1 <= size <= 100:
        raise InputError("--limit must be between 1 and 100")
    return size


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise InputError(message)


def page_options(parser):
    parser.add_argument("--limit", type=limit, default=20, metavar="N", help="Page size from 1 to 100, default 20")
    parser.add_argument("--start", type=offset, default=0, metavar="N", help="Offset from nextStart")


def command(subparsers, name, handler, description):
    parser = subparsers.add_parser(name, help=description, description=description)
    parser.set_defaults(handler=handler)
    return parser


def issue_commands(subparsers):
    search = command(subparsers, "search", issues.search, "List one page of Xray issues")
    search.add_argument("kind", choices=issues.KINDS)
    scope = search.add_mutually_exclusive_group(required=True)
    scope.add_argument("--jql", type=nonempty)
    scope.add_argument("--keys", type=issue_key, nargs="+")
    scope.add_argument("--ids", type=issue_id, nargs="+")
    page_options(search)

    for name, handler, description in (
        ("get", issues.get, "Read one Xray issue"),
        ("related", issues.related, "List one page of related Xray issues or runs"),
    ):
        parser = command(subparsers, name, handler, description)
        parser.add_argument("kind", choices=issues.KINDS)
        parser.add_argument("ref", type=issues.parse_ref, metavar="KEY_OR_ID")
        if name == "get":
            parser.add_argument("--details", action="store_true", help="Include the test or precondition definition")
        else:
            parser.add_argument("relation", choices=sorted({key for relations in issues.RELATIONS.values() for key in relations}))
            page_options(parser)

    create = command(subparsers, "create", issues.create, "Create an Xray issue")
    kinds = create.add_subparsers(dest="kind", required=True)
    for kind in issues.KINDS:
        if kind == "requirement":
            continue
        parser = kinds.add_parser(kind)
        parser.add_argument("project", type=project_key)
        parser.add_argument("summary", type=nonempty)
        parser.add_argument("--fields", type=Path, metavar="JSON", help="Additional Jira fields as an object")
        if kind in ("test", "precondition"):
            parser.add_argument("--type", type=nonempty, help="Xray type name")
            parser.add_argument("--folder", type=folder_path)
        if kind == "test":
            definitions = parser.add_mutually_exclusive_group()
            definitions.add_argument("--steps", type=Path, metavar="JSON")
            definitions.add_argument("--gherkin", type=Path, metavar="FILE")
            definitions.add_argument("--definition", type=Path, metavar="FILE")
            parser.add_argument("--preconditions", nargs="+", type=issues.parse_ref, metavar="KEY_OR_ID")
        else:
            parser.add_argument("--tests", nargs="+", type=issues.parse_ref, metavar="KEY_OR_ID")
        if kind == "precondition":
            parser.add_argument("--definition", type=Path, metavar="FILE")
        if kind == "execution":
            parser.add_argument("--environment", action="append", type=nonempty, metavar="NAME")

    edit = command(subparsers, "edit", issues.edit, "Update an Xray test or precondition definition, type or folder")
    edit.add_argument("kind", choices=("test", "precondition"))
    edit.add_argument("ref", type=issues.parse_ref, metavar="KEY_OR_ID")
    changes = edit.add_mutually_exclusive_group(required=True)
    changes.add_argument("--type", type=nonempty)
    changes.add_argument("--gherkin", type=Path, metavar="FILE")
    changes.add_argument("--definition", type=Path, metavar="FILE")
    changes.add_argument("--folder", type=folder_path)

    step = command(subparsers, "step", issues.step, "Add, update or remove a manual test definition step")
    steps = step.add_subparsers(dest="action", required=True)
    for action in ("add", "update", "remove"):
        parser = steps.add_parser(action)
        if action == "add":
            parser.add_argument("test", type=issues.parse_ref, metavar="TEST_KEY_OR_ID")
        else:
            parser.add_argument("step_id", type=nonempty)
        if action != "remove":
            parser.add_argument("file", type=Path, metavar="STEP_JSON")

    link = command(subparsers, "link", issues.link, "Add or remove Xray relationships")
    link.add_argument("action", choices=("add", "remove"))
    link.add_argument("kind", choices=("set", "plan", "execution", "precondition"))
    link.add_argument("ref", type=issues.parse_ref, metavar="KEY_OR_ID")
    link.add_argument("relation", choices=("tests", "executions", "environments"))
    link.add_argument("values", nargs="+", type=nonempty, metavar="VALUE")


def run_commands(subparsers):
    listing = command(subparsers, "runs", runs, "List test runs by test, execution or plan")
    for kind in ("test", "execution", "plan"):
        listing.add_argument(f"--{kind}", action="append", type=issues.parse_ref, metavar="KEY_OR_ID")
    listing.add_argument("--status", action="append", type=nonempty, metavar="STATUS_ID")
    page_options(listing)

    view = command(subparsers, "run", run, "Read a test run")
    view.add_argument("run_id", type=nonempty)
    view.add_argument("--details", action="store_true", help="Include steps, evidence metadata, defects and comment")

    update = command(subparsers, "record", record, "Set a run status or comment")
    update.add_argument("run_id", type=nonempty)
    fields = update.add_mutually_exclusive_group(required=True)
    fields.add_argument("--status", type=nonempty)
    fields.add_argument("--comment")
    fields.add_argument("--comment-file", type=Path, metavar="FILE")

    step = command(subparsers, "record-step", record_step, "Record a manual run step result")
    step.add_argument("run_id", type=nonempty)
    step.add_argument("step_id", type=nonempty)
    step.add_argument("--status", type=nonempty)
    step.add_argument("--actual-result")
    step.add_argument("--iteration", type=nonempty, metavar="RANK")
    comments = step.add_mutually_exclusive_group()
    comments.add_argument("--comment")
    comments.add_argument("--comment-file", type=Path, metavar="FILE")

    links = command(subparsers, "defects", defects, "Add or remove Jira defects on a run")
    links.add_argument("action", choices=("add", "remove"))
    links.add_argument("run_id", type=nonempty)
    links.add_argument("issues", nargs="+", type=issues.parse_ref, metavar="KEY_OR_ID")

    upload = command(subparsers, "evidence", evidence, "Attach a local evidence file to a run")
    upload.add_argument("run_id", type=nonempty)
    upload.add_argument("file", type=Path)


def report_commands(subparsers):
    results = command(subparsers, "import-results", reports.import_results, "Validate or import execution results")
    results.add_argument("format", choices=reports.FORMATS)
    results.add_argument("file", type=Path)
    results.add_argument("--validate-only", action="store_true", help="Check the file locally")
    results.add_argument("--project", type=project_key, help="Destination project for XML")
    results.add_argument("--execution", type=issue_key, help="Existing execution for XML")
    results.add_argument("--plan", type=issue_key, help="Associated test plan for XML")
    results.add_argument("--environment", action="append", type=nonempty, help="Test environment for XML")

    tests = command(subparsers, "import-tests", reports.import_tests, "Validate or submit a bulk test import")
    tests.add_argument("file", type=Path)
    tests.add_argument("--validate-only", action="store_true", help="Check the file locally")

    status = command(subparsers, "import-status", reports.import_status, "Read a bulk test import job")
    status.add_argument("job_id", type=job_id)
    status.add_argument("--details", action="store_true", help="Include full progress and issue lists")

    export = command(subparsers, "export-cucumber", reports.export_cucumber, "Save generated Cucumber features as ZIP")
    export.add_argument("output", type=Path, metavar="NEW_ZIP")
    scope = export.add_mutually_exclusive_group(required=True)
    scope.add_argument("--keys", nargs="+", type=issue_key)
    scope.add_argument("--filter", type=issue_id, metavar="JIRA_FILTER_ID")


def parser():
    root = Parser(description=__doc__)
    subparsers = root.add_subparsers(dest="command", required=True)
    issue_commands(subparsers)
    run_commands(subparsers)
    report_commands(subparsers)

    settings = command(subparsers, "project", project, "Read project test types, environments and coverage settings")
    settings.add_argument("project", type=project_ref, metavar="PROJECT_KEY_OR_ID")
    status = command(subparsers, "statuses", statuses, "List available statuses and check authentication")
    status.add_argument("project", nargs="?", type=project_ref, metavar="PROJECT_KEY_OR_ID")
    status.add_argument("--steps", action="store_true", help="List step statuses instead of run statuses")

    folders = command(subparsers, "folder", folder, "Browse or organize the test repository")
    actions = folders.add_subparsers(dest="action", required=True)
    for action in ("get", "tests", "create", "rename", "move"):
        item = actions.add_parser(action)
        item.add_argument("project", type=project_ref, metavar="PROJECT_KEY_OR_ID")
        item.add_argument("path", type=folder_path)
        if action == "tests":
            item.add_argument("--descendants", action="store_true", help="Include tests from child folders")
            page_options(item)
        if action == "rename":
            item.add_argument("name", type=nonempty)
        if action == "move":
            item.add_argument("destination", type=folder_path)

    custom = command(subparsers, "graphql", graphql, "Run one GraphQL document from a file")
    custom.add_argument("file", type=Path)
    custom.add_argument("--variables", type=Path, metavar="JSON")
    return root


def main(argv=None):
    client = XrayClient()
    try:
        args = parser().parse_args(argv)
        result = args.handler(client, args)
        print(json_text(client.safe(result)))
    except (InputError, OSError, ValueError) as exc:
        print(json_text(client.safe({"error": str(exc)})), file=sys.stderr)
        return 2
    except XrayError as exc:
        error = {"error": str(exc)}
        if exc.status is not None:
            error["status"] = exc.status
        if exc.retry_after:
            error["retryAfter"] = exc.retry_after
        print(json_text(client.safe(error)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
