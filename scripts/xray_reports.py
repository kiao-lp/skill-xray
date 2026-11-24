"""Validate local reports and exchange them through the Xray REST API."""

import io
from xml.etree import ElementTree
from zipfile import is_zipfile

from xray_client import InputError, XrayError, decode_response, json_text, parse_json, read_file, read_json
from xray_issues import ISSUE_KEY, PROJECT_KEY


XML_ROOTS = {
    "junit": ("testsuite", "testsuites"),
    "nunit": ("test-results", "test-run"),
    "xunit": ("assembly", "assemblies"),
    "testng": ("testng-results",),
    "robot": ("robot",),
}
FORMATS = ("xray", "cucumber", "behave", *XML_ROOTS)


def validate_xray_json(report):
    if not isinstance(report, dict) or not isinstance(report.get("tests"), list) or not report["tests"]:
        raise InputError("Expected an Xray JSON object with a nonempty tests array")
    if "testExecutionKey" in report and not matches(report["testExecutionKey"], ISSUE_KEY):
        raise InputError("testExecutionKey must be a Jira issue key")
    if "info" in report:
        info = report["info"]
        if not isinstance(info, dict):
            raise InputError("info must be an object")
        for name, pattern in (("project", PROJECT_KEY), ("testPlanKey", ISSUE_KEY)):
            if name in info and not matches(info[name], pattern):
                raise InputError(f"info.{name} must be a Jira key")
    for index, test in enumerate(report["tests"]):
        prefix = f"tests[{index}]"
        if not isinstance(test, dict) or not has_text(test.get("status")):
            raise InputError(f"{prefix} must be an object with a status string")
        if "testKey" in test and not matches(test["testKey"], ISSUE_KEY):
            raise InputError(f"{prefix}.testKey must be a Jira issue key")
        if "testInfo" in test:
            info = test["testInfo"]
            if not isinstance(info, dict) or not all(has_text(info.get(key)) for key in ("summary", "type")):
                raise InputError(f"{prefix}.testInfo requires summary and type strings")
            if not matches(info.get("projectKey"), PROJECT_KEY):
                raise InputError(f"{prefix}.testInfo.projectKey must be a Jira project key")
        elif "testKey" not in test:
            raise InputError(f"{prefix} requires testKey or testInfo")
    return len(report["tests"])


def matches(value, pattern):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def has_text(value):
    return isinstance(value, str) and bool(value.strip())


def validate_report(data, format_name):
    if format_name in XML_ROOTS:
        try:
            root = ElementTree.fromstring(data)
        except (ElementTree.ParseError, LookupError, ValueError):
            raise InputError(f"Invalid {format_name} XML") from None
        tag = root.tag.rsplit("}", 1)[-1]
        if tag not in XML_ROOTS[format_name]:
            raise InputError(f"Unexpected {format_name} XML root; expected " + " or ".join(XML_ROOTS[format_name]))
        return {"root": tag}

    try:
        report = parse_json(data)
        json_text(report).encode("utf-8")
    except (ValueError, RecursionError):
        raise InputError(f"Invalid {format_name} JSON") from None
    if format_name == "xray":
        return {"tests": validate_xray_json(report)}
    if not isinstance(report, list) or not report or any(not isinstance(item, dict) for item in report):
        raise InputError(f"Expected a nonempty {format_name} JSON array of feature objects")
    return {"features": len(report)}


def import_results(client, args):
    format_name = args.format
    options = {
        "projectKey": args.project, "testExecKey": args.execution, "testPlanKey": args.plan,
        "testEnvironments": ";".join(args.environment or []),
    }
    query = {key: value for key, value in options.items() if value}
    is_xml = format_name in XML_ROOTS
    if query and not is_xml:
        raise InputError("Use the JSON report to specify the destination; destination flags apply to XML")
    data = read_file(args.file)
    summary = validate_report(data, format_name)
    if args.validate_only:
        return {"format": format_name, "valid": True, **summary}

    suffix = "" if format_name == "xray" else f"/{format_name}"
    content_type = "text/xml" if is_xml else "application/json"
    value = decode_response(client.request(
        "POST", "/import/execution" + suffix, data=data, content_type=content_type, query=query,
    ))
    if not isinstance(value, dict):
        raise XrayError("Xray did not return the imported execution")
    issue = value.get("testExecIssue", value)
    if not isinstance(issue, dict) or not issue.get("key"):
        raise XrayError("Xray did not return the imported execution key")
    result = {key: issue[key] for key in ("key", "id") if key in issue}
    if value.get("warnings"):
        result["warnings"] = value["warnings"]
    return result


def import_tests(client, args):
    tests = read_json(args.file)
    if not isinstance(tests, list) or not 1 <= len(tests) <= 1000:
        raise InputError("Expected a JSON array of 1 to 1000 test objects")
    if any(not isinstance(test, dict) or not test for test in tests):
        raise InputError("Each test must be a nonempty object")
    if args.validate_only:
        return {"valid": True, "tests": len(tests)}
    result = client.json_request("POST", "/import/test/bulk", payload=tests)
    if not isinstance(result, dict) or not has_text(result.get("jobId")):
        raise XrayError("Xray did not return a bulk import job ID")
    return {"jobId": result["jobId"]}


def import_status(client, args):
    path = f"/import/test/bulk/{args.job_id}/status"
    status = decode_response(client.request("GET", path))
    if not isinstance(status, dict) or not has_text(status.get("status")):
        raise XrayError("Xray did not return an import status")
    if args.details:
        return status
    summary = {key: status[key] for key in ("status", "progressValue") if key in status}
    result = status.get("result")
    if isinstance(result, dict):
        if isinstance(result.get("issues"), list):
            issues = result["issues"]
            summary["issueCount"] = len(issues)
            summary["sampleKeys"] = [item["key"] for item in issues[:10] if isinstance(item, dict) and "key" in item]
        if isinstance(result.get("errors"), list):
            summary["errorCount"] = len(result["errors"])
        if isinstance(result.get("error"), str):
            summary["error"] = client.safe(result["error"])[:300]
    return summary


def export_cucumber(client, args):
    if args.output.exists() or args.output.is_symlink():
        raise InputError(f"Output already exists: {args.output}")
    query = {"keys": ";".join(dict.fromkeys(args.keys))} if args.keys else {"filter": args.filter}
    data = client.request("GET", "/export/cucumber", query=query, accept="application/octet-stream")
    if not is_zipfile(io.BytesIO(data)):
        raise XrayError("Xray did not return a Cucumber ZIP archive")
    with args.output.open("xb") as file:
        file.write(data)
    return {"file": str(args.output), "bytes": len(data)}
