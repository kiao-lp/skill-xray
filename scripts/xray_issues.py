"""Issue reads, test definitions and Xray relationships."""

import re

from xray_client import InputError, XrayError, operation, read_json, read_text


IDENTITY = 'issueId jira(fields:["key","summary"])'
STEP = "id action data result callTestIssueId"
RUN = f"id status{{name}} test{{{IDENTITY}}} testExecution{{{IDENTITY}}}"
KINDS = {
    "test": "Test",
    "plan": "TestPlan",
    "execution": "TestExecution",
    "set": "TestSet",
    "precondition": "Precondition",
    "requirement": "CoverableIssue",
}
SUMMARY = {
    "test": "testType{name} status{name}",
    "execution": "testEnvironments",
    "precondition": "preconditionType{name}",
    "requirement": "status(isFinal:true){name}",
}
DETAILS = {
    "test": f"steps{{{STEP}}} gherkin unstructured folder{{path}}",
    "precondition": "definition folder{path}",
}
RELATIONS = {
    "test": {
        "preconditions": "precondition", "sets": "set", "plans": "plan",
        "executions": "execution", "requirements": "requirement", "runs": "run",
    },
    "plan": {"tests": "test", "executions": "execution"},
    "execution": {"tests": "test", "plans": "plan", "runs": "run"},
    "set": {"tests": "test"},
    "precondition": {"tests": "test"},
    "requirement": {"tests": "test"},
}
RELATION_FIELDS = {
    "sets": "testSets", "plans": "testPlans", "executions": "testExecutions",
    "requirements": "coverableIssues", "runs": "testRuns",
}
WARNINGS = {"test", "plan", "requirement"}
PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]*", re.ASCII)
ISSUE_KEY = re.compile(r"[A-Z][A-Z0-9_]*-[1-9][0-9]*", re.ASCII)
ISSUE_ID = re.compile(r"[1-9][0-9]*", re.ASCII)


def issue_selection(kind):
    return f"{IDENTITY} {SUMMARY.get(kind, '')}".rstrip()


def list_selection(kind, selection):
    diagnostics = "warnings " if kind in WARNINGS else ""
    return f"total start {diagnostics}results{{{selection}}}"


def page_args(limit, start):
    return {"limit": ("Int!", limit), "start": ("Int", start)}


def page(value):
    if not isinstance(value, dict) or not isinstance(value.get("results"), list):
        raise XrayError("Xray returned no result page")
    items = value["results"]
    total, start = value.get("total"), value.get("start")
    if not isinstance(total, int) or not isinstance(start, int):
        raise XrayError("Xray did not return pagination metadata")
    result = {"items": items, "total": total}
    if items and start + len(items) < total:
        result["nextStart"] = start + len(items)
    if value.get("warnings"):
        result["warnings"] = value["warnings"]
    return result


def key_jql(keys):
    return "key in (" + ",".join(keys) + ")"


def jira_key(issue):
    if isinstance(issue, dict) and isinstance(issue.get("jira"), dict):
        return issue["jira"].get("key")
    return None


def search(client, args):
    jql = key_jql(args.keys) if args.keys else args.jql
    value = client.field(
        f"get{KINDS[args.kind]}s",
        {"jql": ("String", jql), "issueIds": ("[String]", args.ids), **page_args(args.limit, args.start)},
        list_selection(args.kind, issue_selection(args.kind)),
    )
    return page(value)


def read_issue(client, kind, ref, selection):
    field = f"get{KINDS[kind]}"
    if ISSUE_ID.fullmatch(ref):
        value = client.field(field, {"issueId": ("String!", ref)}, selection)
    else:
        value = client.field(
            field + "s", {"jql": ("String", key_jql([ref])), **page_args(1, 0)},
            list_selection(kind, selection),
        )
        value = next((item for item in page(value)["items"] if jira_key(item) == ref), None)
    if not isinstance(value, dict):
        raise XrayError(f"Xray {kind} {ref} was not found")
    return value


def get(client, args):
    selection = issue_selection(args.kind)
    if args.details and args.kind in DETAILS:
        selection += " " + DETAILS[args.kind]
    return read_issue(client, args.kind, args.ref, selection)


def related(client, args):
    relation = args.relation
    kind = RELATIONS[args.kind].get(relation)
    if kind is None:
        raise InputError(f"{args.kind} relations: " + ", ".join(RELATIONS[args.kind]))
    field = RELATION_FIELDS.get(relation, relation)
    selection = RUN if kind == "run" else issue_selection(kind)
    connection = list_selection(kind, selection)
    value = read_issue(
        client, args.kind, args.ref,
        f'jira(fields:["key"]) {field}(limit:{args.limit},start:{args.start}){{{connection}}}',
    )
    return page(value.get(field))


def resolve_ids(client, groups):
    """Resolve all supplied issue keys with bounded batches of GraphQL fields."""
    fields, expected, matches = [], [], {}
    for kind, refs in groups.items():
        keys = list(dict.fromkeys(ref for ref in refs if ISSUE_KEY.fullmatch(ref)))
        for start in range(0, len(keys), 100):
            chunk = keys[start:start + 100]
            alias = f"refs{len(fields)}"
            fields.append((
                alias, f"get{KINDS[kind]}s",
                {"jql": ("String", key_jql(chunk)), **page_args(len(chunk), 0)},
                'results{issueId jira(fields:["key"])}',
            ))
            expected.append((kind, alias, chunk))
    for start in range(0, len(fields), 10):
        document, variables = operation(fields[start:start + 10])
        data = client.graphql(document, variables)
        for kind, alias, keys in expected[start:start + 10]:
            rows = (data.get(alias) or {}).get("results") or []
            found = {jira_key(row): row.get("issueId") for row in rows if isinstance(row, dict)}
            missing = [key for key in keys if not ISSUE_ID.fullmatch(str(found.get(key)))]
            if missing:
                detail = ", ".join(missing[:5])
                raise XrayError(f"Could not resolve {len(missing)} {kind} key(s): {detail}")
            matches.update({(kind, key): str(found[key]) for key in keys})
    return {
        kind: list(dict.fromkeys(matches.get((kind, ref), ref) for ref in refs))
        for kind, refs in groups.items()
    }


def valid_steps(value, *, multiple=False):
    steps = value if multiple else [value]
    if not isinstance(steps, list) or not steps or any(not isinstance(step, dict) or not step for step in steps):
        raise InputError("Steps must be a nonempty JSON array of step objects" if multiple else "Expected a step object")
    for step in steps:
        if any(key in step and not isinstance(step[key], str) for key in ("action", "data", "result")):
            raise InputError("Step action, data and result must be strings")
    return value


def create(client, args):
    fields = read_json(args.fields) if args.fields else {}
    if not isinstance(fields, dict) or {"project", "summary", "issuetype"} & fields.keys():
        raise InputError("Additional Jira fields must be an object; project, summary and issue type come from the command")
    fields.update(project={"key": args.project}, summary=args.summary)
    payload = {"jira": ("JSON!", {"fields": fields})}

    if args.kind == "test":
        if args.steps:
            payload["steps"] = ("[CreateStepInput]", valid_steps(read_json(args.steps), multiple=True))
        if args.gherkin:
            payload["gherkin"] = ("String", read_text(args.gherkin))
        if args.definition:
            payload["unstructured"] = ("String", read_text(args.definition))
        if args.type:
            payload["testType"] = ("UpdateTestTypeInput", {"name": args.type})
        if args.preconditions:
            ids = resolve_ids(client, {"precondition": args.preconditions})
            payload["preconditionIssueIds"] = ("[String]", ids["precondition"])
    elif args.kind == "precondition":
        if args.definition:
            payload["definition"] = ("String", read_text(args.definition))
        if args.type:
            payload["preconditionType"] = ("UpdatePreconditionTypeInput", {"name": args.type})
    elif args.kind == "execution" and args.environment:
        payload["testEnvironments"] = ("[String]", args.environment)

    if getattr(args, "folder", None):
        payload["folderPath"] = ("String", args.folder)
    if getattr(args, "tests", None):
        ids = resolve_ids(client, {"test": args.tests})
        payload["testIssueIds"] = ("[String]", ids["test"])

    issue_type = KINDS[args.kind]
    field = issue_type[0].lower() + issue_type[1:]
    extra = " createdTestEnvironments" if args.kind == "execution" else ""
    result = client.field(f"create{issue_type}", payload, f"{field}{{{IDENTITY}}} warnings{extra}", mutation=True)
    if not isinstance(result, dict) or not isinstance(result.get(field), dict):
        raise XrayError("Xray did not return the created issue")
    return result


def edit(client, args):
    if args.kind == "precondition":
        if args.gherkin:
            raise InputError("Use --definition for a precondition")
        update = {}
        if args.definition:
            update["definition"] = read_text(args.definition)
        if args.type:
            update["preconditionType"] = {"name": args.type}
        if args.folder:
            update["folderPath"] = args.folder
        field, payload = "updatePrecondition", {"data": ("UpdatePreconditionInput!", update)}
        selection = IDENTITY
    else:
        selection = IDENTITY
        if args.type:
            field = "updateTestType"
            payload = {"testType": ("UpdateTestTypeInput!", {"name": args.type})}
        elif args.gherkin:
            field = "updateGherkinTestDefinition"
            payload = {"gherkin": ("String!", read_text(args.gherkin))}
        elif args.definition:
            field = "updateUnstructuredTestDefinition"
            payload = {"unstructured": ("String!", read_text(args.definition))}
        else:
            field = "updateTestFolder"
            payload = {"folderPath": ("String!", args.folder)}
            selection = ""
    issue = resolve_ids(client, {args.kind: [args.ref]})[args.kind][0]
    return mutate(client, field, {"issueId": ("String!", issue), **payload}, selection)


def mutate(client, field, payload, selection=""):
    result = client.field(field, payload, selection, mutation=True)
    if result is None:
        raise XrayError(f"Xray did not return a result for {field}")
    return result if isinstance(result, dict) else {"result": result}


def step(client, args):
    if args.action == "remove":
        return mutate(client, "removeTestStep", {"stepId": ("String!", args.step_id)})
    data = valid_steps(read_json(args.file))
    if args.action == "update":
        return mutate(
            client, "updateTestStep", {"stepId": ("String!", args.step_id), "step": ("UpdateStepInput!", data)},
            "addedAttachments removedAttachments warnings",
        )
    test = resolve_ids(client, {"test": [args.test]})["test"][0]
    return mutate(client, "addTestStep", {"issueId": ("String!", test), "step": ("CreateStepInput!", data)}, "id")


def link(client, args):
    targets = {"tests": "test", "executions": "execution", "environments": None}
    target = targets[args.relation]
    supported = args.relation == "tests" or (args.kind, args.relation) in {
        ("plan", "executions"), ("execution", "environments"),
    }
    if not supported:
        raise InputError(f"{args.kind} does not support linking {args.relation}")
    if target:
        refs = [parse_ref(value) for value in args.values]
        ids = resolve_ids(client, {args.kind: [args.ref], target: refs})
        values = ids[target]
    else:
        values = list(dict.fromkeys(args.values))
        ids = resolve_ids(client, {args.kind: [args.ref]})
    suffix = KINDS[args.kind]
    verb = "To" if args.action == "add" else "From"
    name, argument, selection = {
        "tests": ("Tests", "testIssueIds", "addedTests warning"),
        "executions": ("TestExecutions", "testExecIssueIds", "addedTestExecutions warning"),
        "environments": ("TestEnvironments", "testEnvironments", "associatedTestEnvironments createdTestEnvironments warning"),
    }[args.relation]
    return mutate(
        client, f"{args.action}{name}{verb}{suffix}",
        {"issueId": ("String!", ids[args.kind][0]), argument: ("[String]!", values)},
        selection if args.action == "add" else "",
    )


def parse_ref(value):
    if ISSUE_ID.fullmatch(value):
        return value
    key = value.upper()
    if not ISSUE_KEY.fullmatch(key):
        raise InputError("Expected a Jira issue key or numeric issue ID")
    return key
