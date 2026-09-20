"""Batch data-plan endpoint tests: cross-case combination, pruning, sorting.

Run with: python tests/test_batch.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.store import store  # noqa: E402

client = TestClient(app)

CAP_ENV = "MAX_BATCH_DATA_PLAN_COMBINATIONS"


def setup_function():
    store.clear()
    os.environ.pop(CAP_ENV, None)


CMP = lambda f, op, v: {"field": f, "op": op, "value": v}


def create_policy(rules=None):
    policy = {"rules": rules or [
        {"id": "r", "priority": 10, "conclusion": "ok",
         "expr": {"any": [CMP("a", "eq", 1), CMP("b", "eq", 1)]}},
    ]}
    r = client.post("/api/v1/policies", json=policy)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def post_batch(pid, cases):
    return client.post(
        f"/api/v1/policies/{pid}/batch-data-plans", json={"cases": cases}
    )


def unknown_case(case_id, facts=None):
    return {"case_id": case_id, "facts": {} if facts is None else facts}


def test_single_unknown_case_reuses_engine_minimal_sets():
    pid = create_policy()
    r = post_batch(pid, [unknown_case("c1")])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["policy_id"] == pid
    assert body["unknown_case_ids"] == ["c1"]
    assert body["cases"][0]["status"] == "unknown"
    assert body["cases"][0]["minimal_missing_field_sets"] == [["a"], ["b"]]
    plans = body["plans"]
    assert [p["fields"] for p in plans] == [["a"], ["b"]]
    assert plans[0]["covered"] == [
        {"case_id": "c1", "minimal_missing_field_sets": [["a"]]}
    ]
    assert plans[1]["covered"] == [
        {"case_id": "c1", "minimal_missing_field_sets": [["b"]]}
    ]


def test_decided_cases_need_no_fields_and_never_appear_in_coverage():
    rules = [
        {"id": "r1", "priority": 10, "conclusion": "ok",
         "expr": {"any": [CMP("a", "eq", 1), CMP("b", "eq", 1)]}},
        {"id": "r2", "priority": 1, "conclusion": "other",
         "expr": {"any": [CMP("c", "eq", 1), CMP("d", "eq", 1)]}},
    ]
    pid = create_policy(rules)
    r = post_batch(pid, [
        unknown_case("c1", {}),
        unknown_case("c2", {"a": 1}),  # r1 fires -> already decided
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unknown_case_ids"] == ["c1"]
    assert body["cases"][1]["status"] == "decided"
    assert body["cases"][1]["conclusion"] == "ok"
    assert [p["fields"] for p in body["plans"]] == [["a"], ["b"]]
    assert all(
        item["case_id"] == "c1"
        for plan in body["plans"] for item in plan["covered"]
    )


def test_shared_field_collapses_to_one_minimal_plan():
    # r = any(a==1, b==1, d==1):
    # case 1 (d known false) -> [[a],[b]]; case 2 (b known false) -> [[a],[d]]
    rules = [
        {"id": "r", "priority": 10, "conclusion": "ok",
         "expr": {"any": [
             CMP("a", "eq", 1), CMP("b", "eq", 1), CMP("d", "eq", 1)]}},
    ]
    pid = create_policy(rules)
    r = post_batch(pid, [
        unknown_case("c1", {"d": 0}),
        unknown_case("c2", {"b": 0}),
    ])
    assert r.status_code == 200, r.text
    plans = r.json()["plans"]
    # Unions: {a}+{a}={a}; {a}+{d}, {b}+{a} are strict supersets of {a} and
    # are pruned; {b}+{d} stays.
    assert [p["fields"] for p in plans] == [["a"], ["b", "d"]]
    assert plans[0]["covered"] == [
        {"case_id": "c1", "minimal_missing_field_sets": [["a"]]},
        {"case_id": "c2", "minimal_missing_field_sets": [["a"]]},
    ]
    assert plans[1]["covered"] == [
        {"case_id": "c1", "minimal_missing_field_sets": [["b"]]},
        {"case_id": "c2", "minimal_missing_field_sets": [["d"]]},
    ]


def test_independent_cases_full_cross_product_sorted():
    rules = [
        {"id": "r", "priority": 10, "conclusion": "ok",
         "expr": {"any": [
             CMP("a", "eq", 1), CMP("b", "eq", 1),
             CMP("c", "eq", 1), CMP("d", "eq", 1)]}},
    ]
    pid = create_policy(rules)
    # c1: c,d false -> [[a],[b]]; c2: a,b false -> [[c],[d]].
    payload_cases = [
        unknown_case("c1", {"c": 0, "d": 0}),
        unknown_case("c2", {"a": 0, "b": 0}),
    ]
    r = post_batch(pid, payload_cases)
    assert r.status_code == 200, r.text
    plans = r.json()["plans"]
    assert [p["fields"] for p in plans] == [
        ["a", "c"], ["a", "d"], ["b", "c"], ["b", "d"]
    ]
    # Coverage is keyed by sorted case_id, independent of request order.
    r = post_batch(pid, list(reversed(payload_cases)))
    assert r.json()["plans"] == plans


def test_plans_sorted_by_size_then_field_names():
    # c1 -> [[a],[b]]; c2 -> [[a],[c],[d]]
    rules = [
        {"id": "r", "priority": 10, "conclusion": "ok",
         "expr": {"any": [
             CMP("a", "eq", 1), CMP("b", "eq", 1),
             CMP("c", "eq", 1), CMP("d", "eq", 1)]}},
    ]
    pid = create_policy(rules)
    r = post_batch(pid, [
        unknown_case("c1", {"c": 0, "d": 0}),
        unknown_case("c2", {"b": 0}),
    ])
    assert r.status_code == 200, r.text
    fields = [p["fields"] for p in r.json()["plans"]]
    # {a} dominates every union involving it; remaining: {b,c},{b,d}.
    assert fields == [["a"], ["b", "c"], ["b", "d"]]


def test_no_unknown_cases_returns_single_empty_plan():
    rules = [
        {"id": "r1", "priority": 10, "conclusion": "ok",
         "expr": CMP("a", "eq", 1)},
        {"id": "r2", "priority": 1, "conclusion": "low",
         "expr": CMP("z", "eq", 1)},
    ]
    pid = create_policy(rules)
    r = post_batch(pid, [
        {"case_id": "decided", "facts": {"a": 1}},
        {"case_id": "no-match", "facts": {"a": 0, "z": 0}},
    ])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unknown_case_ids"] == []
    statuses = {c["case_id"]: c["status"] for c in body["cases"]}
    assert statuses == {"decided": "decided", "no-match": "no_match"}
    assert body["count"] == 1
    assert body["plans"] == [{"fields": [], "covered": []}]


def test_conflict_case_is_not_unknown():
    rules = [
        {"id": "yes", "priority": 1, "conclusion": "yes",
         "expr": CMP("v", "ge", 10)},
        {"id": "no", "priority": 1, "conclusion": "no",
         "expr": CMP("v", "ne", 5)},
    ]
    pid = create_policy(rules)
    r = post_batch(pid, [unknown_case("c1", {"v": 10})])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cases"][0]["status"] == "conflict"
    assert body["unknown_case_ids"] == []
    assert body["plans"] == [{"fields": [], "covered": []}]


def test_duplicate_case_id_returns_422():
    pid = create_policy()
    r = post_batch(pid, [unknown_case("dup"), unknown_case("dup")])
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "Policy structure validation failed"
    assert any("dup" in e for e in body["errors"])


def test_validation_errors_return_422():
    pid = create_policy()
    bad_bodies = [
        {},
        {"cases": []},
        {"cases": "nope"},
        {"cases": [{}]},
        {"cases": [{"case_id": "", "facts": {}}]},
    ]
    for body in bad_bodies:
        r = client.post(
            f"/api/v1/policies/{pid}/batch-data-plans", json=body
        )
        assert r.status_code == 422, (body, r.status_code, r.text)
        out = r.json()
        assert out["detail"] == "Policy structure validation failed"
        assert isinstance(out["errors"], list) and out["errors"]

    # A case without a "facts" key defaults to {} and is accepted.
    r = post_batch(pid, [{"case_id": "c1"}])
    assert r.status_code == 200, r.text

    r = post_batch(pid, [
        {"case_id": "c1", "facts": []},
        {"case_id": 1, "facts": {}},
    ])
    assert r.status_code == 422


def test_combination_cap_returns_422_without_truncation():
    rules = [
        {"id": "r", "priority": 10, "conclusion": "ok",
         "expr": {"any": [
             CMP("a", "eq", 1), CMP("b", "eq", 1),
             CMP("c", "eq", 1), CMP("d", "eq", 1)]}},
    ]
    pid = create_policy(rules)
    os.environ[CAP_ENV] = "5"  # full run generates 2 then 4 unions (6 total)
    r = post_batch(pid, [
        unknown_case("c1", {"c": 0, "d": 0}),
        unknown_case("c2", {"a": 0, "b": 0}),
    ])
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "Batch data-plan combination failed"
    assert body["errors"] and "5" in body["errors"][0]


def test_combination_cap_is_configurable():
    rules = [
        {"id": "r", "priority": 10, "conclusion": "ok",
         "expr": {"any": [
             CMP("a", "eq", 1), CMP("b", "eq", 1),
             CMP("c", "eq", 1), CMP("d", "eq", 1)]}},
    ]
    pid = create_policy(rules)
    os.environ[CAP_ENV] = "6"
    r = post_batch(pid, [
        unknown_case("c1", {"c": 0, "d": 0}),
        unknown_case("c2", {"a": 0, "b": 0}),
    ])
    assert r.status_code == 200, r.text
    assert len(r.json()["plans"]) == 4


def test_malformed_json_is_400():
    pid = create_policy()
    r = client.post(
        f"/api/v1/policies/{pid}/batch-data-plans",
        content="{not json",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_unknown_policy_returns_404():
    r = client.post(
        "/api/v1/policies/nope/batch-data-plans",
        json={"cases": [unknown_case("c1")]},
    )
    assert r.status_code == 404


def _run_all():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for test in tests:
        setup_function()
        try:
            test()
            print(f"PASS {test.__name__}")
        except Exception:
            failures += 1
            import traceback
            traceback.print_exc()
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return failures


if __name__ == "__main__":
    sys.exit(1 if _run_all() else 0)
