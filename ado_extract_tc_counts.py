import requests

import pandas as pd

from datetime import datetime
from requests.auth import HTTPBasicAuth

ORG = "your_org"
PROJECT = "your_project"
PLAN_ID = your_plan_id
#PAT requires read rights to work items and test management
PAT = "your_pat"

# These can be top-level suite IDs or specific child suite IDs.
# The script will discover child suites and extract from all relevant suites.

ROOT_SUITE_IDS = [
    
]

OUTPUT_FILE = "ado_test_case_snapshot.csv"

API_VERSION = "7.1"
BASE_URL = f"https://dev.azure.com/{ORG}/{PROJECT}/_apis"

auth = HTTPBasicAuth("", PAT)


# =========================

# HELPERS

# =========================

def ado_get(url, params=None):
    response = requests.get(url, auth=auth, params=params, timeout=(5, 30))
    response.raise_for_status()
    return response.json()

def normalise_outcome(outcome):
    if not outcome:
        return "Never Executed"

    outcome = str(outcome).strip()

    if outcome.lower() in [
        "unspecified",
        "none",
        "notexecuted",
        "not executed",
        "never executed",

    ]:
        return "Never Executed"

    return outcome

def get_all_suites_for_plan():
    url = f"{BASE_URL}/testplan/Plans/{PLAN_ID}/Suites"

    params = {
        "api-version": API_VERSION,
        "expand": "children",
    }

    data = ado_get(url, params=params)

    return data.get("value", [])

def build_suite_lookup(suites):
    suite_lookup = {}

    in_children_by_parent_id = {}

    for suite in suites:
        in_suite_id = int(suite["id"])
        suite_lookup[in_suite_id] = suite
        parent = suite.get("parentSuite")
        parent_id = int(parent["id"]) if parent and parent.get("id") else None

        if parent_id is not None:
            in_children_by_parent_id.setdefault(parent_id, []).append(in_suite_id)

    return suite_lookup, in_children_by_parent_id

def collect_descendant_suite_ids(root_suite_ids, children_by_parent_id):
    collected = set()

    def walk(current_suite_id):

        current_suite_id = int(current_suite_id)
        if current_suite_id in collected:
            return

        collected.add(current_suite_id)

        for child_id in children_by_parent_id.get(current_suite_id, []):
            walk(child_id)

    for root_id in root_suite_ids:
        walk(root_id)

    return sorted(collected)

def get_test_points_for_suite(suite_id):
    url = f"{BASE_URL}/testplan/Plans/{PLAN_ID}/Suites/{suite_id}/TestPoint"

    params = {
        "api-version": API_VERSION,
        "includePointDetails": "true",
        "returnIdentityRef": "true",
        "isRecursive": "false",
    }

    all_points = []
    continuation_token = None

    while True:
        if continuation_token:
            params["continuationToken"] = continuation_token
        elif "continuationToken" in params:
            del params["continuationToken"]

        response = requests.get(url, auth=auth, params=params)
        response.raise_for_status()
        data = response.json()
        all_points.extend(data.get("value", []))

        continuation_token = response.headers.get("x-ms-continuationtoken")
        if not continuation_token:
            break

    return all_points

def get_test_case_work_items(test_case_ids):
    """
    Bulk fetches priority and tags from the Test Case work items.
    Azure DevOps batch endpoint usually supports chunks up to 200 IDs safely.
    """

    lookup = {}

    ids = sorted({
        int(str(x).strip())
        for x in test_case_ids
        if str(x).strip().isdigit()
    })

    if not ids:
        return lookup

    chunk_size = 200

    for i in range(0, len(ids), chunk_size):
        chunk = ids[i:i + chunk_size]
        url = f"{BASE_URL}/wit/workitemsbatch"

        params = {
            "api-version": API_VERSION,
        }

        headers = {
            "Content-Type": "application/json",
        }

        body = {
            "ids": chunk,
            "fields": [
                "System.Id",
                "System.Title",
                "System.Tags",
                "Microsoft.VSTS.Common.Priority",
            ],
        }

        response = requests.post(url,
                                auth=auth,
                                headers=headers,
                                params=params,
                                json=body,
                                timeout=(5, 30)
                            )

        response.raise_for_status()
        data = response.json()

        for item in data.get("value", []):
            fields = item.get("fields", {})

            lookup[str(item["id"])] = {
                "title": fields.get("System.Title", ""),
                "tags": fields.get("System.Tags", ""),
                "priority": fields.get("Microsoft.VSTS.Common.Priority", ""),
            }

    return lookup

def extract_point_row(point, suite_id, extracted_date):
    test_case = (
        point.get("testCaseReference")
        or point.get("testCase")
        or {}
    )

    results = point.get("results", {}) or {}

    last_run = (
        point.get("lastTestRun")
        or results.get("lastTestRun")
        or {}
    )

    last_updated_by = point.get("lastUpdatedBy") or {}

    test_case_id = (
        test_case.get("id")
        or point.get("testCaseId")
        or ""
    )

    test_case_title = (
        test_case.get("name")
        or test_case.get("title")
        or ""
    )

    outcome = normalise_outcome(
        results.get("outcome")
        or point.get("outcome")
        or ""
    )

    run_id = (
        results.get("lastTestRunId")
        or last_run.get("id")
        or ""
    )

    run_name = (
        last_run.get("name")
        or ""
    )

    executed_by = (
        last_updated_by.get("displayName")
        or last_updated_by.get("uniqueName")
        or ""
    )

    executed_date = (
        point.get("lastUpdatedDate")
        or results.get("lastResultDate")
        or results.get("lastUpdatedDate")
        or ""
    )

    if outcome == "Never Executed":
        executed_by = ""
        executed_date = ""
        run_id = ""
        run_name = ""

    return {

        "test_case_id": test_case_id,
        "test_case_title": test_case_title,
        "priority": "",
        "suite_ids": suite_id,
        "tags": "",
        "outcome": outcome,
        "executed_by": executed_by,
        "executed_date": executed_date,
        "run_id": run_id,
        "run_name": run_name,
        "extracted_date": extracted_date,

    }

# =========================

# MAIN

# =========================

print("Retrieving suites...")

all_suites = get_all_suites_for_plan()

suite_by_id, children_by_parent_id = build_suite_lookup(all_suites)

suite_ids_to_check = collect_descendant_suite_ids(
    ROOT_SUITE_IDS,
    children_by_parent_id,
)

print(f"Suites selected for checking: {len(suite_ids_to_check)}")

rows = []

extracted_date = datetime.today().strftime("%d/%m/%Y")

for suite_id in suite_ids_to_check:
    suite_name = suite_by_id.get(suite_id, {}).get("name", "")
    points = get_test_points_for_suite(suite_id)

    print(f"Suite {suite_id} | {suite_name} | points found: {len(points)}")

    for point in points:
        row = extract_point_row(point, suite_id, extracted_date)

        if not str(row["test_case_id"]).strip():
            print(f"WARNING: Missing test_case_id in suite {suite_id}")
            print(point.keys())

            continue

        rows.append(row)

expected_columns = [
    "test_case_id",
    "test_case_title",
    "priority",
    "suite_ids",
    "tags",
    "outcome",
    "executed_by",
    "executed_date",
    "run_id",
    "run_name",
    "extracted_date",
]

df = pd.DataFrame(rows, columns=expected_columns)

if df.empty:
    print("No test points found.")
    df.to_csv(OUTPUT_FILE, index=False)
    exit()

# =========================

# ENRICH TEST CASE FIELDS

# =========================

print("Enriching test cases with title, priority and tags...")

test_case_ids = (
    df["test_case_id"]
    .dropna()
    .astype(str)
    .str.strip()
    .unique()
    .tolist()
)

work_item_lookup = get_test_case_work_items(test_case_ids)

df["test_case_title"] = df.apply(
    lambda row: (
        work_item_lookup
        .get(str(row["test_case_id"]), {})
        .get("title", row["test_case_title"])
        or row["test_case_title"]
    ),
    axis=1,
)

df["priority"] = df["test_case_id"].astype(str).map(
    lambda x: work_item_lookup.get(x, {}).get("priority", "")
)

df["tags"] = df["test_case_id"].astype(str).map(
    lambda x: work_item_lookup.get(x, {}).get("tags", "")
)

# =========================

# OPTIONAL COLLAPSE DUPLICATES

# =========================

df = (
    df.groupby("test_case_id", as_index=False)
      .agg({
          "test_case_title": "first",
          "priority": "first",
          "suite_ids": lambda x: "; ".join(str(v) for v in sorted(set(x))),
          "tags": "first",
          "outcome": "first",
          "executed_by": "first",
          "executed_date": "first",
          "run_id": "first",
          "run_name": "first",
          "extracted_date": "first",
      })

)

df = df[expected_columns]
df.to_csv(OUTPUT_FILE, index=False)

print(f"Done. Rows exported: {len(df)}")
print(f"File created: {OUTPUT_FILE}")
